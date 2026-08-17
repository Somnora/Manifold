"""Phase 76b: the max-lifetime ceiling.

An opt-in wall-clock bound on an instance's TOTAL lifetime, anchored on
`launches.launched_at` — the moment the provider accepted the launch. It is
the backstop for the failure the idle timeout cannot catch: a box that looks
busy forever (a served model, a chatty terminal, a stuck teardown) and bills
until someone notices.

Every test here drives the real `_check_idle` and records, rather than
performs, the one destructive call. The single most important test in the
file is the first one: with no ceiling set — the default, and what every
existing launch has — nothing changes at all.
"""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import IdleSettings
from app.connections import ConnectionState
from app.dispatcher import Dispatcher
from app.lambda_api import MockLambdaClient
from app.orchestrator import Orchestrator, TerminationBlocked
from app.providers.base import ProviderError
from app.task_queue import SQLiteTaskQueue
from app.templates import load_templates
from tests.conftest import make_settings, mock_connect_fn

IDLE_TIMEOUT = 1800.0
CEILING = 4 * 3600.0            # 4h, comfortably above the test minimum


def ago(seconds: float) -> str:
    """A wall-clock ISO stamp `seconds` in the past, as the DB writes them."""
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds)).isoformat(timespec="seconds")


class FakeConn:
    def __init__(self, state=ConnectionState.CONNECTED):
        self.state = state


class Harness:
    def __init__(self, tmp_path, db, *, notifier=None):
        templates, _ = load_templates(
            Path(__file__).resolve().parent.parent.parent / "templates")
        self.settings = make_settings(
            tmp_path, idle=IdleSettings(timeout_seconds=IDLE_TIMEOUT,
                                        poll_seconds=15.0))
        self.db = db
        self.queue = SQLiteTaskQueue(db)
        self.now = 10_000.0                 # fake monotonic, advanceable
        self.orch = Orchestrator(self.settings, MockLambdaClient(), db,
                                 connect_fn=mock_connect_fn)
        self.dispatcher = Dispatcher(self.settings, self.orch, self.queue,
                                     templates, db, MockLambdaClient(),
                                     notifier=notifier,
                                     clock=lambda: self.now)
        self.calls: list[tuple[str, dict]] = []
        self.raises: dict[str, Exception] = {}

        async def _terminate(instance_id, **kwargs):
            self.calls.append((instance_id, kwargs))
            exc = self.raises.get(instance_id)
            if exc is not None:
                raise exc
            self.orch.connections.pop(instance_id, None)
            return {"instance_id": instance_id, "terminated": True}

        self.orch.terminate = _terminate

    # -- fixtures on the box ---------------------------------------------------

    def add(self, instance_id, *, connected=True, launched_seconds_ago=None,
            max_lifetime=None, keep_alive=False, idle_for=0.0,
            launched_at="") -> str:
        self.orch.connections[instance_id] = FakeConn(
            ConnectionState.CONNECTED if connected
            else ConnectionState.RECONNECTING)
        self.dispatcher.last_activity[instance_id] = self.now - idle_for
        launch_id = self.db.create_launch(
            requested_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data", connection_mode="direct-ssh",
            hourly_rate_cents=129, max_lifetime_seconds=max_lifetime)
        fields = {"status": "active", "lambda_instance_id": instance_id,
                  "keep_alive": 1 if keep_alive else 0}
        if launched_seconds_ago is not None:
            fields["launched_at"] = ago(launched_seconds_ago)
        elif launched_at:
            fields["launched_at"] = launched_at
        self.db.update_launch(launch_id, **fields)
        return launch_id

    def pin(self, instance_id, template) -> str:
        task_id = self.queue.enqueue(template=template, parameters={})
        self.queue.mark_running(task_id, instance_id)
        return task_id

    def own_auto(self, instance_id, launch_id, lifecycle, detail=None) -> str:
        task_id = self.db.create_task(
            template="whisper-batch", parameters={}, auto_manage=True,
            gpu_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data")
        self.db.set_task_lifecycle(task_id, lifecycle, launch_id=launch_id,
                                   detail=detail)
        return task_id

    def terminated(self) -> list[str]:
        return [i for i, _ in self.calls]

    def audit(self) -> list[str]:
        return [r["action"] for r in self.db._execute(
            "SELECT action FROM audit_log ORDER BY id").fetchall()]


class RecordingNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str, str | None]] = []

    def notify(self, kind, title, body="", ref=None):
        self.sent.append((kind, title, body, ref))
        return "n1"


