"""FastAPI application — the single guarded gateway to Lambda Cloud.

Every client (dashboard, MCP server, future tools) is a thin consumer of
these endpoints; business logic and guards live in the Orchestrator, never
in clients.

Run modes:
- Real:  `uv run uvicorn app.main:create_default_app --factory` with .env set.
- Mock:  same, with MANIFOLD_MOCK=1 — canned Lambda API, in-memory storage,
         fake SSH. Zero live spend, works offline.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import os
import secrets
import shlex
from contextlib import asynccontextmanager
from dataclasses import replace

from fastapi import (
    FastAPI,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .sidecar_client import SidecarError

from .config import DATA_ROOT, Settings, load_settings
from .connections import MockSSHConnection
from .db import Database, utcnow
from .config import update_env_file
from .lambda_api import (
    FilesystemInfo,
    InstanceTypeInfo,
    LambdaAPIError,
    LambdaClient,
    MockLambdaClient,
    RealLambdaClient,
    SwappableLambdaClient,
    UnconfiguredLambdaClient,
    capacity_error,
)
from .agent import Autopilot, find_serving_task
from .auth import (
    ROLES,
    NetworkGuardMiddleware,
    NonceStore,
    PrincipalResolver,
    RoleTable,
    TokenAuthMiddleware,
    current_principal,
    current_role,
    ensure_api_token,
    hash_token,
    role_allows,
    token_matches,
    valid_principal_name,
)
from . import localmodels
from .dispatcher import Dispatcher, ParameterError, coerce_parameters
from .model_client import MockModelClient, ModelClientError
from .notifications import NotificationCenter, os_notify
from .orchestrator import (
    LaunchRejected,
    Orchestrator,
    TerminationBlocked,
    TerminationRefused,
    launch_options,
    launch_progress,
    max_lifetime_bounds,
    validate_max_lifetime,
)
from .preferences import GATEABLE_ACTIONS, PreferenceStore
from . import spend
from .sidecar_client import MockSidecarClient
from .image_checker import MockImageChecker, RealImageChecker
from .storage import MockStorage, S3AdapterStorage, StorageClient
from .task_queue import SQLiteTaskQueue
from .templates import load_templates
from .ide_attach import write_ssh_config_block, get_ide_urls
from .terminal_sessions import (
    WS_SHELL_GONE,
    TerminalSession,
    TerminalSessionManager,
)
from .providers import ProviderRegistry, LambdaProvider, GCPProvider, MockGCPProvider, RealGCPProvider
from .providers.base import ProviderError, ProviderUnavailable
from .subagent_engine import engine, NoHealthyEndpoint, SubagentDispatchError

logger = logging.getLogger("manifold.main")


# How each local shell's teardown ended: "sighup" = the group obeyed the
# hangup (the normal case), "sigkill" = it ignored the hangup past the
# grace window and was force-killed. A rising sigkill count is the smell
# of a rogue utility or wedged CLI living inside the terminals.
TERMINAL_TEARDOWNS = {"sighup": 0, "sigkill": 0}

# Strong references to in-flight reap tasks. asyncio.create_task keeps
# only a weak reference; without this set a pending reap could be
# garbage-collected mid-flight - and a vanished reap means zombies and
# never-fired escalations.
_REAP_TASKS: set = set()


def _replaced_shell_notice(local_cwd=None) -> str:
    """What to print when a session id was asked for and its shell is gone.

    Only printed when the CLIENT said it expected one (?resume=1). Printed
    on every new session id, this told a brand-new tab that "the previous
    shell for this session had ended" when there had never been a previous
    shell - a false report of lost work, on the one screen whose whole job
    is being honest about lost work.

    Reattaching used to hand back a fresh prompt with no explanation, which
    reads as "my work vanished" - the shell had been reaped or had exited,
    and nothing said so. Manifold restores the TERMINAL; it cannot restore a
    process it killed. What it can do is say what happened and point at the
    thing that CAN be resumed.

    Claude Code writes every conversation to
    ~/.claude/projects/<cwd-with-slashes-as-dashes>/<session>.jsonl on the
    machine it ran on, and killing a shell does not touch those files. So a
    lost session is nearly always recoverable, and the user's own report -
    "I lose my entire chat history" - was about not being told where it
    went. Only the directory LISTING is read here; no transcript is opened.
    """
    lines = [
        "\r\n[manifold] The previous shell for this session had ended, so "
        "this is a new one.",
        "[manifold] Anything that was running in it (an agent, a job) "
        "stopped when it ended; see Activity for when and why.",
    ]
    if local_cwd is not None:
        try:
            from pathlib import Path
            encoded = str(Path(local_cwd).resolve()).replace("/", "-")
            transcripts = sorted(
                (Path.home() / ".claude" / "projects" / encoded).glob("*.jsonl"))
        except OSError:
            transcripts = []
        if transcripts:
            lines.append(
                f"[manifold] Claude Code recorded {len(transcripts)} "
                f"conversation(s) in this directory and they survived: "
                f"run  claude --resume  to pick one up.")
    return "\r\n".join(lines) + "\r\n\r\n"


def _end_shell_group(pid: int, *, grace_seconds: float = 5.0,
                     label: str = "", on_escalation=None):
    """End a local terminal shell and everything it spawned, then reap it.

    The escalation ladder: SIGHUP to the whole group first (pty.fork made
    the child a session leader, so its pid doubles as a process-group id -
    killpg catches children the shell left running, where a bare
    kill(pid) let them linger), then a non-blocking waitpid(WNOHANG)
    verification loop for `grace_seconds`, then SIGKILL to the group if
    the hangup was ignored. The reap waitpid()s the child - without it
    EVERY exited local shell stayed a zombie until the backend itself
    exited. Each outcome bumps TERMINAL_TEARDOWNS by which rung ended the
    shell; `on_escalation` (best-effort) fires just before the SIGKILL so
    the caller can record pgid + shell context to the audit trail.

    Returns the reap task (production callers ignore it; tests await it
    for deterministic counter visibility). Safe to call on an
    already-dead shell: signalling errors are ignored and the reap still
    collects the zombie. POSIX only (the local terminal endpoint already
    refuses Windows)."""
    import signal

    def signal_group(sig: int) -> None:
        """killpg, falling back to a direct kill if the group does not
        exist YET: pty.fork's child calls setsid in the child, so a
        teardown racing a just-forked shell (tab opened and instantly
        closed) can beat the setsid - killpg raises ProcessLookupError
        while the process itself is alive and un-signalled."""
        try:
            os.killpg(pid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    signal_group(signal.SIGHUP)

    async def _reap() -> None:
        for _ in range(max(1, int(grace_seconds / 0.1))):
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                TERMINAL_TEARDOWNS["sighup"] += 1
                return                            # already collected
            if done:
                TERMINAL_TEARDOWNS["sighup"] += 1
                return
            await asyncio.sleep(0.1)
        TERMINAL_TEARDOWNS["sigkill"] += 1
        logger.warning(
            "terminal %s (pgid %d) ignored SIGHUP for %.1fs; escalating "
            "to SIGKILL", label or "shell", pid, grace_seconds)
        if on_escalation is not None:
            try:
                on_escalation()
            except Exception:
                pass
        signal_group(signal.SIGKILL)              # it ignored the hangup
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    task = asyncio.get_running_loop().create_task(_reap())
    _REAP_TASKS.add(task)
    task.add_done_callback(_REAP_TASKS.discard)
    return task


class DispatchRequest(BaseModel):
    model: str
    prompt: str
    role: str = "coding"
    tools: list[dict] | None = None


class LaunchRequest(BaseModel):
    instance_type: str
    region: str
    filesystem: str
    # Phase 103: more filesystems to MOUNT beside the primary one, for a run
    # that reads one dataset and writes another. Attach-only: jobs still mount
    # the primary, and extras are reached by absolute /lambda/nfs/<name> path
    # from commands, the file routes and a shell. The orchestrator owns every
    # rule about them (cap, duplicates, region lock) so background callers hit
    # the same wall this one does.
    extra_filesystems: list[str] = Field(default_factory=list)
    connection_mode: str | None = None
    ssh_key_name: str | None = None    # falls back to ssh.key_name in config.yaml
    name: str = Field(default="", max_length=64)
    idle_timeout_seconds: float | None = None
    # Opt-in hard ceiling on total lifetime, measured from the moment the
    # provider ACCEPTS the launch (so it includes boot). None = no ceiling,
    # which is the default and the behaviour every existing client gets.
    max_lifetime_seconds: float | None = None
    # Ceiling on ACTIVE time, anchored at health-check pass (Phase 97):
    # boot and driver reboots never come out of this budget. The absolute
    # max_lifetime above remains the outer bound; either firing terminates
    # through the same rescue-first flow.
    max_active_seconds: float | None = None
    # None = "use the account's default provider" (Phase 102), which is what
    # a client that never heard of the toggle sends. Naming one still wins,
    # and the dashboard always names one. The default lives in preferences
    # and is resolved in the orchestrator, never here: a route that picked
    # the cloud would be a second place the answer is decided.
    provider: str | None = None
    # What this box is for, in the launcher's own words (Phase 94). Shown to
    # everyone who lists instances, so a reader who did not launch it can
    # tell work from waste. Optional: an empty purpose is reported as
    # unattributed rather than filled in with a guess.
    purpose: str = Field(default="", max_length=200)
    # A setup script the box runs once when it comes up (Phase 104): the
    # clone-install-pull dance nobody should have to come back and start by
    # hand. Empty = none, and none is the default. 16 KiB matches the
    # detached-command cap these bytes travel through anyway; over it the
    # request is refused with the number rather than truncated.
    bootstrap: str = Field(default="", max_length=16384)


class ClusterLaunchRequest(BaseModel):
    instance_type: str
    region: str
    filesystem: str
    node_count: int
    connection_mode: str | None = None
    ssh_key_name: str | None = None
    name: str = ""
    # Explicitly 'lambda', NOT the Phase 102 account default: a cluster's
    # nodes have to reach each other, so it stays on one proven cloud until
    # cross-provider clusters are designed. See Orchestrator.launch_cluster.
    # (There is a second, identical ClusterLaunchRequest nested inside
    # create_app next to the /clusters/launch route. FastAPI resolves the
    # annotation against module globals - see the note on PrincipalRequest -
    # so THIS one is what parses the body; the nested copy is dead weight
    # that must be kept in step until someone deletes it.)
    provider: str = "lambda"


class IdleTimeoutRequest(BaseModel):
    idle_timeout_seconds: float | None = None
    provider: str = 'lambda'


class MaxLifetimeRequest(BaseModel):
    max_lifetime_seconds: float | None = None


class PrincipalRequest(BaseModel):
    # Module level, not nested in create_app: `from __future__ import
    # annotations` turns hints into strings that FastAPI resolves against
    # module globals - a closure-local model silently degrades to a query
    # parameter and every POST 422s.
    name: str
    # Phase 80: viewer observes, operator works, admin governs.
    role: str = "operator"
    # Phase 81: ENFORCED hourly ceiling on this principal's attributed
    # burn (None = unlimited). A rate guard, not the advisory wallet.
    max_hourly_spend_usd: float | None = None


class TaskRequest(BaseModel):
    template: str
    parameters: dict = Field(default_factory=dict)
    # Auto-manage (Phase 24): when true, Manifold owns the whole instance
    # lifecycle for this job (launch -> run -> sync -> terminate) using the
    # GPU/region/filesystem below. When false, the job runs on whatever
    # instance is already connected, exactly as before.
    auto_manage: bool = False
    gpu_type: str | None = None
    region: str | None = None
    filesystem: str | None = None
    # Pin a manual job to a specific connected instance (multi-GPU). Omit
    # to take the first free instance. Ignored when auto_manage is set.
    target_instance_id: str | None = None
    # Phase 77: task ids this job runs AFTER. It stays queued until every
    # one succeeds, and settles as 'skipped' if any of them cannot. Must
    # reference tasks that already exist; immutable after enqueue (which is
    # what makes cycles impossible - see DECISIONS.md).
    depends_on: list[str] = Field(default_factory=list)


class CreateFilesystemRequest(BaseModel):
    name: str
    region: str


class WatchRequest(BaseModel):
    instance_type: str
    region: str
    filesystem: str | None = None      # required only for auto_launch
    auto_launch: bool = False


class AgentAuditRequest(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)
    note: str = ""                     # caller-supplied session note
    result: str = ""                   # one-line result summary


class AutopilotRequest(BaseModel):
    goal: str = Field(min_length=4, max_length=4000)
    # Either a full brain ref ("instance:<id>" | "local:<ep>/<model>" |
    # "api:<name>") or the legacy instance id field below.
    brain: str | None = None
    brain_instance_id: str | None = None
    max_steps: int | None = Field(default=None, ge=1)
    # True removes the step cap entirely (stored as max_steps=0): the run
    # ends only via done/cancel/failure. Spend stays bounded by the guards
    # and any approval gates; this only unbounds the TURN count.
    unlimited_steps: bool = False
    # Which actions pause for a human Approve/Deny. None = use the saved
    # policy from Settings (launch-only by default). An explicit list is a
    # per-run override; [] means fully autonomous within the guards.
    approve_actions: list[str] | None = None
    # Legacy (pre-Phase 37) boolean: true gates ALL gateable actions. Only
    # consulted when approve_actions is absent.
    require_approval: bool | None = None


class ApprovalDecision(BaseModel):
    approve: bool


class PreferencesPatch(BaseModel):
    """A partial update; every section and field is optional. Unknown keys
    are ignored rather than rejected (see preferences.py)."""
    approvals: dict | None = None
    notifications: dict | None = None
    data_safety: dict | None = None
    guardrails: dict | None = None
    # {"default_provider": "lambda"|"gcp"|...}: which cloud a launch that
    # names no provider lands on. Validated in the route against the
    # registered providers - preferences.py cannot see the registry, and
    # silently keeping the old value would look like a saved setting.
    providers: dict | None = None
    # {"favorites": ["vllm-serve", ...]}: template names pinned to the top
    # of every template list (Phase 107).
    templates: dict | None = None
    # Every section of Preferences must be listed here. A section that is
    # missing is dropped by model_dump(exclude_none=True) before the handler
    # ever sees it, so PUT returns 200 with the value unchanged - a silent
    # success on a failed write. worklog was in exactly that state until
    # Phase 76c; test_preferences_round_trip_every_section guards it now.
    worklog: dict | None = None
    onboarding: dict | None = None


class NotificationsReadRequest(BaseModel):
    # Omit to mark everything read.
    ids: list[str] | None = None


class CustomTemplateRequest(BaseModel):
    yaml: str = Field(min_length=20, max_length=65536)


class RunCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=8192)
    timeout: float = Field(default=120.0, gt=0, le=600)


class RegisterEndpointRequest(BaseModel):
    port: int = Field(ge=1, le=65535)
    model_id: str = Field(min_length=1, max_length=200)
    # Why this server exists - shown beside the endpoint, lands in the
    # audit row. Same job as a launch purpose.
    note: str = Field(default="", max_length=200)


class SetResearchKeyRequest(BaseModel):
    value: str = Field(min_length=1, max_length=4096)
    note: str = Field(default="", max_length=200)


class RevealResearchKeyRequest(BaseModel):
    # Required at the BACKEND, unlike launch purpose (which stayed
    # backend-optional for old bridges): this endpoint is born with the
    # requirement and has no legacy callers to indulge. A secret handout
    # with an unexplained purpose should not be possible to express.
    purpose: str = Field(min_length=1, max_length=200)


class RunDetachedRequest(BaseModel):
    command: str = Field(min_length=1, max_length=16384)
    # What this work is, in words. Lands in the activity verdict that keeps
    # the idle sweep off the box, so a good note is self-defence.
    note: str = Field(default="", max_length=200)


class RenameRequest(BaseModel):
    # Empty restores the Lambda launch-time name.
    name: str = Field(default="", max_length=64)


class ChatRequest(BaseModel):
    # content may be a string, or OpenAI content-parts (text + image_url)
    # for vision models — the payload is relayed verbatim either way.
    messages: list[dict] = Field(min_length=1)   # [{role, content}, ...]
    max_tokens: int = Field(default=1024, ge=1, le=32768)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    # Tools mode: the backend runs a guarded action loop (browse/read the
    # instance's filesystems, queue jobs) between the user and the model.
    tools: bool = False


class LambdaKeyRequest(BaseModel):
    api_key: str

class GCPConfigRequest(BaseModel):
    # Only the project id is required. ADC (`gcloud auth application-default
    # login`) is the PRIMARY auth path and involves no credentials file, so
    # zone and file are optional - the original required-with-min-length
    # fields made the Settings form unusable for exactly the setup the docs
    # recommend, caught the first time a real GCP project was configured
    # (2026-08-18).
    project_id: str = Field(min_length=4)
    default_zone: str = ""
    credentials_file: str = ""


class S3KeysRequest(BaseModel):
    access_key_id: str = Field(min_length=4)
    secret_access_key: str = Field(min_length=8)


class KeepAliveRequest(BaseModel):
    enabled: bool


class ProjectBriefRequest(BaseModel):
    content: str = Field(max_length=20000)


class AgentHandshakeRequest(BaseModel):
    session_id: str
    protocol: str = "manifold-v1"


class DistillConfigRequest(BaseModel):
    # Module level, not nested in create_app - see PrincipalRequest above for
    # why a closure-local model degrades to a query parameter and 422s.
    # What the student should learn, in plain words, e.g. "distill film-shot
    # tagging into a 3B LoRA that fits an A10".
    spec: str = Field(min_length=10, max_length=4000)
    # The curated training file's bare name under <filesystem>/synthesized
    # (llm-judge's kept-*.jsonl, or llm-synthesize's output).
    dataset: str = Field(min_length=1, max_length=128)
    # Which brain writes the config: any ref from GET /brains.
    brain: str = Field(min_length=3, max_length=200)
    # Optional: pin the student base instead of letting the brain pick one
    # off the shelf. Must still be a shelf entry; the validator checks it.
    student_model: str = Field(default="", max_length=200)


class ModelPullRequest(BaseModel):
    # Module level, not nested in create_app - see PrincipalRequest above.
    # Which running instance to pull through: the persistent filesystem is
    # only reachable over a managed SSH connection, so a pull needs one.
    instance_id: str = Field(min_length=1, max_length=128)
    # The .gguf's bare filename under <filesystem>/models (gguf-quantize's
    # output_name plus .gguf). A filename, never a path: the remote path and
    # the local destination are both built by the backend.
    name: str = Field(min_length=1, max_length=128)


class ModelInstallRequest(BaseModel):
    # A file already in the local library (GET /models/local).
    name: str = Field(min_length=1, max_length=128)
    # What it will answer to in Ollama, and in the brain picker.
    ollama_name: str = Field(default="", max_length=64)
    # Ollama's `create` overwrites an existing name without asking; this
    # makes that the caller's explicit decision rather than a side effect.
    overwrite: bool = False


class AgentContextUpdateRequest(BaseModel):
    workspace_environment: dict | None = None
    active_gpu_connections: dict | None = None
    task_graphs: dict | None = None
    # No session_tokens: secrets belong in .env, never in a session-keyed
    # in-memory store any caller could read back (project hard rules).


def create_app(
    settings: Settings | None = None,
    *,
    lambda_client: LambdaClient | None = None,
    storage_factory=None,          # (FilesystemInfo) -> StorageClient
    connect_fn=None,               # (host) -> coroutine factory, for tests
    sidecar_factory=None,          # (ManagedConnection) -> SidecarClient
    model_client_factory=None,     # (ManagedConnection) -> ModelClient
    image_checker=None,            # ImageChecker; mock mode injects MockImageChecker
    hf_lookup_fn=None,             # async (model_id, token) -> dict|None; model-fit exactness
    lambda_client_factory=None,    # (api_key) -> LambdaClient, for key validation
    notification_sender=None,      # (title, body) -> None; tests record, mock no-ops
    env_path=None,                 # where /settings writes secrets (.env)
    research_keys_path=None,       # research-key vault file (DATA_ROOT/research-keys.env)
    templates_dir=None,
    custom_templates_dir=None,     # user-authored templates (DATA_ROOT/custom-templates)
    mock: bool = False,
    mock_seed_days: int = 0,       # mock mode only: days of fabricated demo history
    policy=None,                   # policy.Policy; None = permissive (tests, mock)
) -> FastAPI:
    settings = settings or load_settings()
    from .policy import PERMISSIVE
    policy = policy or PERMISSIVE
    lambda_client_factory = lambda_client_factory or RealLambdaClient
    from .config import DATA_ROOT, RESOURCE_ROOT
    env_file = env_path if env_path is not None else DATA_ROOT / ".env"
    # Phase 100: the research-key vault. Its OWN file, deliberately not
    # env_file: the handout endpoint reads only this store, so Manifold's
    # own credentials are structurally unreachable through it.
    from .research_keys import ResearchKeyStore, validate_name, validate_value
    research_keys = ResearchKeyStore(
        research_keys_path if research_keys_path is not None
        else DATA_ROOT / "research-keys.env")

    # Image preflight wiring. Mock mode gets the offline approve-everything
    # checker; production (no injected client) verifies against registries.
    # A test harness that injects a lambda_client but no checker gets the
    # preflight switched OFF (None) — tests must never hit the network.
    if image_checker is None:
        if mock:
            image_checker = MockImageChecker()
        elif lambda_client is None:
            image_checker = RealImageChecker()
    # HF exact-size lookup follows the image-checker rule: real only in
    # production (no injected client, not mock); tests never hit the network.
    if hf_lookup_fn is None and not mock and lambda_client is None:
        from .hf_lookup import lookup_weights_gb
        hf_lookup_fn = lookup_weights_gb
    # API-token generation, gated on the SAME production-wiring test as the
    # image checker above: only a real backend with no injected client mints
    # one. Tests (injected client) and mock mode never generate and never
    # write a .env - they stay open, zero-credential. Real mode is therefore
    # never open: no token means mint-and-persist now, or refuse to boot
    # (ensure_api_token raises SystemExit if .env cannot be written).
    if not mock and lambda_client is None and not settings.api_token:
        settings = replace(settings, api_token=ensure_api_token(env_file))

    if mock:
        shared_sidecar = None
        if sidecar_factory is None:
            shared_sidecar = MockSidecarClient()
            sidecar_factory = lambda conn: shared_sidecar  # noqa: E731
        if model_client_factory is None:
            shared_model = MockModelClient()
            model_client_factory = lambda conn: shared_model  # noqa: E731
        lambda_client = lambda_client or MockLambdaClient()
        if storage_factory is None:
            shared = MockStorage()
            storage_factory = lambda fs: shared  # noqa: E731
        if connect_fn is None:
            async def _mock_dial():
                conn = MockSSHConnection()
                # Seed the demo's unpersisted files into the mock SFTP store so
                # a mock-mode rescue really transfers something and the report
                # is honest. Content is a placeholder; the SIZES the policy
                # budgets against come from the sidecar, as in real mode.
                for f in (shared_sidecar.unpersisted if shared_sidecar else []):
                    conn.sftp_files[f"/workspace/ephemeral/{f['path']}"] = (
                        f"[mock] contents of {f['path']}\n".encode()
                    )
                return conn
            connect_fn = lambda host: _mock_dial  # noqa: E731
        # Mock mode must work with ANY configuration: the mock catalog only
        # registers mock keys, so a real key name from config.yaml (e.g.
        # lambda-burst-ed25519) would fail every default-key launch - the
        # auto-manage path hit exactly this at the Phase 35 test pass.
        settings = replace(
            settings, ssh=replace(settings.ssh, key_name="mock-key")
        )
        # Mock isolation (incident 2026-07-17: a mock backend started for
        # screenshots swapped fixture state under a live agent session and
        # reconciled its real in-flight launch against the mock catalog).
        # 1. REFUSE to start while the real database records launches that
        #    may still have paying instances behind them - every client on
        #    this port would silently switch to fixture data mid-session.
        # 2. Fixture state lives in its own database file, so mock can
        #    never read or rewrite real rows even when it does run.
        from pathlib import Path as _Path
        from .db import live_launches
        real_db = _Path(settings.db_path)
        if os.environ.get("MANIFOLD_MOCK_FORCE", "") != "1":
            live = live_launches(str(real_db))
            if live:
                names = ", ".join(
                    f"{l['id']} ({l['requested_type']} in {l['region']}, "
                    f"{l['status']})" for l in live[:5])
                raise SystemExit(
                    f"manifold: refusing to start in mock mode: the real "
                    f"database ({real_db}) has {len(live)} launch(es) that "
                    f"may still be running: {names}. A mock backend here "
                    f"would serve fixture data to every connected client "
                    f"and could strand a paying instance. Terminate them "
                    f"first, or set MANIFOLD_MOCK_FORCE=1 if you are sure."
                )
        settings = replace(
            settings,
            db_path=str(real_db.with_name(
                real_db.stem + "-mock" + (real_db.suffix or ".db"))),
        )
    elif lambda_client is None:
        # Real mode: never crash on a missing key. Start with a placeholder
        # that returns a clear "configure me" error on every call; the
        # Settings page swaps in a real client once a key is validated.
        if settings.lambda_api_key:
            lambda_client = SwappableLambdaClient(
                RealLambdaClient(settings.lambda_api_key)
            )
        else:
            lambda_client = SwappableLambdaClient(UnconfiguredLambdaClient())

    if storage_factory is None:
        def storage_factory(fs: FilesystemInfo) -> StorageClient:
            return S3AdapterStorage(
                region=fs.region,
                bucket=fs.id,
                access_key_id=settings.s3_access_key_id,
                secret_access_key=settings.s3_secret_access_key,
            )

    db = Database(settings.db_path)

    # Demo history (MANIFOLD_MOCK_SEED_DAYS): fabricated launches so the
    # spend page has a past to show in mock mode. Never in real mode — this
    # writes invented dollar amounts, and there is no undo. Both of the
    # seeder's own gates still apply underneath: it refuses without an
    # explicit mock=True, and unless the connection it is handed is open on a
    # '<stem>-mock.db' file (the mock branch above derived exactly that path),
    # so this call site being wrong is not enough to touch the real ledger.
    if mock and mock_seed_days > 0:
        from .mock_seed import register_live_instances, seed_mock_history
        created = seed_mock_history(db, days=mock_seed_days, now_iso=utcnow(),
                                    mock=True, db_path=settings.db_path)
        # The fixture's still-running launch is only honest if the mock cloud
        # lists the instance behind it; it then counts toward the concurrency
        # and budget guards exactly as a real running box would.
        running = register_live_instances(db, lambda_client)
        # Name the instance, not just the count: it holds the single
        # concurrency slot, so the next launch attempt comes back 409 and the
        # rejection alone does not explain why. Name the knob too, so the way
        # to turn the fixture off is in the same line as its consequence.
        logger.info(
            "mock seed (MANIFOLD_MOCK_SEED_DAYS=%d): %d fabricated launch(es). "
            "Fixture instance(s) now RUNNING in the mock cloud: %s. They hold "
            "the concurrency slot and count against the hourly budget exactly "
            "as real boxes would, so your next launch can be refused with a "
            "409 until you terminate them; start without "
            "MANIFOLD_MOCK_SEED_DAYS for an empty demo.",
            mock_seed_days, created,
            ", ".join(running) if running else "none",
        )

    # Preferences: the policies the user edits in Settings (approval gates,
    # notification toggles, data safety). config.yaml supplies the defaults;
    # the DB holds what they changed. Every component reads through the store,
    # so a change takes effect on the next tick with no restart.
    prefs = PreferenceStore(db, settings.preferences)
    # In mock mode and under tests the OS ping is a no-op: a test suite must
    # not spray the user's Notification Center.
    notifier = NotificationCenter(
        db, prefs,
        sender=(notification_sender if notification_sender is not None
                else ((lambda title, body: None) if mock else os_notify)),
    )

    providers = ProviderRegistry()
    providers.register('lambda', LambdaProvider(lambda_client))
    if mock:
        providers.register('gcp', MockGCPProvider())
    else:
        def _local_public_key(_settings=settings) -> str:
            """The local SSH public key, for GCE instance metadata.

            <private_key_path>.pub when it exists; otherwise derived from
            the private key itself (asyncssh is already a dependency). GCE
            takes key MATERIAL, not a registered name like Lambda - and it
            is the same key pair ManagedConnection dials with, so a GCP box
            is reachable the moment it boots."""
            from pathlib import Path
            priv = Path(_settings.ssh.private_key_path).expanduser()
            pub = priv.with_name(priv.name + ".pub")
            try:
                if pub.exists():
                    return pub.read_text().strip()
                import asyncssh
                key = asyncssh.read_private_key(str(priv))
                return key.export_public_key().decode().strip()
            except Exception:
                return ""

        providers.register('gcp', RealGCPProvider(
            settings.gcp.project_id, settings.gcp.default_zone,
            settings.gcp.credentials_file,
            public_key_fn=_local_public_key))
    orchestrator = Orchestrator(
        settings, providers, db,
        connect_fn=connect_fn, sidecar_factory=sidecar_factory,
        model_client_factory=model_client_factory,
        prefs=prefs, notifier=notifier, policy=policy,
    )
    storage_cache: dict[str, StorageClient] = {}

    # Templates come from two places: the bundled set (read-only, ships with
    # the app) and the user's own custom-templates dir (created from the Jobs
    # page or by an agent via MCP). One shared dict is handed to the
    # dispatcher/autopilot/brains, so reloads mutate it IN PLACE and every
    # consumer sees new templates without a restart. User templates win name
    # collisions - overriding a bundled template is a feature, not an error.
    bundled_dir = (templates_dir if templates_dir is not None
                   else RESOURCE_ROOT / "templates")
    custom_dir = (custom_templates_dir if custom_templates_dir is not None
                  else DATA_ROOT / "custom-templates")
    templates: dict = {}
    template_errors: dict = {}
    custom_names: set[str] = set()

    def reload_templates() -> None:
        loaded, errors = load_templates(bundled_dir)
        custom, custom_errors = load_templates(custom_dir)
        loaded.update(custom)
        errors.update({f"custom/{k}": v for k, v in custom_errors.items()})
        templates.clear()
        templates.update(loaded)
        template_errors.clear()
        template_errors.update(errors)
        custom_names.clear()
        custom_names.update(custom)

    reload_templates()

    def save_custom_template_text(yaml_text: str):
        """The ONE validated path for saving a custom template, shared by
        the Jobs-page route, the MCP tool, and the autopilot action. Raises
        TemplateError/YAMLError with the loader's message on a bad template;
        on success the template is on disk and live in the shared dict."""
        from .templates import parse_template
        template = parse_template(yaml_text, source="custom")
        custom_dir.mkdir(parents=True, exist_ok=True)
        (custom_dir / f"{template.name}.yaml").write_text(yaml_text)
        reload_templates()
        return templates[template.name]

    queue = SQLiteTaskQueue(db)
    # The worklog lives next to the database, so dev, tests, and the frozen
    # app each keep their own; the optional mirror dir comes from prefs.
    from pathlib import Path as _P
    from .worklog import Worklog
    worklog = Worklog(_P(settings.db_path).with_name("worklog.md"), prefs)
    # Instance lifetimes are work too (Phase 97): the orchestrator writes
    # one entry per launch when it settles.
    orchestrator.worklog = worklog
    dispatcher = Dispatcher(
        settings, orchestrator, queue, templates, db, lambda_client,
        image_checker=image_checker, notifier=notifier, worklog=worklog,
        prefs=prefs,
    )
    autopilot = Autopilot(settings, orchestrator, queue, templates, db,
                          notifier=notifier, worklog=worklog,
                          template_saver=save_custom_template_text)
    from .brains import BrainRegistry
    brains = BrainRegistry(settings, orchestrator, queue, templates)
    # Shells outlive their WebSocket (a refresh reattaches instead of
    # re-setting up whatever was running); see terminal_sessions.py.
    def _report_reaped_shell(session, detached_seconds: float) -> None:
        """A reaped shell is a destroyed workspace: say so, twice.

        The audit log is the record that survives; the notification is what
        reaches a user who was away - which, by definition of this reaper,
        every one of them was. The tail of the screen goes in the body so
        the row answers "what did I lose" rather than only "something died".
        """
        tail = " ".join(session.tail_text(160).split())[-160:]
        minutes = detached_seconds / 60
        db.record_audit(
            "backend", "terminal_reaped",
            f"{session.id}: shell killed after {minutes:.0f}m detached"
            + (f"; last output: {tail}" if tail else ""))
        notifier.notify(
            "terminal_reaped",
            "A terminal shell was closed",
            f"Session {session.id[:8]} had no viewer for {minutes:.0f} "
            f"minutes, so its shell was ended. Anything running in it "
            f"(including an agent) has stopped."
            + (f" Last output: {tail}" if tail else ""),
            ref=session.id)

    term_sessions = TerminalSessionManager(
        grace_seconds=settings.hub.terminal_grace_seconds,
        on_reap=_report_reaped_shell)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # -- the tombstone (Phase 94c) ---------------------------------------
        # Ask, before anything else, whether the PREVIOUS run stopped on
        # purpose. Until this shipped there was no way to tell: a clean quit
        # bypasses this lifespan via desktop.py's os._exit, so it left the
        # log ending mid-line with no marker - identical to a crash. A
        # 331-second quit on 2026-08-16 was chased for hours as a silent
        # crash for exactly that reason. None means "no prior marker", which
        # is not the same as "it crashed" and must not be reported as one.
        from .liveness import previous_run_ended_cleanly
        clean = previous_run_ended_cleanly(settings.db_path)
        if clean is False:
            logger.warning(
                "the previous backend run stopped without recording a "
                "shutdown - it was killed or it crashed, rather than being "
                "quit. See the log directory for what it was doing.")
            db.record_audit("backend", "backend_died_unrecorded",
                            "the previous run left no shutdown marker")
        db.record_audit("backend", "backend_started",
                        f"pid {os.getpid()}")
        # Re-attach to instances still running on Lambda (e.g. after a
        # backend restart) before starting the loops, so the dispatcher and
        # idle watcher see them immediately. Best-effort; never blocks boot.
        adopted = await orchestrator.adopt_running_instances()
        if adopted:
            logger.info("reconnect-on-startup: adopted %d instance(s)", adopted)
        # A launch left mid-boot by the restart (common under --reload) has a
        # real instance still booting on Lambda; resume its wait so it does not
        # hang in 'booting' forever while it bills. Runs after adopt so already-
        # active launches are just settled, not re-dialed.
        resumed = await orchestrator.resume_pending_launches()
        if resumed:
            logger.info("resumed %d launch(es) left mid-boot", resumed)
        # An agent loop is in-memory; a run left 'running' by a previous
        # process is dead. Say so instead of showing it running forever.
        orphaned = db.fail_orphaned_agent_runs()
        if orphaned:
            logger.info("marked %d orphaned autopilot run(s) failed", orphaned)
        dispatcher.start()
        term_sessions.start()
        # A heartbeat that measures its own oversleep. If a synchronous call
        # ever blocks the event loop, every request stalls at once and each
        # handler still looks innocent on its own; this is the only line
        # that names the real condition. One timer per second.
        lag_task = None
        if settings.diagnostics.loop_lag_seconds > 0:
            from .diagnostics import loop_lag_monitor
            lag_task = asyncio.create_task(loop_lag_monitor(
                threshold_seconds=settings.diagnostics.loop_lag_seconds))
        yield
        # The other half of the tombstone. This runs on a GRACEFUL stop
        # (uvicorn caught SIGTERM/SIGINT, the test harness exited the
        # context). It deliberately does NOT run under desktop.py's
        # os._exit path - that one writes its own row before exiting,
        # because nothing here would get the chance.
        db.record_audit("backend", "backend_stopped", "lifespan shutdown")
        if lag_task is not None:
            lag_task.cancel()
        await autopilot.stop()
        await dispatcher.stop()
        await term_sessions.stop()
        await orchestrator.shutdown()
        await lambda_client.close()
        db.close()

    app = FastAPI(title="Manifold", lifespan=lifespan)
    app.state.orchestrator = orchestrator
    app.state.settings = settings
    app.state.dispatcher = dispatcher
    app.state.terminal_sessions = term_sessions
    app.state.queue = queue
    app.state.brains = brains
    app.state.research_keys = research_keys
    app.state.autopilot = autopilot

    # Single-use download credentials (auth.NonceStore): browser <a>
    # downloads cannot send the Authorization header, and the long-lived
    # token must never ride a query string (uvicorn's access log records
    # the request line, secret and all).
    nonces = NonceStore()
    app.state.download_nonces = nonces
    # Phase 85: where pulled .gguf models live on THIS machine. On app.state
    # so the desktop build (DATA_ROOT outside the repo) and tests can point
    # it elsewhere without reaching into config.
    app.state.model_library = localmodels.library_dir(DATA_ROOT)

    # Phase 79: tokens resolve to NAMED principals. The .env token is
    # "owner"; additional tokens are minted through /principals and land
    # in api_principals as hashes. The resolver is built even when
    # enforcement is off so the routes below exist consistently.
    principal_resolver = PrincipalResolver(settings.api_token, db)

    # Phase 80: the role table is BUILT at the very end of create_app
    # (RoleTable.build walks the finished route table and refuses to boot
    # over an unclassified route). A mutable holder is handed to the
    # middleware now; Starlette instantiates the stack lazily, so the
    # table is populated before the first request can arrive.
    role_table_holder: list = []

    class _Roles:
        """Deferred view over the holder, so the middleware can be
        constructed before the routes exist."""
        @staticmethod
        def role_for(path: str, method: str) -> str | None:
            return (role_table_holder[0].role_for(path, method)
                    if role_table_holder else None)

    if settings.api_token and not mock:
        # Empty token = no middleware: mock mode and the test harness stay
        # a zero-credential demo.
        #
        # `and not mock` because the token OUTLIVES the mode. Real mode mints
        # one and persists it to .env; every later MOCK start then read it
        # back and demanded it, so a single accidental real-mode start
        # permanently gated the "Try it in 90 seconds, no credentials" demo
        # behind a credential. Mock mode has nothing to protect - a mock
        # client, no cloud, no spend - and NetworkGuardMiddleware is
        # installed unconditionally, so an untokened backend still refuses
        # any non-loopback caller. Found by the 2026-08-14 pre-launch audit.
        #
        # Added BEFORE CORS on purpose -
        # add_middleware prepends, so CORS (added last) wraps auth. The
        # other way around, the browser preflight OPTIONS (which never
        # carries Authorization) would 401 before CORS could answer it, and
        # 401s would lack Access-Control-Allow-Origin, which the :3000 dev
        # dashboard reads as a network error instead of the token gate.
        app.add_middleware(
            TokenAuthMiddleware, resolver=principal_resolver, nonces=nonces,
            env_path=str(env_file), roles=_Roles(),
        )

    # The dashboard (Phase 2) runs on localhost:3000 and is the only
    # expected browser client; the backend itself binds to localhost.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Times every request and logs the ones that crossed the threshold, so
    # "No answer after 30s (/instances)" stops being unanswerable after the
    # fact: either the log names the slow endpoint, or it says nothing and
    # the backend was fine while the browser stalled. Registered before the
    # network guard so the guard stays outermost.
    if settings.diagnostics.slow_request_seconds > 0:
        from .diagnostics import measure_request

        @app.middleware("http")
        async def _time_requests(request, call_next):
            return await measure_request(
                call_next, request,
                settings.diagnostics.slow_request_seconds)

    # Phase 81: added LAST = OUTERMOST, and unconditionally. The network
    # policy is judged before any credential conversation: an unauthorized
    # transport gets a refusal, not a 401 inviting it to try a token.
    app.add_middleware(
        NetworkGuardMiddleware,
        token_configured=bool(settings.api_token),
        allow_plaintext=settings.server.allow_plaintext_lan,
    )

    @app.exception_handler(LaunchRejected)
    async def _launch_rejected(request, exc: LaunchRejected):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": exc.detail})

    @app.exception_handler(TerminationBlocked)
    async def _termination_blocked(request, exc: TerminationBlocked):
        from fastapi.responses import JSONResponse
        # 409 with the evidence: the rescue already ran, so this names the
        # files it could NOT save, and `rescue` says what it did save.
        # Clients show both and offer force=true. Never a silent block.
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "blocked": True,
                "instance_id": exc.instance_id,
                "unpersisted_files": exc.files,
                "rescue": exc.report,
            },
        )

    @app.exception_handler(TerminationRefused)
    async def _termination_refused(request, exc: TerminationRefused):
        from fastapi.responses import JSONResponse
        # 409 and, crucially, the OWNER: a refusal that would not say whose
        # box it is leaves the caller exactly where it started, which is
        # how a box gets terminated on a second try instead of a question.
        # purpose is included for the same reason - "Tally extraction run"
        # ends the discussion faster than any policy text.
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "refused": True,
                "instance_id": exc.instance_id,
                "owner": exc.owner,
                "purpose": exc.purpose,
                "caller": exc.caller,
                "override": {"confirm_owner": exc.owner},
            },
        )

    @app.exception_handler(LambdaAPIError)
    async def _lambda_error(request, exc: LambdaAPIError):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=exc.status if exc.status >= 400 else 502,
            content={"detail": f"Lambda API: {exc.message}",
                     "code": exc.code, "suggestion": exc.suggestion},
        )

    # -- meta -------------------------------------------------------------------

    @app.get("/health")
    async def health():
        # version: so a client built from an older tree (a frozen MCP bridge
        # spawned before an upgrade) can NOTICE it is behind. Twice, a tool
        # shipped that a running agent provably needed and could not call -
        # and nothing told it a newer surface existed.
        from .breadcrumb import _version
        return {"status": "ok", "mock": mock, "version": _version()}

    @app.get("/skill", response_class=PlainTextResponse)
    async def agent_skill():
        """The agent onboarding document (docs/manifold-skill.md): how an
        LLM should drive Manifold. Served so agents can fetch it at session
        start (the MCP get_skill tool reads this route)."""
        path = RESOURCE_ROOT / "docs" / "manifold-skill.md"
        if not path.exists():
            raise HTTPException(404, "manifold-skill.md not bundled")
        return path.read_text()

    # -- settings (first-run setup; secrets go to .env, never echoed back) --------

    @app.get("/settings/status")
    async def settings_status():
        """Configuration status only — booleans, never secret values."""
        return {
            "mock": mock,
            "lambda_configured": bool(settings.lambda_api_key),
            "gcp_configured": bool(settings.gcp.project_id),
            "s3_configured": bool(
                settings.s3_access_key_id and settings.s3_secret_access_key
            ),
            "tailscale_available": bool(settings.tailscale_authkey),
            "proxy_protected": bool(settings.proxy_api_key),
            "policy_active": policy.active,
            # Presence only, like everything above: IS a token enforced,
            # never what it is.
            "auth_required": bool(settings.api_token),
            "env_path": str(env_file),
            # The max-lifetime ceiling's bounds, so the launch form can state
            # the real minimum instead of letting the user discover it as a
            # 400. The floor is not arbitrary: the clock starts at launch
            # acceptance, so it has to cover the boot budget.
            "max_lifetime_min_seconds": max_lifetime_bounds(settings)[0],
            "max_lifetime_max_seconds": max_lifetime_bounds(settings)[1],
            "boot_timeout_seconds": settings.launch.boot_timeout_seconds,
        }

    @app.post("/settings/lambda-key")
    async def set_lambda_key(req: LambdaKeyRequest):
        """Validate a Lambda API key against the live API, persist it to
        .env, and hot-swap the running client. The key is never logged,
        audited, or returned."""
        nonlocal settings
        candidate = lambda_client_factory(req.api_key)
        try:
            types = await candidate.list_instance_types()
        except LambdaAPIError as exc:
            await candidate.close()
            raise HTTPException(
                400, f"Lambda rejected this key: {exc.message}"
            )
        except Exception as exc:
            await candidate.close()
            raise HTTPException(502, f"Could not reach Lambda to validate: {exc}")

        update_env_file(env_file, {"LAMBDA_API_KEY": req.api_key})
        settings = replace(settings, lambda_api_key=req.api_key)
        orchestrator.settings = settings
        dispatcher.settings = settings
        if not mock and isinstance(lambda_client, SwappableLambdaClient):
            old = lambda_client.inner
            lambda_client.inner = candidate
            await old.close()
        else:
            # Mock mode keeps serving the demo catalog; the key is saved
            # for the next real-mode start.
            await candidate.close()
        db.record_audit(
            current_principal(), "settings_lambda_key",
            f"Lambda API key validated ({len(types)} instance types visible) "
            f"and saved to .env",
        )
        return {
            "valid": True,
            "instance_types_visible": len(types),
            "applied_live": not mock,
        }

    @app.post("/settings/gcp-config")
    async def set_gcp_config(req: GCPConfigRequest):
        creds = req.credentials_file.strip()
        if creds:
            # A credentials file is the exception (headless installs). If
            # one is named, it must exist NOW: a typo'd path would
            # otherwise surface as an inscrutable SDK error at next boot,
            # long after anyone remembers typing it.
            from pathlib import Path as _Path
            if not _Path(creds).expanduser().is_file():
                raise HTTPException(
                    422, f"credentials file not found: {creds}. Leave it "
                         f"empty to use ADC (gcloud auth "
                         f"application-default login), the normal path.")
        # Only the keys actually provided are written. Writing an empty
        # GOOGLE_APPLICATION_CREDENTIALS would OVERRIDE working ADC with a
        # broken pointer - the SDK treats the variable's presence as an
        # instruction.
        updates = {"GCP_PROJECT_ID": req.project_id.strip()}
        if req.default_zone.strip():
            updates["GCP_DEFAULT_ZONE"] = req.default_zone.strip()
        if creds:
            updates["GOOGLE_APPLICATION_CREDENTIALS"] = creds
        update_env_file(env_file, updates)
        db.record_audit(
            current_principal(), "settings_gcp_config",
            f"GCP configuration saved to .env (project "
            f"{req.project_id.strip()}); takes effect on next backend start",
        )
        return {"valid": True, "applied_live": False}

    @app.post("/settings/s3-keys")
    async def set_s3_keys(req: S3KeysRequest):
        """Persist S3-adapter credentials to .env. Validated against the
        first filesystem when one is visible; saved either way."""
        nonlocal settings
        validated = False
        try:
            filesystems = await lambda_client.list_filesystems()
        except LambdaAPIError:
            filesystems = []
        if filesystems and not mock:
            probe = S3AdapterStorage(
                region=filesystems[0].region,
                bucket=filesystems[0].id,
                access_key_id=req.access_key_id,
                secret_access_key=req.secret_access_key,
            )
            try:
                await run_in_threadpool(probe.list_files, "")
                validated = True
            except Exception as exc:
                raise HTTPException(
                    400,
                    f"S3 adapter rejected these keys against filesystem "
                    f"'{filesystems[0].name}': {str(exc)[:200]}",
                )
        update_env_file(env_file, {
            "S3_ACCESS_KEY_ID": req.access_key_id,
            "S3_SECRET_ACCESS_KEY": req.secret_access_key,
        })
        settings = replace(
            settings,
            s3_access_key_id=req.access_key_id,
            s3_secret_access_key=req.secret_access_key,
        )
        orchestrator.settings = settings
        dispatcher.settings = settings
        storage_cache.clear()   # rebuild storage clients with the new keys
        db.record_audit(
            current_principal(), "settings_s3_keys",
            f"S3 adapter keys saved to .env "
            f"({'validated against a filesystem' if validated else 'not validated: no filesystem visible'})",
        )
        return {"saved": True, "validated": validated}

    # -- preferences (the Settings-page policies; not secrets) ---------------------

    @app.get("/preferences")
    async def get_preferences():
        """The three policies the Settings page edits, plus the vocabulary a
        client needs to render them (so the UI never hardcodes the lists)."""
        from .preferences import NOTIFICATION_KINDS
        return {
            "preferences": prefs.get().to_dict(),
            "gateable_actions": list(GATEABLE_ACTIONS),
            "notification_kinds": list(NOTIFICATION_KINDS),
            # The legal values for preferences.providers.default_provider:
            # the clouds THIS backend registered, so the Settings control
            # can never offer a name the launch path would refuse.
            "registered_providers": sorted(
                n for n, _ in orchestrator.providers.items()),
            # What the guardrails fall back to when unset (0) here - the
            # Settings page shows these as placeholders.
            "guardrail_defaults": {
                "max_concurrent_instances":
                    settings.guardrails.max_concurrent_instances,
                "max_hourly_spend_usd":
                    settings.guardrails.max_hourly_spend_usd,
            },
        }

    @app.put("/preferences")
    async def update_preferences(patch: PreferencesPatch):
        # The default provider is the one preference whose legal values are
        # not knowable inside preferences.py: they are the providers THIS
        # backend registered. Checked here, where the registry is in reach,
        # and REFUSED rather than clamped - a silently-ignored write would
        # read as "saved" and then send every default launch to the old
        # cloud. The orchestrator checks again at launch time.
        requested = (patch.providers or {}).get("default_provider")
        if requested is not None:
            registered = sorted(n for n, _ in orchestrator.providers.items())
            if requested not in registered:
                raise HTTPException(
                    422,
                    f"Unknown provider '{requested}'. Registered providers: "
                    f"{', '.join(registered) or '(none)'}.")
        updated = prefs.update(patch.model_dump(exclude_none=True))
        db.record_audit(
            current_principal(), "preferences_update",
            f"approvals={sorted(updated.approvals.gated_actions())} "
            f"data_safety.to_local={updated.data_safety.to_local} "
            f"data_safety.if_unsaveable={updated.data_safety.if_unsaveable} "
            f"providers.default={updated.providers.default_provider}",
        )
        return {"preferences": updated.to_dict()}

    # -- notifications --------------------------------------------------------------

    @app.get("/notifications")
    async def list_notifications(unread_only: bool = False, limit: int = 50):
        return {
            "notifications": notifier.list(unread_only=unread_only, limit=limit),
            "unread": notifier.unread_count(),
        }

    @app.post("/notifications/read")
    async def mark_notifications_read(req: NotificationsReadRequest):
        return {"marked": notifier.mark_read(req.ids)}

    @app.delete("/notifications")
    async def clear_notifications():
        return {"cleared": notifier.clear()}

    # -- agent context -------------------------------------------------------------

    from .agent_context import agent_contexts

    @app.post("/agent/handshake")
    async def agent_handshake(req: AgentHandshakeRequest):
        context = agent_contexts.create_context(req.session_id)
        # Handle protocol if needed
        return {"status": "ok", "context": context.to_dict()}

    @app.get("/agent/context/{session_id}")
    async def get_agent_context_route(session_id: str):
        context = agent_contexts.get_context(session_id)
        if not context:
            raise HTTPException(404, "Context not found")
        return context.to_dict()

    @app.post("/agent/context/{session_id}/update")
    async def update_agent_context_route(session_id: str, req: AgentContextUpdateRequest):
        updates = req.model_dump(exclude_none=True)
        context = agent_contexts.update_context(session_id, updates)
        if not context:
            raise HTTPException(404, "Context not found")
        return {"status": "ok", "context": context.to_dict()}

    # -- instances ----------------------------------------------------------------

    async def _catalog_for(provider: str) -> dict:
        """The instance-type catalog for ONE provider, one response shape.

        The dashboard has sent ?provider= since the GCP toggle appeared, and
        this route ignored it - so selecting Google Cloud showed LAMBDA's
        catalog, prices and availability with Google's name above it. In a
        product whose rule is that a number on a spend screen is provider
        data or absent, that was the worst available bug. Found by the owner
        toggling the two and seeing identical lists (2026-08-14).

        Lambda keeps its original, field-complete path. Everything else goes
        through the provider registry, whose GCP stub returns an EMPTY
        catalog in real mode - "you cannot launch GCP types yet" said as
        data - and a small, clearly-GCP-shaped catalog in mock mode.
        """
        if provider == "lambda":
            types = await lambda_client.list_instance_types()
            return {
                name: {
                    "description": t.description,
                    "gpu_description": t.gpu_description,
                    "price_usd_per_hour": t.price_cents_per_hour / 100,
                    "specs": t.specs,
                    "regions_with_capacity": t.regions_with_capacity,
                }
                for name, t in sorted(types.items())
            }
        try:
            cloud = orchestrator.providers.get_provider(provider)
        except ValueError:
            known = ", ".join(sorted(n for n, _ in orchestrator.providers.items()))
            raise HTTPException(
                422, f"Unknown provider '{provider}'. Registered: {known}.")
        specs = await cloud.list_instance_types()
        basis = ""
        if provider == "gcp":
            from .providers.gcp_catalog import PRICE_BASIS
            basis = PRICE_BASIS
        return {
            t.name: {
                "description": t.description,
                "gpu_description": (
                    f"{t.gpu_type} ({t.gpu_ram_gb} GB)"
                    if t.gpus and t.gpu_type else "N/A"),
                "price_usd_per_hour": t.price_cents_per_hour / 100,
                # Dated list price, not a live meter: the label rides every
                # entry so no screen can show the number without it.
                **({"price_basis": basis} if basis else {}),
                # storage size is not part of the cross-provider spec;
                # 0 is honest here where a guess would not be.
                "specs": {"vcpus": t.vcpus, "memory_gib": t.ram_gb,
                          "storage_gib": 0, "gpus": t.gpus},
                "regions_with_capacity": list(t.regions_available),
            }
            for t in sorted(specs, key=lambda x: x.name)
        }

    @app.get("/instance-types")
    async def instance_types(provider: str = "lambda"):
        return await _catalog_for(provider)

    @app.get("/gpu-guide")
    async def gpu_guide_route(provider: str = "lambda"):
        """The hardware ladder: curated notes joined to live catalog numbers.

        Words from gpu_guide.py, numbers from the provider via the SAME
        catalog call /instance-types serves - so a price shown here can
        never disagree with the launch form beside it, and there is no
        second price path to go stale.
        """
        from . import gpu_guide
        return gpu_guide.build_guide(await _catalog_for(provider))

    @app.get("/gcp/quota")
    async def gcp_quota(region: str = ""):
        """GPU quota for the operator's GCP project, global + one region.

        Fresh projects hold ZERO GPU quota, and that - not code - is what
        blocks a first GCP launch. The launch form shows this number before
        the click; the error after the click links the request page."""
        cloud = orchestrator.providers.get_provider("gcp")
        fetch = getattr(cloud, "gpu_quota", None)
        if fetch is None:
            return {"quotas": [], "project": ""}
        from .providers.gcp_catalog import zone_to_region
        target = zone_to_region(region) if region else "us-central1"
        try:
            rows = await fetch(target)
        except ProviderUnavailable as exc:
            raise HTTPException(503, str(exc))
        except ProviderError as exc:
            raise HTTPException(502, str(exc))
        return {"quotas": rows,
                "project": getattr(cloud, "project_id", ""),
                "request_url": (
                    f"https://console.cloud.google.com/iam-admin/quotas"
                    f"?project={getattr(cloud, 'project_id', '')}")}

    @app.get("/launch-options")
    async def launch_options_route(provider: str | None = None):
        """Launchable (type, region, filesystem) targets one cloud can
        satisfy right now, ranked so options co-located with the user's
        existing data come first. The launch form and any agent use this to
        pick an available, co-located target instead of guessing a region.

        Which cloud: the account's default provider (Phase 102) unless
        ?provider= names another. Agents are taught to copy a target
        straight into launch_gpu, so after a default flip this route MUST
        follow - Lambda targets against a GCP default are a list of
        guaranteed rejections. Every row names its own provider, and so
        does the response.
        """
        try:
            resolved = orchestrator.resolve_provider(provider)
        except LaunchRejected as exc:
            # Same shape /instance-types uses for an unknown provider.
            raise HTTPException(422, exc.detail)
        cloud = orchestrator.providers.get_provider(resolved)
        if resolved == "lambda":
            types = await lambda_client.list_instance_types()
            filesystems = await lambda_client.list_filesystems()
        else:
            # Persistent filesystems are a Lambda feature, and request_launch
            # refuses one on any other provider - so every target here is
            # honestly scratch-only rather than pretending co-location.
            filesystems = []
            try:
                specs = await cloud.list_instance_types()
            except ProviderUnavailable as exc:
                raise HTTPException(503, str(exc))
            except ProviderError as exc:
                raise HTTPException(502, str(exc))
            # Same adaptation /instance-types makes for a non-Lambda
            # catalog, so a GPU reads identically on both routes.
            types = {
                spec.name: InstanceTypeInfo(
                    name=spec.name,
                    description=spec.description,
                    gpu_description=(
                        f"{spec.gpu_type} ({spec.gpu_ram_gb} GB)"
                        if spec.gpus and spec.gpu_type else "N/A"),
                    price_cents_per_hour=spec.price_cents_per_hour,
                    specs={"vcpus": spec.vcpus, "memory_gib": spec.ram_gb,
                           "storage_gib": 0, "gpus": spec.gpus},
                    regions_with_capacity=list(spec.regions_available),
                )
                for spec in specs
            }
        options = launch_options(types, filesystems, provider=resolved)
        options["provider"] = resolved
        # An empty target list from a cloud nobody has set up yet would read
        # as "no capacity anywhere". Present ONLY when there is a reason, so
        # its absence still means what it always meant.
        reason = cloud.unconfigured_reason()
        if reason:
            options["unavailable_reason"] = reason
        # Agents act on this data: fixture state must be self-identifying
        # (an agent once had to spot a TEST-NET IP to detect mock mode).
        options["mock"] = mock
        return options

    @app.get("/regions")
    async def list_regions(provider: str = "lambda"):
        if provider != "lambda":
            # A provider's region list is ITS OWN: for GCP these are zones,
            # taken from the same live catalog the launch form shows, so
            # the dropdown can never offer a place the shelf is not.
            zones: list[str] = []
            for rung in (await _catalog_for(provider)).values():
                for z in rung["regions_with_capacity"]:
                    if z not in zones:
                        zones.append(z)
            return {"regions": [{"code": z, "name": z} for z in sorted(zones)]}
        """The full region universe with human names, so the launch form can
        show every region and grey out the ones a chosen GPU can't use.

        Order: the known NA regions east->west first, then any extra region
        the live catalog reports (named if we know it, else its code). If the
        Lambda client is unconfigured, we still return the static NA set."""
        from .lambda_api import KNOWN_REGIONS, REGION_NAMES
        codes = list(KNOWN_REGIONS)
        try:
            types = await lambda_client.list_instance_types()
            for t in types.values():
                for code in t.regions_with_capacity:
                    if code not in codes:
                        codes.append(code)
        except LambdaAPIError:
            pass  # unconfigured/unreachable: the static NA set is still useful
        return {
            "regions": [
                {"code": c, "name": REGION_NAMES.get(c, c)} for c in codes
            ]
        }

    @app.post("/instances", status_code=202)
    async def launch_instance(req: LaunchRequest):
        launch = await orchestrator.request_launch(
            instance_type=req.instance_type,
            region=req.region,
            filesystem=req.filesystem,
            extra_filesystems=req.extra_filesystems,
            connection_mode=req.connection_mode,
            ssh_key_name=req.ssh_key_name,
            name=req.name,
            idle_timeout_seconds=req.idle_timeout_seconds,
            max_lifetime_seconds=req.max_lifetime_seconds,
            max_active_seconds=req.max_active_seconds,
            provider=req.provider,
            created_by=current_principal(),
            purpose=req.purpose,
            bootstrap=req.bootstrap,
        )
        return {"launch": launch}

    @app.post("/instances/{instance_id}/idle-timeout")
    async def set_idle_timeout(instance_id: str, req: IdleTimeoutRequest):
        """Update the per-instance idle timeout. clamped to min/max."""
        launch = db.find_launch_by_instance(instance_id)
        if not launch:
            raise HTTPException(404, f"No launch found for instance {instance_id}")
        
        value = req.idle_timeout_seconds
        if value is not None:
            value = max(
                settings.idle.timeout_min_seconds,
                min(value, settings.idle.timeout_max_seconds)
            )
        
        db.update_launch(launch["id"], idle_timeout_seconds=value)
        if value is not None:
            db.record_audit(current_principal(), "idle_timeout_update", f"{instance_id} timeout set to {value}s")
        else:
            db.record_audit(current_principal(), "idle_timeout_update", f"{instance_id} timeout restored to default")
            
        return {"idle_timeout_seconds": value}

    @app.post("/instances/{instance_id}/max-lifetime")
    async def set_max_lifetime(instance_id: str, req: MaxLifetimeRequest):
        """Set (or clear) this instance's maximum total lifetime.

        The bound is REJECTED, never silently clamped, and it is the same
        bound the launch path applies — one definition, in the orchestrator,
        so the two write paths cannot drift apart and leave a hole.
        """
        launch = db.find_launch_by_instance(instance_id)
        if not launch:
            raise HTTPException(404, f"No launch found for instance {instance_id}")
        value = validate_max_lifetime(settings, req.max_lifetime_seconds)
        db.update_launch(launch["id"], max_lifetime_seconds=value)
        db.record_audit(
            current_principal(), "max_lifetime_update",
            f"{instance_id} max lifetime "
            + (f"set to {value:.0f}s (from launch acceptance, boot included)"
               if value is not None else "removed; no ceiling"))
        return {"max_lifetime_seconds": value}

    @app.get("/ssh-keys")
    async def list_ssh_keys():
        keys = await lambda_client.list_ssh_keys()
        return {
            "ssh_keys": [k.name for k in keys],
            "default": settings.ssh.key_name,
        }

    @app.get("/instances")
    async def list_instances():
        from . import bootstrap as boot
        instances = await orchestrator.instances_with_state()
        # Hoisted: ONE query for the whole fleet, not one per instance inside
        # the loop. This route is the hottest in the app (the home page polls
        # it every 2s), and an N+1 here would put a query per box behind every
        # tick — the shape of the pile-up Phase 93 had to undo.
        latest_telemetry = db.latest_telemetry([i["id"] for i in instances])
        for inst in instances:
            # Loaded ONCE and shared by the three helpers below: this runs on
            # a poll x N instances, and the same row answering all three
            # questions is both cheaper and impossible to get inconsistent.
            launch = db.find_launch_by_instance(inst["id"])
            # Idle auto-termination countdown + keep-alive switch state, so
            # the card can warn BEFORE the dispatcher acts (a live instance
            # vanished mid-test-session with no warning; never again).
            inst["idle"] = (
                dispatcher.idle_status(inst["id"])
                if inst["connection_state"] == "connected" else None
            )
            # WHY the sweep judges it that way (Phase 94). idle_seconds alone
            # cannot distinguish a model loading its weights from a box
            # nobody wants — an agent read "up 6 min, no user processes" as
            # abandoned and terminated a vLLM instance 60 seconds from
            # serving. The dispatcher already knew better; it just had no
            # way to say so. Unconditional, unlike idle: "unreachable" is
            # itself the answer a reader needs most.
            inst["activity"] = dispatcher.activity_status(inst["id"], launch)
            # The last GPU reading the dispatcher recorded, or None if this box
            # has never been sampled. None and not a row of zeroes: a reader
            # must be able to tell "never measured" from "measured, idle", and
            # `at` rides along so a stale sample cannot be drawn as a live one.
            # This is the same data the dispatcher already collects every 30s,
            # served from SQLite - NOT a second live stream. A live read costs
            # an SSH port-forward per instance every 2 seconds, which is
            # affordable for one focused chart and not for a fleet list.
            inst["telemetry"] = latest_telemetry.get(inst["id"])
            # The max-lifetime ceiling sits on the INSTANCE, deliberately not
            # inside inst["idle"]: idle is None for a box that is not
            # connected, and a box that has dropped off SSH while past its
            # ceiling is precisely the one whose limit the user needs to see.
            inst.update(dispatcher.ceiling_status(inst["id"], launch))
            # The launch bootstrap (Phase 104), read off the detached row -
            # no probe, because this route is polled every 2s per instance.
            # The key is ABSENT for a launch that had no script and for one
            # whose script has not started yet: "we have nothing to say" is
            # not one of the four states and is not invented into one.
            if launch and launch.get("bootstrap"):
                report = boot.report(
                    db.find_detached_by_note(boot.note_for(launch["id"])),
                    connected=inst["connection_state"] == "connected")
                if report is not None:
                    inst["bootstrap"] = report
        # Agents act on this data: fixture state must be self-identifying
        # (an agent once had to spot a TEST-NET IP to detect mock mode).
        return {"instances": instances, "mock": mock}

    @app.post("/instances/{instance_id}/keep-alive")
    async def set_keep_alive(instance_id: str, req: KeepAliveRequest):
        """Switch idle auto-termination off (enabled=true) or back on."""
        return dispatcher.set_keep_alive(instance_id, req.enabled)

    @app.post("/instances/{instance_id}/name")
    async def rename_instance(instance_id: str, req: RenameRequest):
        """Set the display name Manifold shows for this instance. Lambda
        fixes the real name at launch, so this is a local overlay; an empty
        name restores Lambda's."""
        db.set_instance_name(instance_id, req.name.strip())
        db.record_audit(current_principal(), "instance_renamed",
                        f"{instance_id} -> {req.name.strip()!r}")
        return {"instance_id": instance_id, "name": req.name.strip()}

    @app.delete("/instances/{instance_id}")
    async def terminate_instance(instance_id: str, force: bool = False,
                                 confirm_owner: str = ""):
        # caller is passed HERE and not read inside the orchestrator, because
        # the background loops that also call terminate() (idle sweep,
        # ceiling) act for the system rather than a principal and must not be
        # ownership-checked. A request, by definition, has someone behind it.
        return await orchestrator.terminate(
            instance_id, force=force, caller=current_principal(),
            confirm_owner=confirm_owner)

    @app.post("/instances/{instance_id}/sync")
    async def sync_instance(instance_id: str):
        return await orchestrator.sync_ephemeral(instance_id)

    @app.post("/instances/{instance_id}/rescue")
    async def rescue_instance(instance_id: str):
        """Run the data-safety policy NOW, without terminating: save this
        instance's ephemeral files to the persistent volume and/or pull them
        down to this machine. The same code termination runs — so the report
        you get here is exactly what a termination would do."""
        return {"rescue": await orchestrator.rescue(instance_id)}

    @app.post("/instances/{instance_id}/ide-attach")
    async def attach_ide(instance_id: str):
        conn = orchestrator.connections.get(instance_id)
        if conn is None or conn.ssh_connection() is None:
            raise HTTPException(409, f"no connected instance {instance_id}")

        # The IdentityFile is the configured SSH private key (config.yaml
        # ssh.private_key_path, default ~/.ssh/id_ed25519). The known-hosts
        # file is the TOFU pin store the ConnectionManager writes next to the
        # database (orchestrator builds HostKeyStore at the same path), so the
        # IDE's ssh verifies the host against the key Manifold already pinned.
        key_path = os.path.expanduser(settings.ssh.private_key_path)
        host_keys_path = os.path.join(
            os.path.dirname(settings.db_path), "host_keys.json")
        write_ssh_config_block(
            instance_id=instance_id,
            host_ip=conn.host,
            ssh_key_path=key_path,
            host_keys_file_path=host_keys_path,
        )

        db.record_audit(current_principal(), "ide_attach", f"Generated SSH config block for instance {instance_id}")
        return get_ide_urls(instance_id)

    @app.post("/instances/{instance_id}/run")
    async def run_instance_command(instance_id: str, req: RunCommandRequest):
        """Run one command on the instance over the managed SSH connection.

        This is the SSH-parity endpoint for agents: everything a shell could
        do, but through the guarded gateway, so every command lands in the
        audit log with its exit code. Bounded by a hard timeout; output is
        capped so a runaway command cannot flood the response. Long-running
        work belongs in a job (run_job streams logs and survives restarts) -
        this is for the quick, real commands in between.
        """
        conn = orchestrator.connections.get(instance_id)
        if conn is None or conn.ssh_connection() is None:
            raise HTTPException(
                409, f"no connected instance {instance_id}")
        dispatcher.touch_activity(instance_id)
        try:
            exit_code, stdout, stderr = await conn.run(
                req.command, timeout=req.timeout)
        except ConnectionError as exc:
            raise HTTPException(409, str(exc))
        db.record_audit(
            current_principal(), "instance_command",
            f"{instance_id}: {req.command[:200]!r} -> exit {exit_code}",
        )
        cap = 64 * 1024
        return {
            "instance_id": instance_id,
            "exit_code": exit_code,
            "stdout": stdout[-cap:],
            "stderr": stderr[-cap:],
            "truncated": len(stdout) > cap or len(stderr) > cap,
        }

    @app.post("/instances/{instance_id}/run-detached", status_code=202)
    async def run_detached_command(instance_id: str, req: RunDetachedRequest):
        """Start long work that outlives this request - and the backend.

        The command is written VERBATIM to a script on the box over SFTP
        (file bytes, never interpolated into a shell line), launched under
        setsid with a wrapper that records the exit code on completion.
        While it runs, the telemetry loop's probe counts it as activity, so
        the box protects itself from the idle sweep without anyone
        remembering to poll or set keep-alive. Poll GET
        /instances/{id}/detached/{handle} for status and the log tail.
        """
        from . import detached as det
        conn = orchestrator.connections.get(instance_id)
        if conn is None or conn.ssh_connection() is None:
            raise HTTPException(409, f"no connected instance {instance_id}")
        # The write-launch-parse-register sequence lives in detached.py, so
        # the launch-bootstrap sweep runs the identical one (Phase 104).
        try:
            started = await det.start_detached(
                conn, db, instance_id=instance_id, command=req.command,
                note=req.note, created_by=current_principal())
        except ConnectionError as exc:
            raise HTTPException(409, str(exc))
        except det.DetachedStartRejected as exc:
            raise HTTPException(exc.status, exc.message)
        handle, pid = started["handle"], started["pid"]
        dispatcher.touch_activity(instance_id)
        db.record_audit(
            current_principal(), "detached_started",
            f"{instance_id}: {handle} pid {pid} "
            f"{(req.note or req.command)[:160]!r}")
        return {"handle": handle, "instance_id": instance_id, "pid": pid,
                "log_path": f"~/.manifold/detached/{handle}.log"}

    @app.get("/instances/{instance_id}/detached/{handle}")
    async def detached_status(instance_id: str, handle: str):
        """Where a detached command stands: running | exited | vanished |
        unreachable. Unreachable is a state of the CONNECTION, not of the
        command - a box we cannot probe is never reported as stopped."""
        from . import detached as det
        if not det.HANDLE_RE.match(handle):
            raise HTTPException(404, f"no detached command {handle!r}")
        row = db.get_detached(handle)
        if row is None or row["instance_id"] != instance_id:
            raise HTTPException(404, f"no detached command {handle!r} "
                                     f"on {instance_id}")

        def payload(state: str, exit_code, log_tail):
            return {
                "handle": handle, "instance_id": instance_id, "state": state,
                "exit_code": exit_code, "started_at": row["started_at"],
                "command": row["command"], "note": row["note"],
                "created_by": row["created_by"], "log_tail": log_tail,
            }

        conn = orchestrator.connections.get(instance_id)
        reachable = conn is not None and conn.ssh_connection() is not None
        if row["exited_at"] is not None and not reachable:
            # Settled in the registry; the box (and its log) may be gone.
            state = "exited" if row["exit_code"] is not None else "vanished"
            return payload(state, row["exit_code"], None)
        if not reachable:
            return payload("unreachable", None, None)
        try:
            _code, stdout, _err = await conn.run(
                det.probe_line(handle), timeout=20.0)
        except ConnectionError:
            return payload("unreachable", None, None)
        state, exit_code, log_tail = det.parse_probe(stdout)
        if state == "running":
            dispatcher.touch_activity(instance_id)
        elif state in ("exited", "vanished") and row["exited_at"] is None:
            db.finish_detached(handle,
                               exit_code if state == "exited" else None)
            # This poll may be the FIRST place a bootstrap's failure is
            # seen - the telemetry probe is the other, and which one wins
            # depends on whether anybody happened to be looking. Both call
            # the same helper; notify_once keyed on the handle means one
            # ping either way (see bootstrap.announce_exit).
            from . import bootstrap as boot
            boot.announce_exit(
                notifier, instance_id=instance_id, handle=handle,
                note=row["note"],
                exit_code=exit_code if state == "exited" else None)
        # A settled registry row is the tie-breaker for a probe that could
        # not read the exit file (e.g. cleaned up on the box).
        if state == "unknown" and row["exited_at"] is not None:
            state = "exited" if row["exit_code"] is not None else "vanished"
            exit_code = row["exit_code"]
        return payload(state, exit_code, log_tail)

    @app.get("/instances/{instance_id}/detached")
    async def list_detached_commands(instance_id: str):
        return {"detached": db.list_detached(instance_id)}

    @app.post("/instances/{instance_id}/endpoints", status_code=201)
    async def register_model_endpoint(instance_id: str,
                                      req: RegisterEndpointRequest):
        """Adopt a hand-started model server into the OpenAI proxy.

        The port is a LOOPBACK port on the instance, reached only over the
        managed SSH connection; nothing new listens anywhere. Once
        registered, the model is routed at localhost:8000/v1 like any
        template-served one, appears in /v1/models when it answers, and its
        proxy traffic counts as activity for the idle sweep."""
        conn = orchestrator.connections.get(instance_id)
        if conn is None or conn.ssh_connection() is None:
            raise HTTPException(409, f"no connected instance {instance_id}")
        db.register_endpoint(instance_id=instance_id, port=req.port,
                             model_id=req.model_id, note=req.note,
                             created_by=current_principal())
        dispatcher.touch_activity(instance_id)
        db.record_audit(
            current_principal(), "endpoint_registered",
            f"{instance_id}: {req.model_id!r} on loopback:{req.port}"
            + (f" ({req.note})" if req.note else ""))
        return {"instance_id": instance_id, "port": req.port,
                "model_id": req.model_id}

    @app.get("/instances/{instance_id}/endpoints")
    async def list_model_endpoints(instance_id: str):
        return {"endpoints": db.list_registered_endpoints(instance_id)}

    @app.delete("/instances/{instance_id}/endpoints/{port}")
    async def deregister_model_endpoint(instance_id: str, port: int):
        if not db.delete_registered_endpoint(instance_id, port):
            raise HTTPException(
                404, f"no registered endpoint on {instance_id}:{port}")
        db.record_audit(
            current_principal(), "endpoint_deregistered",
            f"{instance_id}: loopback:{port}")
        return {"instance_id": instance_id, "port": port, "removed": True}

    @app.get("/instances/{instance_id}/metrics")
    async def instance_metrics(instance_id: str):
        sidecar = orchestrator.sidecar_for(instance_id)
        if sidecar is None:
            raise HTTPException(409, f"no managed connection to {instance_id}")
        try:
            return await sidecar.metrics()
        except SidecarError:
            # No sidecar on this box (launched outside Manifold): same
            # payload from nvidia-smi over the managed SSH connection.
            payload = await orchestrator.gpu_metrics_via_ssh(instance_id)
            if payload is not None:
                return payload
            raise

    async def _drive_terminal(
        ws: WebSocket,
        session: TerminalSession,
        *,
        persistent: bool,
        on_input=None,
    ) -> None:
        """Shared WS half of every terminal: attach (replays scrollback),
        forward input/resize, and on the way out decide the shell's fate. A
        plain socket drop (refresh, frozen tab) DETACHES a persistent session
        - the shell keeps running for a reattach; an explicit {"type":
        "close"} from the panel's x button kills it."""
        await session.attach(ws)
        killed = False
        try:
            while True:
                msg = await ws.receive_json()
                if on_input:
                    on_input()
                kind = msg.get("type")
                if kind == "input":
                    session.write_input(msg.get("data", ""))
                elif kind == "resize":
                    session.resize(
                        int(msg.get("cols", 80)), int(msg.get("rows", 24)))
                elif kind == "ack":
                    # Flow control: the browser rendered this many more chars.
                    session.ack(int(msg.get("bytes", 0)))
                elif kind == "close":
                    killed = True
                    term_sessions.kill(session.id)
                    # GONE, not a bare close: the panel reconnects on an
                    # unexplained one, and this shell is not coming back.
                    await ws.close(code=WS_SHELL_GONE)
                    return
        except (WebSocketDisconnect, KeyError, ValueError, OSError):
            pass
        finally:
            if not killed:
                if persistent and not session.exited:
                    session.detach(ws)
                else:
                    term_sessions.kill(session.id)

    @app.websocket("/instances/{instance_id}/terminal")
    async def instance_terminal(ws: WebSocket, instance_id: str):
        """Browser terminal: xterm.js <-> this WS <-> SSH shell session.

        Rides the managed connection — no ttyd, nothing new listening on
        the instance. Protocol: client sends JSON {type: "input"|"resize"|
        "close"}, server sends raw text frames of terminal output. All
        traffic counts as activity for idle detection.

        Pass ?session=<id> to make the shell survive the socket: reconnect
        with the same id (after a refresh) and it reattaches with scrollback
        instead of starting over. No session id = the old ephemeral behavior.
        """
        await ws.accept()
        sid = ws.query_params.get("session", "")
        # Did the client believe a shell was already here? A restored dock
        # tab and any reconnect say yes; a freshly opened tab says nothing.
        # Only the browser knows this - the backend cannot tell a new id
        # from one whose shell it reaped (and after a restart it remembers
        # neither), which is exactly how the notice came to fire on tabs
        # that never had a previous shell.
        resuming = ws.query_params.get("resume", "") == "1"
        key = f"inst:{instance_id}:{sid}" if sid else ""
        touch = lambda: dispatcher.touch_activity(instance_id)  # noqa: E731

        existing = term_sessions.get(key) if key else None
        if existing is not None:
            touch()
            await _drive_terminal(ws, existing, persistent=True,
                                  on_input=touch)
            return

        conn = orchestrator.connections.get(instance_id)
        ssh = conn.ssh_connection() if conn else None
        if ssh is None:
            await ws.send_text(
                f"\r\n[manifold] no SSH connection to {instance_id} "
                f"(state: {conn.state.value if conn else 'unknown'})\r\n"
            )
            await ws.close()
            return

        process = await ssh.create_process(
            term_type="xterm-256color", term_size=(80, 24)
        )
        touch()
        session = TerminalSession(
            key or f"inst:{instance_id}:ephemeral-{id(process)}",
            write_input=lambda data: process.stdin.write(data),
            resize=lambda cols, rows: process.change_terminal_size(cols, rows),
            close=process.close,
            on_output=touch,
        )

        async def pump_output():
            while True:
                # Backpressure: if the browser is behind, pause BEFORE reading
                # more so the SSH channel window fills and the remote shell
                # throttles itself, instead of buffering unboundedly here.
                await session.await_writable()
                data = await process.stdout.read(4096)
                if not data:
                    break
                await session.feed(data)
            await session.mark_exited()

        session.pump_task = asyncio.create_task(pump_output())
        term_sessions.register(session)
        if sid and resuming:
            # The client expected a shell here and we are building a NEW one,
            # so whatever was there is gone. Recorded into scrollback so it
            # survives the attach and any later reattach.
            #
            # `resuming` is the whole difference between a true report and a
            # false one: a first connect on a fresh tab id lands here too,
            # and telling it that its previous shell ended is a lie about the
            # one thing this message exists to tell the truth about.
            await session.feed(_replaced_shell_notice())
        await _drive_terminal(ws, session, persistent=bool(sid),
                              on_input=touch)

    @app.websocket("/local/terminal")
    async def local_terminal(ws: WebSocket):
        """A shell on THIS machine, in the dashboard - the local half of the
        hub. Same wire protocol as the instance terminal, so the same panel
        drives both.

        Security posture (see DECISIONS.md): the backend only listens on
        loopback, but browsers allow cross-origin WebSocket connections, so
        a malicious web page could otherwise reach this endpoint. Defense:
        a strict Origin allowlist (localhost only) - checked HERE because
        CORS middleware does not cover WebSockets - plus a config kill
        switch (hub.local_terminal).
        """
        origin = ws.headers.get("origin", "")
        host = origin.split("://", 1)[-1].split(":")[0].lower()
        if not settings.hub.local_terminal or host not in (
                "localhost", "127.0.0.1"):
            await ws.close(code=4403)
            return
        if os.name == "nt":
            await ws.accept()
            await ws.send_text("\r\n[manifold] the local terminal is not "
                               "supported on Windows yet\r\n")
            # GONE: retrying cannot help, so the panel must stop trying.
            await ws.close(code=WS_SHELL_GONE)
            return

        await ws.accept()
        sid = ws.query_params.get("session", "")
        resuming = ws.query_params.get("resume", "") == "1"   # see above
        key = f"local:{sid}" if sid else ""
        existing = term_sessions.get(key) if key else None
        if existing is not None:
            await _drive_terminal(ws, existing, persistent=True)
            return

        import fcntl
        import pty
        import shutil
        import struct
        import termios

        shell = os.environ.get("SHELL") or shutil.which("zsh") or "/bin/sh"
        # ?model=<id>: pre-wire this shell to a model served through the
        # OpenAI-compatible proxy, so any OpenAI-compatible CLI started in
        # it (aider, opencode, ...) talks to the user's own served model
        # with zero setup. Values land in the child's ENV only - nothing is
        # interpolated into a shell command.
        model = ws.query_params.get("model", "")[:200]
        proxy_port = os.environ.get("MANIFOLD_PORT", "8000")
        proxy_base = f"http://127.0.0.1:{proxy_port}/v1"
        pid, fd = pty.fork()
        if pid == 0:                       # child: become the user's shell
            # The child must NEVER return into this code: if exec fails (a
            # bad $SHELL, permissions), falling through would run a second
            # FastAPI process against the parent's inherited fds and
            # database handles. _exit skips all of that - no unwinding, no
            # atexit, no shared-state teardown.
            try:
                if model:
                    os.environ["OPENAI_BASE_URL"] = proxy_base
                    os.environ["OPENAI_API_BASE"] = proxy_base  # older SDKs
                    # The credential /v1 actually accepts: the dedicated
                    # proxy key when set, else the API token (real mode
                    # always has one). The old hardcoded "manifold" literal
                    # died with the open proxy - it would now be a wrong
                    # key baked into every shell. When neither is set the
                    # proxy is open and the placeholder only satisfies
                    # SDKs that refuse an empty key string.
                    os.environ["OPENAI_API_KEY"] = (
                        settings.proxy_api_key or settings.api_token
                        or "unused")
                    os.environ["MANIFOLD_MODEL"] = model
                os.execvp(shell, [shell, "-l"])
            except BaseException:
                pass
            os._exit(127)

        loop = asyncio.get_event_loop()
        # Bounded so a firehose can't grow unboundedly here: when it fills, we
        # stop reading the pty (below), leaving output in the kernel's pty
        # buffer, whose backpressure eventually throttles the local shell.
        out_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
        reader_paused = False

        def on_readable():
            nonlocal reader_paused
            if out_queue.full():
                # The pump is behind; pause reading and let the pty buffer
                # hold the data. pump_output re-arms us after it drains one.
                loop.remove_reader(fd)
                reader_paused = True
                return
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b""
            out_queue.put_nowait(data or None)
            if not data:
                loop.remove_reader(fd)

        loop.add_reader(fd, on_readable)

        def resize_pty(cols: int, rows: int) -> None:
            size = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, size)

        # Idempotent teardown. Two paths end a shell - explicit kill
        # (close_pty) and the shell exiting on its own (pump EOF) - and
        # both run for one session (exit, then the WS funnel's kill). The
        # guard makes the second call a no-op, because BOTH halves are
        # unsafe to repeat once the kernel recycles the identifier: a
        # closed fd NUMBER may now be an unrelated descriptor, and a
        # reaped PID may now lead an unrelated process group that a
        # second killpg would hang up.
        torn_down = False

        def teardown_once() -> None:
            nonlocal torn_down
            if torn_down:
                return
            torn_down = True
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            # Hangup + reap the whole process group (see _end_shell_group):
            # a closed tab must never leave zombies or orphaned children.
            # An escalation to SIGKILL is worth an audit row: it means
            # something inside the shell ignored the hangup, and the last
            # output is the best clue to what that was.
            _end_shell_group(
                pid, label=shell,
                on_escalation=lambda: db.record_audit(
                    current_principal(), "terminal_sigkill_escalation",
                    f"{shell} pgid {pid} ignored SIGHUP; last output: "
                    f"{session.tail_text()!r}"))

        def close_pty() -> None:
            teardown_once()

        session = TerminalSession(
            key or f"local:ephemeral-{pid}",
            write_input=lambda data: os.write(fd, data.encode()),
            resize=resize_pty,
            close=close_pty,
        )

        # A 4096-byte pty read can split a multi-byte UTF-8 sequence (a TUI's
        # box glyphs, spinners, emoji); decoding each chunk in isolation
        # turned the split halves into U+FFFD garbage on screen. The
        # incremental decoder holds the partial sequence until its tail
        # arrives in the next chunk.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        async def pump_output():
            nonlocal reader_paused
            while True:
                # Backpressure: hold off while the browser is behind, so the
                # queue stays full and the pty reader stays paused.
                await session.await_writable()
                data = await out_queue.get()
                if reader_paused and not session.exited:
                    reader_paused = False
                    loop.add_reader(fd, on_readable)
                if data is None:
                    break
                text = decoder.decode(data)
                if text:
                    await session.feed(text)
            tail = decoder.decode(b"", final=True)
            if tail:
                await session.feed(tail)
            # The shell exited on its own: close the master fd and reap.
            # Neither happened here before - a shell that exited while
            # DETACHED leaked its master fd (and sat as a zombie, pre-65)
            # until the backend itself exited.
            teardown_once()
            await session.mark_exited()

        session.pump_task = asyncio.create_task(pump_output())
        term_sessions.register(session)
        if sid and resuming:
            # Asked for a session we no longer have: say so, and point at
            # the conversations that DID survive. The shell inherits this
            # process's cwd (no chdir above), so that is where any Claude
            # Code transcripts for it were written.
            await session.feed(_replaced_shell_notice(local_cwd=os.getcwd()))
        if model:
            # Through feed(), so the banner lands in scrollback and a
            # reattach after refresh replays it too.
            await session.feed(
                f"\r\n[manifold] this shell is wired to your served model:"
                f"\r\n[manifold]   OPENAI_BASE_URL={proxy_base}"
                f"\r\n[manifold]   MANIFOLD_MODEL={model}"
                f"\r\n[manifold] any OpenAI-compatible CLI started here "
                f"talks to it, e.g.: aider --model openai/$MANIFOLD_MODEL"
                f"\r\n\r\n")
        db.record_audit(current_principal(), "local_terminal_open",
                        f"{shell} (model env: {model})" if model else shell)
        await _drive_terminal(ws, session, persistent=bool(sid))

    # -- chat with a served model -----------------------------------------------

    def _serving_task(instance_id: str) -> dict | None:
        """A live model server on this instance (see agent.find_serving_task,
        the shared single source of truth)."""
        return find_serving_task(queue, templates, instance_id)

    @app.get("/instances/{instance_id}/model")
    async def instance_model(instance_id: str):
        """Is a model being served here, which one, and is it answering yet?

        `serving` means the vllm-serve container is running; `ready` means
        its API actually responds (vLLM finished loading). The chat panel
        shows a loading state while serving-but-not-ready."""
        task = _serving_task(instance_id)
        if task is None:
            return {"serving": False, "ready": False}
        readiness = await dispatcher.model_ready(
            instance_id, task["id"], task["port"]
        )
        return {
            "serving": True,
            "ready": readiness["ready"],
            "status_detail": readiness["error"],
            "task_id": task["id"],
            "template": task["template"],
            "model_id": task["model_id"],
            "port": task["port"],
        }

    @app.post("/instances/{instance_id}/chat")
    async def instance_chat(instance_id: str, req: ChatRequest):
        """Relay a chat completion to the model served on the instance,
        streaming the OpenAI-style SSE response straight through. The model
        listens on the instance's loopback; this rides the managed SSH
        connection — the chat never touches the public internet unencrypted."""
        task = _serving_task(instance_id)
        if task is None:
            raise HTTPException(
                409,
                "No model is being served on this instance. Queue a "
                "vllm-serve job first (Jobs page), then chat once it is "
                "running.",
            )
        model_client = orchestrator.model_client_for(instance_id)
        if model_client is None:
            raise HTTPException(409, f"no managed connection to {instance_id}")
        readiness = await dispatcher.model_ready(
            instance_id, task["id"], task["port"]
        )
        if not readiness["ready"]:
            raise HTTPException(
                503,
                f"{task['model_id']} is still loading on this instance "
                f"({readiness['error']}). Large models take a few minutes to "
                f"download and load; try again shortly.",
            )

        db.record_audit(
            current_principal(), "chat",
            f"{instance_id}: {len(req.messages)} message(s) -> "
            f"{task['model_id']}" + (" [tools]" if req.tools else ""),
        )
        dispatcher.touch_activity(instance_id)

        import json
        from fastapi.responses import StreamingResponse

        if req.tools:
            return StreamingResponse(
                _chat_with_tools(instance_id, task, model_client, req),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache",
                         "X-Accel-Buffering": "no"},
            )

        payload = {
            "model": task["model_id"],
            "messages": req.messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }

        async def relay():
            try:
                async for line in model_client.chat_stream(task["port"], payload):
                    yield line
                    dispatcher.touch_activity(instance_id)
            except ModelClientError as exc:
                # Mid-stream failure: surface it as an SSE event the panel
                # can render instead of silently truncating the reply.
                yield f'data: {{"error": {json.dumps(str(exc))}}}\n\n'

        return StreamingResponse(
            relay(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _chat_with_tools(instance_id: str, task: dict,
                               model_client, req: ChatRequest):
        """Tool loop between the user and the served model.

        Each turn the model may reply with one JSON tool call; the backend
        executes it through the guarded paths (chat_tools.py) and feeds the
        observation back. Plain text ends the loop as the final answer.
        Emits SSE: {"tool": ...} progress events, then one delta chunk with
        the answer (turn-at-once — tools mode trades token streaming for
        arms; the plain relay above still streams)."""
        import json as _json

        from .agent import parse_action
        from .chat_tools import (
            MAX_TOOL_TURNS,
            TOOLS_PROMPT,
            ChatToolExecutor,
        )

        executor = ChatToolExecutor(orchestrator, queue, templates, db,
                                    instance_id)
        history = [{"role": "system", "content": TOOLS_PROMPT}] + req.messages

        def delta(text: str) -> str:
            chunk = {"choices": [{"delta": {"content": text}}]}
            return f"data: {_json.dumps(chunk)}\n\n"

        try:
            for turn in range(MAX_TOOL_TURNS + 1):
                reply = await model_client.chat_completion(task["port"], {
                    "model": task["model_id"],
                    "messages": history,
                    "max_tokens": req.max_tokens,
                    "temperature": req.temperature,
                })
                text = reply["choices"][0]["message"]["content"] or ""
                parsed, _err = parse_action(text)
                if parsed is None or turn == MAX_TOOL_TURNS:
                    # Plain text (or out of turns): the final answer.
                    yield delta(text)
                    break
                action, args = parsed["action"], parsed["args"]
                observation = await executor.execute(action, args)
                yield ("data: " + _json.dumps({"tool": {
                    "action": action, "args": args,
                    "ok": "error" not in observation,
                    "error": observation.get("error"),
                }}) + "\n\n")
                history.append({"role": "assistant", "content": text})
                history.append({"role": "user",
                                "content": _json.dumps(observation)})
                dispatcher.touch_activity(instance_id)
            yield "data: [DONE]\n\n"
        except ModelClientError as exc:
            yield f'data: {{"error": {_json.dumps(str(exc))}}}\n\n'

    # -- OpenAI-compatible proxy (/v1) ---------------------------------------------
    # Point any OpenAI client at http://localhost:8000/v1 and it talks to a
    # model served on one of your instances (vllm-serve). Routes by the
    # request's `model`; the completion rides the managed SSH connection.
    # Adds NO new listener on the instance and launches nothing — it only
    # reaches models already running, whose launch already cleared the
    # budget/concurrency guards.

    def _openai_error(status: int, message: str, code: str,
                      kind: str = "invalid_request_error"):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=status,
            content={"error": {"message": message, "type": kind, "code": code}},
        )

    def _proxy_auth_error(request: Request, needed_role: str):
        """None when the request may proceed, else the OpenAI-shaped
        error response.

        The dedicated proxy key is THE credential when set (no principal,
        no role - it exists only to talk to models). Otherwise any valid
        API credential works - the .env token or a minted principal's -
        and since Phase 80 the principal's role must clear the route's
        bar (chat completions drive a paid GPU: operator). Neither
        configured = open - the mock/test harness and an explicitly
        tokenless dev backend. Production always holds an api_token after
        first boot, so a real backend is fail-closed here."""
        if not settings.proxy_api_key and not settings.api_token:
            return None
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if settings.proxy_api_key and token_matches(token,
                                                    settings.proxy_api_key):
            return None
        identity = principal_resolver.resolve(token) if token else None
        if identity is None:
            return _proxy_unauthorized()
        name, role = identity
        if not role_allows(role, needed_role):
            return _openai_error(
                403,
                f"'{name}' has role '{role}', but this endpoint needs "
                f"'{needed_role}'. An admin can mint a token with the "
                f"right role on the Settings page.",
                "insufficient_role", "permission_error")
        return None

    def _proxy_unauthorized():
        # OpenAI envelope, kept: SDK clients parse {"error": {...}}. Points
        # at where the credential lives, never its value.
        return _openai_error(
            401,
            "Invalid API key. Use MANIFOLD_PROXY_KEY (or, if that is "
            f"unset, MANIFOLD_API_TOKEN) from Manifold's .env "
            f"({env_file}); see docs/openai-proxy.md.",
            "invalid_api_key", "authentication_error")

    def _serving_endpoints() -> list[dict]:
        """Every model server on a CONNECTED instance: template jobs, plus
        hand-started servers adopted via register_endpoint (Phase 99).

        Registered endpoints ride a synthetic task id ("ep:<box>:<port>") so
        the readiness cache keys them like any served model. A hand-started
        server used to be invisible here, which cost its owner proxy
        routing AND activity tracking in one move - the 07:42 reap was a
        box this function could not see.
        """
        eps = []
        for task in queue.list():
            if task["status"] != "running":
                continue
            template = templates.get(task["template"])
            if template is None or not template.ports:
                continue
            conn = orchestrator.connections.get(task["instance_id"])
            if conn is None or conn.ssh_connection() is None:
                continue
            eps.append({
                "instance_id": task["instance_id"],
                "task_id": task["id"],
                "model_id": task["parameters"].get("model_id") or task["template"],
                "port": template.ports[0].host,
            })
        for row in db.list_registered_endpoints():
            conn = orchestrator.connections.get(row["instance_id"])
            if conn is None or conn.ssh_connection() is None:
                continue
            eps.append({
                "instance_id": row["instance_id"],
                "task_id": f"ep:{row['instance_id']}:{row['port']}",
                "model_id": row["model_id"],
                "port": row["port"],
                "registered": True,
            })
        return eps

    def _resolve_model(requested: str):
        eps = _serving_endpoints()
        if not eps:
            return None, "no_models"
        for e in eps:                      # pin by instance id
            if e["instance_id"] == requested:
                return e, None
        for e in eps:                      # exact model match (first wins)
            if e["model_id"] == requested:
                return e, None
        if len(eps) == 1:                  # lenient: only one model served
            return eps[0], None
        return None, "not_found"

    @app.get("/v1/models")
    async def openai_list_models(request: Request):
        denied = _proxy_auth_error(request, "viewer")
        if denied is not None:
            return denied
        # Only advertise models that actually answer — a client picking from
        # this list expects to be able to use it. Still-loading models are
        # simply not listed yet.
        seen, data = set(), []
        for e in _serving_endpoints():
            if e["model_id"] in seen:
                continue
            readiness = await dispatcher.model_ready(
                e["instance_id"], e["task_id"], e["port"]
            )
            if not readiness["ready"]:
                continue
            seen.add(e["model_id"])
            data.append({
                "id": e["model_id"], "object": "model", "created": 0,
                "owned_by": f"manifold:{e['instance_id'][:12]}",
            })
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions")
    async def openai_chat_completions(request: Request):
        import json
        from fastapi.responses import StreamingResponse
        denied = _proxy_auth_error(request, "operator")
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:
            return _openai_error(400, "Request body is not valid JSON.",
                                 "invalid_json")
        if not isinstance(body, dict) or not body.get("messages"):
            return _openai_error(400, "`messages` is required.",
                                 "missing_messages")

        requested = str(body.get("model", ""))
        endpoint, err = _resolve_model(requested)
        if err == "no_models":
            return _openai_error(
                503, "No model is being served. Start a vllm-serve job on a "
                "connected instance first.", "no_model_served")
        if err == "not_found":
            available = [e["model_id"] for e in _serving_endpoints()]
            return _openai_error(
                404, f"Model '{requested}' is not being served. Available: "
                f"{', '.join(available)}.", "model_not_found")

        instance_id = endpoint["instance_id"]
        model_client = orchestrator.model_client_for(instance_id)
        if model_client is None:
            return _openai_error(503, f"Lost connection to {instance_id}.",
                                 "connection_lost")
        readiness = await dispatcher.model_ready(
            instance_id, endpoint["task_id"], endpoint["port"]
        )
        if not readiness["ready"]:
            return _openai_error(
                503, f"Model '{endpoint['model_id']}' is still loading "
                f"({readiness['error']}). Try again shortly.",
                "model_loading", "api_error")

        # Force the real served model id (makes the single-model lenient
        # route work), pass every other OpenAI param straight through.
        payload = {**body, "model": endpoint["model_id"]}
        payload.pop("stream", None)
        stream = bool(body.get("stream"))
        dispatcher.touch_activity(instance_id)
        db.record_audit(current_principal(), "openai_proxy",
                        f"{instance_id}: {endpoint['model_id']} stream={stream}")

        if not stream:
            try:
                return await model_client.chat_completion(
                    endpoint["port"], payload)
            except ModelClientError as exc:
                return _openai_error(502, str(exc), "upstream_error",
                                     "api_error")

        async def relay():
            try:
                async for line in model_client.chat_stream(
                    endpoint["port"], payload
                ):
                    yield line
                    dispatcher.touch_activity(instance_id)
            except ModelClientError as exc:
                yield ('data: '
                       + json.dumps({"error": {"message": str(exc),
                                               "type": "api_error"}})
                       + "\n\n")

        return StreamingResponse(
            relay(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- file bridge (upload/download over the managed SSH connection) -------------

    @app.post("/downloads/token")
    async def mint_download_token():
        """Mint a single-use, short-lived nonce for the two browser
        download links (file download and folder archive). Minting itself
        requires the normal auth - this route is NOT exempt - and the
        nonce then stands in for the token on exactly one GET, so the
        long-lived secret never appears in a query string or the access
        log. When no token is enforced the nonce is minted but never
        checked, which keeps the dashboard on a single code path."""
        return {"nonce": nonces.mint(current_principal()),
                "expires_in_seconds": nonces.ttl_seconds}

    # -- api principals (Phase 79) -------------------------------------------------

    def _require_credential_authority(target_role: str) -> None:
        """Who may mint/revoke which credentials (Phase 80).

        The route itself is admin-only (ROUTE_ROLES), so anyone here
        holds admin. One rule remains ABOVE that: only the owner token
        touches ADMIN credentials. An admin principal minting another
        admin would let a leaked admin token escalate laterally forever;
        the .env token stays the sole authority over its own tier."""
        if not settings.api_token:
            raise HTTPException(
                409, "API auth is not enabled (no MANIFOLD_API_TOKEN "
                     "configured), so there is nothing for a principal "
                     "to authenticate against.")
        if target_role == "admin" and current_principal() != "owner":
            raise HTTPException(
                403, "Only the owner token (the MANIFOLD_API_TOKEN in "
                     ".env) may mint or revoke admin credentials.")

    @app.post("/principals", status_code=201)
    async def create_principal(req: PrincipalRequest):
        """Mint a named token with a role. The value is in THIS response
        and nowhere else - the database keeps only its hash. Name it
        after the caller it is for (an agent, a coworker's laptop, a
        script), because the name is what every launch, task, and audit
        row will carry."""
        role = req.role.strip().lower()
        if role not in ROLES:
            raise HTTPException(
                422, f"Unknown role '{req.role}'. Roles: "
                     f"{', '.join(ROLES)} (viewer observes, operator "
                     f"works, admin governs).")
        _require_credential_authority(role)
        name = req.name.strip().lower()
        if not valid_principal_name(name):
            raise HTTPException(
                422, "Principal names are 2-32 chars of [a-z0-9-], and "
                     "the built-in actor names (owner, backend, autopilot, "
                     "api, anonymous) are reserved.")
        if db.principal_by_name(name) is not None:
            raise HTTPException(
                409, f"A principal named '{name}' already exists "
                     f"(revoked names are kept for attribution history; "
                     f"pick a new name).")
        ceiling = req.max_hourly_spend_usd
        if ceiling is not None and (ceiling != ceiling or ceiling <= 0):
            raise HTTPException(
                422, "max_hourly_spend_usd must be a positive number of "
                     "dollars per hour, or omitted for no ceiling.")
        token = secrets.token_urlsafe(32)
        db.create_principal(name=name, token_hash=hash_token(token),
                            created_by=current_principal(), role=role,
                            max_hourly_spend_usd=ceiling)
        db.record_audit(
            current_principal(), "principal_created",
            f"{name} ({role})"
            + (f" ceiling ${ceiling:.2f}/hr" if ceiling else ""))
        return {"name": name, "role": role, "token": token,
                "max_hourly_spend_usd": ceiling,
                "note": "Shown once. Store it now; only its hash is kept."}

    @app.get("/principals")
    async def list_principals():
        """Names and liveness only - never token values, never hashes."""
        return {"principals": db.list_principals(),
                "auth_enabled": bool(settings.api_token)}

    @app.get("/policy")
    async def get_policy():
        """The launch policy as ENFORCED right now: which file, which
        rules, per role. Read-only by design - the policy changes by
        editing policy.yaml and restarting, so the change is a reviewed
        commit, not a click."""
        from .policy import describe
        return describe(policy)

    @app.delete("/principals/{name}")
    async def revoke_principal(name: str):
        row = db.principal_by_name(name)
        if row is None:
            raise HTTPException(404, f"No principal named '{name}'.")
        _require_credential_authority(row.get("role") or "operator")
        if row["revoked_at"]:
            raise HTTPException(409, f"'{name}' is already revoked.")
        db.revoke_principal(name)
        db.record_audit(current_principal(), "principal_revoked", name)
        return {"revoked": name}

    ALLOWED_FILE_ROOTS = ("/lambda/nfs/", "/workspace/ephemeral/")

    def _resolve_remote_path(instance_id: str, path: str) -> str:
        """Resolve a user/agent-supplied path to a safe absolute remote path.

        Relative paths land on the instance's persistent filesystem. The
        result must stay under the same sanctioned roots templates may
        mount — no traversal out of them."""
        import posixpath
        if not path.startswith("/"):
            launch = db.find_launch_by_instance(instance_id)
            filesystem = (launch or {}).get("filesystem")
            if not filesystem:
                raise HTTPException(
                    409,
                    f"No filesystem recorded for {instance_id}; use an "
                    f"absolute path under /lambda/nfs/ or /workspace/ephemeral/.",
                )
            path = f"/lambda/nfs/{filesystem}/{path}"
        resolved = posixpath.normpath(path)
        if not any(resolved.startswith(root) for root in ALLOWED_FILE_ROOTS):
            raise HTTPException(
                400,
                f"Path must stay under {' or '.join(ALLOWED_FILE_ROOTS)} "
                f"(got {resolved!r}).",
            )
        return resolved

    def _connected(instance_id: str):
        conn = orchestrator.connections.get(instance_id)
        if conn is None or conn.ssh_connection() is None:
            raise HTTPException(
                409,
                f"No connected instance {instance_id}. Files move over the "
                f"managed SSH connection, so the instance must be running "
                f"and connected.",
            )
        return conn

    @app.post("/instances/{instance_id}/files/upload")
    async def upload_file(instance_id: str, file: UploadFile,
                          dest: str = Form("inbox/")):
        """Upload a local file to the instance over SFTP. `dest` ending in
        '/' is a directory (keeps the original filename); relative paths
        land on the persistent filesystem."""
        conn = _connected(instance_id)
        target = dest + (file.filename or "upload.bin") if dest.endswith("/") \
            else dest
        remote = _resolve_remote_path(instance_id, target)

        async def chunks():
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

        try:
            written = await conn.sftp_write(remote, chunks())
        except ConnectionError as exc:
            raise HTTPException(409, str(exc))
        except Exception as exc:
            raise HTTPException(502, f"upload failed: {exc}")
        dispatcher.touch_activity(instance_id)
        db.record_audit(
            current_principal(), "file_upload",
            f"{file.filename} -> {instance_id}:{remote} ({written} bytes)",
        )
        return {"path": remote, "bytes": written}

    @app.get("/instances/{instance_id}/files/download")
    async def download_file(instance_id: str, path: str,
                            offset: int = 0, max_bytes: int | None = None):
        """Stream a file down from the instance over SFTP.

        `offset`/`max_bytes` serve a byte range, and X-File-Size always
        carries the full remote size - together they let a client fetch a
        big file as a series of short, resumable requests (the MCP
        download_file tool does exactly this; one long-held socket dies at
        every proxy/client timeout in the chain).
        """
        import posixpath
        from fastapi.responses import StreamingResponse
        conn = _connected(instance_id)
        remote = _resolve_remote_path(instance_id, path)

        try:
            total = await conn.sftp_size(remote)
        except FileNotFoundError:
            raise HTTPException(404, f"{remote} not found on the instance")
        except ConnectionError as exc:
            raise HTTPException(409, str(exc))
        except Exception as exc:
            raise HTTPException(502, f"download failed: {exc}")
        if offset < 0 or offset > total:
            raise HTTPException(
                416,
                f"offset {offset} is outside the file (size {total}); the "
                f"remote file may have changed since the last chunk",
            )

        # Pull the first chunk BEFORE responding, so missing files are a
        # real 404 instead of a broken 200 stream.
        gen = conn.sftp_read(remote, offset=offset, max_bytes=max_bytes)
        first = b""
        try:
            first = await gen.__anext__()
        except StopAsyncIteration:
            pass                     # empty file: valid, zero-byte download
        except FileNotFoundError:
            raise HTTPException(404, f"{remote} not found on the instance")
        except ConnectionError as exc:
            raise HTTPException(409, str(exc))
        except Exception as exc:
            raise HTTPException(502, f"download failed: {exc}")

        dispatcher.touch_activity(instance_id)
        db.record_audit(current_principal(), "file_download", f"{instance_id}:{remote}")

        async def stream():
            if first:
                yield first
            async for chunk in gen:
                yield chunk

        filename = posixpath.basename(remote)
        return StreamingResponse(
            stream(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-File-Size": str(total),
                "X-Offset": str(offset),
            },
        )

    # -- file navigator (sidecar-backed browse/usage/delete + tar.gz archive) -------

    def _sidecar_or_409(instance_id: str):
        sidecar = orchestrator.sidecar_for(instance_id)
        if sidecar is None:
            raise HTTPException(409, f"no managed connection to {instance_id}")
        return sidecar

    def _sidecar_error_to_http(exc: SidecarError):
        message = str(exc)
        if "not found" in message:
            return HTTPException(404, message)
        if "recursive" in message:
            return HTTPException(409, message)
        return HTTPException(400, message)

    @app.get("/instances/{instance_id}/files/list")
    async def fs_list(instance_id: str, root_name: str = "persistent",
                      path: str = ""):
        """One directory level, served by the sidecar (local disk speed)."""
        try:
            return await _sidecar_or_409(instance_id).list_dir(root_name, path)
        except SidecarError as exc:
            raise _sidecar_error_to_http(exc)

    @app.get("/instances/{instance_id}/files/usage")
    async def fs_usage(instance_id: str, root_name: str = "persistent",
                       path: str = ""):
        """Recursive child sizes, heaviest first — the cleanup view."""
        try:
            return await _sidecar_or_409(instance_id).usage(root_name, path)
        except SidecarError as exc:
            raise _sidecar_error_to_http(exc)

    @app.delete("/instances/{instance_id}/files")
    async def fs_delete(instance_id: str, root_name: str, path: str,
                        recursive: bool = False):
        """Delete a file or directory on the instance. Destructive and
        audited; directories require recursive=true (the UI confirms)."""
        try:
            result = await _sidecar_or_409(instance_id).delete_path(
                root_name, path, recursive
            )
        except SidecarError as exc:
            raise _sidecar_error_to_http(exc)
        dispatcher.touch_activity(instance_id)
        db.record_audit(
            current_principal(), "file_delete",
            f"{instance_id} {root_name}:{path}"
            + (" (recursive)" if recursive else ""),
        )
        return result

    @app.get("/instances/{instance_id}/files/archive")
    async def fs_archive(instance_id: str, path: str):
        """Download a whole directory as one .tar.gz: tar runs ON the
        instance (compression where bandwidth is cheap), the archive is
        streamed down over SFTP, and the temp file is removed after."""
        import hashlib
        import posixpath
        from fastapi.responses import StreamingResponse
        conn = _connected(instance_id)
        remote = _resolve_remote_path(instance_id, path)
        parent, name = posixpath.dirname(remote), posixpath.basename(remote)
        if not name:
            raise HTTPException(400, "cannot archive a filesystem root")
        tmp = ("/workspace/ephemeral/.manifold-archives/"
               + hashlib.sha256(remote.encode()).hexdigest()[:16] + ".tar.gz")
        # Compressing a large tree can take a while; bound it generously.
        exit_status, _, stderr = await conn.run(
            f"mkdir -p /workspace/ephemeral/.manifold-archives && "
            f"tar czf {shlex.quote(tmp)} -C {shlex.quote(parent)} "
            f"{shlex.quote(name)}",
            timeout=600,
        )
        if exit_status != 0:
            raise HTTPException(
                502, f"tar failed (exit {exit_status}): {stderr[:200]}")
        dispatcher.touch_activity(instance_id)
        db.record_audit(current_principal(), "file_archive", f"{instance_id}:{remote}")

        async def stream():
            try:
                async for chunk in conn.sftp_read(tmp):
                    yield chunk
            finally:
                try:
                    await conn.run(f"rm -f {shlex.quote(tmp)}")
                except ConnectionError:
                    pass   # connection died mid-download; temp dies with box

        return StreamingResponse(
            stream(),
            media_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="{name}.tar.gz"'
            },
        )

    @app.get("/instances/{instance_id}/files/recent")
    async def instance_recent_files(instance_id: str, hours: float = 24,
                                    limit: int = 50):
        """Recently changed files on the instance (ephemeral + persistent),
        relayed from the sidecar — the 'what is my job producing?' view."""
        sidecar = orchestrator.sidecar_for(instance_id)
        if sidecar is None:
            raise HTTPException(409, f"no managed connection to {instance_id}")
        return await sidecar.recent_files(hours=hours, limit=limit)

    @app.get("/instances/{instance_id}/sidecar/diagnose")
    async def diagnose_sidecar(instance_id: str):
        """Why is the sidecar not answering? Probe the instance over the
        managed SSH connection and return an actionable cause + evidence."""
        return await orchestrator.diagnose_sidecar(instance_id)

    @app.websocket("/instances/{instance_id}/metrics/stream")
    async def instance_metrics_stream(ws: WebSocket, instance_id: str):
        """Relay: sidecar (via SSH forward) -> this WS -> browser chart."""
        await ws.accept()
        sidecar = orchestrator.sidecar_for(instance_id)
        if sidecar is None:
            await ws.send_json({"error": f"no managed connection to {instance_id}"})
            await ws.close()
            return
        try:
            async for payload in sidecar.metrics_stream():
                await ws.send_json(payload)
        except WebSocketDisconnect:
            pass
        except SidecarError:
            # No sidecar (externally-launched box): poll nvidia-smi over
            # the managed SSH connection instead, same payload shape.
            try:
                while True:
                    payload = await orchestrator.gpu_metrics_via_ssh(
                        instance_id)
                    if payload is None:
                        break
                    await ws.send_json(payload)
                    await asyncio.sleep(3.0)
            except (WebSocketDisconnect, RuntimeError):
                pass
        finally:
            try:
                await ws.close()
            except RuntimeError:
                pass  # already closed by the client

    # -- job templates --------------------------------------------------------------

    @app.get("/templates")
    async def list_templates():
        """Valid templates with parameter schemas, plus load errors so a
        broken YAML file is visible instead of silently missing. Custom
        (user-authored) templates carry their raw YAML for editing.

        Favorites first (Phase 107): the picker passed twenty entries, so
        favorited names lead the list in the user's stored order, each
        flagged. The ORDER is decided here, once, so the dashboard select,
        the MCP list_templates tool, and anything else that renders
        templates agree without re-implementing the sort."""
        favorites = list(prefs.get().templates.favorites)
        out = []
        for t in templates.values():
            entry = t.to_api()
            entry["custom"] = t.name in custom_names
            entry["favorite"] = t.name in favorites
            if entry["custom"]:
                path = custom_dir / f"{t.name}.yaml"
                entry["yaml"] = path.read_text() if path.exists() else ""
            out.append(entry)
        out.sort(key=lambda e: (0, favorites.index(e["name"]))
                 if e["favorite"] else (1, 0))
        return {"templates": out, "errors": template_errors}

    @app.post("/templates/custom", status_code=201)
    async def save_custom_template(req: CustomTemplateRequest):
        """Create or update a user template from raw YAML.

        Validated by the SAME loader as bundled templates - the mount jail
        (only /workspace/ephemeral and {persistent}), the parameter schema,
        and the port rules all apply. A custom template gets no powers a
        bundled one lacks; it is a recipe, not an escape hatch. Live
        immediately: the shared dict is reloaded in place, no restart."""
        try:
            template = save_custom_template_text(req.yaml)
        except Exception as exc:
            raise HTTPException(422, f"template rejected: {exc}")
        db.record_audit(
            current_principal(), "template_saved",
            f"custom template '{template.name}' "
            f"({'overrides bundled' if (bundled_dir / (template.name + '.yaml')).exists() else 'new'})",
        )
        entry = templates[template.name].to_api()
        entry["custom"] = True
        return {"template": entry}

    @app.delete("/templates/custom/{name}")
    async def delete_custom_template(name: str):
        """Remove a user template. Bundled templates cannot be deleted; if
        this one was shadowing a bundled template, the bundled version comes
        back on the reload."""
        if name not in custom_names:
            raise HTTPException(
                404 if name not in templates else 400,
                f"'{name}' is not a custom template"
                + (" (bundled templates cannot be deleted)"
                   if name in templates else ""),
            )
        (custom_dir / f"{name}.yaml").unlink(missing_ok=True)
        reload_templates()
        db.record_audit(current_principal(), "template_deleted", f"custom template '{name}'")
        return {"deleted": name, "restored_bundled": name in templates}

    @app.post("/templates/{name}/render")
    async def render_template_route(name: str, parameters: dict):
        template = templates.get(name)
        if template is None:
            raise HTTPException(404, f"Unknown template '{name}'")
        try:
            from .dispatcher import coerce_parameters
            from .templates import render_template
            coerced = coerce_parameters(template, parameters)
            # Find an existing filesystem to use as the token replacement, or default to <filesystem>
            filesystems = []
            if not mock and isinstance(lambda_client, SwappableLambdaClient):
                try:
                    filesystems = await lambda_client.list_filesystems()
                except Exception:
                    pass
            filesystem_name = filesystems[0].name if filesystems else "<filesystem>"
            return render_template(template, coerced, filesystem_name)
        except ParameterError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(500, f"Render failed: {exc}")

    # -- tasks ------------------------------------------------------------------------

    @app.post("/tasks", status_code=202)
    async def enqueue_task(req: TaskRequest):
        template = templates.get(req.template)
        if template is None:
            raise HTTPException(
                404,
                f"Unknown template '{req.template}'. "
                f"Available: {', '.join(sorted(templates)) or '(none)'}",
            )
        # Validate NOW so a bad request fails at enqueue, not minutes later
        # on the instance. The dispatcher re-validates before running.
        try:
            coerce_parameters(template, req.parameters)
        except ParameterError as exc:
            raise HTTPException(422, str(exc))
        depends_on = _validate_dependencies(req.depends_on)

        if req.auto_manage:
            # Fail fast on a bad GPU/region/filesystem here; the guarded launch
            # path validates again (and enforces budget/concurrency) when the
            # lifecycle actually fires.
            await _validate_auto_manage(req)
            task_id = queue.enqueue(
                template=req.template, parameters=req.parameters,
                auto_manage=True, gpu_type=req.gpu_type, region=req.region,
                filesystem=req.filesystem, depends_on=depends_on,
                created_by=current_principal())
            db.record_audit(
                current_principal(), "task_enqueue_auto",
                f"{task_id} ({req.template}) auto-manage "
                f"{req.gpu_type}/{req.region}/{req.filesystem}"
                + (f" after {', '.join(depends_on)}" if depends_on else ""))
        else:
            task_id = queue.enqueue(template=req.template,
                                    parameters=req.parameters,
                                    target_instance_id=req.target_instance_id,
                                    depends_on=depends_on,
                                    created_by=current_principal())
            db.record_audit(
                current_principal(), "task_enqueue",
                f"{task_id} ({req.template})"
                + (f" -> {req.target_instance_id}"
                   if req.target_instance_id else "")
                + (f" after {', '.join(depends_on)}" if depends_on else ""))
        return {"task": queue.get(task_id)}

    def _validate_dependencies(dep_ids: list[str]) -> list[str] | None:
        """Enqueue-time dependency checks, all 422s (fail at submit, not
        minutes later on a GPU). Deps may only reference tasks that already
        exist and can still succeed - which, with immutability, is what
        makes the graph a DAG without any cycle detector: a new task cannot
        be depended on by an older one, because the older one's deps were
        frozen before this task existed."""
        deps: list[str] = []
        for dep_id in dep_ids:
            if dep_id in deps:
                continue   # duplicate: harmless, dedupe silently
            dep = queue.get(dep_id)
            if dep is None:
                raise HTTPException(
                    422, f"depends_on: task '{dep_id}' does not exist")
            if dep["status"] in ("failed", "skipped"):
                raise HTTPException(
                    422,
                    f"depends_on: task {dep_id} ({dep['template']}) already "
                    f"{dep['status']} - this pipeline is dead; enqueueing "
                    f"its tail would only produce another skipped job")
            if dispatcher._is_server(dep["template"]):
                raise HTTPException(
                    422,
                    f"depends_on: task {dep_id} runs {dep['template']}, a "
                    f"server that never exits on its own - 'after it "
                    f"succeeds' would mean never. To run a batch job "
                    f"against a live server, target the server's instance: "
                    f"server and batch coexist there by design")
            deps.append(dep_id)
        return deps or None

    async def _validate_auto_manage(req: "TaskRequest") -> None:
        if not (req.gpu_type and req.region and req.filesystem):
            raise HTTPException(
                422, "auto-manage needs gpu_type, region, and filesystem")
        types = await lambda_client.list_instance_types()
        if req.gpu_type not in types:
            raise HTTPException(400, f"Unknown instance type '{req.gpu_type}'")
        filesystems = {fs.name: fs for fs in await lambda_client.list_filesystems()}
        fs = filesystems.get(req.filesystem)
        if fs is None:
            raise HTTPException(400, f"Unknown filesystem '{req.filesystem}'")
        if fs.region != req.region:
            raise HTTPException(
                400,
                f"Region mismatch: filesystem '{req.filesystem}' is in "
                f"{fs.region}, not {req.region}. Lambda filesystems are "
                f"region-locked; pick {fs.region}.")

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        """Cancel any job: queued jobs settle as cancelled; running jobs get
        their container stopped on the instance (including servers like
        vllm-serve, which otherwise never exit); an auto-managed job's
        lifecycle tears down whatever it already launched, guarded."""
        try:
            return await dispatcher.cancel_task(task_id)
        except LaunchRejected as exc:
            raise HTTPException(exc.status_code, exc.detail)

    @app.get("/tasks")
    async def list_tasks():
        """Tasks, each finished one annotated with its actual runtime and
        cost (wall time at the launch's hourly rate) so the user can check
        the pre-launch estimates against reality as history accumulates."""
        costs = db.task_costs()
        tasks = queue.list()
        by_id = {t["id"]: t for t in tasks}
        for t in tasks:
            c = costs.get(t["id"])
            t["runtime_seconds"] = c["runtime_seconds"] if c else None
            t["actual_cost_cents"] = c["actual_cost_cents"] if c else None
            t["deps"] = _resolve_deps(t, by_id)
        return {"tasks": tasks}

    def _resolve_deps(task: dict, by_id: dict | None = None) -> list[dict]:
        """depends_on resolved to live rows, so clients render dependency
        chips without re-joining. A deleted parent reports status 'missing'
        rather than vanishing from the list - the edge existed."""
        out = []
        for dep_id in task.get("depends_on") or []:
            dep = by_id.get(dep_id) if by_id is not None else queue.get(dep_id)
            out.append({
                "id": dep_id,
                "template": dep["template"] if dep else None,
                "status": dep["status"] if dep else "missing",
            })
        return out

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str):
        task = queue.get(task_id)
        if task is None:
            raise HTTPException(404, f"task {task_id} not found")
        task["deps"] = _resolve_deps(task)
        return task

    @app.get("/tasks/{task_id}/logs")
    async def get_task_logs(task_id: str, tail: int | None = None):
        if queue.get(task_id) is None:
            raise HTTPException(404, f"task {task_id} not found")
        return {"task_id": task_id, "lines": queue.get_logs(task_id, tail)}

    @app.get("/tasks/{task_id}/logs/stream")
    async def stream_task_logs(task_id: str, poll_interval: float = 0.2):
        """Stream task log lines as Server-Sent Events (SSE) as they arrive until completion."""
        import json
        from fastapi.responses import StreamingResponse
        if queue.get(task_id) is None:
            raise HTTPException(404, f"task {task_id} not found")

        async def event_generator():
            sent_seq = -1
            while True:
                lines = queue.get_logs(task_id)
                for line in lines:
                    seq = line.get("seq", 0)
                    if seq > sent_seq:
                        sent_seq = seq
                        yield f"data: {json.dumps(line)}\n\n"
                t = queue.get(task_id)
                if t and t.get("status") in ("succeeded", "failed", "skipped"):
                    lines = queue.get_logs(task_id)
                    for line in lines:
                        seq = line.get("seq", 0)
                        if seq > sent_seq:
                            sent_seq = seq
                            yield f"data: {json.dumps(line)}\n\n"
                    break
                await asyncio.sleep(poll_interval)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.get("/tasks/{task_id}/events")
    async def get_task_events(task_id: str):
        if queue.get(task_id) is None:
            raise HTTPException(404, f"task {task_id} not found")
        return {"task_id": task_id, "events": db.get_task_events(task_id)}

    @app.get("/tasks/{task_id}/events/stream")
    async def stream_task_events(task_id: str, poll_interval: float = 0.2,
                                 max_seconds: float = 3600.0):
        """Stream task lifecycle events as Server-Sent Events (SSE) until the
        task settles (or max_seconds elapses).

        Cursor-based on the event row id, exactly like stream_task_logs on
        seq. The old loop deduped on a non-existent 'event' field keyed by a
        second-precision timestamp, so its key was always "None_<second>": it
        silently dropped every event that shared a second with an earlier one
        (queued/launched/started routinely coincide) and re-fetched the whole
        history each tick. A heartbeat comment keeps a long-quiet training run
        alive; max_seconds caps a stuck 'running' task."""
        import json
        from fastapi.responses import StreamingResponse
        if queue.get(task_id) is None:
            raise HTTPException(404, f"task {task_id} not found")

        async def event_generator():
            last_id = 0
            loop = asyncio.get_event_loop()
            deadline = loop.time() + max_seconds
            last_beat = loop.time()
            heartbeat_interval = 15.0
            while True:
                for ev in db.get_task_events_after(task_id, last_id):
                    last_id = ev["id"]
                    yield f"data: {json.dumps(ev)}\n\n"
                t = queue.get(task_id)
                if t and t.get("status") in ("succeeded", "failed", "skipped"):
                    for ev in db.get_task_events_after(task_id, last_id):
                        last_id = ev["id"]
                        yield f"data: {json.dumps(ev)}\n\n"
                    break
                now = loop.time()
                if now >= deadline:
                    yield ": stream timeout (task still running)\n\n"
                    break
                if now - last_beat >= heartbeat_interval:
                    last_beat = now
                    yield ": keep-alive\n\n"
                await asyncio.sleep(poll_interval)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.post("/subagents/dispatch")
    async def dispatch_local_subagent_route(req: DispatchRequest):
        # format_tool_call(prompt, role="coding", tools=None): pass tools and
        # role by keyword so tools never lands in the role slot (which raised
        # ValueError on every call before).
        try:
            payload = engine.format_tool_call(req.prompt, role=req.role,
                                              tools=req.tools)
        except ValueError as exc:
            raise HTTPException(422, str(exc))     # bad role, etc.
        # Map engine failures to clean statuses instead of a raw 500: nothing
        # serving the model -> 503; the model was reached but errored (bad
        # status, transport, or a failed SSH forward) -> 502.
        try:
            return await engine.dispatch(req.model, payload)
        except NoHealthyEndpoint as exc:
            raise HTTPException(503, str(exc))
        except SubagentDispatchError as exc:
            raise HTTPException(502, str(exc))

    @app.get("/subagents/models")
    async def get_subagent_models():
        return {"models": (await engine.status())["models"]}

    @app.get("/subagents/swarm/status")
    async def get_swarm_status():
        snapshot = await engine.status()
        active = snapshot["healthy"]
        return {
            "health": "ok" if active > 0 else "degraded",
            "queue_depth": engine.queue.qsize(),
            "active_subagents": active,
        }


    # Literal path declared BEFORE /tasks/{task_id} so "finished" is not
    # captured as a task id.
    @app.delete("/tasks/finished")
    async def clear_finished_tasks():
        """Clear finished (succeeded/failed/skipped) jobs from history.
        Active jobs (queued/running) are left untouched, as is any finished
        task a queued job still depends on."""
        return {"cleared": queue.clear_finished()}

    @app.delete("/tasks/{task_id}")
    async def delete_task(task_id: str):
        task = queue.get(task_id)
        if task is None:
            raise HTTPException(404, f"task {task_id} not found")
        if task["status"] == "running":
            raise HTTPException(
                409, "cannot remove a running job; wait for it to finish")
        dependents = db.queued_dependents(task_id)
        if dependents:
            names = ", ".join(f"{t['id']} ({t['template']})"
                              for t in dependents)
            raise HTTPException(
                409,
                f"queued job(s) depend on this one: {names}. Their gate "
                f"reads this task's row; remove or cancel them first")
        queue.delete(task_id)
        return {"deleted": task_id}

    @app.get("/model-presets")
    async def model_presets():
        """Curated, ungated vLLM-serveable models tiered by GPU VRAM."""
        from .model_catalog import MODEL_PRESETS
        return {"presets": MODEL_PRESETS}

    # -- distillation ------------------------------------------------------------------

    @app.get("/student-presets")
    async def student_presets():
        """The other end of the shelf: small open bases to distill INTO.

        Static catalog, same shape as /model-presets. It is a separate read
        rather than a field on the generation response below because you
        pick the student BEFORE you describe the task, and a catalog that
        only arrives with the first answer is a catalog nobody can use."""
        from .model_catalog import STUDENT_PRESETS
        return {"presets": STUDENT_PRESETS}

    @app.post("/distill/config")
    async def generate_distill_config(req: DistillConfigRequest):
        """Ask a brain for an axolotl LoRA config; validate it; return it.

        REVIEW ONLY. Nothing is written and no training starts: saving is
        the existing upload route (POST /instances/{id}/files/upload with
        dest=configs/<name>.yaml, so it needs a connected instance) and
        training is the existing axolotl-finetune job. Both stay human
        decisions.

        Validation lives in distill.py and is a security boundary, not a
        lint: axolotl EXECUTES the config on the GPU box, so a model-written
        file is checked against an allowlist before anyone can save it.

        Slow on purpose: a CLI brain is allowed minutes. A client with the
        default 30s request timeout will report a failure while the backend
        is still working, so callers must raise theirs (the dashboard passes
        an explicit timeoutMs)."""
        from . import distill
        from .model_catalog import STUDENT_PRESETS

        try:
            dataset = distill.validate_dataset_name(req.dataset)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if req.student_model and req.student_model not in {
                s["model_id"] for s in STUDENT_PRESETS}:
            raise HTTPException(
                422,
                f"'{req.student_model}' is not on the student shelf. Pick "
                f"one from GET /student-presets, or leave it empty and let "
                f"the brain choose.")

        try:
            client, brain_model, brain_port = brains.resolve(req.brain)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

        prompt = distill.build_prompt(
            spec=req.spec, dataset=dataset, students=STUDENT_PRESETS,
            student_model=req.student_model)
        try:
            reply = await client.chat_completion(brain_port, {
                "model": brain_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2048,
                # A config, not prose: near-greedy keeps the same spec from
                # producing a different set of hyperparameters every click.
                "temperature": 0.2,
            })
        except ModelClientError as exc:
            raise HTTPException(502, f"the {req.brain} brain failed: {exc}")
        try:
            text = reply["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            # A brain that answers 200 with a shape we do not recognise is
            # the same class of problem as one that answers prose: say what
            # came back instead of raising a KeyError at the user.
            raise HTTPException(
                502,
                f"the {req.brain} brain answered in an unexpected shape: "
                f"{str(reply)[:200]}")

        try:
            config = distill.validate_config(
                text, dataset=dataset, students=STUDENT_PRESETS)
        except distill.ConfigRejected as exc:
            raise HTTPException(502, str(exc))

        # An api: brain spends the user's money and a cli: brain acts under
        # their login, so which one wrote this belongs in the audit trail.
        db.record_audit(
            current_principal(), "distill_config",
            f"{req.brain} wrote a training config for {dataset} "
            f"(student {config['base_model']}); review only, nothing saved")
        return {"config": {**config, "brain": req.brain,
                           "suggested_path": distill.config_filename(dataset)}}

    # -- the local model library (Phase 85) ----------------------------------------

    def _library():
        """The library directory, created on demand.

        Read off app.state rather than recomputed, so a test (and the
        desktop build, which moves DATA_ROOT out of the repo) can point it
        somewhere else without reaching into config.
        """
        root = app.state.model_library
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _ollama_binary() -> str:
        """Ollama's path, or "" when it is not installed.

        which_with_fallback, not shutil.which: a macOS app launched from
        Finder inherits launchd's bare PATH and cannot see a Homebrew
        install (the same bug that reported "No brains found" to a user
        logged into all three CLIs).
        """
        from .brains import which_with_fallback
        return which_with_fallback("ollama") or ""

    async def _installed_ollama_models(executable: str) -> list[str]:
        """`ollama list`, parsed. Never raises: this decorates the library
        listing, and a listing that 500s because Ollama is mid-upgrade is
        worse than one that cannot say what is installed."""
        if not executable:
            return []
        try:
            proc = await asyncio.create_subprocess_exec(
                executable, "list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except (OSError, asyncio.TimeoutError):
            return []
        return localmodels.parse_ollama_list(stdout.decode(errors="replace"))

    @app.get("/models/local")
    async def list_local_models():
        """What is in the library on THIS machine, and whether Ollama is
        here to run it.

        `ollama` absent is a normal answer, not an error: the library still
        holds real files, and the UI degrades to "here is the path and the
        command" rather than offering a button that cannot work.
        """
        executable = _ollama_binary()
        installed = await _installed_ollama_models(executable)
        models = []
        for path in sorted(_library().glob("*" + localmodels.GGUF_SUFFIX)):
            suggested = localmodels.default_ollama_name(path.name)
            models.append({
                "name": path.name,
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "suggested_ollama_name": suggested,
                "installed": localmodels.is_installed(suggested, installed),
            })
        return {"models": models, "library_path": str(_library()),
                "ollama_available": bool(executable),
                "ollama_models": installed}

    @app.post("/models/pull")
    async def pull_model(req: ModelPullRequest):
        """Bring a quantized model home over the managed SSH connection.

        Not the browser download route: the backend has to be able to find
        this file again in order to install it, and a file the browser
        routed into ~/Downloads is one Manifold can only talk about. It
        lands in DATA_ROOT/models, beside manifold.db.

        Pull BEFORE you terminate. The filesystem is reached through a
        running instance, so a filesystem with no instance attached is
        unreachable - that is a property of how storage is mounted, not a
        limitation this route could work around.
        """
        try:
            name = localmodels.validate_gguf_name(req.name)
            final = localmodels.destination(_library(), name)
        except localmodels.LocalModelError as exc:
            raise HTTPException(422, str(exc))
        _library()

        conn = _connected(req.instance_id)
        remote = _resolve_remote_path(req.instance_id, f"models/{name}")
        try:
            total = await conn.sftp_size(remote)
        except FileNotFoundError:
            raise HTTPException(
                404,
                f"{remote} is not on the instance. gguf-quantize writes "
                f"<output_name>.gguf into <filesystem>/models; check the job "
                f"finished and the name matches.")
        except ConnectionError as exc:
            raise HTTPException(409, str(exc))
        except Exception as exc:
            raise HTTPException(502, f"pull failed: {exc}")

        # Written to .partial and renamed only once the last byte lands, so
        # an interrupted transfer never leaves a plausible-looking model in
        # the library for the installer to pick up.
        partial = localmodels.partial_path(final)
        written = 0
        # A long pull IS activity, and saying so is what keeps the instance
        # alive to finish it. Without this the idle timer (30 min by
        # default) fires MID-TRANSFER: the box is terminated out from under
        # the download, the partial is deleted, and the user has paid for a
        # boot and lost the model. At the measured 0.6-0.7 MB/s that put a
        # hard ceiling near 1.2 GB - a 7B student could never be pulled at
        # all, and the failure looked like a network problem rather than a
        # self-inflicted teardown. Found by the 2026-08-14 audit.
        #
        # Throttled by BYTES, not chunks, so the cost stays flat however the
        # chunk size is tuned later.
        touch_every = 32 * 1024 * 1024
        next_touch = touch_every
        try:
            with open(partial, "wb") as handle:
                async for chunk in conn.sftp_read(remote):
                    handle.write(chunk)
                    written += len(chunk)
                    if written >= next_touch:
                        dispatcher.touch_activity(req.instance_id)
                        next_touch = written + touch_every
        except ConnectionError as exc:
            partial.unlink(missing_ok=True)
            raise HTTPException(409, str(exc))
        except Exception as exc:
            partial.unlink(missing_ok=True)
            raise HTTPException(502, f"pull failed after {written} bytes: {exc}")
        if written != total:
            partial.unlink(missing_ok=True)
            raise HTTPException(
                502,
                f"pull is short: {written} of {total} bytes arrived. Nothing "
                f"was added to the library; try again.")
        partial.replace(final)

        dispatcher.touch_activity(req.instance_id)
        db.record_audit(current_principal(), "model_pull",
                        f"{req.instance_id}:{remote} -> {final} "
                        f"({written} bytes)")
        return {"name": name, "path": str(final), "bytes": written,
                "suggested_ollama_name": localmodels.default_ollama_name(name)}

    @app.post("/models/install")
    async def install_model(req: ModelInstallRequest):
        """Register a library model with Ollama, which puts it in the brain
        picker.

        No new brain code exists for this: 127.0.0.1:11434 is already a
        probed local endpoint, so an installed model turns up as
        `local:ollama/<name>` within the detection cache window. That is the
        whole point of installing rather than inventing a fourth brain kind.
        """
        try:
            name = localmodels.validate_gguf_name(req.name)
            source = localmodels.destination(_library(), name)
            ollama_name = localmodels.validate_ollama_name(
                req.ollama_name or localmodels.default_ollama_name(name))
        except localmodels.LocalModelError as exc:
            raise HTTPException(422, str(exc))
        if not source.exists():
            raise HTTPException(
                404,
                f"{name} is not in the library ({_library()}). Pull it from "
                f"the instance first.")

        executable = _ollama_binary()
        if not executable:
            raise HTTPException(
                409,
                f"Ollama is not installed on this machine, so there is "
                f"nothing to install into. The model is already yours at "
                f"{source} - install Ollama from ollama.com and run: "
                f"ollama create {ollama_name} -f <Modelfile>")

        installed = await _installed_ollama_models(executable)
        if localmodels.is_installed(ollama_name, installed) and not req.overwrite:
            raise HTTPException(
                409,
                f"Ollama already has a model named '{ollama_name}'. Choose "
                f"another name, or pass overwrite to replace it.")

        modelfile = _library() / f"{ollama_name}.Modelfile"
        try:
            modelfile.write_text(localmodels.modelfile_text(source))
        except localmodels.LocalModelError as exc:
            raise HTTPException(422, str(exc))

        argv = localmodels.install_argv(executable, ollama_name, modelfile)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(
                504, "ollama create took over 10 minutes and was stopped.")
        except OSError as exc:
            raise HTTPException(502, f"could not run ollama: {exc}")
        output = stdout.decode(errors="replace")[-2000:]
        if proc.returncode != 0:
            raise HTTPException(
                502,
                f"ollama create exited {proc.returncode}: {output.strip()[-600:]}")

        db.record_audit(current_principal(), "model_install",
                        f"{name} -> ollama:{ollama_name}")
        return {"name": name, "ollama_name": ollama_name,
                # :latest, because that is what Ollama named it and what
                # GET /brains will therefore list. Returning the bare name
                # here sent the user looking for a picker entry that does
                # not exist under that spelling (found at the 2026-08-14
                # real gate, where install promised local:ollama/shot-tagger
                # and the picker showed local:ollama/shot-tagger:latest).
                "brain_ref": f"local:ollama/{ollama_name}:latest",
                "output": output}

    # -- capacity watches ---------------------------------------------------------------

    @app.post("/watches", status_code=201)
    async def create_watch(req: WatchRequest):
        types = await lambda_client.list_instance_types()
        if req.instance_type not in types:
            raise HTTPException(
                400,
                f"Unknown instance type '{req.instance_type}'. "
                f"Valid types: {', '.join(sorted(types))}",
            )
        if req.auto_launch:
            if not req.filesystem:
                raise HTTPException(
                    400, "auto_launch requires a filesystem to attach"
                )
            filesystems = {
                fs.name: fs for fs in await lambda_client.list_filesystems()
            }
            fs = filesystems.get(req.filesystem)
            if fs is None:
                raise HTTPException(400, f"Unknown filesystem '{req.filesystem}'")
            if fs.region != req.region:
                raise HTTPException(
                    400,
                    f"Region mismatch: filesystem '{req.filesystem}' lives in "
                    f"{fs.region} but the watch targets {req.region}.",
                )
        watch_id = db.create_watch(
            instance_type=req.instance_type, region=req.region,
            filesystem=req.filesystem, auto_launch=req.auto_launch,
            created_by=current_principal(),
        )
        db.record_audit(
            current_principal(), "watch_create",
            f"{watch_id}: {req.instance_type} in {req.region}"
            f"{' (auto-launch)' if req.auto_launch else ''}",
        )
        return {"watch": db.get_watch(watch_id)}

    @app.get("/watches")
    async def list_watches():
        return {
            "watches": db.list_watches(),
            "auto_launch_enabled": settings.watches.auto_launch_enabled,
        }

    @app.delete("/watches/{watch_id}")
    async def cancel_watch(watch_id: str):
        if db.get_watch(watch_id) is None:
            raise HTTPException(404, f"watch {watch_id} not found")
        db.update_watch(watch_id, status="cancelled")
        return {"watch": db.get_watch(watch_id)}

    # -- autopilot (agent runs driven by a model served on an instance) ------------

    @app.post("/autopilot/runs", status_code=202)
    async def start_autopilot_run(req: AutopilotRequest):
        # The brain can be any registered kind: instance:<id> (a model
        # served on a Manifold GPU), local:<endpoint>/<model> (Ollama /
        # LM Studio on this machine), or api:<name> (frontier API with a
        # key in .env). brain_instance_id remains as the legacy spelling.
        ref = req.brain or (f"instance:{req.brain_instance_id}"
                            if req.brain_instance_id else None)
        if not ref:
            raise HTTPException(422, "pick a brain (instance/local/api)")

        if ref.startswith("instance:"):
            instance_id = ref.partition(":")[2]
            serving = _serving_task(instance_id)
            if serving is None:
                raise HTTPException(
                    409,
                    f"No model is being served on {instance_id}. "
                    "Queue a vllm-serve job there first; the running model "
                    "becomes the run's brain.",
                )
            readiness = await dispatcher.model_ready(
                instance_id, serving["id"], serving["port"]
            )
            if not readiness["ready"]:
                raise HTTPException(
                    409,
                    f"The brain model {serving['model_id']} is still loading "
                    f"({readiness['error']}). Wait until it is ready, then "
                    f"start the run.",
                )
            if orchestrator.model_client_for(instance_id) is None:
                raise HTTPException(
                    409, f"no managed connection to {instance_id}"
                )
            brain_model, brain_port = serving["model_id"], serving["port"]
            client_fn = None      # per-turn resolution via the orchestrator
        else:
            try:
                client, brain_model, brain_port = brains.resolve(ref)
            except ValueError as exc:
                raise HTTPException(409, str(exc))
            client_fn = lambda: client  # noqa: E731

        # Approval policy: an explicit per-run list wins; then the legacy
        # boolean (true = gate everything); otherwise the saved Settings
        # policy, which defaults to launches only.
        if req.approve_actions is not None:
            unknown = set(req.approve_actions) - set(GATEABLE_ACTIONS)
            if unknown:
                raise HTTPException(
                    422,
                    f"cannot gate {', '.join(sorted(unknown))}. Gateable "
                    f"actions: {', '.join(GATEABLE_ACTIONS)}")
            gated = frozenset(req.approve_actions)
        elif req.require_approval is not None:
            gated = frozenset(GATEABLE_ACTIONS) if req.require_approval \
                else frozenset()
        else:
            gated = prefs.get().approvals.gated_actions()

        if req.unlimited_steps:
            max_steps = 0    # unlimited: an explicit user choice
        else:
            cap = settings.autopilot.max_steps_cap
            max_steps = min(
                req.max_steps or settings.autopilot.max_steps_default, cap)
        run_id = autopilot.start_run(
            goal=req.goal,
            brain_ref=ref,
            brain_model=brain_model,
            brain_port=brain_port,
            max_steps=max_steps,
            client_fn=client_fn,
            gated_actions=gated,
            created_by=current_principal(),
        )
        return {"run": db.get_agent_run(run_id)}

    @app.get("/brains")
    async def list_brains():
        """Every model that can drive Manifold right now: served on a GPU
        instance, running locally (Ollama/LM Studio), or a frontier API
        with a key configured."""
        from dataclasses import asdict
        return {"brains": [asdict(b) for b in await brains.list_brains()]}

    @app.get("/autopilot/approvals")
    @app.get("/approvals/pending")
    async def list_pending_approvals():
        """Actions waiting on a human Approve/Deny (approval-gated runs).

        timeout_seconds is part of the answer, not a detail: an undecided
        approval AUTO-DENIES when it expires, so a client that does not show
        the clock is hiding the most important thing about the card."""
        return {
            "approvals": db.pending_approvals(),
            "timeout_seconds": settings.autopilot.approval_timeout_seconds,
        }

    @app.post("/autopilot/approvals/{approval_id}")
    @app.post("/approvals/{approval_id}")
    async def decide_approval(approval_id: str, req: ApprovalDecision):
        status = "approved" if req.approve else "denied"
        if not db.decide_approval(approval_id, status):
            raise HTTPException(
                409, "already decided (or expired) - the run has moved on")
        db.record_audit(current_principal(), f"approval_{status}",
                        f"approval {approval_id}")
        return {"approval": db.get_approval(approval_id)}

    @app.get("/autopilot/runs")
    async def list_autopilot_runs():
        # Each row carries what the run DID (see agent.run_effect), so the
        # list can stop calling a run that accomplished nothing a success.
        # Derived from the stored steps rather than a column: no migration,
        # and runs recorded before this existed are judged too.
        from .agent import run_effect
        return {"runs": [{**r, **run_effect(db.get_agent_steps(r["id"]))}
                         for r in db.list_agent_runs()]}

    @app.get("/autopilot/runs/{run_id}")
    async def get_autopilot_run(run_id: str):
        from .agent import run_effect
        run = db.get_agent_run(run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} not found")
        steps = db.get_agent_steps(run_id)
        return {**run, **run_effect(steps), "steps": steps}

    @app.post("/autopilot/runs/{run_id}/cancel")
    async def cancel_autopilot_run(run_id: str):
        run = db.get_agent_run(run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} not found")
        if run["status"] != "running":
            raise HTTPException(409, f"run is already {run['status']}")
        autopilot.cancel_run(run_id)
        return {"cancelling": True}

    # -- audit (agent activity) -----------------------------------------------------

    # -- research keys (Phase 100) ------------------------------------------

    def _research_key_entry(name: str, present: dict[str, int],
                            meta: dict[str, dict]) -> dict:
        m = meta.get(name, {})
        return {
            "name": name,
            "present": name in present,
            # None when the value is gone - never 0, which would claim a
            # measured length for a key that does not exist.
            "length": present.get(name),
            "note": m.get("note") or "",
            "created_by": m.get("created_by"),
            "created_at": m.get("created_at"),
            "updated_at": m.get("updated_at"),
            "last_used_at": m.get("last_used_at"),
            "last_used_by": m.get("last_used_by"),
        }

    @app.get("/research-keys")
    async def list_research_keys():
        """Names, presence, length, and annotation. NEVER values: this is
        the same presence/length/status tier the doctor reports secrets at.

        The view is the UNION of the file and the metadata table, because
        the file is hand-editable: a hand-added key appears (provenance
        unknown), and a metadata row whose value was hand-deleted shows
        present=false instead of vanishing - both discrepancies are shown
        rather than smoothed over."""
        present = research_keys.names()
        meta = db.list_research_key_meta()
        names = sorted(set(present) | set(meta))
        return {"keys": [_research_key_entry(n, present, meta)
                         for n in names]}

    @app.put("/research-keys/{name}")
    async def set_research_key(name: str, req: SetResearchKeyRequest):
        """Deposit or rotate a key. Upsert: rotating overwrites in place
        and keeps the original depositor's provenance."""
        complaint = validate_name(name) or validate_value(req.value)
        if complaint:
            raise HTTPException(422, complaint)
        replaced = research_keys.set(name, req.value)
        db.upsert_research_key_meta(
            name=name, note=req.note, created_by=current_principal(),
            replaced=replaced)
        db.record_audit(
            current_principal(), "research_key_set",
            f"{name}: {'replaced' if replaced else 'created'}, "
            f"{len(req.value)} chars"
            + (f" ({req.note})" if req.note else ""))
        return _research_key_entry(name, research_keys.names(),
                                   db.list_research_key_meta())

    @app.post("/research-keys/{name}/value")
    async def read_research_key(name: str, req: RevealResearchKeyRequest):
        """Hand out one key value. POST, not GET, because it is honest
        about its side effects: the fetch stamps last-used and writes an
        audit row carrying the caller's required purpose.

        This can only ever return keys from the research vault file;
        Manifold's own .env is a different file this handler cannot read,
        so asking for e.g. "lambda_api_key" is a 404, not a disclosure."""
        value = research_keys.get(name)
        if value is None:
            available = ", ".join(sorted(research_keys.names()))
            raise HTTPException(
                404, f"no research key named {name!r}; available: "
                     f"{available or 'none stored yet'}")
        db.touch_research_key(name, current_principal())
        db.record_audit(current_principal(), "research_key_read",
                        f"{name}: {req.purpose}")
        return {"name": name, "value": value}

    @app.delete("/research-keys/{name}")
    async def delete_research_key(name: str):
        """Remove a key for every agent on the account. Deliberately not
        exposed as an MCP tool: agents rotate by overwriting; deletion is
        a human housekeeping action (Settings or this route directly)."""
        if not research_keys.delete(name):
            raise HTTPException(404, f"no research key named {name!r}")
        db.delete_research_key_meta(name)
        db.record_audit(current_principal(), "research_key_deleted", name)
        return {"deleted": name}

    @app.post("/audit/agent", status_code=201)
    async def record_agent_call(req: AgentAuditRequest):
        """MCP tool-call audit: tool, args, session note, result. The MCP
        server posts one entry per tool invocation."""
        import json as json_module
        args = req.args
        # Phase 100 belt-and-suspenders: the research-key tools redact the
        # value in their own audit args, but a version-drifted bridge might
        # not. Exact tool+field match only - heuristic secret-sniffing
        # would silently rewrite honest history, which is worse.
        if (req.tool.endswith("research_key") and isinstance(args, dict)
                and isinstance(args.get("value"), str)):
            args = {**args,
                    "value": f"<redacted, {len(args['value'])} chars>"}
        db.record_audit(
            "mcp", req.tool,
            json_module.dumps(
                {"args": args, "note": req.note, "result": req.result}
            ),
        )
        return {"recorded": True}

    @app.get("/audit")
    async def list_audit(actor: str | None = None, limit: int = 200):
        return {"entries": db.list_audit(actor=actor, limit=limit)}

    @app.get("/project-brief")
    async def get_project_brief():
        """The persistent Autopilot project brief (see agent._run_loop)."""
        return db.get_project_brief()

    @app.put("/project-brief")
    async def set_project_brief(req: ProjectBriefRequest):
        db.set_project_brief(req.content.strip())
        db.record_audit(
            current_principal(), "project_brief_updated",
            f"{len(req.content.strip())} chars")
        return db.get_project_brief()

    @app.get("/worklog")
    async def get_worklog(limit: int = 20):
        """Recent worklog entries (markdown, oldest first): what jobs and
        autopilot runs accomplished. The same record any agent can read
        from the worklog file (or its mirror); this serves it over HTTP so
        the get_work_log MCP tool works from any machine."""
        # Off-loop: the file grows for the life of the install, and a slow
        # read must not stall every other request while it runs.
        entries = await asyncio.to_thread(worklog.tail, limit)
        return {"entries": entries, "path": worklog.path}

    # -- launches (retry status + cost history) ------------------------------------

    @app.get("/launches")
    async def list_launches():
        now = utcnow()
        boot_timeout = settings.launch.boot_timeout_seconds
        return {"launches": [
            launch_progress(l, boot_timeout, now) for l in db.list_launches()
        ]}

    @app.get("/launches/{launch_id}")
    async def get_launch(launch_id: str):
        launch = db.get_launch(launch_id)
        if launch is None:
            raise HTTPException(404, f"launch {launch_id} not found")
        return launch_progress(
            launch, settings.launch.boot_timeout_seconds, utcnow()
        )

    @app.get("/launches/{launch_id}/wait")
    async def wait_launch(launch_id: str, timeout: float = 120.0):
        """Long-poll: block until the launch settles (active/failed/terminated)
        or `timeout` seconds pass, then return the (enriched) record. Replaces
        a poll loop of get_launch_status calls while a slow instance boots. The
        per-call wait is capped so the HTTP request never hangs indefinitely; a
        caller that is still booting simply calls again."""
        timeout = max(1.0, min(float(timeout), 300.0))
        launch = await orchestrator.wait_until_settled(launch_id, timeout)
        if launch is None:
            raise HTTPException(404, f"launch {launch_id} not found")
        return launch_progress(
            launch, settings.launch.boot_timeout_seconds, utcnow()
        )

    # -- cost/utilization intelligence (read-only; advisory) -----------------------

    @app.get("/estimate")
    async def estimate_job_route(template: str, instance_type: str):
        """Pre-launch estimate for `template` on `instance_type`, from this
        pair's own run history (median) or a coarse default. Advisory."""
        from .estimates import estimate_job
        if template not in templates:
            raise HTTPException(404, f"unknown template '{template}'")
        durations = db.task_durations(template, instance_type)
        rate_cents = None
        try:
            types = await lambda_client.list_instance_types()
            info = types.get(instance_type)
            if info is not None:
                rate_cents = info.price_cents_per_hour
        except Exception:
            rate_cents = None   # unconfigured/unreachable: estimate time only
        return estimate_job(
            template, instance_type, durations, rate_cents
        ).to_dict()

    @app.get("/estimate/model-fit")
    async def model_fit_route(model: str, instance_type: str):
        """Advisory pre-launch check: will this model's weights plausibly
        fit in that GPU's VRAM? Prefers the repo's exact size from the HF
        API (works for gated repos when HF_TOKEN is in .env); falls back
        to the model-name estimate. Never blocks."""
        from .estimates import model_fit
        gpu_description = ""
        try:
            types = await lambda_client.list_instance_types()
            info = types.get(instance_type)
            if info is not None:
                gpu_description = info.gpu_description or info.description
        except Exception:
            pass   # unconfigured/unreachable: verdict comes back "unknown"
        exact = await hf_lookup_fn(model, settings.hf_token) \
            if hf_lookup_fn else None
        return model_fit(model, instance_type, gpu_description, exact=exact)

    @app.get("/launches/{launch_id}/utilization")
    async def launch_utilization(launch_id: str):
        """Post-run utilization verdict, conservative right-size hint, and
        idle-spend accounting, from telemetry sampled while the instance ran.

        Advisory only, in the strong sense: nothing here gates anything. The
        two numbers deliberately read DIFFERENT columns — the hint keys on
        peak VRAM and the per-sample utilization MAXIMUM (a max tightens the
        hint, the safe direction for an OOM), while idle spend keys on the
        per-sample MEAN across the box's GPUs (a max would let one busy GPU
        hide seven idle ones). See spend.idle_spend.
        """
        from datetime import datetime
        from .estimates import utilization_summary
        launch = db.get_launch(launch_id)
        if launch is None:
            raise HTTPException(404, f"launch {launch_id} not found")
        instance_id = launch.get("lambda_instance_id")
        if not instance_id:
            return {"available": False,
                    "reason": "this launch never reached a running instance"}
        summary = db.telemetry_summary(instance_id)

        runtime_seconds = None
        start = launch.get("launched_at")
        end = launch.get("terminated_at") or utcnow()
        if start:
            try:
                runtime_seconds = (
                    datetime.fromisoformat(end) - datetime.fromisoformat(start)
                ).total_seconds()
            except (TypeError, ValueError):
                runtime_seconds = None

        gpu_desc = summary["gpu_name"] or launch.get("launched_type") or "GPU"
        util = utilization_summary(
            gpu_description=gpu_desc,
            runtime_seconds=runtime_seconds,
            peak_vram_used_mib=summary["peak_vram_used_mib"],
            vram_total_mib=summary["vram_total_mib"],
            avg_util_pct=summary["avg_util_pct"],
            sample_count=summary["sample_count"],
        )
        now = utcnow()
        window = spend.idle_window(launch, now_iso=now)
        samples = [] if window is None else db.telemetry_samples_between(
            instance_id, window["start_iso"], window["end_iso"])
        idle = spend.idle_spend(
            launch, samples, now_iso=now,
            util_pct=settings.idle_spend.util_pct,
            sample_interval_seconds=settings.telemetry.sample_seconds,
            min_window_seconds=settings.idle_spend.min_window_seconds,
        )
        return {"available": summary["sample_count"] > 0,
                "gpu_count": summary["gpu_count"],
                "idle_spend": idle, **util.to_dict()}

    # -- spend (accounting: what the launches actually cost) -----------------------
    # Thin, like every route here: spend.py owns the whole cost formula, these
    # three only fetch the rows, hand over the evidence, and stamp the demo
    # marker. `tz_offset_minutes` comes from the client (a browser's
    # getTimezoneOffset, negated) because "today" is a locale fact the caller
    # owns; the backend has no business guessing which midnight the user means.

    # A year of history is plenty for a chart, and a bound is what keeps a
    # hand-typed days=999999 from gap-filling a million buckets.
    SPEND_MAX_DAYS = 365

    # Real UTC offsets run from -12:00 (Baker Island) to +14:00 (Kiritimati).
    # Outside that it is a typo or a probe, and an unbounded offset does not
    # fail loudly — it silently moves which midnight "today" and "month to
    # date" are measured from (99999 minutes shifts them by 69 days), so the
    # page reports the wrong number while looking perfectly fine.
    TZ_OFFSET_MIN, TZ_OFFSET_MAX = -720, 840

    def clamp_tz(tz_offset_minutes: int) -> int:
        return max(TZ_OFFSET_MIN, min(tz_offset_minutes, TZ_OFFSET_MAX))

    def spend_evidence() -> tuple[list[dict], set[str] | None, set[str] | None]:
        """Every launch row, plus the cloud evidence to classify it against.

        The evidence is the LAST instances sweep, never a fresh cloud call:
        these routes get polled, and a spend page must not add an API call
        per poll (nor let the cloud's latency decide whether the page loads).
        Before the first sweep it is (None, None), which spend.py reads as
        "no snapshot" and so keeps trusting each row's own status — a live
        box goes on billing instead of being written off as stopped.
        """
        live_ids, listed_providers = orchestrator.last_cloud_snapshot()
        return db.list_launches(), live_ids, listed_providers

    # NO FLEET-WIDE IDLE-SPEND TOTAL HERE, deliberately. Idle spend is
    # per-launch (`GET /launches/{id}/utilization`) and stays there because
    # this route is polled and reads exactly ONE query, `db.list_launches()`.
    # An aggregate would need either (a) one windowed sample load per launch,
    # turning one query into N, or (b) a GROUP BY over telemetry_samples,
    # which cannot reproduce the same number: the exact math needs each
    # sample's NEXT sample (to bound its span) and each launch's own window,
    # and a count-based approximation would silently convert sampling gaps
    # into measured time - the precise under-reporting that spend.idle_spend
    # exists to prevent, in a second implementation of a number spend.py
    # insists on owning once. A wrong fleet total is worse than no fleet
    # total. Revisit with a rollup table, not with a clever query.

    @app.get("/spend/summary")
    async def spend_summary(tz_offset_minutes: int = 0):
        """Today / this week / month to date / all time, the current burn
        rate, and the launches whose cost is NOT known (never counted as $0)."""
        rows, live_ids, listed_providers = spend_evidence()
        summary = spend.summarize(
            rows, now_iso=utcnow(),
            tz_offset_minutes=clamp_tz(tz_offset_minutes),
            live_ids=live_ids, listed_providers=listed_providers,
            boot_timeout_seconds=settings.launch.boot_timeout_seconds,
            monthly_budget_usd=prefs.get().guardrails.monthly_budget_usd,
        )
        # Storage (Phase 95): filesystems bill per GB-month for as long as
        # they exist, and none of it appears in launch-based spend - a real
        # ~$50/month sat invisible in every number this product could report
        # until a manual audit found it. Lambda's API publishes NO rate, so
        # this is an ESTIMATE at the rate the user wrote in config.yaml,
        # kept in its own block and never folded into the launch totals -
        # the same discipline as `unresolved`. None (absent, not $0) when
        # the rate is switched off or the filesystems cannot be read.
        storage_estimate = None
        rate = settings.storage.rate_usd_per_gb_month
        if rate > 0:
            try:
                fs_list = await lambda_client.list_filesystems()
                # The dollars are computed FROM the displayed GB, so the
                # block can never contradict itself - a reader multiplying
                # the two shown numbers must land on the shown estimate.
                gb = round(
                    sum((f.bytes_used or 0) for f in fs_list) / 1e9, 3)
                storage_estimate = {
                    "filesystems": len(fs_list),
                    "gb_used": gb,
                    "rate_usd_per_gb_month": rate,
                    "usd_per_month_estimate": round(gb * rate, 2),
                    "note": ("Estimated at the rate in config.yaml "
                             "(storage.rate_usd_per_gb_month) - Lambda "
                             "publishes no rate via API, and its bytes_used "
                             "counter can lag real contents by hours (a "
                             "just-deleted or just-written volume reads "
                             "stale). Not included in the launch totals "
                             "above; verify against your invoice."),
                }
            except Exception:   # noqa: BLE001 - unreadable is absent, not $0
                storage_estimate = None
        # Fixture spend has to be self-identifying wherever it is shown: a
        # dollar figure in a screenshot with no demo marker is the worst
        # artifact this project could publish.
        return {**summary, "storage_estimate": storage_estimate, "mock": mock}

    @app.get("/spend/series")
    async def spend_series(bucket: str = "day", days: int = 30,
                           tz_offset_minutes: int = 0):
        """Spend over time, oldest first, gap-filled with zeros.
        bucket: day | week | month."""
        rows, live_ids, listed_providers = spend_evidence()
        try:
            # spend.py owns the valid bucket names; re-listing them here would
            # be a second copy of the contract, free to drift.
            points = spend.series(
                rows, now_iso=utcnow(), bucket=bucket,
                days=max(1, min(days, SPEND_MAX_DAYS)),
                tz_offset_minutes=clamp_tz(tz_offset_minutes),
                live_ids=live_ids, listed_providers=listed_providers,
                boot_timeout_seconds=settings.launch.boot_timeout_seconds,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"series": points, "mock": mock}

    @app.get("/spend/breakdown")
    async def spend_breakdown(by: str = "instance_type", days: int = 30,
                              tz_offset_minutes: int = 0):
        """Where the money went, biggest first.
        by: instance_type | region | provider | status | created_by | purpose."""
        rows, live_ids, listed_providers = spend_evidence()
        try:
            groups = spend.breakdown(
                rows, now_iso=utcnow(), by=by,
                days=max(1, min(days, SPEND_MAX_DAYS)),
                tz_offset_minutes=clamp_tz(tz_offset_minutes),
                live_ids=live_ids, listed_providers=listed_providers,
                boot_timeout_seconds=settings.launch.boot_timeout_seconds,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"breakdown": groups, "mock": mock}

    # -- cluster management --------------------------------------------------------

    class ClusterLaunchRequest(BaseModel):
        instance_type: str
        region: str
        filesystem: str
        node_count: int
        connection_mode: str | None = None
        ssh_key_name: str | None = None
        name: str = ""
        provider: str = "lambda"

    @app.post("/clusters/launch")
    async def launch_cluster_route(req: ClusterLaunchRequest):
        try:
            return await orchestrator.launch_cluster(
                instance_type=req.instance_type,
                region=req.region,
                filesystem=req.filesystem,
                node_count=req.node_count,
                connection_mode=req.connection_mode,
                ssh_key_name=req.ssh_key_name,
                name=req.name,
                provider=req.provider,
                created_by=current_principal(),
            )
        except LaunchRejected as e:
            raise HTTPException(e.status_code, str(e))

    @app.get("/clusters")
    async def list_clusters_route():
        return {"clusters": db.list_clusters()}

    @app.get("/clusters/{cluster_id}")
    async def get_cluster_route(cluster_id: str):
        cluster = db.get_cluster(cluster_id)
        if not cluster:
            raise HTTPException(404, f"Cluster {cluster_id} not found")
        return cluster

    @app.post("/clusters/{cluster_id}/terminate")
    async def terminate_cluster_route(cluster_id: str, force: bool = False):
        try:
            return await orchestrator.terminate_cluster(cluster_id, force=force)
        except LaunchRejected as e:
            raise HTTPException(e.status_code, str(e))

    # -- filesystems & storage ------------------------------------------------------

    @app.get("/filesystems")
    async def list_filesystems():
        return {
            "filesystems": [
                {
                    "name": fs.name,
                    "region": fs.region,
                    "mount_point": fs.mount_point,
                    "is_in_use": fs.is_in_use,
                    "bytes_used": fs.bytes_used,
                }
                for fs in await lambda_client.list_filesystems()
            ],
            # Self-identifying fixture state; see /instances.
            "mock": mock,
        }

    @app.post("/filesystems", status_code=201)
    async def create_filesystem(req: CreateFilesystemRequest):
        """Create a persistent filesystem in a region, without leaving for
        the Lambda console. Creation is free; storage bills by GB-month
        actually used."""
        return await orchestrator.create_filesystem(req.name, req.region)

    @app.delete("/filesystems/{name}")
    async def delete_filesystem(name: str, confirm_name: str = ""):
        """Permanently delete a filesystem. Refuses while attached to an
        instance, and (428) until confirm_name repeats the exact name -
        the response explains what would be destroyed. No force flag:
        there is no rescue path for a whole filesystem."""
        return await orchestrator.delete_filesystem(
            name, confirm_name=confirm_name)

    async def _storage_for(filesystem: str) -> StorageClient:
        filesystems = {fs.name: fs for fs in await lambda_client.list_filesystems()}
        if filesystem not in filesystems:
            raise HTTPException(
                404,
                f"Unknown filesystem '{filesystem}'. "
                f"Available: {', '.join(sorted(filesystems)) or '(none)'}",
            )
        fs = filesystems[filesystem]
        if fs.id not in storage_cache:
            try:
                storage_cache[fs.id] = storage_factory(fs)
            except ValueError as exc:
                # Browsing persistent files rides the Lambda S3 "Files" API,
                # whose access keys live in .env separately from the Lambda
                # API key. Without them the factory raises; surface that as a
                # clear 503 instead of an opaque 500 that decodes to nothing,
                # and teach the keyless route so a user without keys is not
                # blind (field report: "no instance = blind filesystem").
                raise HTTPException(
                    503,
                    f"{exc}. Without these keys, files are still browsable "
                    f"whenever an instance mounting this filesystem is "
                    f"running: use its Files panel, or the agent's "
                    f"list_persistent_files which rides the SSH connection.",
                ) from exc
        return storage_cache[fs.id]

    @app.get("/storage/files")
    async def list_storage_files(filesystem: str, prefix: str = ""):
        storage = await _storage_for(filesystem)
        files = await run_in_threadpool(storage.list_files, prefix)
        return {
            "filesystem": filesystem,
            "files": [
                {"key": f.key, "size_bytes": f.size_bytes,
                 "last_modified": f.last_modified}
                for f in files
            ],
        }

    @app.delete("/storage/files/{key:path}")
    async def delete_storage_file(key: str, filesystem: str):
        storage = await _storage_for(filesystem)
        try:
            await run_in_threadpool(storage.delete_file, key)
        except KeyError:
            raise HTTPException(404, f"file '{key}' not found")
        return {"deleted": key}

    # -- the dashboard itself (static export) ------------------------------------------
    # When the exported dashboard exists (dashboard/out in dev, ui/ inside a
    # PyInstaller bundle), serve it at "/" so the whole product is ONE
    # process. Mounted last: every API route above wins first. Next's static
    # export writes each route as <route>.html, so a direct load of /jobs
    # falls back to jobs.html.
    import sys as _sys

    ui_dir = (RESOURCE_ROOT / "ui" if getattr(_sys, "frozen", False)
              else RESOURCE_ROOT / "dashboard" / "out")
    if (ui_dir / "index.html").exists():
        from starlette.exceptions import HTTPException as StarletteHTTPException
        from starlette.staticfiles import StaticFiles

        class ExportedUI(StaticFiles):
            async def get_response(self, path: str, scope):
                # StaticFiles reports "not found" two ways: raising 404, or
                # returning the export's 404.html page (when a same-named
                # directory of route payloads exists). Catch both and retry
                # with the route's <path>.html file.
                try:
                    response = await super().get_response(path, scope)
                except StarletteHTTPException as exc:
                    if exc.status_code != 404 or "." in path:
                        raise
                    return await super().get_response(f"{path}.html", scope)
                if response.status_code == 404 and "." not in path:
                    try:
                        return await super().get_response(f"{path}.html", scope)
                    except StarletteHTTPException:
                        pass
                return response

        app.mount("/", ExportedUI(directory=str(ui_dir), html=True), name="ui")

    # Phase 80: compile the role table against the FINISHED route set.
    # Raises on any route missing from auth.ROUTE_ROLES, so an endpoint
    # cannot exist without a decision about who may call it. Built even
    # when auth is off, so the harness fails on unclassified routes too.
    role_table_holder.append(RoleTable.build(app))

    return app


def mock_seed_days_from_env(raw: str) -> int:
    """MANIFOLD_MOCK_SEED_DAYS, turned into a number the seeder will accept.

    A demo knob must never be able to stop the backend from starting, and
    both of the obvious wrong values used to do exactly that: `abc` raised
    from int(), and `400` raised from the seeder's own 1..365 check, either
    one killing the process at startup. So: unreadable input is ignored,
    an out-of-range number is clamped, and both say so in the log.
    """
    from .mock_seed import MAX_SEED_DAYS, MIN_SEED_DAYS

    if not raw.strip():
        return 0
    try:
        days = int(raw)
    except ValueError:
        logger.warning(
            "MANIFOLD_MOCK_SEED_DAYS=%r is not a whole number; starting with "
            "no demo history (set it to 1..%d to seed some)",
            raw, MAX_SEED_DAYS,
        )
        return 0
    if days <= 0:
        return 0                      # 0 and negatives are both "off"
    clamped = min(max(days, MIN_SEED_DAYS), MAX_SEED_DAYS)
    if clamped != days:
        logger.warning(
            "MANIFOLD_MOCK_SEED_DAYS=%d is outside the seeder's %d..%d "
            "window; seeding %d day(s) of demo history instead",
            days, MIN_SEED_DAYS, MAX_SEED_DAYS, clamped,
        )
    return clamped


def create_default_app() -> FastAPI:
    """Uvicorn entry point (run with --factory so importing this module
    never requires credentials): reads MANIFOLD_MOCK to pick the mode.

    In mock mode, MANIFOLD_MOCK_CAPACITY_FAILURES=N scripts N
    insufficient-capacity errors before launches succeed, so the
    dashboard's retry states can be demonstrated end to end.

    Also in mock mode, MANIFOLD_MOCK_SEED_DAYS=N fabricates N days of demo
    launch history (0, the default, is off) so the spend page has a past to
    show. It is forced to 0 outside mock mode: invented dollar amounts must
    never reach the real ledger.
    """
    mock = os.environ.get("MANIFOLD_MOCK", "") == "1"
    seed_days = mock_seed_days_from_env(
        os.environ.get("MANIFOLD_MOCK_SEED_DAYS", ""))
    lambda_client = None
    if mock:
        # Same tolerance as MANIFOLD_MOCK_SEED_DAYS: a mistyped demo knob
        # must never stop the backend from starting.
        try:
            failures = int(os.environ.get("MANIFOLD_MOCK_CAPACITY_FAILURES", "0"))
        except ValueError:
            logger.warning(
                "ignoring MANIFOLD_MOCK_CAPACITY_FAILURES=%r: not a number",
                os.environ.get("MANIFOLD_MOCK_CAPACITY_FAILURES"))
            failures = 0
        if failures:
            lambda_client = MockLambdaClient(
                scripted_launch_errors=[capacity_error() for _ in range(failures)]
            )
    # Phase 82: the launch policy, loaded strictly. Missing = permissive;
    # invalid = refuse to boot. A guard that fails open because of a typo
    # is a hole shaped exactly like a guard, so PolicyError is fatal here
    # (the same posture as a token that cannot be persisted).
    from .config import DATA_ROOT
    from .policy import PolicyError, load_policy
    try:
        policy = load_policy(DATA_ROOT / "policy.yaml")
    except PolicyError as exc:
        raise SystemExit(f"manifold: refusing to start: {exc}") from exc

    # Logs to a FILE, not just the terminal uvicorn was launched in. An
    # intermittent freeze was reported three times and investigated with
    # nothing to go on, because the only record died with the window. Like
    # the breadcrumb below: only the real entry point does this, so tests
    # that build apps in a loop never touch the user's log.
    from .diagnostics import setup_file_logging
    log_path = setup_file_logging(DATA_ROOT)
    if log_path is not None:
        logger.info("logging to %s", log_path)

    # Phase 88: leave the discovery breadcrumb (~/.config/manifold) so an
    # agent probing the filesystem finds the running product in seconds
    # instead of a lost session. Best-effort by contract; only the REAL
    # entry point writes it - create_app stays side-effect-free for tests.
    from .breadcrumb import write_breadcrumb
    port = os.environ.get("MANIFOLD_PORT", "8000")
    write_breadcrumb(f"http://127.0.0.1:{port}")

    return create_app(mock=mock, lambda_client=lambda_client,
                      mock_seed_days=seed_days if mock else 0,
                      policy=policy)
