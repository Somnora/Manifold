"""Queue-then-launch (Phase 24): an auto-managed job owns its whole instance
lifecycle — launch -> run -> sync -> terminate — through the SAME guarded
functions the dashboard uses. These drive the full loop against mocks (zero
spend) and pin the Gate B behavior.
"""

import time

from fastapi.testclient import TestClient

from app.config import (
    AutoManageSettings,
    Guardrails,
    IdleSettings,
    TaskSettings,
)
from app.lambda_api import MockLambdaClient
from app.main import create_app
from app.model_client import MockModelClient
from app.sidecar_client import MockSidecarClient
from app.storage import MockStorage
from tests.conftest import make_settings, mock_connect_fn


def _fast(tmp_path, **overrides):
    """Test settings with every loop cranked fast so the lifecycle runs in ms."""
    base = dict(
        tasks=TaskSettings(poll_seconds=0.02),
        auto_manage=AutoManageSettings(poll_seconds=0.02),
        idle=IdleSettings(timeout_seconds=1800, poll_seconds=0.02),
    )
    base.update(overrides)
    return make_settings(tmp_path, **base)


def _app(settings, *, sidecar, mock=None, image_checker=None):
    from app.image_checker import MockImageChecker
    mock = mock or MockLambdaClient()
    app = create_app(
        settings,
        lambda_client=mock,
        storage_factory=lambda fs: MockStorage(),
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: sidecar,
        model_client_factory=lambda conn: MockModelClient(),
        image_checker=image_checker or MockImageChecker(),
        notification_sender=lambda title, body: None,
    )
    return app, mock


def _queue_auto(client, *, template="whisper-batch", gpu="gpu_1x_a10",
                region="us-east-1", filesystem="manifold-data"):
    r = client.post("/tasks", json={
        "template": template, "parameters": {}, "auto_manage": True,
        "gpu_type": gpu, "region": region, "filesystem": filesystem,
    })
    return r


def _wait_lifecycle(client, task_id, targets, timeout=10.0):
    deadline = time.monotonic() + timeout
    task = client.get(f"/tasks/{task_id}").json()
    while time.monotonic() < deadline:
        task = client.get(f"/tasks/{task_id}").json()
        if task["lifecycle"] in targets:
            return task
        time.sleep(0.02)
    raise AssertionError(
        f"task {task_id} never reached {targets}; last: {task.get('lifecycle')} "
        f"({task.get('lifecycle_detail')})")


# -- happy path: the whole lifecycle, no instance running to start ---------------


def test_auto_managed_whisper_full_lifecycle(tmp_path):
    # A clean instance (nothing left in ephemeral) so the safety hook passes
    # and termination completes — the zero-waste happy path.
    sidecar = MockSidecarClient(unpersisted=[])
    app, mock = _app(_fast(tmp_path), sidecar=sidecar)
    with TestClient(app) as client:
        r = _queue_auto(client)
        assert r.status_code == 202
        task = r.json()["task"]
        assert task["auto_manage"] is True
        assert task["lifecycle"] == "queued"
        task_id = task["id"]

        done = _wait_lifecycle(client, task_id, ("done", "failed"))
        assert done["lifecycle"] == "done", done["lifecycle_detail"]

        # The dispatched job itself succeeded, and launch-to-ready was measured.
        assert done["status"] == "succeeded"
        assert done["launch_to_ready_seconds"] is not None
        assert done["launch_to_ready_seconds"] >= 0

        # Every lifecycle transition is in the audit log with the job id.
        actions = [e["action"] for e in client.get("/audit").json()["entries"]]
        for step in ("auto_manage_launching", "auto_manage_ready",
                     "auto_manage_running", "auto_manage_syncing",
                     "auto_manage_terminating", "auto_manage_done"):
            assert step in actions, f"missing {step} in {actions}"

        # The box it launched is gone (terminated through the guarded path).
        live = [i for i in mock.instances.values() if i.is_running]
        assert live == []
        # And the launch history row is closed.
        launch = client.get(f"/launches/{done['launch_id']}").json()
        assert launch["status"] == "terminated"


# -- Gate B: capacity is scarce, then appears; the guarded retry rides it --------