@pytest.fixture
def harness(tmp_path, db):
    return Harness(tmp_path, db)


# -- the default: nothing changes -----------------------------------------------


async def test_no_ceiling_means_no_ceiling(harness):
    """THE most important test here. max_lifetime_seconds is NULL on every
    launch that does not ask for one and on every row that predates this
    column. A box that has been up for a week, with no ceiling, is not
    touched — no terminate, no notification, no audit row."""
    harness.add("i-old", launched_seconds_ago=7 * 86400, idle_for=0.0)
    await harness.dispatcher._check_idle()
    assert harness.calls == []
    assert "ceiling_termination" not in harness.audit()


async def test_a_ceiling_that_has_not_been_reached_does_nothing(harness):
    harness.add("i-young", launched_seconds_ago=600, max_lifetime=CEILING)
    await harness.dispatcher._check_idle()
    assert harness.calls == []


async def test_ceiling_fires_from_launched_at_not_from_activity(harness):
    """The whole point: the box is being used RIGHT NOW (activity a second
    ago, nowhere near the idle timeout) and still hits its lifetime bound.
    Nothing on the instance can push this clock out."""
    harness.add("i-busy", launched_seconds_ago=CEILING + 120,
                max_lifetime=CEILING, idle_for=1.0)
    await harness.dispatcher._check_idle()
    assert harness.terminated() == ["i-busy"]
    assert harness.calls[0][1]["force"] is False   # never unattended force
    detail = harness.db._execute(
        "SELECT detail FROM audit_log WHERE action = 'ceiling_termination'"
    ).fetchone()["detail"]
    assert "past max_lifetime" in detail


async def test_ceiling_survives_a_restart(tmp_path, db):
    """It is anchored in the database, not in process memory: a backend that
    restarts (or a dev-mode --reload) resumes counting from launch, not from
    zero. This is why the anchor is wall clock and not time.monotonic."""
    first = Harness(tmp_path, db)
    first.add("i-restart", launched_seconds_ago=CEILING + 60,
              max_lifetime=CEILING)
    reborn = Harness(tmp_path, db)          # fresh dispatcher, same database
    reborn.orch.connections["i-restart"] = FakeConn()

    await reborn.dispatcher._check_idle()
    assert reborn.terminated() == ["i-restart"]
    assert reborn.calls[0][1]["force"] is False   # never unattended force


# -- what the ceiling does and does not defer to --------------------------------


async def test_ceiling_fires_through_a_served_model(harness):
    """A vllm-serve task never leaves 'running', so a ceiling that deferred
    to it would be permanently unreachable on the most expensive workload
    Manifold has — a feature that never fires.

    The same task must still block IDLE termination: that protection is not
    being loosened, only the ceiling is being given a way past it.
    """
    harness.add("i-serving", launched_seconds_ago=CEILING + 60,
                max_lifetime=CEILING, idle_for=IDLE_TIMEOUT * 10)
    harness.pin("i-serving", "vllm-serve")

    # First, prove the idle path is still blocked by that same task: with no
    # ceiling on an identical box, the server pins it exactly as before.
    harness.add("i-serving-no-ceiling", idle_for=IDLE_TIMEOUT * 10)
    harness.pin("i-serving-no-ceiling", "vllm-serve")

    await harness.dispatcher._check_idle()

    assert harness.terminated() == ["i-serving"]
    assert harness.calls[0][1]["force"] is False   # never unattended force
    assert "ceiling_termination" in harness.audit()
    assert "idle_termination" not in harness.audit()


