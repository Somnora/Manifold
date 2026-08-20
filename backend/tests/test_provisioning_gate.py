"""Phase 110: the provisioning gate - "SSH answers" is not "the box works".

THE COMPLAINT, from a real GCP T4 on 2026-08-17. The launch reported
settled=true, phase="ready", "SSH is up; the instance is ready" 36 seconds
in. The first command against it failed with "no connected instance"; the
launch bootstrap fired minutes before /workspace/ephemeral existed, exited 1
with three "No such file or directory" errors, and its exactly-once marker
then correctly prevented the retry that would have saved it; and no template
job could run at all, because the SSH session had been established before
cloud-init added ubuntu to the docker group and POSIX supplementary groups
are fixed for the life of a session.

On Lambda "the provider says active" and "the machine is provisioned" very
nearly coincide. On a stock-Ubuntu GCE image they are six minutes apart:
RUNNING at ~36s, sshd at ~90s, cloud-init done (NVIDIA driver built, Docker,
the NVIDIA runtime, the sidecar) at ~7 minutes.

So the launch row stays 'booting' until the box says its own setup finished.
These tests are mostly about the four things that are easy to get wrong:
promoting on evidence rather than on a connection existing, never waiting
forever on a machine that bills by the second, never destroying a box to
tidy up a status field, and never overwriting a terminate that landed while
we were asking.
"""

import asyncio

import pytest

from app import bootstrap as boot
from app.config import LaunchPolicy
from app.connections import ConnectionState, MockSSHConnection
from app.db import utcnow
from app.dispatcher import Dispatcher
from app.lambda_api import InstanceInfo, MockLambdaClient
from app.orchestrator import (
    CLOUD_INIT_DONE_PROBE,
    DOCKER_ACCESS_PROBE,
    INIT_MARKER_PROBE,
    Orchestrator,
)
from app.task_queue import SQLiteTaskQueue
from tests.conftest import make_settings, wait_for_launch_status
from tests.test_idle_matrix import Harness
from tests.test_launch import wait_for_connection

SCRIPT = "pip install -r ~/repo/requirements.txt\n"


# -- the box, and a session that can say no --------------------------------------


class Box:
    """What the instance would answer about itself. Mutable mid-test: a real
    box finishes provisioning while we are watching."""

    def __init__(self, *, init_done=False, cloud_init_done=False,
                 docker_ok=True):
        self.init_done = init_done              # /var/lib/manifold/init-done
        self.cloud_init_done = cloud_init_done  # cloud-init's own marker
        self.docker_ok = docker_ok              # this session can use docker


class ScriptedSSH(MockSSHConnection):
    """MockSSHConnection.run returns exit 0 for EVERY command, so a gate that
    asks `test -f /var/lib/manifold/init-done` over it passes instantly and
    proves nothing. This answers the three probes from the Box and leaves
    every other command to the mock."""

    def __init__(self, box: Box):
        super().__init__()
        self.box = box

    def _probe_code(self, command: str) -> int | None:
        if command == INIT_MARKER_PROBE:
            return 0 if self.box.init_done else 1
        if command == CLOUD_INIT_DONE_PROBE:
            return 0 if self.box.cloud_init_done else 1
        if command == DOCKER_ACCESS_PROBE:
            return 0 if self.box.docker_ok else 1
        return None

    async def run(self, command: str):
        code = self._probe_code(command)
        if code is None:
            return await super().run(command)
        self.commands.append(command)

        class _Result:
            exit_status = code
            stdout = ""
            stderr = ""

        return _Result()


