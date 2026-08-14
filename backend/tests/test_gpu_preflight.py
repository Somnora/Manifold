"""First-job GPU preflight: don't dispatch onto a GPU that can't run CUDA yet.

Field case (sprite-to-3d test pass): an A100 SXM4 job dispatched 2.5 minutes
after cloud-init finished died with "No CUDA GPUs are available" and burned
~5 billed minutes - nvidia-fabricmanager was still initializing, while
nvidia-smi looked perfectly healthy to every hand-check. The dispatcher now
probes `nvidia-smi -q` before the FIRST job on each instance and waits for
the fabric state to settle (bounded, fail-open).
"""

import asyncio
from pathlib import Path

import pytest

from app.dispatcher import (
    CUDA_RACE_SIGNATURES,
    Dispatcher,
    GPU_PROBE_COMMAND,
    gpu_readiness,
)
from app.lambda_api import MockLambdaClient
from app.orchestrator import Orchestrator
from app.task_queue import SQLiteTaskQueue
from tests.conftest import make_settings, mock_connect_fn


# A trimmed real-world `nvidia-smi -q` fabric section, both phases.
SXM_BOOTING = """\
==============NVSMI LOG==============
Attached GPUs                             : 1
GPU 00000000:07:00.0
    Product Name                          : NVIDIA A100-SXM4-40GB
    Persistence Mode                      : Enabled
    Fabric
        State                             : In Progress
        Status                            : N/A
    Performance State                     : P0
"""

SXM_READY = SXM_BOOTING.replace("In Progress", "Completed")

PCIE_BOX = """\
==============NVSMI LOG==============
Attached GPUs                             : 1
GPU 00000000:06:00.0
    Product Name                          : NVIDIA A10
    Persistence Mode                      : Enabled
    Performance State                     : P8
"""


# -- pure parsing ----------------------------------------------------------------

def test_probe_failure_means_not_ready():
    ready, reason = gpu_readiness(127, "nvidia-smi: command not found")
    assert not ready
    assert "driver" in reason


def test_fabric_in_progress_means_not_ready():
    ready, reason = gpu_readiness(0, SXM_BOOTING)
    assert not ready
    assert "fabric manager" in reason


def test_fabric_completed_means_ready():
    ready, reason = gpu_readiness(0, SXM_READY)
    assert ready
    assert "completed" in reason


def test_pcie_box_without_fabric_is_ready():
    ready, reason = gpu_readiness(0, PCIE_BOX)
    assert ready


def test_mock_shell_output_is_ready():
    # The mock connection answers every command with this shape; existing
    # dispatch tests must sail through the preflight without waiting.
    ready, _ = gpu_readiness(0, f"mock output of: {GPU_PROBE_COMMAND}")
    assert ready


# -- the dispatcher gate ---------------------------------------------------------

