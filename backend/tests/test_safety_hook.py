"""The pre-termination data rescue and safety hook, end to end over HTTP.

Phase 37 changed this contract deliberately. Termination used to REFUSE while
valuable files sat on the instance's scratch disk; now it RESCUES them first
(per the data-safety policy) and only refuses if something could not be saved.
Refusing was the right answer with a human watching and the wrong one at 3am,
when an unattended run would just leave the GPU billing against a 409.
"""

from tests.conftest import cannot_rescue, set_data_safety, wait_for_launch_status


def launch_and_wait(client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10",
        "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    assert resp.status_code == 202
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    assert launch["status"] == "active"
    return launch["lambda_instance_id"]


def wait_connected(client, instance_id, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        inst = next(
            i for i in client.get("/instances").json()["instances"]
            if i["id"] == instance_id
        )
        if inst["connection_state"] == "connected":
            return
        time.sleep(0.02)
    raise AssertionError("never connected")


def test_terminate_rescues_the_data_then_proceeds(client, mock_client, os_pings):
    """The default policy: sync scratch to the persistent volume, then
    terminate. The user gets their GPU stopped AND their files kept."""
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)

    resp = client.delete(f"/instances/{instance_id}")
    assert resp.status_code == 200, resp.text
    rescue = resp.json()["rescue"]
    assert rescue["attempted"] is True
    assert rescue["files_found"] == 2
    assert rescue["synced_to"] == "/lambda/nfs/manifold-data/ephemeral-backup/"
    # A successful sync copies the WHOLE scratch dir, so nothing is at risk.
    assert rescue["unsaved"] == []
    assert mock_client.instances[instance_id].status == "terminated"
    # And the user was told their data moved.
    assert any("Saved data" in title for title, _ in os_pings)


def test_terminate_blocked_when_the_data_cannot_be_saved(
        client, mock_client, os_pings):
    """The block still exists — it just means something now. With nowhere to
    put the files, termination refuses and the instance keeps running."""
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    cannot_rescue(client)

    resp = client.delete(f"/instances/{instance_id}")
    assert resp.status_code == 409
    body = resp.json()
    assert body["blocked"] is True
    paths = [f["path"] for f in body["unpersisted_files"]]
    assert "checkpoints/step-2000.safetensors" in paths
    # And the instance is genuinely still running.
    assert mock_client.instances[instance_id].status == "active"
    # A block that nobody hears about is a stall, not a safeguard.
    assert any("left running to protect data" in title for title, _ in os_pings)


def test_blocked_termination_notifies_once_until_the_files_change(
        client, mock_client, mock_sidecar, os_pings):
    """Phase 76b (F7): a blocked termination is retried forever by the idle
    loop (~15s) and the auto-manage loop (faster), and every retry used to
    fire a fresh in-app row AND an OS desktop ping — about four notifications
    a minute, indefinitely, all saying the same thing.

    One ping per blocked instance until the unsaved file set CHANGES. A
    changed set is real news ("you saved one of the two"); an identical set
    is the same block we already reported.
    """
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    cannot_rescue(client)

    def blocked_pings():
        return [t for t, _ in os_pings if "left running to protect data" in t]

    assert client.delete(f"/instances/{instance_id}").status_code == 409
    assert len(blocked_pings()) == 1

    # Same two files, three more retries: still exactly one ping, and no new
    # in-app rows either (the row and the ping share one funnel).
    for _ in range(3):
        assert client.delete(f"/instances/{instance_id}").status_code == 409
    assert len(blocked_pings()) == 1
    rows = [n for n in client.get("/notifications").json()["notifications"]
            if n["kind"] == "data_transferred" and n["ref"] == instance_id]
    assert len(rows) == 1

    # The user saves one of the two. The situation changed, so say so.
    mock_sidecar.unpersisted = mock_sidecar.unpersisted[:1]
    assert client.delete(f"/instances/{instance_id}").status_code == 409
    assert len(blocked_pings()) == 2
    assert "1 file(s) could not be saved" in os_pings[-1][1]

    # ...and the new state then dedupes on its own terms.
    assert client.delete(f"/instances/{instance_id}").status_code == 409
    assert len(blocked_pings()) == 2
    assert mock_client.instances[instance_id].status == "active"


def test_if_unsaveable_terminate_chooses_the_wallet(client, mock_client):
    """The opposite policy: stop the billing, lose the files, say so."""
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    set_data_safety(client, to_filesystem=False, to_local=False,
                    if_unsaveable="terminate")

    resp = client.delete(f"/instances/{instance_id}")
    assert resp.status_code == 200
    assert len(resp.json()["rescue"]["unsaved"]) == 2
    assert mock_client.instances[instance_id].status == "terminated"
    actions = [e["action"] for e in client.get("/audit").json()["entries"]]
    assert "terminate_data_lost" in actions


def test_rescue_downloads_to_this_machine(client, tmp_path, mock_client):
    """to_local pulls the files down over the managed connection, into
    <local_dir>/<instance id>/<path on the instance>."""
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    # Seed the mock SFTP store the way mock mode does (the conftest connection
    # is bare), so there is really something to fetch.
    from app.orchestrator import Orchestrator  # noqa: F401  (typing only)
    conn = client.app.state.orchestrator.connections[instance_id]
    ssh = conn.ssh_connection()
    ssh.sftp_files["/workspace/ephemeral/checkpoints/step-2000.safetensors"] = b"weights"
    ssh.sftp_files["/workspace/ephemeral/outputs/samples/grid-final.png"] = b"pixels"

    set_data_safety(client, to_filesystem=False, to_local=True,
                    local_dir=str(tmp_path))
    resp = client.post(f"/instances/{instance_id}/rescue")
    assert resp.status_code == 200, resp.text
    rescue = resp.json()["rescue"]
    assert rescue["unsaved"] == []
    assert {d["path"] for d in rescue["downloaded"]} == {
        "checkpoints/step-2000.safetensors",
        "outputs/samples/grid-final.png",
    }
    landed = tmp_path / instance_id / "checkpoints" / "step-2000.safetensors"
    assert landed.read_bytes() == b"weights"
    # A rescue is not a termination: the box is still up.
    assert mock_client.instances[instance_id].status == "active"


def test_outputs_scope_leaves_the_checkpoint_behind(client, tmp_path):
    """scope=outputs saves the deliverables and skips the 4 GiB checkpoint —
    and says so, rather than pretending everything is safe."""
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    ssh = client.app.state.orchestrator.connections[instance_id].ssh_connection()
    ssh.sftp_files["/workspace/ephemeral/outputs/samples/grid-final.png"] = b"pixels"

    set_data_safety(client, to_filesystem=False, to_local=True,
                    scope="outputs", local_dir=str(tmp_path))
    rescue = client.post(f"/instances/{instance_id}/rescue").json()["rescue"]

    assert [d["path"] for d in rescue["downloaded"]] == [
        "outputs/samples/grid-final.png"]
    skipped = {s["path"]: s["reason"] for s in rescue["skipped"]}
    assert "outside the 'outputs' rescue scope" in skipped[
        "checkpoints/step-2000.safetensors"]
    # It was NOT saved, so it is honestly reported as still at risk.
    assert [f["path"] for f in rescue["unsaved"]] == [
        "checkpoints/step-2000.safetensors"]


def test_local_transfer_budget_skips_what_does_not_fit(client, tmp_path):
    """A 4 GiB checkpoint against a 1 GiB budget is skipped WITH A REASON.
    A rescue that quietly drops files is worse than none: it lies."""
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    ssh = client.app.state.orchestrator.connections[instance_id].ssh_connection()
    ssh.sftp_files["/workspace/ephemeral/outputs/samples/grid-final.png"] = b"pixels"

    set_data_safety(client, to_filesystem=False, to_local=True,
                    max_local_gib=1.0, local_dir=str(tmp_path))
    rescue = client.post(f"/instances/{instance_id}/rescue").json()["rescue"]

    # The 8 MB image fits; the 4 GiB checkpoint does not.
    assert [d["path"] for d in rescue["downloaded"]] == [
        "outputs/samples/grid-final.png"]
    reason = next(s["reason"] for s in rescue["skipped"]
                  if s["path"] == "checkpoints/step-2000.safetensors")
    assert "budget" in reason


def test_force_terminate_bypasses_hook(client, mock_sidecar, mock_client):
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)

    resp = client.delete(f"/instances/{instance_id}", params={"force": "true"})
    assert resp.status_code == 200
    assert mock_client.instances[instance_id].status == "terminated"


