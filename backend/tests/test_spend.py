"""Spend accounting: the six cost states, and the promise that an unknown
cost is reported as unknown rather than as $0.

Everything here is pure (app/spend.py does no I/O), but the rows are seeded
through the real Database so the tests also prove the column names and the
update_launch whitelist line up — a silently-dropped write would otherwise
look exactly like a $0 launch.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import spend

# A10: 129 cents/hour in the mock catalog (lambda_api.py).
RATE_CENTS = 129
BASE = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)


def _iso(seconds_after_base: float = 0) -> str:
    return (BASE + timedelta(seconds=seconds_after_base)).isoformat(
        timespec="seconds")


def _seed_launch(db, instance_id="i-a10", *, gpu_type="gpu_1x_a10",
                 rate_cents=RATE_CENTS, region="us-east-1", **fields):
    """One launch row, written the way the launch pipeline writes it."""
    lid = db.create_launch(
        requested_type=gpu_type, region=region,
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=rate_cents,
    )
    db.update_launch(lid, lambda_instance_id=instance_id,
                     launched_type=gpu_type, **fields)
    return lid


def _row(db, launch_id):
    return db.get_launch(launch_id)


# -- the six states -----------------------------------------------------------

def test_never_started_is_a_known_zero(db):
    """No launched_at means no instance ever existed: the ONE state where $0
    is a fact rather than a guess."""
    lid = _seed_launch(db, status="failed", error="no capacity")
    db.update_launch(lid, lambda_instance_id=None)

    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(3600))
    assert cost["state"] == "never_started"
    assert cost["usd"] == 0.0
    assert cost["seconds"] == 0.0
    assert cost["boot_seconds"] is None


def test_billed_duration_rounds_up_to_the_next_minute(db):
    """Lambda bills in one-minute increments, so 45m01s is 46 minutes."""
    lid = _seed_launch(db, status="terminated", launched_at=_iso(),
                       active_at=_iso(300),
                       terminated_at=_iso(45 * 60 + 1))

    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(99999))
    assert cost["state"] == "billed"
    assert cost["seconds"] == 46 * 60
    assert cost["usd"] == pytest.approx(46 / 60 * 1.29)
    assert cost["usd_low"] == cost["usd"] == cost["usd_high"]
    assert cost["boot_seconds"] == 300.0


def test_exactly_whole_minutes_are_not_rounded_up(db):
    lid = _seed_launch(db, status="terminated", launched_at=_iso(),
                       terminated_at=_iso(45 * 60))
    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(99999))
    assert cost["seconds"] == 45 * 60


def test_billing_accrues_against_the_clock_the_caller_passes(db):
    lid = _seed_launch(db, status="active", launched_at=_iso(),
                       active_at=_iso(120))

    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(1800),
                             live_ids={"i-a10"})
    assert cost["state"] == "billing"
    assert cost["usd"] == pytest.approx(0.5 * 1.29)
    assert cost["boot_seconds"] == 120.0


def test_orphaned_when_a_failed_row_is_still_live(db):
    """The dangerous one: the row says the launch failed, but the instance is
    listed as running, so it is burning money right now."""
    lid = _seed_launch(db, status="failed", launched_at=_iso(),
                       error="boot timed out")

    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(3600),
                             live_ids={"i-a10"})
    assert cost["state"] == "orphaned"
    assert cost["usd"] == pytest.approx(1.29)
    # We never saw it boot, so boot time stays unknown rather than becoming 0.
    assert cost["boot_seconds"] is None


def test_unresolved_reports_a_range_and_never_a_point(db):
    lid = _seed_launch(db, status="failed", launched_at=_iso(),
                       last_seen_at=_iso(7200))

    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(3 * 3600),
                             live_ids=set())
    assert cost["state"] == "unresolved"
    assert cost["usd"] is None          # a point estimate would be invented
    assert cost["seconds"] is None
    assert cost["usd_low"] == pytest.approx(2 * 1.29)   # alive at least this long
    assert cost["usd_high"] == pytest.approx(3 * 1.29)  # + the unwatched hour
    assert cost["bound_basis"] == "last_seen_at"
    assert cost["capped"] is False


def test_unresolved_ceiling_is_identical_at_40_80_and_365_days(db):
    """THE regression test. The ceiling must come from evidence, never from
    the clock: an id missing from a provider that answered is positive proof
    the instance is NOT running now, so "it might have billed until this
    moment" is a claim we have already disproved. Left in, it grew a stale
    boot-timeout row into a five-figure "upper bound"."""
    lid = _seed_launch(db, status="failed", launched_at=_iso(),
                       error="boot timed out")
    row = _row(db, lid)

    ceilings = {
        days: spend.launch_cost(row, now_iso=_iso(days * 86400),
                                live_ids=set(),
                                boot_timeout_seconds=2400.0)["usd_high"]
        for days in (40, 80, 365)
    }
    assert len(set(ceilings.values())) == 1, ceilings
    assert ceilings[365] == pytest.approx(2400 / 3600 * 1.29)


def test_a_row_with_no_evidence_is_bounded_by_the_boot_timeout(db):
    """No sighting, no resolution: a launch that failed at boot cannot have
    outlived its own boot timeout, and the floor is honestly zero because it
    may have died immediately."""
    lid = _seed_launch(db, status="failed", launched_at=_iso(),
                       error="boot timed out")

    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(42 * 86400),
                             live_ids=set(), boot_timeout_seconds=2400.0)
    assert cost["usd"] is None
    assert cost["usd_low"] == 0.0
    assert cost["usd_high"] == pytest.approx(2400 / 3600 * 1.29)
    assert cost["capped"] is True
    assert cost["bound_basis"] == "boot_timeout"


def test_a_row_that_never_came_up_is_bounded_by_its_boot_window(db):
    """The live-system case. resolved_at is evidence of ABSENCE at that
    moment, not of PRESENCE until it: this instance never came up
    (active_at NULL) and no sweep ever saw it (last_seen_at NULL), so it is
    bounded by its own boot window even though the sweep that noticed it was
    gone did not run for five days. Ranking resolved_at first billed a box
    that never passed a health check for five days of A100."""
    lid = _seed_launch(db, status="failed", launched_at=_iso(),
                       resolved_at=_iso(5 * 86400), error="boot timed out")

    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(6 * 86400),
                             live_ids=set(), boot_timeout_seconds=2400.0)
    assert cost["usd_high"] == pytest.approx(2400 / 3600 * 1.29)
    assert cost["bound_basis"] == "boot_timeout"
    assert cost["capped"] is True
    assert cost["usd_low"] == 0.0


def test_a_row_that_did_come_up_is_not_bounded_by_the_boot_window(db):
    """The mirror image: active_at proves it ran, so a crash six hours in is
    NOT capped at the boot timeout, and the floor rises to the boot."""
    lid = _seed_launch(db, status="failed", launched_at=_iso(),
                       active_at=_iso(10 * 60), resolved_at=_iso(6 * 3600),
                       error="pipeline crashed")

    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(6 * 86400),
                             live_ids=set(), boot_timeout_seconds=2400.0)
    assert cost["usd_high"] == pytest.approx(6 * 1.29)     # to resolved_at
    assert cost["usd_low"] == pytest.approx(10 / 60 * 1.29)  # it did boot
    assert cost["bound_basis"] == "resolved_at"
    assert cost["capped"] is False


def test_the_ceiling_never_rises_as_evidence_is_added(db):
    """Each bound is independently an upper bound, so the ceiling is a min()
    over all of them and adding one can only tighten it.

    The comparison holds the SIGHTING fixed on purpose. A sighting is proof
    the instance outlived its boot window, which correctly removes the
    boot-timeout term — so a sighted row may legitimately sit ABOVE an
    unsighted one. That is a different fact about a different row, not the
    ceiling loosening.
    """
    scenarios = [
        {"last_seen_at": _iso(35 * 60)},                        # sighting only
        {"last_seen_at": _iso(35 * 60), "resolved_at": _iso(6 * 3600)},
        {"last_seen_at": _iso(35 * 60), "resolved_at": _iso(50 * 60)},
    ]
    ceilings = []
    for i, evidence in enumerate(scenarios):
        lid = _seed_launch(db, f"i-{i}", status="failed", launched_at=_iso(),
                           **evidence)
        ceilings.append(spend.launch_cost(
            _row(db, lid), now_iso=_iso(9 * 86400), live_ids=set(),
            boot_timeout_seconds=2400.0)["usd_high"])

    assert ceilings == sorted(ceilings, reverse=True), ceilings
    # A resolved_at six hours out is looser than the sighting, so it changes
    # nothing; a tight one wins the min() and pulls the ceiling down.
    assert ceilings[0] == ceilings[1] == pytest.approx(95 / 60 * 1.29)
    assert ceilings[2] == pytest.approx(50 / 60 * 1.29)


def test_last_seen_evidence_raises_the_floor_and_lowers_the_ceiling(db):
    """Evidence must tighten the range from BOTH ends. The original code moved
    the floor (downwards, wrongly) and ignored the ceiling entirely."""
    now = _iso(40 * 86400)
    blind = _seed_launch(db, "i-blind", status="failed", launched_at=_iso())
    seen = _seed_launch(db, "i-seen", status="failed", launched_at=_iso(),
                        last_seen_at=_iso(35 * 60))
    # The same sighted row, once a sweep has also concluded it was gone.
    concluded = _seed_launch(db, "i-concluded", status="failed",
                             launched_at=_iso(), last_seen_at=_iso(35 * 60),
                             resolved_at=_iso(50 * 60))

    without = spend.launch_cost(_row(db, blind), now_iso=now, live_ids=set(),
                                boot_timeout_seconds=2400.0)
    sighted = spend.launch_cost(_row(db, seen), now_iso=now, live_ids=set(),
                                boot_timeout_seconds=2400.0)
    resolved = spend.launch_cost(_row(db, concluded), now_iso=now,
                                 live_ids=set(), boot_timeout_seconds=2400.0)

    # A sighting proves it billed at least that long: the floor rises off 0.
    assert without["usd_low"] == 0.0
    assert sighted["usd_low"] == pytest.approx(35 / 60 * 1.29)
    # And a tighter bound on the same row pulls the ceiling down.
    assert resolved["usd_high"] < sighted["usd_high"]
    assert resolved["usd_low"] == sighted["usd_low"]


def test_resolved_at_freezes_the_upper_bound(db):
    """Once a sweep records that it noticed the instance was gone, the top of
    the range is final and never moves again. (This row came up, so the boot
    window does not apply and resolved_at is genuinely the tightest bound.)"""
    lid = _seed_launch(db, status="failed", launched_at=_iso(),
                       active_at=_iso(600), resolved_at=_iso(2 * 3600))

    row = _row(db, lid)
    ceilings = [spend.launch_cost(row, now_iso=_iso(days * 86400),
                                  live_ids=set())["usd_high"]
                for days in (1, 40, 365)]
    assert ceilings == [pytest.approx(2 * 1.29)] * 3
    latest = spend.launch_cost(row, now_iso=_iso(365 * 86400), live_ids=set())
    assert latest["usd_low"] == pytest.approx(10 / 60 * 1.29)  # it did boot
    assert latest["bound_basis"] == "resolved_at"
    assert latest["capped"] is False


def test_the_tightest_bound_wins_when_several_apply(db):
    """last_seen_at plus the unwatched window is tighter than a resolved_at
    two hours later, so it sets the ceiling. A first-match chain ranking
    resolved_at first would have discarded it."""
    lid = _seed_launch(db, status="failed", launched_at=_iso(),
                       last_seen_at=_iso(3600), resolved_at=_iso(3 * 3600))

    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(9 * 86400),
                             live_ids=set())
    assert cost["usd_high"] == pytest.approx(2 * 1.29)   # sighting + the hour
    assert cost["bound_basis"] == "last_seen_at"
    assert cost["usd_low"] == pytest.approx(1.29)


def test_a_live_row_with_no_price_still_reports_as_burning(db):
    """Liveness outranks price. A null hourly_rate_cents must never demote a
    running box to `rate_unknown`, because that zeroes orphaned.count and
    silences the one alarm that matters: this thing is billing right now."""
    lid = _seed_launch(db, "i-orphan", status="failed", launched_at=_iso())
    db.update_launch(lid, hourly_rate_cents=None)

    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(3600),
                             live_ids={"i-orphan"})
    assert cost["state"] == "orphaned"      # NOT rate_unknown
    assert cost["usd"] is None              # ...but the money stays unknown
    assert cost["rate_known"] is False
    assert cost["seconds"] == 3600

    summary = spend.summarize(db.list_launches(), now_iso=_iso(3600),
                              live_ids={"i-orphan"})
    assert summary["orphaned"]["count"] == 1
    assert summary["orphaned"]["launch_ids"] == [lid]
    # The missing price is still reported, just not at liveness's expense.
    assert summary["rate_unknown_count"] == 1


def test_a_live_priced_row_is_billing_not_rate_unknown(db):
    """Same ordering on the healthy path: an active box with no price is
    `billing`, so it keeps its place in the live view."""
    lid = _seed_launch(db, "i-live", status="active", launched_at=_iso())
    db.update_launch(lid, hourly_rate_cents=None)

    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(3600),
                             live_ids={"i-live"})
    assert cost["state"] == "billing"
    assert cost["usd"] is None


def test_rate_unknown_is_unknown_and_never_zero(db):
    lid = _seed_launch(db, status="terminated", launched_at=_iso(),
                       terminated_at=_iso(45 * 60))
    db.update_launch(lid, hourly_rate_cents=None)

    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(99999))
    assert cost["state"] == "rate_unknown"
    assert cost["usd"] is None          # NOT 0.0
    assert cost["usd_low"] is None and cost["usd_high"] is None
    assert cost["seconds"] == 45 * 60   # the duration is still known
    assert cost["rate_known"] is False


def test_every_cost_state_is_reachable(db):
    """The state set is exhaustive and each one is producible from a real row."""
    rows = [
        _row(db, _seed_launch(db, "i-never", status="failed")),
        _row(db, _seed_launch(db, "i-billed", status="terminated",
                              launched_at=_iso(), terminated_at=_iso(600))),
        _row(db, _seed_launch(db, "i-live", status="active",
                              launched_at=_iso())),
        _row(db, _seed_launch(db, "i-orphan", status="failed",
                              launched_at=_iso())),
        _row(db, _seed_launch(db, "i-gone", status="failed",
                              launched_at=_iso())),
    ]
    no_rate = _seed_launch(db, "i-priceless", status="terminated",
                           launched_at=_iso(), terminated_at=_iso(600))
    db.update_launch(no_rate, hourly_rate_cents=None)
    rows.append(_row(db, no_rate))

    states = {spend.launch_cost(r, now_iso=_iso(3600),
                                live_ids={"i-live", "i-orphan"})["state"]
              for r in rows}
    assert states == set(spend.COST_STATES)


# -- evidence handling --------------------------------------------------------

def test_a_row_whose_provider_did_not_list_keeps_billing(db):
    """An absent id proves nothing when its provider could not be read, so the
    row is NOT written off — over-reporting a live box is the safe error."""
    lid = _seed_launch(db, status="active", launched_at=_iso())
    row = _row(db, lid)

    blind = spend.launch_cost(row, now_iso=_iso(3600), live_ids=set(),
                              provider_listed=False)
    assert blind["state"] == "billing"
    assert blind["usd"] == pytest.approx(1.29)

    # Same row, but this time its provider DID answer and did not list it.
    informed = spend.launch_cost(row, now_iso=_iso(3600), live_ids=set(),
                                 provider_listed=True)
    assert informed["state"] == "unresolved"


def test_no_live_snapshot_trusts_the_rows_own_status(db):
    lid = _seed_launch(db, status="active", launched_at=_iso())
    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(3600), live_ids=None)
    assert cost["state"] == "billing"


def test_malformed_timestamps_never_raise(db):
    lid = _seed_launch(db, status="terminated", launched_at="not-a-timestamp",
                       terminated_at="also-not-one")
    cost = spend.launch_cost(_row(db, lid), now_iso=_iso(3600))
    assert cost["state"] == "never_started"
    # And the aggregates survive it too.
    assert spend.summarize(db.list_launches(), now_iso=_iso(3600))["all_time_usd"] == 0.0


# -- aggregates ---------------------------------------------------------------

def test_summarize_keeps_unresolved_out_of_the_totals(db):
    _seed_launch(db, "i-billed", status="terminated", launched_at=_iso(),
                 terminated_at=_iso(3600))
    unresolved = _seed_launch(db, "i-gone", status="failed",
                              launched_at=_iso(), last_seen_at=_iso(3600))

    summary = spend.summarize(db.list_launches(), now_iso=_iso(2 * 3600),
                              live_ids=set())
    assert summary["all_time_usd"] == pytest.approx(1.29)   # the billed row only
    assert summary["today_usd"] == pytest.approx(1.29)
    assert summary["unresolved"]["count"] == 1
    assert summary["unresolved"]["launch_ids"] == [unresolved]
    assert summary["unresolved"]["usd_low"] == pytest.approx(1.29)
    assert summary["unresolved"]["usd_high"] == pytest.approx(2 * 1.29)
    assert summary["lower_bound"] is True
    assert "upper bound" in summary["disclaimer"]


def test_summarize_reports_orphans_burn_and_unpriced_rows(db):
    orphan = _seed_launch(db, "i-orphan", status="failed", launched_at=_iso())
    no_rate = _seed_launch(db, "i-priceless", status="terminated",
                           launched_at=_iso(), terminated_at=_iso(600))
    db.update_launch(no_rate, hourly_rate_cents=None)

    summary = spend.summarize(db.list_launches(), now_iso=_iso(3600),
                              live_ids={"i-orphan"})
    assert summary["orphaned"] == {"count": 1, "launch_ids": [orphan]}
    assert summary["rate_unknown_count"] == 1
    assert summary["live_burn_usd_per_hour"] == pytest.approx(1.29)


def test_summarize_buckets_in_the_callers_timezone(db):
    """A PST user's evening launch belongs to THEIR today, not to UTC's
    tomorrow."""
    evening_pst = datetime(2026, 8, 11, 3, 30, tzinfo=timezone.utc)
    lid = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=RATE_CENTS)
    db.update_launch(
        lid, status="terminated", lambda_instance_id="i-pst",
        launched_at=evening_pst.isoformat(timespec="seconds"),
        terminated_at=(evening_pst + timedelta(hours=1)).isoformat(
            timespec="seconds"))
    now = datetime(2026, 8, 11, 5, 0, tzinfo=timezone.utc).isoformat(
        timespec="seconds")
    rows = db.list_launches()

    utc = spend.series(rows, now_iso=now, days=3)
    pacific = spend.series(rows, now_iso=now, days=3, tz_offset_minutes=-480)
    assert utc[-1]["bucket"] == "2026-08-11"
    assert utc[-1]["usd"] == pytest.approx(1.29)
    assert pacific[-1]["bucket"] == "2026-08-10"      # their own evening
    assert pacific[-1]["usd"] == pytest.approx(1.29)

    summary = spend.summarize(rows, now_iso=now, tz_offset_minutes=-480)
    assert summary["today_usd"] == pytest.approx(1.29)
    assert summary["timezone_label"] == "UTC-08:00"
    assert summary["timezone_offset_minutes"] == -480


def test_series_is_gap_filled_and_oldest_first(db):
    old = BASE - timedelta(days=3)
    lid = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=RATE_CENTS)
    db.update_launch(
        lid, status="terminated", lambda_instance_id="i-old",
        launched_at=old.isoformat(timespec="seconds"),
        terminated_at=(old + timedelta(hours=2)).isoformat(timespec="seconds"))

    points = spend.series(db.list_launches(), now_iso=_iso(), days=5)
    assert [p["bucket"] for p in points] == [
        "2026-08-06", "2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10"]
    assert points[1]["usd"] == pytest.approx(2.58)
    # Every other day in the window is present, and zero.
    assert [p["usd"] for p in points if p["bucket"] != "2026-08-07"] == [0.0] * 4
    assert points[1]["launches"] == 1
    assert points[1]["seconds"] == 2 * 3600
    assert points[0]["start_iso"].startswith("2026-08-06T00:00:00")


def test_breakdown_sorts_by_spend(db):
    _seed_launch(db, "i-a10", gpu_type="gpu_1x_a10", status="terminated",
                 launched_at=_iso(), terminated_at=_iso(3600))
    _seed_launch(db, "i-h100", gpu_type="gpu_1x_h100", rate_cents=249,
                 status="terminated", launched_at=_iso(),
                 terminated_at=_iso(3600))

    by_type = spend.breakdown(db.list_launches(), now_iso=_iso(2 * 3600))
    assert [e["key"] for e in by_type] == ["gpu_1x_h100", "gpu_1x_a10"]
    assert by_type[0]["usd"] == pytest.approx(2.49)
    assert by_type[0]["count"] == 1
    assert by_type[0]["seconds"] == 3600

    by_region = spend.breakdown(db.list_launches(), now_iso=_iso(2 * 3600),
                                by="region")
    assert [e["key"] for e in by_region] == ["us-east-1"]
    assert by_region[0]["count"] == 2

    with pytest.raises(ValueError):
        spend.breakdown(db.list_launches(), now_iso=_iso(), by="nonsense")
