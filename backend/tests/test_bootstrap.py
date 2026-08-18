"""Phase 104: the launch bootstrap - the setup script a box runs on its own.

THE COMPLAINT. Every serious launch was followed by the same manual dance:
clone, pip install, pull the model, warm the cache. Somebody had to be
watching at the right moment to start it, so an agent that walked away came
back to a billing GPU with nothing on it.

THE THING THAT IS EASY TO GET WRONG, and what these tests are mostly about:
WHEN it fires. Not at the moment the launch flips to active. Three separate
paths make a launch active (the normal pipeline, resume-after-restart,
orphan repair) and at every one of them the SSH supervisor is still
CONNECTING - conn.run and conn.sftp_write raise until CONNECTED. A hook at
any of those sites fires twice, or loses the start in a crash window, or
both.

So nothing fires at the transition. The dispatcher RECONCILES on the
telemetry tick: active launch + a script on the row + a CONNECTED
connection + no detached row carrying this launch's note. The detached row
is the marker, it is in SQLite, and it is checked before every start - so a
repeated sweep, a restarted backend, and all three activation paths
converge on exactly one bootstrap.

And the rule that survives everything else here: a bootstrap that FAILS
does not terminate the box. Work already on that disk is not destroyed
because a setup line had a typo.
"""

import time

import pytest

from app import bootstrap as boot
from app import detached as det
from app.connections import ConnectionState
from app.dispatcher import Dispatcher
from app.lambda_api import MockLambdaClient
from tests.conftest import wait_for_launch_status
from tests.test_detached import ProbeConn
from tests.test_idle_matrix import Harness

SCRIPT = (
    "set -e\n"
    "git clone https://example.invalid/repo ~/repo\n"
    "pip install -r ~/repo/requirements.txt\n"
)


# -- harness -------------------------------------------------------------------


