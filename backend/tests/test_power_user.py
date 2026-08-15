"""Phase 39: power without training wheels.

- Guardrail NUMBERS editable from Settings (the guards stay in the
  orchestrator; only the limits move).
- Scratch-only launches: no filesystem required, data-safety still applies.
- Custom job templates: user/agent-authored YAML, same validation jail as
  bundled templates, live without a restart.
- run_command: SSH parity through the guarded gateway, audited.
"""

import time

from tests.conftest import wait_for_launch_status
from tests.test_safety_hook import launch_and_wait, wait_connected


# -- guardrails from Settings --------------------------------------------------------


def test_settings_guardrails_override_config(client):
    """config.yaml says 1 concurrent instance; the Settings page can raise
    it without a YAML edit or a restart. The guard itself still runs."""
    launch_and_wait(client)

    # Second launch: rejected by the config default (limit 1), and the
    # message points at Settings, not at a file.
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data"})
    assert resp.status_code == 409
    assert "Spending guardrails" in resp.json()["detail"]

    # Raise both limits in Settings (spend too: two A10s exceed $4/hr? No -
    # $2.58 - but a third guard test below needs headroom anyway).
    client.put("/preferences", json={
        "guardrails": {"max_concurrent_instances": 2}})
    launch_and_wait(client)   # now admitted

    # The guard is still a guard: a third instance is refused at the NEW limit.
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data"})
    assert resp.status_code == 409
    assert "limit is 2" in resp.json()["detail"]


def test_zero_means_config_default(client):
    """Guardrail prefs of 0 mean 'unset': the config value applies."""
    client.put("/preferences", json={
        "guardrails": {"max_concurrent_instances": 0,
                       "max_hourly_spend_usd": 0}})
    launch_and_wait(client)
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data"})
    assert resp.status_code == 409          # config default (1) still binds


# -- scratch-only launches -----------------------------------------------------------


def launch_scratch_only(client) -> str:
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-2",
        "filesystem": ""})
    assert resp.status_code == 202, resp.text
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    assert launch["status"] == "active"
    return launch["lambda_instance_id"]


def test_launch_without_filesystem(client):
    """A region with no filesystem is launchable: filesystem is optional.
    us-east-2 has capacity in the mock catalog but no filesystem."""
    instance_id = launch_scratch_only(client)
    inst = next(i for i in client.get("/instances").json()["instances"]
                if i["id"] == instance_id)
    assert inst["status"] == "active"


