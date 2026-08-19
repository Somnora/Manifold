"""Configuration loading.

Two sources, strictly separated:
- .env (gitignored) holds secrets: API keys, S3 credentials, Tailscale key.
- config.yaml holds tunables: guardrails, retry policy, SSH settings.

Nothing in this module talks to the network or the database.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .preferences import Preferences, preferences_from_dict

logger = logging.getLogger("manifold.config")

# Repo root is one level above backend/.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Desktop packaging splits files by role (see docs/desktop-build.md):
#
# RESOURCE_ROOT - read-only assets shipped INSIDE the app: templates/,
#   sidecar/, and the exported dashboard (ui/). In a PyInstaller bundle this
#   is the unpack dir (sys._MEIPASS); in development it is the repo root.
#
# DATA_ROOT - mutable, user-owned state: .env, config.yaml, manifold.db,
#   host_keys.json. A packaged app must never write inside its own bundle,
#   so this goes to the platform's app-data dir. In development it stays
#   the repo root, so nothing changes for `uv run uvicorn ...`.

_FROZEN = bool(getattr(sys, "frozen", False))

RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", REPO_ROOT)) if _FROZEN else REPO_ROOT


def _default_data_root() -> Path:
    override = os.environ.get("MANIFOLD_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if not _FROZEN:
        return REPO_ROOT
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Manifold"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "Manifold"
    return Path.home() / ".local" / "share" / "manifold"


DATA_ROOT = _default_data_root()


@dataclass(frozen=True)
class Guardrails:
    max_concurrent_instances: int = 1
    max_hourly_spend_usd: float = 4.00


@dataclass(frozen=True)
class LaunchPolicy:
    max_attempts: int = 5
    backoff_base_seconds: float = 5.0
    backoff_max_seconds: float = 120.0
    fallback_instance_types: tuple[str, ...] = ()
    # SXM4/large multi-GPU instances routinely take 15-30+ minutes to reach
    # 'active' on Lambda's side. 900s (15 min) failed real launches that were
    # still booting; 2400s (40 min) is the observed ceiling with headroom.
    boot_timeout_seconds: float = 2400.0
    boot_poll_seconds: float = 10.0
    # Phase 110: the SECOND window, and deliberately not part of the one
    # above. boot_timeout_seconds covers "the provider says RUNNING"; this
    # covers "SSH answers" -> "cloud-init finished installing the driver,
    # Docker, the NVIDIA runtime and the sidecar", anchored at connected_at.
    # Observed on a stock-Ubuntu GCE T4: RUNNING at ~36s, sshd at ~90s,
    # cloud-init done at ~7 min. 1200s leaves room for a DKMS driver build
    # without folding that tail into the boot budget - doing that would make
    # today's successful 40-minute Lambda boots start failing.
    provisioning_timeout_seconds: float = 1200.0
    # How often the backend sweeps for active instances it has no managed
    # connection to (launched from the Lambda console or a raw API script,
    # or launched while the backend was down). 0 disables the sweep.
    adopt_poll_seconds: float = 30.0


@dataclass(frozen=True)
class SSHSettings:
    key_name: str = ""
    private_key_path: str = "~/.ssh/id_ed25519"
    username: str = "ubuntu"
    connect_timeout_seconds: float = 15.0
    reconnect_base_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    # Detect a silently-dead TCP path fast: ping every interval, drop after
    # this many unanswered pings (~45s at 15s x 3), so the supervisor can
    # reconnect instead of the connection appearing "connected" for the
    # ~15 min it takes the OS to give up.
    keepalive_interval_seconds: float = 15.0
    keepalive_count_max: int = 3
    # Ceiling on a single remote command run over the connection. A stalled
    # NFS mount would otherwise wedge a request (archive/sync/diagnose)
    # forever. Job dispatch streams for hours and passes its own (None).
    command_timeout_seconds: float = 120.0


@dataclass(frozen=True)
class TaskSettings:
    poll_seconds: float = 1.0
    # First-job GPU preflight: on A100 SXM boxes CUDA cannot initialize
    # until nvidia-fabricmanager finishes starting - minutes after boot,
    # while nvidia-smi already looks healthy. The dispatcher probes until
    # the fabric state is settled (bounded by the timeout, then dispatches
    # anyway) instead of burning billed minutes on a doomed container.
    gpu_ready_timeout_seconds: float = 180.0
    gpu_ready_poll_seconds: float = 10.0


@dataclass(frozen=True)
class IdleSettings:
    timeout_seconds: float = 1800.0
    timeout_max_seconds: float = 14400.0
    timeout_min_seconds: float = 300.0
    poll_seconds: float = 15.0
    # Phase 76b, the max-lifetime ceiling (opt-in, NULL = off).
    # The floor is None by default, meaning "the boot budget plus one idle
    # timeout" — see max_lifetime_bounds(). It is NOT idle.timeout_min_seconds:
    # the ceiling is anchored at launch ACCEPTANCE, before boot, and boot is
    # 15-40 minutes on a multi-GPU box, so a 5-minute floor would let someone
    # set a ceiling that destroys an 8xH100 the instant it first connects.
    max_lifetime_min_seconds: float | None = None
    max_lifetime_max_seconds: float = 2_592_000.0        # 30 days
    # How long before the ceiling the user is warned. Fixed lead, not a
    # percentage: 90% of 30 days is three days of nagging, and 90% of 70
    # minutes is seven minutes of notice.
    ceiling_warning_seconds: float = 600.0
    # Phase 94b: GPU utilization that counts as "this box is working",
    # whatever Manifold can see of the traffic driving it. An idle card
    # reports 0-2%, so 10 clears noise without needing the box to be
    # saturated. Set to 0 to switch the telemetry check off entirely and
    # go back to judging solely by Manifold-visible activity.
    busy_util_pct: int = 10


@dataclass(frozen=True)
class WatchSettings:
    poll_seconds: float = 60.0
    auto_launch_enabled: bool = False


@dataclass(frozen=True)
class AutopilotSettings:
    max_steps_default: int = 20
    max_steps_cap: int = 50
    wait_cap_seconds: float = 120.0
    chat_timeout_seconds: float = 300.0
    # How long a run waits on a human Approve/Deny before the pending
    # action auto-denies (the run then adapts; it does not die).
    approval_timeout_seconds: float = 600.0


@dataclass(frozen=True)
class LocalBrainEndpoint:
    name: str = ""
    base_url: str = ""


@dataclass(frozen=True)
class ApiBrain:
    name: str = ""
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""      # env var holding the key (.env; never stored)


# Default local-hub wiring: the two standard local model servers, and the
# three frontier APIs that expose OpenAI-compatible chat endpoints. An API
# brain only appears once its key env var is set (Settings page or .env).
DEFAULT_LOCAL_ENDPOINTS = (
    LocalBrainEndpoint("ollama", "http://127.0.0.1:11434/v1"),
    LocalBrainEndpoint("lmstudio", "http://127.0.0.1:1234/v1"),
)
DEFAULT_API_BRAINS = (
    ApiBrain("claude", "https://api.anthropic.com/v1",
             "claude-sonnet-4-5", "ANTHROPIC_API_KEY"),
    ApiBrain("openai", "https://api.openai.com/v1",
             "gpt-4o", "OPENAI_API_KEY"),
    ApiBrain("gemini",
             "https://generativelanguage.googleapis.com/v1beta/openai",
             "gemini-2.5-pro", "GEMINI_API_KEY"),
)


@dataclass(frozen=True)
class DiagnosticsSettings:
    """What the backend writes down about its own health.

    Exists because an intermittent freeze was reported three times and
    investigated with nothing to go on: uvicorn logs to the terminal it was
    launched in, so every freeze erased its own evidence. See
    diagnostics.py for what each of these answers.
    """
    # Mirror logs into DATA_ROOT/logs/manifold.log (rotating, bounded).
    log_to_file: bool = True
    # Log any request at or over this many seconds, with its path. The
    # dashboard gives up at 30s, so this must be well under that to catch
    # the call it gave up on.
    slow_request_seconds: float = 5.0
    # Log when the event loop oversleeps by this much: the signature of a
    # synchronous call blocking every request at once. 0 disables.
    loop_lag_seconds: float = 1.0


@dataclass(frozen=True)
class HubSettings:
    # Local model servers to probe for brains (Ollama, LM Studio, ...).
    local_endpoints: tuple[LocalBrainEndpoint, ...] = DEFAULT_LOCAL_ENDPOINTS
    # Frontier APIs usable as brains once their key is in .env.
    api_brains: tuple[ApiBrain, ...] = DEFAULT_API_BRAINS
    # Frontier CLIs usable as brains via YOUR OWN login (claude / codex /
    # gemini): each authenticates with the provider's official OAuth, and
    # Manifold just invokes the CLI - no tokens or keys ever touch Manifold.
    cli_brains: tuple[str, ...] = ("claude", "codex", "gemini")
    # The in-dashboard terminal on THIS machine (loopback + origin-checked).
    local_terminal: bool = True
    # How long a terminal session whose browser tab went away (refresh,
    # freeze, crash) keeps its shell alive waiting for a reattach. A refresh
    # reattaches in seconds; a tab closed for good never does, and its shell
    # is reaped after this window instead of leaking.
    terminal_grace_seconds: float = 28800.0


@dataclass(frozen=True)
class StorageSettings:
    """Filesystem-billing visibility (Phase 95).

    Lambda bills filesystems per GB-month for as long as they exist, and
    none of that appears in launch-based spend - a real ~$50/month sat
    invisible in every number this product could report until a manual
    audit found it. There is NO rate in Lambda's API to read, so this is
    the honest compromise: a rate the USER writes down, used only for a
    clearly-labelled estimate that is never folded into the launch totals.
    0 disables the estimate entirely rather than estimating with a number
    nobody vouched for.
    """
    rate_usd_per_gb_month: float = 0.20


@dataclass(frozen=True)
class TelemetrySettings:
    # How often the dispatcher records a GPU telemetry sample per connected
    # instance. Backs the post-run utilization verdict; advisory only.
    sample_seconds: float = 30.0
    # Phase 98: how long samples are kept. 30 days and not shorter because
    # max_lifetime_max_seconds is 30 days - no live launch may outlive its
    # own telemetry (idle-spend accounting reads samples across a launch's
    # whole window). 0 = keep forever. audit_log is NEVER pruned: it is the
    # forensic record, and one night already depended on it.
    retain_days: float = 30.0


@dataclass(frozen=True)
class ServerSettings:
    # Phase 81 (team mode): whether requests arriving on a NON-loopback
    # interface may ride plain http. Default no: a bearer token on a
    # plaintext LAN hop is a credential broadcast. Set true only when the
    # wire is already encrypted below http - a Tailscale/WireGuard tailnet
    # is the canonical case. TLS (uvicorn --ssl-*) needs no opt-in.
    allow_plaintext_lan: bool = False


@dataclass(frozen=True)
class IdleSpendSettings:
    """Idle-spend accounting: how much of a bill ran with the GPUs unused.

    REPORT ONLY, and that is a hard rule rather than a current limitation.
    Nothing here may gate a termination, a launch, or any other destructive
    decision: low utilization is not proof that work is not happening (a
    memory-bound job and a served model between requests both read as idle),
    so this produces a number to look at, never a verdict to act on. Idle
    auto-termination keys on jobs and terminal activity and is deliberately
    unaware of these settings.
    """
    # At or below this MEAN utilization across the box's GPUs, a sample's
    # span counts as idle. The mean, never the max: on an 8-GPU box the max
    # lets one busy card hide seven idle ones, and idle spend would then be
    # under-reported - the one direction a spend-safety number must not err.
    util_pct: float = 5.0
    # Under this much wall clock we decline to judge at all. A two-minute
    # instance is boot plus noise, and an idle fraction of it means nothing.
    min_window_seconds: float = 600.0
    # The `instance_idle` notification needs BOTH of these: idle for at least
    # this long, AND this much money in idle spend. Two gates because either
    # alone misfires - 30 idle minutes on a cheap box is not worth an
    # interruption, and $1 of idle spend on an 8xH100 happens in three.
    notify_after_seconds: float = 1800.0
    notify_usd: float = 1.0


@dataclass(frozen=True)
class AutoManageSettings:
    # How often the auto-manage lifecycle loop advances a job (launch -> run
    # -> sync -> terminate). Modest by default: the only API call it makes is
    # request_launch while a job waits for a free slot; everything else is a
    # local DB read.
    poll_seconds: float = 5.0


@dataclass(frozen=True)
class GCPSettings:
    project_id: str = ""
    default_zone: str = "us-central1-a"
    credentials_file: str = ""


@dataclass(frozen=True)
class Settings:
    # Secrets (from .env). Empty string means "not configured".
    lambda_api_key: str = ""
    # NOTE: `preferences` below holds the DEFAULTS for the user-editable
    # policies (approval gates, notifications, data safety). The user's own
    # choices live in the database and override these; see preferences.py.
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    tailscale_authkey: str = ""
    # Optional: if set, the OpenAI-compatible /v1 proxy requires this as a
    # bearer token. Empty = open (fine for localhost-only single-user use).
    proxy_api_key: str = ""
    # The local API token: when set, every HTTP/WebSocket request must send
    # `Authorization: Bearer <this>` (see auth.py). Empty = no enforcement,
    # which is what mock mode and the test harness rely on; create_app's
    # real path generates one when missing, so production is never open.
    api_token: str = ""
    # Optional HuggingFace token: lets the model-fit preflight read exact
    # sizes for gated repos whose license this account has accepted.
    hf_token: str = ""

    guardrails: Guardrails = field(default_factory=Guardrails)
    launch: LaunchPolicy = field(default_factory=LaunchPolicy)
    ssh: SSHSettings = field(default_factory=SSHSettings)
    tasks: TaskSettings = field(default_factory=TaskSettings)
    idle: IdleSettings = field(default_factory=IdleSettings)
    watches: WatchSettings = field(default_factory=WatchSettings)
    autopilot: AutopilotSettings = field(default_factory=AutopilotSettings)
    hub: HubSettings = field(default_factory=HubSettings)
    telemetry: TelemetrySettings = field(default_factory=TelemetrySettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    diagnostics: DiagnosticsSettings = field(
        default_factory=DiagnosticsSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    idle_spend: IdleSpendSettings = field(default_factory=IdleSpendSettings)
    auto_manage: AutoManageSettings = field(default_factory=AutoManageSettings)
    gcp: GCPSettings = field(default_factory=GCPSettings)
    preferences: "Preferences" = field(default_factory=lambda: Preferences())
    default_connection_mode: str = "direct-ssh"
    db_path: str = str(DATA_ROOT / "manifold.db")


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file, preserving comments and order.

    Existing keys are updated in place; missing keys are appended. Values
    are written verbatim and never logged.
    """
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0] if "=" in stripped else None
        if key and not stripped.startswith("#") and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")