def test_auto_managed_job_rides_out_capacity_failures(tmp_path):
    # The first two launch attempts hit insufficient-capacity (as Lambda often
    # does); the launch pipeline's existing retry keeps going and the job still
    # completes. Same capacity-retry the manual launch path uses.
    from app.lambda_api import capacity_error

    mock = MockLambdaClient(
        scripted_launch_errors=[capacity_error(), capacity_error()])
    sidecar = MockSidecarClient(unpersisted=[])
    app, mock = _app(_fast(tmp_path), sidecar=sidecar, mock=mock)
    with TestClient(app) as client:
        r = _queue_auto(client)
        task_id = r.json()["task"]["id"]
        done = _wait_lifecycle(client, task_id, ("done", "failed"))
        assert done["lifecycle"] == "done", done["lifecycle_detail"]
        # 2 capacity failures + 1 success = 3 launch attempts.
        assert len(mock.launch_calls) == 3


# -- Gate B part 2: a guard rejection, surfaced, never launched ------------------


def test_auto_managed_over_budget_is_rejected_with_reason(tmp_path):
    # Budget cap below the cheapest GPU: the launch can never be admitted, so
    # the job fails with the guard's reason and NO instance is ever created.
    settings = _fast(
        tmp_path,
        guardrails=Guardrails(max_concurrent_instances=1,
                              max_hourly_spend_usd=0.50))
    sidecar = MockSidecarClient(unpersisted=[])
    app, mock = _app(settings, sidecar=sidecar)
    with TestClient(app) as client:
        r = _queue_auto(client)   # gpu_1x_a10 is $1.29/hr > $0.50 cap
        assert r.status_code == 202
        task_id = r.json()["task"]["id"]

        failed = _wait_lifecycle(client, task_id, ("failed",))
        assert failed["lifecycle"] == "failed"
        assert "Budget guard" in (failed["lifecycle_detail"] or "")
        assert failed["status"] == "failed"

        # No launch was ever attempted -> no instance exists.
        assert mock.launch_calls == []
        assert mock.instances == {}

        # The rejection is in the audit trail.
        actions = [e["action"] for e in client.get("/audit").json()["entries"]]
        assert "auto_manage_failed" in actions


# -- safety hook: files remain after sync -> surface, never force ----------------


def test_termination_blocked_is_surfaced_not_forced(tmp_path):
    # With a data-safety policy that can save NOTHING (no persistent volume,
    # no download), the 2 files the sidecar reports are unsaveable, so
    # terminate(force=False) blocks. The job must NOT force: it parks at
    # 'terminating' with the box still up, then completes on its own once the
    # files are cleared. Data beats billing, and never silently.
    sidecar = MockSidecarClient()   # 2 unpersisted files
    app, mock = _app(_fast(tmp_path), sidecar=sidecar)
    with TestClient(app) as client:
        client.put("/preferences", json={
            "data_safety": {"to_filesystem": False, "to_local": False}})
        r = _queue_auto(client)
        task_id = r.json()["task"]["id"]

        parked = _wait_lifecycle(client, task_id, ("terminating",))
        # Give the loop a few ticks; it must stay parked, not advance to done.
        time.sleep(0.2)
        parked = client.get(f"/tasks/{task_id}").json()
        assert parked["lifecycle"] == "terminating"
        assert "could not be saved" in (parked["lifecycle_detail"] or "").lower()

        # The instance is deliberately left running for review (not forced).
        live = [i for i in mock.instances.values() if i.is_running]
        assert len(live) == 1

        actions = [e["action"] for e in client.get("/audit").json()["entries"]]
        assert "auto_manage_terminate_blocked" in actions

        # Resolve it the way the manual flow does (files now persisted); the
        # loop's next force=False retry succeeds and the job finishes.
        sidecar.clear_unpersisted()
        done = _wait_lifecycle(client, task_id, ("done",))
        assert done["lifecycle"] == "done"
        assert [i for i in mock.instances.values() if i.is_running] == []


# -- reconciliation: the idle loop never races the lifecycle ---------------------