class ScriptedConn:
    """Duck-types the one ManagedConnection method the preflight uses.
    Answers run() from a script of (exit_code, stdout) pairs; the last
    entry repeats forever."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def run(self, command, **kwargs):
        self.calls += 1
        exit_code, stdout = (
            self.script.pop(0) if len(self.script) > 1 else self.script[0])
        return exit_code, stdout, ""


@pytest.fixture
def dispatcher(tmp_path, db):
    from app.config import TaskSettings
    # Instant polling so a wait loop runs in milliseconds.
    settings = make_settings(tmp_path, tasks=TaskSettings(
        poll_seconds=0.02,
        gpu_ready_poll_seconds=0.001,
        gpu_ready_timeout_seconds=0.05,
    ))
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    queue = SQLiteTaskQueue(db)
    d = Dispatcher(settings, orch, queue, {}, db, MockLambdaClient())
    return d


def task_log(dispatcher, task_id):
    return [row["line"] for row in dispatcher.queue.get_logs(task_id)]


async def test_gate_waits_until_fabric_settles(dispatcher):
    task_id = dispatcher.queue.enqueue(template="gpu-smoke", parameters={})
    conn = ScriptedConn([(0, SXM_BOOTING), (0, SXM_BOOTING), (0, SXM_READY)])
    await dispatcher._ensure_gpu_ready(conn, "i-sxm", task_id)
    assert conn.calls == 3
    log = task_log(dispatcher, task_id)
    assert any("waiting for the GPU" in ln for ln in log)
    assert any("GPU ready" in ln for ln in log)
    assert "i-sxm" in dispatcher._gpu_ready


async def test_gate_skipped_for_later_jobs(dispatcher):
    task_id = dispatcher.queue.enqueue(template="gpu-smoke", parameters={})
    conn = ScriptedConn([(0, SXM_READY)])
    await dispatcher._ensure_gpu_ready(conn, "i-sxm", task_id)
    assert conn.calls == 1
    await dispatcher._ensure_gpu_ready(conn, "i-sxm", task_id)
    assert conn.calls == 1                    # cached; no second probe
    # A ready box logs nothing: silence when there is nothing to say.
    assert task_log(dispatcher, task_id) == []


async def test_gate_fails_open_after_timeout(dispatcher):
    task_id = dispatcher.queue.enqueue(template="gpu-smoke", parameters={})
    conn = ScriptedConn([(0, SXM_BOOTING)])   # never settles
    await dispatcher._ensure_gpu_ready(conn, "i-stuck", task_id)
    log = task_log(dispatcher, task_id)
    assert any("dispatching anyway" in ln for ln in log)
    assert "i-stuck" in dispatcher._gpu_ready  # later jobs not re-gated


def test_probe_also_checks_the_container_runtime():
    """Field pass round 2: host nvidia-smi was fine but the container
    toolkit wasn't serving GPUs yet. The probe covers both, guarded so a
    box without nvidia-container-cli stays fail-open."""
    assert "nvidia-smi -q" in GPU_PROBE_COMMAND
    assert "nvidia-container-cli info" in GPU_PROBE_COMMAND
    assert "command -v nvidia-container-cli" in GPU_PROBE_COMMAND


def test_probe_walks_through_the_container_door_itself():
    """Field pass round 3 (2026-08-14 mini-gate): BOTH host-side stages read
    healthy while a job dispatched at connect died with "out of bounds for
    0 devices". The only probe that cannot be fooled is the path the job
    takes, so the last stage runs a real container with --gpus all - guarded
    by command -v docker, same fail-open rule as the toolkit stage."""
    assert "docker run --rm --gpus all" in GPU_PROBE_COMMAND
    assert "command -v docker" in GPU_PROBE_COMMAND
    # Ordered: cheap host checks first, the container pull last, so the
    # image fetch only happens once the driver is actually answering.
    assert GPU_PROBE_COMMAND.index("nvidia-smi -q") \
        < GPU_PROBE_COMMAND.index("docker run")


def test_the_mini_gate_signature_now_triggers_the_retry():
    """The exact torch wording the 2026-08-14 mini-gate died with. Every
    signature already on the list missed it, so the job failed instead of
    retrying after the runtime settled."""
    from app.dispatcher import CUDA_RACE_SIGNATURES

    line = "DP adjusted local rank 0 is out of bounds for 0 devices"
    assert any(sig in line for sig in CUDA_RACE_SIGNATURES)


def test_a_container_stage_failure_reads_as_not_ready():
    """Exit != 0 from ANY stage is 'not ready', and the reason must not
    blame nvidia-smi when docker may be the one still coming up."""
    ready, reason = gpu_readiness(125, "")
    assert not ready
    assert "docker" in reason and "toolkit" in reason


class RunTaskConn:
    """Enough of ManagedConnection for _run_task's fallback path."""

    def __init__(self, script):
        self.script = list(script)
        from app.connections import ConnectionState
        self.state = ConnectionState.CONNECTED

    def ssh_connection(self):
        return object()          # truthy, no create_process -> run() fallback

    async def run(self, command, **kwargs):
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


