"""Idle spend, and the multi-GPU telemetry it rests on.

Two properties carry this whole file, and both are safety properties rather
than accuracy niceties:

1. A telemetry sample describes the WHOLE BOX. Recording only GPU 0 made
   peak VRAM a GPU-0 figure, so a run that filled GPU 3 looked roomy and the
   right-size hint could send the next run into an OOM.
2. UNMEASURED IS NEVER IDLE, and IDLE IS NEVER ZERO BY DEFAULT. An instance
   nobody could sample must not accrue idle time, and an instance nobody
   ever sampled must report "not measured" rather than $0.00.
"""

from datetime import datetime, timedelta, timezone

from app.config import IdleSpendSettings, TelemetrySettings
from app.connections import ConnectionState
from app.dispatcher import Dispatcher
from app.lambda_api import MockLambdaClient
from app.notifications import NotificationCenter
from app.orchestrator import Orchestrator
from app.preferences import PreferenceStore
from app.spend import idle_spend, idle_window
from app.task_queue import SQLiteTaskQueue
from tests.conftest import make_settings, mock_connect_fn

CSV_TWO_GPUS = "NVIDIA H100, 40100, 81559, 92\nNVIDIA H100, 79000, 81559, 8\n"

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def at(seconds: float) -> str:
    """An ISO timestamp `seconds` after BASE, in the format db.utcnow writes
    (the windowed query compares these as strings)."""
    return iso(BASE + timedelta(seconds=seconds))


def launch_row(**overrides) -> dict:
    row = {
        "id": "l-1",
        "lambda_instance_id": "i-1",
        "launched_type": "gpu_8x_h100",
        "hourly_rate_cents": 2400,
        "launched_at": iso(BASE),
        "active_at": None,
        "terminated_at": None,
        "resolved_at": None,
    }
    row.update(overrides)
    return row


def samples(*pairs) -> list[dict]:
    """[(offset_seconds, util_pct_mean or None), ...] as telemetry rows."""
    return [{"at": at(offset), "util_pct_mean": mean} for offset, mean in pairs]


def steady(count: int, mean: float, *, every: float = 30.0,
           start: float = 0.0) -> list[dict]:
    return samples(*[(start + i * every, mean) for i in range(count)])


# -- the window --------------------------------------------------------------


def test_window_starts_at_launched_at_never_active_at():
    """active_at looks like the better anchor and is not one: Phase 76a
    leaves it NULL on every orphan-repaired row rather than fabricating a
    boot, so anchoring there would drop exactly the abandoned instances this
    accounting exists to find."""
    window = idle_window(
        launch_row(active_at=at(900)), now_iso=at(3600))
    assert window["start_iso"] == iso(BASE)
    assert window["seconds"] == 3600


def test_window_ends_on_the_best_evidence_available():
    ended = idle_window(launch_row(terminated_at=at(600),
                                   resolved_at=at(9999)), now_iso=at(99999))
    assert (ended["end_basis"], ended["seconds"]) == ("terminated_at", 600)

    swept = idle_window(launch_row(resolved_at=at(600)), now_iso=at(99999))
    assert (swept["end_basis"], swept["seconds"]) == ("resolved_at", 600)

    running = idle_window(launch_row(), now_iso=at(600))
    assert (running["end_basis"], running["seconds"]) == ("now", 600)


def test_window_is_none_without_launched_at():
    assert idle_window(launch_row(launched_at=None), now_iso=at(600)) is None


# -- unknown is not idle, and not zero ---------------------------------------


def test_no_samples_at_all_is_unmeasured_never_zero_idle():
    """The NORMAL case for an adopted instance: no sidecar, ssh fallback
    failed, zero rows. The UI must be able to say "not measured"; if this
    ever returns 0 it will say "$0.00 idle" about a box nothing is known
    about."""
    report = idle_spend(launch_row(), [], now_iso=at(3600))
    assert report["available"] is True
    assert report["measured"] is False
    assert report["idle_seconds"] is None
    assert report["idle_usd"] is None
    assert report["unknown_seconds"] == 3600
    assert "not measured" in report["reason"]


def test_old_sidecar_samples_are_unmeasured_not_idle():
    """A sidecar frozen into a running instance predates util_pct_mean and
    omits it. NULL must not collapse into 0% utilization."""
    report = idle_spend(launch_row(), samples((0, None), (30, None), (60, None)),
                        now_iso=at(3600))
    assert report["measured"] is False
    assert report["idle_seconds"] is None
    assert report["sample_count"] == 3        # they were seen, just not usable


def test_a_sampling_gap_is_unknown_not_idle():
    """An unreachable box must never accrue idle time: that would bill a user
    for being unmonitorable. Two samples an hour apart measure one interval
    each, not an hour."""
    report = idle_spend(launch_row(), samples((0, 0.0), (3600, 0.0)),
                        now_iso=at(7200), sample_interval_seconds=30)
    assert report["idle_seconds"] == 60       # 2 samples x 30s, not 3600
    assert report["unknown_seconds"] == 7140
    assert report["coverage"] < 0.01