def test_idle_loop_skips_auto_managed_instance(tmp_path):
    # An instance an auto-managed job owns must be exempt from idle
    # auto-termination (its lifecycle owns teardown), even at timeout 0.
    from tests.test_reconcile import launch_connected

    # timeout 0 so the instance is INSTANTLY idle-eligible, but the idle
    # LOOP parked (poll 1h) because this test drives _check_idle() itself
    # below. At 0.02s the loop fired ~50 times a second underneath the
    # assertions and the test failed intermittently on CI with StopIteration
    # - a background sweep racing the one the test meant to observe. The
    # exemption being tested is unaffected by how often the loop ticks.
    settings = _fast(tmp_path, idle=IdleSettings(timeout_seconds=0,
                                                 poll_seconds=3600))
    sidecar = MockSidecarClient(unpersisted=[])
    app, mock = _app(settings, sidecar=sidecar)
    with TestClient(app) as client:
        launch_id, instance_id = launch_connected(client)
        db = client.app.state.orchestrator.db

        # Attach an in-flight auto-managed job to this instance.
        task_id = db.create_task(template="whisper-batch", parameters={},
                                 auto_manage=True, gpu_type="gpu_1x_a10",
                                 region="us-east-1", filesystem="manifold-data")
        db.set_task_lifecycle(task_id, "running", launch_id=launch_id)

        assert instance_id in db.auto_managed_instance_ids()

        dispatcher = client.app.state.dispatcher
        import asyncio
        asyncio.run(dispatcher._check_idle())

        # Still connected: the idle loop skipped it.
        assert instance_id in client.app.state.orchestrator.connections
        assert mock.instances[instance_id].is_running


def test_auto_job_waits_for_slot_behind_a_manual_instance(tmp_path):
    # A manually launched instance occupies the single slot. An auto-managed
    # job must NOT hijack it; it waits (concurrency guard) and never launches a
    # second box. Then cancelling it leaves the manual instance untouched.
    from tests.test_reconcile import launch_connected

    sidecar = MockSidecarClient(unpersisted=[])
    app, mock = _app(_fast(tmp_path), sidecar=sidecar)
    with TestClient(app) as client:
        _, manual_id = launch_connected(client)   # holds the only slot

        r = _queue_auto(client)
        task_id = r.json()["task"]["id"]
        waiting = _wait_lifecycle(client, task_id, ("waiting",))
        assert waiting["lifecycle"] == "waiting"
        assert "slot" in (waiting["lifecycle_detail"] or "").lower()

        # It stays waiting and never launches its own instance (only the
        # manual launch was ever attempted), i.e. it did not run on the
        # manual box either.
        time.sleep(0.15)
        assert client.get(f"/tasks/{task_id}").json()["lifecycle"] == "waiting"
        assert len(mock.launch_calls) == 1
        assert client.get(f"/tasks/{task_id}").json()["status"] == "queued"

        # Cancel the waiting job: the manual instance is left running.
        assert client.post(f"/tasks/{task_id}/cancel").status_code == 200
        assert client.get(f"/tasks/{task_id}").json()["lifecycle"] == "cancelled"
        assert mock.instances[manual_id].is_running


def test_cancel_after_launch_tears_down_its_instance(tmp_path):
    # Cancelling a job that already booted a box tears the box down through the
    # guarded path. Loops are frozen so we control the state deterministically.
    from tests.test_reconcile import launch_connected

    settings = _fast(
        tmp_path,
        tasks=TaskSettings(poll_seconds=999),
        auto_manage=AutoManageSettings(poll_seconds=999))
    sidecar = MockSidecarClient(unpersisted=[])   # clean: hook passes
    app, mock = _app(settings, sidecar=sidecar)
    with TestClient(app) as client:
        launch_id, instance_id = launch_connected(client)
        db = client.app.state.orchestrator.db
        task_id = db.create_task(
            template="whisper-batch", parameters={}, auto_manage=True,
            gpu_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data")
        db.set_task_lifecycle(task_id, "ready", launch_id=launch_id)

        assert client.post(f"/tasks/{task_id}/cancel").status_code == 200
        assert client.get(f"/tasks/{task_id}").json()["lifecycle"] == "cancelled"
        assert not mock.instances[instance_id].is_running


def test_reason_code_classifies_guard_rejections(tmp_path):
    # The lifecycle relies on reason_code to tell wait (concurrency) from fail
    # (budget); pin both at the source.
    import asyncio

    from app.db import Database
    from app.orchestrator import LaunchRejected, Orchestrator

    settings = _fast(
        tmp_path,
        guardrails=Guardrails(max_concurrent_instances=1,
                              max_hourly_spend_usd=0.50))
    db = Database(settings.db_path)
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    try:
        exc = None
        try:
            asyncio.run(orch.request_launch(
                instance_type="gpu_1x_a10", region="us-east-1",
                filesystem="manifold-data"))
        except LaunchRejected as e:
            exc = e
        assert exc is not None and exc.reason_code == "budget"
    finally:
        db.close()
