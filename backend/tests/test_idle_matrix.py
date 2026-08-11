"""GOLDEN: the idle loop's decision matrix, pinned exactly as it shipped.

This file is a fence, not a feature. `_check_idle` is the only unattended
code in Manifold that destroys a paid instance, and Phase 76b refactors it
and bolts a lifetime ceiling onto it. Every row below was written and made
green against the PRE-76b `_check_idle` and must keep passing, unedited,
through that refactor. A row that needs a change is not a test problem — it
is a behaviour change nobody asked for.

Read the table as: given this instance's state, does the idle loop destroy
it or leave it alone?
"""

from pathlib import Path

import pytest

from app.config import IdleSettings
from app.connections import ConnectionState
from app.dispatcher import Dispatcher
from app.lambda_api import MockLambdaClient
from app.orchestrator import Orchestrator, TerminationBlocked
from app.task_queue import SQLiteTaskQueue
from app.templates import load_templates
from tests.conftest import make_settings, mock_connect_fn

TIMEOUT = 1800.0
NOW = 10_000.0            # fake monotonic "now"; the loop reads _clock()


class FakeConn:
    """The one thing the idle loop asks a ManagedConnection: its state."""

    def __init__(self, state=ConnectionState.CONNECTED):
        self.state = state


class Harness:
    """A dispatcher whose only destructive call is recorded, not performed."""

    def __init__(self, tmp_path, db):
        templates, _ = load_templates(
            Path(__file__).resolve().parent.parent.parent / "templates")
        settings = make_settings(
            tmp_path, idle=IdleSettings(timeout_seconds=TIMEOUT,
                                        poll_seconds=15.0))
        self.db = db
        self.queue = SQLiteTaskQueue(db)
        self.orch = Orchestrator(settings, MockLambdaClient(), db,
                                 connect_fn=mock_connect_fn)
        self.dispatcher = Dispatcher(settings, self.orch, self.queue,
                                     templates, db, MockLambdaClient(),
                                     clock=lambda: NOW)
        self.terminated: list[tuple[str, dict]] = []
        self.raises: Exception | None = None

        async def _terminate(instance_id, **kwargs):
            self.terminated.append((instance_id, kwargs))
            if self.raises is not None:
                raise self.raises
            self.orch.connections.pop(instance_id, None)
            return {"instance_id": instance_id, "terminated": True}

        self.orch.terminate = _terminate

    def add_instance(self, instance_id, *, connected=True, launch=True,
                     keep_alive=False, idle_for=TIMEOUT + 60) -> str | None:
        self.orch.connections[instance_id] = FakeConn(
            ConnectionState.CONNECTED if connected
            else ConnectionState.RECONNECTING)
        self.dispatcher.last_activity[instance_id] = NOW - idle_for
        if not launch:
            return None
        launch_id = self.db.create_launch(
            requested_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data", connection_mode="direct-ssh",
            hourly_rate_cents=129)
        self.db.update_launch(launch_id, status="active",
                              lambda_instance_id=instance_id,
                              keep_alive=1 if keep_alive else 0)
        return launch_id

    def pin_task(self, instance_id, template) -> str:
        """A RUNNING job on this instance (server or batch)."""
        task_id = self.queue.enqueue(template=template, parameters={})
        self.queue.mark_running(task_id, instance_id)
        return task_id

    def own_auto(self, instance_id, launch_id, lifecycle) -> str:
        """An auto-managed job that owns this instance's teardown."""
        task_id = self.db.create_task(
            template="whisper-batch", parameters={}, auto_manage=True,
            gpu_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data")
        self.db.set_task_lifecycle(task_id, lifecycle, launch_id=launch_id)
        return task_id


@pytest.fixture
def harness(tmp_path, db):
    return Harness(tmp_path, db)


# -- the matrix ---------------------------------------------------------------


async def test_over_timeout_and_connected_is_terminated(harness):
    """The base case the whole loop exists for."""
    harness.add_instance("i-idle")
    await harness.dispatcher._check_idle()
    assert [i for i, _ in harness.terminated] == ["i-idle"]
    # Never unattended-force: the rescue runs first, always.
    assert harness.terminated[0][1] == {"force": False}
    assert "i-idle" not in harness.dispatcher.last_activity


async def test_under_timeout_is_left_alone(harness):
    harness.add_instance("i-busy", idle_for=TIMEOUT - 60)
    await harness.dispatcher._check_idle()
    assert harness.terminated == []


async def test_disconnected_is_left_alone_and_its_clock_is_dropped(harness):
    """Unreachable time is not idle time: we cannot even rescue it."""
    harness.add_instance("i-gone", connected=False)
    await harness.dispatcher._check_idle()
    assert harness.terminated == []
    assert "i-gone" not in harness.dispatcher.last_activity


async def test_keep_alive_is_left_alone(harness):
    harness.add_instance("i-pinned-by-user", keep_alive=True)
    await harness.dispatcher._check_idle()
    assert harness.terminated == []


async def test_auto_managed_instance_is_left_alone(harness):
    """Its job's lifecycle owns teardown; the idle loop must not race it."""
    launch_id = harness.add_instance("i-auto")
    harness.own_auto("i-auto", launch_id, "running")
    await harness.dispatcher._check_idle()
    assert harness.terminated == []


async def test_auto_managed_instance_in_terminating_is_left_alone(harness):
    """A blocked auto-managed teardown parks in 'terminating' and retries on
    its own tick; the idle loop must not issue a second terminate."""
    launch_id = harness.add_instance("i-auto-term")
    harness.own_auto("i-auto-term", launch_id, "terminating")
    await harness.dispatcher._check_idle()
    assert harness.terminated == []


async def test_running_server_job_pins_its_instance(harness):
    """vllm-serve streams for its lifetime; killing it kills a served model."""
    harness.add_instance("i-serving")
    harness.pin_task("i-serving", "vllm-serve")
    await harness.dispatcher._check_idle()
    assert harness.terminated == []


async def test_running_batch_job_pins_its_instance(harness):
    """A batch job at 90% is exactly what must never be destroyed."""
    harness.add_instance("i-batch")
    harness.pin_task("i-batch", "whisper-batch")
    await harness.dispatcher._check_idle()
    assert harness.terminated == []


async def test_a_job_on_another_box_does_not_pin_this_one(harness):
    """Phase 35: a task pins ITS OWN instance only."""
    harness.add_instance("i-working")
    harness.add_instance("i-empty")
    harness.pin_task("i-working", "whisper-batch")
    await harness.dispatcher._check_idle()
    assert [i for i, _ in harness.terminated] == ["i-empty"]


async def test_instance_with_no_launch_row_uses_the_default_timeout(harness):
    """An adopted box that Manifold never launched still has no ceiling and
    no per-instance timeout: the global default decides."""
    harness.add_instance("i-adopted", launch=False)
    await harness.dispatcher._check_idle()
    assert [i for i, _ in harness.terminated] == ["i-adopted"]


async def test_blocked_termination_leaves_the_box_up_and_is_recorded(harness):
    """Data beats billing: the rescue could not save everything, so the box
    stays. The loop records it and retries next pass."""
    harness.add_instance("i-blocked")
    harness.raises = TerminationBlocked(
        "i-blocked", [{"path": "checkpoints/step-2000.safetensors"}])
    await harness.dispatcher._check_idle()

    assert [i for i, _ in harness.terminated] == ["i-blocked"]
    assert "i-blocked" in harness.orch.connections      # still up
    actions = [r["action"] for r in harness.db._execute(
        "SELECT action FROM audit_log ORDER BY id").fetchall()]
    assert "idle_termination" in actions
    assert "idle_termination_blocked" in actions
