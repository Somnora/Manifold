"""Dispatcher: pushes queued tasks to a connected instance over SSH.

Flow per task:
1. Wait until a queued task AND a connected instance exist (poll loop).
2. Resolve the template; validate + coerce the stored parameter values.
3. Render the docker invocation: substitute {{parameters}} in the command,
   replace {persistent} in mounts with /lambda/nfs/<filesystem>, publish
   declared ports on 127.0.0.1 only, add --gpus all.
4. Run it over the managed SSH connection, streaming stdout/stderr lines
   into task_logs as they arrive (visible live via GET /tasks/{id}/logs).
   A persistent copy is written on the instance too (docker logs also
   retains them until the container is pruned).
5. Record exit code + output paths (the template's persistent mounts).

Idle auto-termination lives here as a second loop: if no task is running
and no terminal has been active for idle.timeout_seconds, request the
STANDARD termination flow — the Phase 3 safety hook still applies.

The capacity watcher is a third loop: polls the instance-type catalog and
flips watches to "available" (or auto-launches through the guarded path
when enabled and configured on the watch).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
from datetime import datetime, timezone

from .auth import current_principal
from .config import Settings
from .connections import ConnectionState, ManagedConnection
from .db import Database, utcnow
from .image_checker import ImageChecker
from .lambda_api import LambdaClient
from .model_client import ModelClientError
from .orchestrator import LaunchRejected, Orchestrator, TerminationBlocked
from .subagent_engine import engine as subagent_engine
from .task_queue import TaskQueue
from .templates import JobTemplate, PERSISTENT_TOKEN

logger = logging.getLogger("manifold.dispatcher")


class ParameterError(Exception):
    """User-supplied task parameters don't satisfy the template schema."""


# GPU-readiness probe, run on the instance before its FIRST job. `nvidia-smi
# -q` is the one host-side signal that exposes the A100-SXM trap: the fabric
# manager still initializing, during which nvidia-smi looks healthy but any
# CUDA init inside a container fails with "No CUDA GPUs are available".
# Host CUDA readiness (fabric manager) AND container-runtime readiness in one
# probe: the field pass showed a second race where host nvidia-smi is fine
# but the NVIDIA container toolkit isn't serving GPUs yet, so a job dies with
# "No CUDA GPUs are available" despite --gpus all. nvidia-container-cli talks
# to the same library docker's --gpus path uses; probed only when installed
# so a box without the toolkit stays fail-open.
#
# THE LAST STAGE WALKS THROUGH THE JOB'S OWN DOOR. The 2026-08-14 mini-gate
# proved the two host-side checks are not sufficient: a job dispatched
# seconds after connect died with "DP adjusted local rank 0 is out of bounds
# for 0 devices" while both host probes read healthy. The only probe that
# cannot be fooled is the exact path the job takes - `docker run --gpus all`
# - so the final stage runs a real (tiny) container and asks the GPU to show
# itself from inside. The NVIDIA runtime injects nvidia-smi into any image,
# so plain ubuntu works; the image is fetched once and cached, and boxes
# without docker skip the stage (fail-open, same rule as the toolkit stage).
GPU_PROBE_COMMAND = (
    "nvidia-smi -q && "
    "{ ! command -v nvidia-container-cli >/dev/null || nvidia-container-cli info; } && "
    "{ ! command -v docker >/dev/null || "
    "docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L; }"
)

# Container stderr signatures of that same race, for the last-resort retry.
CUDA_RACE_SIGNATURES = (
    "No CUDA GPUs are available",
    "could not select device driver",
    "nvidia-container-cli: initialization error",
    # torch's DataParallel wording for the same race - the exact line the
    # 2026-08-14 mini-gate died with while every signature above missed it.
    "out of bounds for 0 devices",
)

# Fabric states that mean CUDA is (or will trivially be) initializable.
# Anything else - "In Progress" above all - means wait.
_FABRIC_READY_STATES = ("completed", "n/a", "not supported", "none", "")


def gpu_readiness(exit_code: int, output: str) -> tuple[bool, str]:
    """Interpret a GPU_PROBE_COMMAND run: (ready, human reason).

    Pure, so the parsing is testable against captured nvidia-smi output.
    Three cases:
    - probe failed: driver isn't up yet (or nvidia-smi missing) -> not ready
    - a Fabric section reports a non-settled State -> not ready (SXM boxes)
    - no Fabric section (PCIe boxes) or a settled state -> ready
    """
    if exit_code != 0:
        # Any stage can be the one that failed (driver, toolkit, or the
        # container-level check), so the reason names the probe, not one
        # stage - a message blaming nvidia-smi when docker was the failure
        # sends the reader to the wrong log.
        return False, ("GPU not servable yet (driver, container toolkit, or "
                       "docker --gpus still coming up)")
    in_fabric = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Fabric"):
            in_fabric = True
            continue
        if in_fabric and stripped.startswith("State"):
            state = stripped.split(":", 1)[-1].strip().lower()
            if state in _FABRIC_READY_STATES:
                return True, f"fabric state: {state or 'settled'}"
            return False, f"fabric manager still initializing (state: {state})"
    return True, "GPU driver up (no fabric manager on this box)"


# A flag's value: version tags, model ids, dtypes, decimals, paths. NO
# quotes, NO whitespace, NO shell metacharacters - not because a shell is
# in the path (the serve bootstrap builds argv in python and execvp's it;
# there is no shell), but because a value that needs anything outside this
# set is not a tuning value.
_ARG_VALUE_RE = re.compile(r"^[A-Za-z0-9._:/=-]+$")


def _validate_arg_string(param_name: str, raw: str,
                         allowlist: tuple[str, ...]) -> str | None:
    """One problem string, or None if `raw` is an acceptable flag string.

    Grammar: tokens are `--flag`, `--flag=value`, or a bare value directly
    after an allowlisted flag. Every flag must be NAMED in the allowlist -
    the allowlist matters more than the passthrough (--max-num-seqs is a
    tuning knob, --trust-remote-code is supply-chain surface), and the
    refusal carries the full list so the wall is discoverable without
    hitting it twice.
    """
    tokens = str(raw).split()
    expecting_value = False
    for tok in tokens:
        if tok.startswith("-"):
            flag = tok.split("=", 1)[0]
            if flag not in allowlist:
                return (f"parameter '{param_name}': flag '{flag}' is not in "
                        f"the allowlist ({', '.join(allowlist)})")
            value = tok.split("=", 1)[1] if "=" in tok else None
            if value is not None and not _ARG_VALUE_RE.match(value):
                return (f"parameter '{param_name}': value {value!r} for "
                        f"{flag} contains characters outside "
                        f"[A-Za-z0-9._:/=-]")
            expecting_value = value is None
        else:
            if not expecting_value:
                return (f"parameter '{param_name}': bare value {tok!r} "
                        f"follows no flag")
            if not _ARG_VALUE_RE.match(tok):
                return (f"parameter '{param_name}': value {tok!r} contains "
                        f"characters outside [A-Za-z0-9._:/=-]")
            expecting_value = False
    return None


def coerce_parameters(template: JobTemplate, values: dict) -> dict:
    """Validate user values against the template schema; apply defaults.

    Returns the complete parameter dict. Raises ParameterError with a
    message naming every problem at once (nicer than one-at-a-time).
    """
    problems = []
    declared = {p.name for p in template.parameters}
    for extra in sorted(set(values) - declared):
        problems.append(f"unknown parameter '{extra}'")

    result: dict = {}
    for p in template.parameters:
        if p.name in values:
            raw = values[p.name]
            try:
                if p.type == "integer":
                    result[p.name] = int(raw)
                elif p.type == "number":
                    result[p.name] = float(raw)
                elif p.type == "boolean":
                    if isinstance(raw, bool):
                        result[p.name] = raw
                    elif str(raw).lower() in ("true", "1", "yes"):
                        result[p.name] = True
                    elif str(raw).lower() in ("false", "0", "no"):
                        result[p.name] = False
                    else:
                        raise ValueError(raw)
                else:
                    result[p.name] = str(raw)
                    if p.arg_allowlist and result[p.name].strip():
                        problem = _validate_arg_string(
                            p.name, result[p.name], p.arg_allowlist)
                        if problem:
                            problems.append(problem)
            except (TypeError, ValueError):
                problems.append(
                    f"parameter '{p.name}' must be {p.type}, got {raw!r}"
                )
        elif p.required:
            problems.append(f"missing required parameter '{p.name}'")
        else:
            result[p.name] = p.default
    if problems:
        raise ParameterError("; ".join(problems))
    return result


def render_docker_command(
    template: JobTemplate, parameters: dict, *, filesystem: str, task_id: str
) -> str:
    """Build the docker run invocation for a task.

    Every substituted value is shell-quoted. Ports are ALWAYS published on
    127.0.0.1 — a template cannot open a public listener no matter what it
    declares (see CLAUDE.md hard rules).
    """
    persistent_root = f"/lambda/nfs/{filesystem}"

    command = template.command
    for name, value in parameters.items():
        command = command.replace("{{" + name + "}}", shlex.quote(str(value)))

    parts = [
        "docker run --rm",
        f"--name manifold-task-{task_id}",
        "--gpus all",
    ]
    if template.user:
        # Restores docker's root default for images that drop privileges
        # (validated to "root" at template load; see templates.py).
        parts.append("--user 0:0")
    if template.entrypoint:
        # Overrides an image's opinionated ENTRYPOINT, which would
        # otherwise consume the command below as its own arguments.
        parts.append(f"--entrypoint {shlex.quote(template.entrypoint)}")
    if template.network == "host":
        # Loopback-consumer jobs (llm-synthesize) dial servers other jobs
        # publish on the host's 127.0.0.1. Mutually exclusive with ports
        # (enforced at template load).
        parts.append("--network host")
    for volume in template.volumes:
        host = volume.host.replace(PERSISTENT_TOKEN, persistent_root)
        # Parameters may appear inside mount paths too (e.g. input_dir).
        for name, value in parameters.items():
            host = host.replace("{{" + name + "}}", str(value))
        suffix = ":ro" if volume.read_only else ""
        parts.append(f"-v {shlex.quote(host)}:{shlex.quote(volume.container)}{suffix}")
    for port in template.ports:
        parts.append(f"-p 127.0.0.1:{port.host}:{port.container}")
    for key, value in template.env.items():
        parts.append(f"-e {shlex.quote(f'{key}={value}')}")
    parts.append(template.image)
    parts.append(command)
    return " ".join(parts)


def wrap_remote_command(docker_cmd: str, remote_log: str, *,
                        ensure_dirs: list[str]) -> str:
    """Wrap a docker command for remote dispatch: create the dirs it needs,
    tee all output to a persistent log, and — critically — propagate the
    CONTAINER's exit code through the pipe.

    Without `set -o pipefail` a pipeline's exit code is the LAST command's
    (tee, which always exits 0), so every job would report "succeeded" no
    matter what the container did. Found on real hardware at the Phase 15
    gate: two crashed vllm-serve jobs showed green.
    """
    mkdirs = " ".join(shlex.quote(d) for d in ensure_dirs)
    # The job must SURVIVE the streaming SSH session. A backend restart (or
    # network blip) kills the session; when the docker client was the
    # session's child piping into the channel, the container died with it
    # (observed live: exit 141, SIGPIPE, 2026-07-16). So the container runs
    # detached under nohup with its output going to the persistent LOG FILE
    # (never the SSH pipe), then writes its exit code to <task>.exit. The
    # session merely follows the log and waits for the exit file: if the
    # session dies, only the tail dies, and a restarted backend re-adopts
    # the task by polling for the exit file (_readopt_running_tasks).
    # nohup over setsid: same immunity for this shape, and it exists on
    # macOS too, so the wrapper tests can execute it in a real shell.
    log_q = shlex.quote(remote_log)
    exit_q = shlex.quote(remote_log.rsplit(".", 1)[0] + ".exit")
    runner = f"({docker_cmd}) > {log_q} 2>&1; echo $? > {exit_q}"
    return (
        f"mkdir -p {mkdirs} && rm -f {exit_q} && : > {log_q} && "
        f"nohup bash -c {shlex.quote(runner)} < /dev/null > /dev/null 2>&1 & "
        f"tail -n +1 -F {log_q} 2>/dev/null & TAILPID=$!; "
        f"while [ ! -f {exit_q} ]; do sleep 2; done; sleep 1; "
        f"kill $TAILPID 2>/dev/null; rc=$(cat {exit_q}); exit $rc"
    )


def output_paths_for(template: JobTemplate, parameters: dict,
                     filesystem: str) -> list[str]:
    """The persistent host paths a task writes to (its writable mounts)."""
    persistent_root = f"/lambda/nfs/{filesystem}"
    paths = []
    for volume in template.volumes:
        if volume.read_only or not volume.host.startswith(PERSISTENT_TOKEN):
            continue
        host = volume.host.replace(PERSISTENT_TOKEN, persistent_root)
        for name, value in parameters.items():
            host = host.replace("{{" + name + "}}", str(value))
        paths.append(host)
    return paths


