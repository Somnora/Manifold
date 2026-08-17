"""Phase 95: detached commands - the work asserts its own liveness.

THE COMPLAINT, from the heaviest agent user this product has had: the most
repeated boilerplate of their session was `setsid nohup ... > log 2>&1 &`
followed by polling a log file, because run_command is capped at 50s. And
the workaround was OUR DOCUMENTED ADVICE - run_command's docstring
prescribed exactly that dance. Meanwhile their 34 GB rsync read as "idle"
to the sweep for its entire runtime (0% GPU from start to finish), kept
alive only by the agent remembering to poll.

So: run_detached writes the command VERBATIM to a script on the box (SFTP
bytes - never interpolated into a shell line), launches it under setsid
with a wrapper that records the exit code, and the telemetry loop probes
the pid. A live pid is evidence of work, and evidence - not an agent's
memory - is what keeps the idle sweep off a busy box.

Four states, read literally: running | exited | vanished (ended, HOW is
not knowable - never dressed as an exit code) | unreachable (a state of
the connection, never reported as the command having stopped).
"""

import pytest

from app import detached as det
from tests.conftest import wait_for_launch_status
from tests.test_idle_matrix import NOW, TIMEOUT, Harness
from tests.test_terminal import launch_connected


# -- the pure pieces -----------------------------------------------------------


def test_parse_probe_reads_the_three_states():
    assert det.parse_probe("RUNNING\n---MANIFOLD-LOG---\nline1\nline2") == (
        "running", None, "line1\nline2")
    assert det.parse_probe("EXIT 3\n---MANIFOLD-LOG---\n") == ("exited", 3, "")
    assert det.parse_probe("VANISHED\n---MANIFOLD-LOG---\nx") == (
        "vanished", None, "x")


def test_garbage_probe_output_is_unknown_not_a_state():
    """A parse failure must not be reported as any state of the command -
    the truthful-or-absent rule, applied to our own plumbing."""
    state, code, _log = det.parse_probe("Connection reset by peer")
    assert state == "unknown"
    assert code is None


def test_parse_pids_alive_separates_alive_from_settled():
    h1, h2, h3 = "d" + "a" * 11, "d" + "b" * 11, "d" + "c" * 11
    alive, settled = det.parse_pids_alive(
        f"{h1}\n{h2} EXIT 137\n{h3} VANISHED\nnoise line\n")
    assert alive == {h1}
    assert settled == {h2: 137, h3: None}


def test_handles_are_shell_safe_by_construction():
    for _ in range(50):
        assert det.HANDLE_RE.match(det.new_handle())


# -- starting one (API level, mock connection) ---------------------------------


HOSTILE = "echo \"$(rm -rf /)\" '`backtick`' ; done & \n echo second line"


def start(client, instance_id, command="sleep 999", note="test work"):
    resp = client.post(f"/instances/{instance_id}/run-detached",
                       json={"command": command, "note": note})
    assert resp.status_code == 202, resp.text
    return resp.json()


def test_the_command_travels_as_file_bytes_never_through_a_shell(client):
    """The quoting hazard is not solved, it is REMOVED: the user's command
    goes to the box as SFTP bytes, and the only thing interpolated into any
    shell line is the generated hex handle. A command full of quotes,
    subshells and newlines must round-trip exactly, and no fragment of it
    may appear in what the shell was given."""
    instance_id = launch_connected(client)
    body = start(client, instance_id, command=HOSTILE)
    handle = body["handle"]
    assert det.HANDLE_RE.match(handle)

    conn = client.app.state.orchestrator.connections[instance_id]._conn
    stored = conn.sftp_files[f".manifold/detached/{handle}.sh"]
    assert stored.decode() == HOSTILE, "the command did not round-trip"
    for shell_line in conn.commands:
        assert "rm -rf" not in shell_line, (
            "user command text reached a shell line")
        assert "backtick" not in shell_line


def test_a_start_is_registered_and_audited(client):
    instance_id = launch_connected(client)
    body = start(client, instance_id, note="Red Hope migration rsync")
    row = client.app.state.orchestrator.db.get_detached(body["handle"])
    assert row["instance_id"] == instance_id
    assert row["pid"] == 4242                       # the mock's pid
    assert row["exited_at"] is None
    audits = client.app.state.orchestrator.db.list_audit(limit=10)
    assert any(a["action"] == "detached_started" and body["handle"] in a["detail"]
               for a in audits)