def test_scratch_only_jobs_need_a_filesystem(client):
    """A job on a scratch-only instance fails with a clear reason instead of
    dispatching into nowhere ({persistent} has no target)."""
    instance_id = launch_scratch_only(client)
    wait_connected(client, instance_id)
    task_id = client.post("/tasks", json={
        "template": "gpu-smoke", "parameters": {},
    }).json()["task"]["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = client.get(f"/tasks/{task_id}").json()
        if task["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.02)
    assert task["status"] == "failed"
    # The refusal must explain itself: the cause (scratch-only), the
    # template's need (persistent storage), and both ways out - the old
    # "no filesystem recorded" was correct behaviour wearing an opaque
    # message, found live at the 2026-08-15 GCE gate.
    assert "scratch-only" in (task["error"] or "")
    assert "persistent storage" in (task["error"] or "")


def test_scratch_only_termination_still_protects_data(client, mock_client):
    """The promise the launch form makes: scratch data is not silently lost.
    With files on the box and nowhere to sync (no filesystem), the default
    policy blocks termination; force is the explicit override."""
    instance_id = launch_scratch_only(client)
    wait_connected(client, instance_id)

    resp = client.delete(f"/instances/{instance_id}")
    assert resp.status_code == 409
    body = resp.json()
    assert body["blocked"] is True
    # The rescue honestly reports WHY it could not sync.
    assert "no filesystem" in body["rescue"]["sync_error"]

    resp = client.delete(f"/instances/{instance_id}", params={"force": "true"})
    assert resp.status_code == 200
    assert mock_client.instances[instance_id].status == "terminated"


# -- custom templates ----------------------------------------------------------------


CUSTOM_YAML = """
name: sketch-to-3d
description: Turn 2D concept images into 3D mesh renders.
image: pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime
command: >-
  bash -c 'echo rendering {{input_dir}} at {{resolution}}' argv0
parameters:
  - name: input_dir
    type: string
    description: Directory of source images under the filesystem
  - name: resolution
    type: integer
    description: Output resolution
    default: 512
volumes:
  - host: "{persistent}/renders"
    container: /out
"""

EVIL_YAML = """
name: exfiltrate
description: Mount the host filesystem.
image: alpine
command: cat /host/etc/passwd
volumes:
  - host: /etc
    container: /host/etc
"""


def test_custom_template_lifecycle(client):
    """Create -> visible+runnable -> delete, all without a restart."""
    resp = client.post("/templates/custom", json={"yaml": CUSTOM_YAML})
    assert resp.status_code == 201, resp.text
    assert resp.json()["template"]["custom"] is True

    listed = client.get("/templates").json()["templates"]
    mine = next(t for t in listed if t["name"] == "sketch-to-3d")
    assert mine["custom"] is True
    assert "sketch-to-3d" in mine["yaml"] or "name: sketch-to-3d" in mine["yaml"]
    # Bundled templates are not editable and say so.
    assert next(t for t in listed if t["name"] == "gpu-smoke")["custom"] is False

    # Runnable through the normal path: parameter validation applies.
    resp = client.post("/tasks", json={
        "template": "sketch-to-3d",
        "parameters": {"resolution": "not-a-number"}})
    assert resp.status_code == 422
    resp = client.post("/tasks", json={
        "template": "sketch-to-3d",
        "parameters": {"input_dir": "concepts/robot"}})
    assert resp.status_code == 202

    resp = client.delete("/templates/custom/sketch-to-3d")
    assert resp.status_code == 200
    names = [t["name"] for t in client.get("/templates").json()["templates"]]
    assert "sketch-to-3d" not in names


def test_custom_template_same_jail_as_bundled(client):
    """The mount jail binds custom templates identically: a template asking
    for /etc never becomes launchable, whoever wrote it."""
    resp = client.post("/templates/custom", json={"yaml": EVIL_YAML})
    assert resp.status_code == 422
    assert "/etc" in resp.json()["detail"]
    names = [t["name"] for t in client.get("/templates").json()["templates"]]
    assert "exfiltrate" not in names


def test_bundled_templates_cannot_be_deleted(client):
    resp = client.delete("/templates/custom/gpu-smoke")
    assert resp.status_code == 400
    assert "bundled" in resp.json()["detail"]


def test_custom_template_survives_restart(client, settings, mock_client,
                                          mock_storage, mock_sidecar,
                                          mock_model):
    """Templates are files, not rows: a new app process loads them again."""
    from fastapi.testclient import TestClient
    from app.image_checker import MockImageChecker
    from app.main import create_app
    from tests.conftest import mock_connect_fn, tmp_path_factory_dir

    client.post("/templates/custom", json={"yaml": CUSTOM_YAML})
    app = create_app(
        settings, lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        model_client_factory=lambda conn: mock_model,
        image_checker=MockImageChecker(),
        notification_sender=lambda t, b: None,
        custom_templates_dir=tmp_path_factory_dir(settings),
    )
    with TestClient(app) as reborn:
        names = [t["name"]
                 for t in reborn.get("/templates").json()["templates"]]
        assert "sketch-to-3d" in names


# -- run_command ---------------------------------------------------------------------


def test_run_command_executes_and_audits(client):
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)

    resp = client.post(f"/instances/{instance_id}/run",
                       json={"command": "nvidia-smi"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["exit_code"] == 0
    assert "nvidia-smi" in body["stdout"]      # mock echoes the command

    # Audited with the command and its exit code - the whole point.
    entries = client.get("/audit").json()["entries"]
    hit = next(e for e in entries if e["action"] == "instance_command")
    assert "nvidia-smi" in hit["detail"] and "exit 0" in hit["detail"]


def test_run_command_needs_a_connection(client):
    resp = client.post("/instances/ghost/run", json={"command": "ls"})
    assert resp.status_code == 409


# -- instance rename -----------------------------------------------------------------


def test_rename_instance_overlays_lambda_name(client):
    instance_id = launch_and_wait(client)

    client.post(f"/instances/{instance_id}/name", json={"name": "render-box"})
    inst = next(i for i in client.get("/instances").json()["instances"]
                if i["id"] == instance_id)
    assert inst["name"] == "render-box"

    # Empty restores Lambda's launch-time name.
    client.post(f"/instances/{instance_id}/name", json={"name": ""})
    inst = next(i for i in client.get("/instances").json()["instances"]
                if i["id"] == instance_id)
    assert inst["name"] != "render-box"


# -- unlimited autopilot steps -------------------------------------------------------


def test_unlimited_steps_run(tmp_path):
    """unlimited_steps=true stores max_steps=0 and the loop keeps going past
    the finite default until the brain says done."""
    from fastapi.testclient import TestClient
    from tests.test_local_hub import DONE, approval_app, wait_run

    wait_line = '{"thought": "poll", "action": "wait", "args": {"seconds": 0}}'
    app, _, _ = approval_app(tmp_path, [wait_line] * 60 + [DONE])
    with TestClient(app) as client:
        run = client.post("/autopilot/runs", json={
            "goal": "Poll for a while, then stop.",
            "brain": "local:x/scripted",
            "unlimited_steps": True,
            "approve_actions": [],
        }).json()["run"]
        assert run["max_steps"] == 0

        done = wait_run(client, run["id"], timeout=20)
        assert done["status"] == "succeeded"
        # 61 steps: over the 50-step hard cap a finite run could never pass.
        assert done["steps_taken"] == 61


# -- autopilot authors its own template ------------------------------------------------


def test_autopilot_can_save_a_template(tmp_path):
    """The save_template action: a run whose goal fits no bundled template
    authors one mid-run, and it persists for the user afterwards."""
    import json as _json
    from fastapi.testclient import TestClient
    from tests.test_local_hub import DONE, approval_app, wait_run
    from tests.test_power_user import CUSTOM_YAML  # noqa: PLW0406 (self)

    save_line = _json.dumps({
        "thought": "no bundled template renders sketches; make one",
        "action": "save_template",
        "args": {"yaml": CUSTOM_YAML},
    })
    app, _, _ = approval_app(tmp_path, [save_line, DONE])
    with TestClient(app) as client:
        run = client.post("/autopilot/runs", json={
            "goal": "Create a sketch-to-3d pipeline template.",
            "brain": "local:x/scripted",
            "approve_actions": [],
        }).json()["run"]
        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"
        step = next(s for s in done["steps"]
                    if s["action"] == "save_template")
        assert step["result"]["saved"] == "sketch-to-3d"

        listed = client.get("/templates").json()["templates"]
        mine = next(t for t in listed if t["name"] == "sketch-to-3d")
        assert mine["custom"] is True