async def test_ceiling_defers_to_a_running_batch_job(harness):
    """A batch job has a 90%. Destroying a fine-tune at 90% to save a billing
    hour is the trade this project refuses to make; the deferral is recorded
    (once) so the box is not silently over its limit."""
    harness.add("i-batch", launched_seconds_ago=CEILING + 60,
                max_lifetime=CEILING)
    harness.pin("i-batch", "whisper-batch")

    await harness.dispatcher._check_idle()
    assert harness.calls == []
    assert "ceiling_deferred" in harness.audit()
    assert harness.dispatcher.ceiling_status("i-batch")[
        "ceiling_deferred_by"] == "batch job running"


async def test_ceiling_deferral_is_recorded_once_not_every_pass(harness):
    """The idle loop runs every 15 seconds. An audit row per pass is 5,760
    rows a day per instance, which buries the rows that matter."""
    harness.add("i-batch", launched_seconds_ago=CEILING + 60,
                max_lifetime=CEILING)
    harness.pin("i-batch", "whisper-batch")

    for _ in range(5):
        await harness.dispatcher._check_idle()
    assert harness.audit().count("ceiling_deferred") == 1


async def test_ceiling_defers_to_a_live_auto_managed_job(harness):
    """An auto-managed job owns its instance's teardown; a second terminate
    would race its lifecycle and re-enter the rescue twice."""
    launch_id = harness.add("i-auto", launched_seconds_ago=CEILING + 60,
                            max_lifetime=CEILING)
    harness.own_auto("i-auto", launch_id, "running")

    await harness.dispatcher._check_idle()
    assert harness.calls == []
    assert harness.dispatcher.ceiling_status("i-auto")[
        "ceiling_deferred_by"] == "auto-managed job owns teardown"


async def test_auto_managed_stuck_in_terminating_is_not_destroyed_but_is_loud(
        tmp_path, db):
    """A blocked auto-managed teardown parks in 'terminating' and retries
    force=False forever. That is the one state where money burns without
    bound, so it must not be treated as "handled" — but Manifold still does
    not issue a destroy of its own (the lifecycle is already retrying, and
    forcing is never automatic). It notifies instead, naming what is stuck.
    """
    notifier = RecordingNotifier()
    harness = Harness(tmp_path, db, notifier=notifier)
    launch_id = harness.add("i-stuck", launched_seconds_ago=CEILING + 60,
                            max_lifetime=CEILING)
    harness.own_auto("i-stuck", launch_id, "terminating",
                     detail="termination blocked: 2 file(s) could not be saved")

    await harness.dispatcher._check_idle()

    assert harness.calls == []                       # NOT destroyed
    kinds = [k for k, *_ in notifier.sent]
    assert kinds == ["instance_ceiling"]
    assert "2 file(s) could not be saved" in notifier.sent[0][2]
    assert notifier.sent[0][3] == "i-stuck"

    # ...and it says it once, not every 15 seconds.
    for _ in range(4):
        await harness.dispatcher._check_idle()
    assert len(notifier.sent) == 1


# -- the ceiling beats the switches the user can flip ---------------------------


async def test_ceiling_overrides_keep_alive(harness):
    """Keep-alive stops the IDLE clock. It is not a way to opt out of a
    ceiling you set yourself — that would make the ceiling advisory."""
    harness.add("i-kept", launched_seconds_ago=CEILING + 60,
                max_lifetime=CEILING, keep_alive=True)
    await harness.dispatcher._check_idle()
    assert harness.terminated() == ["i-kept"]
    assert harness.calls[0][1]["force"] is False   # never unattended force