class Dispatcher:
    """Owns the three background loops: tasks, idle, capacity watches."""

    def __init__(
        self,
        settings: Settings,
        orchestrator: Orchestrator,
        queue: TaskQueue,
        templates: dict[str, JobTemplate],
        db: Database,
        lambda_client: LambdaClient,
        *,
        image_checker: ImageChecker | None = None,
        notifier=None,
        worklog=None,
        prefs=None,
        clock=time.monotonic,
    ):
        self.settings = settings
        self.orchestrator = orchestrator
        self.queue = queue
        self.templates = templates
        self.db = db
        self.client = lambda_client
        # Pre-launch image preflight (None = skip, images assumed fine).
        self.image_checker = image_checker
        # NotificationCenter (optional): pings when a job settles. Batch jobs
        # run for hours unattended - the point of Manifold is that you are not
        # sitting there watching the log.
        self.notifier = notifier
        # Worklog (optional): every settled job becomes one markdown entry
        # other agents can read (see worklog.py).
        self.worklog = worklog
        # PreferenceStore (optional): only the monthly budget is read here,
        # and only to decide whether a threshold ping is due. The guards
        # themselves stay in the orchestrator (hard rule) - this loop never
        # refuses anything, it only tells you where you are.
        self.prefs = prefs
        # Highest budget threshold already announced this local month, keyed
        # "YYYY-MM" so a new month starts quiet without any reset job.
        self._budget_announced: tuple[str, float] | None = None
        self._clock = clock
        self._loops: list[asyncio.Task] = []
        # In-flight dispatched jobs: task id -> the asyncio task running it.
        # Guards the queued->running gap against double-dispatch and lets
        # stop() cancel work in progress.
        self._dispatching: dict[str, asyncio.Task] = {}
        # Terminal activity is reported by the terminal WS handler (Phase 5);
        # jobs update it too, so "idle" means neither jobs nor shells.
        self.last_activity: dict[str, float] = {}
        # The idle sweep's own verdict per instance, kept so READERS can see
        # the reasoning the sweep already does (Phase 94). The sweep knows
        # the difference between a model that is still loading and a box
        # nobody wants, and used to spend that knowledge on one decision and
        # throw it away; an agent looking at the same instance got
        # idle_seconds and had to guess. It guessed wrong and terminated a
        # vLLM box six minutes into its warmup.
        #
        # A CACHE, deliberately: answering "is it loading?" live means
        # probing the instance, and this feeds a list the dashboard polls
        # every few seconds. Each entry carries the sweep tick that wrote
        # it so a stale verdict can be recognised rather than trusted.
        self._activity: dict[str, dict] = {}
        # Last "spared because the GPU was working" reason audited per
        # instance, so a long job writes ONE row instead of one per poll,
        # and writes a fresh one when the reason changes. Cleared when the
        # box goes quiet, so a second busy spell is reported again.
        self._gpu_busy_noted: dict[str, str] = {}
        # What the last detached-command probe saw per instance (Phase 95):
        # (checked_at monotonic, handles confirmed ALIVE). The probe rides
        # the 30s telemetry loop; the 15s idle sweep reads this instead of
        # probing again, and treats it as evidence only while FRESH - stale
        # confirmation is no confirmation (see _check_idle).
        self._detached_alive: dict[str, tuple[float, list[str]]] = {}
        # Instances whose idle auto-termination the user switched off; also
        # persisted on the launch row (see keep_alive_enabled).
        self._keep_alive_mem: set[str] = set()
        # Instance ids already given the external-instance keep-alive
        # default, so a user switching it OFF is not overridden next sweep.
        self._external_defaulted: set[str] = set()
        self.on_capacity_available = None   # hook for notifications (set by app)
        # Catalog snapshot for the auto-manage capacity pre-check, refreshed
        # at most every watches.poll_seconds so parked jobs never hammer the
        # rate-limited catalog on the fast auto-manage tick. _capacity_denied
        # holds (gpu_type, region) pairs whose LAUNCH failed on capacity
        # after the snapshot said yes (the catalog lags reality); a denial
        # stands until the next snapshot refresh.
        self._capacity_types: dict | None = None
        self._capacity_checked_at: float = 0.0
        self._capacity_denied: set[tuple[str, str]] = set()
        # Model-readiness cache: task_id -> {ready, error, checked_at}. A
        # served model's task goes 'running' the instant its container is
        # launched, but vLLM needs minutes to pull the image, download
        # weights, and load the GPU before its API answers. This tracks
        # "actually answering", probed via GET /v1/models with a TTL.
        self._readiness: dict[str, dict] = {}
        # Instances whose GPU passed (or timed out of) the first-job CUDA
        # preflight; later jobs skip the probe. In-memory on purpose: a
        # backend restart re-probes once, which costs seconds and re-covers
        # any instance that was mid-boot during the restart.
        self._gpu_ready: set[str] = set()
        # Task ids the user asked to stop: their completion is labeled
        # "cancelled by user" instead of a raw container exit code.
        self._cancel_requested: set[str] = set()
        # F7b: blocked-termination retry backoff, instance id ->
        # (next attempt at, last delay). See _terminate_for.
        self._blocked_retry: dict[str, tuple[float, float]] = {}
        # Max-lifetime ceiling bookkeeping (Phase 76b), all in memory on
        # purpose. The idle loop runs every 15s, so an audit row or a
        # notification per pass would be 5,760 rows a day per instance: a
        # flood that buries the rows that matter. Losing this state on a
        # restart costs at most one repeated note.
        #   _ceiling_notes    instance id -> the audit note we last recorded
        #   _ceiling_pings    instance id -> the notification we last sent
        #   _ceiling_deferred instance id -> why the ceiling did NOT fire
        #                     (read straight back out by ceiling_status)
        self._ceiling_notes: dict[str, str] = {}
        self._ceiling_pings: dict[str, str] = {}
        self._ceiling_deferred: dict[str, str] = {}

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        # Startup adoption (main's lifespan) ran just before this, so any
        # externally-launched instance it connected gets its keep-alive
        # default before the idle loop takes its first look.
        self._protect_external_instances()
        self._loops = [
            asyncio.create_task(self._readopt_running_tasks()),
            asyncio.create_task(self._task_loop()),
            asyncio.create_task(self._idle_loop()),
            asyncio.create_task(self._watch_loop()),
            asyncio.create_task(self._telemetry_loop()),
            asyncio.create_task(self._auto_manage_loop()),
        ]
        if self.settings.launch.adopt_poll_seconds > 0:
            self._loops.append(asyncio.create_task(self._adopt_loop()))

    async def stop(self) -> None:
        for loop in self._loops:
            loop.cancel()
        for fut in self._dispatching.values():
            fut.cancel()
        for fut in list(self._loops) + list(self._dispatching.values()):
            try:
                await fut
            except asyncio.CancelledError:
                pass
        self._loops = []
        self._dispatching = {}

    def touch_activity(self, instance_id: str) -> None:
        """Record activity (job start/end, terminal traffic) on an instance."""
        self.last_activity[instance_id] = self._clock()

    async def _probe_detached(self, instance_id: str, conn) -> None:
        """One SSH round trip that settles finished detached commands and
        records which are still alive. No open handles, no SSH cost."""
        from . import detached as det
        open_rows = self.db.open_detached(instance_id)
        if not open_rows:
            self._detached_alive.pop(instance_id, None)
            return
        pid_by_handle = {r["handle"]: r["pid"] for r in open_rows}
        _code, stdout, _err = await conn.run(
            det.pids_alive_line(pid_by_handle), timeout=20.0)
        alive, settled = det.parse_pids_alive(stdout)
        for handle, exit_code in settled.items():
            self.db.finish_detached(handle, exit_code)
            self.db.record_audit(
                "backend", "detached_finished",
                f"{instance_id}: {handle} "
                + (f"exit {exit_code}" if exit_code is not None
                   # None is VANISHED: it ended, and how is not knowable.
                   # Never written as an exit code.
                   else "vanished (no exit recorded - reboot or external kill)"))
        if alive:
            self.touch_activity(instance_id)
            self._detached_alive[instance_id] = (self._clock(), sorted(alive))
        else:
            self._detached_alive.pop(instance_id, None)

    def _detached_evidence(self, instance_id: str) -> str | None:
        """The reason to spare this box, if a detached command was confirmed
        alive RECENTLY. Stale confirmation is no confirmation: past two
        telemetry intervals with no sighting, this returns None and the
        sweep judges by its other signals - an evidence gate that outlives
        its evidence would be keep-alive wearing a lab coat."""
        seen = self._detached_alive.get(instance_id)
        if seen is None:
            return None
        checked_at, handles = seen
        max_age = max(90.0, self.settings.telemetry.sample_seconds * 2 + 10)
        if self._clock() - checked_at > max_age:
            return None
        rows = {r["handle"]: r for r in self.db.open_detached(instance_id)}
        names = ", ".join(
            f"{h} ({(rows.get(h) or {}).get('note') or (rows.get(h) or {}).get('command', '')[:40]})"
            for h in handles[:3])
        more = f" and {len(handles) - 3} more" if len(handles) > 3 else ""
        return (f"detached command(s) confirmed running here: {names}{more} - "
                f"work started through Manifold protects its own box")

    def _telemetry_says_busy(self, instance_id: str,
                             window_seconds: float) -> str | None:
        """Did this GPU do real work during the window we are calling idle?

        Returns the reason to spare it, or None to let the sweep proceed.

        WHY A WINDOW AND NOT THE LATEST SAMPLE. Utilization is instantaneous
        and spiky: the box that provoked this read 100, 100, 0, 100, 100, 0
        across six consecutive samples while serving. A single reading is
        unreliable in BOTH directions, so the question is "did any sample in
        this window show work", over exactly the period the sweep is about
        to call idle.

        WHY PEAK AND NOT MEAN. One busy card out of eight is a working box.
        A mean would let seven idle cards vote a real job to death.

        WHY VRAM IS NOT CONSULTED. A loaded model sitting at 0% is precisely
        the abandoned server Phase 90 was written to reap - it holds 30GB
        forever and answers nobody. Protecting on VRAM would undo that and
        recreate the hour-long bill. Utilization is work; VRAM is only
        residency.

        WHAT THIS DOES NOT COVER, stated so nobody trusts it further than it
        goes: a CPU-bound phase - CUDA extension builds, weights streaming
        off NFS - shows little or no GPU utilization, so a long setup is NOT
        protected by this. Neither is a box whose telemetry is broken or
        whose sidecar never came up, where there are no samples at all. In
        both cases the sweep behaves exactly as it did before. This only
        ever ADDS protection on positive evidence of work; it never removes
        any, and it is not a substitute for keep-alive or an idle timeout
        that matches the job.
        """
        threshold = getattr(self.settings.idle, "busy_util_pct", 0)
        if not threshold:
            return None                      # switched off in config
        since = self._iso_seconds_ago(window_seconds)
        seen = self.db.peak_util_since(instance_id, since)
        if not seen["samples"]:
            # No evidence either way. Deliberately NOT treated as "idle":
            # it is treated as unchanged, which is what the old behaviour
            # already was. Saying so here so the silence is a decision.
            return None
        peak = seen["peak"] or 0
        if peak < threshold:
            return None
        return (f"GPU reached {peak}% utilization in the last "
                f"{window_seconds:.0f}s ({seen['samples']} sample(s)), so "
                f"work is running here that Manifold cannot see the traffic "
                f"for")

    def _telemetry_note(self, instance_id: str, window_seconds: float) -> str:
        """The GPU evidence behind a reap, in words, for the audit row.

        The sparing branch already records WHY it spared a box. The killing
        branch recorded only the clock - so a terminated instance left
        "idle 1811s (limit 1800s)" and nothing about what its GPU had been
        doing. Reconstructing the 2026-08-16 reap meant joining audit_log
        against telemetry_samples by hand, which only happens if someone
        already suspects the sweep was wrong. Naming the evidence at the
        moment of the decision makes it auditable instead of merely
        recoverable.

        One extra query, on the rarest event in the loop (a box dies once).
        """
        try:
            seen = self.db.peak_util_since(
                instance_id, self._iso_seconds_ago(window_seconds))
        except Exception:   # noqa: BLE001 - never block a reap on a lookup
            return "GPU telemetry unavailable"
        if not seen["samples"]:
            return ("no GPU telemetry in that window, so no evidence of work "
                    "either way")
        return (f"GPU peaked at {seen['peak']}% over {seen['samples']} "
                f"sample(s), under the {self.settings.idle.busy_util_pct}% "
                f"bar for work")

    def _iso_seconds_ago(self, seconds: float) -> str:
        """The wall-clock ISO timestamp `seconds` before now.

        Uses real time, not self._clock: _clock is a monotonic test seam,
        while telemetry rows are stamped with utcnow(). Comparing the two
        would silently select nothing.

        timespec="seconds" to MATCH db.utcnow() exactly. These are compared as
        strings by SQLite, and a bound carrying microseconds sorts after a
        stored value in the same second ('.' > '+'), which quietly drops the
        boundary second from the window. One second, but the kind of thing
        that is invisible until it matters.
        """
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds)
                ).isoformat(timespec="seconds")

    def _note_activity(self, instance_id: str, state: str, busy: bool | None,
                       reason: str) -> None:
        """Record why the sweep judged this instance the way it did."""
        self._activity[instance_id] = {
            "state": state, "busy": busy, "reason": reason,
            "checked_at": self._clock(),
        }

    def activity_status(self, instance_id: str,
                        launch: dict | None = None) -> dict:
        """What the idle sweep last concluded about this instance, for any
        reader deciding whether the box is doing something.

        `busy` is the FACTUAL question - is work loaded and running here -
        and is None when the sweep could not tell. It is deliberately not
        the same question as "may Manifold reap this": a server answering
        requests is busy, and is still subject to the idle timeout if it
        goes quiet for the whole window (Phase 90). Policy stays in
        idle_seconds/timeout_seconds where a reader can see the arithmetic.

        Never guesses. An instance the sweep has not judged yet comes back
        state "unknown" with busy None, because "we have not looked" and
        "there is nothing here" are different answers and only one of them
        is safe to act on.

        `launch` is the instance's launch row when the caller already holds
        it, and answers the one question the sweep structurally cannot: a
        box still BOOTING has no SSH connection, so the sweep has never
        seen it and "unknown" is all it could otherwise say. A booting box
        is the most misleading thing on the list - nothing is running on it
        yet, by design, and it is thirty seconds from being someone's job.
        The first instance destroyed in the 2026-08-17 incident was in
        exactly this state.
        """
        seen = self._activity.get(instance_id)
        if seen is None:
            status = (launch or {}).get("status")
            if status in ("launching", "retrying", "booting"):
                return {"state": "booting", "busy": True,
                        "reason": f"still coming up (launch status "
                                  f"{status!r}); nothing runs on a box that "
                                  f"has not finished booting, and this one "
                                  f"is about to be someone's work",
                        "age_seconds": None}
            return {"state": "unknown", "busy": None,
                    "reason": "no idle sweep has judged this instance yet",
                    "age_seconds": None}
        return {**{k: v for k, v in seen.items() if k != "checked_at"},
                "age_seconds": round(self._clock() - seen["checked_at"], 1)}

    # -- job completion (the single funnel) ----------------------------------------

    def _finish_task(self, task_id: str, *, exit_code: int,
                     output_paths: list[str], error: str = "",
                     notify: bool = True) -> None:
        """Settle a task and ping once.

        EVERY completion path in this file goes through here - dispatch
        errors, bad parameters, a missing image, a lost connection, the
        container's own exit code, an auto-manage failure. One funnel means
        a job can never finish silently, which is the whole point when the
        job is running unattended on a GPU that costs money.
        """
        # A settling server task's model is no longer being served (the task
        # finished, or its instance was torn down and every path funnels here):
        # drop it from the subagent registry so a dead model is not advertised.
        settling = self.queue.get(task_id)
        if settling is not None:
            served = self._served_endpoint(settling)
            if served is not None:
                model_id, remote_port = served
                iid = settling.get("instance_id") or ""
                subagent_engine.deregister_endpoint(
                    model_id, key=self._served_key(iid, remote_port))

        if task_id in self._cancel_requested:
            # The user asked for this stop: label it so the record says
            # "cancelled by user", not a baffling "container exited 137",
            # and skip the failure ping (they are standing right there).
            self._cancel_requested.discard(task_id)
            error = "cancelled by user"
            notify = False
            self.db.record_task_event(task_id, "interrupted", detail=error)
        elif exit_code == 0 and not error:
            self.db.record_task_event(task_id, "finished", detail=f"exit {exit_code}")
        else:
            self.db.record_task_event(task_id, "failed", detail=error or f"exit {exit_code}")

        self.queue.mark_finished(task_id, exit_code=exit_code,
                                 output_paths=output_paths, error=error)
        self._worklog_task(task_id)
        task = self.queue.get(task_id) or {}
        succeeded = task.get("status") == "succeeded"
        # A task that will never succeed takes its queued dependents with it
        # - cancelled included, and whether or not anyone gets pinged. The
        # cascade must not depend on the notification path.
        downstream = [] if succeeded else self._skip_dependents(task_id)
        if not notify or self.notifier is None:
            return
        name = task.get("template", "job")
        where = f" on {task['instance_id']}" if task.get("instance_id") else ""
        if succeeded:
            outputs = task.get("output_paths") or []
            self.notifier.notify(
                "job_succeeded", f"Job succeeded: {name}",
                f"{task_id}{where}"
                + (f"\nOutputs: {', '.join(outputs[:3])}" if outputs else ""),
                ref=task_id,
            )
        else:
            body = f"{task_id}{where}\n{(error or f'exit {exit_code}')[:200]}"
            if downstream:
                body += "\nSkipped downstream: " + ", ".join(downstream)
            self.notifier.notify(
                "job_failed", f"Job failed: {name}", body, ref=task_id,
            )

    def _worklog_task(self, task_id: str) -> None:
        """One worklog entry per settled job: what ran, where, what it cost,
        and where the outputs are - written where other agents can read it."""
        if self.worklog is None:
            return
        try:
            task = self.queue.get(task_id)
            if task is None:
                return
            lines = [f"task {task_id}"]
            iid = task.get("instance_id")
            launch = self.db.find_launch_by_instance(iid) if iid else None
            if launch:
                lines.append(
                    f"{launch['launched_type'] or launch['requested_type']} "
                    f"in {launch['region']}, instance {iid}")
            elif iid:
                lines.append(f"instance {iid}")
            cost = self.db.task_costs().get(task_id)
            if cost:
                mins = cost["runtime_seconds"] / 60.0
                line = f"runtime {mins:.1f} min"
                if cost["actual_cost_cents"] is not None:
                    line += f", cost ${cost['actual_cost_cents'] / 100:.2f}"
                lines.append(line)
            if task.get("exit_code") is not None:
                lines.append(f"exit code {task['exit_code']}")
            if task.get("output_paths"):
                lines.append("outputs: " + ", ".join(task["output_paths"]))
            if task.get("error"):
                lines.append(f"error: {task['error'][:200]}")
            if task.get("status") != "succeeded":
                # The last output lines are the crash signature: they let the
                # next agent session judge the failure without an instance to
                # inspect (the full log may be gone with the box).
                tail = self.queue.get_logs(task_id, tail=3)
                if tail:
                    joined = " / ".join(r["line"].strip() for r in tail)
                    lines.append(f"last output: {joined[:300]}")
            model = (task.get("parameters") or {}).get("model_id")
            if model:
                lines.append(f"model: {model}")
            self.worklog.record(
                f"job {task['template']} {task['status']}", lines)
        except Exception:   # a log entry must never break the funnel
            logger.exception("worklog entry for task %s failed", task_id)

    # -- task dependencies (Phase 77) --------------------------------------------------

    def _dep_state(self, task: dict) -> tuple[bool, str]:
        """Resolve a task's depends_on against the live task table.

        Returns (met, blocked). met means every dependency succeeded and the
        task may dispatch (or, for auto-manage, launch). blocked is non-empty
        when a dependency can never succeed anymore - failed, skipped, or its
        row is gone - so the task should settle as skipped rather than sit
        queued forever."""
        met = True
        for dep_id in task.get("depends_on") or []:
            dep = self.queue.get(dep_id)
            if dep is None:
                return False, f"dependency {dep_id} no longer exists"
            if dep["status"] in ("failed", "skipped"):
                return False, (f"dependency {dep_id} ({dep['template']}) "
                               f"{dep['status']}")
            if dep["status"] != "succeeded":
                met = False
        return met, ""

    def _skip_task(self, task: dict, reason: str) -> None:
        """Settle a queued task as skipped: it never ran and never will.

        Mirrors _finish_task's bookkeeping (event, audit, worklog) but never
        pings per task - the root failure's own notification carries the list
        of everything it took down, so a dead pipeline is one ping, not N."""
        reason = f"skipped: {reason}"
        self.db.record_task_event(task["id"], "skipped", detail=reason)
        self.queue.mark_skipped(task["id"], reason)
        if task.get("auto_manage"):
            # Terminal lifecycle too, or the pending scan would keep offering
            # this job the launch slot forever.
            self.db.set_task_lifecycle(task["id"], "skipped", detail=reason)
        self.db.record_audit("backend", "task_skipped",
                             f"{task['id']}: {reason}")
        self._worklog_task(task["id"])

    def _skip_dependents(self, root_id: str) -> list[str]:
        """Cascade a non-success: every queued task depending on root,
        transitively, settles as skipped. Returns the skipped ids."""
        skipped: list[str] = []
        frontier = [root_id]
        while frontier:
            parent_id = frontier.pop(0)
            parent = self.queue.get(parent_id)
            label = (f"{parent_id} ({parent['template']})"
                     if parent else parent_id)
            for t in self.db.queued_dependents(parent_id):
                self._skip_task(t, f"dependency {label} did not succeed")
                skipped.append(t["id"])
                frontier.append(t["id"])
        return skipped

    # -- idle keep-alive ---------------------------------------------------------------

    def keep_alive_enabled(self, instance_id: str) -> bool:
        """Whether the user has switched idle auto-termination off for this
        instance. Persisted on the launch row when one exists, so it survives
        a backend restart; in-memory otherwise (adopted external instances)."""
        if instance_id in self._keep_alive_mem:
            return True
        launch = self.db.find_launch_by_instance(instance_id)
        return bool(launch and launch.get("keep_alive"))

    def set_keep_alive(self, instance_id: str, enabled: bool) -> dict:
        if enabled:
            self._keep_alive_mem.add(instance_id)
        else:
            self._keep_alive_mem.discard(instance_id)
        launch = self.db.find_launch_by_instance(instance_id)
        if launch:
            self.db.update_launch(launch["id"], keep_alive=1 if enabled else 0)
        # Keep-alive stops the IDLE clock only. Saying "idle auto-termination
        # off" was true until this instance could also carry a max-lifetime
        # ceiling, which keep-alive does not touch — an audit line that
        # over-promises about a destructive control is worse than none.
        detail = f"{instance_id} idle auto-termination {'off' if enabled else 'on'}"
        if enabled and launch and launch.get("max_lifetime_seconds") is not None:
            detail += (f" (its {float(launch['max_lifetime_seconds']):.0f}s "
                       f"max-lifetime ceiling still applies)")
        # current_principal, not "dashboard": any client can flip this
        # switch, and since Phase 79 the audit row says which one did.
        self.db.record_audit(current_principal(), "keep_alive", detail)
        return {"instance_id": instance_id, "keep_alive": enabled}

    def _protect_external_instances(self) -> None:
        """Default keep-alive ON for instances Manifold did not launch.

        An adopted external box's owner works over their own SSH, which the
        idle tracker cannot see, so "no Manifold activity" is not evidence
        it is unused. Without this, adoption (which brings Files/chat/jobs
        to the box) would also put it on the idle termination clock and
        Manifold would kill someone else's running work. Applied once per
        instance id so the user can still switch keep-alive off from the
        card; a backend restart re-applies the default, erring toward
        keeping an externally-owned box alive.
        """
        for iid in list(self.orchestrator.connections):
            if iid in self._external_defaulted:
                continue
            self._external_defaulted.add(iid)
            if self.db.find_launch_by_instance(iid):
                continue
            self._keep_alive_mem.add(iid)
            self.db.record_audit(
                "backend", "keep_alive",
                f"{iid} was launched outside Manifold; idle auto-termination "
                f"defaulted off (switch it on from the instance card)",
            )

    def _effective_timeout(self, instance_id: str) -> float:
        launch = self.db.find_launch_by_instance(instance_id)
        if launch and launch.get("idle_timeout_seconds") is not None:
            return launch["idle_timeout_seconds"]
        return self.settings.idle.timeout_seconds

    def idle_status(self, instance_id: str) -> dict:
        """Idle countdown info for the instance card. idle_seconds counts
        from the last job/terminal activity (0 if none recorded yet).

        Deliberately NOT where the ceiling countdown lives: the instances
        route sets inst["idle"] to None for a box that is not connected, and
        an unreachable box past its ceiling is exactly the one whose limit
        the user most needs to see. See ceiling_status.
        """
        last = self.last_activity.get(instance_id)
        idle = max(0.0, self._clock() - last) if last is not None else 0.0
        return {
            "idle_seconds": round(idle),
            "timeout_seconds": round(self._effective_timeout(instance_id)),
            "keep_alive": self.keep_alive_enabled(instance_id),
        }

    def ceiling_status(self, instance_id: str,
                       launch: dict | None = None) -> dict:
        """Max-lifetime fields for the instance card.

        `launch` is passed in by callers that already loaded the row, so the
        card costs no extra query at a 2s poll x N instances. All three
        fields are None when no ceiling is set, which is the default.
        """
        if launch is None:
            launch = self.db.find_launch_by_instance(instance_id)
        limit = launch.get("max_lifetime_seconds") if launch else None
        if limit is None:
            return {"max_lifetime_seconds": None,
                    "ceiling_seconds_remaining": None,
                    "ceiling_deferred_by": None}
        age = self._launch_age_seconds(launch)
        return {
            "max_lifetime_seconds": float(limit),
            # None, not 0: an unparsable or missing launched_at means we do
            # not know how old this box is, and a confident "0 seconds left"
            # would be a fabricated countdown on a destructive control.
            "ceiling_seconds_remaining": (
                None if age is None else round(float(limit) - age)),
            "ceiling_deferred_by": self._ceiling_deferred.get(instance_id),
        }

    # -- model readiness ---------------------------------------------------------------

    # Re-probe cadence: a model confirmed ready is rechecked rarely; one
    # that's still loading is rechecked often so the UI flips promptly.
    READY_TTL = 30.0
    LOADING_TTL = 3.0

    async def model_ready(self, instance_id: str, task_id: str,
                          port: int) -> dict:
        """Whether the model served by `task_id` actually answers yet.

        Probes GET /v1/models on the instance at most once per TTL and
        caches the verdict. Returns {"ready": bool, "error": str}. The
        error carries the probe failure ('connection refused' while vLLM is
        still starting) so callers can show a helpful loading message."""
        now = self._clock()
        cached = self._readiness.get(task_id)
        ttl = self.READY_TTL if (cached and cached["ready"]) else self.LOADING_TTL
        if cached and now - cached["checked_at"] < ttl:
            return {"ready": cached["ready"], "error": cached["error"]}

        client = self.orchestrator.model_client_for(instance_id)
        if client is None:
            result = {"ready": False, "error": "no managed connection"}
        else:
            try:
                await client.model_info(port)
                result = {"ready": True, "error": ""}
            except ModelClientError as exc:
                result = {"ready": False, "error": str(exc)}
            except Exception as exc:   # never let a probe raise into a caller
                result = {"ready": False, "error": str(exc)}
        self._readiness[task_id] = {**result, "checked_at": now}
        return result

    # -- image preflight ---------------------------------------------------------------

    async def _image_preflight(self, template: JobTemplate) -> str | None:
        """Verify the template's image exists in its registry BEFORE spending
        anything on it. Returns an error message when the image is
        DEFINITIVELY missing; None to proceed.

        Fail-open on anything undetermined (network blip, gated registry):
        a flaky check must never become a wall in front of every launch. The
        job then fails on the instance at `docker pull` — exactly what
        happened before this preflight existed, no worse.
        """
        if self.image_checker is None:
            return None
        try:
            check = await self.image_checker.image_exists(template.image)
        except Exception:   # noqa: BLE001 - preflight must never crash a loop
            logger.exception("image preflight errored for %s", template.image)
            return None
        if check.definitely_missing:
            return (f"image not found: {template.image} ({check.detail}) — "
                    f"fix the template's image before re-queueing")
        if check.exists is None:
            logger.warning("image preflight undetermined for %s: %s "
                           "(proceeding)", template.image, check.detail)
        return None

    # -- task loop -----------------------------------------------------------------

    def _is_server(self, template_name: str) -> bool:
        """Server templates publish ports and stream for their lifetime
        (vllm-serve, sglang-serve); batch templates run to completion."""
        template = self.templates.get(template_name)
        return bool(template is not None and template.ports)

    async def _server_answering(self, instance_id: str, task: dict) -> bool:
        """Is this server task actually serving requests yet?

        False means "do not judge this box idle": either the model is still
        loading, or we could not ask. Both are reasons to leave it alone,
        and both must fail SAFE (protect) rather than terminate on a probe
        that errored - the wrong answer here destroys a running instance.

        The probe is model_ready's, cached per task (30s once ready, 3s
        while loading), so a 15s idle poll costs at most one extra HTTP
        round trip over a forward that is already open.
        """
        endpoint = self._served_endpoint(task)
        if endpoint is None:
            return False
        try:
            verdict = await self.model_ready(instance_id, task["id"],
                                             endpoint[1])
        except Exception:   # noqa: BLE001 - a probe must never terminate a box
            logger.debug("readiness probe failed for %s; treating as busy",
                         instance_id, exc_info=True)
            return False
        return bool(verdict.get("ready"))

    def _served_endpoint(self, task: dict) -> tuple[str, int] | None:
        """The subagent-registry (model_id, remote_port) a server task exposes,
        or None for a batch task.

        The model key is the task's model_id parameter (falling back to the
        template name), matching how the chat/brains code names served models
        (main.py _serving_endpoints). remote_port is the instance-side loopback
        port the template publishes; the engine reaches it over the managed SSH
        forward (never a socket off the backend host), so the port is handed to
        the engine WITH the ManagedConnection, not baked into a URL string.
        """
        template = self.templates.get(task["template"])
        if template is None or not template.ports:
            return None
        model_id = (task.get("parameters") or {}).get("model_id") \
            or task["template"]
        return model_id, template.ports[0].host

    @staticmethod
    def _served_key(instance_id: str, remote_port) -> str:
        """Stable identity for a served endpoint, unique per instance+port so
        two boxes serving the same model on the same port never collide (and
        deregistering one never drops the other)."""
        return f"{instance_id}:{remote_port}"

    def _busy_map(self) -> tuple[set[str], set[str]]:
        """Per-instance busy state from RUNNING tasks: (batch, server).

        The concurrency rule per instance: one batch task at a time (GPU
        contention), one server at a time (its port), but a server and a
        batch task COEXIST - that is the documented serve+synthesize
        pipeline. Instances are independent of each other."""
        busy_batch: set[str] = set()
        busy_server: set[str] = set()
        for task in self.db.running_tasks():
            iid = task.get("instance_id")
            if not iid:
                continue
            if self._is_server(task["template"]):
                busy_server.add(iid)
            else:
                busy_batch.add(iid)
        return busy_batch, busy_server

    def _pick_dispatchable(self) -> list[tuple[dict, str, ManagedConnection]]:
        """Every queued task that has an eligible connected instance RIGHT
        NOW, each bound to its instance. One pass can dispatch to several
        instances at once - each GPU runs its own work independently.

        Binding rules:
        - an auto-managed job runs ONLY on the instance its own lifecycle
          launched, once that instance is 'ready' (connected);
        - a manual job with target_instance_id runs only there (and never
          on an auto-owned box); untargeted manual jobs take the first free
          non-auto-owned instance;
        - per instance: one batch task at a time, one server at a time,
          server+batch coexist (see _busy_map).
        """
        connected = {
            iid: conn
            for iid, conn in self.orchestrator.connections.items()
            if conn.state == ConnectionState.CONNECTED
        }
        if not connected:
            return []
        auto_owned = self.db.auto_managed_instance_ids()
        busy_batch, busy_server = self._busy_map()
        picks: list[tuple[dict, str, ManagedConnection]] = []

        def free(iid: str, server: bool) -> bool:
            return iid not in (busy_server if server else busy_batch)

        def take(task: dict, iid: str) -> None:
            picks.append((task, iid, connected[iid]))
            (busy_server if self._is_server(task["template"])
             else busy_batch).add(iid)

        for task in self.db.queued_tasks():
            if task["id"] in self._dispatching:
                continue   # picked on a previous tick, not yet marked running
            met, blocked = self._dep_state(task)
            if blocked:
                # Normally the cascade settles these the moment the parent
                # fails; this catches anything that slipped through (a crash
                # between settle and cascade). Self-healing, never stuck.
                self._skip_task(task, blocked)
                continue
            if not met:
                continue   # waiting on a parent; younger tasks flow past
            server = self._is_server(task["template"])
            if task["auto_manage"]:
                if task["lifecycle"] != "ready" or not task["launch_id"]:
                    continue
                launch = self.db.get_launch(task["launch_id"])
                iid = launch["lambda_instance_id"] if launch else None
                if iid and iid in connected and free(iid, server):
                    take(task, iid)
            else:
                target = task.get("target_instance_id")
                candidates = [target] if target else [
                    iid for iid in connected if iid not in auto_owned]
                for iid in candidates:
                    if (iid in connected and iid not in auto_owned
                            and free(iid, server)):
                        take(task, iid)
                        break
        return picks

    async def _task_loop(self) -> None:
        while True:
            try:
                self._dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("task loop iteration failed")
            await asyncio.sleep(self.settings.tasks.poll_seconds)

    def _dispatch_once(self) -> None:
        """Spawn every dispatchable task as its own asyncio task.

        Dispatch must NOT await a job inline: a server job (vllm-serve)
        streams for hours, and awaiting it would freeze every other
        instance's queue - the exact bug found at the Phase 35 test pass."""
        for task_id in [t for t, fut in self._dispatching.items() if fut.done()]:
            self._dispatching.pop(task_id)
        for task, instance_id, conn in self._pick_dispatchable():
            self._dispatching[task["id"]] = asyncio.create_task(
                self._run_task_guarded(task, instance_id, conn))

    async def _readopt_running_tasks(self) -> None:
        """Re-adopt tasks left 'running' by a backend restart.

        The restart kills the SSH session that was streaming the job, but the
        container keeps running on the instance, so before this the task sat
        'running' forever with frozen logs (found live, 2026-07-16). The
        wrapped command persists the container's exit code to
        task-logs/<id>.exit on the filesystem, so re-adoption is: wait for the
        instance's connection, then poll for that file and finish the task
        with the real exit code. Live log lines during the gap stay in the
        archived task-logs/<id>.log (noted in the job log)."""
        running = [t for t in self.queue.list() if t["status"] == "running"]
        if not running:
            return
        await asyncio.gather(*(self._readopt_one(t) for t in running))

    async def _readopt_one(self, task: dict, *,
                           reason: str = "backend restarted") -> None:
        task_id = task["id"]
        instance_id = task.get("instance_id") or ""
        self.db.record_task_event(task_id, "resumed", instance_id=instance_id, detail=reason)
        launch = self.db.find_launch_by_instance(instance_id)
        filesystem = (launch or {}).get("filesystem")
        if not filesystem:
            self._finish_task(task_id, exit_code=-1, output_paths=[],
                              error=f"{reason}; instance or its "
                                    "filesystem is gone")
            return
        remote_log = f"/lambda/nfs/{filesystem}/task-logs/{task_id}.log"
        exit_file = f"/lambda/nfs/{filesystem}/task-logs/{task_id}.exit"
        self.queue.append_log(
            task_id,
            f"[manifold] {reason}; reattached (live lines during "
            f"the gap are in {remote_log})")
        self.db.record_audit("backend", "task_readopt",
                             f"{task_id} on {instance_id}")
        template = self.templates.get(task["template"])
        outputs = (output_paths_for(template, task["parameters"], filesystem)
                   if template else [])
        while True:
            conn = self.orchestrator.connections.get(instance_id)
            if conn is None or conn.state != ConnectionState.CONNECTED:
                # Connection manager is (re)dialing; if the instance is truly
                # gone the launch row flips to terminated and we fail honestly.
                if launch and (self.db.get_launch(launch["id"]) or {}).get(
                        "status") == "terminated":
                    self._finish_task(
                        task_id, exit_code=-1, output_paths=[],
                        error="instance terminated while the task was "
                              "detached from a backend restart")
                    return
                await asyncio.sleep(5.0)
                continue
            try:
                # -s: the exit file must exist AND be non-empty. The wrapper's
                # `echo $? > file` creates-then-writes, so a bare `cat` racing
                # that write succeeds with empty output - which read as "gone"
                # and failed a task whose container had just finished fine.
                code, out, _ = await conn.run(
                    f"[ -s {shlex.quote(exit_file)} ] && "
                    f"cat {shlex.quote(exit_file)} 2>/dev/null || "
                    f"docker inspect -f '{{{{.State.Status}}}}' "
                    f"manifold-task-{task_id} 2>/dev/null || echo gone")
            except Exception:
                await asyncio.sleep(5.0)
                continue
            state = out.strip().splitlines()[-1] if out.strip() else "gone"
            if state.lstrip("-").isdigit():
                exit_code = int(state)
                self.queue.append_log(
                    task_id,
                    f"[manifold] exited {exit_code}; log archived at "
                    f"{remote_log}")
                self._finish_task(
                    task_id, exit_code=exit_code, output_paths=outputs,
                    error="" if exit_code == 0
                          else f"container exited {exit_code}")
                return
            if state == "gone":
                # No exit file and no container: it finished and was removed
                # before the exit file existed (task predates this fix).
                self._finish_task(
                    task_id, exit_code=-1, output_paths=outputs,
                    error=f"{reason} mid-task and the container is "
                          f"gone; result unknown, output log at {remote_log}")
                return
            if state == "exited":
                # Container exists but stopped, and no exit file (old wrap):
                # the exit code is still on the container itself.
                try:
                    _, code_out, _ = await conn.run(
                        f"docker inspect -f '{{{{.State.ExitCode}}}}' "
                        f"manifold-task-{task_id}")
                    exit_code = int(code_out.strip())
                except Exception:
                    exit_code = -1
                self._finish_task(
                    task_id, exit_code=exit_code, output_paths=outputs,
                    error="" if exit_code == 0
                          else f"container exited {exit_code}")
                return
            await asyncio.sleep(5.0)   # still running; keep waiting

    async def _run_task_guarded(self, task: dict, instance_id: str,
                                conn: ManagedConnection) -> None:
        """_run_task with a crash net: a spawned task's exception would
        otherwise vanish, leaving the job stuck 'running' forever."""
        try:
            await self._run_task(task, instance_id, conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("dispatched task %s crashed", task["id"])
            current = self.queue.get(task["id"])
            if current and current["status"] in ("queued", "running"):
                self._finish_task(
                    task["id"], exit_code=-1, output_paths=[],
                    error=f"internal dispatch error: {exc}")

    async def _run_task(self, task: dict, instance_id: str,
                        conn: ManagedConnection) -> None:
        task_id = task["id"]
        self.db.record_task_event(task_id, "launched", instance_id=instance_id, detail="Assigned to instance")
        template = self.templates.get(task["template"])
        if template is None:
            self._finish_task(
                task_id, exit_code=-1, output_paths=[],
                error=f"template '{task['template']}' no longer exists",
            )
            return

        launch = self.db.find_launch_by_instance(instance_id)
        filesystem = (launch or {}).get("filesystem")
        if not filesystem:
            self._finish_task(
                task_id, exit_code=-1, output_paths=[],
                error=(
                    f"instance {instance_id} is scratch-only (launched "
                    f"without a filesystem), and this template mounts "
                    f"persistent storage, so there is nowhere to put its "
                    f"files. Launch an instance with a filesystem attached "
                    f"to run it - or use a template that only touches "
                    f"ephemeral scratch."),
            )
            return

        try:
            parameters = coerce_parameters(template, task["parameters"])
        except ParameterError as exc:
            self._finish_task(
                task_id, exit_code=-1, output_paths=[], error=str(exc)
            )
            return

        # Image preflight: a definitively-missing image fails the job here,
        # before any docker pull ever runs on the instance.
        image_error = await self._image_preflight(template)
        if image_error is not None:
            self._finish_task(
                task_id, exit_code=-1, output_paths=[], error=image_error
            )
            self.db.record_audit(
                "backend", "task_image_missing",
                f"{task_id} ({task['template']}): {image_error}")
            return

        docker_cmd = render_docker_command(
            template, parameters, filesystem=filesystem, task_id=task_id
        )
        outputs = output_paths_for(template, parameters, filesystem)

        self.queue.mark_running(task_id, instance_id)
        # A server task (vllm-serve/sglang-serve) now publishes its model on
        # the instance; advertise it in the subagent registry so
        # /subagents/models and the swarm status reflect it. Register WITH this
        # instance's ManagedConnection + the instance-side port, so the engine
        # reaches the model over the managed SSH forward (per the hard rule),
        # not a bare loopback URL. Deregistered in the completion funnel
        # (_finish_task) when the task settles or its instance is torn down.
        served = self._served_endpoint(task)
        if served is not None:
            model_id, remote_port = served
            subagent_engine.register_endpoint(
                model_id, connection=conn, remote_port=remote_port,
                key=self._served_key(instance_id, remote_port))
        if task.get("auto_manage"):
            # The lifecycle loop launched this box and will sync+terminate it
            # once the job settles; mark the running phase for the job card.
            self.db.set_task_lifecycle(task_id, "running",
                                       detail=f"running on {instance_id}")
            self.db.record_audit("backend", "auto_manage_running",
                                 f"job {task_id} instance {instance_id}: dispatched")
        self.touch_activity(instance_id)
        # First job on this instance: hold until CUDA is actually
        # initializable (fabric manager on SXM boxes), instead of burning
        # billed minutes on a container that dies with "No CUDA GPUs".
        await self._ensure_gpu_ready(conn, instance_id, task_id)
        self.queue.append_log(task_id, f"[manifold] dispatching to {instance_id}")
        self.queue.append_log(task_id, f"[manifold] $ {docker_cmd}")
        self.db.record_audit("backend", "task_dispatch",
                             f"{task_id} ({task['template']}) -> {instance_id}")

        # Also keep a persistent copy of the log on the filesystem.
        remote_log = f"/lambda/nfs/{filesystem}/task-logs/{task_id}.log"
        wrapped = wrap_remote_command(
            docker_cmd, remote_log,
            ensure_dirs=["/workspace/ephemeral",
                         f"/lambda/nfs/{filesystem}/task-logs"],
        )

        for attempt in (1, 2):
            try:
                self.db.record_task_event(task_id, "started", instance_id=instance_id, detail="Command execution started")
                exit_code, stdout, stderr = await self._stream_run(
                    conn, wrapped, task_id
                )
            except ConnectionError as exc:
                # The streaming session died, but the container survives it
                # by design (nohup + log file + exit file, see
                # wrap_remote_command). Failing the task here orphaned a
                # container that was still running - and still billing - so
                # instead hand off to the same exit-file poller a backend
                # restart uses: it waits out the reconnect and settles the
                # task with the container's real result.
                self.queue.append_log(task_id,
                                      f"[manifold] connection lost: {exc}")
                await self._readopt_one(task, reason="connection lost")
                return
            finally:
                self.touch_activity(instance_id)
            # Boot race, last resort: the container itself reported CUDA
            # missing even though the preflight passed. Wait for readiness
            # again and retry ONCE, instead of confusing the user with a
            # failure that would succeed a minute later (field report).
            if exit_code == 0 or attempt == 2:
                break
            recent = " ".join(
                r["line"] for r in self.queue.get_logs(task_id, tail=40)
            ) + stdout + stderr
            if not any(sig in recent for sig in CUDA_RACE_SIGNATURES):
                break
            self.queue.append_log(
                task_id,
                "[manifold] the GPU was not visible inside the container "
                "(boot race); waiting and retrying once")
            self._gpu_ready.discard(instance_id)
            await asyncio.sleep(20)
            await self._ensure_gpu_ready(conn, instance_id, task_id)

        for line in stderr.splitlines():
            self.queue.append_log(task_id, f"[stderr] {line}")
        self.queue.append_log(
            task_id,
            f"[manifold] exited {exit_code}; log archived at {remote_log}",
        )
        self._finish_task(
            task_id,
            exit_code=exit_code,
            output_paths=outputs,
            error="" if exit_code == 0 else f"container exited {exit_code}",
        )

    async def _ensure_gpu_ready(self, conn: ManagedConnection,
                                instance_id: str, task_id: str) -> None:
        """Gate the FIRST job on an instance until its GPU can really run
        CUDA. Field case: an A100 SXM4 job dispatched 2.5 min after cloud-init
        finished died with "No CUDA GPUs are available" - the fabric manager
        was still initializing, invisibly to every nvidia-smi hand-check.

        Fail-open by design: when the window expires (or the probe itself
        errors), dispatch anyway with an honest log line - a wrong probe must
        never brick job dispatch, and the pre-preflight behavior is the floor.
        Either way the instance is marked so later jobs skip the probe."""
        if instance_id in self._gpu_ready:
            return
        timeout = self.settings.tasks.gpu_ready_timeout_seconds
        poll = self.settings.tasks.gpu_ready_poll_seconds
        deadline = self._clock() + timeout
        waiting_logged = False
        while True:
            try:
                exit_code, stdout, _ = await conn.run(GPU_PROBE_COMMAND)
            except Exception as exc:
                # A dead/flaky connection here will fail the job properly at
                # dispatch; don't let the preflight be the thing that blocks.
                self.queue.append_log(
                    task_id,
                    f"[manifold] GPU preflight skipped (probe error: {exc})")
                self._gpu_ready.add(instance_id)
                return
            ready, reason = gpu_readiness(exit_code, stdout)
            if ready:
                self._gpu_ready.add(instance_id)
                if waiting_logged:
                    self.queue.append_log(
                        task_id, f"[manifold] GPU ready ({reason})")
                return
            if self._clock() >= deadline:
                self.queue.append_log(
                    task_id,
                    f"[manifold] GPU still not ready after {timeout:.0f}s "
                    f"({reason}); dispatching anyway - if this job fails "
                    f"with 'No CUDA GPUs are available', retry it in a few "
                    f"minutes",
                )
                self._gpu_ready.add(instance_id)
                return
            if not waiting_logged:
                self.queue.append_log(
                    task_id,
                    f"[manifold] waiting for the GPU to finish initializing "
                    f"({reason}) - on A100 SXM boxes the fabric manager can "
                    f"take a few minutes after boot",
                )
                waiting_logged = True
            self.touch_activity(instance_id)   # waiting is not idleness
            await asyncio.sleep(poll)

    async def _stream_run(self, conn: ManagedConnection, command: str,
                          task_id: str) -> tuple[int, str, str]:
        """Run a command, streaming stdout lines into the task log.

        Uses the connection's streaming API when available (real asyncssh:
        create_process); falls back to run() for simple mocks.
        """
        ssh = conn.ssh_connection()
        if ssh is None:
            raise ConnectionError(f"no SSH connection (state: {conn.state.value})")
        create_process = getattr(ssh, "create_process", None)
        if create_process is None:
            exit_code, stdout, stderr = await conn.run(command)
            for line in stdout.splitlines():
                self.queue.append_log(task_id, line)
            return exit_code, stdout, stderr

        process = await create_process(command)
        stdout_lines: list[str] = []
        async for line in process.stdout:
            line = line.rstrip("\n")
            stdout_lines.append(line)
            self.queue.append_log(task_id, line)
        stderr = await process.stderr.read()
        await process.wait()
        exit_code = process.exit_status if process.exit_status is not None else -1
        return exit_code, "\n".join(stdout_lines), stderr or ""

    # -- idle loop -----------------------------------------------------------------

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.idle.poll_seconds)
            try:
                await self._check_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("idle loop iteration failed")

    async def _check_idle(self) -> None:
        """Terminate instances past a limit, via the STANDARD flow.

        Two independent verdicts, in this order:

        CEILING (opt-in, off by default). The launch's max_lifetime_seconds
        measured from launched_at: a wall-clock bound that no session,
        terminal, output stream, or backend restart can push out. It is the
        backstop for "I forgot it was running", so it overrides keep-alive
        and it fires THROUGH a served model — a vllm-serve task never leaves
        'running', so a ceiling that deferred to it would be permanently
        unreachable on the most expensive workload Manifold runs.

        IDLE (on by default). Connected, no running BATCH task, no activity
        (job, terminal, or a request to a served model) for
        idle.timeout_seconds. The clock starts when the connection comes up,
        so a freshly booted instance gets a full quiet period before it is
        eligible.

        A SERVER task no longer makes its box immune (Phase 90). It used to,
        and the consequence was a hole shaped exactly like the bill this
        product exists to prevent: a vllm-serve task never leaves 'running',
        so an abandoned model server was never idle, and with no ceiling set
        it billed until a human noticed. One did, an hour and $1.29 later.

        What protects a server instead is what "idle" actually means for one:
        - STILL LOADING (serving but not answering /v1/models yet) counts as
          activity. Weights for a large model take longer than the idle
          window, and reaping a box seconds before it becomes useful is the
          worst possible moment.
        - IN USE resets the clock on its own: /v1/chat/completions, the chat
          panel, and run all call touch_activity already.
        - READY AND SILENT for the whole window is the real definition of an
          abandoned server, and is now terminated like any other idle box.

        A BATCH job still pins its instance absolutely. A fine-tune has a
        90%, and destroying one to save a billing hour is the trade this
        project refuses to make - the same reasoning the ceiling uses.
        auto_owned and keep-alive are untouched escape hatches.
        """
        now = self._clock()
        # Instances an auto-managed job owns are governed by that job's
        # lifecycle, which owns teardown (sync -> terminate). The idle loop
        # must not race it: skip them entirely, keep-alive or not. If the
        # lifecycle is ever lost (its job reached a terminal state), the
        # instance drops out of this set and the idle loop resumes as backstop.
        auto_owned = self.db.auto_managed_instance_ids()
        running = self.db.running_tasks()
        # A running task pins ITS OWN instance only (Phase 35): with several
        # GPUs up, a job on box A must not keep an idle box B billing. Both
        # verdicts now use the BATCH set below - Phase 90 removed the last
        # reader of the all-tasks set rather than leave a name that reads
        # like a live protection but guards nothing.
        #
        # The ceiling defers to a BATCH job only. A batch job has a 90% — a
        # fine-tune destroyed at 90% is the failure this project refuses to
        # cause. A server daemon has no 90%: it streams until something stops
        # it, so deferring to one would make the ceiling a feature that never
        # does anything, which is worse than not shipping it.
        pinned_batch = {t["instance_id"] for t in running
                        if t.get("instance_id")
                        and not self._is_server(t["template"])}
        # The server task per instance, so the idle verdict can ask the one
        # question that separates "loading" from "abandoned": is it ANSWERING
        # yet? Last writer wins - a box running two servers is protected
        # while either is still coming up, which is the safe direction.
        serving = {t["instance_id"]: t for t in running
                   if t.get("instance_id") and self._is_server(t["template"])}
        for instance_id, conn in list(self.orchestrator.connections.items()):
            # One poison instance must not disable termination for every box
            # behind it in this iteration. Only TerminationBlocked used to be
            # caught, so a ProviderError out of terminate() escaped to the
            # loop's blanket handler and abandoned the rest of the sweep —
            # every cycle, silently, for as long as the bad box stayed up.
            try:
                connected = conn.state == ConnectionState.CONNECTED
                launch = self.db.find_launch_by_instance(instance_id)
                over = self._ceiling_breach(launch)

                if over is not None:
                    # -- ceiling verdict. At most ONE terminate per instance
                    # per pass: two independent blocks would let the same box
                    # be terminated twice, and the second call re-enters
                    # rescue() against an already-popped connection, gets an
                    # empty report back, and fires a second provider destroy.
                    if not connected:
                        # We cannot rescue what we cannot reach: rescue()
                        # returns an empty report over a dead connection, so
                        # terminating here would destroy data behind a rescue
                        # that did nothing. The box outlives its ceiling; the
                        # user is told, not left to discover the bill.
                        self._note_ceiling_unreachable(instance_id)
                        self.last_activity.pop(instance_id, None)
                    elif instance_id in auto_owned:
                        self._defer_ceiling_to_auto_managed(
                            instance_id, auto_owned)
                    elif instance_id in pinned_batch:
                        self._note_ceiling_deferred(
                            instance_id, "batch job running")
                    else:
                        self._clear_ceiling_deferral(instance_id)
                        await self._terminate_for(
                            instance_id, "ceiling",
                            f"{over:.0f}s past max_lifetime")
                    continue

                self._clear_ceiling_deferral(instance_id)
                self._maybe_warn_ceiling(instance_id, launch)

                # -- idle verdict. Pinned by a BATCH task only (Phase 90); a
                # server is judged by whether anyone is using it, below.
                if instance_id in auto_owned:
                    self._note_activity(
                        instance_id, "auto_managed", True,
                        "an auto-managed job owns this instance and will "
                        "tear it down when it finishes")
                    continue
                if instance_id in pinned_batch:
                    self._note_activity(
                        instance_id, "batch_running", True,
                        "a batch job is running here; it pins the instance "
                        "until it completes")
                    continue
                if not connected:
                    # Not reachable: don't count unreachable time as idle.
                    self._note_activity(
                        instance_id, "unreachable", None,
                        "not reachable over SSH, so nothing can be "
                        "concluded about what is running on it")
                    self.last_activity.pop(instance_id, None)
                    continue
                if self.keep_alive_enabled(instance_id):
                    self._note_activity(
                        instance_id, "keep_alive", False,
                        "idle auto-termination is switched off for this "
                        "instance; it will not be reaped")
                    continue
                # Detached work (Phase 95): a pid the telemetry probe
                # confirmed alive within the last couple of intervals is
                # evidence of work - the case the GPU gate below cannot see
                # (an rsync or a compile is 0% GPU from start to finish).
                # Fresh evidence only; _detached_evidence returns None once
                # the sighting goes stale.
                evidence = self._detached_evidence(instance_id)
                if evidence is not None:
                    self._note_activity(
                        instance_id, "detached_running", True, evidence)
                    self.touch_activity(instance_id)
                    continue
                server = serving.get(instance_id)
                if server is not None and not await self._server_answering(
                        instance_id, server):
                    # Coming up (or unprobeable): treat as activity so the
                    # window restarts from readiness, not from dispatch. A
                    # 70B that downloads for 40 minutes must never be reaped
                    # at minute 30 for the crime of not being loaded yet.
                    self._note_activity(
                        instance_id, "loading", True,
                        f"{server.get('template', 'a model server')} is "
                        f"starting up and not answering yet - loading "
                        f"weights can take far longer than the idle window")
                    self.touch_activity(instance_id)
                    continue
                timeout = self._effective_timeout(instance_id)
                last = self.last_activity.setdefault(instance_id, now)
                if now - last < timeout:
                    quiet = now - last
                    if server is not None:
                        self._note_activity(
                            instance_id, "serving", True,
                            f"{server.get('template', 'a model server')} is "
                            f"loaded and answering; last request "
                            f"{quiet:.0f}s ago")
                        continue
                    # The telemetry question is asked HERE too, not only at
                    # the reap gate below. The gate protects the box from
                    # Manifold; this protects it from the reader. A box
                    # 30 seconds into a 7200s window is nowhere near being
                    # reaped, so the gate never runs - and an agent looking
                    # at the list would be told busy=false about a GPU at
                    # 100%, which is precisely the reading that destroyed
                    # someone's model server. Verified against a live box
                    # immediately after shipping the gate alone.
                    busy = self._telemetry_says_busy(instance_id, timeout)
                    if busy is not None:
                        self._note_activity(
                            instance_id, "gpu_busy", True, busy)
                    else:
                        self._note_activity(
                            instance_id, "idle_countdown", False,
                            f"no jobs or terminal traffic for {quiet:.0f}s "
                            f"of a {timeout:.0f}s window, and no GPU work "
                            f"in that window either")
                    continue
                # LAST GATE. Manifold has seen no traffic for a full window,
                # which is not the same as the box having done nothing - it
                # only ever counted the traffic that came through Manifold.
                # A model served over the user's own SSH tunnel, or a
                # detached training run, drives a GPU flat out and touches
                # nothing here. Ask the telemetry we are already collecting.
                busy = self._telemetry_says_busy(instance_id, timeout)
                if busy is not None:
                    self._note_activity(
                        instance_id, "gpu_busy", True, busy)
                    # Restart the window from now: the box is working, and
                    # the next pass should judge the NEXT quiet period, not
                    # re-run this arithmetic on the same stale timestamp.
                    self.touch_activity(instance_id)
                    if self._gpu_busy_noted.get(instance_id) != busy:
                        self._gpu_busy_noted[instance_id] = busy
                        self.db.record_audit(
                            "backend", "idle_deferred_gpu_busy",
                            f"{instance_id}: {busy}")
                    continue
                self._gpu_busy_noted.pop(instance_id, None)
                await self._terminate_for(
                    instance_id, "idle",
                    f"idle {now - last:.0f}s (limit {timeout:.0f}s); "
                    f"{self._telemetry_note(instance_id, timeout)}")
            except Exception:   # noqa: BLE001 - see the comment above
                logger.exception("idle check failed for %s", instance_id)
                continue

    # -- the ceiling ---------------------------------------------------------------

    @staticmethod
    def _launch_age_seconds(launch: dict | None) -> float | None:
        """Wall-clock seconds since the provider ACCEPTED this launch.

        None (never an exception) when there is no row, no launched_at, or an
        unparsable one: an unreadable anchor must be a no-op, not a crash and
        certainly not a termination.
        """
        stamp = (launch or {}).get("launched_at")
        if not stamp:
            return None
        try:
            started = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            return None
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - started).total_seconds()

    def _ceiling_breach(self, launch: dict | None) -> float | None:
        """Seconds this launch is PAST its max lifetime, or None.

        WALL clock, never self._clock(). _clock defaults to time.monotonic,
        and launched_at is a UTC ISO timestamp in SQLite: subtracting one
        from the other yields a number with no meaning whatsoever, and that
        number decides whether a paid instance is destroyed. Wall clock is
        also the only anchor that survives a backend restart, which is the
        entire reason the ceiling lives in the database.
        """
        if not launch:
            return None
        limit = launch.get("max_lifetime_seconds")
        if limit is None:
            return None                       # the default: no ceiling at all
        age = self._launch_age_seconds(launch)
        if age is None:
            return None
        over = age - float(limit)
        return over if over >= 0 else None

    def _auto_owned_active(self, auto_owned: set[str]) -> set[str]:
        """Auto-managed instances whose job is still doing something.

        Excludes 'terminating'. A blocked auto-managed teardown parks in that
        state and retries forever, so treating it as "the lifecycle has this
        covered" would make the ceiling unreachable in the one state where
        money burns without bound. v1 auto-manage is sequential: at most one
        job is in flight, which is the same assumption _auto_manage_once
        makes.
        """
        job = self.db.active_auto_managed_task()
        if job is None or job.get("lifecycle") != "terminating":
            return set(auto_owned)
        stuck = self._job_instance_id(job)
        return {i for i in auto_owned if i != stuck}

    def _defer_ceiling_to_auto_managed(self, instance_id: str,
                                       auto_owned: set[str]) -> None:
        """The ceiling fired on a box an auto-managed job owns.

        Either way we issue no terminate of our own — the job's lifecycle is
        the only thing allowed to tear its own instance down, and racing it
        double-rescues and double-destroys. What differs is how loud we are:
        a live job will finish and clean up, while one parked in 'terminating'
        is blocked on files nobody has saved and will sit there billing until
        a human acts.
        """
        if instance_id in self._auto_owned_active(auto_owned):
            self._note_ceiling_deferred(
                instance_id, "auto-managed job owns teardown")
            return
        self._note_ceiling_deferred(
            instance_id, "auto-managed teardown blocked on unsaved files")
        detail = ""
        job = self.db.active_auto_managed_task()
        if job is not None:
            detail = job.get("lifecycle_detail") or ""
        self._notify_ceiling_once(
            instance_id, "blocked-teardown",
            f"Instance {instance_id[:12]} is past its max lifetime",
            (f"Its auto-managed job is trying to shut it down and cannot: "
             f"{detail or 'files on it could not be saved'}. "
             f"Manifold will not force a destroy. Save or discard those "
             f"files from the instance card and the teardown completes on "
             f"its own — until then the GPU is still billing."))

    def _clear_ceiling_deferral(self, instance_id: str) -> None:
        """Forget any recorded deferral for this instance.

        Called on every pass where the ceiling is not deferring, which is what
        makes the card's ceiling_deferred_by honest the moment the blocking
        job finishes — and what re-arms the audit note if it comes back.
        """
        self._ceiling_deferred.pop(instance_id, None)
        self._ceiling_notes.pop(instance_id, None)

    def _note_ceiling_deferred(self, instance_id: str, why: str) -> None:
        """Record (once) that the ceiling fired and we did NOT terminate."""
        self._ceiling_deferred[instance_id] = why
        if self._ceiling_notes.get(instance_id) == why:
            return                      # already recorded; do not re-flood
        self._ceiling_notes[instance_id] = why
        self.db.record_audit(
            "backend", "ceiling_deferred",
            f"{instance_id} is past its max lifetime but {why}; "
            f"not terminating")

    def _note_ceiling_unreachable(self, instance_id: str) -> None:
        """Past its ceiling and off SSH: state the limit, do not hide it."""
        self._ceiling_deferred[instance_id] = "instance unreachable"
        if self._ceiling_notes.get(instance_id) != "unreachable":
            self._ceiling_notes[instance_id] = "unreachable"
            self.db.record_audit(
                "backend", "ceiling_unreachable",
                f"{instance_id} is past its max lifetime but is not "
                f"reachable over SSH; its files cannot be saved, so it was "
                f"NOT terminated")
        self._notify_ceiling_once(
            instance_id, "unreachable",
            f"Instance {instance_id[:12]} is past its max lifetime and "
            f"unreachable",
            "Manifold only terminates a box it can reach and save the files "
            "off first, so this one is still billing. Check your cloud "
            "console.")

    def _maybe_warn_ceiling(self, instance_id: str,
                            launch: dict | None) -> None:
        """One heads-up per instance, on a FIXED lead before the ceiling.

        Fixed, not a percentage: 90% of a 30-day ceiling is three days of
        nagging, and 90% of a 70-minute one is seven minutes of notice. A
        ceiling shorter than twice the lead gets no warning at all, because
        that warning would land at (or before) launch and mean nothing.
        """
        limit = (launch or {}).get("max_lifetime_seconds")
        if limit is None:
            return
        lead = self.settings.idle.ceiling_warning_seconds
        if lead <= 0 or float(limit) < 2 * lead:
            return
        age = self._launch_age_seconds(launch)
        if age is None:
            return
        remaining = float(limit) - age
        if remaining > lead:
            return
        self._notify_ceiling_once(
            instance_id, "warning",
            f"Instance {instance_id[:12]} hits its max lifetime in "
            f"{remaining / 60:.0f} min",
            (f"It has been running for {age / 3600:.1f}h against a "
             f"{float(limit) / 3600:.1f}h limit. Manifold will terminate it "
             f"then — if it can reach it and save its files first. Raise the "
             f"limit from the instance card if you still need the box."))

    def _notify_ceiling_once(self, instance_id: str, key: str, title: str,
                             body: str) -> None:
        """One ceiling ping per (instance, kind of news). In memory: the idle
        loop runs every 15s and this is a bell, not a log."""
        if self.notifier is None:
            return
        if self._ceiling_pings.get(instance_id) == key:
            return
        self._ceiling_pings[instance_id] = key
        self.notifier.notify("instance_ceiling", title, body, ref=instance_id)

    # F7b: a blocked termination is retried by this loop forever, and every
    # retry re-runs the FULL rescue (sidecar walk, whole-scratch rsync,
    # per-file SSH downloads) against files that have not moved. At the 15s
    # idle poll that is four full rescues a minute, indefinitely. Back off
    # per instance instead: 15s -> 30s -> 60s -> ... -> 15 minutes.
    BLOCKED_RETRY_BASE_SECONDS = 15.0
    BLOCKED_RETRY_MAX_SECONDS = 900.0

    async def _terminate_for(self, instance_id: str, kind: str,
                             detail: str) -> None:
        """The single destructive call in this loop. `force=True` is the
        user's explicit "burn it" (CLAUDE.md); no unattended loop may issue
        it. If you are adding a `force` parameter here, you are adding an
        automatic destroy path — that needs its own phase and its own gate.

        force=False means terminate() rescues the instance's data first (sync
        to the persistent volume and/or download here, per the data-safety
        policy) and refuses only if something could NOT be saved. No
        sync-then-force dance lives here: that belonged to this loop back when
        terminate() did not rescue, and it meant every OTHER caller had to
        reimplement the same dance.

        `kind` names the verdict that sent us here ("idle", "ceiling"). It is
        the audit action's prefix and nothing more — it never reaches
        terminate() and never changes what is destroyed or how.
        """
        retry_at, last_delay = self._blocked_retry.get(instance_id, (0.0, 0.0))
        if self._clock() < retry_at:
            return                          # still backing off from a block
        logger.info("instance %s: %s termination requested (%s)",
                    instance_id, kind, detail)
        self.db.record_audit("backend", f"{kind}_termination",
                             f"{instance_id} {detail}")
        try:
            await self.orchestrator.terminate(instance_id, force=False)
            self._blocked_retry.pop(instance_id, None)
        except TerminationBlocked as exc:
            # The rescue could not save everything. Leave the box up with the
            # data intact rather than destroying it; the orchestrator has
            # already pinged the user (once, until the file set changes).
            delay = min(self.BLOCKED_RETRY_MAX_SECONDS,
                        max(self.BLOCKED_RETRY_BASE_SECONDS, last_delay * 2))
            self._blocked_retry[instance_id] = (self._clock() + delay, delay)
            logger.warning(
                "%s termination of %s refused: %d file(s) unsaveable; "
                "retrying in %.0fs", kind, instance_id, len(exc.files), delay)
            self.db.record_audit(
                "backend", f"{kind}_termination_blocked",
                f"{instance_id}: {len(exc.files)} file(s) could not be "
                f"saved; instance left running",
            )
        # Both outcomes settle this instance's idle clock: on success the box
        # is gone (a stale clock would be reported for a dead instance); on a
        # block the countdown has already fired and re-arming it would hide
        # the fact that this box is over its limit and still billing.
        self.last_activity.pop(instance_id, None)
        # The stored verdict goes with it: a terminated box that still
        # answers "serving" would be a lie, and a blocked one is about to be
        # re-judged on the next pass anyway.
        self._activity.pop(instance_id, None)
        self._gpu_busy_noted.pop(instance_id, None)

    # -- auto-manage lifecycle loop -----------------------------------------------------

    async def _auto_manage_loop(self) -> None:
        """Drive auto-managed jobs through their whole instance lifecycle:

            waiting -> launching -> ready -> running -> syncing -> terminating -> done

        Sequential (v1): at most one auto-managed job holds the single-instance
        slot at a time; the next waits its turn. Every guarded step routes
        through the SAME orchestrator functions the dashboard uses
        (request_launch, sync_ephemeral, terminate) — no guard is duplicated
        or bypassed. The loop is stateless across ticks (it reads the job's
        lifecycle from the DB each time), so a backend restart resumes wherever
        the job left off.
        """
        while True:
            await asyncio.sleep(self.settings.auto_manage.poll_seconds)
            try:
                await self._auto_manage_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("auto-manage loop iteration failed")

    async def _auto_manage_once(self) -> None:
        # One in-flight job at a time; only promote the next pending job when
        # the slot is free (the current one reached a terminal state).
        job = self.db.active_auto_managed_task()
        if job is None:
            job = self._next_ready_auto_job()
            if job is None:
                return
        lc = job["lifecycle"]
        if lc in ("queued", "waiting"):
            await self._auto_launch(job)
        elif lc == "launching":
            self._auto_check_boot(job)
        elif lc in ("ready", "running"):
            # 'ready' normally just waits for the task loop to dispatch, but a
            # dispatch-time failure (image missing, bad parameters) finishes
            # the task WITHOUT ever reaching 'running' — the settled-check
            # must still advance to syncing/terminating or the box would sit
            # launched forever (the idle loop deliberately skips it).
            self._auto_check_run_done(job)
        elif lc == "syncing":
            await self._auto_sync(job)
        elif lc == "terminating":
            await self._auto_terminate(job)

    def _next_ready_auto_job(self) -> dict | None:
        """Oldest pending auto-managed job whose dependencies are met.

        Promotion LAUNCHES a GPU, so a child whose parent is still running
        must not be promoted - a box booted early bills for the whole wait.
        The child waits on its parent, not on the slot, so a younger
        independent job may take the slot meanwhile and nothing starves.
        Doomed jobs (a parent that can never succeed) settle as skipped here
        rather than clogging the pending scan."""
        for job in self.db.pending_auto_managed_tasks():
            met, blocked = self._dep_state(job)
            if blocked:
                self._skip_task(job, blocked)
                continue
            if met:
                return job
        return None

    def _job_instance_id(self, job: dict,
                         launch_id: str | None = None) -> str | None:
        lid = launch_id or job.get("launch_id")
        if not lid:
            return None
        launch = self.db.get_launch(lid)
        return launch["lambda_instance_id"] if launch else None

    def _transition(self, job: dict, lifecycle: str, detail: str = "", *,
                    launch_id: str | None = None,
                    instance_id: str | None = None,
                    audit_action: str | None = None) -> None:
        """Move a job to a new lifecycle state and audit the change once.

        Every transition writes an audit row carrying the job id and (when the
        instance exists) the instance id, per the spec. Re-entering the same
        state (e.g. staying in 'waiting') does not re-audit."""
        changed = job.get("lifecycle") != lifecycle
        self.db.set_task_lifecycle(job["id"], lifecycle, detail=detail or None,
                                   launch_id=launch_id, stamp=changed)
        if changed:
            iid = instance_id or self._job_instance_id(job, launch_id)
            loc = f"job {job['id']}" + (f" instance {iid}" if iid else "")
            self.db.record_audit(
                "backend", audit_action or f"auto_manage_{lifecycle}",
                loc + (f": {detail}" if detail else ""))

    def _fail(self, job: dict, reason: str) -> None:
        task = self.queue.get(job["id"])
        if task and task["status"] == "queued":
            # Never dispatched (guard rejection or boot failure): close it out
            # so it leaves the active list with the reason attached.
            self._finish_task(job["id"], exit_code=-1, output_paths=[],
                                     error=reason)
        self._transition(job, "failed", detail=reason,
                         audit_action="auto_manage_failed")

    async def _capacity_status(self, gpu_type: str, region: str) -> bool | None:
        """Does the catalog report capacity for gpu_type in region right now?

        None means UNKNOWN (catalog unreachable, or a type the catalog does
        not list): callers fail open and let the launch path find out for
        real, so a flaky catalog can never park a job forever."""
        now = self._clock()
        if (self._capacity_types is None
                or now - self._capacity_checked_at
                >= self.settings.watches.poll_seconds):
            try:
                self._capacity_types = \
                    await self.orchestrator.client.list_instance_types()
                self._capacity_denied.clear()
            except Exception:
                self._capacity_types = None
            self._capacity_checked_at = now
        if self._capacity_types is None:
            return None
        info = self._capacity_types.get(gpu_type)
        if info is None:
            return None
        if (gpu_type, region) in self._capacity_denied:
            return False
        return region in info.regions_with_capacity

    def _park_for_capacity(self, job: dict, *, quiet: bool = False) -> None:
        """Hold the job in 'waiting' until its GPU has capacity again.

        This is the fire-and-forget promise: a job queued against a full
        region does not fail, it waits, and launches the moment the catalog
        shows capacity. Notified once (capacity_available kind, same toggle
        as capacity watches); re-parking after a lost launch race passes
        quiet=True."""
        first_park = not quiet and job["lifecycle"] != "waiting"
        self._transition(
            job, "waiting",
            detail=(f"no {job['gpu_type']} capacity in {job['region']}; "
                    f"will launch the moment it appears"),
            audit_action="auto_manage_waiting_capacity")
        if first_park and self.notifier is not None:
            self.notifier.notify(
                "capacity_available",
                f"Job parked: no {job['gpu_type']} capacity",
                f"{job['id']} ({job['template']}) is waiting for "
                f"{job['gpu_type']} in {job['region']}. It will launch, "
                f"run, and terminate on its own when capacity appears.",
                ref=job["id"],
            )

    async def _auto_launch(self, job: dict) -> None:
        """queued/waiting -> launching, through the guarded launch path."""
        # Image preflight FIRST: never boot (and bill) a GPU to discover at
        # docker-pull time that the template's image does not exist.
        template = self.templates.get(job["template"])
        if template is not None:
            image_error = await self._image_preflight(template)
            if image_error is not None:
                self._fail(job, image_error)
                return
        # Capacity pre-check: a full region parks the job instead of burning
        # launch attempts into a wall (the old behavior failed the job after
        # the retries ran out). Unknown fails open.
        if await self._capacity_status(job["gpu_type"], job["region"]) is False:
            self._park_for_capacity(job)
            return
        try:
            launch = await self.orchestrator.request_launch(
                instance_type=job["gpu_type"], region=job["region"],
                filesystem=job["filesystem"],
                # Chain attribution (Phase 79): the loop launches, but the
                # PERSON is whoever enqueued the job.
                created_by=job.get("created_by"))
        except LaunchRejected as exc:
            if exc.reason_code == "concurrency":
                # The single slot is busy (a manual/external instance is up).
                # Wait and retry next tick; do NOT fail the job.
                self._transition(
                    job, "waiting",
                    detail=f"waiting for a free instance slot ({exc.detail})",
                    audit_action="auto_manage_waiting")
                return
            # budget / validation / mode: can never admit -> fail with reason.
            self._fail(job, exc.detail)
            return
        self._transition(job, "launching", launch_id=launch["id"],
                         detail=f"launching {job['gpu_type']} in {job['region']}")

    def _auto_check_boot(self, job: dict) -> None:
        """launching -> ready once the launch is active and connected."""
        launch = self.db.get_launch(job["launch_id"]) if job["launch_id"] else None
        if launch is None:
            self._fail(job, "launch record missing")
            return
        if launch["status"] == "failed":
            error = launch["error"] or "launch failed"
            if "No capacity" in error:
                # The pre-check's snapshot said yes but the launch lost the
                # race (the catalog lags reality). Deny this pair until the
                # next snapshot refresh and park again - capacity scarcity
                # is a reason to wait, never a reason to fail the job.
                self._capacity_denied.add((job["gpu_type"], job["region"]))
                self._park_for_capacity(job, quiet=True)
                return
            self._fail(job, error)
            return
        if launch["status"] == "active":
            iid = launch["lambda_instance_id"]
            conn = self.orchestrator.connections.get(iid) if iid else None
            if iid and conn and conn.state == ConnectionState.CONNECTED:
                self._transition(job, "ready", instance_id=iid,
                                 detail="instance connected; ready to run")
        # else still booting/retrying: wait for the next tick

    def _auto_check_run_done(self, job: dict) -> None:
        """running -> syncing once the dispatched task settles."""
        task = self.queue.get(job["id"])
        if task and task["status"] in ("succeeded", "failed", "skipped"):
            self._transition(
                job, "syncing", instance_id=self._job_instance_id(job),
                detail=f"job {task['status']}; syncing outputs to persistent")

    async def _auto_sync(self, job: dict) -> None:
        """syncing -> terminating. Always sync ephemeral scratch first."""
        iid = self._job_instance_id(job)
        self.db.record_task_event(job["id"], "synced", instance_id=iid, detail="Syncing outputs")
        if iid:
            try:
                await self.orchestrator.sync_ephemeral(iid)
            except LaunchRejected as exc:
                # Sync could not run (no connection, rsync error). Record it and
                # still attempt the guarded terminate; the safety hook blocks
                # below if data is genuinely at risk.
                self.db.record_audit(
                    "backend", "auto_manage_sync_failed",
                    f"job {job['id']} instance {iid}: {exc.detail}")
        self._transition(job, "terminating", instance_id=iid,
                         detail="sync done; terminating (safety hook applies)")

    async def _auto_terminate(self, job: dict) -> None:
        """terminating -> done, via the guarded terminate. Never force."""
        iid = self._job_instance_id(job)
        launch = self.db.get_launch(job["launch_id"]) if job["launch_id"] else None
        if not iid or (launch and launch["status"] == "terminated"):
            # Already gone (user resolved a block, or reconcile closed it).
            self._transition(job, "done", instance_id=iid,
                             detail="instance terminated")
            return
        try:
            await self.orchestrator.terminate(iid, force=False)
            self._transition(job, "done", instance_id=iid,
                             detail="synced and terminated")
        except TerminationBlocked as exc:
            # The rescue could not save every file, and the data-safety policy
            # says data beats billing. Do NOT force. Surface it exactly like
            # the manual flow and leave the box up for review; the loop keeps
            # retrying force=False, so the moment the user resolves the files
            # (or terminates manually) the job completes on its own.
            msg = (f"termination blocked: {len(exc.files)} file(s) could not "
                   f"be saved; instance {iid} left running for review")
            if job.get("lifecycle_detail") != msg:
                self.db.set_task_lifecycle(job["id"], "terminating",
                                           detail=msg, stamp=False)
                self.db.record_audit(
                    "backend", "auto_manage_terminate_blocked",
                    f"job {job['id']} instance {iid}: {msg}")

    async def cancel_task(self, task_id: str) -> dict:
        """Cancel any job, in any pre-terminal state.

        Field gap: the old endpoint only cancelled auto-managed jobs, so a
        vllm-serve started from the Jobs page could not be stopped through
        Manifold at all (the distill guide's own serve-then-train flow needs
        exactly that). Routing:

        - auto-managed and not yet running -> cancel_auto_managed (tears
          down any box its lifecycle already launched, guarded);
        - queued -> finished as cancelled, nothing ever ran;
        - running -> stop the container on the instance; the normal
          completion funnel then settles it, labeled "cancelled by user".
          An auto-managed job's lifecycle sees the settle and proceeds to
          sync + terminate on its own.
        """
        task = self.queue.get(task_id)
        if task is None:
            raise LaunchRejected(404, f"task {task_id} not found")
        if task["auto_manage"] and task["status"] != "running":
            return await self.cancel_auto_managed(task_id)
        if task["status"] == "queued":
            self._finish_task(task_id, exit_code=-1, output_paths=[],
                              error="cancelled by user", notify=False)
            self.db.record_audit("backend", "task_cancelled",
                                 f"{task_id}: cancelled while queued")
            return {"cancelled": task_id}
        if task["status"] != "running":
            raise LaunchRejected(409, f"job is already {task['status']}")

        instance_id = task.get("instance_id") or ""
        conn = self.orchestrator.connections.get(instance_id)
        if conn is None or conn.state != ConnectionState.CONNECTED:
            raise LaunchRejected(
                409, f"no connection to {instance_id} to stop the job; if "
                     f"the instance is gone, the task will settle on its own")
        # Label FIRST so however the stop lands (rm -f, or the client dying
        # mid-pull), the completion funnel records a cancel, not a crash.
        self._cancel_requested.add(task_id)
        # `docker rm -f` covers a running container (SIGKILL + remove); the
        # pkill covers a job still in image-pull, where no container exists
        # yet and the docker CLIENT is the thing to stop. The [b]racket trick
        # keeps pkill from matching this very command line.
        stop_cmd = (
            f"docker rm -f manifold-task-{task_id} >/dev/null 2>&1; "
            f"pkill -f '[m]anifold-task-{task_id}' 2>/dev/null; true"
        )
        try:
            await conn.run(stop_cmd)
        except Exception as exc:
            self._cancel_requested.discard(task_id)
            raise LaunchRejected(
                502, f"could not stop the container on {instance_id}: {exc}")
        self.queue.append_log(task_id, "[manifold] stop requested by user")
        self.db.record_audit(
            "backend", "task_cancelled",
            f"{task_id} on {instance_id}: container stopped by user")
        return {"cancelled": task_id}

    async def cancel_auto_managed(self, task_id: str) -> dict:
        """Cancel an auto-managed job that has not started running.

        Allowed while queued/waiting/launching/ready. If a box was already
        launched, tear it down through the guarded path (nothing ran, so the
        hook passes; if it somehow blocks, surface rather than force). Running
        or tearing-down jobs are left to finish."""
        task = self.queue.get(task_id)
        if task is None:
            raise LaunchRejected(404, f"task {task_id} not found")
        if not task["auto_manage"]:
            raise LaunchRejected(400, f"task {task_id} is not auto-managed")
        lc = task["lifecycle"]
        if lc in ("done", "failed", "cancelled", "skipped"):
            raise LaunchRejected(409, f"job is already {lc}")
        if lc in ("running", "syncing", "terminating"):
            raise LaunchRejected(
                409, f"cannot cancel a job that is {lc}; let it finish or "
                f"terminate the instance from the instance card")
        iid = self._job_instance_id(task)
        if iid:
            try:
                await self.orchestrator.terminate(iid, force=False)
            except TerminationBlocked as exc:
                raise LaunchRejected(
                    409, f"instance {iid} has {len(exc.files)} unpersisted "
                    f"file(s); resolve them before cancelling")
        self.db.set_task_lifecycle(task_id, "cancelled",
                                   detail="cancelled by user")
        if task["status"] == "queued":
            # notify=False: the user is standing right there having just
            # clicked Cancel. Pinging them about it would be noise.
            self._finish_task(task_id, exit_code=-1, output_paths=[],
                              error="cancelled by user", notify=False)
        self.db.record_audit(
            "backend", "auto_manage_cancelled",
            f"job {task_id}" + (f" instance {iid}" if iid else "")
            + ": cancelled by user")
        return {"cancelled": task_id}

    # -- telemetry sampling loop -------------------------------------------------------

    async def _telemetry_loop(self) -> None:
        """Record one GPU telemetry sample per connected instance on a slow
        cadence, so a post-run utilization verdict and right-size hint can be
        computed from real data. Best-effort and fully off the launch path:
        a probe failure just skips that tick."""
        while True:
            await asyncio.sleep(self.settings.telemetry.sample_seconds)
            try:
                await self._sample_telemetry_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("telemetry loop iteration failed")
            try:
                self._check_budget_once()
            except Exception:
                logger.exception("budget check failed")

    def _check_budget_once(self) -> None:
        """Ping when month-to-date crosses a share of the monthly budget.

        Rides the telemetry cadence rather than a loop of its own: a budget
        is a slow number and this is a whole-table read. It runs here, not
        lazily inside GET /spend/summary, because a user with no dashboard
        open is exactly the one who needs telling.

        Advisory only. This never refuses a launch - see GuardrailPrefs for
        why a lower-bound number must not gate spend.
        """
        if self.prefs is None or self.notifier is None:
            return
        budget = self.prefs.get().guardrails.monthly_budget_usd
        if budget <= 0:
            return                              # no wallet set: nothing to cross

        from . import spend
        summary = spend.summarize(
            self.db.list_launches(), now_iso=utcnow(),
            monthly_budget_usd=budget,
        )
        status = summary["budget"]
        used = (status["used_pct"] or 0.0) / 100.0
        crossed = [t for t in spend.BUDGET_THRESHOLDS if used >= t]
        if not crossed:
            return
        highest = max(crossed)

        month = utcnow()[:7]                 # "YYYY-MM"
        if self._budget_announced == (month, highest):
            return                              # already said this, this month
        self._budget_announced = (month, highest)

        spent = status["month_to_date_usd"]
        if highest >= 1.0:
            title = f"Monthly budget spent: ${spent:.2f} of ${budget:.2f}"
        else:
            title = (f"{int(highest * 100)}% of the monthly budget: "
                     f"${spent:.2f} of ${budget:.2f}")
        body = ("Manifold does not block launches on this figure, and it "
                "only counts instances Manifold started.")
        if status["exhausted_on"]:
            body = (f"At the current burn it runs out on "
                    f"{status['exhausted_on'][:10]}. " + body)
        self.notifier.notify("budget_threshold", title, body, ref=month)

    @staticmethod
    def _reported(gpus: list[dict], key: str) -> list[float]:
        """Every GPU's value for `key`, skipping the ones that did not
        report it.

        ABSENT IS NOT ZERO, and that is why this exists rather than an inline
        `.get(key, 0)`. A sidecar is frozen into an instance at launch, so a
        box running since before a field existed simply omits it; defaulting
        that to 0 would record "the GPUs were doing nothing" for a box we
        know nothing about, and idle-spend accounting would then bill its
        whole lifetime as idle. An empty list becomes a NULL column, which
        every reader treats as "not measured".
        """
        out = []
        for gpu in gpus:
            raw = gpu.get(key)
            if raw is None:
                continue
            try:
                out.append(float(raw))
            except (TypeError, ValueError):
                continue
        return out

    async def _sample_telemetry_once(self) -> None:
        for instance_id, conn in list(self.orchestrator.connections.items()):
            if conn.state != ConnectionState.CONNECTED:
                continue
            # Detached commands first (Phase 95): work started through
            # Manifold asserts its own liveness. One probe covers every
            # open handle on the box; a live pid is evidence of work, and
            # evidence - not an agent's memory of having started something -
            # is what keeps the idle sweep's hands off a busy box.
            try:
                await self._probe_detached(instance_id, conn)
            except Exception:   # noqa: BLE001 - a probe failure must never
                # break telemetry sampling; the sweep just sees stale (i.e.
                # no) confirmation and judges by its other signals.
                logger.exception("detached probe failed for %s", instance_id)
            gpus = None
            sidecar = self.orchestrator.sidecar_for(instance_id)
            if sidecar is not None:
                try:
                    metrics = await sidecar.metrics()
                    gpus = (metrics.get("gpus")
                            if metrics.get("available") else None)
                    if metrics.get("active_ide_processes"):
                        self.touch_activity(instance_id)
                except Exception:
                    gpus = None   # sidecar not up yet / not installed
            if not gpus:
                # Externally-launched boxes have no sidecar (our cloud-init
                # never ran there): nvidia-smi over the managed connection.
                payload = await self.orchestrator.gpu_metrics_via_ssh(
                    instance_id)
                gpus = (payload or {}).get("gpus")
            if not gpus:
                continue

            # ONE ROW PER BOX, NOT PER CARD. This used to record gpus[0] and
            # nothing else, which made peak VRAM a GPU-0 figure on a
            # multi-GPU instance - so a run that filled GPU 3 looked like it
            # had room to spare, and the right-size hint could tell you to
            # downsize into an OOM. MAX across the cards is the OOM-relevant
            # number and tightens the hint, which is the safe direction.
            used = self._reported(gpus, "vram_used_mib")
            total = self._reported(gpus, "vram_total_mib")
            util = self._reported(gpus, "utilization_pct")
            self.db.record_telemetry_sample(
                instance_id,
                gpu_name=gpus[0].get("name", ""),
                vram_used_mib=int(max(used)) if used else None,
                vram_total_mib=int(max(total)) if total else None,
                util_pct=int(max(util)) if util else None,
                # The mean is what idle-spend accounting reads: with the max,
                # one busy GPU out of eight would hide seven idle ones and
                # idle spend would be under-reported. See spend.idle_spend.
                util_pct_mean=(sum(util) / len(util)) if util else None,
                gpu_count=len(gpus),
            )
            self._maybe_notify_idle(instance_id)

    def _maybe_notify_idle(self, instance_id: str) -> None:
        """Ping ONCE when an instance has been idle long enough to matter, in
        money. Never raises, and never terminates anything.

        This is the point of idle-spend accounting: an instance nobody
        noticed is exactly the one still billing. It reports and stops there
        - the phase rule is that GPU utilization may report but must never
        gate a destructive decision, so this raises a notification and leaves
        the decision with the person reading it.

        Deduped on (kind, ref) in the notifications table rather than in
        memory, so neither the next telemetry tick nor a backend restart can
        re-ping a condition you have already seen.
        """
        if self.notifier is None:
            return
        # Imported here rather than at module scope: this is the only use of
        # the accounting module in the dispatcher, and a report-only helper
        # has no business appearing among the dispatch path's dependencies.
        from . import spend
        try:
            # notify_once() would catch this anyway; checking first is what
            # keeps an already-pinged instance from re-loading its whole
            # sample window on every tick for the rest of its life.
            if self.db.notification_exists("instance_idle",
                                           f"idle:{instance_id}"):
                return
            policy = self.settings.idle_spend
            launch = self.db.find_launch_by_instance(instance_id)
            if launch is None:
                return
            now = utcnow()
            window = spend.idle_window(launch, now_iso=now)
            if window is None:
                return
            report = spend.idle_spend(
                launch,
                self.db.telemetry_samples_between(
                    instance_id, window["start_iso"], window["end_iso"]),
                now_iso=now, util_pct=policy.util_pct,
                sample_interval_seconds=self.settings.telemetry.sample_seconds,
                min_window_seconds=policy.min_window_seconds,
            )
            idle_seconds, idle_usd = report["idle_seconds"], report["idle_usd"]
            # BOTH gates, and an unknown cost fails them: "idle for a while,
            # value unknown" is not worth interrupting someone for, and the
            # message would have no number in it.
            if idle_seconds is None or idle_usd is None:
                return
            if (idle_seconds < policy.notify_after_seconds
                    or idle_usd < policy.notify_usd):
                return
            gpu = (launch.get("launched_type") or launch.get("requested_type")
                   or "This instance")
            self.notifier.notify_once(
                "instance_idle",
                f"{gpu} has been idle for {round(idle_seconds / 60)} minutes",
                f"It has stayed at or below {policy.util_pct:g}% average GPU "
                f"utilization for {round(idle_seconds / 60)} minutes, which "
                f"is ${idle_usd:.2f} of idle spend so far. That can be "
                f"normal for a served model between requests. If it is not, "
                f"give it work or terminate it.",
                ref=f"idle:{instance_id}",
            )
        except Exception:   # noqa: BLE001 - reporting must never break sampling
            logger.exception("idle-spend notification check failed for %s",
                             instance_id)

    # -- adoption sweep ----------------------------------------------------------------

    async def _adopt_loop(self) -> None:
        # An instance launched outside Manifold (Lambda console, raw API
        # script) used to get a managed connection only at backend startup,
        # leaving Files/chat/jobs dead for it until a restart. This sweep
        # connects to any active-but-untracked instance within a poll
        # interval. adopt_running_instances skips ids it already tracks,
        # so the steady-state cost is one list_instances call per tick.
        while True:
            await asyncio.sleep(self.settings.launch.adopt_poll_seconds)
            try:
                await self.orchestrator.adopt_running_instances(startup=False)
                self._protect_external_instances()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("adoption sweep iteration failed")

    # -- capacity watch loop ----------------------------------------------------------

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.watches.poll_seconds)
            try:
                await self._check_watches()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("watch loop iteration failed")

    async def _check_watches(self) -> None:
        watches = self.db.active_watches()
        if not watches:
            return
        types = await self.client.list_instance_types()
        now = utcnow()
        for watch in watches:
            self.db.update_watch(watch["id"], last_checked=now)
            info = types.get(watch["instance_type"])
            if info is None or watch["region"] not in info.regions_with_capacity:
                continue
            # Capacity found.
            self.db.update_watch(watch["id"], status="available", triggered_at=now)
            self.db.record_audit(
                "backend", "capacity_available",
                f"{watch['instance_type']} in {watch['region']} (watch {watch['id']})",
            )
            # A watch WITHOUT auto-launch is only this notification; it was
            # silent before (found in field QA: the hook was never wired).
            if self.notifier:
                self.notifier.notify(
                    "capacity_available",
                    f"{watch['instance_type']} available in {watch['region']}",
                    "Capacity watch matched. "
                    + ("Auto-launching through the guarded pipeline."
                       if watch["auto_launch"]
                       and self.settings.watches.auto_launch_enabled
                       and watch["filesystem"]
                       else "Launch it from the dashboard while it lasts."),
                    ref=f"watch:{watch['id']}",
                )
            if self.on_capacity_available:
                try:
                    self.on_capacity_available(watch)
                except Exception:
                    logger.exception("capacity notification hook failed")
            if (
                watch["auto_launch"]
                and self.settings.watches.auto_launch_enabled
                and watch["filesystem"]
            ):
                try:
                    # Straight through the guarded pipeline: budget,
                    # concurrency, and region-match all still apply.
                    await self.orchestrator.request_launch(
                        instance_type=watch["instance_type"],
                        region=watch["region"],
                        filesystem=watch["filesystem"],
                        # Phase 79: the watch fires, but the launch belongs
                        # to whoever set the watch.
                        created_by=watch.get("created_by"),
                    )
                    self.db.update_watch(watch["id"], status="launched")
                    self.db.record_audit(
                        "backend", "watch_auto_launch",
                        f"watch {watch['id']}: launched {watch['instance_type']}",
                    )
                except LaunchRejected as exc:
                    # Guards said no. The watch stays "available" so the
                    # user sees capacity exists and why we did not launch.
                    self.db.record_audit(
                        "backend", "watch_auto_launch_rejected",
                        f"watch {watch['id']}: {exc.detail}",
                    )
