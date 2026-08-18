"""Phase 103: attaching more than one filesystem at launch.

Extras are ATTACH-ONLY. They ride along on the provider call so the box has
/lambda/nfs/<name> for each, and that is the whole feature: template jobs
still mount the primary ({persistent}), sync still targets the primary, and
relative file paths still resolve against the primary. These tests pin both
halves - that the extra names reach the provider, and that nothing else
started meaning anything different.
"""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.config import IdleSettings, TaskSettings, WatchSettings
from app.db import Database
from app.lambda_api import FilesystemInfo, MockLambdaClient
from app.main import create_app
from app.orchestrator import LaunchRejected
from tests.conftest import (
    make_settings,
    mock_connect_fn,
    wait_for_launch_status,
)


def _fs(name: str, region: str) -> FilesystemInfo:
    return FilesystemInfo(
        id=f"fs-{name}", name=name, mount_point=f"/lambda/nfs/{name}",
        region=region, is_in_use=False, bytes_used=0,
    )


@pytest.fixture
def mock_client() -> MockLambdaClient:
    """Overrides the single-filesystem mock in conftest: this phase needs
    several in one region (to attach) and one in another (to refuse).
    Six in us-east-1 means the cap can be tested with names that all exist,
    so what fails is the cap and not a typo."""
    return MockLambdaClient(filesystems=[
        _fs("manifold-data", "us-east-1"),
        _fs("crop-archive", "us-east-1"),
        _fs("tally-raw", "us-east-1"),
        _fs("tally-out", "us-east-1"),
        _fs("mesh-cache", "us-east-1"),
        _fs("eval-sets", "us-east-1"),
        _fs("west-scratch", "us-west-1"),
    ])


# -- the provider call --------------------------------------------------------


async def test_two_filesystems_reach_the_provider_primary_first(
        orchestrator, mock_client):
    launch = await orchestrator.request_launch(
        instance_type="gpu_1x_a10",
        region="us-east-1",
        filesystem="manifold-data",
        extra_filesystems=["crop-archive"],
    )
    final = await orchestrator.wait_for_launch(launch["id"])
    assert final["status"] == "active"

    call = mock_client.launch_calls[0]
    assert call["filesystem_names"] == ["manifold-data", "crop-archive"]
    # ...and the instance itself reports both mounts, which is where every
    # payload's `filesystems` field comes from.
    instance = mock_client.instances[final["lambda_instance_id"]]
    assert instance.file_system_names == ["manifold-data", "crop-archive"]


async def test_a_scratch_launch_still_sends_no_filesystems(
        orchestrator, mock_client):
    """The old shape is untouched: no primary, no extras, empty list."""
    launch = await orchestrator.request_launch(
        instance_type="gpu_1x_a10", region="us-east-1", filesystem="",
    )
    await orchestrator.wait_for_launch(launch["id"])
    assert mock_client.launch_calls[0]["filesystem_names"] == []


def test_the_route_carries_extras_and_the_payload_lists_them(
        client, mock_client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10",
        "region": "us-east-1",
        "filesystem": "manifold-data",
        "extra_filesystems": ["crop-archive", "eval-sets"],
    })
    assert resp.status_code == 202, resp.text
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    assert launch["status"] == "active"
    assert mock_client.launch_calls[0]["filesystem_names"] == [
        "manifold-data", "crop-archive", "eval-sets"]

    instance = next(
        i for i in client.get("/instances").json()["instances"]
        if i["id"] == launch["lambda_instance_id"])
    assert instance["filesystems"] == [
        "manifold-data", "crop-archive", "eval-sets"]


# -- refusals -----------------------------------------------------------------


async def test_region_mismatch_on_an_extra_names_the_offender(
        orchestrator, mock_client):
    with pytest.raises(LaunchRejected) as exc:
        await orchestrator.request_launch(
            instance_type="gpu_1x_a10",
            region="us-east-1",
            filesystem="manifold-data",     # correct region
            extra_filesystems=["west-scratch"],   # us-west-1
        )
    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert "west-scratch" in detail and "us-west-1" in detail
    # The primary is NOT the offender and must not be blamed for it.
    assert "'manifold-data'" not in detail
    assert mock_client.launch_calls == []