def test_status_reports_running_with_the_log_tail(client):
    instance_id = launch_connected(client)
    handle = start(client, instance_id)["handle"]
    resp = client.get(f"/instances/{instance_id}/detached/{handle}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "running"
    assert body["exit_code"] is None
    assert "(mock detached log)" in body["log_tail"]


def test_an_unknown_handle_is_404_not_a_probe(client):
    instance_id = launch_connected(client)
    assert client.get(
        f"/instances/{instance_id}/detached/d{'f' * 11}").status_code == 404
    # A malformed handle must be rejected BEFORE it could reach any shell.
    assert client.get(
        f"/instances/{instance_id}/detached/$(evil)").status_code == 404


def test_an_unreachable_box_is_not_a_stopped_command(client):
    """The connection being down is a state of the CONNECTION. Reporting it
    as the command having ended is the exact inference - absence of
    evidence read as evidence of absence - this whole line of work exists
    to remove."""
    instance_id = launch_connected(client)
    handle = start(client, instance_id)["handle"]
    client.app.state.orchestrator.connections.pop(instance_id)
    body = client.get(f"/instances/{instance_id}/detached/{handle}").json()
    assert body["state"] == "unreachable"
    assert body["exit_code"] is None


def test_the_open_handle_cap_refuses_the_next_start(client):
    instance_id = launch_connected(client)
    db = client.app.state.orchestrator.db
    for i in range(det.MAX_OPEN_PER_INSTANCE):
        db.create_detached(handle=det.new_handle(), instance_id=instance_id,
                           command="x", note="", created_by=None, pid=i + 1)
    resp = client.post(f"/instances/{instance_id}/run-detached",
                       json={"command": "one more"})
    assert resp.status_code == 409


# -- the liveness half: evidence keeps the sweep away --------------------------


class ProbeConn:
    """A connection whose probe answer the test scripts."""

    def __init__(self, reply: str):
        self.reply = reply
        self.ran: list[str] = []

    async def run(self, command, timeout=None):
        self.ran.append(command)
        return 0, self.reply, ""


@pytest.fixture
def harness(tmp_path, db):
    return Harness(tmp_path, db)


async def test_a_live_detached_command_spares_an_idle_box(harness):
    """THE point of the feature. A box idle past its whole window, 0% GPU,
    nothing Manifold-visible - the rsync case - but a detached pid the
    probe just confirmed alive. It must not be reaped, and the verdict must
    say why in the words the starter gave."""
    harness.add_instance("i-rsync")            # idle_for = TIMEOUT + 60
    handle = det.new_handle()
    harness.db.create_detached(handle=handle, instance_id="i-rsync",
                               command="rsync -a /src /dst",
                               note="Red Hope storage migration",
                               created_by="owner", pid=4242)
    await harness.dispatcher._probe_detached("i-rsync", ProbeConn(handle))

    await harness.dispatcher._check_idle()

    assert harness.terminated == []
    status = harness.dispatcher.activity_status("i-rsync")
    assert status["state"] == "detached_running"
    assert status["busy"] is True
    assert "Red Hope storage migration" in status["reason"]


async def test_stale_evidence_is_no_evidence(harness):
    """A sighting two-plus telemetry intervals old proves nothing about
    NOW. Letting it protect forever would be keep-alive wearing a lab coat,
    and the reaper would never fire on a box whose probe stopped working."""
    harness.add_instance("i-ghost")
    handle = det.new_handle()
    harness.db.create_detached(handle=handle, instance_id="i-ghost",
                               command="x", note="", created_by=None,
                               pid=4242)
    harness.dispatcher._detached_alive["i-ghost"] = (NOW - 500.0, [handle])

    await harness.dispatcher._check_idle()

    assert [i for i, _ in harness.terminated] == ["i-ghost"]


async def test_a_finished_command_settles_and_stops_protecting(harness):
    handle = det.new_handle()
    harness.add_instance("i-done")
    harness.db.create_detached(handle=handle, instance_id="i-done",
                               command="x", note="", created_by=None,
                               pid=4242)
    await harness.dispatcher._probe_detached(
        "i-done", ProbeConn(f"{handle} EXIT 0"))

    row = harness.db.get_detached(handle)
    assert row["exit_code"] == 0
    assert row["exited_at"] is not None
    assert "i-done" not in harness.dispatcher._detached_alive
    audits = harness.db.list_audit(limit=10)
    assert any(a["action"] == "detached_finished" and "exit 0" in a["detail"]
               for a in audits)

    await harness.dispatcher._check_idle()
    assert [i for i, _ in harness.terminated] == ["i-done"]


async def test_vanished_is_recorded_as_vanished_never_as_an_exit(harness):
    """No exit file and a dead pid: it ended, and how is not knowable. The
    row keeps exit_code NULL and the audit says so - inventing a code here
    would be the absence-as-zero mistake in its most literal form."""
    handle = det.new_handle()
    harness.add_instance("i-rebooted")
    harness.db.create_detached(handle=handle, instance_id="i-rebooted",
                               command="x", note="", created_by=None,
                               pid=4242)
    await harness.dispatcher._probe_detached(
        "i-rebooted", ProbeConn(f"{handle} VANISHED"))

    row = harness.db.get_detached(handle)
    assert row["exit_code"] is None
    assert row["exited_at"] is not None
    audits = harness.db.list_audit(limit=10)
    assert any(a["action"] == "detached_finished" and "vanished" in a["detail"]
               for a in audits)


async def test_no_open_handles_means_no_ssh_cost(harness):
    """The probe rides a 30s loop across every connected box; a box that
    never used the feature must pay nothing for it."""
    harness.add_instance("i-plain", idle_for=10)
    conn = ProbeConn("")
    await harness.dispatcher._probe_detached("i-plain", conn)
    assert conn.ran == []


async def test_the_first_settle_wins(harness):
    """A late VANISHED verdict (pid recycled, probe raced the exit file)
    must not overwrite a recorded exit code."""
    handle = det.new_handle()
    harness.db.create_detached(handle=handle, instance_id="i-x",
                               command="x", note="", created_by=None,
                               pid=1)
    harness.db.finish_detached(handle, 0)
    harness.db.finish_detached(handle, None)     # the race, losing
    assert harness.db.get_detached(handle)["exit_code"] == 0