def test_keep_alive_audit_admits_the_ceiling_still_applies(harness):
    """"idle auto-termination off" was the whole truth until a box could also
    carry a ceiling. An audit line that over-promises about a destructive
    control is worse than no line."""
    harness.add("i-kept", launched_seconds_ago=60, max_lifetime=CEILING)
    harness.dispatcher.set_keep_alive("i-kept", True)
    detail = harness.db._execute(
        "SELECT detail FROM audit_log WHERE action = 'keep_alive'"
    ).fetchone()["detail"]
    assert "max-lifetime ceiling still applies" in detail

    harness.add("i-plain", launched_seconds_ago=60)
    harness.dispatcher.set_keep_alive("i-plain", True)
    rows = harness.db._execute(
        "SELECT detail FROM audit_log WHERE action = 'keep_alive' ORDER BY id"
    ).fetchall()
    assert "ceiling" not in rows[1]["detail"]        # no ceiling, no promise


# -- data safety is not negotiable on this path ---------------------------------


async def test_ceiling_honours_a_blocked_termination(harness):
    """force=False means terminate() rescues first and refuses if a file
    could not be saved. The ceiling does not get to override that: data beats
    billing, and the box is left up and recorded."""
    harness.add("i-blocked", launched_seconds_ago=CEILING + 60,
                max_lifetime=CEILING)
    harness.raises["i-blocked"] = TerminationBlocked(
        "i-blocked", [{"path": "checkpoints/step-2000.safetensors"}])

    await harness.dispatcher._check_idle()

    assert harness.calls[0][1]["force"] is False   # never unattended force
    assert "i-blocked" in harness.orch.connections   # still up
    assert "ceiling_termination_blocked" in harness.audit()


async def test_a_blocked_termination_backs_off_instead_of_storming(harness):
    """Every blocked retry re-runs the FULL rescue (sidecar walk, whole-scratch
    rsync, per-file downloads) against files that have not moved. At the 15s
    poll that is four full rescues a minute, forever."""
    harness.add("i-blocked", launched_seconds_ago=CEILING + 60,
                max_lifetime=CEILING)
    harness.raises["i-blocked"] = TerminationBlocked("i-blocked", [{"path": "a"}])

    await harness.dispatcher._check_idle()
    assert len(harness.calls) == 1
    for _ in range(5):                       # same instant: still backing off
        await harness.dispatcher._check_idle()
    assert len(harness.calls) == 1

    harness.now += 16                        # first backoff step is 15s
    await harness.dispatcher._check_idle()
    assert len(harness.calls) == 2
    harness.now += 16                        # ...and the next one is longer
    await harness.dispatcher._check_idle()
    assert len(harness.calls) == 2


async def test_the_unreachable_box_outlives_its_ceiling_and_says_so(
        tmp_path, db):
    """A rescue over a dead connection returns an empty report, so
    terminating an unreachable box would destroy its data behind a rescue
    that did nothing. Manifold therefore does not terminate it — and says
    that plainly rather than letting the user believe the limit held."""
    notifier = RecordingNotifier()
    harness = Harness(tmp_path, db, notifier=notifier)
    harness.add("i-lost", connected=False, launched_seconds_ago=CEILING + 60,
                max_lifetime=CEILING)

    await harness.dispatcher._check_idle()

    assert harness.calls == []
    assert "ceiling_unreachable" in harness.audit()
    assert "i-lost" not in harness.dispatcher.last_activity
    assert [k for k, *_ in notifier.sent] == ["instance_ceiling"]
    assert "unreachable" in notifier.sent[0][1]


# -- degenerate anchors ----------------------------------------------------------


async def test_null_launched_at_is_a_no_op_not_a_crash(harness):
    """An adopted or repaired row can carry a ceiling with no anchor. Unknown
    age must mean "do nothing", never "destroy it"."""
    harness.add("i-anchorless", max_lifetime=CEILING)      # launched_at NULL
    await harness.dispatcher._check_idle()
    assert harness.calls == []
    status = harness.dispatcher.ceiling_status("i-anchorless")
    assert status["max_lifetime_seconds"] == CEILING
    assert status["ceiling_seconds_remaining"] is None     # not a fake zero