def test_sync_then_terminate(client, mock_sidecar, mock_client):
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)

    # Sync runs rsync over the managed connection...
    resp = client.post(f"/instances/{instance_id}/sync")
    assert resp.status_code == 200
    assert resp.json()["synced_to"] == "/lambda/nfs/manifold-data/ephemeral-backup/"

    # ...after which nothing is left to rescue, so terminate passes even under
    # the strictest policy (nowhere to put files, block if any are at risk).
    cannot_rescue(client)
    mock_sidecar.clear_unpersisted()
    resp = client.delete(f"/instances/{instance_id}")
    assert resp.status_code == 200
    assert resp.json()["rescue"]["files_found"] == 0
    assert mock_client.instances[instance_id].status == "terminated"


def test_terminate_proceeds_when_no_connection(client, mock_client, mock_sidecar):
    """An instance with no managed connection (e.g. found already running at
    startup) must still be terminable — the hook is evidence, not a wedge."""
    from app.lambda_api import InstanceInfo
    mock_client.instances["orphan"] = InstanceInfo(
        id="orphan", name="orphan", status="active", ip="192.0.2.99",
        region="us-east-1", instance_type="gpu_1x_a10", hourly_rate_cents=129,
    )
    resp = client.delete("/instances/orphan")
    assert resp.status_code == 200
    assert mock_client.instances["orphan"].status == "terminated"


def test_metrics_endpoint_relays_sidecar(client, mock_sidecar):
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    body = client.get(f"/instances/{instance_id}/metrics").json()
    assert body["available"] is True
    assert body["gpus"][0]["name"] == "Mock A10"


def test_metrics_stream_websocket_relays(client, mock_sidecar):
    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    with client.websocket_connect(f"/instances/{instance_id}/metrics/stream") as ws:
        payload = ws.receive_json()
    assert payload["gpus"][0]["vram_total_mib"] == 24564
