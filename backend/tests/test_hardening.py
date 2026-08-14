"""Phase 16 hardening: honest job exit codes (pipefail), idle keep-alive,
and the idle countdown the dashboard shows. Driven by real-hardware
findings: crashed containers reported "succeeded", and an instance was
idle-terminated mid-test-session with no warning."""

import sqlite3
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app.config import IdleSettings, TaskSettings, WatchSettings
from app.db import Database
from app.dispatcher import wrap_remote_command
from app.main import create_app
from tests.conftest import make_settings, mock_connect_fn, wait_for_launch_status
from tests.test_dispatch_flow import wait_until


# -- pipefail: the container's exit code must survive the tee pipe ----------------


def test_wrap_remote_command_propagates_container_exit_code(tmp_path):
    """Execute the REAL wrapper in a REAL shell. Before the fix the pipeline
    reported tee's exit code (always 0), so every crashed job showed green."""
    log = tmp_path / "task-logs" / "t1.log"
    cmd = wrap_remote_command(
        "echo boom && exit 7", str(log),
        ensure_dirs=[str(tmp_path / "task-logs")],
    )
    result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert result.returncode == 7          # the container's code, not tee's
    assert "boom" in log.read_text()       # output still teed to the log


def test_wrap_remote_command_success_still_zero(tmp_path):
    log = tmp_path / "task-logs" / "t2.log"
    cmd = wrap_remote_command(
        "echo all-good", str(log),
        ensure_dirs=[str(tmp_path / "task-logs")],
    )
    result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    assert result.returncode == 0
    assert "all-good" in log.read_text()


def test_dispatched_command_includes_pipefail(client):
    """The wrapper actually reaches the instance: the command recorded by the
    mock SSH connection carries set -o pipefail."""
    from tests.test_reconcile import launch_connected

    _, instance_id = launch_connected(client)
    resp = client.post("/tasks", json={
        "template": "gpu-smoke", "parameters": {"note": "hardened"},
    })
    assert resp.status_code == 202
    task_id = resp.json()["task"]["id"]
    wait_until(
        lambda: client.get(f"/tasks/{task_id}").json()["status"]
        not in ("queued", "running"),
        message="task completion",
    )
    conn = client.app.state.orchestrator.connections[instance_id]
    ssh = conn.ssh_connection()
    # "docker run" alone now also matches the GPU-readiness probe (its last
    # stage walks through the container door on purpose); the dispatch is
    # the one that carries the task container's --name.
    wrapped = next(c for c in ssh.commands
                   if "docker run" in c and "manifold-task-" in c)
    # Restart-proof shape: detached runner + exit-file wait, not a pipeline
    # that ties the container's life to the SSH session.
    assert "nohup bash -c" in wrapped
    assert ".exit" in wrapped


# -- idle keep-alive and countdown -------------------------------------------------


@pytest.fixture
def idle_app(tmp_path, mock_client, mock_storage, mock_sidecar):
    settings = make_settings(
        tmp_path,
        tasks=TaskSettings(poll_seconds=0.02),
        idle=IdleSettings(timeout_seconds=0.4, poll_seconds=0.05),
        watches=WatchSettings(poll_seconds=0.05),
    )
    return create_app(
        settings,
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )


def _launch_connected(client):
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
    return instance_id


def test_keep_alive_blocks_idle_termination(idle_app, mock_client):
    with TestClient(idle_app) as client:
        instance_id = _launch_connected(client)

        # Switch auto-termination off, then outlive the idle timeout by 3x.
        resp = client.post(f"/instances/{instance_id}/keep-alive",
                           json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["keep_alive"] is True
        time.sleep(1.2)
        assert mock_client.instances[instance_id].status == "active"

        # The card reflects it, and the switch is persisted on the launch row.
        inst = next(i for i in client.get("/instances").json()["instances"]
                    if i["id"] == instance_id)
        assert inst["idle"]["keep_alive"] is True
        db = idle_app.state.orchestrator.db
        assert db.find_launch_by_instance(instance_id)["keep_alive"] == 1

        # Switch it back on: the idle loop terminates as before.
        client.post(f"/instances/{instance_id}/keep-alive",
                    json={"enabled": False})
        wait_until(
            lambda: mock_client.instances[instance_id].status == "terminated",
            timeout=10.0, message="idle termination after keep-alive off",
        )
        actions = [r["action"] for r in db._execute(
            "SELECT action FROM audit_log ORDER BY id").fetchall()]
        assert "keep_alive" in actions
        assert "idle_termination" in actions


def test_instances_expose_idle_countdown(idle_app):
    with TestClient(idle_app) as client:
        instance_id = _launch_connected(client)
        inst = next(i for i in client.get("/instances").json()["instances"]
                    if i["id"] == instance_id)
        idle = inst["idle"]
        assert idle is not None
        # Phase 76b added the max-lifetime ceiling and deliberately did NOT
        # put it in here. `idle` is null whenever the instance is not
        # connected, and a box that has dropped off SSH past its ceiling is
        # exactly the one whose limit the user needs to see — so the ceiling
        # fields live on the INSTANCE, asserted just below. This exact-set
        # assertion is what keeps them from drifting back inside.
        assert set(idle) == {"idle_seconds", "timeout_seconds", "keep_alive"}
        assert idle["keep_alive"] is False
        assert idle["idle_seconds"] >= 0
        assert inst["max_lifetime_seconds"] is None      # off by default
        assert inst["ceiling_seconds_remaining"] is None
        assert inst["ceiling_deferred_by"] is None


# -- db migration -------------------------------------------------------------------


def test_existing_db_gains_keep_alive_column(tmp_path):
    """A database created before the keep_alive column existed is migrated
    in place on open (ALTER TABLE), with existing rows defaulting to 0."""
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
        "connection_mode, status) VALUES ('L1', 'now', 'gpu_1x_a10', "
        "'us-east-1', 'direct-ssh', 'active')"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        row = db._execute("SELECT * FROM launches WHERE id='L1'").fetchone()
        assert row["keep_alive"] == 0
        db.update_launch("L1", keep_alive=1)
        row = db._execute("SELECT * FROM launches WHERE id='L1'").fetchone()
        assert row["keep_alive"] == 1
    finally:
        db.close()
