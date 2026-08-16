"""Phase 37: what makes an UNATTENDED run safe.

Three mechanisms, all pinned here:
- approvals     which agent actions pause for a human, and why the default is
                launches only (gating termination loses money, see below)
- notifications a pause or a failure that nobody hears about is a stall
- data safety   covered by test_safety_hook.py; the policy plumbing is here
"""

import time

import pytest

from app.preferences import (
    NOTIFICATION_KINDS,
    ApprovalPrefs,
    DataSafetyPrefs,
    NotificationPrefs,
    Preferences,
    preferences_from_dict,
)
from tests.test_safety_hook import launch_and_wait, wait_connected


# -- preferences (pure) ------------------------------------------------------------


def test_default_policy_gates_launches_only():
    """The load-bearing default. An approval nobody answers AUTO-DENIES, so
    gating an action means "if I am away, this does not happen":

        launch_gpu         not launching costs $0.
        terminate_instance NOT terminating keeps the GPU billing.

    Gating termination therefore burns money exactly when you are away from
    the keyboard - which is when autopilot runs. It is off by default, and
    this test exists so nobody "helpfully" turns it on later.
    """
    assert ApprovalPrefs().gated_actions() == {"launch_gpu"}
    assert ApprovalPrefs().terminate_instance is False
    assert ApprovalPrefs().run_job is False


def test_every_notification_kind_has_a_toggle():
    """The two lists must match BOTH ways.

    A kind in NOTIFICATION_KINDS but missing from NotificationPrefs fails
    wants() and is dropped silently: the feature ships dead. A field on
    NotificationPrefs but missing from NOTIFICATION_KINDS fires fine but is
    absent from /preferences/meta, so no UI can offer its switch: the user
    gets a notification they cannot turn off.

    This test claimed to catch "the next kind somebody adds to only one of
    the two lists" while checking one direction only - and the next kind
    (terminal_reaped, Phase 91) went in the other one and sailed past. Both
    directions now.
    """
    prefs = NotificationPrefs()
    missing_toggle = [k for k in NOTIFICATION_KINDS if not hasattr(prefs, k)]
    assert missing_toggle == [], "listed as a kind, but has no toggle"
    # `desktop` is the OS-ping switch, not a kind: it gates how a
    # notification is delivered, not whether this event notifies at all.
    unlisted = [f for f in vars(prefs)
                if f != "desktop" and f not in NOTIFICATION_KINDS]
    assert unlisted == [], "has a toggle, but is not offered as a kind"
    assert all(prefs.wants(kind) for kind in NOTIFICATION_KINDS)
    assert prefs.wants("no_such_kind") is False


def test_partial_patch_leaves_other_fields_alone():
    base = Preferences()
    updated = preferences_from_dict(base, {"approvals": {"run_job": True}})
    assert updated.approvals.run_job is True
    assert updated.approvals.launch_gpu is True          # untouched
    assert updated.notifications.job_failed is True      # untouched section


@pytest.mark.parametrize("garbage", [
    {"data_safety": {"scope": "everything"}},
    {"data_safety": {"if_unsaveable": "yolo"}},
    {"data_safety": {"max_local_gib": -5}},
    {"approvals": {"nonexistent_action": True}},
    {"notifications": {"desktop": "sure"}},
])
def test_garbage_preferences_never_break_the_backend(garbage):
    """A bad config.yaml or a hostile PUT must clamp, not crash. This file is
    read on every boot; an exception here would be an unstartable app."""
    prefs = preferences_from_dict(Preferences(), garbage)
    assert prefs.data_safety.scope in ("all", "outputs")
    assert prefs.data_safety.if_unsaveable in ("block", "terminate")
    assert prefs.data_safety.max_local_gib >= 0
    assert not hasattr(prefs.approvals, "nonexistent_action")


def test_scope_and_budget_are_pure_decisions():
    """plan_local_transfer does no I/O: it can be reasoned about without an
    instance, an SSH server, or a byte of network."""
    from app.data_safety import GIB, plan_local_transfer
    files = [
        {"path": "checkpoints/big.safetensors", "size_bytes": 4 * GIB},
        {"path": "outputs/result.jsonl", "size_bytes": 1024},
        {"path": "outputs/samples/a.png", "size_bytes": 2048},
    ]
    # Smallest first, so a limited budget saves the many, not the one.
    plan = plan_local_transfer(files, scope="all", max_bytes=1 * GIB)
    assert [f["path"] for f in plan.download] == [
        "outputs/result.jsonl", "outputs/samples/a.png"]
    assert len(plan.skipped) == 1
    assert "budget" in plan.skipped[0]["reason"]

    plan = plan_local_transfer(files, scope="outputs", max_bytes=100 * GIB)
    assert all(f["path"].startswith("outputs/") for f in plan.download)
    assert plan.skipped[0]["path"] == "checkpoints/big.safetensors"


