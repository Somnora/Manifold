"""Test lifecycle events recording and API route."""

import time
import pytest
from fastapi.testclient import TestClient

from app.config import IdleSettings, TaskSettings, WatchSettings
from app.main import create_app
from tests.conftest import make_settings, mock_connect_fn, wait_for_launch_status

@pytest.fixture
def fast_app(tmp_path, mock_client, mock_storage, mock_sidecar):
    settings = make_settings(
        tmp_path,
        tasks=TaskSettings(poll_seconds=0.02),
        idle=IdleSettings(timeout_seconds=0.4, poll_seconds=0.05),
        watches=WatchSettings(poll_seconds=0.05),
    )
    from app.image_checker import MockImageChecker
    app = create_app(
        settings,
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )
    return app

def wait_until(predicate, timeout=8.0, interval=0.02, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")

def test_lifecycle_events_recorded_and_api(fast_app, mock_client):
    with TestClient(fast_app) as client:
        # 1. Launch an instance to run the task
        resp = client.post("/instances", json={
            "instance_type": "gpu_1x_a10",
            "region": "us-east-1",
            "filesystem": "manifold-data",
        })
        assert resp.status_code == 202
        launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
        instance_id = launch["lambda_instance_id"]
        wait_until(
            lambda: next(
                i for i in client.get("/instances").json()["instances"]
                if i["id"] == instance_id
            )["connection_state"] == "connected",
            message="SSH connected",
        )

        # 2. Enqueue a task
        resp = client.post("/tasks", json={
            "template": "whisper-batch",
            "parameters": {"input_dir": "interviews", "model_size": "small"},
        })
        assert resp.status_code == 202
        task_id = resp.json()["task"]["id"]

        # 3. Wait for completion
        task = wait_until(
            lambda: (t := client.get(f"/tasks/{task_id}").json())["status"]
            not in ("queued", "running") and t,
            message="task completion",
        )
        assert task["status"] == "succeeded"

        # 4. Fetch events
        events_resp = client.get(f"/tasks/{task_id}/events")
        assert events_resp.status_code == 200
        events = events_resp.json()["events"]

        # Event sequence for a successful task should be:
        # queued -> launched -> started -> finished
        # It won't have 'synced' unless it's auto_managed and runs _auto_sync, 
        # but let's check what events are recorded.
        kinds = [e["kind"] for e in events]
        assert "queued" in kinds
        assert "launched" in kinds
        assert "started" in kinds
        assert "finished" in kinds
        
        # Verify order
        assert kinds.index("queued") < kinds.index("launched")
        assert kinds.index("launched") < kinds.index("started")
        assert kinds.index("started") < kinds.index("finished")

        # Verify timestamps are non-decreasing
        from datetime import datetime
        times = [datetime.fromisoformat(e["at"]) for e in events]
        for i in range(1, len(times)):
            assert times[i] >= times[i-1]
            
        # Verify cost tracking (even if it's 0)
        costs = [e["cost_cents_at_event"] for e in events]
        for cost in costs:
            assert isinstance(cost, int)
            assert cost >= 0
