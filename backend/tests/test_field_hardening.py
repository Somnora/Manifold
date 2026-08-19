"""Phase 40 field hardening: launches survive --reload, boots get a real
timeout window, launch progress is structured, and progress-bar log churn is
collapsed. All against mocks - zero spend."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import LaunchPolicy, load_settings
from app.connections import ConnectionState
from app.lambda_api import InstanceInfo, MockLambdaClient
from app.main import create_app
from app.orchestrator import Orchestrator, launch_progress
from app.task_queue import (
    SQLiteTaskQueue,
    collapse_progress,
    is_docker_pull_noise,
)
from tests.conftest import make_settings, mock_connect_fn, wait_for_launch_status


# -- collapse_progress (pure) ----------------------------------------------------

def test_collapse_progress_keeps_last_segment_of_a_bar():
    # A progress bar redraws one line with \r; only the final frame is shown.
    line = "10%\r 40%\r 80%\r100% done"
    assert collapse_progress(line) == "100% done"


def test_collapse_progress_strips_trailing_cr_from_crlf():
    assert collapse_progress("finished\r") == "finished"


def test_collapse_progress_leaves_plain_lines_untouched():
    assert collapse_progress("[manifold] $ docker run ...") == \
        "[manifold] $ docker run ..."


def test_collapse_progress_empty_stays_empty():
    assert collapse_progress("") == ""


def test_job_logs_collapse_carriage_returns_end_to_end(db):
    """A line captured with embedded \r lands in the store already collapsed,
    so get_job_logs (and the tokens an agent spends reading it) stay small."""
    queue = SQLiteTaskQueue(db)
    task_id = queue.enqueue(template="gpu-smoke", parameters={})
    queue.append_log(task_id, "epoch 1\repoch 2\repoch 3\repoch 4 final")
    queue.append_log(task_id, "plain line")
    lines = [row["line"] for row in queue.get_logs(task_id)]
    assert "epoch 4 final" in lines
    assert "plain line" in lines
    # None of the intermediate frames were stored.
    assert not any("\r" in ln for ln in lines)
    assert not any("epoch 1" in ln for ln in lines)


# -- docker pull noise (pure) ----------------------------------------------------

def test_docker_layer_lines_are_noise():
    for line in (
        "980c13e156f9: Waiting",
        "2b441cc2eb0c: Downloading [===>    ]  5.2MB/12MB",
        "46c9c54348df: Verifying Checksum",
        "3713021b0277: Download complete",
        "b6ceb6c620b6: Extracting [====>   ]",
        "39b4878ee125: Pull complete",
        "be37c9137332: Already exists",
        "273246b59ac2: Pulling fs layer",
    ):
        assert is_docker_pull_noise(line), line


def test_meaningful_pull_lines_survive():
    # Image identity and the pull's start/finish must NOT be dropped.
    for line in (
        "latest: Pulling from huggingface/transformers-pytorch-gpu",
        "Digest: sha256:4c7317881a534b22e18add49c925096fa902651fb0571c69f3",
        "Status: Downloaded newer image for huggingface/transformers:latest",
        "saved /data/output/sdxl-0.png",
        "CUDA Version 12.6.0",
    ):
        assert not is_docker_pull_noise(line), line


def test_append_log_drops_docker_churn_end_to_end(db):
    queue = SQLiteTaskQueue(db)
    task_id = queue.enqueue(template="sdxl-generate", parameters={})
    queue.append_log(task_id, "latest: Pulling from library/x")
    queue.append_log(task_id, "980c13e156f9: Waiting")
    queue.append_log(task_id, "980c13e156f9: Pull complete")
    queue.append_log(task_id, "Status: Downloaded newer image for x:latest")
    queue.append_log(task_id, "saved /data/output/sdxl-0.png")
    lines = [row["line"] for row in queue.get_logs(task_id)]
    assert "latest: Pulling from library/x" in lines
    assert "Status: Downloaded newer image for x:latest" in lines
    assert "saved /data/output/sdxl-0.png" in lines
    # The per-layer churn never reached the store.
    assert not any("Waiting" in ln or "Pull complete" in ln for ln in lines)


# -- launch_progress (pure) ------------------------------------------------------

def test_launch_progress_ready_is_settled():
    out = launch_progress({"status": "active"}, 2400.0, "2026-07-14T00:00:00+00:00")
    assert out["phase"] == "ready"
    assert out["settled"] is True


def test_launch_progress_failed_is_settled():
    out = launch_progress({"status": "failed"}, 2400.0, "2026-07-14T00:00:00+00:00")
    assert out["phase"] == "failed"
    assert out["settled"] is True


def test_launch_progress_booting_has_countdown():
    launch = {
        "status": "booting",
        "lambda_instance_id": "i-boot",
        "launched_at": "2026-07-14T00:00:00+00:00",
    }
    out = launch_progress(launch, 2400.0, "2026-07-14T00:05:00+00:00")  # 300s later
    assert out["phase"] == "waiting_for_active"
    assert out["settled"] is False
    assert out["boot_elapsed_seconds"] == 300
    assert out["boot_timeout_seconds"] == 2400
    assert out["boot_remaining_seconds"] == 2100
    assert "i-boot" in out["phase_detail"]


def test_launch_progress_reports_provisioning_once_ssh_is_up():
    """'booting' spans two windows and only the first one has that deadline.
    Once SSH answers, the boot countdown is measuring a deadline that no
    longer decides anything, so it goes away and the detail says what is
    actually happening on the box."""
    launch = {
        "status": "booting",
        "lambda_instance_id": "i-gce",
        "launched_at": "2026-08-17T00:00:00+00:00",
        "connected_at": "2026-08-17T00:01:30+00:00",
    }
    out = launch_progress(launch, 2400.0, "2026-08-17T00:04:30+00:00", 1200.0)

    assert out["phase"] == "waiting_for_active"      # no new vocabulary
    assert out["settled"] is False
    assert "i-gce" in out["phase_detail"]
    assert "180s of 1200s" in out["phase_detail"]
    assert "boot_remaining_seconds" not in out
    assert "boot_elapsed_seconds" not in out


def test_launch_progress_booting_clamps_when_past_timeout():
    launch = {
        "status": "booting",
        "lambda_instance_id": "i-boot",
        "launched_at": "2026-07-14T00:00:00+00:00",
    }
    out = launch_progress(launch, 60.0, "2026-07-14T01:00:00+00:00")  # 1h later
    assert out["boot_remaining_seconds"] == 0     # never negative


# -- boot timeout default --------------------------------------------------------

def test_boot_timeout_default_is_generous():
    """The shipped default must cover a slow SXM4 boot, not cut it off."""
    assert LaunchPolicy().boot_timeout_seconds == 2400.0


def test_config_yaml_boot_timeout_loaded(tmp_path, monkeypatch):
    settings = load_settings()
    # Repo config.yaml carries the hardened value.
    assert settings.launch.boot_timeout_seconds >= 2400.0


# -- resume pending launches (survive --reload) ---------------------------------

def _booting_launch(db, instance_id="i-boot"):
    launch_id = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
    )
    db.update_launch(
        launch_id, status="booting", lambda_instance_id=instance_id,
        launched_type="gpu_1x_a10", launched_at="2026-07-14T00:00:00+00:00",
    )
    return launch_id


async def test_resume_finishes_a_still_booting_launch(tmp_path, db):
    """A launch orphaned mid-boot (its waiter died with the old process) gets
    a fresh waiter that carries it to active + connected."""
    settings = make_settings(tmp_path)
    mock = MockLambdaClient()
    mock.instances["i-boot"] = InstanceInfo(
        id="i-boot", name="manifold-boot", status="booting", ip=None,
        region="us-east-1", instance_type="gpu_1x_a10", hourly_rate_cents=129,
    )
    launch_id = _booting_launch(db)

    orch = Orchestrator(settings, mock, db, connect_fn=mock_connect_fn)
    resumed = await orch.resume_pending_launches()
    assert resumed == 1

    settled = await orch.wait_for_launch(launch_id, timeout=3.0)
    assert settled["status"] == "active"
    assert "i-boot" in orch.connections


async def test_resume_marks_active_when_adopt_already_reconnected(tmp_path, db):
    """If the instance finished booting during the downtime, adopt reconnects
    it and resume hands the still-'booting' record to the provisioning gate.

    Phase 110 changed WHO closes this record. It used to be closed here, on
    the strength of a connection existing - the one path that reached
    'active' without ever asking the box whether it was set up. Now the same
    gate the launch pipeline uses does it, and a ready box passes on its
    first pass, so the outcome is unchanged for an instance that IS ready.
    """
    settings = make_settings(tmp_path)
    mock = MockLambdaClient()
    mock.instances["i-boot"] = InstanceInfo(
        id="i-boot", name="manifold-boot", status="active", ip="203.0.113.9",
        region="us-east-1", instance_type="gpu_1x_a10", hourly_rate_cents=129,
    )
    launch_id = _booting_launch(db)

    orch = Orchestrator(settings, mock, db, connect_fn=mock_connect_fn)
    await orch.adopt_running_instances()          # reconnects the active one
    assert "i-boot" in orch.connections
    resumed = await orch.resume_pending_launches()
    assert resumed == 1

    settled = await orch.wait_for_launch(launch_id, timeout=3.0)
    assert settled["status"] == "active"
    assert settled["connected_at"] is not None


async def test_resume_fails_booting_launch_with_no_instance(tmp_path, db):
    """A 'booting' row with no instance id can never settle on its own; resume
    fails it rather than leaving a zombie."""
    settings = make_settings(tmp_path)
    launch_id = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
    )
    db.update_launch(launch_id, status="booting")   # no lambda_instance_id
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    await orch.resume_pending_launches()
    assert db.get_launch(launch_id)["status"] == "failed"


async def test_resume_fails_stale_pending_launch(tmp_path, db):
    """A 'launching' row whose task died with the old process is closed at
    startup — the spend guard counts open pending rows, so a stale one
    would wrongly eat a concurrency slot forever."""
    settings = make_settings(tmp_path)
    launch_id = db.create_launch(          # rows start as 'launching'
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
    )
    assert db.pending_launch_count() == 1
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    await orch.resume_pending_launches()
    assert db.get_launch(launch_id)["status"] == "failed"
    assert db.pending_launch_count() == 0


def test_restart_resumes_mid_boot_over_http(tmp_path, mock_storage, mock_sidecar):
    """End to end: a launch left 'booting' in the DB (as --reload would) is
    carried to active by a fresh app on startup, with an audit trail."""
    from app.db import Database

    settings = make_settings(tmp_path)
    shared_mock = MockLambdaClient()
    shared_mock.instances["i-boot"] = InstanceInfo(
        id="i-boot", name="manifold-boot", status="booting", ip=None,
        region="us-east-1", instance_type="gpu_1x_a10", hourly_rate_cents=129,
    )

    # Seed a booting launch row BEFORE the app starts, so startup's
    # resume_pending_launches sees it (mirrors a --reload mid-boot).
    seed_db = Database(settings.db_path)
    launch_id = _booting_launch(seed_db)
    seed_db.close()

    app = create_app(
        settings, lambda_client=shared_mock,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )
    with TestClient(app) as client:
        settled = wait_for_launch_status(client, launch_id, timeout=3.0)
        assert settled["status"] == "active"
        audit = client.get("/audit").json()["entries"]
        assert any(e["action"] == "resume_pending_launches" for e in audit)


# -- wait long-poll + structured phase over HTTP --------------------------------

def test_launch_route_returns_structured_phase(client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    launch_id = resp.json()["launch"]["id"]
    settled = wait_for_launch_status(client, launch_id)
    body = client.get(f"/launches/{launch_id}").json()
    assert body["phase"] == "ready"
    assert body["settled"] is True
    assert "phase_detail" in body


def test_wait_route_blocks_until_settled(client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    launch_id = resp.json()["launch"]["id"]
    body = client.get(f"/launches/{launch_id}/wait", params={"timeout": 5}).json()
    assert body["settled"] is True
    assert body["status"] in ("active", "failed")


def test_wait_route_404_for_unknown_launch(client):
    resp = client.get("/launches/nope/wait", params={"timeout": 1})
    assert resp.status_code == 404