def launch_with_bootstrap(client, script=SCRIPT, timeout=5.0):
    """Launch (through the real route) and wait for SSH. Returns
    (launch_id, instance_id)."""
    body = {"instance_type": "gpu_1x_a10", "region": "us-east-1",
            "filesystem": "manifold-data"}
    if script is not None:
        body["bootstrap"] = script
    resp = client.post("/instances", json=body)
    assert resp.status_code == 202, resp.text
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    instance_id = launch["lambda_instance_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if instance_of(client, instance_id)["connection_state"] == "connected":
            return launch["id"], instance_id
        time.sleep(0.02)
    raise AssertionError("instance never connected")


def instance_of(client, instance_id) -> dict:
    return next(i for i in client.get("/instances").json()["instances"]
                if i["id"] == instance_id)


def sweep(client, instance_id) -> None:
    """One reconciling sweep, exactly as the telemetry loop calls it."""
    dispatcher = client.app.state.dispatcher
    conn = client.app.state.orchestrator.connections[instance_id]
    client.portal.call(dispatcher._sweep_bootstrap, instance_id, conn)


def bootstrap_row(client, launch_id) -> dict | None:
    db = client.app.state.orchestrator.db
    return db.find_detached_by_note(boot.note_for(launch_id))


class SettledSSH:
    """An asyncssh stand-in whose detached probe answers "already finished".

    Swapped into a ManagedConnection so the STATUS-POLL settle site can be
    the one that discovers an exit - the half of the race the mock
    connection (perpetually RUNNING) cannot produce on its own.
    """

    def __init__(self, exit_code: int):
        self.exit_code = exit_code
        self.commands: list[str] = []

    async def run(self, command: str):
        self.commands.append(command)
        stdout = (f"EXIT {self.exit_code}\n---MANIFOLD-LOG---\n"
                  f"E: Unable to locate package definitely-not-a-package")

        class _Result:
            exit_status = 0
            stderr = ""

        _Result.stdout = stdout
        return _Result()

    # The supervisor closes whatever it is holding at shutdown.
    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


# -- the pure pieces -----------------------------------------------------------


def test_the_note_is_the_marker_and_it_names_its_launch():
    assert boot.note_for("abc123") == "bootstrap:abc123"
    assert boot.is_bootstrap_note("bootstrap:abc123") is True
    assert boot.is_bootstrap_note("Red Hope migration rsync") is False
    assert boot.is_bootstrap_note(None) is False


def test_the_fingerprint_is_size_and_hash_and_nothing_else():
    size, digest = boot.fingerprint("export HF_TOKEN=hf_secret\n")
    assert size == len("export HF_TOKEN=hf_secret\n".encode())
    assert len(digest) == 12
    assert "hf_secret" not in digest
    # Same script, same fingerprint; one byte different, different hash.
    assert boot.fingerprint("a")[1] != boot.fingerprint("b")[1]


def test_report_never_invents_a_state():
    assert boot.report(None, connected=True) is None
    assert boot.report({"exited_at": None}, connected=True) == {
        "state": "running"}
    # A box we cannot see is never reported as a command that stopped.
    assert boot.report({"exited_at": None}, connected=False) == {
        "state": "unreachable"}
    assert boot.report({"exited_at": "t", "exit_code": 0}, connected=True) == {
        "state": "exited", "exit_code": 0}
    # Ended, and HOW is not knowable. Never dressed up as an exit code.
    assert boot.report({"exited_at": "t", "exit_code": None},
                       connected=True) == {"state": "vanished"}


# -- starting it (API level, mock connection) ----------------------------------


def test_the_script_reaches_the_box_as_file_bytes(client):
    """The launch pipeline itself starts nothing - the sweep does, and what
    lands on the box is the script verbatim, as SFTP bytes."""
    launch_id, instance_id = launch_with_bootstrap(client)
    assert bootstrap_row(client, launch_id) is None, (
        "the launch pipeline started the bootstrap; it must not")
    assert "bootstrap" not in instance_of(client, instance_id)

    sweep(client, instance_id)

    row = bootstrap_row(client, launch_id)
    assert row is not None and row["instance_id"] == instance_id
    conn = client.app.state.orchestrator.connections[instance_id]._conn
    stored = conn.sftp_files[f".manifold/detached/{row['handle']}.sh"]
    assert stored.decode() == SCRIPT, "the script did not round-trip"
    for shell_line in conn.commands:
        assert "requirements.txt" not in shell_line, (
            "script text reached a shell line")


def test_the_telemetry_tick_is_what_starts_it(client):
    """The wiring pin: the sweep hangs off the same loop as the detached
    probe, so nobody has to remember to call it."""
    launch_id, instance_id = launch_with_bootstrap(client)
    client.portal.call(client.app.state.dispatcher._sample_telemetry_once)
    assert bootstrap_row(client, launch_id) is not None


def test_a_connection_that_is_not_up_yet_is_waited_for_not_failed(client):
    """No retry loop, no error state: a tick that finds the box down does
    nothing, and the next tick asks again. This is why the transition hook
    was not needed - CONNECTED arrives on its own."""
    launch_id, instance_id = launch_with_bootstrap(client)
    conn = client.app.state.orchestrator.connections[instance_id]
    conn.state = ConnectionState.RECONNECTING

    client.portal.call(client.app.state.dispatcher._sample_telemetry_once)
    assert bootstrap_row(client, launch_id) is None
    # And directly, not only through the loop's own gate: the sweep owns
    # this condition, so calling it on a down box is a quiet no-op rather
    # than a ConnectionError somebody has to catch.
    sweep(client, instance_id)
    assert bootstrap_row(client, launch_id) is None

    conn.state = ConnectionState.CONNECTED
    client.portal.call(client.app.state.dispatcher._sample_telemetry_once)
    assert bootstrap_row(client, launch_id) is not None


def test_a_launch_without_a_bootstrap_is_untouched(client):
    """Absent means absent, everywhere: no row, no field, no SSH."""
    launch_id, instance_id = launch_with_bootstrap(client, script=None)
    db = client.app.state.orchestrator.db
    assert db.get_launch(launch_id)["bootstrap"] is None

    sweep(client, instance_id)

    assert db.list_detached(instance_id) == []
    assert "bootstrap" not in instance_of(client, instance_id)


def test_an_empty_bootstrap_is_the_same_as_none(client):
    launch_id, instance_id = launch_with_bootstrap(client, script="   \n  ")
    db = client.app.state.orchestrator.db
    assert db.get_launch(launch_id)["bootstrap"] is None
    sweep(client, instance_id)
    assert db.list_detached(instance_id) == []


def test_an_adopted_box_has_no_launch_and_no_bootstrap(client):
    """find_launch_by_instance returns None for a box Manifold did not
    launch, and the sweep stops there rather than guessing."""
    _launch_id, instance_id = launch_with_bootstrap(client)
    db = client.app.state.orchestrator.db
    launch = db.find_launch_by_instance(instance_id)
    db.update_launch(launch["id"], lambda_instance_id="i-somebody-elses")

    sweep(client, instance_id)

    assert db.list_detached(instance_id) == []


def test_an_oversized_bootstrap_is_refused_with_the_cap_in_the_message(client):
    """A 422 that names the number, not a silent truncation: a script cut in
    half runs half the setup and reports success."""
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data",
        "bootstrap": "x" * (boot.MAX_BOOTSTRAP_BYTES + 1),
    })
    assert resp.status_code == 422
    assert str(boot.MAX_BOOTSTRAP_BYTES) in resp.text
    assert client.get("/launches").json()["launches"] == []


