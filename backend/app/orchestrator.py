"""Launch orchestration: validation -> guards -> retry -> persistence -> connection.

This module is the single path to a running instance. Every client — the
dashboard, the MCP server (Phase 6), anything else — goes through
Orchestrator.request_launch, so the guards here cannot be bypassed.

Control flow for a launch:

1. VALIDATE   connection mode is available; instance type exists; the
              filesystem exists and its region matches the requested region
              (filesystems are region-locked and attach only at launch).
2. GUARDS     max concurrent instances, then max hourly spend, both computed
              against LIVE instances from the Lambda API. Violations reject
              the request with 409 — nothing is ever queued silently.
3. PERSIST    a launches row is created; every later transition (retrying,
              booting, active, failed) updates it, so the dashboard can
              always show what is happening. The API returns 202 here.
4. RETRY      in the background: one "attempt" tries the requested type and
              then each budget-safe fallback type once. Capacity errors move
              to the next candidate; any other error fails the launch
              immediately. Between attempts: exponential backoff, capped.
5. BOOT       poll the instance until it is "active" with an IP (or time out).
6. CONNECT    ask the launch's ConnectionManager for the dial target (the
              only mode-specific step) and start a ManagedConnection, which
              keeps itself alive with reconnect+backoff from then on.
7. PROVISION  the launch stays "booting" until the box says its own setup
              finished (cloud-init: driver, Docker, runtime, sidecar). Only
              then does it become "active". SSH answering is not the same
              fact as the machine being usable - see sweep_provisioning.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from .cloud_init import build_user_data
from .config import Settings
from .data_safety import (
    GIB,
    local_path,
    plan_local_transfer,
    remote_path,
    summarize,
)
from .connections import (
    CONNECTION_MODES,
    ConnectionManager,
    ConnectionState,
    DirectSSHConnectionManager,
    HostKeyStore,
    ManagedConnection,
    TailscaleConnectionManager,
    backoff_delay,
)
from .db import Database, utcnow
from .lambda_api import (
    FilesystemInfo,
    InstanceInfo,
    InstanceTypeInfo,
    LambdaAPIError,
    LambdaClient,
)
from .model_client import ModelClient, RealModelClient
from .sidecar_client import RealSidecarClient, SidecarClient, SidecarError
from .ide_attach import remove_ssh_config_block
from .providers import ProviderRegistry
from .providers.base import ProviderCapacityError, ProviderError
from .providers.base import ProviderUnavailable

logger = logging.getLogger("manifold.orchestrator")

# Statuses that end a boot: keep waiting for one of these is pointless, the
# launch has failed.
BOOT_ABORT_STATUSES = ("terminated", "terminating", "preempted", "unhealthy")

# ...and the subset of those that mean the box has actually STOPPED, so the
# pipeline may record an observed stop time (see _wait_until_active).
# 'unhealthy' is deliberately absent: an unhealthy instance is still running
# and still billing, and calling that a stop would under-report the cost.
OBSERVED_STOP_STATUSES = ("terminated", "terminating", "preempted")

# Phase 103: how many filesystems may be attached BEYOND the primary one.
# Lambda does not publish its own attach limit, so this is Manifold's cap,
# not theirs - a number we can state in a refusal instead of letting a
# launch fail minutes later at the API with something opaque.
MAX_EXTRA_FILESYSTEMS = 4

# Sidecar-free GPU sampling (gpu_metrics_via_ssh): one CSV line per GPU.
NVIDIA_SMI_METRICS = (
    "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu "
    "--format=csv,noheader,nounits"
)

# -- the provisioning gate's three questions (Phase 110) -----------------------
#
# "The provider says RUNNING" is not "the machine is ready". On Lambda the two
# nearly coincide; on a stock-Ubuntu GCE image sshd answers minutes before
# cloud-init has finished building the NVIDIA driver, installing Docker and
# starting the sidecar. These are the cheapest questions that tell the
# difference, asked over the managed connection.

# 1. Did OUR cloud-init run to completion? The script sets -e, so the marker
#    exists only if every line before it succeeded. Durable path on purpose
#    (see cloud_init.py): /var/run is tmpfs and a reboot would erase it.
INIT_MARKER_PROBE = "test -f /var/lib/manifold/init-done"

# 2. If not: has cloud-init itself given up the boot? Weaker evidence - a
#    reboot rewrites boot-finished at the end of the NEXT boot while the
#    per-instance user-data script stays incomplete - so it is only ever the
#    fallback that stops us waiting forever on a billing box.
CLOUD_INIT_DONE_PROBE = "test -f /var/lib/cloud/instance/boot-finished"

# 3. Can THIS SSH session use the docker socket? Supplementary groups are
#    fixed when a session is established, so a connection opened before
#    cloud-init ran `usermod -aG docker ubuntu` is locked out of Docker for
#    its whole life - which is exactly how a live GCE box ended up unable to
#    run any template job. `test -w` honours the POSIX ACL cloud-init sets,
#    so a box the ACL already repaired answers yes and is not redialled. A
#    box with no socket at all answers yes too: there is nothing a redial
#    could fix there (same fail-open rule as the dispatcher's GPU probe).
DOCKER_ACCESS_PROBE = (
    "test ! -e /var/run/docker.sock || test -w /var/run/docker.sock"
)


class LaunchRejected(Exception):
    """A launch request refused before any Lambda API launch call.

    reason_code labels WHICH check refused, so callers can classify the
    rejection without re-deriving the guard's math. The auto-manage lifecycle
    uses it to tell a transient refusal (concurrency: the single slot is busy,
    wait and retry) from a permanent one (budget/validation/mode/provider:
    fail the job). Values: "validation" | "mode" | "concurrency" | "budget"
    | "provider". "provider" covers a cloud that is unknown, not set up, or
    could not be read at all - none of which a retry loop can fix, and all
    of which carry the sentence that does.
    """

    def __init__(self, status_code: int, detail: str,
                 reason_code: str = "validation"):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.reason_code = reason_code


class TerminationBlocked(Exception):
    """Termination refused: files on the instance could not be saved.

    Raised AFTER the rescue has run and done everything the data-safety
    policy allows (see data_safety.py). `files` is therefore not "files that
    exist" but "files still at risk" — the ones a rescue could not save. The
    report carries what WAS saved, so a client can show both halves.
    """

    def __init__(self, instance_id: str, files: list[dict],
                 report: dict | None = None):
        super().__init__(
            f"{len(files)} file(s) on {instance_id} could not be saved; "
            f"rescue them or pass force=true to terminate anyway"
        )
        self.instance_id = instance_id
        self.files = files
        self.report = report or {}


class TerminationRefused(Exception):
    """Termination refused: this instance belongs to another principal.

    Deliberately NOT folded into force=true. force means "burn it, I accept
    the data loss" and skips the rescue entirely — so if it also waived
    ownership, the one call for taking someone else's box would be the one
    call that destroys their unsaved files without looking. These are two
    different admissions and they get two different keys.

    The override is confirm_owner: the caller must name the principal they
    are overriding, which they can only learn by reading the instance. That
    is the same shape as delete_filesystem's confirm_name, and for the same
    reason — a guard you can clear without looking is a guard that gets
    cleared without looking.
    """

    def __init__(self, instance_id: str, owner: str, purpose: str | None,
                 caller: str):
        detail = f" It is described as: {purpose}." if purpose else ""
        super().__init__(
            f"Instance {instance_id} was launched by {owner!r}, not by "
            f"{caller!r}.{detail} If you are certain it should be "
            f"terminated, pass confirm_owner={owner!r}. Prefer asking its "
            f"owner first: a box that looks idle may be loading a model."
        )
        self.instance_id = instance_id
        self.owner = owner
        self.purpose = purpose
        self.caller = caller


@dataclass
class LaunchPlan:
    """Everything _run_launch needs, resolved and validated up front."""
    launch_id: str
    region: str
    filesystem: str
    connection_mode: str
    ssh_key_name: str
    types_to_try: list[str]          # requested type first, then fallbacks
    prices: dict[str, int]           # cents/hour per candidate type
    name: str
    provider: str = "lambda"
    # Phase 103: filesystems mounted BESIDE `filesystem`, already validated
    # (they exist, they are in `region`, no duplicates). Only the provider
    # call reads them; every other step still means the primary.
    extra_filesystems: list[str] = field(default_factory=list)


# A launch a poller can stop watching: nothing further will change on its own.
SETTLED_LAUNCH_STATUSES = frozenset({"active", "failed", "terminated"})

# Coarse, stable phase label + one-line human summary for each launch status,
# so a client shows a real step (and, while booting, a countdown) instead of
# an empty error string.
_LAUNCH_PHASES = {
    "launching": ("requesting_capacity",
                  "Asking Lambda for a matching instance"),
    "retrying":  ("retrying_capacity",
                  "No capacity yet; backing off and retrying"),
    "booting":   ("waiting_for_active",
                  "Instance created; waiting for it to boot and get an IP"),
    # Not "SSH is up" any more: SSH being up is the milestone BEFORE this
    # one, and saying so here was how a launch announced itself ready six
    # minutes early on GCE (Phase 110).
    "active":    ("ready", "Set up and ready to use"),
    "failed":    ("failed", "Launch did not complete"),
    "terminated": ("terminated", "Instance has been terminated"),
}


def launch_progress(launch: dict, boot_timeout_seconds: float,
                    now_iso: str,
                    provisioning_timeout_seconds: float | None = None) -> dict:
    """Return `launch` enriched with structured progress fields.

    Pure (the caller supplies `now_iso`): `phase` is a stable machine label,
    `phase_detail` a one-line human summary, and `settled` tells a poller it
    can stop. While the instance is still coming up on the provider we add
    `boot_elapsed_seconds` / `boot_timeout_seconds` / `boot_remaining_seconds`
    so a client renders a countdown rather than a blank.

    'booting' spans TWO windows (Phase 110), and the countdown belongs to the
    first one only. Once connected_at is set the boot wait is over and the
    box is provisioning itself; leaving the boot countdown on screen would
    keep ticking down a deadline that no longer decides anything, so the
    fields are dropped and phase_detail says what is actually happening. The
    phase label does not change: a client that branches on it keeps working,
    and a launch that is not usable yet keeps saying so.
    """
    status = launch.get("status", "")
    phase, detail = _LAUNCH_PHASES.get(status, (status or "unknown", ""))
    enriched = dict(launch)
    enriched["phase"] = phase
    enriched["settled"] = status in SETTLED_LAUNCH_STATUSES
    if status == "booting" and launch.get("connected_at"):
        elapsed = _interval_seconds(launch["connected_at"], now_iso)
        inst = launch.get("lambda_instance_id") or "instance"
        detail = f"SSH is up on {inst}; installing drivers and runtime"
        if elapsed is not None:
            elapsed = max(0.0, elapsed)
            budget = ("" if provisioning_timeout_seconds is None
                      else f" of {round(provisioning_timeout_seconds)}s")
            detail += f" ({round(elapsed)}s{budget})"
    elif status == "booting" and launch.get("launched_at"):
        try:
            elapsed = (datetime.fromisoformat(now_iso)
                       - datetime.fromisoformat(launch["launched_at"])
                       ).total_seconds()
        except ValueError:
            elapsed = None
        if elapsed is not None:
            elapsed = max(0.0, elapsed)
            remaining = max(0.0, boot_timeout_seconds - elapsed)
            enriched["boot_elapsed_seconds"] = round(elapsed)
            enriched["boot_timeout_seconds"] = round(boot_timeout_seconds)
            enriched["boot_remaining_seconds"] = round(remaining)
            inst = launch.get("lambda_instance_id") or "instance"
            detail = (f"{inst} booting; waited {round(elapsed)}s of "
                      f"{round(boot_timeout_seconds)}s "
                      f"(~{round(remaining)}s left before timeout)")
    enriched["phase_detail"] = detail
    return enriched


def _interval_seconds(start_iso, end_iso) -> float | None:
    """Seconds between two ISO stamps, or None when either is missing or
    unreadable - an unknowable duration is omitted, never zeroed."""
    if not start_iso or not end_iso:
        return None
    try:
        start = datetime.fromisoformat(str(start_iso))
        end = datetime.fromisoformat(str(end_iso))
    except (TypeError, ValueError):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (end - start).total_seconds()


def max_lifetime_bounds(settings) -> tuple[float, float]:
    """The (minimum, maximum) a max_lifetime_seconds may take.

    ONE definition, used by every write path (the launch request and the
    per-instance route), because a bound that two call sites compute
    separately is a bound with a hole in it.

    The minimum defaults to the boot budget plus one idle timeout rather than
    reusing idle.timeout_min_seconds. `launched_at` — the ceiling's anchor —
    is stamped when the provider ACCEPTS the launch, before the box boots,
    and a big multi-GPU instance takes 15-40 minutes to come up. A 30-minute
    ceiling would therefore destroy an 8xH100 the moment it first connected,
    after the user had already paid for the entire boot.
    """
    configured = settings.idle.max_lifetime_min_seconds
    if configured is None:
        configured = (settings.launch.boot_timeout_seconds
                      + settings.idle.timeout_seconds)
    return float(configured), float(settings.idle.max_lifetime_max_seconds)


def validate_max_lifetime(settings, value: float | None) -> float | None:
    """Check a requested ceiling, or raise LaunchRejected explaining why not.

    REJECTS rather than clamps. Silently doubling a number the user typed
    into a control that destroys instances is its own kind of lie: they would
    believe the box dies at 30 minutes and find it alive at 70. The message
    names the boot budget, because that is the part nobody expects.
    """
    if value is None:
        return None
    minimum, maximum = max_lifetime_bounds(settings)
    if value < minimum:
        raise LaunchRejected(
            400,
            f"max_lifetime_seconds must be at least {minimum:.0f}s "
            f"({minimum / 3600:.1f}h). The clock starts when the provider "
            f"ACCEPTS the launch, before the instance boots, and boot alone "
            f"is budgeted at {settings.launch.boot_timeout_seconds:.0f}s "
            f"(15-40 minutes on a multi-GPU box). A shorter ceiling would "
            f"terminate the instance about as soon as it became usable.",
        )
    if value > maximum:
        raise LaunchRejected(
            400,
            f"max_lifetime_seconds must be at most {maximum:.0f}s "
            f"({maximum / 86400:.0f} days).",
        )
    return float(value)


def validate_max_active(settings, value: float | None) -> float | None:
    """Check a requested ACTIVE ceiling, or raise LaunchRejected.

    Anchored at active_at (health-check pass), so unlike max_lifetime it
    needs no boot budget in its floor: the clock has not started while the
    box boots. The floor is the idle-timeout floor - a bound shorter than
    the shortest idle window would terminate a box faster than the idle
    sweep is even allowed to. Rejects rather than clamps, same reasoning as
    validate_max_lifetime: silently rewriting a number that destroys
    instances is its own kind of lie.
    """
    if value is None:
        return None
    minimum = float(settings.idle.timeout_min_seconds)
    maximum = float(settings.idle.max_lifetime_max_seconds)
    if value < minimum:
        raise LaunchRejected(
            400,
            f"max_active_seconds must be at least {minimum:.0f}s. It is "
            f"anchored at the moment the instance becomes ACTIVE (boot "
            f"excluded), so there is no boot budget to add - but a bound "
            f"below the minimum idle window would destroy the box faster "
            f"than the idle sweep itself is permitted to.")
    if value > maximum:
        raise LaunchRejected(
            400,
            f"max_active_seconds must be at most {maximum:.0f}s "
            f"({maximum / 86400:.0f} days).")
    return float(value)


def launch_options(
    instance_types: dict[str, InstanceTypeInfo],
    filesystems: list[FilesystemInfo],
    provider: str = "lambda",
) -> dict:
    """Cross-reference the live catalog with the user's filesystems into a
    ranked list of launchable targets. Pure — no I/O.

    `provider` names the cloud these targets belong to and is stamped on
    every row. A target is only launchable on the cloud it came from, and
    the account's default provider can be switched (Phase 102), so a row
    that did not say which cloud it described was one toggle away from
    being copied straight into a guaranteed rejection.

    A launch must name a (type, region, filesystem) that all line up: the type
    needs current capacity in that region, and Lambda filesystems are
    region-locked, so a persistent launch can only use a filesystem that lives
    in the launch's region. Guessing a region blind is how a launch hits
    "no capacity" or a region-mismatch rejection. Each returned target is a
    combination Lambda can actually satisfy right now, so a caller can pick the
    top one instead of guessing.

    Ranking puts the targets the user most likely wants first:
      1. co-located with EXISTING data (a filesystem with bytes_used > 0 in
         that region) — keeps a job next to the files it reads/writes;
      2. co-located with an empty filesystem in that region;
      3. scratch-only (capacity, but no filesystem there — everything is
         ephemeral);
    and within each band, cheaper first. `unavailable` lists types with zero
    regions reporting capacity right now.
    """
    fs_by_region: dict[str, list[FilesystemInfo]] = {}
    for fs in filesystems:
        fs_by_region.setdefault(fs.region, []).append(fs)

    targets: list[dict] = []
    unavailable: list[str] = []
    for name, t in instance_types.items():
        if not t.regions_with_capacity:
            unavailable.append(name)
            continue
        price = t.price_cents_per_hour / 100
        for region in t.regions_with_capacity:
            here = fs_by_region.get(region, [])
            if here:
                # Most-populated filesystem first, so "where my data is" wins.
                for fs in sorted(here, key=lambda f: -f.bytes_used):
                    targets.append({
                        "provider": provider,
                        "instance_type": name,
                        "gpu": t.gpu_description,
                        "price_usd_per_hour": price,
                        "region": region,
                        "filesystem": fs.name,
                        "filesystem_bytes_used": fs.bytes_used,
                        "colocated": True,
                    })
            else:
                targets.append({
                    "provider": provider,
                    "instance_type": name,
                    "gpu": t.gpu_description,
                    "price_usd_per_hour": price,
                    "region": region,
                    "filesystem": None,
                    "filesystem_bytes_used": 0,
                    "colocated": False,
                })

    def rank(o: dict) -> tuple:
        return (
            not o["colocated"],                 # co-located first
            o["filesystem_bytes_used"] == 0,    # existing data before empty
            o["price_usd_per_hour"],            # then cheaper
            o["instance_type"],
            o["region"],
        )

    targets.sort(key=rank)
    return {"targets": targets, "unavailable": sorted(unavailable)}


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        providers: 'ProviderRegistry',
        db: Database,
        *,
        connect_fn: Callable[[str], Callable[[], Awaitable]] | None = None,
        sidecar_factory: Callable[[ManagedConnection], SidecarClient] | None = None,
        model_client_factory: Callable[[ManagedConnection], "ModelClient"] | None = None,
        prefs=None,          # PreferenceStore: the data-safety policy
        notifier=None,       # NotificationCenter: pings on rescue/blocked
        policy=None,         # policy.Policy: the reviewable launch policy
    ):
        from .policy import PERMISSIVE
        self.policy = policy or PERMISSIVE
        self.settings = settings
        if not isinstance(providers, ProviderRegistry):
            from .providers import CloudProvider, LambdaProvider
            if isinstance(providers, CloudProvider):
                reg = ProviderRegistry()
                reg.register('lambda', providers)
                providers = reg
            else:
                reg = ProviderRegistry()
                reg.register('lambda', LambdaProvider(providers))
                providers = reg
        self.providers = providers
        lambda_prov = providers.get_provider('lambda')
        self.client = getattr(lambda_prov, 'client', None)
        self.db = db
        # Both optional so a bare Orchestrator (tests, scripts) keeps working:
        # no prefs means the built-in defaults, no notifier means no pings.
        self.prefs = prefs
        self.notifier = notifier
        # Injected post-construction by create_app (the Worklog is built
        # later there); None = no instance-lifetime entries, never an error.
        self.worklog = None
        # connect_fn(host) returns the coroutine factory a ManagedConnection
        # uses to dial; tests and mock mode inject one, real mode uses asyncssh.
        self._connect_fn = connect_fn
        # sidecar_factory(conn) builds the sidecar client for an instance;
        # mock mode injects MockSidecarClient.
        self._sidecar_factory = sidecar_factory or RealSidecarClient
        # model_client_factory(conn) reaches a model served on the instance
        # (vllm-serve etc.) over the same managed connection.
        self._model_client_factory = model_client_factory or RealModelClient
        self.managers: dict[str, ConnectionManager] = {
            m.mode: m
            for m in (DirectSSHConnectionManager(), TailscaleConnectionManager())
        }
        self.connections: dict[str, ManagedConnection] = {}   # lambda instance id -> conn
        self._launch_tasks: dict[str, asyncio.Task] = {}
        # Instances a provisioning sweep is currently asking about. TWO
        # callers drive that gate - the launch tail's fast-path loop and the
        # telemetry tick - and it awaits several SSH probes, so without this
        # they interleave: both can read the row as still 'booting', both
        # find the session cannot reach docker, and the second redial drops
        # the session the first one just rebuilt.
        self._provisioning_sweeps: set[str] = set()
        # Evidence from the last instances_with_state() sweep: which ids the
        # cloud listed as running, and which providers actually answered.
        # Kept so read-only consumers (the polled spend routes) can classify
        # launch rows against the same evidence without firing a cloud call
        # of their own. None until the first sweep — "no snapshot", which the
        # cost model reads as "trust the row" rather than "it all stopped".
        self._live_ids: set[str] | None = None
        self._listed_providers: set[str] | None = None
        # Blocked-termination notification dedupe (Phase 76b, F7).
        # instance id -> the unsaved file set we last pinged about. A blocked
        # termination is RETRIED forever by two loops (the idle loop every
        # ~15s, the auto-manage loop faster), and each retry re-entered
        # _notify_blocked: ~4 desktop pings a minute, indefinitely, saying the
        # same thing. One ping per instance until the unsaved set CHANGES —
        # a changed set is genuinely new information ("you saved 3 of 5").
        self._blocked_notified: dict[str, frozenset[str]] = {}
        # TOFU host-key pins live next to the database (both are local state).
        self.host_keys = HostKeyStore(
            str(Path(settings.db_path).with_name("host_keys.json"))
        )

    # -- public API ------------------------------------------------------------

    def resolve_provider(self, provider: str | None) -> str:
        """Which cloud a launch lands on. None = the account's default.

        The Phase 102 toggle lives here rather than in any client, for the
        usual reason: a client that resolved it would be a second place
        where "which cloud" is decided, and the two would drift. Reading
        the preference at launch time (not at startup) is what lets one
        flip on the Settings page reach every agent already running.

        An explicit name always wins and is never overridden. A name that
        no registered provider answers to is REFUSED here, with the list of
        names that would work - including when it came from the stored
        default, because a provider can be de-registered (or a config file
        hand-edited) after the choice was made, and falling through would
        surface as a 500 from deep inside the registry.
        """
        from_default = provider is None
        if from_default:
            stored = self.prefs.get().providers if self.prefs else None
            provider = (stored.default_provider if stored else "") or "lambda"
        registered = sorted(name for name, _ in self.providers.items())
        if provider not in registered:
            known = ", ".join(registered) or "(none)"
            if from_default:
                raise LaunchRejected(
                    400,
                    f"The account's default provider is '{provider}', which "
                    f"this backend has not registered. Registered providers: "
                    f"{known}. Pick one under Settings -> Default provider, "
                    f"or name a provider on the launch itself.",
                    reason_code="provider")
            raise LaunchRejected(
                400,
                f"Unknown provider '{provider}'. Registered providers: "
                f"{known}.",
                reason_code="provider")
        return provider

    async def request_launch(
        self,
        *,
        instance_type: str,
        region: str,
        filesystem: str,
        connection_mode: str | None = None,
        ssh_key_name: str | None = None,
        name: str = "",
        idle_timeout_seconds: float | None = None,
        max_lifetime_seconds: float | None = None,
        max_active_seconds: float | None = None,
        provider: str | None = None,
        created_by: str | None = None,
        purpose: str = "",
        extra_filesystems: list[str] | None = None,
        bootstrap: str = "",
    ) -> dict:
        """Validate and admit a launch; returns the persisted launch row.

        Raises LaunchRejected (with an HTTP status) on any validation or
        guardrail failure. On success the retry/boot/connect pipeline runs
        as a background task; poll GET /launches/{id} for progress.

        created_by (Phase 79) is the principal this launch is attributed
        to. Passed EXPLICITLY rather than read from the request context
        here, because half this function's callers are background loops
        (auto-manage, capacity watches, autopilot) that never saw a
        request and attribute through a chain: the job's creator, the
        watch's creator, the run's creator.

        provider (Phase 102) is None for "whatever cloud this account
        defaults to", which is what a client that never heard of the
        toggle sends. Callers that must not move clouds under a toggle
        name theirs; see resolve_provider. The RESOLVED name is what lands
        on the launch row - a row saying null would leave every later
        reader (boot poll, reconcile sweep, spend attribution) guessing.

        extra_filesystems (Phase 103) are MOUNTED alongside `filesystem`,
        nothing more: the box gets /lambda/nfs/<name> for each, reachable
        from run_command, detached commands, the file routes and a shell.
        `filesystem` remains THE primary - template mounts ({persistent}),
        sync_outputs and relative file paths all resolve against it - so an
        extra is a place to read from and write to by absolute path, not a
        second persistent home.
        bootstrap (Phase 104) is a setup script stored on the launch row
        and started once on the instance by the dispatcher's reconciling
        sweep, NOT here: at every point this pipeline could call it the
        connection is still coming up. See bootstrap.py. Nothing about the
        launch depends on it, and a launch with one is admitted exactly as
        a launch without one.
        """
        extra_filesystems = list(extra_filesystems or [])
        mode = connection_mode or self.settings.default_connection_mode
        self._validate_mode(mode)

        # Resolved before anything else reads it: every check below, and the
        # row itself, must be about the cloud this launch will really use.
        provider = self.resolve_provider(provider)

        # Checked before the catalog call: this refusal costs nothing, needs
        # no API, and is the more actionable message when both would apply.
        # Extras land in the same refusal (Phase 103): they are the same
        # Lambda-only feature, and enforcing here rather than in a client
        # means every caller - dashboard, MCP, autopilot - hits one wall.
        if (filesystem or extra_filesystems) and provider != 'lambda':
            raise LaunchRejected(
                400,
                f"{provider} launches are scratch-only for now: persistent "
                f"filesystems are a Lambda feature (GCP Filestore is not "
                f"wired up). Launch without a filesystem "
                f"(extra_filesystems included).")

        # Extras attach ALONGSIDE a primary; they never stand in for one.
        # Admitting extras onto a scratch-only box would produce an instance
        # whose {persistent} mounts, sync and relative paths still have
        # nowhere to go while /lambda/nfs held real data - an incoherent
        # state nobody asked for. Refuse and say which name to promote.
        if extra_filesystems and not filesystem:
            raise LaunchRejected(
                422,
                f"extra_filesystems needs a primary filesystem: this launch "
                f"asks for {', '.join(extra_filesystems)} with none chosen. "
                f"Jobs, sync and relative paths all resolve against the "
                f"primary, so pass one of them as `filesystem` and keep the "
                f"rest as extras.")

        if len(extra_filesystems) > MAX_EXTRA_FILESYSTEMS:
            raise LaunchRejected(
                422,
                f"Too many extra filesystems: {len(extra_filesystems)} asked "
                f"for, and Manifold attaches at most "
                f"{MAX_EXTRA_FILESYSTEMS} beyond the primary one. Attach the "
                f"ones this run reads and writes; the rest stay unmounted.")

        seen = [filesystem] if filesystem else []
        for extra in extra_filesystems:
            if not extra.strip():
                raise LaunchRejected(
                    422,
                    "extra_filesystems contains an empty name. Drop the "
                    "entry rather than sending a blank one - a blank is not "
                    "'no filesystem', it is a name we cannot look up.")
            if extra in seen:
                raise LaunchRejected(
                    422,
                    f"Filesystem '{extra}' is listed twice. Lambda mounts "
                    f"each filesystem once at /lambda/nfs/{extra}; name it "
                    f"once (as the primary or as an extra, not both).")
            seen.append(extra)

        cloud_provider = self.providers.get_provider(provider)
        # A provider nobody has set up yet has an EMPTY catalog, and an empty
        # catalog would refuse this launch with "Valid types: " - a dead end
        # that reads like a bad instance type. Ask the provider why first.
        unconfigured = cloud_provider.unconfigured_reason()
        if unconfigured:
            raise LaunchRejected(400, unconfigured, reason_code="provider")
        types = {t.name: t for t in await self._catalog_of(cloud_provider)}
        if instance_type not in types:
            raise LaunchRejected(
                400,
                f"Unknown instance type '{instance_type}'. "
                f"Valid types: {', '.join(sorted(types))}",
            )

        if idle_timeout_seconds is not None:
            clamped = max(
                self.settings.idle.timeout_min_seconds,
                min(idle_timeout_seconds, self.settings.idle.timeout_max_seconds)
            )
            if clamped != idle_timeout_seconds:
                self.db.record_audit(
                    "backend", "idle_timeout_clamp",
                    f"requested {idle_timeout_seconds}s clamped to {clamped}s"
                )
                idle_timeout_seconds = clamped

        # The ceiling REJECTS out-of-range values instead of clamping them:
        # see validate_max_lifetime. Checked before anything is created, so a
        # bad number costs nothing.
        max_lifetime_seconds = validate_max_lifetime(
            self.settings, max_lifetime_seconds)

        # Filesystem is OPTIONAL (Phase 39): "" launches a scratch-only
        # instance in any region, including ones where the user has no
        # filesystem. Everything on it is ephemeral - jobs that mount
        # {persistent} refuse to run, sync has nowhere to go, and the
        # termination rescue can only save files by downloading them here.
        # The launch form says all of this before the user clicks.
        if filesystem and self.client:
            filesystems = {fs.name: fs
                           for fs in await self.client.list_filesystems()}
            if filesystem not in filesystems:
                raise LaunchRejected(
                    400,
                    f"Unknown filesystem '{filesystem}'. "
                    f"Available: {', '.join(sorted(filesystems)) or '(none)'}"
                    f" - or launch without one (scratch only).",
                )
            fs = filesystems[filesystem]
            if fs.region != region:
                raise LaunchRejected(
                    400,
                    f"Region mismatch: filesystem '{filesystem}' lives in "
                    f"{fs.region} but the launch requests {region}. Lambda "
                    f"filesystems are region-locked and can only be attached "
                    f"at launch. Launch in {fs.region} instead, or launch "
                    f"without a filesystem (scratch only).",
                )
            # Every extra passes the SAME two checks as the primary, one
            # name at a time, and the refusal names WHICH one failed: with
            # several filesystems in the request, "region mismatch" without
            # the offender is a puzzle rather than a fix.
            for extra in extra_filesystems:
                if extra not in filesystems:
                    raise LaunchRejected(
                        400,
                        f"Unknown filesystem '{extra}' in extra_filesystems. "
                        f"Available: "
                        f"{', '.join(sorted(filesystems)) or '(none)'}"
                        f" - or drop it and launch with '{filesystem}' "
                        f"alone.",
                    )
                extra_fs = filesystems[extra]
                if extra_fs.region != region:
                    raise LaunchRejected(
                        400,
                        f"Region mismatch: extra filesystem '{extra}' lives "
                        f"in {extra_fs.region} but the launch requests "
                        f"{region}. Lambda filesystems are region-locked and "
                        f"can only be attached at launch, so a filesystem "
                        f"from another region cannot be mounted here at all. "
                        f"Drop '{extra}', or launch in {extra_fs.region} if "
                        f"that is where the data lives.",
                    )

        # SSH key: per-launch choice wins, config.yaml is the fallback. The
        # name must be registered with Lambda or the launch call would fail
        # minutes later — catch typos now.
        resolved_key = ssh_key_name or self.settings.ssh.key_name
        if provider == 'lambda':
            registered = [k.name for k in await self.client.list_ssh_keys()]
        else:
            registered = [resolved_key]
        if not resolved_key:
            raise LaunchRejected(
                400,
                "No SSH key selected. Pick one of your Lambda SSH keys "
                f"({', '.join(registered) or 'none registered yet'}) or set "
                "ssh.key_name in config.yaml.",
            )
        if resolved_key not in registered:
            raise LaunchRejected(
                400,
                f"SSH key '{resolved_key}' is not registered with Lambda. "
                f"Registered keys: {', '.join(registered) or '(none)'}.",
            )

        price = types[instance_type].price_cents_per_hour
        # Phase 82: the reviewable policy, checked before any money-side
        # guard (a denial costs nothing and names its rule). Binds every
        # principal INCLUDING the owner: the way around policy.yaml is a
        # commit to policy.yaml, not a credential.
        role = self._role_of(created_by)
        denial = self.policy.allows_launch(
            instance_type=instance_type, region=region,
            hourly_rate_usd=price / 100.0,
            max_lifetime_seconds=max_lifetime_seconds, role=role)
        if denial is not None:
            self.db.record_audit(
                created_by or "api", "policy_denied",
                f"launch {instance_type} in {region}: {denial}")
            raise LaunchRejected(
                403, f"Launch denied by policy: {denial}. The policy file "
                     f"is {self.policy.source}; changing it is a reviewed "
                     f"edit, not a setting.",
                reason_code="policy")
        current_spend, budget_cents = await self._guard_capacity(
            cloud_provider=cloud_provider,
            instance_type=instance_type,
            unit_price_cents=price,
            added_count=1,
            created_by=created_by,
        )

        # Fallback types must exist, independently fit the budget, AND
        # pass the same policy the requested type passed; ones that don't
        # are skipped rather than failing the launch.
        candidates = [instance_type]
        for fb in self.settings.launch.fallback_instance_types:
            if fb == instance_type or fb in candidates or fb not in types:
                continue
            if current_spend + types[fb].price_cents_per_hour > budget_cents:
                continue
            if self.policy.allows_launch(
                    instance_type=fb, region=region,
                    hourly_rate_usd=types[fb].price_cents_per_hour / 100.0,
                    max_lifetime_seconds=max_lifetime_seconds,
                    role=role) is not None:
                continue
            candidates.append(fb)

        launch_id = self.db.create_launch(
            requested_type=instance_type,
            region=region,
            filesystem=filesystem,
            connection_mode=mode,
            hourly_rate_cents=price,
            idle_timeout_seconds=idle_timeout_seconds,
            max_lifetime_seconds=max_lifetime_seconds,
            max_active_seconds=validate_max_active(
                self.settings, max_active_seconds),
            provider=provider,
            created_by=created_by,
            purpose=purpose,
            extra_filesystems=extra_filesystems,
            bootstrap=bootstrap,
        )
        plan = LaunchPlan(
            launch_id=launch_id,
            region=region,
            filesystem=filesystem,
            connection_mode=mode,
            ssh_key_name=resolved_key,
            types_to_try=candidates,
            prices={t: types[t].price_cents_per_hour for t in candidates},
            name=name or f"manifold-{launch_id}",
            provider=provider,
            extra_filesystems=extra_filesystems,
        )
        self._launch_tasks[launch_id] = asyncio.create_task(self._run_launch(plan))
        return self.db.get_launch(launch_id)

    async def _catalog_of(self, cloud_provider: CloudProvider) -> list:
        """The provider's instance types, with its own failures translated.

        A provider that cannot be READ raises rather than returning an
        empty catalog, and an exception loose on the launch path used to
        leave POST /instances with a bare 500 - hiding a message like "run
        `gcloud auth application-default login`" that would have fixed it
        in one command. Re-raised as LaunchRejected so every caller (route,
        auto-manage loop, capacity watch, autopilot) handles it the way it
        already handles a refusal, and the provider's own sentence reaches
        the human intact. Same status split as GET /gcp/quota: 503 for
        "could not read it", 502 for "it answered with a problem".
        """
        try:
            return await cloud_provider.list_instance_types()
        except ProviderUnavailable as exc:
            raise LaunchRejected(503, str(exc), reason_code="provider") from exc
        except ProviderError as exc:
            raise LaunchRejected(502, str(exc), reason_code="provider") from exc

    def _role_of(self, created_by: str | None) -> str | None:
        """The role a launch is policied under. Owner is admin by
        definition; minted principals carry their row's role; legacy
        actors and open mode have none and are bound by the global
        policy block alone."""
        if not created_by:
            return None
        if created_by == "owner":
            return "admin"
        row = self.db.principal_by_name(created_by)
        return (row.get("role") or "operator") if row else None

    async def _guard_capacity(
        self,
        *,
        cloud_provider: CloudProvider,
        instance_type: str,
        unit_price_cents: int,
        added_count: int,
        created_by: str | None = None,
    ) -> tuple[int, int]:
        """The concurrency and budget guards, shared by single launches
        (added_count=1) and whole clusters (added_count=N, judged atomically
        so N nodes cannot each sneak past a per-node check).

        Guards run against LIVE state, not our database, so instances
        launched outside Manifold still count toward the limits. fresh=True
        bypasses the list cache: a spend guard must never pass on a stale
        snapshot (two quick launches both seeing "0 running"). On top of the
        live list, BOTH guards fold in launches already ADMITTED but not yet
        visible on the cloud (pending rows) — each launch runs in a detached
        task, so without this, siblings admitted in the same window would all
        see the same baseline: the concurrency guard adds their COUNT, the
        budget guard adds their hourly SPEND.

        Returns (current_spend_cents, budget_cents) so request_launch can
        filter fallback types against the same numbers this guard used.
        Raises LaunchRejected on violation.

        ONE ASYMMETRY, ON PURPOSE (Phase 102). The live half of the
        baseline comes from the LAUNCHING provider alone - it is the only
        provider we are about to spend money on, and asking every
        registered provider would let one unreadable cloud veto a launch on
        a working one. The pending half (rows admitted but not yet visible)
        is account-wide, because a pending row is Manifold's own commitment
        to spend and it counts wherever it was made. So a running Lambda
        box does not hold the concurrency slot against a GCP launch, while
        a Lambda launch admitted seconds ago does. Under the default
        limit of one instance that is deliberately the safer direction to
        err in - both halves count something real, and neither invents a
        number for a cloud it could not read. Pinned in
        tests/test_provider_toggle.py.
        """
        try:
            listed = await cloud_provider.list_instances(fresh=True)
        except ProviderUnavailable as exc:
            # A guard that cannot see what is running must not guess zero:
            # that is the one wrong answer that spends money. Refused with
            # the provider's own fix, same translation as _catalog_of.
            raise LaunchRejected(
                503,
                f"Cannot check what is already running before launching: "
                f"{exc}", reason_code="provider") from exc
        except ProviderError as exc:
            raise LaunchRejected(
                502,
                f"Cannot check what is already running before launching: "
                f"{exc}", reason_code="provider") from exc
        running = [i for i in listed if i.is_running]
        pending = self.db.pending_launch_count()
        in_flight = len(running) + pending
        # The NUMBERS come from Settings when the user set them there
        # (preferences.guardrails; 0 = unset), falling back to config.yaml.
        # The guards themselves never move out of this function.
        prefs_guard = self.prefs.get().guardrails if self.prefs else None
        limit = (prefs_guard.max_concurrent_instances
                 if prefs_guard and prefs_guard.max_concurrent_instances > 0
                 else self.settings.guardrails.max_concurrent_instances)
        if in_flight + added_count > limit:
            if added_count == 1:
                detail = (
                    f"Concurrency guard: {in_flight} instance(s) running or "
                    f"launching, limit is {limit}. Terminate one first or "
                    f"raise the limit under Settings -> Spending guardrails."
                )
            else:
                detail = (
                    f"Concurrency guard: launching {added_count} nodes with "
                    f"{in_flight} instance(s) running or launching would "
                    f"exceed the limit of {limit}. Terminate instances first "
                    f"or raise the limit under Settings -> Spending guardrails."
                )
            raise LaunchRejected(409, detail, reason_code="concurrency")

        # Cloud-visible spend PLUS the spend already committed by pending
        # launches — the budget parallel of the pending count above.
        current_spend = (sum(i.hourly_rate_cents for i in running)
                         + self.db.pending_launch_spend_cents())
        budget_usd = (prefs_guard.max_hourly_spend_usd
                      if prefs_guard and prefs_guard.max_hourly_spend_usd > 0
                      else self.settings.guardrails.max_hourly_spend_usd)
        budget_cents = round(budget_usd * 100)
        added_cents = added_count * unit_price_cents
        if current_spend + added_cents > budget_cents:
            if added_count == 1:
                detail = (
                    f"Budget guard: launching {instance_type} "
                    f"(${added_cents / 100:.2f}/hr) would bring hourly spend to "
                    f"${(current_spend + added_cents) / 100:.2f}, over the "
                    f"${budget_cents / 100:.2f} limit "
                    f"(Settings -> Spending guardrails)."
                )
            else:
                detail = (
                    f"Budget guard: launching {added_count}x {instance_type} "
                    f"(${added_cents / 100:.2f}/hr total) would bring hourly "
                    f"spend to ${(current_spend + added_cents) / 100:.2f}, "
                    f"over the ${budget_cents / 100:.2f} limit "
                    f"(Settings -> Spending guardrails)."
                )
            raise LaunchRejected(409, detail, reason_code="budget")

        # Phase 81: the per-principal ceiling, judged against the same live
        # baseline as the global guards (and the same pending-launch
        # double-admit protection, filtered to this principal). Attribution
        # is the chain's origin, so an auto-managed job's launch counts
        # against whoever enqueued the job. "owner" and legacy actors have
        # no row and no ceiling; a row without a ceiling is unlimited.
        if created_by:
            row = self.db.principal_by_name(created_by)
            ceiling_usd = (row or {}).get("max_hourly_spend_usd")
            if ceiling_usd:
                principal_spend = self.db.principal_pending_spend_cents(
                    created_by)
                for inst in running:
                    launch = self.db.find_launch_by_instance(inst.id)
                    if launch and launch.get("created_by") == created_by:
                        principal_spend += inst.hourly_rate_cents
                ceiling_cents = round(ceiling_usd * 100)
                added_cents = added_count * unit_price_cents
                if principal_spend + added_cents > ceiling_cents:
                    raise LaunchRejected(
                        409,
                        f"Principal budget guard: '{created_by}' is burning "
                        f"${principal_spend / 100:.2f}/hr and its ceiling is "
                        f"${ceiling_cents / 100:.2f}/hr; this launch "
                        f"(${added_cents / 100:.2f}/hr) would exceed it. "
                        f"Terminate one of its instances, or have an admin "
                        f"raise the ceiling (Settings -> API access).",
                        reason_code="principal_budget")
        return current_spend, budget_cents

    async def create_filesystem(self, name: str, region: str) -> dict:
        """Create a persistent filesystem through the guarded gateway.

        Creation is free (storage bills by GB-month used), so no spend
        guard applies; validation here exists to fail fast with a clear
        message instead of a raw API 400, and to audit the action.
        """
        name = (name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,59}", name):
            raise LaunchRejected(
                422,
                "Filesystem names are 1-60 characters: letters, digits, "
                "dots, dashes, underscores; starting with a letter or digit.",
            )
        from .lambda_api import KNOWN_REGIONS
        if region not in KNOWN_REGIONS:
            raise LaunchRejected(
                422,
                f"Unknown region '{region}'. Pick one from the region list.",
            )
        fs = await self.client.create_filesystem(name=name, region=region)
        self.db.record_audit(
            "dashboard", "filesystem_created", f"{fs.name} in {fs.region}",
        )
        return {
            "id": fs.id, "name": fs.name, "mount_point": fs.mount_point,
            "region": fs.region, "is_in_use": fs.is_in_use,
            "bytes_used": fs.bytes_used,
        }

    async def launch_cluster(
        self,
        *,
        instance_type: str,
        region: str,
        filesystem: str,
        node_count: int,
        connection_mode: str | None = None,
        ssh_key_name: str | None = None,
        name: str = "",
        provider: str = "lambda",
        created_by: str | None = None,
    ) -> dict:
        """Launch a multi-node GPU cluster with head and worker nodes.

        created_by (Phase 81; missed in 79's threading): the whole cluster
        is attributed to one principal, judged atomically against their
        ceiling here and stamped on every node's launch row below.

        provider stays EXPLICITLY 'lambda' by default and is passed down to
        every node's request_launch, so a cluster never resolves the Phase
        102 account default. A cluster is one interconnected unit: nodes
        split across two clouds could not reach each other, and today only
        the Lambda path is proven multi-node. Changing this means designing
        cross-provider clusters first, not flipping a default."""
        if node_count < 1:
            raise LaunchRejected(400, "Cluster must have at least 1 node.")
        if node_count > 16:
            raise LaunchRejected(
                400,
                f"Cluster of {node_count} nodes refused: the cap is 16 nodes "
                f"per cluster. Launch a smaller cluster.",
            )

        # Admit the WHOLE cluster before writing any row. Each node launches
        # in a detached task, so per-node guards would all see the same
        # baseline and admit N nodes against a 1-instance limit; guarding
        # N-at-once here closes that. Rejecting before create_cluster also
        # means a refused cluster leaves no ghost "provisioning" row behind.
        cloud_provider = self.providers.get_provider(provider)
        types = {t.name: t for t in await cloud_provider.list_instance_types()}
        if instance_type not in types:
            raise LaunchRejected(
                400,
                f"Unknown instance type '{instance_type}'. "
                f"Valid types: {', '.join(sorted(types))}",
            )
        # Phase 82: policy first, like the single-launch path - a denied
        # cluster must leave no ghost row and cost no guard work. Judged
        # once for the whole cluster (every node shares type and region).
        denial = self.policy.allows_launch(
            instance_type=instance_type, region=region,
            hourly_rate_usd=types[instance_type].price_cents_per_hour / 100.0,
            max_lifetime_seconds=None,
            role=self._role_of(created_by))
        if denial is not None:
            self.db.record_audit(
                created_by or "api", "policy_denied",
                f"cluster {node_count}x {instance_type} in {region}: "
                f"{denial}")
            raise LaunchRejected(
                403, f"Cluster denied by policy: {denial}. The policy file "
                     f"is {self.policy.source}.",
                reason_code="policy")
        await self._guard_capacity(
            cloud_provider=cloud_provider,
            instance_type=instance_type,
            unit_price_cents=types[instance_type].price_cents_per_hour,
            added_count=node_count,
            created_by=created_by,
        )

        cluster_id = uuid.uuid4().hex[:12]
        cluster_name = name or f"cluster-{cluster_id}"

        # Initialize cluster record in database
        self.db.create_cluster(
            cluster_id=cluster_id,
            name=cluster_name,
            gpu_type=instance_type,
            region=region,
            filesystem=filesystem,
            node_count=node_count,
        )

        try:
            # 1. Launch head node
            head_name = f"{cluster_name}-head"
            head_launch = await self.request_launch(
                instance_type=instance_type,
                region=region,
                filesystem=filesystem,
                connection_mode=connection_mode,
                ssh_key_name=ssh_key_name,
                name=head_name,
                provider=provider,
                created_by=created_by,
            )
            self.db.add_cluster_node(
                cluster_id=cluster_id,
                instance_id=head_launch["id"],
                role="head",
                node_index=0,
                status=head_launch.get("status", "provisioning"),
            )
            self.db.update_cluster_status(cluster_id, "provisioning", head_instance_id=head_launch["id"])

            # 2. Launch worker nodes
            for i in range(1, node_count):
                worker_name = f"{cluster_name}-worker-{i}"
                worker_launch = await self.request_launch(
                    instance_type=instance_type,
                    region=region,
                    filesystem=filesystem,
                    connection_mode=connection_mode,
                    ssh_key_name=ssh_key_name,
                    name=worker_name,
                    provider=provider,
                    created_by=created_by,
                )
                self.db.add_cluster_node(
                    cluster_id=cluster_id,
                    instance_id=worker_launch["id"],
                    role="worker",
                    node_index=i,
                    status=worker_launch.get("status", "provisioning"),
                )
        except Exception:
            # A node was refused mid-loop (a guard race, validation, or a
            # provider error). A half cluster is worse than none: tear down
            # the nodes that did launch, mark the cluster failed, and let
            # the original error reach the caller.
            logger.exception(
                "cluster %s failed mid-launch; rolling back", cluster_id)
            try:
                await self.terminate_cluster(cluster_id, force=False)
            except Exception:
                logger.exception(
                    "cluster %s rollback termination failed", cluster_id)
            try:
                self.db.update_cluster_status(cluster_id, "failed")
            except Exception:
                # A DB error marking the cluster failed must not mask the
                # real launch exception we are about to re-raise.
                logger.exception(
                    "cluster %s could not be marked failed", cluster_id)
            raise

        self.db.record_audit("backend", "cluster_launch", f"Launched cluster {cluster_id} with {node_count} nodes")
        return self.db.get_cluster(cluster_id) or {}

    async def terminate_cluster(self, cluster_id: str, *, force: bool = False) -> dict:
        """Safely terminate all instances in a cluster, honoring data-safety hooks."""
        cluster = self.db.get_cluster(cluster_id)
        if not cluster:
            raise LaunchRejected(404, f"Cluster {cluster_id} not found")

        reports = []
        for node in cluster.get("nodes", []):
            inst_id = node.get("instance_id")
            if not inst_id:
                continue
            launch = self.db.get_launch(inst_id)
            if launch and not launch.get("lambda_instance_id"):
                # The node was admitted but its detached launch task may
                # still be running (retrying for capacity). Cancel it FIRST,
                # or it would go on to create a real billed instance whose
                # row already says terminated.
                task = self._launch_tasks.get(inst_id)
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    # The task may have won the race and created the
                    # instance just before the cancel landed; re-read so a
                    # real instance goes down the real terminate path below.
                    launch = self.db.get_launch(inst_id)
            if launch and not launch.get("lambda_instance_id"):
                self.db.update_launch(inst_id, status="terminated", terminated_at=utcnow())
                reports.append({"instance_id": inst_id, "terminated": True})
            else:
                target_id = (launch.get("lambda_instance_id") if launch else None) or inst_id
                try:
                    res = await self.terminate(target_id, force=force)
                    reports.append(res)
                except TerminationBlocked as e:
                    reports.append({"error": str(e), "instance_id": target_id})
                except Exception as e:
                    reports.append({"error": str(e), "instance_id": target_id})

        blocked = any("error" in r for r in reports)
        if not blocked:
            self.db.update_cluster_status(cluster_id, "terminated")
            self.db.record_audit("backend", "terminate_cluster", f"Cluster {cluster_id} terminated")

        return {"cluster_id": cluster_id, "terminated": not blocked, "reports": reports}

    async def delete_filesystem(self, name: str, *,
                                confirm_name: str = "") -> dict:
        """Permanently delete a filesystem, with the data-safety dance.

        Deletion destroys every byte on the volume, so it follows the same
        philosophy as termination: the guard SHOWS what would be lost and
        refuses until the caller proves intent by typing the filesystem's
        name back (confirm_name). No force flag: unlike an instance, there
        is no rescue path for a whole filesystem - the only honest options
        are "type the name" or "keep it".
        """
        filesystems = {fs.name: fs for fs in
                       await self.client.list_filesystems()}
        fs = filesystems.get(name)
        if fs is None:
            raise LaunchRejected(
                404,
                f"No filesystem named '{name}'. "
                f"Have: {', '.join(sorted(filesystems)) or '(none)'}",
            )
        if fs.is_in_use:
            raise LaunchRejected(
                409,
                f"Filesystem '{name}' is attached to a running instance. "
                f"Terminate that instance first; Lambda cannot delete an "
                f"attached filesystem.",
            )
        if confirm_name != name:
            gib = fs.bytes_used / (1024 ** 3)
            raise LaunchRejected(
                428,
                f"Deleting '{name}' permanently destroys {gib:.1f} GiB in "
                f"{fs.region}. There is no undo and no rescue. To proceed, "
                f"repeat the exact filesystem name in confirm_name.",
            )
        await self.client.delete_filesystem(fs.id)
        self.db.record_audit(
            "dashboard", "filesystem_deleted",
            f"{name} in {fs.region} ({fs.bytes_used} bytes destroyed)",
        )
        return {"deleted": name, "region": fs.region,
                "bytes_destroyed": fs.bytes_used}

    def sidecar_for(self, instance_id: str) -> SidecarClient | None:
        conn = self.connections.get(instance_id)
        if conn is None:
            return None
        return self._sidecar_factory(conn)

    async def gpu_metrics_via_ssh(self, instance_id: str) -> dict | None:
        """GPU metrics without a sidecar, same payload shape as the
        sidecar's /metrics. Instances launched outside Manifold never got
        our cloud-init, so no sidecar runs on them - but the managed SSH
        connection is there, and nvidia-smi answers over it. Returns None
        when the connection is down or the box has no working nvidia-smi.
        """
        conn = self.connections.get(instance_id)
        if conn is None:
            return None
        try:
            code, out, _err = await conn.run(NVIDIA_SMI_METRICS, timeout=15)
        except Exception:
            return None
        if code != 0:
            return None
        gpus = []
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 4:
                continue
            try:
                gpus.append({
                    "name": parts[0],
                    "vram_used_mib": int(float(parts[1])),
                    "vram_total_mib": int(float(parts[2])),
                    "utilization_pct": int(float(parts[3])),
                })
            except ValueError:
                continue
        if not gpus:
            return None
        return {"available": True, "gpus": gpus, "source": "ssh"}

    async def diagnose_sidecar(self, instance_id: str) -> dict:
        """Ask the instance directly why its sidecar is not answering, over
        the managed SSH connection (which is known-good when we get here)."""
        conn = self.connections.get(instance_id)
        if conn is None:
            raise LaunchRejected(409, f"no managed connection to {instance_id}")
        if conn.state != ConnectionState.CONNECTED:
            raise LaunchRejected(
                409,
                f"SSH is not connected to {instance_id} "
                f"(state: {conn.state.value}); cannot probe the instance",
            )
        from .diagnostics import diagnose_sidecar as _diagnose
        return await _diagnose(conn.run)

    def model_client_for(self, instance_id: str) -> ModelClient | None:
        conn = self.connections.get(instance_id)
        if conn is None:
            return None
        return self._model_client_factory(conn)

    def _data_safety(self) -> "DataSafetyPrefs":
        from .preferences import DataSafetyPrefs
        return self.prefs.get().data_safety if self.prefs else DataSafetyPrefs()

    async def terminate(self, instance_id: str, *, force: bool = False,
                        caller: str | None = None,
                        confirm_owner: str = "",
                        reason: str = "") -> dict:
        """Terminate an instance, rescuing its data first.

        `caller` is the principal asking, and passing it OPTS IN to the
        ownership guard: an instance launched by a different principal is
        refused unless confirm_owner names that principal. It is passed
        explicitly, and defaults to None (no check), for the same reason
        request_launch takes created_by explicitly — half the callers here
        are background loops. The idle sweep and the ceiling terminate on
        the system's behalf, not a principal's, and must never be told a
        box "belongs to someone else"; that is the whole point of them.

        The guard is inert on a single-principal install, because every
        launch and every caller resolve to the same name. It starts
        protecting when per-principal tokens are issued (Phase 81 team
        mode), and it cannot protect a box with no recorded owner: a NULL
        created_by (adopted, or launched before Phase 94) is unattributed,
        and refusing on an unknown owner would just teach callers to pass
        confirm_owner reflexively.

        Unless force=true:
        1. Ask the sidecar what valuable files sit in ephemeral scratch (the
           disk that dies with the box).
        2. RESCUE them per the data-safety policy: rsync the whole scratch
           dir to the persistent volume (cheap, datacenter-local) and/or pull
           files down to this machine over the managed connection.
        3. Anything the rescue could not save decides the outcome:
           if_unsaveable="block" refuses the termination and pings you (data
           is unrecoverable, a billing hour is not); "terminate" proceeds and
           records exactly what was lost.

        If the sidecar is unreachable (still booting, connection down) we
        proceed — the hook is best-effort evidence, not a way to wedge
        termination. force=true skips all of it: the explicit "burn it".
        """
        # Ownership first: refuse before the rescue runs. Rescue talks to the
        # instance over SSH and can take seconds; a call that was never going
        # to be allowed should not touch someone else's box at all.
        if caller is not None:
            owned = self.db.find_launch_by_instance(instance_id)
            owner = (owned or {}).get("created_by") or ""
            if owner and owner != caller and confirm_owner != owner:
                self.db.record_audit(
                    caller, "terminate_refused",
                    f"{instance_id}: launched by {owner}, not {caller}; "
                    f"refused (pass confirm_owner to override)")
                raise TerminationRefused(
                    instance_id, owner, (owned or {}).get("purpose"), caller)
            if owner and owner != caller:
                # Allowed, but deliberately loud: one principal destroying
                # another's box is exactly the event tonight's incident
                # review needed and could not find.
                self.db.record_audit(
                    caller, "terminate_across_owners",
                    f"{instance_id}: launched by {owner}, terminated by "
                    f"{caller} with confirm_owner")

        report = None
        if not force:
            report = await self.rescue(instance_id)
            unsaved = report.get("unsaved", [])
            if unsaved and self._data_safety().if_unsaveable == "block":
                self._notify_blocked(instance_id, report)
                raise TerminationBlocked(instance_id, unsaved, report)
            if unsaved:
                self.db.record_audit(
                    "backend", "terminate_data_lost",
                    f"{instance_id}: terminating with {len(unsaved)} unsaved "
                    f"file(s) (data_safety.if_unsaveable=terminate)")

        conn = self.connections.pop(instance_id, None)
        if conn is not None:
            await conn.close()
            # The IP may be recycled to a future instance with a new host
            # key; keeping the pin would wrongly reject that instance.
            self.host_keys.forget(conn.host)
        launch = self.db.find_launch_by_instance(instance_id)
        provider_name = launch['provider'] if launch else 'lambda'
        await self.providers.get_provider(provider_name).terminate_instance(instance_id)
        launch = self.db.find_launch_by_instance(instance_id)
        if launch:
            self.db.update_launch(
                launch["id"], status="terminated", terminated_at=utcnow()
            )
            # Phase 97: an instance LIFETIME is work worth a worklog entry.
            # get_work_log answered "what happened on this account" with
            # jobs and autopilot runs only, so days of raw GPU sessions -
            # launches, notes, durations, costs, terminations - left no
            # trace, and "what are these A100s?" became a whodunit. The
            # data was already held; it just never reached the log.
            self._worklog_instance(launch, instance_id,
                                   reason or (f"requested by {caller}"
                                              if caller else "requested"))
        # A terminated box's registered endpoints die with it: a route to a
        # gone instance that still looks like a served model is exactly the
        # kind of stale fact this codebase exists to not have.
        self.db.delete_endpoints_for_instance(instance_id)
        remove_ssh_config_block(instance_id)
        # The box is gone: forget its blocked-notification state so a future
        # instance id collision (or a re-adopted box) can ping again.
        self._blocked_notified.pop(instance_id, None)
        return {"instance_id": instance_id, "terminated": True,
                "rescue": report}

    def _worklog_instance(self, launch: dict, instance_id: str,
                          reason: str) -> None:
        """One worklog entry per instance lifetime, from data already held.

        Best-effort and never raises: a log entry must not be able to break
        a termination. Cost is the same figure spend.py reports - rate x
        wall clock from acceptance, an UPPER BOUND, and says so; unknowable
        pieces are omitted rather than zeroed.
        """
        if self.worklog is None:
            return
        try:
            lines = [
                f"instance {instance_id[:12]} "
                f"({launch.get('launched_type') or launch.get('requested_type')}"
                f", {launch.get('region')})",
                (f"purpose: {launch['purpose']}" if launch.get("purpose")
                 else "purpose: (none stated)"),
                (f"launched by {launch['created_by']}"
                 if launch.get("created_by") else "launcher unattributed"),
                f"ended: {reason}",
            ]
            for label, start in (("active", launch.get("active_at")),
                                 ("total", launch.get("launched_at"))):
                secs = _interval_seconds(start, utcnow())
                if secs is not None:
                    lines.append(f"{label} time: {secs / 3600:.1f}h")
            rate = launch.get("hourly_rate_cents")
            total = _interval_seconds(launch.get("launched_at"), utcnow())
            if rate and total is not None:
                lines.append(
                    f"cost upper bound: ~${rate / 100 * total / 3600:.2f} "
                    f"(rate x wall clock from acceptance; billing really "
                    f"starts at health-check pass)")
            self.worklog.record("instance session ended", lines)
        except Exception:   # noqa: BLE001
            logger.warning("instance worklog entry failed for %s",
                           instance_id, exc_info=True)

    @staticmethod
    def _unsaved_key(report: dict) -> frozenset[str]:
        """Identity of an unsaved file set, for notification dedupe.

        Paths only: sizes and error strings jitter between rescue attempts
        (a partial download, a different sidecar error) and would make an
        unchanged situation look new on every retry.
        """
        return frozenset(
            str(f.get("path", f)) for f in report.get("unsaved", []))

    def _notify_blocked(self, instance_id: str, report: dict) -> None:
        if self.notifier is None:
            return
        key = self._unsaved_key(report)
        if self._blocked_notified.get(instance_id) == key:
            return              # same files, same block: already told them
        self._blocked_notified[instance_id] = key
        unsaved = report.get("unsaved", [])
        self.notifier.notify(
            "data_transferred",
            f"Instance {instance_id[:12]} left running to protect data",
            f"{len(unsaved)} file(s) could not be saved, so termination was "
            f"refused and the GPU is still billing. {summarize(report)}. "
            f"Save them or force-terminate from the instance card.",
            ref=instance_id,
        )

    async def rescue(self, instance_id: str,
                     files: list[dict] | None = None) -> dict:
        """Save an instance's ephemeral files per the data-safety policy.

        Returns a report that names, precisely, what was saved and what was
        not. `unsaved` is the load-bearing field: it is what termination and
        the UI key on, and it is only empty when the data is genuinely safe.

        Never raises: a rescue failure becomes evidence in the report, which
        the caller (terminate) then weighs against the policy. Raising here
        would mean a broken sidecar could wedge every termination.
        """
        prefs = self._data_safety()
        report: dict = {
            "instance_id": instance_id, "attempted": False,
            "files_found": 0, "synced_to": None, "sync_error": "",
            "downloaded": [], "downloaded_bytes": 0, "skipped": [],
            "unsaved": [], "local_dir": None,
        }

        sidecar = self.sidecar_for(instance_id)
        if sidecar is None:
            return report                      # no connection: nothing to ask
        if files is None:
            try:
                files = (await sidecar.unpersisted_files()).get("files", [])
            except SidecarError as exc:
                logger.warning("sidecar check skipped for %s: %s",
                               instance_id, exc)
                return report                  # unreachable: proceed, as before
        report["files_found"] = len(files)
        if not files:
            return report
        report["attempted"] = True

        # 1. The persistent volume. An rsync inside the datacenter is fast and
        #    free, and it copies the WHOLE scratch dir - not just the files the
        #    sidecar flagged - so a success makes everything safe at once.
        if prefs.to_filesystem:
            try:
                synced = await self.sync_ephemeral(instance_id)
                report["synced_to"] = synced["synced_to"]
            except LaunchRejected as exc:
                # No filesystem attached, no connection, or rsync failed. Not
                # fatal: the local download below may still save the data.
                report["sync_error"] = exc.detail
                logger.warning("rescue: sync failed for %s: %s",
                               instance_id, exc.detail)

        # 2. This machine. Costs real transfer time over the SSH connection
        #    while the GPU bills, so it is budgeted and reported honestly.
        if prefs.to_local:
            plan = plan_local_transfer(
                files, scope=prefs.scope,
                max_bytes=int(prefs.max_local_gib * GIB))
            report["local_dir"] = str(
                Path(prefs.local_dir).expanduser() / instance_id)
            for f in plan.download:
                try:
                    written = await self._download_to_local(
                        instance_id, f["path"], prefs.local_dir)
                except Exception as exc:   # noqa: BLE001 - report, never raise
                    logger.warning("rescue: could not download %s from %s: %s",
                                   f["path"], instance_id, exc)
                    plan.skipped.append({**f, "reason": f"download failed: {exc}"})
                    continue
                report["downloaded"].append({**f, "bytes_written": written})
                report["downloaded_bytes"] += written
            report["skipped"] = plan.skipped

        # 3. What is STILL at risk? A successful filesystem sync copied
        #    everything, so nothing is. Otherwise it is whatever did not make
        #    it down to this machine.
        if report["synced_to"]:
            report["unsaved"] = []
        else:
            saved = {d["path"] for d in report["downloaded"]}
            report["unsaved"] = [f for f in files if f["path"] not in saved]

        detail = summarize(report)
        self.db.record_audit("backend", "data_rescue", f"{instance_id}: {detail}")
        if self.notifier is not None and (report["downloaded"] or report["synced_to"]):
            self.notifier.notify(
                "data_transferred",
                f"Saved data from instance {instance_id[:12]}",
                detail + (f" -> {report['local_dir']}"
                          if report["downloaded"] else ""),
                ref=instance_id,
            )
        return report

    async def _download_to_local(self, instance_id: str, rel_path: str,
                                 local_dir: str) -> int:
        """Stream one ephemeral file down to this machine over SFTP.

        Written to a .part file and renamed on completion, so an interrupted
        rescue can never leave a truncated file that LOOKS like a saved one.
        """
        conn = self.connections.get(instance_id)
        if conn is None:
            raise ConnectionError(f"no managed connection to {instance_id}")
        remote = remote_path(rel_path)
        target = local_path(local_dir, instance_id, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        written = 0
        try:
            with partial.open("wb") as fh:
                async for chunk in conn.sftp_read(remote):
                    fh.write(chunk)
                    written += len(chunk)
            partial.replace(target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return written

    async def sync_ephemeral(self, instance_id: str) -> dict:
        """rsync ephemeral scratch to the persistent filesystem, over the
        managed connection. Destination: <mount>/ephemeral-backup/."""
        conn = self.connections.get(instance_id)
        if conn is None:
            raise LaunchRejected(409, f"no managed connection to {instance_id}")
        launch = self.db.find_launch_by_instance(instance_id)
        filesystem = launch["filesystem"] if launch else None
        if not filesystem:
            raise LaunchRejected(
                409, f"no filesystem recorded for {instance_id}; cannot sync"
            )
        dest = f"/lambda/nfs/{filesystem}/ephemeral-backup/"
        # rsync of scratch can legitimately move a lot of data; bound it
        # generously (10 min) rather than at the default command timeout.
        exit_status, stdout, stderr = await conn.run(
            f"mkdir -p {dest} && rsync -a --info=stats1 /workspace/ephemeral/ {dest}",
            timeout=600,
        )
        if exit_status != 0:
            raise LaunchRejected(
                502, f"sync failed (rsync exit {exit_status}): {stderr[:300]}"
            )
        self.db.record_audit("backend", "sync_ephemeral", f"{instance_id} -> {dest}")
        return {"instance_id": instance_id, "synced_to": dest,
                "rsync_stats": stdout.strip()[:500]}

    async def instances_with_state(self) -> list[dict]:
        """Live cloud instances joined with connection state + launch info.

        Also the reconcile point with the cloud's truth: instances that were
        terminated out-of-band (Lambda console, API) are dropped from the
        view, their SSH supervisors reaped, and their launch rows resolved —
        otherwise a ghost card lingers and a supervisor reconnect-loops at
        a dead host forever. See _reconcile_launches for what "resolved"
        may and may not write."""
        listed = []
        listed_providers: set[str] = set()
        unavailable_providers: set[str] = set()
        for name, p in self.providers.items():
            try:
                listed.extend(await p.list_instances())
                listed_providers.add(name)
            except ProviderUnavailable as exc:
                # The provider could not be READ at all. An empty answer from
                # it is not evidence of anything, so it stays OUT of
                # listed_providers and none of its rows are judged this sweep.
                # It goes into unavailable_providers instead, because a
                # provider that cannot list also cannot be RUNNING anything
                # for us: no launch of ours ever reached it. See
                # _may_conclude for why that difference matters.
                # Debug, not warning: a not-yet-wired provider is a known
                # state, and this runs on a poll.
                unavailable_providers.add(name)
                logger.debug("instances view: provider %r unavailable: %s",
                             name, exc)
            except Exception as exc:   # noqa: BLE001 - one bad provider must
                # never blank the whole view. Skip it, keep the others.
                # Deliberately NOT unavailable_providers: this provider works
                # in general and just failed this call, so it may well be
                # running one of our instances. It has to keep blocking
                # conclusions.
                logger.warning(
                    "instances view: skipping provider %r: %s", name, exc)
        gone_statuses = ("terminated", "terminating")
        live = {i.id: i for i in listed if i.status not in gone_statuses}
        live_ids = set(live)
        self._live_ids, self._listed_providers = live_ids, listed_providers

        # Evidence first: stamp every live instance's launch row. When one of
        # them later stops unobserved, last_seen_at is the only honest LOWER
        # bound on how long it billed (see spend.py "unresolved").
        self.db.mark_launches_seen(list(live_ids))

        # Reap connections to instances the cloud no longer runs, so a
        # supervisor stops reconnect-looping at a dead host. Scoped the same
        # way as the row reconcile: an incomplete snapshot must not reap a
        # healthy connection just because its instance is missing from a
        # partial list.
        for instance_id in list(self.connections):
            if instance_id in live_ids:
                continue
            launch = self.db.find_launch_by_instance(instance_id)
            if not self._may_conclude(launch, listed_providers,
                                      unavailable_providers):
                continue
            conn = self.connections.pop(instance_id)
            await conn.close()
            self.host_keys.forget(conn.host)   # IP may be recycled
            if launch is None:
                # An adopted instance has no launch row for the reconcile pass
                # below to close, so its disappearance would otherwise leave no
                # trace at all. Rows are audited there, exactly once.
                self.db.record_audit(
                    "backend", "external_termination_detected",
                    f"{instance_id} is no longer running; connection reaped "
                    f"(adopted instance, so there is no launch history to "
                    f"close)",
                )

        self._reconcile_launches(live, listed_providers)

        result = []
        # User display names overlay Lambda's launch-time names (which
        # cannot be changed after launch).
        aliases = self.db.instance_names()
        for inst in listed:
            if inst.status in gone_statuses:
                continue   # Lambda can report these for a while; not a card
            conn = self.connections.get(inst.id)
            launch = self.db.find_launch_by_instance(inst.id)
            result.append({
                "id": inst.id,
                # Which cloud runs this box. The launch row is the primary
                # source; an ADOPTED instance has no row, so the provider
                # object's own stamp is the fallback, and only then lambda
                # (the sole provider that predates the field). A mixed fleet
                # without this is ambiguous exactly when it matters - the
                # 2026-08-15 GCE gate had to infer it from the id shape.
                "provider": ((launch.get("provider") if launch else None)
                             or getattr(inst, "provider", None) or "lambda"),
                "name": aliases.get(inst.id) or inst.name,
                "status": inst.status,
                "ip": inst.ip,
                "region": inst.region,
                "instance_type": inst.instance_type,
                "gpu_description": inst.gpu_description,
                "hourly_rate_usd": inst.hourly_rate_cents / 100,
                "filesystems": inst.file_system_names,
                "connection_mode": launch["connection_mode"] if launch else None,
                "connection_state": conn.state.value if conn else "disconnected",
                "connection_error": conn.last_error if conn else "",
                "launch_id": launch["id"] if launch else None,
                # WHOSE box, and WHAT FOR (Phase 94). Several agents share
                # one account, and this list was their only view of it: it
                # carried launch_id and nothing that said who was behind it,
                # so an instance another session was mid-way through using
                # read as "unexplained" and got terminated. Both fields are
                # None for an adopted box (no launch row) and for anything
                # launched before this shipped - rendered as unattributed,
                # never guessed.
                "created_by": (launch.get("created_by") if launch else None),
                "purpose": (launch.get("purpose") if launch else None),
            })
        return result

    def last_cloud_snapshot(self) -> tuple[set[str] | None, set[str] | None]:
        """What the most recent instances_with_state() sweep saw: the ids the
        cloud listed as running, and the providers that answered it.

        (None, None) before any sweep has run. Read-only consumers pass these
        straight to spend.launch_cost, which is written for exactly this
        evidence: None means "no snapshot, trust the row", and a provider
        missing from the second set means its rows are not judged at all.
        Copies, so a caller cannot edit the orchestrator's view of the cloud.
        """
        return (None if self._live_ids is None else set(self._live_ids),
                None if self._listed_providers is None
                else set(self._listed_providers))

    def _may_conclude(
        self, launch: dict | None, listed_providers: set[str],
        unavailable_providers: frozenset[str] | set[str] = frozenset(),
    ) -> bool:
        """May this sweep draw conclusions about `launch` from the live list?

        Only if the row's OWN provider answered. A single global "did anything
        fail" flag is not enough: with two providers registered, GCP answering
        while Lambda is unreachable would otherwise write off every Lambda row
        as gone.

        An ADOPTED connection has no launch row, so no known provider, and
        takes the strict rule: nobody may still owe us an answer. Two ways to
        not owe one, and conflating them is a bug we have already shipped:

        - the provider LISTED, so its answer is in hand;
        - the provider is UNAVAILABLE (raised ProviderUnavailable), meaning it
          cannot be read at all. A provider we cannot read is also a provider
          we never launched through, so it owns no instances by construction
          and it has no answer to owe. Treating "unavailable" as "silent" is
          what made filling in GCP_PROJECT_ID silently disable connection
          reaping for everything, leaving supervisors reconnect-looping at
          dead hosts forever.

        Any OTHER failure (a transient outage, a broken client) still blocks:
        that provider works in general, so it might be running the very
        instance we are about to write off.
        """
        if launch is None:
            return all(name in listed_providers or name in unavailable_providers
                       for name, _ in self.providers.items())
        return (launch.get("provider") or "lambda") in listed_providers

    def _reconcile_launches(self, live: dict,
                            listed_providers: set[str]) -> None:
        """Bring launch rows back in line with what the cloud actually runs.

        ONE pass with an explicit state dispatch. Two passes would let a row
        be repaired by the first and then re-examined (and undone) by the
        second inside the same sweep.

        Two rules keep this honest:

        - `terminated_at` means "we SAW it stop", and only an observed stop
          may write it. A stop we merely infer goes to `resolved_at`, which
          bounds the unknown instead of inventing a fact: stamping a guessed
          stop time on a row that boot-timed-out 6 weeks ago would print a
          five-figure "final" cost that never happened.
        - a 'failed' row whose instance is ALIVE is not history, it is a box
          burning money behind a row that reads as over. Repairing it makes
          cost right from that moment on.
        """
        for launch in self.db.list_launches():
            instance_id = launch.get("lambda_instance_id")
            if not instance_id:
                continue
            if not self._may_conclude(launch, listed_providers):
                continue
            status = launch["status"]
            instance = live.get(instance_id)

            if status == "active" and instance is None:
                # OBSERVED: it was running for us, and its own provider now
                # does not list it. terminated_at is legitimate here;
                # resolved_at is stamped alongside it so the cost model can
                # still tell an observed stop from an inferred one.
                self.db.update_launch(
                    launch["id"], status="terminated",
                    terminated_at=utcnow(), resolved_at=utcnow(),
                )
                self._worklog_instance(
                    launch, instance_id,
                    "terminated outside Manifold (vanished from the "
                    "provider's list)")
                self.db.delete_endpoints_for_instance(instance_id)
                self.db.record_audit(
                    "backend", "external_termination_detected",
                    f"launch {launch['id']}: instance {instance_id} is no "
                    f"longer running; connection reaped and history closed",
                )
            elif (status == "failed" and instance is not None
                    and instance.status == "active"):
                # ORPHAN: the launch gave up (usually a boot timeout) but the
                # instance came up anyway and is billing. Repair the status
                # only from a genuinely ACTIVE instance — repairing a
                # booting/unhealthy one re-enters the boot pipeline with no
                # waiter behind it. Deliberately NOT stamping active_at: we
                # never saw this boot finish, and inventing the timestamp
                # would fabricate the very boot_seconds figure we publish to
                # show how much of the bill is boot.
                self.db.update_launch(launch["id"], status="active")
                self.db.record_audit(
                    "backend", "orphan_repaired",
                    f"launch {launch['id']}: instance {instance_id} is alive "
                    f"despite a failed launch row; status repaired to active "
                    f"so its cost is counted",
                )
            elif (status == "failed" and instance is None
                    and launch.get("launched_at")
                    and not launch.get("resolved_at")):
                # It started, and it is gone, and nobody watched it stop.
                # Record WHEN WE FOUND OUT, in its own column: that freezes
                # the upper bound of the cost range instead of letting it
                # grow with the clock forever.
                self.db.update_launch(launch["id"], resolved_at=utcnow())
                self.db.record_audit(
                    "backend", "launch_cost_bounded",
                    f"launch {launch['id']}: instance {instance_id} is gone "
                    f"and its stop was never observed; cost bounded as a "
                    f"range rather than guessed",
                )

    async def shutdown(self) -> None:
        """Close background tasks and connections (instances keep running)."""
        for task in self._launch_tasks.values():
            task.cancel()
        for conn in list(self.connections.values()):
            await conn.close()
        self.connections.clear()

    # -- launch pipeline ---------------------------------------------------------

    def _validate_mode(self, mode: str) -> None:
        if mode not in CONNECTION_MODES:
            raise LaunchRejected(
                400,
                f"Unknown connection mode '{mode}'. "
                f"Valid modes: {', '.join(CONNECTION_MODES)}",
                reason_code="mode",
            )
        if mode == "tailscale" and not self.settings.tailscale_authkey:
            raise LaunchRejected(
                400,
                "Connection mode 'tailscale' is unavailable: TAILSCALE_AUTHKEY "
                "is not set in .env. Add one or use direct-ssh.",
                reason_code="mode",
            )

    async def _run_launch(self, plan: LaunchPlan) -> None:
        try:
            launched = await self._launch_with_retry(plan)
            if launched is None:
                return  # already marked failed
            instance_id, launched_type = launched
            instance = await self._wait_until_active(plan, instance_id)
            if instance is None:
                return
            await self._establish_connection(plan, instance)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("launch %s crashed", plan.launch_id)
            self.db.update_launch(
                plan.launch_id, status="failed",
                error=f"internal error: {exc}",
            )

    async def _launch_with_retry(self, plan: LaunchPlan) -> tuple[str, str] | None:
        """Try candidates with backoff. Returns (instance_id, type) or None."""
        policy = self.settings.launch
        attempts = 0
        for round_no in range(policy.max_attempts):
            for candidate in plan.types_to_try:
                # Terminated while we were backing off? terminate_cluster
                # closes pending rows (and cancels tasks, but a cancel can
                # lose the race); creating an instance now would bill for a
                # box whose row already says terminated. Abort cleanly.
                launch_rec = self.db.get_launch(plan.launch_id)
                if launch_rec and launch_rec["status"] == "terminated":
                    return None
                attempts += 1
                self.db.update_launch(plan.launch_id, attempts=attempts)
                try:
                    p_name = launch_rec['provider'] if launch_rec else 'lambda'
                    instance_id = await self.providers.get_provider(p_name).launch_instance(
                        instance_type=candidate,
                        region=plan.region,
                        ssh_key_names=[plan.ssh_key_name],
                        # Primary first, then the extras (Phase 103). The
                        # provider API has always taken a list; validation
                        # guarantees extras only exist with a primary, so a
                        # scratch launch still sends [].
                        filesystem_names=(
                            [plan.filesystem, *plan.extra_filesystems]
                            if plan.filesystem else []),
                        name=plan.name,
                        user_data=build_user_data(
                            # Only a tailscale-mode launch carries the key.
                            tailscale_authkey=(
                                self.settings.tailscale_authkey
                                if plan.connection_mode == "tailscale" else ""
                            ),
                            hostname=plan.name,
                        ),
                    )
                except ProviderCapacityError as err:
                    # GCE "resource pool exhausted": same transient shape as
                    # Lambda's insufficient-capacity, same treatment.
                    logger.info(
                        "launch %s: provider capacity for %s (attempt %d)",
                        plan.launch_id, candidate, attempts)
                    self.db.update_launch(
                        plan.launch_id, status="retrying",
                        error=f"attempt {attempts}: {err}")
                    continue
                except ProviderError as err:
                    # Not transient (quota, bad config): the message already
                    # carries the fix, so retrying would only obscure it.
                    self.db.update_launch(
                        plan.launch_id, status="failed", error=str(err))
                    return None
                except LambdaAPIError as err:
                    if err.is_capacity_error:
                        logger.info(
                            "launch %s: no capacity for %s (attempt %d)",
                            plan.launch_id, candidate, attempts,
                        )
                        self.db.update_launch(
                            plan.launch_id, status="retrying",
                            error=f"attempt {attempts}: no capacity for "
                                  f"{candidate} in {plan.region}",
                        )
                        continue
                    # Non-capacity errors are not retryable: fail loudly.
                    self.db.update_launch(
                        plan.launch_id, status="failed",
                        error=f"{err.code}: {err.message}",
                    )
                    return None
                self.db.update_launch(
                    plan.launch_id,
                    status="booting",
                    lambda_instance_id=instance_id,
                    launched_type=candidate,
                    hourly_rate_cents=plan.prices[candidate],
                    launched_at=utcnow(),
                    error=None,
                )
                return instance_id, candidate
            if round_no < policy.max_attempts - 1:
                delay = backoff_delay(
                    round_no, policy.backoff_base_seconds, policy.backoff_max_seconds
                )
                await asyncio.sleep(delay)
        hint = await self._capacity_hint(plan)
        self.db.update_launch(
            plan.launch_id, status="failed",
            error=f"No capacity after {attempts} attempts across "
                  f"{', '.join(plan.types_to_try)} in {plan.region}. "
                  + (hint or "Try again later, launch in another region, or "
                             "add fallback types in config.yaml."),
        )
        return None

    async def _capacity_hint(self, plan: "LaunchPlan") -> str:
        """Best-effort: name where the requested types DO have capacity right
        now, so the failure is actionable instead of a dead end. Never masks
        the real failure — any catalog error just yields no hint."""
        try:
            cloud_provider = self.providers.get_provider(plan.provider)
            types = {t.name: t for t in await cloud_provider.list_instance_types()}
        except Exception:
            return ""
        elsewhere: list[str] = []
        for name in plan.types_to_try:
            info = types.get(name)
            regions = [r for r in (info.regions_with_capacity if info else [])
                       if r != plan.region]
            if regions:
                elsewhere.append(f"{name} in {', '.join(sorted(regions))}")
        if not elsewhere:
            return ("None of those types have capacity in any region right "
                    "now; try again later.")
        return ("Available right now: " + "; ".join(elsewhere)
                + ". Relaunch there (a persistent filesystem must be in the "
                  "same region), or call list-launch-options for co-located "
                  "targets.")

    async def _wait_until_active(
        self, plan: LaunchPlan, instance_id: str
    ) -> InstanceInfo | None:
        policy = self.settings.launch
        waited = 0.0
        while True:
            launch_rec = self.db.get_launch(plan.launch_id)
            p_name = launch_rec['provider'] if launch_rec else 'lambda'
            instance = await self.providers.get_provider(p_name).get_instance(instance_id)
            if instance is None:
                # OBSERVED: the provider we just launched through no longer
                # has this instance. That is the best evidence in the system —
                # we were watching — so the stop is recorded as a fact rather
                # than left to the reconcile sweep to bound as a guess. The
                # LAUNCH still failed, so status and error are unchanged.
                self.db.update_launch(
                    plan.launch_id, status="failed",
                    error=f"instance {instance_id} no longer exists",
                    terminated_at=utcnow(), resolved_at=utcnow(),
                )
                return None
            if instance.status == "active" and instance.ip:
                return instance
            if instance.status in BOOT_ABORT_STATUSES:
                fields: dict = {
                    "status": "failed",
                    "error": f"instance entered status '{instance.status}' "
                             f"while booting",
                }
                if instance.status in OBSERVED_STOP_STATUSES:
                    # Same observed stop as above: we watched it reach a
                    # status that means it is gone. An 'unhealthy' box gets
                    # no timestamps — it is still running, and still billing.
                    fields["terminated_at"] = fields["resolved_at"] = utcnow()
                self.db.update_launch(plan.launch_id, **fields)
                return None
            if waited >= policy.boot_timeout_seconds:
                self.db.update_launch(
                    plan.launch_id, status="failed",
                    error=f"instance {instance_id} did not become active within "
                          f"{policy.boot_timeout_seconds:.0f}s; it may still be "
                          f"running and billing; check the dashboard and terminate "
                          f"it if unwanted.",
                )
                return None
            await asyncio.sleep(policy.boot_poll_seconds)
            waited += policy.boot_poll_seconds

    async def _establish_connection(
        self, plan: LaunchPlan, instance: InstanceInfo
    ) -> None:
        """Open the managed connection, then hand the launch to the gate.

        This used to stamp active right here, one line after conn.start() -
        which is asynchronous and returns while the supervisor is still
        CONNECTING. So a launch reported "SSH is up; the instance is ready"
        before any SSH session existed, and on GCE it said that roughly six
        minutes before the box could run anything. Worse, active_at is the
        anchor of the max_active budget, whose contract explicitly excludes
        boot: ~500s of a 1800s budget was being spent booting.

        Nothing is stamped here now. The row stays 'booting' and the gate
        (sweep_provisioning) promotes it, from the row's own state, so a
        backend restart in the middle costs nothing.
        """
        self._open_connection(plan.connection_mode, instance)
        await self._await_provisioning(plan.launch_id, instance.id)

    async def _await_provisioning(self, launch_id: str,
                                  instance_id: str) -> None:
        """Drive the gate for one launch until it settles.

        The gate is a reconciler and the dispatcher calls the same function
        on its telemetry tick; this loop is only the fast path, so a launch
        does not sit in 'booting' for up to one telemetry interval after its
        box is ready. Everything it decides, it decides in sweep_provisioning
        and writes to the row - lose this task to a crash or a restart and
        the sweep picks the launch up exactly where it was.

        The one thing it owns alone: a box whose SSH NEVER answers has no
        connected_at, so the gate's budget never starts. Bounding that wait
        here (same budget, wall clock from when we opened the connection)
        keeps such a launch from hanging in 'booting' forever while it bills.
        """
        policy = self.settings.launch
        loop = asyncio.get_event_loop()
        started = loop.time()
        while True:
            launch = self.db.get_launch(launch_id)
            if launch is None or launch["status"] != "booting":
                return          # promoted, failed, or terminated by someone
            conn = self.connections.get(instance_id)
            if conn is None:
                return          # terminated: its connection was popped
            await self.sweep_provisioning(instance_id, conn)
            if (not (self.db.get_launch(launch_id) or {}).get("connected_at")
                    and loop.time() - started
                    >= policy.provisioning_timeout_seconds):
                self._fail_provisioning(
                    launch_id,
                    f"instance {instance_id} never accepted an SSH connection "
                    f"within {policy.provisioning_timeout_seconds:.0f}s of "
                    f"becoming reachable",
                )
                return
            await asyncio.sleep(policy.boot_poll_seconds)

    async def sweep_provisioning(self, instance_id: str, conn) -> None:
        """One asker at a time per instance; the work is in _sweep_provisioning.

        Skipping rather than queueing is right for a reconciler: the other
        caller is asking the same questions of the same box right now, and
        whatever it decides is written to the row. A skipped tick loses
        nothing - the next one asks again.
        """
        if instance_id in self._provisioning_sweeps:
            return
        self._provisioning_sweeps.add(instance_id)
        try:
            await self._sweep_provisioning(instance_id, conn)
        finally:
            self._provisioning_sweeps.discard(instance_id)

    async def _sweep_provisioning(self, instance_id: str, conn) -> None:
        """Promote one still-booting launch once its box is PROVISIONED.

        A RECONCILER, in the shape _sweep_bootstrap already uses: it asks
        questions of the row and the box on every tick and acts when they
        all hold, rather than firing at a transition. Three code paths open
        a connection (the launch pipeline, resume-after-downtime, adoption)
        and at every one of them the supervisor is still CONNECTING, so
        there is no moment at which "it is ready" could be decided.

        The launch stays in 'booting' throughout - no new status. The row IS
        the state, so a backend restart resumes the gate for free, the boot
        countdown and the dashboard's booting card keep working, and no
        client learns a new word.

        In order:
          1. stamp connected_at the first time SSH is up (the anchor for the
             provisioning budget, and the only new fact this writes early);
          2. ask for the init signal;
          3. present -> check this session can reach docker, redial if it
             cannot, then promote to active;
          4. absent but cloud-init says it finished -> promote with the
             failure recorded: waiting forever on a billing box is worse
             than an active box carrying a warning;
          5. absent and the budget is spent -> fail the row, and never
             terminate. A box whose sidecar never came up is a box a rescue
             cannot save anything from, so destroying it here would destroy
             data to tidy up a status field.
        """
        launch = self.db.find_launch_by_instance(instance_id)
        if launch is None or launch.get("status") != "booting":
            return              # adopted boxes have no row and stop here
        if conn.state != ConnectionState.CONNECTED:
            return              # not an error: the next tick asks again
        launch_id = launch["id"]
        connected_at = launch.get("connected_at")
        if not connected_at:
            connected_at = utcnow()
            self.db.update_launch(launch_id, connected_at=connected_at)
        signal = await self.provisioning_signal(conn)
        if signal in ("pending", "unknown"):
            waited = _interval_seconds(connected_at, utcnow()) or 0.0
            budget = self.settings.launch.provisioning_timeout_seconds
            if waited >= budget:
                self._fail_provisioning(
                    launch_id,
                    f"instance {instance_id} answered SSH but did not finish "
                    f"provisioning within {budget:.0f}s (the driver, Docker "
                    f"and the sidecar are installed by cloud-init after "
                    f"first boot; see /var/log/manifold-init.log on the box)",
                )
            return
        # Only on failure: a redial destroys every channel on the session,
        # and an unconditional cycle would kill a terminal somebody opened.
        if await self._docker_access_ok(conn) is False:
            conn.redial()
            self.db.record_audit(
                "backend", "connection_redialled",
                f"{instance_id}: this SSH session could not use the docker "
                f"socket (it was opened before cloud-init added the group), "
                f"so it was dropped for a fresh one")
        # Re-read before writing: a terminate that landed while we were
        # asking must not be overwritten back to active.
        current = self.db.get_launch(launch_id)
        if current is None or current["status"] != "booting":
            return
        fields: dict = {"status": "active", "active_at": utcnow()}
        if signal == "incomplete":
            fields["error"] = (
                "cloud-init finished but Manifold's setup did not complete: "
                "the driver, Docker, the NVIDIA runtime or the sidecar may "
                "be missing. Jobs may fail; /var/log/manifold-init.log on "
                "the instance says which step it was.")
            self.db.record_audit(
                "backend", "provisioning_incomplete",
                f"launch {launch_id}: instance {instance_id} promoted to "
                f"active with cloud-init finished and Manifold's init marker "
                f"absent - the box is up, its setup is not proven")
        self.db.update_launch(launch_id, **fields)

    def _fail_provisioning(self, launch_id: str, detail: str) -> None:
        """Close a launch that never became usable, WITHOUT touching the box.

        Same honest copy as the boot timeout: the instance is probably still
        running and still billing, and only a human can decide whether that
        is worth keeping. If it is alive, the reconcile sweep's orphan repair
        will flip this row back to active so its cost keeps being counted -
        which is exactly why the bootstrap sweep checks the init signal
        itself rather than trusting a row that says active.
        """
        launch = self.db.get_launch(launch_id)
        if launch is None or launch["status"] != "booting":
            return
        self.db.update_launch(
            launch_id, status="failed",
            error=f"{detail}; it may still be running and billing; check the "
                  f"dashboard and terminate it if unwanted.",
        )
        self.db.record_audit(
            "backend", "provisioning_timeout",
            f"launch {launch_id}: {detail}; the instance was NOT terminated")

    async def provisioning_signal(self, conn) -> str:
        """What the box says about its own setup, in one word.

        "ready"      Manifold's init marker is there: cloud-init ran our
                     script to the last line, so everything it installs is
                     installed.
        "incomplete" cloud-init has finished but the marker is absent: the
                     script stopped somewhere. The box is as done as it will
                     ever be, and waiting longer only costs money.
        "pending"    neither: still working. Ask again next tick.
        "unknown"    the connection went away mid-question. Kept separate
                     from "pending" even though both callers wait: "we could
                     not ask" and "we asked and the box said no" are
                     different answers, and a caller that wanted to treat
                     them differently should not have to guess which it got.
        """
        try:
            code, _out, _err = await conn.run(INIT_MARKER_PROBE, timeout=30.0)
            if code == 0:
                return "ready"
            code, _out, _err = await conn.run(
                CLOUD_INIT_DONE_PROBE, timeout=30.0)
            return "incomplete" if code == 0 else "pending"
        except (ConnectionError, OSError):
            return "unknown"

    async def _docker_access_ok(self, conn) -> bool | None:
        """Can this SSH SESSION use the docker socket? None = could not ask.

        None is not False: a redial is a real cost (it drops every channel on
        the connection), and "we could not tell" is never grounds for paying
        it.
        """
        try:
            code, _out, _err = await conn.run(DOCKER_ACCESS_PROBE, timeout=30.0)
        except (ConnectionError, OSError):
            return None
        return code == 0

    def _open_connection(self, connection_mode: str, instance: InstanceInfo) -> None:
        """Build and start a ManagedConnection for a live instance. Shared by
        the launch pipeline and reconnect-on-startup."""
        manager = self.managers[connection_mode]
        host = manager.dial_target(instance)   # the only mode-specific step
        conn = ManagedConnection(
            host,
            self.settings.ssh,
            connect_fn=self._connect_fn(host) if self._connect_fn else None,
            host_keys=self.host_keys,
        )
        conn.start()
        self.connections[instance.id] = conn

    async def adopt_running_instances(self, *, startup: bool = True) -> int:
        """Establish managed connections to instances running on Lambda but
        not tracked in memory. Called at startup so a backend restart
        re-attaches to live instances, and periodically by the dispatcher's
        adoption sweep so an instance launched outside Manifold (Lambda
        console, raw API script) gets Files/chat/jobs without a restart.

        Best-effort: an unconfigured/unreachable Lambda client, or a single
        instance we can't classify, must not stop the backend from starting.
        Returns the number of instances adopted.
        """
        # Each provider is swept in its own try/except so one that is
        # unconfigured, unreachable, or not-yet-implemented is skipped while
        # the others (Lambda) still adopt. A broken provider must never
        # silently disable adoption for the working ones.
        instances = []
        for name, p in self.providers.items():
            try:
                instances.extend(await p.list_instances())
            except LambdaAPIError as exc:
                # info at startup, debug on the 30s sweep so an unconfigured
                # API key does not fill the log with the same line forever.
                log = logger.info if startup else logger.debug
                log("skip %s adoption: %s", name, exc.message)
            except ProviderUnavailable as exc:
                # A provider that cannot be read is a known state, not a
                # crash: debug, and never a stack trace on the sweep.
                logger.debug("skip %s adoption: %s", name, exc)
            except Exception:
                if startup:
                    logger.exception(
                        "skip %s adoption: could not list instances", name)
                else:
                    logger.debug(
                        "skip %s adoption sweep: could not list instances",
                        name, exc_info=True)

        adopted = 0
        for inst in instances:
            if inst.status != "active" or not inst.ip:
                continue
            if inst.id in self.connections:
                continue
            launch = self.db.find_launch_by_instance(inst.id)
            # Fall back to the default mode for instances launched outside
            # Manifold (or before launch history existed).
            mode = (launch or {}).get("connection_mode") \
                or self.settings.default_connection_mode
            if mode not in self.managers:
                logger.warning(
                    "cannot reconnect to %s: unknown connection mode %r",
                    inst.id, mode,
                )
                continue
            try:
                self._open_connection(mode, inst)
                adopted += 1
                logger.info("reconnecting to running instance %s (%s)",
                            inst.id, inst.name)
            except Exception:
                logger.exception("failed to reconnect to instance %s", inst.id)
        if adopted:
            if startup:
                self.db.record_audit(
                    "backend", "reconnect_on_startup",
                    f"re-established connections to {adopted} "
                    f"running instance(s)",
                )
            else:
                self.db.record_audit(
                    "backend", "instance_adopted",
                    f"connected to {adopted} running instance(s) "
                    f"launched outside Manifold",
                )
        return adopted

    async def resume_pending_launches(self) -> int:
        """Pick back up launches left mid-boot by a backend restart (--reload).

        A launch in 'booting' has a real instance on Lambda, but the in-memory
        boot-waiter died with the old process. Without this, the instance boots
        to 'active' and nothing dials SSH or closes out the launch: it hangs in
        'booting' forever while it bills. We re-attach - instances that adopt
        already reconnected go straight to the provisioning gate; ones still
        booting get a fresh wait-then-connect task (a fresh timeout window, so
        a restart never shortens a genuine boot). Best-effort; never blocks
        startup. Call it AFTER adopt_running_instances so already-active
        launches are settled, not re-dialed.
        """
        resumed = 0
        for launch in self.db.list_launches():
            if (launch["status"] in ("launching", "retrying")
                    and launch["id"] not in self._launch_tasks
                    and not launch.get("lambda_instance_id")):
                # Its launch task died with the old process before an
                # instance existed. Close the row: nothing will ever finish
                # it, and the spend guard counts open pending rows, so a
                # stale one would wrongly eat a concurrency slot forever.
                self.db.update_launch(
                    launch["id"], status="failed",
                    error="launch was interrupted by a backend restart "
                          "before an instance was created; relaunch it",
                )
                continue
            if launch["status"] != "booting":
                continue
            if launch["id"] in self._launch_tasks:
                continue  # its waiter is already running in this process
            instance_id = launch.get("lambda_instance_id")
            if not instance_id:
                # 'booting' with no instance id shouldn't happen in normal
                # flow; fail it rather than leave a zombie that never settles.
                self.db.update_launch(
                    launch["id"], status="failed",
                    error="launch was interrupted before an instance id was "
                          "recorded; nothing to resume",
                )
                continue
            if instance_id in self.connections:
                # adopt_running_instances already reconnected this one, so the
                # boot finished during the downtime. It does NOT follow that
                # the machine is provisioned: on GCE sshd answers minutes
                # before cloud-init is done, so closing the record here used
                # to be the one path that bypassed the gate entirely. Hand it
                # to the gate instead - which, finding a box that IS ready,
                # closes the record on its first pass anyway.
                self._launch_tasks[launch["id"]] = asyncio.create_task(
                    self._await_provisioning(launch["id"], instance_id))
                resumed += 1
                continue
            plan = self._plan_from_launch(launch)
            if plan is None:
                continue
            self._launch_tasks[launch["id"]] = asyncio.create_task(
                self._resume_launch(plan, instance_id))
            resumed += 1
        if resumed:
            self.db.record_audit(
                "backend", "resume_pending_launches",
                f"resumed {resumed} launch(es) left mid-boot by a restart",
            )
        return resumed

    def _plan_from_launch(self, launch: dict) -> LaunchPlan | None:
        """Reconstruct the minimal LaunchPlan needed to resume a boot wait.

        The instance is already created, so the launch-time fields (ssh key,
        fallback candidates, prices) no longer matter; only launch_id and
        connection_mode drive the wait-then-connect tail.
        """
        mode = launch.get("connection_mode") or self.settings.default_connection_mode
        if mode not in self.managers:
            logger.warning("cannot resume launch %s: unknown connection mode %r",
                           launch["id"], mode)
            return None
        launched_type = (launch.get("launched_type")
                         or launch.get("requested_type") or "")
        return LaunchPlan(
            launch_id=launch["id"],
            region=launch.get("region") or "",
            filesystem=launch.get("filesystem") or "",
            connection_mode=mode,
            ssh_key_name="",   # already used at launch; unused by the tail
            types_to_try=[launched_type] if launched_type else [],
            prices={},
            name=launch.get("requested_type") or launch["id"],
        )

    async def _resume_launch(self, plan: LaunchPlan, instance_id: str) -> None:
        """The tail of _run_launch (wait for active, then connect), run on its
        own after a restart to finish an interrupted boot."""
        try:
            instance = await self._wait_until_active(plan, instance_id)
            if instance is None:
                return
            await self._establish_connection(plan, instance)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("resume of launch %s crashed", plan.launch_id)
            self.db.update_launch(
                plan.launch_id, status="failed",
                error=f"internal error during resume: {exc}",
            )

    # -- test/introspection helpers ----------------------------------------------

    async def wait_for_launch(self, launch_id: str, timeout: float = 10.0) -> dict:
        """Wait until a launch reaches a settled state (active/failed).

        Used by tests and available to callers that want synchronous behavior.
        """
        task = self._launch_tasks.get(launch_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        return self.db.get_launch(launch_id)

    async def wait_until_settled(
        self, launch_id: str, timeout: float, poll: float = 2.0
    ) -> dict | None:
        """Block up to `timeout` seconds until a launch settles (active,
        failed, or terminated), then return its record; return the current
        record if the window elapses first, or None if the id is unknown.

        Polls the DB rather than an in-memory task, so it also serves launches
        resumed after a restart. This is the server-side long-poll behind the
        MCP wait tool: one blocking call replaces dozens of get_launch_status
        round-trips (and the tokens they burn) while a slow SXM4 instance boots.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            launch = self.db.get_launch(launch_id)
            if launch is None:
                return None
            if launch["status"] in SETTLED_LAUNCH_STATUSES:
                return launch
            remaining = deadline - loop.time()
            if remaining <= 0:
                return launch
            await asyncio.sleep(min(poll, remaining))

    def connection_state(self, instance_id: str) -> ConnectionState | None:
        conn = self.connections.get(instance_id)
        return conn.state if conn else None