async def test_garbage_launched_at_is_a_no_op(harness):
    harness.add("i-garbage", max_lifetime=CEILING,
                launched_at="not-a-timestamp")
    await harness.dispatcher._check_idle()
    assert harness.calls == []


async def test_a_naive_timestamp_is_read_as_utc(harness):
    """Rows written before the DB stamped a timezone must not read as an
    instance launched hours in the future (which would never hit a ceiling)."""
    naive = (datetime.now(timezone.utc) - timedelta(seconds=CEILING + 600)
             ).replace(tzinfo=None).isoformat(timespec="seconds")
    harness.add("i-naive", max_lifetime=CEILING, launched_at=naive)
    await harness.dispatcher._check_idle()
    assert harness.terminated() == ["i-naive"]


async def test_orphan_repaired_row_fires_within_one_poll(harness):
    """76a leaves `active_at` NULL on an orphan-repaired row on purpose (we
    never saw that boot finish), so the ceiling cannot depend on it. A box
    that has been billing for two weeks unnoticed is exactly the case the
    ceiling exists for: it fires on the first poll after adoption, and it
    still goes through the rescuing terminate."""
    launch_id = harness.add("i-orphan", launched_seconds_ago=14 * 86400,
                            max_lifetime=CEILING)
    row = harness.db.get_launch(launch_id)
    assert row["active_at"] is None

    await harness.dispatcher._check_idle()
    assert harness.terminated() == ["i-orphan"]
    assert harness.calls[0][1]["force"] is False   # never unattended force


# -- one bad instance must not disable the loop ----------------------------------


async def test_a_provider_error_on_one_box_does_not_abandon_the_others(harness):
    """Only TerminationBlocked used to be caught here, so a ProviderError out
    of terminate() escaped to the loop's blanket handler and abandoned every
    instance later in the sweep — silently, every cycle, for as long as the
    bad box stayed up."""
    harness.add("i-poison", launched_seconds_ago=CEILING + 60,
                max_lifetime=CEILING)
    harness.add("i-second", idle_for=IDLE_TIMEOUT + 60)
    harness.raises["i-poison"] = ProviderError("lambda is having a day")

    await harness.dispatcher._check_idle()

    assert harness.terminated() == ["i-poison", "i-second"]
    assert all(kwargs["force"] is False for _, kwargs in harness.calls)


async def test_every_branch_terminates_with_force_false(tmp_path, db):
    """One assertion over every path that can reach a destroy: the kwarg, not
    just the call. force=True is the user's explicit "burn it" and no
    unattended loop may ever issue it."""
    harness = Harness(tmp_path, db)
    harness.add("i-ceiling", launched_seconds_ago=CEILING + 60,
                max_lifetime=CEILING)
    harness.add("i-ceiling-keepalive", launched_seconds_ago=CEILING + 60,
                max_lifetime=CEILING, keep_alive=True)
    harness.add("i-ceiling-serving", launched_seconds_ago=CEILING + 60,
                max_lifetime=CEILING)
    harness.pin("i-ceiling-serving", "vllm-serve")
    harness.add("i-idle", idle_for=IDLE_TIMEOUT + 60)

    await harness.dispatcher._check_idle()

    assert sorted(harness.terminated()) == [
        "i-ceiling", "i-ceiling-keepalive", "i-ceiling-serving", "i-idle"]
    assert all(kwargs["force"] is False for _, kwargs in harness.calls)
    assert len(harness.calls) == 4


# -- the warning ------------------------------------------------------------------