# -- exactly once --------------------------------------------------------------


def test_a_bootstrap_never_starts_twice(client):
    """THE guarantee. The detached row is the marker and it is checked
    before every start, so sweeping again - and again, and from a
    dispatcher that has just been built from scratch, which is what a
    backend restart is - starts nothing new."""
    launch_id, instance_id = launch_with_bootstrap(client)
    db = client.app.state.orchestrator.db
    sweep(client, instance_id)
    first = bootstrap_row(client, launch_id)["handle"]

    for _ in range(3):
        sweep(client, instance_id)
    assert [r["handle"] for r in db.list_detached(instance_id)] == [first]

    # A restart: a brand-new dispatcher, no memory of anything, same
    # database. It must reach the same answer from the row alone.
    restarted = Dispatcher(
        client.app.state.settings, client.app.state.orchestrator,
        client.app.state.queue, {}, db, MockLambdaClient())
    client.portal.call(
        restarted._sweep_bootstrap, instance_id,
        client.app.state.orchestrator.connections[instance_id])
    assert [r["handle"] for r in db.list_detached(instance_id)] == [first]


def test_a_finished_bootstrap_does_not_start_again(client):
    """The marker outlives the work. If the exactly-once check looked only
    at OPEN handles, every bootstrap would restart the tick after it
    finished - and a setup script is rarely safe to run twice."""
    launch_id, instance_id = launch_with_bootstrap(client)
    db = client.app.state.orchestrator.db
    sweep(client, instance_id)
    handle = bootstrap_row(client, launch_id)["handle"]
    db.finish_detached(handle, 0)

    sweep(client, instance_id)

    assert [r["handle"] for r in db.list_detached(instance_id)] == [handle]


def test_a_launch_row_that_goes_back_to_booting_starts_nothing(client):
    """Statuses move around (resume-after-downtime, orphan repair). None of
    that may produce a second bootstrap once one exists."""
    launch_id, instance_id = launch_with_bootstrap(client)
    db = client.app.state.orchestrator.db
    sweep(client, instance_id)
    handle = bootstrap_row(client, launch_id)["handle"]

    db.update_launch(launch_id, status="booting")
    sweep(client, instance_id)
    db.update_launch(launch_id, status="active")
    sweep(client, instance_id)

    assert [r["handle"] for r in db.list_detached(instance_id)] == [handle]