class Gate:
    """One booting launch, its instance, and a real supervised connection.

    A real ManagedConnection and not a stub: the redial the gate can order
    is only meaningful if the supervisor behind it actually re-dials.
    """

    def __init__(self, tmp_path, db, box, *, provisioning_timeout=600.0,
                 bootstrap=None):
        self.box = box
        self.db = db
        self.sessions: list[ScriptedSSH] = []
        self.settings = make_settings(
            tmp_path,
            launch=LaunchPolicy(boot_timeout_seconds=5, boot_poll_seconds=0,
                                provisioning_timeout_seconds=provisioning_timeout),
        )
        self.client = MockLambdaClient()
        self.instance_id = "i-gce"
        self.instance = InstanceInfo(
            id=self.instance_id, name="manifold-gce", status="active",
            ip="203.0.113.9", region="us-east-1",
            instance_type="gpu_1x_a10", hourly_rate_cents=129,
        )
        self.client.instances[self.instance_id] = self.instance
        self.orch = Orchestrator(self.settings, self.client, db,
                                 connect_fn=self._connect_fn)
        self.launch_id = db.create_launch(
            requested_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data", connection_mode="direct-ssh",
            hourly_rate_cents=129, bootstrap=bootstrap)
        db.update_launch(self.launch_id, status="booting",
                         lambda_instance_id=self.instance_id,
                         launched_type="gpu_1x_a10", launched_at=utcnow())

    def _connect_fn(self, host):
        async def _dial():
            session = ScriptedSSH(self.box)
            self.sessions.append(session)
            return session
        return _dial

    async def connect(self) -> "Gate":
        self.orch._open_connection("direct-ssh", self.instance)
        self.conn = await wait_for_connection(self.orch, self.instance_id)
        return self

    async def sweep(self) -> None:
        """One pass of the gate, exactly as the telemetry tick calls it."""
        await self.orch.sweep_provisioning(self.instance_id, self.conn)

    def launch(self) -> dict:
        return self.db.get_launch(self.launch_id)

    def audits(self, action: str) -> list[dict]:
        return [a for a in self.db.list_audit(limit=200)
                if a["action"] == action]


async def open_gate(tmp_path, db, box, **kwargs) -> Gate:
    return await Gate(tmp_path, db, box, **kwargs).connect()


async def wait_for(predicate, *, timeout=2.0, message="condition") -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {message}")


# -- what the gate waits for ------------------------------------------------------


async def test_ssh_answering_is_not_promoted_to_ready(tmp_path, db):
    """THE defect. conn.start() is asynchronous and the launch used to be
    stamped active on the next line - which on GCE meant "ready" six minutes
    before a single job could run."""
    gate = await open_gate(tmp_path, db, Box(init_done=False))

    await gate.sweep()

    launch = gate.launch()
    assert launch["status"] == "booting"
    assert launch["active_at"] is None
    # But the moment SSH answered IS recorded: it anchors the budget, and it
    # is the one thing this pass learned.
    assert launch["connected_at"] is not None


async def test_the_init_signal_is_what_promotes_it(tmp_path, db):
    box = Box(init_done=False)
    gate = await open_gate(tmp_path, db, box)
    await gate.sweep()
    assert gate.launch()["status"] == "booting"

    box.init_done = True                    # cloud-init reached its last line
    await gate.sweep()

    launch = gate.launch()
    assert launch["status"] == "active"
    assert launch["active_at"] is not None
    assert launch["error"] is None


async def test_connected_at_is_stamped_once_not_on_every_pass(tmp_path, db):
    """It is an anchor, not a heartbeat: re-stamping it would hand a stuck
    box a fresh budget on every tick and the deadline would never arrive."""
    gate = await open_gate(tmp_path, db, Box())
    await gate.sweep()
    first = gate.launch()["connected_at"]

    await gate.sweep()

    assert gate.launch()["connected_at"] == first


async def test_an_adopted_box_has_no_row_and_is_never_gated(tmp_path, db):
    """Exempt by construction, not by a special case: the sweep's first
    question finds no launch and stops."""
    gate = await open_gate(tmp_path, db, Box())
    gate.db.update_launch(gate.launch_id, lambda_instance_id="i-somebody-elses")

    await gate.sweep()

    assert gate.launch()["status"] == "booting"
    assert gate.launch()["connected_at"] is None


# -- never wait forever, never destroy the box ------------------------------------


async def test_the_budget_fails_the_launch_and_leaves_the_box_running(
        tmp_path, db):
    """A zero budget is "the deadline is already past" without a sleep.

    The instance is NOT terminated, and that is the whole point: a box whose
    sidecar never came up is a box a rescue cannot save anything from, so
    destroying it here would destroy data to tidy up a status field. The
    copy says the money is still running, because it is.
    """
    gate = await open_gate(tmp_path, db, Box(), provisioning_timeout=0.0)

    await gate.sweep()

    launch = gate.launch()
    assert launch["status"] == "failed"
    assert "still be running and billing" in launch["error"]
    assert gate.client.instances[gate.instance_id].status == "active"
    assert gate.instance_id in gate.orch.connections
    assert len(gate.audits("provisioning_timeout")) == 1


