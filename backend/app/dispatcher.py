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
import shlex
import time

from .config import Settings
from .connections import ConnectionState, ManagedConnection
from .db import Database, utcnow
from .image_checker import ImageChecker
from .lambda_api import LambdaClient
from .model_client import ModelClientError
from .orchestrator import LaunchRejected, Orchestrator, TerminationBlocked
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
GPU_PROBE_COMMAND = (
    "nvidia-smi -q && "
    "{ ! command -v nvidia-container-cli >/dev/null || nvidia-container-cli info; }"
)

# Container stderr signatures of that same race, for the last-resort retry.
CUDA_RACE_SIGNATURES = (
    "No CUDA GPUs are available",
    "could not select device driver",
    "nvidia-container-cli: initialization error",
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
        return False, "nvidia-smi not answering yet (driver still coming up)"
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
        self._clock = clock
        self._loops: list[asyncio.Task] = []
        # In-flight dispatched jobs: task id -> the asyncio task running it.
        # Guards the queued->running gap against double-dispatch and lets
        # stop() cancel work in progress.
        self._dispatching: dict[str, asyncio.Task] = {}
        # Terminal activity is reported by the terminal WS handler (Phase 5);
        # jobs update it too, so "idle" means neither jobs nor shells.
        self.last_activity: dict[str, float] = {}
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
        if task_id in self._cancel_requested:
            # The user asked for this stop: label it so the record says
            # "cancelled by user", not a baffling "container exited 137",
            # and skip the failure ping (they are standing right there).
            self._cancel_requested.discard(task_id)
            error = "cancelled by user"
            notify = False
        self.queue.mark_finished(task_id, exit_code=exit_code,
                                 output_paths=output_paths, error=error)
        self._worklog_task(task_id)
        if not notify or self.notifier is None:
            return
        task = self.queue.get(task_id) or {}
        name = task.get("template", "job")
        succeeded = task.get("status") == "succeeded"
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
            self.notifier.notify(
                "job_failed", f"Job failed: {name}",
                f"{task_id}{where}\n{(error or f'exit {exit_code}')[:200]}",
                ref=task_id,
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
        self.db.record_audit(
            "dashboard", "keep_alive",
            f"{instance_id} idle auto-termination {'off' if enabled else 'on'}",
        )
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
        from the last job/terminal activity (0 if none recorded yet)."""
        last = self.last_activity.get(instance_id)
        idle = max(0.0, self._clock() - last) if last is not None else 0.0
        return {
            "idle_seconds": round(idle),
            "timeout_seconds": round(self._effective_timeout(instance_id)),
            "keep_alive": self.keep_alive_enabled(instance_id),
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
                error=f"no filesystem recorded for instance {instance_id}",
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
        """Terminate instances idle past the timeout, via the STANDARD flow.

        Idle = connected, no running task, and no activity (job or terminal)
        for idle.timeout_seconds. The clock starts when the connection comes
        up (we seed last_activity then), so a freshly booted instance gets a
        full quiet period before it is eligible.
        """
        now = self._clock()
        # Instances an auto-managed job owns are governed by that job's
        # lifecycle, which owns teardown (sync -> terminate). The idle loop
        # must not race it: skip them entirely, keep-alive or not. If the
        # lifecycle is ever lost (its job reached a terminal state), the
        # instance drops out of this set and the idle loop resumes as backstop.
        auto_owned = self.db.auto_managed_instance_ids()
        # A running task pins ITS OWN instance only (Phase 35): with several
        # GPUs up, a job on box A must not keep an idle box B billing.
        pinned = {t["instance_id"] for t in self.db.running_tasks()
                  if t.get("instance_id")}
        for instance_id, conn in list(self.orchestrator.connections.items()):
            if instance_id in auto_owned or instance_id in pinned:
                continue
            if conn.state != ConnectionState.CONNECTED:
                # Not reachable: don't count unreachable time as idle.
                self.last_activity.pop(instance_id, None)
                continue
            if self.keep_alive_enabled(instance_id):
                continue
            timeout = self._effective_timeout(instance_id)
            last = self.last_activity.setdefault(instance_id, now)
            if now - last < timeout:
                continue
            logger.info("instance %s idle for %.0fs; requesting termination",
                        instance_id, now - last)
            self.db.record_audit(
                "backend", "idle_termination",
                f"{instance_id} idle {now - last:.0f}s (limit {timeout:.0f}s)",
            )
            try:
                # force=False: terminate() rescues the instance's data first
                # (sync to the persistent volume and/or download here, per the
                # data-safety policy) and only refuses if something could NOT
                # be saved. No sync-then-force dance here any more — that lived
                # in this loop when terminate() did not rescue, and it meant
                # every OTHER caller had to reimplement it.
                await self.orchestrator.terminate(instance_id, force=False)
                self.last_activity.pop(instance_id, None)
            except TerminationBlocked as exc:
                # The rescue could not save everything. Leave the box up with
                # the data intact rather than destroying it; the orchestrator
                # has already pinged the user. Retried next cycle.
                logger.warning(
                    "idle termination of %s refused: %d file(s) unsaveable",
                    instance_id, len(exc.files))
                self.db.record_audit(
                    "backend", "idle_termination_blocked",
                    f"{instance_id}: {len(exc.files)} file(s) could not be "
                    f"saved; instance left running",
                )

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
            job = self.db.next_pending_auto_managed_task()
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
                filesystem=job["filesystem"])
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
        if task and task["status"] in ("succeeded", "failed"):
            self._transition(
                job, "syncing", instance_id=self._job_instance_id(job),
                detail=f"job {task['status']}; syncing outputs to persistent")

    async def _auto_sync(self, job: dict) -> None:
        """syncing -> terminating. Always sync ephemeral scratch first."""
        iid = self._job_instance_id(job)
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
        if lc in ("done", "failed", "cancelled"):
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

    async def _sample_telemetry_once(self) -> None:
        for instance_id, conn in list(self.orchestrator.connections.items()):
            if conn.state != ConnectionState.CONNECTED:
                continue
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
            g = gpus[0]
            self.db.record_telemetry_sample(
                instance_id,
                gpu_name=g.get("name", ""),
                vram_used_mib=int(g.get("vram_used_mib", 0)),
                vram_total_mib=int(g.get("vram_total_mib", 0)),
                util_pct=int(g.get("utilization_pct", 0)),
            )

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