# -- what it reports -----------------------------------------------------------


def test_the_state_goes_running_then_exited(client):
    launch_id, instance_id = launch_with_bootstrap(client)
    sweep(client, instance_id)
    assert instance_of(client, instance_id)["bootstrap"] == {"state": "running"}

    handle = bootstrap_row(client, launch_id)["handle"]
    client.app.state.orchestrator.db.finish_detached(handle, 0)

    assert instance_of(client, instance_id)["bootstrap"] == {
        "state": "exited", "exit_code": 0}


def test_an_unreachable_box_is_not_a_finished_bootstrap(client):
    launch_id, instance_id = launch_with_bootstrap(client)
    sweep(client, instance_id)
    client.app.state.orchestrator.connections.pop(instance_id)

    assert instance_of(client, instance_id)["bootstrap"] == {
        "state": "unreachable"}


def test_the_handle_is_findable_the_way_the_docs_say(client):
    """The MCP docstring tells an agent to find the bootstrap in
    list_detached_commands by its note. That has to be true."""
    launch_id, instance_id = launch_with_bootstrap(client)
    sweep(client, instance_id)
    rows = client.get(f"/instances/{instance_id}/detached").json()["detached"]
    assert [r["note"] for r in rows] == [f"bootstrap:{launch_id}"]


# -- failure is reported, never acted on ---------------------------------------


def test_a_nonzero_exit_surfaces_and_the_box_survives(client, os_pings):
    """The whole safety posture in one test. A failed setup script is
    reported loudly and costs the user nothing else: the instance keeps
    running, keep-alive is untouched, and the decision stays with whoever
    reads the notification."""
    launch_id, instance_id = launch_with_bootstrap(client)
    db = client.app.state.orchestrator.db
    sweep(client, instance_id)
    handle = bootstrap_row(client, launch_id)["handle"]
    keep_alive_before = db.get_launch(launch_id)["keep_alive"]

    client.portal.call(client.app.state.dispatcher._probe_detached,
                       instance_id, ProbeConn(f"{handle} EXIT 3"))

    assert instance_of(client, instance_id)["bootstrap"] == {
        "state": "exited", "exit_code": 3}
    launch = db.get_launch(launch_id)
    assert launch["status"] == "active"
    assert launch["terminated_at"] is None
    assert launch["keep_alive"] == keep_alive_before
    kinds = [n["kind"]
             for n in client.get("/notifications").json()["notifications"]]
    assert kinds.count("bootstrap_failed") == 1
    assert len(os_pings) == 1


def test_a_clean_exit_says_nothing(client, os_pings):
    """Success is the boring case; the bell is for interruptions."""
    launch_id, instance_id = launch_with_bootstrap(client)
    sweep(client, instance_id)
    handle = bootstrap_row(client, launch_id)["handle"]

    client.portal.call(client.app.state.dispatcher._probe_detached,
                       instance_id, ProbeConn(f"{handle} EXIT 0"))

    assert client.get("/notifications").json()["notifications"] == []
    assert os_pings == []


def test_one_notification_when_the_probe_settles_first(client, os_pings):
    """Two independent settle sites, and which one wins depends on whether
    anybody happened to be polling. The probe gets there first here; the
    status poll that follows must not ping a second time."""
    launch_id, instance_id = launch_with_bootstrap(client)
    sweep(client, instance_id)
    handle = bootstrap_row(client, launch_id)["handle"]

    client.portal.call(client.app.state.dispatcher._probe_detached,
                       instance_id, ProbeConn(f"{handle} EXIT 3"))
    assert len(os_pings) == 1, "the probe did not raise the notification"

    managed = client.app.state.orchestrator.connections[instance_id]
    managed._conn = SettledSSH(3)
    body = client.get(f"/instances/{instance_id}/detached/{handle}").json()

    assert body["exit_code"] == 3
    assert len(os_pings) == 1