async def test_a_connection_that_drops_after_answering_once_still_ages_out(
        tmp_path, db):
    """THE HOLE the budget had. `_await_provisioning`'s own bound fires only
    while connected_at is NULL, and the gate returned on a non-CONNECTED
    connection BEFORE it looked at the budget at all - so a box that answered
    SSH once and then lost the link had a supervisor retrying forever, a row
    stuck in 'booting' with no provisioning_timeout ever written, and a card
    counting up past its own budget. The idle sweep skips 'booting', so
    nothing else would ever mention the money.

    The budget is anchored in the ROW - hence a connected_at written long
    ago rather than a zero timeout here - so the gate can spend it with no
    session at all, and a backend restart resumes the same deadline instead
    of granting a fresh one.
    """
    gate = await open_gate(tmp_path, db, Box(), provisioning_timeout=600.0)
    gate.db.update_launch(gate.launch_id,
                          connected_at="2020-01-01T00:00:00+00:00")
    gate.conn.state = ConnectionState.RECONNECTING

    await gate.sweep()

    launch = gate.launch()
    assert launch["status"] == "failed"
    assert "answered SSH and then stopped answering" in launch["error"]
    assert "reconnecting" in launch["error"]
    assert "still be running and billing" in launch["error"]
    assert len(gate.audits("provisioning_timeout")) >= 1
    # Never destroyed to tidy up a status field.
    assert gate.client.instances[gate.instance_id].status == "active"


async def test_a_dropped_connection_inside_the_budget_keeps_waiting(
        tmp_path, db):
    """A reconnect is normal - sshd restarts, the network blips - so the
    ageing-out only happens once the budget is genuinely spent."""
    gate = await open_gate(tmp_path, db, Box(), provisioning_timeout=600.0)
    await gate.sweep()
    gate.conn.state = ConnectionState.RECONNECTING

    await gate.sweep()

    assert gate.launch()["status"] == "booting"
    assert gate.audits("provisioning_timeout") == []


async def test_a_box_that_stops_answering_mid_question_says_that(tmp_path, db):
    """`signal == "unknown"` is deliberately distinct from "pending" - "we
    could not ask" and "we asked and it said not yet" are different answers -
    and both were closing the launch with the same sentence, which sent the
    reader to an init log that may have nothing to say about it."""
    gate = await open_gate(tmp_path, db, Box(), provisioning_timeout=0.0)

    class Mute(ScriptedSSH):
        async def run(self, command):
            raise ConnectionError("session went away mid-question")

    gate.conn._conn = Mute(gate.box)

    await gate.sweep()

    launch = gate.launch()
    assert launch["status"] == "failed"
    assert "could not ask it whether provisioning had finished" in launch["error"]
    assert "manifold-init.log" not in launch["error"]


async def test_a_box_that_is_still_installing_says_where_to_look(tmp_path, db):
    """The other half: "pending" is the box answering, and its answer has a
    place to go and read."""
    gate = await open_gate(tmp_path, db, Box(), provisioning_timeout=0.0)

    await gate.sweep()

    assert "did not finish provisioning" in gate.launch()["error"]
    assert "/var/log/manifold-init.log" in gate.launch()["error"]


def test_the_telemetry_tick_gates_a_disconnected_box_too(client, db):
    """The wiring pin for the half above: the tick used to `continue` on any
    non-CONNECTED connection before the gate ran, which is exactly the state
    a launch has to age out in. The gate reads the row, so it needs no
    session - everything BELOW it in the tick still requires one."""
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data"})
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    instance_id = launch["lambda_instance_id"]
    orch = client.app.state.orchestrator
    db.update_launch(launch["id"], status="booting", active_at=None,
                     connected_at="2020-01-01T00:00:00+00:00")
    orch.connections[instance_id].state = ConnectionState.RECONNECTING

    client.portal.call(client.app.state.dispatcher._sample_telemetry_once)

    closed = db.get_launch(launch["id"])
    assert closed["status"] == "failed"
    assert "stopped answering" in closed["error"]


async def test_cloud_init_finished_without_our_marker_promotes_with_a_warning(
        tmp_path, db):
    """The fallback. cloud-init has stopped, so our marker is never going to
    appear - and waiting out the budget on a box that is as done as it will
    ever be costs money for nothing. It goes active carrying the failure."""
    gate = await open_gate(tmp_path, db,
                           Box(init_done=False, cloud_init_done=True))

    await gate.sweep()

    launch = gate.launch()
    assert launch["status"] == "active"
    assert "did not complete" in launch["error"]
    assert len(gate.audits("provisioning_incomplete")) == 1


