import pytest

from app.config import Guardrails
from tests.conftest import make_settings


@pytest.fixture
def settings(tmp_path):
    """This file launches several instances back to back; the honest
    concurrency guard (which counts admitted-but-pending launches) needs
    room, or the default 1-instance limit refuses launch #2."""
    return make_settings(tmp_path, guardrails=Guardrails(
        max_concurrent_instances=8, max_hourly_spend_usd=50.0))


async def test_launch_idle_timeout_clamping(orchestrator):
    # Clamp min
    launch1 = await orchestrator.request_launch(
        instance_type="gpu_1x_a10", region="us-east", filesystem="", idle_timeout_seconds=60
    )
    assert launch1["idle_timeout_seconds"] == 300
    
    # Clamp max
    launch2 = await orchestrator.request_launch(
        instance_type="gpu_1x_a10", region="us-east", filesystem="", idle_timeout_seconds=86400
    )
    assert launch2["idle_timeout_seconds"] == 14400

    # Within bounds
    launch3 = await orchestrator.request_launch(
        instance_type="gpu_1x_a10", region="us-east", filesystem="", idle_timeout_seconds=3600
    )
    assert launch3["idle_timeout_seconds"] == 3600

    # Default
    launch4 = await orchestrator.request_launch(
        instance_type="gpu_1x_a10", region="us-east", filesystem=""
    )
    assert launch4["idle_timeout_seconds"] is None

from tests.conftest import wait_for_launch_status

def test_set_idle_timeout_endpoint(client, db):
    # Launch via API
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10",
        "region": "us-east",
        "filesystem": "",
    })
    launch_id = resp.json()["launch"]["id"]
    
    # Wait for background launch task
    wait_for_launch_status(client, launch_id, "active")
    
    db = client.app.state.orchestrator.db
    launch = db.get_launch(launch_id)
    instance_id = launch["lambda_instance_id"]
    
    # Set timeout
    resp = client.post(f"/instances/{instance_id}/idle-timeout", json={"idle_timeout_seconds": 1800})
    assert resp.status_code == 200
    assert resp.json()["idle_timeout_seconds"] == 1800
    
    launch = db.get_launch(launch_id)
    assert launch["idle_timeout_seconds"] == 1800

    # Reset to default
    resp = client.post(f"/instances/{instance_id}/idle-timeout", json={"idle_timeout_seconds": None})
    assert resp.status_code == 200
    assert resp.json()["idle_timeout_seconds"] is None
    
    launch = db.get_launch(launch_id)
    assert launch["idle_timeout_seconds"] is None