async def test_cuda_race_failure_retries_once_and_succeeds(tmp_path, db):
    """A container that dies with 'No CUDA GPUs are available' is retried
    once after re-checking readiness, instead of failing the job (the boot
    race a solo user's first job hit in the field)."""
    from app.config import TaskSettings
    from app.lambda_api import MockLambdaClient
    from app.orchestrator import Orchestrator
    from app.task_queue import SQLiteTaskQueue
    from app.templates import load_templates
    from tests.conftest import make_settings, mock_connect_fn

    templates, _ = load_templates(
        Path(__file__).resolve().parent.parent.parent / "templates")
    settings = make_settings(tmp_path, tasks=TaskSettings(
        poll_seconds=0.02, gpu_ready_poll_seconds=0.001,
        gpu_ready_timeout_seconds=0.05))
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    queue = SQLiteTaskQueue(db)
    d = Dispatcher(settings, orch, queue, templates, db, MockLambdaClient())

    launch_id = db.create_launch(requested_type="gpu_1x_a10",
                                 region="us-east-1",
                                 filesystem="manifold-data",
                                 connection_mode="direct-ssh",
                                 hourly_rate_cents=129)
    db.update_launch(launch_id, status="active", lambda_instance_id="i-race")
    task_id = queue.enqueue(template="gpu-smoke", parameters={})
    task = queue.get(task_id)

    conn = RunTaskConn([
        (0, PCIE_BOX, ""),                                   # preflight ok
        (1, "RuntimeError: No CUDA GPUs are available", ""),  # race failure
        (0, PCIE_BOX, ""),                                   # re-check ok
        (0, "smoke ok", ""),                                 # retry succeeds
    ])
    real_sleep = asyncio.sleep
    import app.dispatcher as disp_mod
    orig = disp_mod.asyncio.sleep
    disp_mod.asyncio.sleep = lambda s: real_sleep(0)   # skip the 20s wait
    try:
        await d._run_task(task, "i-race", conn)
    finally:
        disp_mod.asyncio.sleep = orig

    done = queue.get(task_id)
    assert done["status"] == "succeeded"
    assert done["exit_code"] == 0
    log = " ".join(r["line"] for r in queue.get_logs(task_id))
    assert "retrying once" in log


async def test_non_cuda_failures_are_not_retried(tmp_path, db):
    from app.config import TaskSettings
    from app.lambda_api import MockLambdaClient
    from app.orchestrator import Orchestrator
    from app.task_queue import SQLiteTaskQueue
    from app.templates import load_templates
    from tests.conftest import make_settings, mock_connect_fn

    templates, _ = load_templates(
        Path(__file__).resolve().parent.parent.parent / "templates")
    settings = make_settings(tmp_path, tasks=TaskSettings(
        poll_seconds=0.02, gpu_ready_poll_seconds=0.001,
        gpu_ready_timeout_seconds=0.05))
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    queue = SQLiteTaskQueue(db)
    d = Dispatcher(settings, orch, queue, templates, db, MockLambdaClient())
    launch_id = db.create_launch(requested_type="gpu_1x_a10",
                                 region="us-east-1",
                                 filesystem="manifold-data",
                                 connection_mode="direct-ssh",
                                 hourly_rate_cents=129)
    db.update_launch(launch_id, status="active", lambda_instance_id="i-fail")
    task_id = queue.enqueue(template="gpu-smoke", parameters={})
    conn = RunTaskConn([
        (0, PCIE_BOX, ""),                      # preflight ok
        (7, "some ordinary crash", ""),         # real failure: no retry
    ])
    await d._run_task(queue.get(task_id), "i-fail", conn)
    done = queue.get(task_id)
    assert done["status"] == "failed"
    assert done["exit_code"] == 7
    assert "retrying once" not in " ".join(
        r["line"] for r in queue.get_logs(task_id))


async def test_gate_fails_open_on_probe_error(dispatcher):
    task_id = dispatcher.queue.enqueue(template="gpu-smoke", parameters={})

    class BrokenConn:
        async def run(self, command, **kwargs):
            raise ConnectionError("no SSH connection")

    await dispatcher._ensure_gpu_ready(BrokenConn(), "i-down", task_id)
    log = task_log(dispatcher, task_id)
    assert any("preflight skipped" in ln for ln in log)
    assert "i-down" in dispatcher._gpu_ready
