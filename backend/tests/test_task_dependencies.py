"""Phase 77: task dependencies.

A job can declare depends_on at enqueue; it stays queued until every parent
succeeded, and settles as 'skipped' (a first-class status: it never ran)
when a parent cannot succeed anymore. Two gates enforce this - the dispatch
scan for manual jobs, and auto-manage promotion for jobs that launch their
own GPU. The second is the one that guards money: a child promoted early
would boot a billing instance just to wait.

The graph is a DAG by construction, not by detection: deps may only
reference tasks that already exist and are immutable after enqueue, so an
older task can never come to depend on a younger one.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.config import IdleSettings, TaskSettings
from app.connections import ConnectionState
from app.dispatcher import Dispatcher
from app.lambda_api import MockLambdaClient
from app.main import create_app
from app.task_queue import SQLiteTaskQueue
from tests.conftest import make_settings, mock_connect_fn


class Conn:
    state = ConnectionState.CONNECTED

    async def run(self, command, **kwargs):
        return 0, "", ""


class Notifier:
    def __init__(self):
        self.pings: list[tuple[str, str, str]] = []

    def notify(self, kind, title, body, **kwargs):
        self.pings.append((kind, title, body))


@pytest.fixture
def rig(tmp_path, db):
    from app.orchestrator import Orchestrator
    settings = make_settings(tmp_path)
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    queue = SQLiteTaskQueue(db)
    notifier = Notifier()
    d = Dispatcher(settings, orch, queue, {}, db, MockLambdaClient(),
                   notifier=notifier)
    return d, orch, queue, notifier


def picked_ids(d):
    return [t["id"] for t, _, _ in d._pick_dispatchable()]


# -- the dispatch gate -------------------------------------------------------


def test_child_waits_while_parent_is_queued_or_running(rig):
    d, orch, queue = rig[:3]
    orch.connections["i-1"] = Conn()
    a = queue.enqueue(template="gpu-smoke", parameters={})
    b = queue.enqueue(template="gpu-smoke", parameters={}, depends_on=[a])

    # Parent queued: only the parent is dispatchable.
    assert picked_ids(d) == [a]

    # Parent running: the child still waits (running is not succeeded).
    queue.mark_running(a, "i-1")
    assert picked_ids(d) == []
    assert queue.get(b)["status"] == "queued"


def test_child_dispatches_once_parent_succeeded(rig):
    d, orch, queue = rig[:3]
    orch.connections["i-1"] = Conn()
    a = queue.enqueue(template="gpu-smoke", parameters={})
    b = queue.enqueue(template="gpu-smoke", parameters={}, depends_on=[a])
    queue.mark_running(a, "i-1")
    queue.mark_finished(a, exit_code=0, output_paths=[])
    assert picked_ids(d) == [b]


def test_dep_on_already_succeeded_parent_is_satisfied(rig):
    """Appending to a finished pipeline: dep on a succeeded task is met."""
    d, orch, queue = rig[:3]
    orch.connections["i-1"] = Conn()
    a = queue.enqueue(template="gpu-smoke", parameters={})
    queue.mark_running(a, "i-1")
    queue.mark_finished(a, exit_code=0, output_paths=[])
    b = queue.enqueue(template="gpu-smoke", parameters={}, depends_on=[a])
    assert picked_ids(d) == [b]


def test_independent_tasks_flow_past_a_waiting_child(rig):
    """No head-of-line blocking: a younger task with no deps dispatches
    while an older child waits on its parent."""
    d, orch, queue = rig[:3]
    orch.connections["i-1"] = Conn()
    a = queue.enqueue(template="gpu-smoke", parameters={})
    queue.mark_running(a, "i-1")            # parent busy on the instance...
    queue.mark_finished(a, exit_code=0, output_paths=[])
    running = queue.enqueue(template="gpu-smoke", parameters={})
    queue.mark_running(running, "i-1")      # ...someone else holds the box
    orch.connections["i-2"] = Conn()
    waiting = queue.enqueue(template="gpu-smoke", parameters={},
                            depends_on=[running])
    younger = queue.enqueue(template="gpu-smoke", parameters={})
    assert picked_ids(d) == [younger]
    assert queue.get(waiting)["status"] == "queued"


# -- failure flows downstream ------------------------------------------------


def test_parent_failure_skips_the_whole_chain(rig):
    d, _, queue, _ = rig
    a = queue.enqueue(template="gpu-smoke", parameters={})
    b = queue.enqueue(template="gpu-smoke", parameters={}, depends_on=[a])
    c = queue.enqueue(template="gpu-smoke", parameters={}, depends_on=[b])
    queue.mark_running(a, "i-1")
    d._finish_task(a, exit_code=1, output_paths=[], error="container exited 1")

    tb, tc = queue.get(b), queue.get(c)
    assert tb["status"] == "skipped" and tc["status"] == "skipped"
    # The reason names the edge that died, one hop at a time.
    assert a in tb["error"] and "did not succeed" in tb["error"]
    assert b in tc["error"]
    # Skips never ran: no exit code, no instance.
    assert tb["exit_code"] is None and tb["instance_id"] is None


def test_one_failed_parent_of_many_skips_the_child(rig):
    d, _, queue, _ = rig
    ok = queue.enqueue(template="gpu-smoke", parameters={})
    queue.mark_running(ok, "i-1")
    queue.mark_finished(ok, exit_code=0, output_paths=[])
    bad = queue.enqueue(template="gpu-smoke", parameters={})
    child = queue.enqueue(template="gpu-smoke", parameters={},
                          depends_on=[ok, bad])
    queue.mark_running(bad, "i-1")
    d._finish_task(bad, exit_code=1, output_paths=[], error="boom")
    assert queue.get(child)["status"] == "skipped"


def test_cancelling_a_parent_cascades_too(rig):
    """notify=False (the user is right there) must not suppress the cascade
    - the children still can never run."""
    d, _, queue, notifier = rig
    a = queue.enqueue(template="gpu-smoke", parameters={})
    b = queue.enqueue(template="gpu-smoke", parameters={}, depends_on=[a])
    d._finish_task(a, exit_code=-1, output_paths=[],
                   error="cancelled by user", notify=False)
    assert queue.get(b)["status"] == "skipped"
    assert notifier.pings == []


def test_one_ping_for_the_whole_dead_pipeline(rig):
    d, _, queue, notifier = rig
    a = queue.enqueue(template="gpu-smoke", parameters={})
    b = queue.enqueue(template="gpu-smoke", parameters={}, depends_on=[a])
    c = queue.enqueue(template="gpu-smoke", parameters={}, depends_on=[b])
    queue.mark_running(a, "i-1")
    d._finish_task(a, exit_code=1, output_paths=[], error="boom")

    kinds = [p[0] for p in notifier.pings]
    assert kinds == ["job_failed"]           # exactly one, for the root
    body = notifier.pings[0][2]
    assert "Skipped downstream" in body
    assert b in body and c in body


def test_missing_parent_row_self_heals_to_skipped(rig):
    """The delete guard makes this unreachable through the API; if a row
    vanishes anyway, the dispatch scan settles the orphan instead of
    leaving it queued forever."""
    d, orch, queue = rig[:3]
    orch.connections["i-1"] = Conn()
    a = queue.enqueue(template="gpu-smoke", parameters={})
    b = queue.enqueue(template="gpu-smoke", parameters={}, depends_on=[a])
    queue.delete(a)
    assert picked_ids(d) == []
    task = queue.get(b)
    assert task["status"] == "skipped"
    assert "no longer exists" in task["error"]


# -- the auto-manage gate (the one that guards money) ------------------------


def test_auto_child_is_not_promoted_while_parent_unfinished(rig):
    """Promotion launches a GPU. A child whose parent is still running must
    not reach _auto_launch - a box booted early bills for the whole wait."""
    d, _, queue, _ = rig
    parent = queue.enqueue(template="gpu-smoke", parameters={})
    queue.mark_running(parent, "i-1")
    queue.enqueue(template="gpu-smoke", parameters={}, auto_manage=True,
                  gpu_type="gpu_1x_a10", region="us-east-1",
                  filesystem="manifold-data", depends_on=[parent])
    assert d._next_ready_auto_job() is None

    # The moment the parent succeeds, the child is offered the slot.
    queue.mark_finished(parent, exit_code=0, output_paths=[])
    promoted = d._next_ready_auto_job()
    assert promoted is not None and promoted["depends_on"] == [parent]


def test_younger_independent_auto_job_takes_the_slot(rig):
    """The waiting child waits on a TASK, not the slot: an independent
    younger auto job is promoted past it and nothing starves."""
    d, _, queue, _ = rig
    parent = queue.enqueue(template="gpu-smoke", parameters={})
    queue.mark_running(parent, "i-1")
    queue.enqueue(template="gpu-smoke", parameters={}, auto_manage=True,
                  gpu_type="gpu_1x_a10", region="us-east-1",
                  filesystem="manifold-data", depends_on=[parent])
    free = queue.enqueue(template="gpu-smoke", parameters={},
                         auto_manage=True, gpu_type="gpu_1x_a10",
                         region="us-east-1", filesystem="manifold-data")
    assert d._next_ready_auto_job()["id"] == free


def test_doomed_auto_child_skips_with_terminal_lifecycle(rig):
    """A skipped auto job must leave the pending scan entirely: status
    skipped AND lifecycle skipped, or the loop would re-offer it forever."""
    d, _, queue, _ = rig
    parent = queue.enqueue(template="gpu-smoke", parameters={})
    child = queue.enqueue(template="gpu-smoke", parameters={},
                          auto_manage=True, gpu_type="gpu_1x_a10",
                          region="us-east-1", filesystem="manifold-data",
                          depends_on=[parent])
    queue.mark_running(parent, "i-1")
    d._finish_task(parent, exit_code=1, output_paths=[], error="boom")

    task = queue.get(child)
    assert task["status"] == "skipped"
    assert task["lifecycle"] == "skipped"
    assert d._next_ready_auto_job() is None
    assert d.db.active_auto_managed_task() is None


# -- enqueue validation over HTTP --------------------------------------------


def _enqueue(client, template="gpu-smoke", **extra):
    return client.post("/tasks", json={
        "template": template, "parameters": {}, **extra})


def test_unknown_dep_is_422(client):
    resp = _enqueue(client, depends_on=["nope"])
    assert resp.status_code == 422
    assert "does not exist" in resp.json()["detail"]


def test_dep_on_dead_task_is_422(client):
    a = _enqueue(client).json()["task"]["id"]
    db = client.app.state.orchestrator.db
    db.update_task(a, status="failed", error="boom")
    resp = _enqueue(client, depends_on=[a])
    assert resp.status_code == 422
    assert "already failed" in resp.json()["detail"]


def test_dep_on_a_server_is_422(client):
    resp = client.post("/tasks", json={
        "template": "vllm-serve",
        "parameters": {"model_id": "Qwen/Qwen2.5-7B-Instruct"},
    })
    assert resp.status_code == 202
    server = resp.json()["task"]["id"]
    resp = _enqueue(client, depends_on=[server])
    assert resp.status_code == 422
    # The message teaches the working pattern instead of just refusing.
    assert "coexist" in resp.json()["detail"]


def test_duplicate_deps_are_deduped(client):
    a = _enqueue(client).json()["task"]["id"]
    task = _enqueue(client, depends_on=[a, a]).json()["task"]
    assert task["depends_on"] == [a]


def test_deps_are_resolved_on_list_and_get(client):
    a = _enqueue(client).json()["task"]["id"]
    b = _enqueue(client, depends_on=[a]).json()["task"]["id"]

    listed = {t["id"]: t for t in client.get("/tasks").json()["tasks"]}
    assert listed[b]["deps"] == [
        {"id": a, "template": "gpu-smoke", "status": "queued"}]
    assert listed[a]["deps"] == []
    single = client.get(f"/tasks/{b}").json()
    assert single["deps"][0]["id"] == a


# -- deletion guards ---------------------------------------------------------


def test_deleting_a_needed_parent_is_409(client):
    a = _enqueue(client).json()["task"]["id"]
    b = _enqueue(client, depends_on=[a]).json()["task"]["id"]
    resp = client.delete(f"/tasks/{a}")
    assert resp.status_code == 409
    assert b in resp.json()["detail"]

    # Settle the child (cancel) and the parent becomes deletable.
    client.post(f"/tasks/{b}/cancel")
    assert client.delete(f"/tasks/{a}").status_code == 200


def test_clear_finished_keeps_parents_queued_children_need(client):
    db = client.app.state.orchestrator.db
    a = _enqueue(client).json()["task"]["id"]
    db.update_task(a, status="succeeded", exit_code=0)
    b = _enqueue(client, depends_on=[a]).json()["task"]["id"]
    unrelated = _enqueue(client).json()["task"]["id"]
    db.update_task(unrelated, status="failed", error="boom")

    assert client.delete("/tasks/finished").json() == {"cleared": 1}
    ids = {t["id"] for t in client.get("/tasks").json()["tasks"]}
    assert a in ids and b in ids and unrelated not in ids


def test_clear_finished_reaps_skipped_tasks(client):
    db = client.app.state.orchestrator.db
    a = _enqueue(client).json()["task"]["id"]
    b = _enqueue(client, depends_on=[a]).json()["task"]["id"]
    db.update_task(a, status="failed", error="boom")
    db.update_task(b, status="skipped", error="skipped: dependency failed")
    assert client.delete("/tasks/finished").json() == {"cleared": 2}


# -- the pipeline end to end -------------------------------------------------


@pytest.fixture
def fast_app(tmp_path, mock_client, mock_storage, mock_sidecar):
    settings = make_settings(
        tmp_path,
        tasks=TaskSettings(poll_seconds=0.02),
        idle=IdleSettings(timeout_seconds=30.0, poll_seconds=0.5),
    )
    return create_app(
        settings,
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )


def wait_until(predicate, timeout=8.0, interval=0.02, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")


def test_chain_runs_in_order_over_http(fast_app):
    """A -> B on one instance: B is held while A runs and dispatches only
    after A succeeded. Ordering proved by timestamps, not by luck."""
    with TestClient(fast_app) as client:
        client.post("/instances", json={
            "instance_type": "gpu_1x_a10", "region": "us-east-1",
            "filesystem": "manifold-data",
        })
        a = client.post("/tasks", json={
            "template": "whisper-batch", "parameters": {},
        }).json()["task"]["id"]
        b = client.post("/tasks", json={
            "template": "whisper-batch", "parameters": {},
            "depends_on": [a],
        }).json()["task"]["id"]

        done = wait_until(
            lambda: (t := client.get(f"/tasks/{b}").json())["status"]
            == "succeeded" and t,
            message="chained task completion",
        )
        parent = client.get(f"/tasks/{a}").json()
        assert parent["status"] == "succeeded"
        # B started at or after A finished - never alongside it.
        assert done["started_at"] >= parent["finished_at"]


def test_cancelled_parent_skips_children_over_http(fast_app):
    """The user-visible failure path: cancel A while queued (no instance
    connected, nothing ever runs) and the whole chain settles skipped."""
    with TestClient(fast_app) as client:
        a = client.post("/tasks", json={
            "template": "whisper-batch", "parameters": {},
        }).json()["task"]["id"]
        b = client.post("/tasks", json={
            "template": "whisper-batch", "parameters": {},
            "depends_on": [a],
        }).json()["task"]["id"]
        client.post(f"/tasks/{a}/cancel")

        task = client.get(f"/tasks/{b}").json()
        assert task["status"] == "skipped"
        assert a in task["error"]
        # The skip is in the event trail, so the card can show WHY.
        events = client.get(f"/tasks/{b}/events").json()["events"]
        assert any(e["kind"] == "skipped" for e in events)