async def test_unknown_extra_is_refused_by_name(orchestrator, mock_client):
    with pytest.raises(LaunchRejected) as exc:
        await orchestrator.request_launch(
            instance_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data",
            extra_filesystems=["crop-archive", "no-such-fs"],
        )
    assert exc.value.status_code == 400
    assert "no-such-fs" in exc.value.detail
    assert mock_client.launch_calls == []


@pytest.mark.parametrize("extras", [
    ["crop-archive", "crop-archive"],     # the same extra twice
    ["manifold-data"],                    # the primary, again
])
def test_duplicate_names_are_refused(client, mock_client, extras):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data", "extra_filesystems": extras,
    })
    assert resp.status_code == 422, resp.text
    assert "twice" in resp.json()["detail"]
    assert mock_client.launch_calls == []


def test_an_empty_name_is_refused_rather_than_read_as_scratch(
        client, mock_client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data", "extra_filesystems": ["crop-archive", ""],
    })
    assert resp.status_code == 422, resp.text
    assert "empty" in resp.json()["detail"]
    assert mock_client.launch_calls == []


def test_the_cap_is_named_in_the_refusal(client, mock_client):
    """Five real, same-region filesystems: what refuses is our cap, and the
    number is in the message so the caller knows what to trim to."""
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data",
        "extra_filesystems": ["crop-archive", "tally-raw", "tally-out",
                              "mesh-cache", "eval-sets"],
    })
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "4" in detail and "5" in detail
    assert mock_client.launch_calls == []


def test_extras_without_a_primary_are_refused(client, mock_client):
    """Extras-without-primary is incoherent: jobs, sync and relative paths
    would still have nowhere to go while real data sat mounted."""
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "", "extra_filesystems": ["crop-archive"],
    })
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "crop-archive" in detail and "primary" in detail
    assert mock_client.launch_calls == []


def test_gcp_with_extras_is_refused_honestly(client, mock_client):
    """GCP is scratch-only, so the guard covers the attach list too - and it
    says why rather than failing later at a provider that cannot mount it."""
    resp = client.post("/instances", json={
        "provider": "gcp", "instance_type": "g2-standard-4",
        "region": "us-central1-a", "filesystem": "",
        "extra_filesystems": ["crop-archive"],
    })
    assert resp.status_code == 400, resp.text
    assert "scratch-only" in resp.json()["detail"]
    assert mock_client.launch_calls == []


# -- the launch row -----------------------------------------------------------


def test_the_row_round_trips_the_list(db):
    launch_id = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
        extra_filesystems=["crop-archive", "eval-sets"],
    )
    assert db.get_launch(launch_id)["extra_filesystems"] == [
        "crop-archive", "eval-sets"]
    assert db.list_launches()[0]["extra_filesystems"] == [
        "crop-archive", "eval-sets"]
    db.update_launch(launch_id, lambda_instance_id="i-1")
    assert db.find_launch_by_instance("i-1")["extra_filesystems"] == [
        "crop-archive", "eval-sets"]


def test_a_launch_without_extras_leaves_the_field_absent(db):
    """Absent, never []. An empty list would claim we looked at this box and
    found no extra mounts; NULL means the row says nothing either way."""
    launch_id = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
    )
    assert "extra_filesystems" not in db.get_launch(launch_id)


