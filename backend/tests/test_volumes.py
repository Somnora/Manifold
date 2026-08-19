"""Phase 112: GCP data volumes, and the fake rescue they would have caused.

THE VECTOR THIS PHASE IS ABOUT. `sync_ephemeral` ran `mkdir -p <dest> &&
rsync ...` with no check that <dest> was a mount. `rescue` sets unsaved=[]
the moment synced_to is set, and the termination safety hook reads unsaved
to decide whether destroying a box is safe. So an UNMOUNTED
/lambda/nfs/<name> meant: rsync silently creates a directory on the
auto-delete boot disk, the rescue reports everything saved, the hook waves
the terminate through, and the user's data burns while the audit log says it
was rescued.

Before this phase that could not happen on GCP - only because sync_ephemeral
raised honestly ("no filesystem recorded"). Populating the `filesystem`
column for GCP volumes removes that accident, so the protection is now
explicit and unconditional (Lambda included, where the same thing happens if
an NFS mount ever falls off).

test_an_unmounted_destination_fails_the_rescue_instead_of_faking_it is the
point of the phase. Everything else here holds it up.

What CANNOT be proven without real hardware: that mkfs and mount actually
work on a GCE Persistent Disk over the managed connection. The decisions are
pure and pinned here; the transport is real-gate work.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess

import pytest

from app import volumes as vol
from app.config import LaunchPolicy
from app.connections import MockSSHConnection
from app.db import utcnow
from app.dispatcher import wrap_remote_command
from app.lambda_api import MockLambdaClient
from app.orchestrator import LaunchRejected, Orchestrator
from app.providers.gcp_provider import MockGCPProvider, RealGCPProvider
from app.providers.registry import ProviderRegistry
from tests.conftest import make_settings, mock_connect_fn, wait_for_launch_status
from tests.test_launch import wait_for_connection
from tests.test_provisioning_gate import Box, ScriptedSSH


# -- the name, before it touches a shell or a device path -------------------------


def test_the_gce_charset_is_enforced_not_paraphrased():
    """These names are the first user-supplied strings this codebase
    interpolates unquoted into shell commands and into
    /dev/disk/by-id/google-<name>. Lambda's names came from Lambda's
    registry; these come from Manifold's own route, so the route proves the
    charset."""
    assert vol.validate_name("data-2026") is None
    assert vol.validate_name("ab") is None
    assert vol.validate_name("a" * 63) is None
    for bad in ["", "a", "Data", "9lives", "trailing-", "under_score",
                "has space", "../../etc/passwd", "a" * 64,
                "x; rm -rf /", "$(whoami)"]:
        assert vol.validate_name(bad) is not None, bad


def test_size_bounds_refuse_the_extra_digit():
    assert vol.validate_size_gb(200) is None
    assert "minimum" in vol.validate_size_gb(1)
    assert "ceiling" in vol.validate_size_gb(50_000)


def test_the_device_path_is_by_id_never_a_kernel_letter():
    """/dev/sdb is enumeration-ordered: the letter that meant the data disk
    last boot can mean the boot disk this one, and mkfs against the wrong
    letter is unrecoverable."""
    assert vol.device_link("crops") == "/dev/disk/by-id/google-crops"
    assert "sd" not in vol.device_link("crops").rsplit("/", 1)[-1]
    assert vol.mount_path("crops") == "/lambda/nfs/crops"


# -- the decision table (pure) ----------------------------------------------------


def plan(**over):
    """plan_prepare with the healthy defaults, one cell overridden."""
    args = dict(name="crops", known=True, link_present=True, blkid_ok=True,
                blkid_output="DEVNAME=/dev/sdb\nTYPE=ext4\n",
                formatted_at="2026-08-19T00:00:00+00:00",
                recorded_fstype="ext4")
    args.update(over)
    return vol.plan_prepare(**args)


def test_blank_disk_and_no_record_is_the_one_permitted_mkfs():
    p = plan(blkid_output="", formatted_at=None, recorded_fstype=None)
    assert p.action == "format"
    assert p.fstype == "ext4"


def test_blank_disk_but_recorded_formatted_is_a_loud_terminal_refusal():
    """An interrupted mkfs is indistinguishable from never-formatted, so it
    is never retried.

    The advice ("delete it and create another") comes with its premise
    attached rather than as proof. formatted_at transitions exactly once,
    before the mkfs, so MANIFOLD'S RECORDS show it never mounted the volume
    and never wrote to it - but a disk formatted, filled and later wiped
    externally looks identical from here, and the by-id link can point at a
    different device than we think (the type-mismatch cell exists for
    exactly that). Scoped to what Manifold can actually know."""
    p = plan(blkid_output="", formatted_at="2026-08-19T00:00:00+00:00")
    assert p.action == "refuse"
    assert p.terminal is True
    assert "interrupted mkfs" in p.detail
    assert "Manifold's own records" in p.detail
    assert "outside Manifold" in p.detail
    assert "delete it and create another" in p.detail
    # Never the unprovable form: a blank blkid NOW is not evidence about
    # every byte that was ever on the disk.
    assert "provably" not in p.detail


def test_a_filesystem_we_did_not_write_is_never_mounted_and_never_formatted():
    """Hand-formatted out of band, a restored older SQLite, or a future
    attach-existing feature. Mounting read-write and pointing jobs at it
    corrupts foreign data; formatting destroys it."""
    p = plan(formatted_at=None, recorded_fstype=None)
    assert p.action == "refuse"
    assert p.terminal is True
    assert "no record of writing" in p.detail


def test_a_recorded_filesystem_of_the_recorded_type_mounts():
    p = plan()
    assert p.action == "mount"
    assert p.fstype == "ext4"


def test_a_type_mismatch_reads_as_a_device_mixup():
    p = plan(blkid_output="TYPE=xfs\n")
    assert p.action == "refuse"
    assert p.terminal is True
    assert "different disk than it thinks" in p.detail


def test_a_missing_by_id_link_refuses_but_is_retryable():
    """Usually just early - the guest agent creates the symlink shortly
    after the attach - so the next tick asks again and the provisioning
    budget bounds the wait."""
    p = plan(link_present=False)
    assert p.action == "refuse"
    assert p.terminal is False
    assert "not attached" in p.detail


def test_blkid_failing_is_never_read_as_blank():
    """"We could not look" and "it is blank" are different facts, and
    collapsing them means mkfs on a disk holding data. Retryable, because
    the failure may be transient."""
    p = plan(blkid_ok=False, blkid_output="")
    assert p.action == "refuse"
    assert p.terminal is False
    assert "does not know" in p.detail


def test_a_disk_manifold_has_no_row_for_is_neither_formatted_nor_mounted():
    """The other half of the restored-SQLite hazard: Google can say the disk
    exists, but only Manifold's own row can say whether it ever wrote to
    it. No row, no action - in either direction."""
    for blkid in ["", "TYPE=ext4\n"]:
        p = plan(known=False, blkid_output=blkid, formatted_at=None,
                 recorded_fstype=None)
        assert p.action == "refuse"
        assert p.terminal is True
        assert "no record of volume" in p.detail


def test_the_probe_separates_no_signature_from_could_not_read():
    """blkid exits 2 for "no filesystem", which is a real answer and the one
    that permits a format. `|| true` would turn every other failure into
    that same answer - the exact move Phase 110 caught in cloud-init."""
    probe = vol.probe_command("crops")
    assert "/dev/disk/by-id/google-crops" in probe
    assert f"exit {vol.PROBE_NO_DEVICE}" in probe
    assert f"exit {vol.PROBE_BLKID_FAILED}" in probe
    assert "|| true" not in probe


def test_fstype_parsing():
    assert vol.fstype_of("DEVNAME=/dev/sdb\nTYPE=ext4\nUSAGE=filesystem") == "ext4"
    assert vol.fstype_of("") == ""
    assert vol.fstype_of("DEVNAME=/dev/sdb\n") == ""


# -- the mount guard, in a real shell ---------------------------------------------


def fake_mountpoint(tmp_path, *, answer: int):
    """A `mountpoint` on PATH that answers `answer`.

    The real binary is util-linux and does not exist on macOS, so a
    portable test of the guard's BEHAVIOUR (rather than its text) has to
    supply one. The guard's exit code is what every caller branches on.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "mountpoint"
    script.write_text(f"#!/bin/sh\nexit {answer}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env


def test_the_guard_refuses_a_path_that_is_not_a_mount(tmp_path):
    env = fake_mountpoint(tmp_path, answer=1)
    marker = tmp_path / "written"
    cmd = f"{vol.mount_guard('crops')} && touch {marker}"

    result = subprocess.run(["bash", "-c", cmd], capture_output=True,
                            text=True, env=env)

    assert result.returncode == vol.NOT_A_MOUNT_EXIT
    assert "is not a mounted filesystem" in result.stderr
    # And nothing after the guard ran: that is the whole contract.
    assert not marker.exists()


def test_the_guard_gets_out_of_the_way_when_the_path_is_a_mount(tmp_path):
    env = fake_mountpoint(tmp_path, answer=0)
    marker = tmp_path / "written"
    cmd = f"{vol.mount_guard('crops')} && touch {marker}"

    result = subprocess.run(["bash", "-c", cmd], capture_output=True,
                            text=True, env=env)

    assert result.returncode == 0
    assert marker.exists()


def test_a_missing_mountpoint_binary_refuses_rather_than_assuming(tmp_path):
    """Fail closed. `mountpoint` is util-linux and present on every image
    Manifold launches, but if it ever were not, "command not found" must
    read as "we could not prove this is a mount" - not as permission to
    write to a disk that dies with the box."""
    bin_dir = tmp_path / "empty-bin"
    bin_dir.mkdir()
    # bash by absolute path: PATH is emptied to hide `mountpoint`, and on a
    # Linux runner it is genuinely there.
    bash = shutil.which("bash") or "/bin/bash"
    env = dict(os.environ, PATH=str(bin_dir))

    result = subprocess.run([bash, "-c", vol.mount_guard("crops")],
                            capture_output=True, text=True, env=env)

    assert result.returncode == vol.NOT_A_MOUNT_EXIT


# -- the job wrapper --------------------------------------------------------------


def test_the_wrapper_guards_the_mount_before_it_mkdirs(tmp_path):
    """`mkdir -p /lambda/nfs/<name>/task-logs` on an unmounted path makes an
    ordinary directory on the boot disk, and the job then writes its whole
    output there and reports success."""
    wrapped = wrap_remote_command(
        "docker run --rm x", "/lambda/nfs/crops/task-logs/abc.log",
        ensure_dirs=["/workspace/ephemeral", "/lambda/nfs/crops/task-logs"],
        require_mount="crops")

    assert wrapped.startswith("mountpoint -q /lambda/nfs/crops ")
    assert wrapped.index("mountpoint") < wrapped.index("mkdir -p")
    # Its own list, ended with `;`. Joined with `&&` it would be swallowed
    # by the trailing `&` that backgrounds the container, its `exit` would
    # leave only that subshell, and the outer shell would hang forever on
    # the exit-file wait. The real-shell tests below are the regression.
    assert f"exit {vol.NOT_A_MOUNT_EXIT}; }}; mkdir -p" in wrapped


def test_the_wrapper_refuses_in_a_real_shell_and_creates_nothing(tmp_path):
    env = fake_mountpoint(tmp_path, answer=1)
    logs = tmp_path / "task-logs"
    wrapped = wrap_remote_command(
        "echo should-not-run", str(logs / "t.log"),
        ensure_dirs=[str(logs)], require_mount="crops")

    # timeout, not patience: a guard joined with `&&` instead of `;` gets
    # backgrounded by the trailing `&` and the shell then waits forever for
    # an exit file nothing will write. That must fail this test, not wedge
    # the run.
    result = subprocess.run(["bash", "-c", wrapped], capture_output=True,
                            text=True, env=env, timeout=30)

    assert result.returncode == vol.NOT_A_MOUNT_EXIT
    assert not logs.exists()


def test_the_wrapper_still_runs_the_job_when_the_mount_is_there(tmp_path):
    env = fake_mountpoint(tmp_path, answer=0)
    log = tmp_path / "task-logs" / "t.log"
    wrapped = wrap_remote_command(
        "echo all-good", str(log), ensure_dirs=[str(log.parent)],
        require_mount="crops")

    result = subprocess.run(["bash", "-c", wrapped], capture_output=True,
                            text=True, env=env, timeout=30)

    assert result.returncode == 0
    assert "all-good" in log.read_text()


# -- an unmounted box, end to end -------------------------------------------------


class UnmountedSSH(MockSSHConnection):
    """A box where /lambda/nfs/<name> is NOT a mount.

    MockSSHConnection answers exit 0 to EVERY command, which is exactly the
    lie this phase exists to stop believing: under it the guard passes, the
    rsync "succeeds", and the rescue reports the data saved. This answers
    the mount question honestly and leaves everything else to the mock.
    """

    async def run(self, command: str):
        if command.startswith("mountpoint -q"):
            self.commands.append(command)

            class _Result:
                exit_status = vol.NOT_A_MOUNT_EXIT
                stdout = ""
                stderr = "manifold: not a mounted filesystem"

            return _Result()
        return await super().run(command)

    async def create_process(self, command: str | None = None, **kwargs):
        process = await super().create_process(command, **kwargs)
        if command and command.startswith("mountpoint -q"):
            process.exit_status = vol.NOT_A_MOUNT_EXIT
        return process


def unmount(client, instance_id: str) -> None:
    """Swap this instance's live session for one that tells the truth."""
    conn = client.app.state.orchestrator.connections[instance_id]
    conn._conn = UnmountedSSH()


def launch_connected(client):
    """A live, CONNECTED Lambda instance, over the real HTTP surface."""
    import time

    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data"})
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    assert launch["status"] == "active"
    instance_id = launch["lambda_instance_id"]
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        inst = next(i for i in client.get("/instances").json()["instances"]
                    if i["id"] == instance_id)
        if inst["connection_state"] == "connected":
            return instance_id
        time.sleep(0.02)
    raise AssertionError(f"{instance_id} never connected")


def test_an_unmounted_destination_fails_the_rescue_instead_of_faking_it(
        client, mock_client):
    """THE TEST THIS PHASE EXISTS FOR.

    With no mount check, rsync creates /lambda/nfs/manifold-data/ on the
    auto-delete boot disk, `rescue` sets synced_to, `rescue` therefore sets
    unsaved=[], and the safety hook - reading unsaved - destroys a box whose
    files were never saved, while the audit log records a successful rescue.

    So: the sync must FAIL, the rescue must report the files as still at
    risk, and termination must be blocked.
    """
    instance_id = launch_connected(client)
    unmount(client, instance_id)

    # 1. The sync refuses, and says which path was not a mount.
    resp = client.post(f"/instances/{instance_id}/sync")
    assert resp.status_code == 409, resp.text
    assert "/lambda/nfs/manifold-data is not a mounted filesystem" in \
        resp.json()["detail"]

    # 2. The rescue does NOT claim a sync, and the files stay unsaved.
    rescue = client.post(f"/instances/{instance_id}/rescue").json()["rescue"]
    assert rescue["synced_to"] is None
    assert "not a mounted filesystem" in rescue["sync_error"]
    assert [f["path"] for f in rescue["unsaved"]] == [
        "checkpoints/step-2000.safetensors",
        "outputs/samples/grid-final.png",
    ]

    # 3. And the safety hook therefore refuses to destroy the box.
    resp = client.delete(f"/instances/{instance_id}")
    assert resp.status_code == 409
    assert resp.json()["blocked"] is True
    assert mock_client.instances[instance_id].status == "active"


def test_a_job_dispatched_to_an_unmounted_box_refuses_instead_of_writing(
        client):
    """Dispatch keys on the CONNECTION being connected, not on the launch
    being active, so a job can reach a box whose volume is not mounted yet -
    on GCE that window is minutes long. Before the guard the job would have
    run, written its outputs to the boot disk, and reported success."""
    from tests.test_dispatch_flow import wait_until

    instance_id = launch_connected(client)
    unmount(client, instance_id)

    resp = client.post("/tasks", json={
        "template": "gpu-smoke", "parameters": {"note": "unmounted"}})
    assert resp.status_code == 202
    task_id = resp.json()["task"]["id"]
    wait_until(lambda: client.get(f"/tasks/{task_id}").json()["status"]
               in ("succeeded", "failed"), timeout=10)

    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert task["exit_code"] == vol.NOT_A_MOUNT_EXIT
    assert "is not mounted" in task["error"]
    assert "Nothing ran" in task["error"]
    # Nothing was claimed as an output: there is nowhere it could have gone.
    assert task["output_paths"] == []


def test_an_upload_to_an_unmounted_path_refuses_instead_of_writing(client):
    """The THIRD write site under /lambda/nfs/<name>, and it was unguarded.

    Two consequences, and the second is worse. `sftp_write` calls
    `makedirs(parent, exist_ok=True)`, so the upload CREATES the mount point
    on the auto-delete boot disk: the bytes die with the instance while the
    route returns 200 and the audit row records a durable location. And
    because `mount_command` refuses to mount over a non-empty unmounted
    directory (terminal), one upload in that window makes the volume
    permanently unmountable on that box and fails every later launch
    carrying it.
    """
    instance_id = launch_connected(client)
    unmount(client, instance_id)
    session = client.app.state.orchestrator.connections[instance_id]._conn

    resp = client.post(
        f"/instances/{instance_id}/files/upload",
        files={"file": ("notes.txt", b"important")},
        data={"dest": "inbox/"})

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "/lambda/nfs/manifold-data is not a mounted filesystem" in detail
    assert "Nothing was uploaded and nothing was lost" in detail
    # Nothing written, and - the part that poisons the volume - no directory
    # created either.
    assert session.sftp_files == {}
    # And no audit row asserting a durable location for a file that is still
    # on the user's own machine.
    entries = client.get("/audit").json()["entries"]
    assert all(e["action"] != "file_upload" for e in entries)


def test_the_upload_guard_is_scoped_to_the_persistent_root(client):
    """It gets out of the way everywhere else. /workspace/ephemeral IS the
    boot disk by design - guarding it would refuse the one path that is
    supposed to be scratch."""
    instance_id = launch_connected(client)
    unmount(client, instance_id)

    resp = client.post(
        f"/instances/{instance_id}/files/upload",
        files={"file": ("scratch.txt", b"throwaway")},
        data={"dest": "/workspace/ephemeral/scratch.txt"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "/workspace/ephemeral/scratch.txt"


def test_an_upload_to_a_mounted_path_still_lands(client):
    """The other half of a guard: it must not break the normal case."""
    instance_id = launch_connected(client)

    resp = client.post(
        f"/instances/{instance_id}/files/upload",
        files={"file": ("notes.txt", b"important")},
        data={"dest": "inbox/"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "/lambda/nfs/manifold-data/inbox/notes.txt"
    assert resp.json()["bytes"] == len(b"important")


def test_browsing_an_unmounted_volume_refuses_instead_of_listing_nothing(
        client):
    """An instance's `filesystems` list means ATTACHED on GCP, not mounted,
    and the MCP browse tool treats it as mounts. On a mounted-then-unmounted
    box that listed the empty boot-disk directory and reported zero entries
    for a volume that may be full - an empty list meaning "we looked and
    found none" when the truth is "we looked in the wrong place"."""
    instance_id = launch_connected(client)
    unmount(client, instance_id)

    resp = client.get(f"/instances/{instance_id}/files/list",
                      params={"root_name": "persistent",
                              "path": "manifold-data/outputs"})

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "/lambda/nfs/manifold-data is not a mounted filesystem" in detail
    assert "Nothing was listed" in detail


def test_a_queued_job_waits_out_the_provisioning_window(client, db):
    """Dispatch keyed on the CONNECTION being connected and never on the
    launch being active, so queued jobs landed on a box that was up but not
    yet provisioned - on GCE that window is minutes, and with a data volume
    it ends only when the mount is verified. The job then died at the mount
    guard as a TERMINAL failure with no requeue, which is why its own error
    text had to say "if the box is still coming up, requeue in a minute".

    Deferring is what makes it correct: the job stays queued, where it can
    still succeed."""
    from tests.test_dispatch_flow import wait_until

    instance_id = launch_connected(client)
    launch = db.find_launch_by_instance(instance_id)
    db.update_launch(launch["id"], status="booting", active_at=None)
    dispatcher = client.app.state.dispatcher

    resp = client.post("/tasks", json={
        "template": "gpu-smoke", "parameters": {"note": "waits"}})
    assert resp.status_code == 202
    task_id = resp.json()["task"]["id"]

    # Nothing is dispatchable while the launch is still provisioning, even
    # though the connection is CONNECTED and the instance is otherwise free.
    assert dispatcher._pick_dispatchable() == []
    assert client.get(f"/tasks/{task_id}").json()["status"] == "queued"

    # And it runs on its own the moment the gate promotes the launch.
    db.update_launch(launch["id"], status="active", active_at=utcnow())
    wait_until(lambda: client.get(f"/tasks/{task_id}").json()["status"]
               != "queued", timeout=10)


def test_an_adopted_box_is_still_dispatchable(client, db):
    """The gate exempts adopted boxes by construction (no launch row, so
    nothing knows when they provisioned) and so does this. Special-casing
    them out of dispatch would strand every job on an adopted instance."""
    instance_id = launch_connected(client)
    launch = db.find_launch_by_instance(instance_id)
    db.update_launch(launch["id"], status="booting", active_at=None,
                     lambda_instance_id="i-somebody-elses")
    dispatcher = client.app.state.dispatcher

    resp = client.post("/tasks", json={
        "template": "gpu-smoke", "parameters": {"note": "adopted"}})
    task_id = resp.json()["task"]["id"]

    assert [t["id"] for t, _, _ in dispatcher._pick_dispatchable()] == [task_id]


# -- the provisioning gate, with a volume -----------------------------------------


class Disk:
    """What the attached disk would answer about itself. Mutable mid-test."""

    def __init__(self, *, attached=True, blkid="", mounted=False,
                 blkid_readable=True, mkfs_ok=True, dir_non_empty=False):
        self.attached = attached
        self.blkid = blkid              # "" = no filesystem signature
        self.mounted = mounted
        self.blkid_readable = blkid_readable
        self.mkfs_ok = mkfs_ok
        self.dir_non_empty = dir_non_empty
        self.mkfs_calls = 0
        self.mount_calls = 0


class VolumeSSH(ScriptedSSH):
    """Answers the four volume commands from a Disk; everything else falls
    through to the provisioning-gate mock."""

    def __init__(self, box: Box, disk: Disk, name: str):
        super().__init__(box)
        self.disk = disk
        self.name = name

    def _volume_result(self, command: str):
        disk = self.disk
        if command == vol.mounted_probe(self.name):
            return (0 if disk.mounted else 1), ""
        if command == vol.probe_command(self.name):
            if not disk.attached:
                return vol.PROBE_NO_DEVICE, ""
            if not disk.blkid_readable:
                return vol.PROBE_BLKID_FAILED, ""
            return 0, (f"DEVNAME=/dev/sdb\nTYPE={disk.blkid}\n"
                       if disk.blkid else "")
        if "mkfs." in command:
            disk.mkfs_calls += 1
            if not disk.mkfs_ok:
                return 1, ""
            disk.blkid = "ext4"
            return 0, ""
        if "mount -t" in command:
            disk.mount_calls += 1
            if disk.dir_non_empty:
                return vol.PROBE_DIR_NOT_EMPTY, ""
            disk.mounted = True
            return 0, ""
        return None

    async def run(self, command: str):
        found = self._volume_result(command)
        if found is None:
            return await super().run(command)
        code, out = found
        self.commands.append(command)

        class _Result:
            exit_status = code
            stdout = out
            stderr = ""

        return _Result()


class VolumeGate:
    """One booting GCP launch carrying a volume, its box, and a real
    supervised connection."""

    NAME = "crops"

    def __init__(self, tmp_path, db, box, disk, *, provisioning_timeout=600.0,
                 row=True, status="booting"):
        self.box = box
        self.disk = disk
        self.db = db
        self.settings = make_settings(
            tmp_path,
            launch=LaunchPolicy(boot_timeout_seconds=5, boot_poll_seconds=0,
                                provisioning_timeout_seconds=provisioning_timeout),
        )
        self.client = MockLambdaClient()
        self.instance_id = "manifold-gce-1"
        from app.lambda_api import InstanceInfo
        self.instance = InstanceInfo(
            id=self.instance_id, name="manifold-gce-1", status="active",
            ip="203.0.113.9", region="us-central1-a",
            instance_type="g2-standard-4", hourly_rate_cents=71)
        self.client.instances[self.instance_id] = self.instance
        self.orch = Orchestrator(self.settings, self.client, db,
                                 connect_fn=self._connect_fn)
        if row:
            db.create_volume(name=self.NAME, zone="us-central1-a",
                             size_gb=200)
        self.launch_id = db.create_launch(
            requested_type="g2-standard-4", region="us-central1-a",
            filesystem=self.NAME, connection_mode="direct-ssh",
            hourly_rate_cents=71, provider="gcp")
        db.update_launch(self.launch_id, status=status,
                         lambda_instance_id=self.instance_id,
                         launched_type="g2-standard-4", launched_at=utcnow())

    def _connect_fn(self, host):
        async def _dial():
            return VolumeSSH(self.box, self.disk, self.NAME)
        return _dial

    async def connect(self) -> "VolumeGate":
        self.orch._open_connection("direct-ssh", self.instance)
        self.conn = await wait_for_connection(self.orch, self.instance_id)
        return self

    async def sweep(self) -> None:
        await self.orch.sweep_provisioning(self.instance_id, self.conn)

    def launch(self) -> dict:
        return self.db.get_launch(self.launch_id)

    def volume(self) -> dict | None:
        return self.db.get_volume(self.NAME)

    def audits(self, action: str) -> list[dict]:
        return [a for a in self.db.list_audit(limit=200)
                if a["action"] == action]


async def open_volume_gate(tmp_path, db, disk, **kwargs) -> VolumeGate:
    box = kwargs.pop("box", None) or Box(init_done=True)
    return await VolumeGate(tmp_path, db, box, disk, **kwargs).connect()


async def test_a_blank_volume_is_formatted_once_then_mounted(tmp_path, db):
    disk = Disk(blkid="")
    gate = await open_volume_gate(tmp_path, db, disk)

    await gate.sweep()

    assert gate.launch()["status"] == "active"
    assert disk.mkfs_calls == 1
    assert disk.mounted is True
    # The record was written BEFORE the mkfs, which is what makes a crash
    # mid-format safe.
    assert gate.volume()["formatted_at"] is not None
    assert gate.volume()["fstype"] == "ext4"
    assert len(gate.audits("volume_format")) == 1
    assert len(gate.audits("volume_mounted")) == 1

    # And it is once, not once per tick: the row is active now.
    await gate.sweep()
    assert disk.mkfs_calls == 1


async def test_a_volume_launch_is_not_active_until_the_mount_is_verified(
        tmp_path, db):
    """The gate's "promote on incomplete with a warning" trade is right for
    a plain box and INVERTS here: an active row over an unmounted volume is
    fake persistence, and jobs would write to the boot disk believing it
    safe."""
    disk = Disk(attached=False)
    gate = await open_volume_gate(tmp_path, db, disk)

    await gate.sweep()
    assert gate.launch()["status"] == "booting"
    assert gate.launch()["active_at"] is None
    assert disk.mkfs_calls == 0

    disk.attached = True            # the by-id link turns up a moment later
    await gate.sweep()
    assert gate.launch()["status"] == "active"
    assert disk.mounted is True


async def test_an_incomplete_cloud_init_does_not_buy_a_volume_a_promotion(
        tmp_path, db):
    """The fallback that promotes a plain box with a warning must not
    promote a box whose volume never mounted."""
    disk = Disk(attached=False)
    gate = await open_volume_gate(
        tmp_path, db, disk, box=Box(init_done=False, cloud_init_done=True))

    await gate.sweep()

    assert gate.launch()["status"] == "booting"
    assert gate.audits("provisioning_incomplete") == []


async def test_the_budget_fails_a_stuck_volume_and_leaves_the_box_running(
        tmp_path, db):
    disk = Disk(attached=False)
    gate = await open_volume_gate(tmp_path, db, disk,
                                  provisioning_timeout=0.0)

    await gate.sweep()

    launch = gate.launch()
    assert launch["status"] == "failed"
    assert "data volume 'crops'" in launch["error"]
    assert "not attached" in launch["error"]
    assert "still be running and billing" in launch["error"]
    # Never destroyed to tidy up a status field.
    assert gate.client.instances[gate.instance_id].status == "active"


async def test_a_foreign_filesystem_fails_the_launch_immediately(tmp_path, db):
    """Terminal: no amount of waiting turns somebody else's ext4 into a disk
    Manifold may mount, so the launch fails now rather than billing out the
    whole provisioning budget to re-learn it."""
    disk = Disk(blkid="ext4")           # formatted, but the row says NULL
    gate = await open_volume_gate(tmp_path, db, disk)

    await gate.sweep()

    assert gate.launch()["status"] == "failed"
    assert "no record of writing" in gate.launch()["error"]
    assert disk.mkfs_calls == 0
    assert disk.mounted is False


async def test_an_interrupted_mkfs_is_never_retried(tmp_path, db):
    """The row says formatted, the disk is blank. That is the ambiguous
    cell, and the safe answer is a loud refusal - carrying what Manifold's
    own records do say, and not a claim about the disk it cannot make."""
    disk = Disk(blkid="")
    gate = await open_volume_gate(tmp_path, db, disk)
    db.mark_volume_formatted(gate.NAME, "ext4")     # the crash, simulated

    await gate.sweep()

    assert gate.launch()["status"] == "failed"
    assert "interrupted mkfs" in gate.launch()["error"]
    assert "Manifold's own records" in gate.launch()["error"]
    assert "provably" not in gate.launch()["error"]
    assert disk.mkfs_calls == 0


async def test_mounting_over_a_non_empty_directory_is_refused(tmp_path, db):
    """Those files are evidence of something that already wrote there;
    mounting would hide them."""
    disk = Disk(blkid="", dir_non_empty=True)
    gate = await open_volume_gate(tmp_path, db, disk)

    await gate.sweep()

    assert gate.launch()["status"] == "failed"
    assert "already exists with files in it" in gate.launch()["error"]
    assert disk.mounted is False


async def test_the_one_format_claim_is_atomic(db):
    """Two sweeps cannot both format. _provisioning_sweeps already serialises
    the gate per instance; this is the second lock, in the row itself."""
    db.create_volume(name="crops", zone="us-central1-a", size_gb=200)
    assert db.mark_volume_formatted("crops", "ext4") is True
    assert db.mark_volume_formatted("crops", "ext4") is False
    assert db.get_volume("crops")["fstype"] == "ext4"


async def test_a_volume_with_no_row_is_refused_rather_than_formatted(
        tmp_path, db):
    disk = Disk(blkid="")
    gate = await open_volume_gate(tmp_path, db, disk, row=False)

    await gate.sweep()

    assert gate.launch()["status"] == "failed"
    assert "no record of volume 'crops'" in gate.launch()["error"]
    assert disk.mkfs_calls == 0


# -- the mount-heal sweep ---------------------------------------------------------


async def test_the_heal_sweep_remounts_an_active_box(tmp_path, db):
    """_reconcile_launches' orphan repair flips a FAILED row to active
    whenever the instance is alive, minting an active row that passed no
    gate - and the gate will not re-enter, because it exits on
    status != booting. A reboot is the other way in (the mount is not in
    fstab)."""
    disk = Disk(blkid="ext4", mounted=False)
    gate = await open_volume_gate(tmp_path, db, disk, status="active")
    db.mark_volume_formatted(gate.NAME, "ext4")

    await gate.orch.sweep_volume_mount(gate.instance_id, gate.conn)

    assert disk.mounted is True
    assert disk.mkfs_calls == 0
    assert len(gate.audits("volume_remounted")) == 1


async def test_the_heal_sweep_never_formats(tmp_path, db):
    """mkfs on a heal path is what turns a recoverable mistake into an
    unrecoverable one. The box is not left silently broken: the mount guard
    makes every job and every sync on it refuse."""
    disk = Disk(blkid="", mounted=False)
    gate = await open_volume_gate(tmp_path, db, disk, status="active")

    await gate.orch.sweep_volume_mount(gate.instance_id, gate.conn)

    assert disk.mkfs_calls == 0
    assert disk.mounted is False
    assert gate.launch()["status"] == "active"    # it IS alive, and billing
    audit = gate.audits("volume_unmounted")
    assert len(audit) == 1
    assert "not to a repair sweep" in audit[0]["detail"]


async def test_the_heal_sweep_is_quiet_on_a_healthy_box(tmp_path, db):
    disk = Disk(blkid="ext4", mounted=True)
    gate = await open_volume_gate(tmp_path, db, disk, status="active")

    await gate.orch.sweep_volume_mount(gate.instance_id, gate.conn)

    assert disk.mount_calls == 0
    assert gate.audits("volume_remounted") == []


async def test_the_heal_sweep_ignores_lambda_and_scratch_boxes(tmp_path, db):
    """No-op by construction, not by a special case: a Lambda filesystem is
    mounted by the provider before Manifold ever sees the box."""
    disk = Disk(blkid="ext4", mounted=False)
    gate = await open_volume_gate(tmp_path, db, disk, status="active")
    db.update_launch  # (no lambda_instance_id change; only the provider)
    db._execute("UPDATE launches SET provider = 'lambda' WHERE id = ?",
                (gate.launch_id,))

    await gate.orch.sweep_volume_mount(gate.instance_id, gate.conn)

    assert disk.mount_calls == 0


def test_the_telemetry_tick_runs_the_heal_sweep(client):
    """The wiring pin, beside the provisioning gate and the bootstrap sweep
    it rides with."""
    seen: list[str] = []

    async def record(instance_id, conn):
        seen.append(instance_id)

    instance_id = launch_connected(client)
    client.app.state.orchestrator.sweep_volume_mount = record

    client.portal.call(client.app.state.dispatcher._sample_telemetry_once)

    assert instance_id in seen


# -- admission: answered by GCE, never by SQLite ----------------------------------


def gcp_orchestrator(tmp_path, db, provider=None):
    settings = make_settings(tmp_path)
    registry = ProviderRegistry()
    from app.providers import LambdaProvider
    registry.register("lambda", LambdaProvider(MockLambdaClient()))
    registry.register("gcp", provider or MockGCPProvider())
    # A real connect_fn matters here: the admitted launches below run their
    # whole pipeline in a background task, and without one it would dial
    # the mock provider's fixture IP for real.
    return Orchestrator(settings, registry, db, connect_fn=mock_connect_fn)


async def launch_gcp(orch, **over):
    args = dict(instance_type="g2-standard-4", region="us-central1-a",
                filesystem="manifold-vol-demo", provider="gcp")
    args.update(over)
    return await orch.request_launch(**args)


async def test_an_unknown_volume_is_refused_with_the_names_that_exist(
        tmp_path, db):
    orch = gcp_orchestrator(tmp_path, db)
    db.create_volume(name="crops", zone="us-central1-a", size_gb=200)

    with pytest.raises(LaunchRejected) as exc:
        await launch_gcp(orch, filesystem="not-a-volume")

    assert exc.value.status_code == 400
    assert "crops" in exc.value.detail


async def test_a_volume_in_another_zone_is_refused_naming_both(tmp_path, db):
    orch = gcp_orchestrator(tmp_path, db)

    with pytest.raises(LaunchRejected) as exc:
        await launch_gcp(orch, region="us-east4-a")

    assert "us-central1-a" in exc.value.detail
    assert "us-east4-a" in exc.value.detail
    assert "ZONAL" in exc.value.detail


async def test_an_attached_volume_is_refused_and_names_its_holder(
        tmp_path, db):
    """A stale local row must never be the authority: this reads GCE's own
    `users`. The refusal is decorated with the holder's purpose and creator
    when a launch row matches one."""
    provider = MockGCPProvider()
    provider.volumes["manifold-vol-demo"]["users"].append("manifold-abc123")
    orch = gcp_orchestrator(tmp_path, db, provider)
    holder = db.create_launch(
        requested_type="g2-standard-4", region="us-central1-a",
        filesystem="manifold-vol-demo", connection_mode="direct-ssh",
        hourly_rate_cents=71, provider="gcp",
        purpose="Tally extraction run", created_by="owner")
    db.update_launch(holder, status="active",
                     lambda_instance_id="manifold-abc123")

    with pytest.raises(LaunchRejected) as exc:
        await launch_gcp(orch)

    assert exc.value.status_code == 409
    assert "manifold-abc123" in exc.value.detail
    assert "Tally extraction run" in exc.value.detail
    assert "owner" in exc.value.detail


async def test_a_holder_whose_state_cannot_be_read_gets_its_own_sentence(
        tmp_path, db):
    """Three outcomes, not two. The enrichment used to collapse "we could
    not check" into "the holder is running" and then give advice that only
    works for a running holder - terminate it - about a box that may be
    STOPPED, which keeps its disks and will not release the volume however
    many times you try that."""
    provider = MockGCPProvider()
    provider.volumes["manifold-vol-demo"]["users"].append("manifold-abc123")

    async def unreachable(_instance_id):
        raise RuntimeError("Google is unreachable right now")

    provider.get_instance = unreachable
    orch = gcp_orchestrator(tmp_path, db, provider)

    with pytest.raises(LaunchRejected) as exc:
        await launch_gcp(orch)

    detail = exc.value.detail
    assert "could not read that instance's state" in detail
    # Both moves named, because which one is right depends on the fact we
    # could not read.
    assert "RUNNING" in detail and "STOPPED" in detail


async def test_a_stopped_holder_still_gets_the_stopped_sentence(tmp_path, db):
    """The branch that must survive the third one being added: GCE's
    TERMINATED means stopped, and a stopped box keeps its disks."""
    provider = MockGCPProvider()
    provider.volumes["manifold-vol-demo"]["users"].append("manifold-abc123")

    async def stopped(_instance_id):
        return _Msg(status="terminated")

    provider.get_instance = stopped
    orch = gcp_orchestrator(tmp_path, db, provider)

    with pytest.raises(LaunchRejected) as exc:
        await launch_gcp(orch)

    assert "stopped rather than running" in exc.value.detail


async def test_a_stale_row_saying_attached_does_not_block_a_free_disk(
        tmp_path, db):
    """The whole reason admission asks Google. A launch row can say
    "attached to X" long after X is gone; if GCE reports users=[], the disk
    is free and the launch proceeds."""
    orch = gcp_orchestrator(tmp_path, db)
    ghost = db.create_launch(
        requested_type="g2-standard-4", region="us-central1-a",
        filesystem="manifold-vol-demo", connection_mode="direct-ssh",
        hourly_rate_cents=71, provider="gcp")
    db.update_launch(ghost, status="active",
                     lambda_instance_id="manifold-long-gone")

    launch = await launch_gcp(orch)

    assert launch["filesystem"] == "manifold-vol-demo"
    assert launch["provider"] == "gcp"
    await orch.shutdown()


async def test_a_bad_volume_name_is_refused_before_anything_is_asked(
        tmp_path, db):
    orch = gcp_orchestrator(tmp_path, db)

    with pytest.raises(LaunchRejected) as exc:
        await launch_gcp(orch, filesystem="Bad Name; rm -rf /")

    assert exc.value.status_code == 422
    assert "Volume names are 2-63 characters" in exc.value.detail


async def test_extra_filesystems_stay_refused_on_gcp(tmp_path, db):
    orch = gcp_orchestrator(tmp_path, db)

    with pytest.raises(LaunchRejected) as exc:
        await launch_gcp(orch, extra_filesystems=["another-vol"])

    assert "exactly one data volume" in exc.value.detail


async def test_the_volume_name_lands_on_the_launch_row(tmp_path, db):
    """Reusing the existing `filesystem` column is the whole reason ten-plus
    downstream sites needed no change: dispatch, task readoption, the
    rescue, the relative file routes and resume-after-restart all read the
    same fact they always read."""
    orch = gcp_orchestrator(tmp_path, db)

    launch = await launch_gcp(orch)

    row = db.get_launch(launch["id"])
    assert row["filesystem"] == "manifold-vol-demo"
    assert Orchestrator._volume_name_of(row) == "manifold-vol-demo"
    assert Orchestrator._volume_name_of(
        {"provider": "lambda", "filesystem": "manifold-data"}) is None
    await orch.shutdown()


# -- the attach configuration: the data-loss cell ---------------------------------


class _Msg:
    """A permissive stand-in for a GAPIC message: set anything, read it back."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeCompute:
    """Just enough of google.cloud.compute_v1 to record what would be sent.

    The SDK never appears in this suite; this records the ONE request whose
    contents decide whether a user's disk survives their instance.
    """

    def __init__(self):
        self.inserted: list[dict] = []
        sink = self.inserted

        class _Op:
            def result(self, timeout=None):
                return None

        class _InstancesClient:
            def insert(self, *, project, zone, instance_resource):
                sink.append({"project": project, "zone": zone,
                             "instance": instance_resource})
                return _Op()

        self._instances = _InstancesClient

    def Instance(self):
        return _Msg()

    def AttachedDisk(self):
        return _Msg()

    def AttachedDiskInitializeParams(self, **kw):
        return _Msg(**kw)

    def NetworkInterface(self):
        return _Msg()

    def AccessConfig(self, **kw):
        return _Msg(**kw)

    def Scheduling(self, **kw):
        return _Msg(**kw)

    def AcceleratorConfig(self, **kw):
        return _Msg(**kw)

    def Items(self, **kw):
        return _Msg(**kw)

    def Metadata(self, **kw):
        return _Msg(**kw)

    def InstancesClient(self):
        return self._instances()


@pytest.mark.asyncio
async def test_the_data_disk_is_attached_with_auto_delete_off(monkeypatch):
    """THE DATA-LOSS CELL. auto_delete=False is the single field that makes
    the disk survive the instance; GCE preserves and detaches it on
    deletion, which is also why terminate needs no provider change. It is
    set explicitly even though proto3's bool default is already False."""
    p = RealGCPProvider("test-project", "us-central1-a",
                        public_key_fn=lambda: "ssh-ed25519 AAAA test")
    fake = FakeCompute()
    monkeypatch.setattr(p, "_compute", lambda: fake)

    await p.launch_instance("us-central1-a", "g2-standard-4", ["k"],
                            ["crops"], name="manifold-x")

    inst = fake.inserted[0]["instance"]
    boot, data = inst.disks
    assert boot.boot is True and boot.auto_delete is True
    assert data.boot is False
    assert data.auto_delete is False
    assert data.device_name == "crops"          # /dev/disk/by-id/google-crops
    assert data.mode == "READ_WRITE"
    assert data.source == "zones/us-central1-a/disks/crops"


@pytest.mark.asyncio
async def test_a_scratch_launch_attaches_no_second_disk(monkeypatch):
    p = RealGCPProvider("test-project", "us-central1-a",
                        public_key_fn=lambda: "ssh-ed25519 AAAA test")
    fake = FakeCompute()
    monkeypatch.setattr(p, "_compute", lambda: fake)

    await p.launch_instance("us-central1-a", "g2-standard-4", ["k"], [])

    assert len(fake.inserted[0]["instance"].disks) == 1


def test_an_instance_card_names_the_volume_attached_to_it():
    """From Google, not from a launch row - the same meaning `filesystems`
    has always had on a Lambda card, and the same limit: attached is not
    mounted. Nothing acts on it destructively (every write asserts the
    mount itself), and it is what lets an agent find the box that holds a
    given volume."""
    p = RealGCPProvider("test-project", "us-central1-a")
    inst = _Msg(
        name="manifold-x", status="RUNNING", network_interfaces=[],
        machine_type="zones/us-central1-a/machineTypes/g2-standard-4",
        disks=[_Msg(boot=True, device_name="manifold-x"),
               _Msg(boot=False, device_name="crops")],
    )

    info = p._to_info(inst, "us-central1-a")

    assert info.file_system_names == ["crops"]      # the boot disk is not one
    assert info.instance_type == "g2-standard-4"


@pytest.mark.asyncio
async def test_the_attach_race_gets_its_own_named_refusal(monkeypatch):
    """Two launches both read users=[] and both asked for the same disk.
    Google answers the second with RESOURCE_IN_USE_BY_ANOTHER_RESOURCE;
    without this it fell through to "GCE launch failed: ...", which reads
    like a broken launch path rather than a second box holding the volume."""
    from app.providers.base import ProviderError

    p = RealGCPProvider("test-project", "us-central1-a",
                        public_key_fn=lambda: "ssh-ed25519 AAAA test")

    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("must not be touched in this test")

    async def run_raising(fn, *a, **k):
        raise RuntimeError(
            "400 The resource 'projects/p/zones/z/disks/crops' is already "
            "being used by 'projects/p/zones/z/instances/other'. "
            "RESOURCE_IN_USE_BY_ANOTHER_RESOURCE")

    monkeypatch.setattr(p, "_compute", lambda: Boom())
    monkeypatch.setattr(p, "_run", run_raising)

    with pytest.raises(ProviderError) as exc:
        await p.launch_instance("us-central1-a", "g2-standard-4", ["k"],
                                ["crops"])

    assert "already attached to another instance" in str(exc.value)
    assert "one instance at a time" in str(exc.value)


# -- the routes -------------------------------------------------------------------


def mock_mode_client(tmp_path, monkeypatch):
    """A TestClient over a mock-mode app, where 'gcp' is MockGCPProvider."""
    from fastapi.testclient import TestClient
    from app.main import create_app

    monkeypatch.delenv("MANIFOLD_MOCK_FORCE", raising=False)
    return TestClient(create_app(make_settings(tmp_path), mock=True))


def test_volumes_are_listed_from_the_cloud_with_sqlite_as_the_overlay(
        tmp_path, monkeypatch):
    """Sourced from GCE, not from SQLite: a disk bills for its provisioned
    size whether attached or not, so a SQLite-sourced list would let a disk
    bill invisibly forever if the data dir were ever lost. A disk with no
    row still appears, marked as one Manifold has no record of."""
    with mock_mode_client(tmp_path, monkeypatch) as c:
        # A disk that exists at "Google" and nowhere else - hand-created, or
        # a SQLite restored from before it existed. It still LISTS, and it
        # is marked as one Manifold cannot use.
        c.app.state.orchestrator.providers.get_provider("gcp").volumes[
            "somebody-elses-disk"] = {
                "name": "somebody-elses-disk", "zone": "us-central1-a",
                "size_gb": 100, "users": [], "status": "READY",
                "disk_type": "pd-balanced"}

        body = c.get("/volumes").json()
        demo = next(v for v in body["volumes"]
                    if v["name"] == "manifold-vol-demo")
        assert demo["zone"] == "us-central1-a"
        assert demo["size_gb"] == 200
        assert demo["mount_point"] == "/lambda/nfs/manifold-vol-demo"
        assert demo["list_price_usd_per_month"] == 20.0
        assert "list price" in body["price_basis"]
        foreign = next(v for v in body["volumes"]
                       if v["name"] == "somebody-elses-disk")
        assert foreign["known_to_manifold"] is False
        assert "formatted_at" not in foreign
        # Never a bytes_used it cannot know: a 0 there would assert "empty"
        # about a disk that may be full.
        assert "bytes_used" not in demo


def test_the_demo_volume_is_usable_in_the_zero_credential_demo(
        tmp_path, monkeypatch):
    """The seeded disk is there so the demo has something to pick, and
    without Manifold's OWN row it was the opposite: `known_to_manifold`
    false, which the Volumes page renders as "not created by Manifold -
    unusable here" and the launch path refuses outright. Mock mode writes
    the row for its own fixture disks, so the demo opens on a working
    combination instead of a blocker."""
    with mock_mode_client(tmp_path, monkeypatch) as c:
        body = c.get("/volumes").json()
        demo = next(v for v in body["volumes"]
                    if v["name"] == "manifold-vol-demo")
        assert demo["known_to_manifold"] is True
        # NULL formatted_at: the demo's first launch is what formats it.
        assert demo["formatted_at"] is None
        # And the zone it lives in is one the zone picker actually offers.
        zones = [z["code"] for z in c.get("/regions?provider=gcp").json()["regions"]]
        assert demo["zone"] in zones


def test_the_mock_zone_list_is_zones_not_regions(tmp_path, monkeypatch):
    """The Volumes panel sources its Zone dropdown from /regions?provider=gcp
    and a volume is ZONAL, so a fixture holding REGIONS made every option it
    offered come back "'asia-east1' is not a GCE zone" - creating or
    attaching a volume was impossible in the zero-credential demo, which
    left the phase-112 UI with no end-to-end path that costs nothing."""
    with mock_mode_client(tmp_path, monkeypatch) as c:
        zones = [z["code"] for z in c.get("/regions?provider=gcp").json()["regions"]]
        assert zones, "mock GCP must offer somewhere to launch"
        for zone in zones:
            assert zone.count("-") >= 2, zone
        resp = c.post("/volumes", json={
            "name": "demo-made-in-the-demo", "zone": zones[0], "size_gb": 200})
        assert resp.status_code == 201, resp.text


def test_the_volume_list_says_when_it_is_fixture_data(tmp_path, monkeypatch):
    """Every sibling agent-facing route carries this flag and the MCP tool
    relays the body verbatim, so without it an agent in the zero-spend demo
    reports disks billing at $10-20 a month that do not exist."""
    with mock_mode_client(tmp_path, monkeypatch) as c:
        assert c.get("/volumes").json()["mock"] is True


def test_creating_a_volume_records_it_and_leaves_formatted_at_null(
        tmp_path, monkeypatch):
    with mock_mode_client(tmp_path, monkeypatch) as c:
        resp = c.post("/volumes", json={
            "name": "crops", "zone": "us-central1-a", "size_gb": 500})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["size_gb"] == 500
        assert body["known_to_manifold"] is True
        # NULL is the permission slip for exactly one mkfs later.
        assert body["formatted_at"] is None
        assert body["list_price_usd_per_month"] == 50.0


def test_a_bad_volume_name_never_reaches_the_cloud(tmp_path, monkeypatch):
    with mock_mode_client(tmp_path, monkeypatch) as c:
        resp = c.post("/volumes", json={
            "name": "Crops; rm -rf /", "zone": "us-central1-a",
            "size_gb": 200})
        assert resp.status_code == 422
        # The rule Manifold ENFORCES, which is 2-63: the regex refuses the
        # one-character name Google's own 1-63 rule allows, and quoting
        # Google's number would send the reader to argue with Google.
        assert "Volume names are 2-63 characters" in resp.json()["detail"]
        assert all(v["name"] != "Crops; rm -rf /"
                   for v in c.get("/volumes").json()["volumes"])


def test_a_region_where_a_zone_belongs_is_refused(tmp_path, monkeypatch):
    with mock_mode_client(tmp_path, monkeypatch) as c:
        resp = c.post("/volumes", json={
            "name": "crops", "zone": "us-central1", "size_gb": 200})
        assert resp.status_code == 422
        assert "ZONAL" in resp.json()["detail"]


def test_deleting_a_volume_needs_the_name_typed_back(tmp_path, monkeypatch):
    with mock_mode_client(tmp_path, monkeypatch) as c:
        c.post("/volumes", json={"name": "crops", "zone": "us-central1-a",
                                 "size_gb": 200})

        resp = c.delete("/volumes/crops")
        assert resp.status_code == 428
        assert "no undo and no rescue" in resp.json()["detail"]
        assert any(v["name"] == "crops"
                   for v in c.get("/volumes").json()["volumes"])

        resp = c.delete("/volumes/crops?confirm_name=crops")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "crops"
        assert all(v["name"] != "crops"
                   for v in c.get("/volumes").json()["volumes"])


def test_an_attached_volume_cannot_be_deleted(tmp_path, monkeypatch):
    with mock_mode_client(tmp_path, monkeypatch) as c:
        gcp = c.app.state.orchestrator.providers.get_provider("gcp")
        gcp.volumes["manifold-vol-demo"]["users"].append("manifold-holder")

        resp = c.delete(
            "/volumes/manifold-vol-demo?confirm_name=manifold-vol-demo")

        assert resp.status_code == 409
        assert "manifold-holder" in resp.json()["detail"]


def test_an_unconfigured_project_never_answers_you_have_no_volumes(client):
    """`GET /volumes -> {"volumes": []}` from a project nobody contacted is
    the forbidden move on the one resource that bills whether or not anyone
    could list it: the page renders "No data volumes yet" and a disk can
    bill invisibly forever. create_volume already asked this question; the
    other two paths did not."""
    resp = client.get("/volumes")

    assert resp.status_code == 400, resp.text
    assert "gcloud auth application-default login" in resp.json()["detail"]
    assert "volumes" not in resp.json()


def test_an_unconfigured_project_does_not_declare_a_volume_absent(client):
    """DELETE answered "No data volume named X in this Google Cloud project"
    without ever contacting a Google Cloud project."""
    resp = client.delete("/volumes/crops?confirm_name=crops")

    assert resp.status_code == 400, resp.text
    assert "gcloud auth application-default login" in resp.json()["detail"]


def test_the_delete_404_says_whose_list_it_is_reading(tmp_path, monkeypatch):
    """It built its list from SQLite while the sentence claimed to enumerate
    the Google project - so it declared a volume absent from the project and
    then listed that same volume among what the project has, while omitting
    the disks Google actually had. Same wording the launch path already
    uses."""
    with mock_mode_client(tmp_path, monkeypatch) as c:
        # A row for a disk that is NOT at Google: deleted in the console, or
        # this database outlived it.
        c.app.state.orchestrator.db.create_volume(
            name="ghost-vol", zone="us-central1-a", size_gb=200)

        resp = c.delete("/volumes/ghost-vol?confirm_name=ghost-vol")

        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert "Volumes Manifold has created: ghost-vol" in detail
        assert "Manifold's own record" in detail
        assert "Have:" not in detail


def test_a_volume_is_not_a_filesystem_and_the_storage_page_says_so(
        tmp_path, monkeypatch):
    """A volume never appears in /filesystems (that route is Lambda's
    registry, and every field on it is one a detached PD cannot answer), and
    the Storage page 404s a volume name. An honest refusal, not a gap."""
    with mock_mode_client(tmp_path, monkeypatch) as c:
        names = [f["name"] for f in c.get("/filesystems").json()["filesystems"]]
        assert "manifold-vol-demo" not in names
        resp = c.get("/storage/files?filesystem=manifold-vol-demo")
        assert resp.status_code == 404


def test_launch_options_still_offers_gcp_no_filesystems(tmp_path, monkeypatch):
    """Deliberately deferred: a volume-aware target would have to carry a
    filesystem_bytes_used a Persistent Disk cannot report, and 0 there would
    assert "empty"."""
    with mock_mode_client(tmp_path, monkeypatch) as c:
        options = c.get("/launch-options?provider=gcp").json()
        assert options["provider"] == "gcp"
        assert all(t["filesystem"] is None for t in options["targets"])