def test_time_before_the_first_sample_is_unknown():
    """Boot lands here: no sidecar is answering yet, so it is unmeasured
    time rather than idle time."""
    report = idle_spend(launch_row(), steady(10, 0.0, start=1800),
                        now_iso=at(3600), sample_interval_seconds=30)
    assert report["idle_seconds"] == 300
    assert report["unknown_seconds"] == 3300


def test_samples_outside_the_window_are_ignored():
    report = idle_spend(launch_row(terminated_at=at(600)),
                        steady(20, 0.0, start=1200), now_iso=at(9999))
    assert report["measured"] is False
    assert report["sample_count"] == 0


# -- the split ---------------------------------------------------------------


def test_idle_and_busy_split_on_the_threshold():
    report = idle_spend(
        launch_row(),
        steady(60, 1.0) + steady(60, 90.0, start=1800),
        now_iso=at(3600), sample_interval_seconds=30, util_pct=5)
    assert report["idle_seconds"] == 1800
    assert report["busy_seconds"] == 1800
    assert report["unknown_seconds"] == 0
    assert report["coverage"] == 1.0
    # 30 min of an $24/hr box: half the bill, reported as idle spend.
    assert report["idle_usd"] == 12.0


def test_idle_reads_the_mean_never_the_max():
    """THE load-bearing rule. One busy GPU out of eight must not hide the
    seven idle ones: with the max, this box reports zero idle spend and the
    number a spend-safety tool exists to produce is silently gone."""
    box = [{"at": at(i * 30), "util_pct": 99, "util_pct_mean": 12.4}
           for i in range(60)]
    report = idle_spend(launch_row(), box, now_iso=at(1800),
                        sample_interval_seconds=30, util_pct=15)
    assert report["idle_seconds"] == 1800
    assert report["idle_usd"] == 12.0


def test_short_windows_are_not_judged():
    report = idle_spend(launch_row(terminated_at=at(120)), steady(4, 0.0),
                        now_iso=at(600), min_window_seconds=600)
    assert report["available"] is False
    assert report["idle_seconds"] is None
    assert "too short" in report["reason"]


def test_idle_usd_is_none_when_the_rate_is_unknown():
    """Same discipline as launch_cost: an unknown cost is never $0.00."""
    report = idle_spend(launch_row(hourly_rate_cents=None), steady(60, 0.0),
                        now_iso=at(3600), sample_interval_seconds=30)
    assert report["idle_seconds"] == 1800
    assert report["idle_usd"] is None
    assert report["rate_known"] is False


def test_spans_never_overlap_when_sampling_runs_early():
    """Jitter samples faster than the nominal interval; each sample may only
    speak up to the next one, so measured time cannot exceed the window."""
    report = idle_spend(launch_row(), steady(120, 0.0, every=10),
                        now_iso=at(1200), sample_interval_seconds=30)
    # 120 samples claiming the nominal 30s each would be 3600s of idle time
    # inside a 1200s window - a third of an hour of money that never existed.
    assert report["idle_seconds"] == 1200 == report["window_seconds"]
    assert report["unknown_seconds"] == 0


# -- recording: one row per box, not per card --------------------------------


class FakeConn:
    state = ConnectionState.CONNECTED

    def __init__(self, exit_code=0, stdout=CSV_TWO_GPUS):
        self.exit_code, self.stdout = exit_code, stdout

    async def run(self, command, **kwargs):
        return self.exit_code, self.stdout, ""


class FakeSidecar:
    """A sidecar whose payload is whatever the test hands it - including an
    OLD one that never heard of a field."""

    def __init__(self, gpus):
        self._gpus = gpus

    async def metrics(self):
        return {"available": True, "gpus": self._gpus}


def make_dispatcher(tmp_path, db, *, sidecar=None, notifier=None,
                    stdout=CSV_TWO_GPUS, **overrides):
    settings = make_settings(
        tmp_path, telemetry=TelemetrySettings(sample_seconds=30), **overrides)
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn,
                        sidecar_factory=(None if sidecar is None
                                         else lambda conn: sidecar))
    orch.connections["i-1"] = FakeConn(stdout=stdout)
    return Dispatcher(settings, orch, SQLiteTaskQueue(db), {}, db,
                      MockLambdaClient(), notifier=notifier)


async def test_sample_records_the_whole_box_not_gpu_zero(tmp_path, db):
    """GPU 0 is at 40 GB and 92%; GPU 1 is at 79 GB and 8%. Recording GPU 0
    alone under-reports peak VRAM by 39 GB, which is how a right-size hint
    talks you into an OOM."""
    dispatcher = make_dispatcher(tmp_path, db)
    await dispatcher._sample_telemetry_once()

    row = db.telemetry_samples_between("i-1", "0000", "9999")[0]
    summary = db.telemetry_summary("i-1")
    assert summary["peak_vram_used_mib"] == 79000      # the fuller card
    assert row["util_pct"] == 92                       # max: the hint's input
    assert row["util_pct_mean"] == 50.0                # mean: idle's input
    assert row["gpu_count"] == 2
    assert summary["gpu_count"] == 2