async def test_a_terminate_landing_mid_provision_is_not_resurrected(tmp_path,
                                                                    db):
    """The gate asks the box questions over SSH, which takes time, and a user
    can hit Terminate inside that window. Promoting on a stale read would
    put an 'active' row on a destroyed instance."""
    box = Box(init_done=True)
    gate = await open_gate(tmp_path, db, box)

    terminated_at = utcnow()

    class Racing(ScriptedSSH):
        async def run(self, command):
            if command == DOCKER_ACCESS_PROBE:
                gate.db.update_launch(gate.launch_id, status="terminated",
                                      terminated_at=terminated_at)
            return await super().run(command)

    gate.conn._conn = Racing(box)

    await gate.sweep()

    launch = gate.launch()
    assert launch["status"] == "terminated"
    assert launch["active_at"] is None


# -- the docker socket, and the one redial ----------------------------------------


async def test_a_session_locked_out_of_docker_is_redialled_exactly_once(
        tmp_path, db):
    """Supplementary groups are fixed when a session is established, so a
    connection opened before cloud-init ran `usermod -aG docker ubuntu` can
    never reach the socket. The fix is a new session - not a new connection
    object, which would leak a supervisor that redials forever."""
    gate = await open_gate(tmp_path, db, Box(init_done=True, docker_ok=False))
    first_session = gate.conn._conn
    assert len(gate.sessions) == 1

    await gate.sweep()

    assert gate.launch()["status"] == "active"      # promoted all the same
    assert len(gate.audits("connection_redialled")) == 1
    # The supervisor dialled again on its own...
    await wait_for(lambda: len(gate.sessions) == 2, message="the redial")
    await wait_for_connection(gate.orch, gate.instance_id)
    assert gate.conn._conn is not first_session
    # ...and everything above the session survived: same object, same entry.
    assert gate.orch.connections[gate.instance_id] is gate.conn
    assert gate.conn.state == ConnectionState.CONNECTED

    # And it is ONE redial, not one per tick: the row is active now, so the
    # gate no longer looks at this box at all.
    await gate.sweep()
    assert len(gate.audits("connection_redialled")) == 1


async def test_a_session_that_can_reach_docker_is_left_alone(tmp_path, db):
    """A redial destroys every channel on the connection - an open terminal,
    a streaming job. It is paid for on evidence only."""
    gate = await open_gate(tmp_path, db, Box(init_done=True, docker_ok=True))

    await gate.sweep()

    assert gate.launch()["status"] == "active"
    assert gate.audits("connection_redialled") == []
    assert len(gate.sessions) == 1


def test_the_probes_ask_the_questions_they_claim_to():
    """These three strings run on someone's paid GPU, and two of them decide
    whether a launch fails or a session is dropped. Pin their shape.

    The docker probe is fail-open by construction - the same rule the GPU
    preflight uses - because there is nothing a fresh session could fix on a
    box that has no docker socket at all. The init marker is the DURABLE
    path: /var/run is tmpfs, and a driver-upgrade reboot would erase a
    marker there while cloud-init's per-instance stage never runs again.
    """
    assert INIT_MARKER_PROBE == "test -f /var/lib/manifold/init-done"
    assert "/var/run/" not in INIT_MARKER_PROBE
    assert DOCKER_ACCESS_PROBE.startswith("test ! -e /var/run/docker.sock ||")


# -- the launch pipeline drives the same gate --------------------------------------


