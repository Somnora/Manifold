"""Per-instance parallel dispatch (Phase 35).

Pins the fixes for what James hit at the test pass:
- a running SERVER job (vllm-serve) froze ALL dispatch - now a server and a
  batch job coexist on one instance (the documented serve+synthesize
  pipeline), and different instances dispatch independently;
- jobs can target a specific instance (multi-GPU routing);
- a running task pins only ITS OWN instance against idle termination;
- mock mode: server tasks stay RUNNING (real fidelity - enables chat and
  autopilot demos) and the auto-manage path works despite a real ssh key
  name in config.yaml (mock forces its registered key).
"""

import time

from fastapi.testclient import TestClient

from app.config import Guardrails, IdleSettings, SSHSettings, TaskSettings
from app.sidecar_client import MockSidecarClient
from tests.conftest import make_settings, wait_for_launch_status
from tests.test_auto_manage import _app, _fast


def launch_connected_on(client, timeout=5.0):
    """Launch an instance via the API and wait until its SSH is connected."""
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    assert resp.status_code == 202, resp.text
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    instance_id = launch["lambda_instance_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        inst = next(i for i in client.get("/instances").json()["instances"]
                    if i["id"] == instance_id)
        if inst["connection_state"] == "connected":
            return instance_id
        time.sleep(0.02)
    raise AssertionError("never connected")


def wait_task(client, task_id, statuses, timeout=8.0):
    deadline = time.monotonic() + timeout
    task = client.get(f"/tasks/{task_id}").json()
    while time.monotonic() < deadline:
        task = client.get(f"/tasks/{task_id}").json()
        if task["status"] in statuses:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} stuck at {task['status']}")


def test_server_job_stays_running_and_batch_coexists(tmp_path):
    """THE bug: vllm-serve used to (a) exit instantly in mock and (b) block
    every other dispatch in real mode. Now: the mock server stays RUNNING,
    the model endpoint reports serving, and a batch job dispatches and
    completes on the SAME instance while the server keeps running."""
    app, mock = _app(_fast(tmp_path), sidecar=MockSidecarClient(unpersisted=[]))
    with TestClient(app) as client:
        iid = launch_connected_on(client)

        serve = client.post("/tasks", json={
            "template": "vllm-serve",
            "parameters": {"model_id": "Qwen/Qwen3-8B"},
        }).json()["task"]
        running = wait_task(client, serve["id"], ("running",))
        assert running["instance_id"] == iid

        # The served model is discoverable -> chat + autopilot have a brain.
        model = client.get(f"/instances/{iid}/model").json()
        assert model["serving"] is True
        assert model["model_id"] == "Qwen/Qwen3-8B"

        # A batch job runs to completion WHILE the server keeps running.
        smoke = client.post("/tasks", json={
            "template": "gpu-smoke", "parameters": {},
        }).json()["task"]
        done = wait_task(client, smoke["id"], ("succeeded", "failed"))
        assert done["status"] == "succeeded"
        assert done["instance_id"] == iid
        assert client.get(f"/tasks/{serve['id']}").json()["status"] == "running"

        # Termination ends the stream; the serve task settles (not stuck).
        resp = client.delete(f"/instances/{iid}?force=true")
        assert resp.status_code == 200, resp.text
        settled = wait_task(client, serve["id"], ("succeeded", "failed"))
        assert settled["status"] in ("succeeded", "failed")


def test_jobs_route_to_targeted_instances_in_parallel(tmp_path):
    """Two instances, two targeted server jobs: each lands on its chosen box
    and both are RUNNING at once - impossible under the old global
    one-job-at-a-time dispatch."""
    settings = _fast(
        tmp_path,
        guardrails=Guardrails(max_concurrent_instances=2,
                              max_hourly_spend_usd=4.00))
    app, mock = _app(settings, sidecar=MockSidecarClient(unpersisted=[]))
    with TestClient(app) as client:
        a = launch_connected_on(client)
        b = launch_connected_on(client)

        ta = client.post("/tasks", json={
            "template": "vllm-serve",
            "parameters": {"model_id": "m/a"},
            "target_instance_id": a,
        }).json()["task"]
        tb = client.post("/tasks", json={
            "template": "vllm-serve",
            "parameters": {"model_id": "m/b"},
            "target_instance_id": b,
        }).json()["task"]

        ra = wait_task(client, ta["id"], ("running",))
        rb = wait_task(client, tb["id"], ("running",))
        assert ra["instance_id"] == a
        assert rb["instance_id"] == b
        # Both truly concurrent.
        assert client.get(f"/tasks/{ta['id']}").json()["status"] == "running"
        assert client.get(f"/tasks/{tb['id']}").json()["status"] == "running"