async def test_absent_fields_record_null_never_zero(tmp_path, db):
    """An old sidecar omits utilization entirely. int(x.get(key, 0)) would
    store 0% - a box we know nothing about, billed as fully idle."""
    old = FakeSidecar([{"name": "NVIDIA A10", "vram_used_mib": 8000,
                        "vram_total_mib": 24564}])
    dispatcher = make_dispatcher(tmp_path, db, sidecar=old)
    await dispatcher._sample_telemetry_once()

    row = db.telemetry_samples_between("i-1", "0000", "9999")[0]
    assert row["util_pct"] is None
    assert row["util_pct_mean"] is None
    assert row["gpu_count"] == 1


def test_windowed_read_only_returns_samples_inside_the_window(db):
    db.record_telemetry_sample("i-1", gpu_name="A10", vram_used_mib=1,
                               vram_total_mib=2, util_pct=3,
                               util_pct_mean=3.0, gpu_count=1, at=at(0))
    db.record_telemetry_sample("i-1", gpu_name="A10", vram_used_mib=1,
                               vram_total_mib=2, util_pct=3,
                               util_pct_mean=3.0, gpu_count=1, at=at(7200))
    db.record_telemetry_sample("i-other", gpu_name="A10", vram_used_mib=1,
                               vram_total_mib=2, util_pct=3,
                               util_pct_mean=3.0, gpu_count=1, at=at(30))

    inside = db.telemetry_samples_between("i-1", at(0), at(3600))
    assert [s["at"] for s in inside] == [at(0)]


# -- the notification --------------------------------------------------------


def seed_idle_instance(db, *, rate_cents=2400, minutes=45) -> str:
    """A launch that has been running for `minutes` with every sample idle."""
    launch_id = db.create_launch(
        requested_type="gpu_8x_h100", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=rate_cents)
    started = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    db.update_launch(launch_id, status="active", lambda_instance_id="i-1",
                     launched_type="gpu_8x_h100",
                     launched_at=iso(started))
    for i in range(minutes * 2):          # one every 30s
        db.record_telemetry_sample(
            "i-1", gpu_name="NVIDIA H100", vram_used_mib=79000,
            vram_total_mib=81559, util_pct=99, util_pct_mean=0.0, gpu_count=8,
            at=iso(started + timedelta(seconds=30 * i)))
    return launch_id


def make_notifier(db, pings):
    return NotificationCenter(db, PreferenceStore(db),
                              sender=lambda title, body: pings.append((title, body)))


async def test_idle_instance_pings_once_with_the_money(tmp_path, db):
    """An idle instance nobody noticed is the point of the feature, and the
    ping is framed as money because that is the decision being made."""
    pings: list[tuple[str, str]] = []
    seed_idle_instance(db)
    dispatcher = make_dispatcher(tmp_path, db,
                                 notifier=make_notifier(db, pings))

    await dispatcher._sample_telemetry_once()
    await dispatcher._sample_telemetry_once()     # deduped, not a second ping

    rows = [n for n in db.list_notifications()if n["kind"] == "instance_idle"]
    assert len(rows) == 1
    assert len(pings) == 1
    assert "gpu_8x_h100" in rows[0]["title"]
    assert "$" in rows[0]["body"]
    assert rows[0]["ref"] == "idle:i-1"


async def test_idle_ping_stays_quiet_below_the_money_gate(tmp_path, db):
    """45 idle minutes on a cheap box is not worth interrupting someone for.
    Both gates must be met."""
    pings: list[tuple[str, str]] = []
    seed_idle_instance(db, rate_cents=10)
    dispatcher = make_dispatcher(tmp_path, db,
                                 notifier=make_notifier(db, pings))

    await dispatcher._sample_telemetry_once()
    assert db.list_notifications() == []


async def test_idle_ping_never_fires_on_unmeasured_time(tmp_path, db):
    """A box with no usable telemetry is unknown, not idle - it must never
    be reported as idle spend, however long it has been running."""
    pings: list[tuple[str, str]] = []
    launch_id = db.create_launch(
        requested_type="gpu_8x_h100", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=2400)
    db.update_launch(launch_id, status="active", lambda_instance_id="i-1",
                     launched_type="gpu_8x_h100",
                     launched_at=iso(datetime.now(timezone.utc)
                                     - timedelta(hours=6)))
    old = FakeSidecar([{"name": "NVIDIA H100", "vram_used_mib": 8000,
                        "vram_total_mib": 81559}])
    dispatcher = make_dispatcher(tmp_path, db, sidecar=old,
                                 notifier=make_notifier(db, pings))

    await dispatcher._sample_telemetry_once()
    assert db.list_notifications() == []


async def test_idle_settings_are_honoured(tmp_path, db):
    """The gates are config, not constants: raising notify_usd silences a
    ping that the default would have raised."""
    pings: list[tuple[str, str]] = []
    seed_idle_instance(db)
    dispatcher = make_dispatcher(
        tmp_path, db, notifier=make_notifier(db, pings),
        idle_spend=IdleSpendSettings(notify_usd=1000.0))

    await dispatcher._sample_telemetry_once()
    assert db.list_notifications() == []