def test_one_notification_when_the_status_poll_settles_first(client, os_pings):
    """The other order. The status poll discovers the exit, and the
    telemetry probe that runs afterwards must not ping again."""
    launch_id, instance_id = launch_with_bootstrap(client)
    sweep(client, instance_id)
    handle = bootstrap_row(client, launch_id)["handle"]

    managed = client.app.state.orchestrator.connections[instance_id]
    managed._conn = SettledSSH(3)
    body = client.get(f"/instances/{instance_id}/detached/{handle}").json()
    assert body["state"] == "exited" and body["exit_code"] == 3
    assert len(os_pings) == 1, "the status poll did not raise the notification"

    client.portal.call(client.app.state.dispatcher._probe_detached,
                       instance_id, ProbeConn(f"{handle} EXIT 3"))

    assert len(os_pings) == 1
    kinds = [n["kind"]
             for n in client.get("/notifications").json()["notifications"]]
    assert kinds.count("bootstrap_failed") == 1


def test_an_ordinary_detached_command_failing_is_not_a_bootstrap_ping(client):
    """The notification is keyed on the bootstrap note, not on "a detached
    command exited nonzero" - run_detached failures are the caller's to
    read, and turning every one into a bell would be noise."""
    instance_id = launch_with_bootstrap(client, script=None)[1]
    handle = client.post(f"/instances/{instance_id}/run-detached",
                         json={"command": "false", "note": "a normal job"},
                         ).json()["handle"]

    client.portal.call(client.app.state.dispatcher._probe_detached,
                       instance_id, ProbeConn(f"{handle} EXIT 1"))

    assert client.get("/notifications").json()["notifications"] == []


# -- the audit knows the size, never the contents ------------------------------


def test_the_audit_records_size_and_hash_never_the_script(client):
    """A bootstrap is exactly where somebody writes `export HF_TOKEN=...`,
    and the audit log is a plain table every viewer reads. Size and hash
    answer "is this the script I meant" without echoing the secret; the
    never-echo rule beats completeness."""
    secret = "export HF_TOKEN=hf_thisisthetokendonotlogit\ntrain.sh\n"
    launch_id, instance_id = launch_with_bootstrap(client, script=secret)
    sweep(client, instance_id)

    audits = client.app.state.orchestrator.db.list_audit(limit=200)
    started = [a for a in audits if a["action"] == "bootstrap_started"]
    assert len(started) == 1
    size, digest = boot.fingerprint(secret)
    assert str(size) in started[0]["detail"]
    assert digest in started[0]["detail"]
    assert not any("hf_thisisthetokendonotlogit" in (a["detail"] or "")
                   for a in audits), "the script leaked into the audit log"


# -- the idle sweep leaves a working box alone ---------------------------------


@pytest.fixture
def harness(tmp_path, db):
    return Harness(tmp_path, db)


async def test_a_running_bootstrap_holds_the_box(harness):
    """The reason this rides the detached machinery at all. A cold-cache
    install shows 0% GPU for its whole runtime, which is indistinguishable
    from an abandoned box - unless the work is visible. It is, and the
    verdict names the bootstrap by its NOTE, not by quoting the script.
    """
    harness.add_instance("i-setup")              # idle past its whole window
    launch = harness.db.find_launch_by_instance("i-setup")
    handle = det.new_handle()
    harness.db.create_detached(
        handle=handle, instance_id="i-setup",
        command="pip install -r ~/repo/requirements.txt",
        note=boot.note_for(launch["id"]), created_by="owner", pid=4242)
    await harness.dispatcher._probe_detached("i-setup", ProbeConn(handle))

    await harness.dispatcher._check_idle()

    assert harness.terminated == []
    status = harness.dispatcher.activity_status("i-setup")
    assert status["busy"] is True
    assert f"bootstrap:{launch['id']}" in status["reason"]
    assert "pip install" not in status["reason"], (
        "the evidence line quoted the script instead of the note")