def test_a_pre_phase_103_row_reads_absent_after_migration(tmp_path):
    """A database written before the column existed gains it on open, and
    its historical rows report no extras rather than an empty list."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE launches (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
            requested_type TEXT NOT NULL, launched_type TEXT,
            region TEXT NOT NULL, filesystem TEXT,
            connection_mode TEXT NOT NULL, hourly_rate_cents INTEGER,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT, lambda_instance_id TEXT, launched_at TEXT,
            active_at TEXT, terminated_at TEXT
        )""")
    conn.execute(
        "INSERT INTO launches (id, created_at, requested_type, region, "
        "filesystem, connection_mode, status) VALUES ('L1', 'now', "
        "'gpu_1x_a10', 'us-east-1', 'manifold-data', 'direct-ssh', 'active')"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        row = db.get_launch("L1")
        assert "extra_filesystems" not in row
        assert row["filesystem"] == "manifold-data"
    finally:
        db.close()


# -- jobs are unaffected ------------------------------------------------------


@pytest.fixture
def fast_app(tmp_path, mock_client, mock_storage, mock_sidecar):
    settings = make_settings(
        tmp_path,
        tasks=TaskSettings(poll_seconds=0.02),
        idle=IdleSettings(timeout_seconds=600, poll_seconds=60),
        watches=WatchSettings(poll_seconds=60),
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


def test_a_job_on_a_two_filesystem_box_still_runs_on_the_primary(
        fast_app, mock_client):
    """The mount jail and {persistent} are untouched by this phase: a
    template job on a box with two filesystems mounts the PRIMARY, exactly
    as it did on a one-filesystem box, and never the extra."""
    with TestClient(fast_app) as client:
        resp = client.post("/instances", json={
            "instance_type": "gpu_1x_a10", "region": "us-east-1",
            "filesystem": "manifold-data",
            "extra_filesystems": ["crop-archive"],
        })
        assert resp.status_code == 202, resp.text
        launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
        instance_id = launch["lambda_instance_id"]
        wait_until(
            lambda: next(
                i for i in client.get("/instances").json()["instances"]
                if i["id"] == instance_id
            )["connection_state"] == "connected",
            message="SSH connected",
        )

        resp = client.post("/tasks", json={
            "template": "whisper-batch",
            "parameters": {"input_dir": "interviews", "model_size": "small"},
        })
        assert resp.status_code == 202, resp.text
        task_id = resp.json()["task"]["id"]
        task = wait_until(
            lambda: (t := client.get(f"/tasks/{task_id}").json())["status"]
            not in ("queued", "running") and t,
            message="task completion",
        )
        assert task["status"] == "succeeded"
        assert task["output_paths"] == [
            "/lambda/nfs/manifold-data/transcripts",
            "/lambda/nfs/manifold-data/cache/huggingface",
        ]
        lines = [
            l["line"]
            for l in client.get(f"/tasks/{task_id}/logs").json()["lines"]
        ]
        docker_line = next(l for l in lines if "$ docker run" in l)
        assert "-v /lambda/nfs/manifold-data/interviews:/data/input:ro" in (
            docker_line)
        assert "crop-archive" not in docker_line


# -- the MCP surface ----------------------------------------------------------


@pytest.fixture
async def mcp_wired(fast_app):
    """The MCP module's HTTP client pointed at the real app, in-process
    (same wiring as tests/test_mcp.py)."""
    import httpx
    from asgi_lifespan import LifespanManager

    import app.mcp_server as mcp_server

    async with LifespanManager(fast_app) as manager:
        old = mcp_server._client
        mcp_server._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app),
            base_url="http://manifold.test",
        )
        yield mcp_server
        await mcp_server._client.aclose()
        mcp_server._client = old


async def test_mcp_launch_gpu_forwards_extras(mcp_wired, mock_client):
    result = await mcp_wired.launch_gpu(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data",
        extra_filesystems=["crop-archive"],
        purpose="two-dataset join",
        note="phase 103 test",
    )
    assert "error" not in result, result
    wait_until(
        lambda: mock_client.launch_calls,
        message="the launch reaching the provider",
    )
    assert mock_client.launch_calls[0]["filesystem_names"] == [
        "manifold-data", "crop-archive"]


async def test_mcp_extras_hit_the_same_guard_as_the_dashboard(mcp_wired,
                                                              mock_client):
    result = await mcp_wired.launch_gpu(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data",
        extra_filesystems=["west-scratch"],
        purpose="two-dataset join",
    )
    assert "west-scratch" in result["error"]
    assert mock_client.launch_calls == []
