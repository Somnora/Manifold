"""Backend restart mid-job: running tasks are re-adopted, not orphaned.

Found live (2026-07-16): a --reload restart killed the SSH stream; the
container kept running but the task sat 'running' forever with frozen logs.
The wrap now persists the exit code to task-logs/<id>.exit and the
dispatcher re-adopts running tasks at startup by polling for it.
"""

import pytest

from app.dispatcher import Dispatcher, wrap_remote_command
from app.lambda_api import MockLambdaClient
from app.orchestrator import Orchestrator
from app.task_queue import SQLiteTaskQueue
from tests.conftest import make_settings, mock_connect_fn


def test_wrap_persists_the_exit_code():
    wrapped = wrap_remote_command(
        "docker run --rm x", "/lambda/nfs/fs/task-logs/abc.log",
        ensure_dirs=["/workspace/ephemeral"], require_mount="fs")
    assert "/lambda/nfs/fs/task-logs/abc.exit" in wrapped
    assert "nohup bash -c" in wrapped          # container detached from session
    assert "exit $rc" in wrapped               # code still propagates


class ScriptedConn:
    """Answers run() from a script; last entry repeats."""

    def __init__(self, script):
        self.script = list(script)
        from app.connections import ConnectionState
        self.state = ConnectionState.CONNECTED

    async def run(self, command, **kwargs):
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return item


@pytest.fixture
def rig(tmp_path, db):
    settings = make_settings(tmp_path)
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    queue = SQLiteTaskQueue(db)
    d = Dispatcher(settings, orch, queue, {}, db, MockLambdaClient())
    return d, orch, queue, db


def plant_running_task(queue, db, instance_id="i-test"):
    task_id = queue.enqueue(template="gpu-smoke", parameters={})
    queue.mark_running(task_id, instance_id)
    launch_id = db.create_launch(requested_type="gpu_1x_a10",
                                 region="us-east-1",
                                 filesystem="manifold-data",
                                 connection_mode="direct-ssh",
                                 hourly_rate_cents=129)
    db.update_launch(launch_id, status="active",
                     lambda_instance_id=instance_id)
    return task_id


async def test_readopt_finishes_with_persisted_exit_code(rig):
    d, orch, queue, db = rig
    task_id = plant_running_task(queue, db)
    orch.connections["i-test"] = ScriptedConn([(0, "0\n", "")])
    await d._readopt_running_tasks()
    task = queue.get(task_id)
    assert task["status"] == "succeeded"
    assert task["exit_code"] == 0
    log = " ".join(r["line"] for r in queue.get_logs(task_id))
    assert "backend restarted; reattached" in log


async def test_readopt_gone_container_fails_honestly(rig):
    d, orch, queue, db = rig
    task_id = plant_running_task(queue, db)
    orch.connections["i-test"] = ScriptedConn([(0, "gone\n", "")])
    await d._readopt_running_tasks()
    task = queue.get(task_id)
    assert task["status"] == "failed"
    assert "result unknown" in task["error"]


async def test_readopt_nonzero_exit_is_a_failure(rig):
    d, orch, queue, db = rig
    task_id = plant_running_task(queue, db)
    orch.connections["i-test"] = ScriptedConn([(0, "137\n", "")])
    await d._readopt_running_tasks()
    task = queue.get(task_id)
    assert task["status"] == "failed"
    assert task["exit_code"] == 137


async def test_readopt_ignores_finished_tasks(rig):
    d, orch, queue, db = rig
    task_id = queue.enqueue(template="gpu-smoke", parameters={})
    queue.mark_running(task_id, "i-test")
    queue.mark_finished(task_id, exit_code=0, output_paths=[])
    await d._readopt_running_tasks()   # no running tasks: returns instantly
    assert queue.get(task_id)["status"] == "succeeded"


class RecordingConn(ScriptedConn):
    def __init__(self, script):
        super().__init__(script)
        self.commands = []

    async def run(self, command, **kwargs):
        self.commands.append(command)
        return await super().run(command, **kwargs)


async def test_readopt_probe_requires_a_nonempty_exit_file(rig):
    """The wrapper's `echo $? > file` creates-then-writes; a bare `cat`
    racing that write succeeded with EMPTY output, which read as "gone"
    and failed a task whose container had just finished fine. The probe
    must demand a non-empty file (falling through to docker inspect)."""
    d, orch, queue, db = rig
    task_id = plant_running_task(queue, db)
    conn = RecordingConn([(0, "0\n", "")])
    orch.connections["i-test"] = conn
    await d._readopt_running_tasks()
    assert queue.get(task_id)["status"] == "succeeded"
    assert "[ -s " in conn.commands[0]


async def test_connection_loss_hands_off_to_the_exit_file_poller(
        rig, monkeypatch):
    """A transient SSH drop mid-stream must NOT fail the task: the container
    survives the session by design (nohup + exit file), so the dispatcher
    re-adopts it exactly like a backend restart and settles it with the
    container's real result."""
    from pathlib import Path

    from app.templates import load_templates

    d, orch, queue, db = rig
    d.templates, _ = load_templates(
        Path(__file__).resolve().parent.parent.parent / "templates")
    task_id = plant_running_task(queue, db)
    task = queue.get(task_id)
    d._gpu_ready.add("i-test")                       # skip the CUDA probe

    async def no_preflight(template):
        return None
    monkeypatch.setattr(d, "_image_preflight", no_preflight)

    async def dead_stream(conn, command, tid):
        raise ConnectionError("session dropped")
    monkeypatch.setattr(d, "_stream_run", dead_stream)

    # The poller answers immediately with the container's persisted exit 0.
    conn = RecordingConn([(0, "0\n", "")])
    orch.connections["i-test"] = conn
    await d._run_task(task, "i-test", conn)

    settled = queue.get(task_id)
    assert settled["status"] == "succeeded"          # NOT "failed"
    assert settled["exit_code"] == 0
    log = " ".join(r["line"] for r in queue.get_logs(task_id))
    assert "connection lost; reattached" in log