async def test_one_warning_on_a_fixed_lead_before_the_ceiling(tmp_path, db):
    notifier = RecordingNotifier()
    harness = Harness(tmp_path, db, notifier=notifier)
    lead = harness.settings.idle.ceiling_warning_seconds
    harness.add("i-soon", launched_seconds_ago=CEILING - lead + 60,
                max_lifetime=CEILING)

    for _ in range(4):
        await harness.dispatcher._check_idle()

    assert [k for k, *_ in notifier.sent] == ["instance_ceiling"]
    assert "max lifetime" in notifier.sent[0][1]
    # F5: the copy owns the limit rather than hiding it.
    assert "if it can reach it and save its files first" in notifier.sent[0][2]
    assert harness.calls == []


async def test_no_warning_for_a_ceiling_shorter_than_twice_the_lead(
        tmp_path, db):
    """A 10-minute warning on a 15-minute ceiling lands at launch and means
    nothing."""
    notifier = RecordingNotifier()
    harness = Harness(tmp_path, db, notifier=notifier)
    lead = harness.settings.idle.ceiling_warning_seconds
    harness.add("i-short", launched_seconds_ago=60, max_lifetime=lead + 30)
    await harness.dispatcher._check_idle()
    assert notifier.sent == []


async def test_no_warning_when_no_ceiling_is_set(tmp_path, db):
    notifier = RecordingNotifier()
    harness = Harness(tmp_path, db, notifier=notifier)
    harness.add("i-plain", launched_seconds_ago=30 * 86400)
    await harness.dispatcher._check_idle()
    assert notifier.sent == []


# -- the bound, at both write paths ------------------------------------------------


async def test_a_ceiling_under_the_boot_budget_is_rejected_not_clamped(
        orchestrator, settings):
    """`launched_at` is stamped when the provider ACCEPTS the launch, before
    the box boots, and boot is 15-40 minutes on a multi-GPU instance. A
    30-minute ceiling would destroy an 8xH100 the moment it became usable.

    REJECTED, not silently raised: quietly doubling a number the user typed
    into a destructive control is its own kind of lie."""
    from app.orchestrator import LaunchRejected, max_lifetime_bounds

    minimum, maximum = max_lifetime_bounds(settings)
    assert minimum == (settings.launch.boot_timeout_seconds
                       + settings.idle.timeout_seconds)

    with pytest.raises(LaunchRejected) as caught:
        await orchestrator.request_launch(
            instance_type="gpu_1x_a10", region="us-east", filesystem="",
            max_lifetime_seconds=minimum - 1)
    assert caught.value.status_code == 400
    assert "boot" in caught.value.detail.lower()
    assert str(int(settings.launch.boot_timeout_seconds)) in caught.value.detail

    with pytest.raises(LaunchRejected):
        await orchestrator.request_launch(
            instance_type="gpu_1x_a10", region="us-east", filesystem="",
            max_lifetime_seconds=maximum + 1)

    ok = await orchestrator.request_launch(
        instance_type="gpu_1x_a10", region="us-east", filesystem="",
        max_lifetime_seconds=minimum)
    assert ok["max_lifetime_seconds"] == minimum


async def test_no_ceiling_is_still_the_default_on_the_launch_path(
        orchestrator):
    launch = await orchestrator.request_launch(
        instance_type="gpu_1x_a10", region="us-east", filesystem="")
    assert launch["max_lifetime_seconds"] is None