def test_running_task_pins_only_its_own_instance(tmp_path):
    """Idle: work on box A must NOT keep idle box B alive.

    Phase 35's point, still the point. What changed in Phase 90 is WHY box
    A survives: a served model is no longer immune just for existing, so A
    is kept alive here the way a real one is - by being used. Every request
    to a served model already calls touch_activity, and this drives that
    same path through the app. Box B is touched by nothing and dies.
    """
    settings = _fast(
        tmp_path,
        guardrails=Guardrails(max_concurrent_instances=2,
                              max_hourly_spend_usd=4.00),
        idle=IdleSettings(timeout_seconds=0.2, poll_seconds=0.03))
    app, mock = _app(settings, sidecar=MockSidecarClient(unpersisted=[]))
    with TestClient(app) as client:
        a = launch_connected_on(client)
        b = launch_connected_on(client)

        serve = client.post("/tasks", json={
            "template": "vllm-serve",
            "parameters": {"model_id": "m/pin"},
            "target_instance_id": a,
        }).json()["task"]
        wait_task(client, serve["id"], ("running",))

        # Idle box B gets reaped; box A, in use, survives. The timeout here
        # is 0.2s, so A must be touched faster than that - a real 30-minute
        # window needs no such urgency.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            client.post(f"/instances/{a}/run", json={"command": "true"})
            if not mock.instances[b].is_running:
                break
            time.sleep(0.05)
        assert not mock.instances[b].is_running, "idle box B never terminated"
        client.post(f"/instances/{a}/run", json={"command": "true"})
        assert mock.instances[a].is_running, "box A in use was wrongly reaped"


def test_a_served_model_nobody_uses_is_reaped(tmp_path):
    """Phase 90, end to end through the app: a ready server that nobody
    queries is idle, and the sweep takes it.

    The mocked-harness matrix (test_idle_serves.py) pins the decision; this
    pins the wiring - a real task row, a real readiness probe, the real
    termination flow. It is the exact shape that billed for an hour: an
    autopilot run left a vllm-serve task running and stopped asking it
    anything.
    """
    settings = _fast(
        tmp_path,
        guardrails=Guardrails(max_concurrent_instances=2,
                              max_hourly_spend_usd=4.00),
        idle=IdleSettings(timeout_seconds=0.2, poll_seconds=0.03))
    app, mock = _app(settings, sidecar=MockSidecarClient(unpersisted=[]))
    with TestClient(app) as client:
        a = launch_connected_on(client)
        serve = client.post("/tasks", json={
            "template": "vllm-serve",
            "parameters": {"model_id": "m/abandoned"},
            "target_instance_id": a,
        }).json()["task"]
        wait_task(client, serve["id"], ("running",))

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and mock.instances[a].is_running:
            time.sleep(0.05)
        assert not mock.instances[a].is_running, (
            "an abandoned served model kept billing")


def test_mock_mode_auto_manage_works_with_real_key_in_config(tmp_path):
    """The exact failure from James's test pass: config.yaml names a real
    Lambda ssh key, mock only registers mock keys -> auto-manage launches
    failed. Mock mode now forces its own registered key."""
    from app.main import create_app

    settings = make_settings(
        tmp_path,
        tasks=TaskSettings(poll_seconds=0.02),
        ssh=SSHSettings(key_name="lambda-burst-ed25519",
                        reconnect_base_seconds=0.01),
    )
    # Full mock wiring like MANIFOLD_MOCK=1, except a clean sidecar: the
    # demo sidecar's canned unpersisted files would (correctly) park the
    # teardown at 'terminating' - this test targets the ssh-key fix.
    app = create_app(settings, mock=True,
                     sidecar_factory=lambda conn: MockSidecarClient(
                         unpersisted=[]))
    # Speed up the auto-manage loop for the test.
    app.state.dispatcher.settings = settings = __import__(
        "dataclasses").replace(
            settings, auto_manage=__import__(
                "app.config", fromlist=["AutoManageSettings"]
            ).AutoManageSettings(poll_seconds=0.02))
    with TestClient(app) as client:
        task = client.post("/tasks", json={
            "template": "gpu-smoke", "parameters": {},
            "auto_manage": True, "gpu_type": "gpu_1x_a10",
            "region": "us-east-1", "filesystem": "manifold-data",
        }).json()["task"]
        deadline = time.monotonic() + 10
        t = task
        while time.monotonic() < deadline:
            t = client.get(f"/tasks/{task['id']}").json()
            if t["lifecycle"] in ("done", "failed"):
                break
            time.sleep(0.03)
        assert t["lifecycle"] == "done", t.get("lifecycle_detail")