# Migrations for SHIPPED DEFAULTS that later proved wrong in the field. The
# packaged app seeds DATA_ROOT/config.yaml ONCE and never overwrites it (it
# is user-owned), so a corrected default would otherwise never reach an
# existing install - found live when a desktop app still ran the old 900s
# boot timeout and could have cut off a slow SXM boot. Each entry rewrites a
# value ONLY while it still exactly equals the old shipped default: a value
# the user changed never matches and is never touched. Edits are line-level
# regex substitutions so the file's comments survive.
CONFIG_MIGRATIONS: list[tuple[str, str, str]] = [
    (
        r"^(\s*)boot_timeout_seconds:\s*900\s*$",
        r"\g<1>boot_timeout_seconds: 2400",
        "launch.boot_timeout_seconds 900 -> 2400 "
        "(SXM boots routinely exceed 900s; see DECISIONS.md 2026-07-14)",
    ),
]


def apply_config_migrations(text: str) -> tuple[str, list[str]]:
    """Rewrite stale shipped defaults in config text. Pure.

    Returns (new_text, descriptions of what changed); an empty list means
    the text is untouched (user-changed values never match)."""
    applied: list[str] = []
    for pattern, replacement, description in CONFIG_MIGRATIONS:
        new_text, count = re.subn(pattern, replacement, text,
                                  flags=re.MULTILINE)
        if count:
            text = new_text
            applied.append(description)
    return text, applied