def test_max_lifetime_route_mirrors_the_launch_bound(client, settings):
    from tests.test_safety_hook import launch_and_wait, wait_connected

    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)
    minimum = (settings.launch.boot_timeout_seconds
               + settings.idle.timeout_seconds)

    too_short = client.post(f"/instances/{instance_id}/max-lifetime",
                            json={"max_lifetime_seconds": minimum - 1})
    assert too_short.status_code == 400
    assert "boot" in too_short.json()["detail"].lower()

    ok = client.post(f"/instances/{instance_id}/max-lifetime",
                     json={"max_lifetime_seconds": CEILING})
    assert ok.status_code == 200
    assert ok.json()["max_lifetime_seconds"] == CEILING

    db = client.app.state.orchestrator.db
    assert db.find_launch_by_instance(
        instance_id)["max_lifetime_seconds"] == CEILING

    cleared = client.post(f"/instances/{instance_id}/max-lifetime",
                          json={"max_lifetime_seconds": None})
    assert cleared.json()["max_lifetime_seconds"] is None
    assert db.find_launch_by_instance(
        instance_id)["max_lifetime_seconds"] is None
    assert "max_lifetime_update" in [
        e["action"] for e in client.get("/audit").json()["entries"]]


def test_route_404s_for_an_instance_manifold_never_launched(client):
    resp = client.post("/instances/i-nope/max-lifetime",
                       json={"max_lifetime_seconds": CEILING})
    assert resp.status_code == 404


# -- the card ----------------------------------------------------------------------


def test_the_instance_card_carries_the_ceiling_outside_idle(client):
    """inst["idle"] is None for a box that is not connected, and a box that
    dropped off SSH past its ceiling is exactly the one whose limit the user
    needs to see. So the ceiling fields live on the instance itself."""
    from tests.test_safety_hook import launch_and_wait, wait_connected

    instance_id = launch_and_wait(client)
    wait_connected(client, instance_id)

    def card():
        return next(i for i in client.get("/instances").json()["instances"]
                    if i["id"] == instance_id)

    before = card()
    assert before["max_lifetime_seconds"] is None
    assert before["ceiling_seconds_remaining"] is None
    assert before["ceiling_deferred_by"] is None

    client.post(f"/instances/{instance_id}/max-lifetime",
                json={"max_lifetime_seconds": CEILING})
    after = card()
    assert after["max_lifetime_seconds"] == CEILING
    assert 0 < after["ceiling_seconds_remaining"] <= CEILING
    assert set(after["idle"]) == {
        "idle_seconds", "timeout_seconds", "keep_alive"}


def test_mock_mode_fixtures_carry_no_firing_ceiling(tmp_path):
    """Demo mode fabricates a month of launch history at wall-clock times in
    the past. If any of those rows carried a ceiling, opening the demo would
    start destroying the demo's own instances."""
    from app.db import Database
    from app.mock_seed import seed_mock_history

    path = str(tmp_path / "manifold-mock.db")
    seeded = Database(path)
    try:
        seed_mock_history(seeded, days=30,
                          now_iso=datetime.now(timezone.utc).isoformat(
                              timespec="seconds"),
                          mock=True, db_path=path)
        rows = seeded.list_launches()
        assert rows                                  # the seeder really ran
        assert all(r["max_lifetime_seconds"] is None for r in rows)
    finally:
        seeded.close()


def test_settings_status_publishes_the_bound(client, settings):
    body = client.get("/settings/status").json()
    assert body["max_lifetime_min_seconds"] == (
        settings.launch.boot_timeout_seconds + settings.idle.timeout_seconds)
    assert body["boot_timeout_seconds"] == settings.launch.boot_timeout_seconds


def test_the_ceiling_reaches_the_instance_through_a_real_launch(client, db):
    """End to end over HTTP: the value the client sends is the value the row
    carries and the value the dispatcher reads."""
    from tests.conftest import wait_for_launch_status

    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data", "max_lifetime_seconds": CEILING})
    assert resp.status_code == 202
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    assert launch["max_lifetime_seconds"] == CEILING

    dispatcher = client.app.state.dispatcher
    row = client.app.state.orchestrator.db.get_launch(launch["id"])
    assert dispatcher._ceiling_breach(row) is None      # brand new box
    status = dispatcher.ceiling_status(launch["lambda_instance_id"], row)
    assert status["max_lifetime_seconds"] == CEILING
    assert status["ceiling_seconds_remaining"] > 0
    assert time.time() > 0                              # (no sleeping here)