def test_a_hostile_sidecar_cannot_escape_the_rescue_directory(tmp_path):
    """Paths come FROM the instance. A traversal must not let one write over
    the user's home directory (or read /etc/shadow on the way out)."""
    from app.data_safety import local_path, remote_path
    with pytest.raises(ValueError):
        local_path(str(tmp_path), "i-1", "../../../../etc/passwd")
    with pytest.raises(ValueError):
        remote_path("../../etc/shadow")
    # The ordinary case still resolves where you would expect.
    assert local_path(str(tmp_path), "i-1", "outputs/a.png") == (
        tmp_path / "i-1" / "outputs" / "a.png")
    assert remote_path("outputs/a.png") == "/workspace/ephemeral/outputs/a.png"


# -- preferences over HTTP ---------------------------------------------------------


def test_preferences_round_trip(client):
    body = client.get("/preferences").json()
    assert body["preferences"]["approvals"]["launch_gpu"] is True
    assert body["gateable_actions"] == [
        "launch_gpu", "run_job", "terminate_instance"]

    client.put("/preferences", json={
        "approvals": {"terminate_instance": True},
        "data_safety": {"to_local": True, "scope": "outputs"},
    })
    after = client.get("/preferences").json()["preferences"]
    assert after["approvals"]["terminate_instance"] is True
    assert after["approvals"]["launch_gpu"] is True       # merged, not replaced
    assert after["data_safety"]["scope"] == "outputs"
    assert after["data_safety"]["to_filesystem"] is True  # untouched


def test_preferences_survive_a_restart(client, settings, mock_client,
                                       mock_storage, mock_sidecar, mock_model):
    """Stored in the database, not in memory: the whole point is that an
    unattended run days later still honors what you chose."""
    from fastapi.testclient import TestClient
    from app.image_checker import MockImageChecker
    from app.main import create_app
    from tests.conftest import mock_connect_fn

    client.put("/preferences", json={"data_safety": {"if_unsaveable": "terminate"}})

    app = create_app(
        settings, lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        model_client_factory=lambda conn: mock_model,
        image_checker=MockImageChecker(),
        notification_sender=lambda t, b: None,
    )
    with TestClient(app) as reborn:
        prefs = reborn.get("/preferences").json()["preferences"]
        assert prefs["data_safety"]["if_unsaveable"] == "terminate"


# -- notifications -----------------------------------------------------------------


def test_job_failure_pings_and_is_recorded(client, os_pings):
    """A job that fails at 3am must reach the user. One funnel in the
    dispatcher means no completion path can finish silently."""
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    # A template with a parameter it cannot satisfy fails at dispatch.
    resp = client.post("/tasks", json={
        "template": "script-run", "parameters": {}})
    assert resp.status_code == 422      # caught even earlier: at enqueue

    # Force a dispatch-time failure instead: a template that no longer exists.
    task_id = client.app.state.queue.enqueue(
        template="ghost-template", parameters={})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.app.state.queue.get(task_id)["status"] == "failed":
            break
        time.sleep(0.02)
    assert client.app.state.queue.get(task_id)["status"] == "failed"

    notifications = client.get("/notifications").json()
    assert notifications["unread"] >= 1
    failed = [n for n in notifications["notifications"]
              if n["kind"] == "job_failed"]
    assert failed and failed[0]["ref"] == task_id


def test_notification_toggles_are_honored(client, os_pings):
    """Switching a kind off stops the ping AND the row - the toggle is the
    whole feature the user asked for."""
    client.put("/preferences", json={"notifications": {"job_failed": False}})
    task_id = client.app.state.queue.enqueue(template="ghost", parameters={})
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.app.state.queue.get(task_id)["status"] == "failed":
            break
        time.sleep(0.02)

    kinds = [n["kind"] for n in
             client.get("/notifications").json()["notifications"]]
    assert "job_failed" not in kinds


def test_desktop_toggle_silences_the_os_ping_but_keeps_history(client, os_pings):
    client.put("/preferences", json={"notifications": {"desktop": False}})
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    client.delete(f"/instances/{instance_id}")          # rescues -> notifies

    assert os_pings == []                               # nothing left the app
    kinds = [n["kind"] for n in
             client.get("/notifications").json()["notifications"]]
    assert "data_transferred" in kinds                  # but it IS recorded


def test_marking_read_clears_the_badge(client):
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    client.delete(f"/instances/{instance_id}")
    assert client.get("/notifications").json()["unread"] >= 1

    client.post("/notifications/read", json={})         # no ids = all
    assert client.get("/notifications").json()["unread"] == 0