def load_settings(
    config_path: Path | None = None, env_path: Path | None = None
) -> Settings:
    """Build Settings from config.yaml + .env under DATA_ROOT (the repo
    root in development, the platform app-data dir in the packaged app)."""
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    load_dotenv(env_path or DATA_ROOT / ".env")

    raw: dict = {}
    path = config_path or DATA_ROOT / "config.yaml"
    if not path.exists() and _FROZEN:
        # First run of the packaged app: seed the user's config from the
        # bundled default so tunables are discoverable and editable.
        bundled = RESOURCE_ROOT / "config.yaml"
        if bundled.exists():
            path.write_text(bundled.read_text())
    if path.exists():
        text = path.read_text()
        text, applied = apply_config_migrations(text)
        if applied:
            # Persist so the fix survives and the user sees the real value
            # when they open the file. Best-effort: a read-only file still
            # gets the migrated values for THIS run via `text` below.
            try:
                path.write_text(text)
            except OSError:
                logger.warning("could not persist config migrations to %s",
                               path)
            for description in applied:
                logger.info("config migration applied to %s: %s",
                            path, description)
        raw = yaml.safe_load(text) or {}

    guard = raw.get("guardrails", {})
    launch = raw.get("launch", {})
    ssh = raw.get("ssh", {})
    conn = raw.get("connection", {})
    database = raw.get("database", {})
    tasks = raw.get("tasks", {})
    idle = raw.get("idle", {})
    watches = raw.get("watches", {})
    autopilot = raw.get("autopilot", {})
    hub = raw.get("hub", {})
    telemetry = raw.get("telemetry", {})
    diagnostics = raw.get("diagnostics", {})
    server = raw.get("server", {})
    idle_spend = raw.get("idle_spend", {})
    auto_manage = raw.get("auto_manage", {})
    gcp = raw.get("gcp", {})
    # Defaults for the Settings-page policies. A garbage value here can never
    # stop the backend from starting: preferences_from_dict ignores what it
    # does not understand and clamps what it does.
    preferences = preferences_from_dict(Preferences(), raw.get("preferences", {}))

    db_path = database.get("path", "manifold.db")
    if not os.path.isabs(db_path):
        db_path = str(DATA_ROOT / db_path)

    return Settings(
        lambda_api_key=os.environ.get("LAMBDA_API_KEY", ""),
        s3_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", ""),
        s3_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", ""),
        tailscale_authkey=os.environ.get("TAILSCALE_AUTHKEY", ""),
        proxy_api_key=os.environ.get("MANIFOLD_PROXY_KEY", ""),
        api_token=os.environ.get("MANIFOLD_API_TOKEN", ""),
        hf_token=os.environ.get("HF_TOKEN", ""),
        guardrails=Guardrails(
            max_concurrent_instances=int(guard.get("max_concurrent_instances", 1)),
            max_hourly_spend_usd=float(guard.get("max_hourly_spend_usd", 4.00)),
        ),
        launch=LaunchPolicy(
            max_attempts=int(launch.get("max_attempts", 5)),
            backoff_base_seconds=float(launch.get("backoff_base_seconds", 5)),
            backoff_max_seconds=float(launch.get("backoff_max_seconds", 120)),
            fallback_instance_types=tuple(launch.get("fallback_instance_types") or ()),
            boot_timeout_seconds=float(launch.get("boot_timeout_seconds", 2400)),
            boot_poll_seconds=float(launch.get("boot_poll_seconds", 10)),
            provisioning_timeout_seconds=float(
                launch.get("provisioning_timeout_seconds", 1200)),
            adopt_poll_seconds=float(launch.get("adopt_poll_seconds", 30)),
        ),
        tasks=TaskSettings(
            poll_seconds=float(tasks.get("poll_seconds", 1.0)),
            gpu_ready_timeout_seconds=float(
                tasks.get("gpu_ready_timeout_seconds", 180)),
            gpu_ready_poll_seconds=float(
                tasks.get("gpu_ready_poll_seconds", 10)),
        ),
        idle=IdleSettings(
            timeout_seconds=float(idle.get("timeout_seconds", 1800)),
            timeout_max_seconds=float(idle.get("timeout_max_seconds", 14400)),
            timeout_min_seconds=float(idle.get("timeout_min_seconds", 300)),
            poll_seconds=float(idle.get("poll_seconds", 15)),
            max_lifetime_min_seconds=(
                float(idle["max_lifetime_min_seconds"])
                if idle.get("max_lifetime_min_seconds") is not None else None),
            max_lifetime_max_seconds=float(
                idle.get("max_lifetime_max_seconds", 2_592_000)),
            ceiling_warning_seconds=float(
                idle.get("ceiling_warning_seconds", 600)),
            # This loader lists every field explicitly, so a new setting that
            # is not named here is silently unreadable from config.yaml: it
            # keeps the dataclass default no matter what the file says. This
            # one shipped documented-but-inert for exactly that reason, which
            # means "set it to 0 to switch the check off" was untrue.
            busy_util_pct=int(idle.get("busy_util_pct", 10)),
        ),
        watches=WatchSettings(
            poll_seconds=float(watches.get("poll_seconds", 60)),
            auto_launch_enabled=bool(watches.get("auto_launch_enabled", False)),
        ),
        autopilot=AutopilotSettings(
            max_steps_default=int(autopilot.get("max_steps_default", 20)),
            max_steps_cap=int(autopilot.get("max_steps_cap", 50)),
            wait_cap_seconds=float(autopilot.get("wait_cap_seconds", 120)),
            chat_timeout_seconds=float(autopilot.get("chat_timeout_seconds", 300)),
            approval_timeout_seconds=float(
                autopilot.get("approval_timeout_seconds", 600)),
        ),
        hub=HubSettings(
            local_endpoints=tuple(
                LocalBrainEndpoint(str(e.get("name", "")),
                                   str(e.get("base_url", "")))
                for e in hub.get("local_endpoints") or []
            ) or DEFAULT_LOCAL_ENDPOINTS,
            api_brains=tuple(
                ApiBrain(str(b.get("name", "")), str(b.get("base_url", "")),
                         str(b.get("model", "")),
                         str(b.get("api_key_env", "")))
                for b in hub.get("api_brains") or []
            ) or DEFAULT_API_BRAINS,
            cli_brains=tuple(
                str(n) for n in hub.get("cli_brains") or []
            ) or ("claude", "codex", "gemini"),
            local_terminal=bool(hub.get("local_terminal", True)),
            terminal_grace_seconds=float(
                hub.get("terminal_grace_seconds", 28800)),
        ),
        telemetry=TelemetrySettings(
            sample_seconds=float(telemetry.get("sample_seconds", 30)),
            retain_days=float(telemetry.get("retain_days", 30)),
        ),
        # Named in the loader or unreadable: this loader lists every field
        # explicitly, and busy_util_pct shipped documented-but-inert for
        # exactly that omission. Do not add a config key without its line here.
        storage=StorageSettings(
            rate_usd_per_gb_month=float(
                (raw.get("storage") or {}).get("rate_usd_per_gb_month", 0.20)),
        ),
        diagnostics=DiagnosticsSettings(
            log_to_file=bool(diagnostics.get("log_to_file", True)),
            slow_request_seconds=float(
                diagnostics.get("slow_request_seconds", 5.0)),
            loop_lag_seconds=float(diagnostics.get("loop_lag_seconds", 1.0)),
        ),
        server=ServerSettings(
            allow_plaintext_lan=bool(server.get("allow_plaintext_lan",
                                                False)),
        ),
        idle_spend=IdleSpendSettings(
            util_pct=float(idle_spend.get("util_pct", 5)),
            min_window_seconds=float(
                idle_spend.get("min_window_seconds", 600)),
            notify_after_seconds=float(
                idle_spend.get("notify_after_seconds", 1800)),
            notify_usd=float(idle_spend.get("notify_usd", 1.0)),
        ),
        auto_manage=AutoManageSettings(
            poll_seconds=float(auto_manage.get("poll_seconds", 5)),
        ),
        preferences=preferences,
        ssh=SSHSettings(
            key_name=str(ssh.get("key_name", "")),
            private_key_path=str(ssh.get("private_key_path", "~/.ssh/id_ed25519")),
            username=str(ssh.get("username", "ubuntu")),
            connect_timeout_seconds=float(ssh.get("connect_timeout_seconds", 15)),
            reconnect_base_seconds=float(ssh.get("reconnect_base_seconds", 1)),
            reconnect_max_seconds=float(ssh.get("reconnect_max_seconds", 30)),
            keepalive_interval_seconds=float(
                ssh.get("keepalive_interval_seconds", 15)),
            keepalive_count_max=int(ssh.get("keepalive_count_max", 3)),
            command_timeout_seconds=float(
                ssh.get("command_timeout_seconds", 120)),
        ),
        gcp=GCPSettings(
            project_id=str(gcp.get("project_id", os.environ.get("GCP_PROJECT_ID", ""))),
            default_zone=str(gcp.get("default_zone", os.environ.get("GCP_DEFAULT_ZONE", "us-central1-a"))),
            credentials_file=str(gcp.get("credentials_file", os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""))),
        ),
        default_connection_mode=str(conn.get("default_mode", "direct-ssh")),
        db_path=db_path,
    )