async def test_a_launch_settles_only_when_its_box_is_provisioned(tmp_path, db):
    """End to end through request_launch: the pipeline no longer stamps
    active, so a launch whose box is still installing stays 'booting' - and
    finishes on its own the moment the marker appears."""
    box = Box(init_done=False)
    settings = make_settings(
        tmp_path,
        launch=LaunchPolicy(boot_timeout_seconds=5, boot_poll_seconds=0.01,
                            provisioning_timeout_seconds=600.0),
    )
    sessions: list[ScriptedSSH] = []

    def connect_fn(host):
        async def _dial():
            session = ScriptedSSH(box)
            sessions.append(session)
            return session
        return _dial

    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=connect_fn)
    launch = await orch.request_launch(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data")

    # Long enough for the boot wait and several gate passes.
    await asyncio.sleep(0.3)
    assert db.get_launch(launch["id"])["status"] == "booting"
    assert db.get_launch(launch["id"])["connected_at"] is not None

    box.init_done = True
    settled = await orch.wait_for_launch(launch["id"], timeout=3.0)
    assert settled["status"] == "active"
    assert settled["active_at"] is not None
    await orch.shutdown()


async def test_a_box_that_never_answers_ssh_does_not_sit_in_booting(tmp_path,
                                                                    db):
    """The gate's budget is anchored at connected_at, and a box whose sshd
    never answers never earns one - so the wait for the FIRST connection is
    bounded on its own. Otherwise the row would sit in 'booting' forever
    while the instance billed, which is the failure the gate was written to
    prevent, wearing a different hat.

    The connection is deliberately NOT torn down: if sshd does turn up
    later, the supervisor is still dialling and the reconcile sweep repairs
    the row from the live instance. Failing a row destroys nothing.
    """
    settings = make_settings(
        tmp_path,
        launch=LaunchPolicy(boot_timeout_seconds=5, boot_poll_seconds=0.01,
                            provisioning_timeout_seconds=0.05),
    )

    def refusing_connect_fn(host):
        async def _dial():
            raise ConnectionError("connection refused")
        return _dial

    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=refusing_connect_fn)
    launch = await orch.request_launch(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data")

    settled = await orch.wait_for_launch(launch["id"], timeout=5.0)
    assert settled["status"] == "failed"
    assert "never accepted an SSH connection" in settled["error"]
    assert "still be running and billing" in settled["error"]
    assert settled["connected_at"] is None
    await orch.shutdown()


def test_the_telemetry_tick_runs_the_gate_too(client):
    """The wiring pin, and the half the launch task cannot cover: a launch
    whose waiter died with the old process (a --reload restart mid-boot)
    still gets promoted, because the same gate rides the telemetry loop
    beside the bootstrap sweep. The row is the state, so there is nothing to
    hand over."""
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data"})
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    db = client.app.state.orchestrator.db
    db.update_launch(launch["id"], status="booting", active_at=None)

    client.portal.call(client.app.state.dispatcher._sample_telemetry_once)

    promoted = db.get_launch(launch["id"])
    assert promoted["status"] == "active"
    assert promoted["active_at"] is not None


# -- the bootstrap asks the same question ------------------------------------------


async def test_the_bootstrap_waits_for_the_init_signal_itself(tmp_path, db):
    """It does NOT rely on the gate having made this true. Orphan repair
    flips a failed row to active whenever the instance is alive, which mints
    an active row that passed no gate - and a bootstrap started on a
    half-provisioned box burns its one exactly-once marker on a machine
    where /workspace/ephemeral does not exist yet."""
    box = Box(init_done=False)
    gate = await open_gate(tmp_path, db, box, bootstrap=SCRIPT)
    gate.db.update_launch(gate.launch_id, status="active", active_at=utcnow())
    dispatcher = Dispatcher(gate.settings, gate.orch, SQLiteTaskQueue(db), {},
                            db, MockLambdaClient())

    await dispatcher._sweep_bootstrap(gate.instance_id, gate.conn)
    assert db.find_detached_by_note(boot.note_for(gate.launch_id)) is None

    box.init_done = True
    await dispatcher._sweep_bootstrap(gate.instance_id, gate.conn)
    assert db.find_detached_by_note(boot.note_for(gate.launch_id)) is not None


# -- the idle sweep keeps its hands off --------------------------------------------


@pytest.fixture
def harness(tmp_path, db):
    return Harness(tmp_path, db)


async def test_the_idle_sweep_leaves_a_provisioning_box_alone(harness):
    """A launch may carry an idle timeout as short as 300s, and a GCE
    provision takes ~7 minutes. The box is CONNECTED and doing nothing
    Manifold can see, so before this it was a textbook reap - of a machine
    mid-setup, with nothing rescuable because its sidecar is not up."""
    launch_id = harness.add_instance("i-prov")       # idle past its window
    harness.db.update_launch(launch_id, status="booting")

    await harness.dispatcher._check_idle()

    assert harness.terminated == []
    status = harness.dispatcher.activity_status("i-prov")
    assert status["state"] == "booting"
    assert status["busy"] is True
    assert "provisioning" in status["reason"]


async def test_two_sweeps_at_once_redial_only_once(tmp_path, db):
    """Two callers drive this gate - the launch tail's fast-path loop and
    the telemetry tick - and it awaits several SSH probes, so they interleave
    at every one of them. Unguarded, both read the row as still booting, both
    find the session locked out of docker, and the SECOND redial drops the
    session the first one just rebuilt.
    """
    gate = await open_gate(tmp_path, db, Box(init_done=True, docker_ok=False))

    await asyncio.gather(gate.sweep(), gate.sweep())

    assert len(gate.audits("connection_redialled")) == 1
    assert gate.launch()["status"] == "active"
