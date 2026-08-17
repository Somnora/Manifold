"""Spend accounting: what the launches Manifold started have actually cost.

Pure functions only — no I/O, no DB, no clock. Callers read the `launches`
rows out of SQLite and pass them in together with `now_iso`; this turns them
into money. Same shape (and same reason) as estimates.py and data_safety.py:
accounting math you can test without a cloud account or a single cent of
spend.

This module is the ONE implementation of the cost formula. Nothing else —
no route, no client, no dashboard helper — may re-derive it, because an
estimate that is wrong is advice while an accounting number that is wrong is
a lie.

Two things make that number honest rather than confident:

1. THE ANCHOR IS AN UPPER BOUND, DELIBERATELY. Lambda bills from the moment
   an instance passes health checks; `launches.launched_at` is stamped
   earlier, when Lambda ACCEPTED the launch. Boot is not free time on a big
   box (15-30+ minutes on multi-GPU SXM4), so the difference is real money.
   A spend-safety tool must over-report rather than under-report, so we
   anchor on acceptance, say so in DISCLAIMER, and expose `boot_seconds` per
   launch so the user can see exactly how much of the bill is boot.

2. UNKNOWNS STAY UNKNOWN. Every launch resolves to one of six COST_STATES.
   Two of them (`unresolved`, `rate_unknown`) have `usd = None` and are
   reported separately, never folded into a total as $0 — a fabricated zero
   is the one number a spend page must never show. `unresolved` carries a
   range instead, and both of its edges come from evidence rather than from
   the clock (see launch_cost).

Aggregates (summarize/series/breakdown) therefore add up only the rows with
a known point cost, and carry the unknowns alongside as counts and ranges.

The one honest exception to (1): an `unresolved` ceiling bounds what we can
EVIDENCE, not what the account was charged, because a launch we stopped
watching was never terminated by our giving up on it. launch_cost says
exactly where that applies and why the gap is narrow.

idle_spend() answers a second question — how much of a bill ran with the
GPUs unused — under the same discipline, and is REPORT ONLY: nothing it
returns may gate a termination or any other destructive decision. It is
called "idle spend" and never "wasted spend", because low utilization is not
a claim we can support about whether work was happening.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

# The six ways a launch's cost can be known. Exhaustive: launch_cost()
# always returns exactly one of these.
COST_STATES = (
    "never_started",   # no instance ever existed: a KNOWN $0
    "billed",          # started and we saw it stop: final
    "billing",         # started, still running: cost "so far"
    "orphaned",        # row says failed, but the instance is alive and burning
    "unresolved",      # started, stopped at a time we never observed: a RANGE
    "rate_unknown",    # duration known, price not: cost is unknown, NOT $0
)

# Lambda bills in one-minute increments, so 45m01s is billed as 46 minutes.
# Rounding up moves our number TOWARD the invoice, not away from it.
BILLING_INCREMENT_SECONDS = 60.0

# How far past a launch's LAST sighting its bill could have run unwatched.
#
# `last_seen_at` is written ONLY by the reconcile sweep in
# orchestrator.instances_with_state(), which runs when something asks for the
# instances view: a dashboard poll, or autopilot. The dispatcher's 30s adoption
# loop does NOT reconcile. So sightings stop the moment nobody is looking, and
# the true gap between the last sighting and the disappearance is unbounded —
# hours, overnight, a weekend.
#
# This is therefore the one HEURISTIC bound in this module, and an hour is
# chosen rather than a sweep interval because under-reporting is the failure
# this module exists to prevent. It is safe to be generous: the ceiling is a
# min() over every applicable bound, so a wide grace simply loses to a tighter
# evidenced bound (`resolved_at`, stamped the moment a sweep concludes) instead
# of inflating anything past it.
UNWATCHED_GRACE_SECONDS = 3600.0

# Which evidence set the ceiling of an `unresolved` range. Never the clock —
# see launch_cost. Reported as `bound_basis` so a UI can say WHY the range is
# the width it is instead of showing a bare pair of numbers. "active_at" shows
# up only in the rare case where a proven floor outranked every ceiling.
BOUND_BASES = ("resolved_at", "last_seen_at", "active_at", "boot_timeout")

# The one sentence every spend surface must carry. Deliberately names what is
# excluded: filesystems bill per GiB per month for as long as they exist, and
# they are not launches, so they are nowhere in this math.
DISCLAIMER = (
    "Computed as rate x wall clock from launch acceptance, rounded up to the "
    "minute. Lambda bills from health-check pass, so this is an upper bound. "
    "Filesystems bill separately and are not included. Reconcile against your "
    "Lambda invoice."
)

# How a breakdown groups rows. `instance_type` prefers what actually launched
# over what was asked for, since a fallback type is what the invoice charges.
_BREAKDOWN_KEYS = {
    "instance_type": lambda row: (row.get("launched_type")
                                  or row.get("requested_type") or "unknown"),
    "region": lambda row: row.get("region") or "unknown",
    "provider": lambda row: row.get("provider") or "lambda",
    "status": lambda row: row.get("status") or "unknown",
    # Phase 81: spend by principal. Pre-79 rows have no attribution and
    # group under "unattributed" - a true statement, never a guess.
    "created_by": lambda row: row.get("created_by") or "unattributed",
    # Phase 96: spend by stated purpose - the second axis of "what did this
    # project cost", reconstructable tonight only by subtracting other
    # projects' launches by hand. "no stated purpose" is a true group name,
    # never folded into anything else.
    "purpose": lambda row: row.get("purpose") or "no stated purpose",
}

_BUCKETS = ("day", "week", "month")


# -- small pure helpers -------------------------------------------------------

def _parse(ts: str | None) -> datetime | None:
    """One timestamp from a database row, or None.

    Tolerant by design (same pattern as db.task_durations): a malformed or
    missing timestamp means "unknown", never an exception. A spend page must
    survive one bad row rather than 500 the whole ledger. Naive timestamps
    are read as UTC — everything Manifold writes is UTC.
    """
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _now(now_iso: str) -> datetime:
    """The caller's clock. Unlike a row timestamp, a bad `now_iso` is a
    programming error in the caller, so it raises instead of degrading."""
    parsed = _parse(now_iso)
    if parsed is None:
        raise ValueError(f"now_iso must be an ISO 8601 timestamp, got {now_iso!r}")
    return parsed


def _span_seconds(start: datetime | None, end: datetime | None) -> float | None:
    """Seconds between two instants, never negative, None if either is
    missing (a clock skew must not produce a negative bill)."""
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def billable_seconds(seconds: float | None) -> float:
    """Wall-clock seconds rounded UP to Lambda's one-minute increment.
    Zero stays zero: a launch that never ran is not billed for a minute."""
    if not seconds or seconds <= 0:
        return 0.0
    return math.ceil(seconds / BILLING_INCREMENT_SECONDS) * BILLING_INCREMENT_SECONDS


def _usd(seconds: float, hourly_rate_cents: int | None) -> float | None:
    if hourly_rate_cents is None:
        return None
    return seconds / 3600.0 * hourly_rate_cents / 100.0


def timezone_label(tz_offset_minutes: int) -> str:
    """"UTC-08:00" for a PST caller. Echoed back in every summary so the user
    can see WHICH day boundary the numbers were bucketed on."""
    sign = "-" if tz_offset_minutes < 0 else "+"
    total = abs(int(tz_offset_minutes))
    return f"UTC{sign}{total // 60:02d}:{total % 60:02d}"


def _local_date(moment: datetime, tz_offset_minutes: int) -> date:
    """The calendar day `moment` fell on for a user at that UTC offset.

    Without this a PST user's 6pm launch lands in tomorrow's bucket every
    single evening. Bucketing lives here (Python), not in SQL: db.py has no
    date math at all, and an offset is a locale fact the caller owns.
    """
    return (moment + timedelta(minutes=tz_offset_minutes)).date()


def _bucket_start(day: date, bucket: str) -> date:
    if bucket == "week":
        return day - timedelta(days=day.weekday())   # ISO weeks start Monday
    if bucket == "month":
        return day.replace(day=1)
    return day


def _bucket_label(start: date, bucket: str) -> str:
    return f"{start.year:04d}-{start.month:02d}" if bucket == "month" \
        else start.isoformat()


def _bucket_start_iso(start: date, tz_offset_minutes: int) -> str:
    """The UTC instant at which that LOCAL bucket begins, so a chart can plot
    it without re-deriving the offset."""
    midnight = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    return (midnight - timedelta(minutes=tz_offset_minutes)).isoformat()


# -- one launch ---------------------------------------------------------------

def launch_cost(row: dict, *, now_iso: str, live_ids: set[str] | None = None,
                provider_listed: bool = True,
                boot_timeout_seconds: float = 2400.0) -> dict:
    """What one `launches` row has cost, and how certain we are.

    Returns {state, seconds, usd, usd_low, usd_high, boot_seconds,
    rate_known, capped, bound_basis}:

      state       one of COST_STATES
      seconds     BILLABLE seconds (rounded up to the minute), or None when
                  the duration itself is unknown (`unresolved`)
      usd         the point cost, or None when it cannot be known. A None
                  here is never to be rendered as $0.
      usd_low     bounds. Equal to `usd` whenever the cost is a point value,
      usd_high    a genuine range for `unresolved`, None for `rate_unknown`.
      boot_seconds  active_at - launched_at, or None for "unknown". Never 0:
                  a fabricated 0 would hide exactly the boot time this
                  module's upper-bound anchor is honest about.
      rate_known  did the row carry a price at all. False does NOT suppress
                  liveness: a running box with no price is still `billing` /
                  `orphaned`, with `usd = None`.
      capped      the CEILING came from `boot_timeout_seconds` because there
                  was no evidence at all (bound_basis == "boot_timeout").
                  Read it as "bounded by the last thing we could prove", not
                  as a proven maximum — see the note below.
      bound_basis which evidence set an `unresolved` ceiling, one of
                  BOUND_BASES, so a UI can explain the range instead of just
                  printing it. None for every other state.

    An `unresolved` range is rate x [min_duration, max_duration], and BOTH
    edges come from evidence:

      max_duration  the MINIMUM of every bound that applies (each is
                    independently an upper bound, so the tightest wins):
                      resolved_at - launched_at                    when set
                      last_seen_at - launched_at + UNWATCHED_GRACE when set
                      boot_timeout_seconds   only when NEITHER last_seen_at
                        nor active_at is set, i.e. it was never demonstrably
                        alive, so we have no evidence it outlived its boot
                        window
                    Never now - launched_at.
      min_duration  last_seen_at - launched_at, else active_at - launched_at,
                    else 0 — with no evidence it may have died immediately.

    Adding evidence about a row can only tighten its ceiling, and no bound
    ever grows just because time passed.

    IMPORTANT, and the one place this module does not strictly over-report:
    the ceiling bounds WHAT WE CAN EVIDENCE, not what the account was
    charged. A boot timeout does not terminate the instance, so a box that
    outlived our last evidence could have billed longer than `usd_high`.
    That gap is narrow by construction — anything still running is listed by
    its provider and classifies as `orphaned`, not `unresolved` — but
    `capped=True` must be read as "bounded by the last thing we could prove",
    never as "proven maximum".

    Evidence the caller supplies:
      live_ids        ids the cloud listed as running right now. None means
                      "no snapshot", and then the row's own status is
                      trusted rather than contradicted.
      provider_listed did THIS row's provider answer the sweep? False means
                      an absent id proves nothing, so a running row keeps
                      billing instead of being written off.
      boot_timeout_seconds  a boot that timed out cannot have outlived its
                      own timeout, which bounds the one case where we have
                      no other evidence at all.
    """
    now = _now(now_iso)
    rate_cents = row.get("hourly_rate_cents")
    launched = _parse(row.get("launched_at"))
    active = _parse(row.get("active_at"))
    terminated = _parse(row.get("terminated_at"))
    resolved = _parse(row.get("resolved_at"))
    last_seen = _parse(row.get("last_seen_at"))

    def result(state: str, *, seconds: float | None, usd: float | None,
               usd_low: float | None, usd_high: float | None,
               capped: bool = False, bound_basis: str | None = None) -> dict:
        return {
            "state": state,
            "seconds": seconds,
            "usd": usd,
            "usd_low": usd_low,
            "usd_high": usd_high,
            "boot_seconds": _span_seconds(launched, active),
            "rate_known": rate_cents is not None,
            "capped": capped,
            "bound_basis": bound_basis,
        }

    # No instance ever existed (rejected, or failed before Lambda accepted
    # it): the one state where $0 is a fact rather than a guess.
    if launched is None:
        return result("never_started", seconds=0.0, usd=0.0,
                      usd_low=0.0, usd_high=0.0)

    # We SAW it stop (terminate() or a reaped connection stamped the time).
    # This outranks the live listing deliberately: terminate() stamps at
    # ISSUE, and a provider can keep listing the box for a few seconds after,
    # so checking liveness first would re-open a closed cost and make a
    # settled number flicker.
    if terminated is not None:
        seconds = billable_seconds(_span_seconds(launched, terminated))
        if rate_cents is None:
            # Duration known, price not. Never $0.
            return result("rate_unknown", seconds=seconds, usd=None,
                          usd_low=None, usd_high=None)
        cost = _usd(seconds, rate_cents)
        return result("billed", seconds=seconds, usd=cost,
                      usd_low=cost, usd_high=cost)

    status = row.get("status") or ""
    instance_id = row.get("lambda_instance_id")
    alive = None if live_ids is None else instance_id in live_ids

    # The cloud is listing this instance right now, so it is billing right
    # now — whatever the row's status claims. A 'failed' row here is the
    # dangerous case: a real box burning money behind a row that reads as
    # over. Naming it `orphaned` is what lets the caller go fix it.
    #
    # LIVENESS OUTRANKS PRICE. `hourly_rate_cents` is nullable, and a missing
    # price used to classify the row as `rate_unknown` before we ever asked
    # whether it was alive — which zeroed `orphaned.count` and silenced the
    # "this is burning money now" alarm for exactly the row that most needed
    # it. Not knowing what it costs is no reason to stop saying it is running:
    # `_usd` returns None for a null rate, so the state stays honest about
    # liveness while the money stays honestly unknown.
    if alive is True:
        seconds = billable_seconds(_span_seconds(launched, now))
        cost = _usd(seconds, rate_cents)
        return result("orphaned" if status == "failed" else "billing",
                      seconds=seconds, usd=cost, usd_low=cost, usd_high=cost)

    # No evidence that it stopped: either we have no snapshot at all, or this
    # row's provider could not be listed. Trust the row and keep billing —
    # over-reporting a live box is the safe direction of error.
    if (alive is None or not provider_listed) and status != "failed":
        seconds = billable_seconds(_span_seconds(launched, now))
        cost = _usd(seconds, rate_cents)
        return result("billing", seconds=seconds, usd=cost,
                      usd_low=cost, usd_high=cost)

    # Not live, and no stop was observed. Price now decides whether a range is
    # even computable: without a rate there is no money axis at all, and the
    # duration is unknown too, so nothing here may be reported as a number.
    if rate_cents is None:
        return result("rate_unknown", seconds=None, usd=None,
                      usd_low=None, usd_high=None)

    # Everything else stopped at a time nobody observed. Bound it, never
    # guess it: a point estimate here would be a fabrication, and stamping
    # one into terminated_at is how a boot-timeout row grows into a $31,000
    # "final" number.
    #
    # Both edges come from EVIDENCE, never from the clock. `now` is provably
    # wrong as a ceiling here: this branch is only reachable when the row's
    # own provider listed successfully and did NOT list this instance, so we
    # have positively confirmed it is not running (a live one is `orphaned`,
    # not `unresolved`). "It might have billed right up to this moment" is
    # therefore something we have already disproved — and left in, it turns a
    # six-week-old boot timeout into a $37,000 "upper bound" that grows every
    # day nobody looks at it. That is the same lie wearing a range.
    # The ceiling is the MINIMUM of every applicable bound, not the first one
    # that matches. Each is independently an upper bound, so the tightest one
    # wins — and a first-match chain silently discards the others. Evidence of
    # ABSENCE at time T does not imply PRESENCE until T: a boot timeout that
    # no sweep noticed for five days is bounded by its boot window, not by the
    # clock of the sweep that eventually found it gone. Ranking resolved_at
    # first priced exactly that row, a box that never passed a health check,
    # at $219.
    ceilings: list[tuple[float, str]] = []
    if resolved is not None:
        # Gone BY then. Says nothing about how much of the span it billed.
        ceilings.append(
            (_span_seconds(launched, resolved) or 0.0, "resolved_at"))
    if last_seen is not None:
        # Alive THEN, plus however long it could have run unwatched after.
        ceilings.append(((_span_seconds(launched, last_seen) or 0.0)
                         + UNWATCHED_GRACE_SECONDS, "last_seen_at"))
    if last_seen is None and active is None:
        # Never demonstrably alive, so we have no evidence it outlived its own
        # boot window. NOTE what this is and is not: a boot timeout does not
        # terminate anything (the orchestrator's own error text says the box
        # "may still be running and billing"), so this caps what we can
        # EVIDENCE, not what the account was charged. It is safe only because
        # anything genuinely still running is `orphaned` by construction —
        # listed by its provider, and never in this branch. The residual
        # unknown is "how long did it run after everyone stopped watching",
        # and `bound_basis="boot_timeout"` is the flag that says so.
        # This term must NOT apply to a launch that DID come up (active_at
        # set) and failed later — that one may have run for days.
        ceilings.append((boot_timeout_seconds, "boot_timeout"))
    if not ceilings:
        # It came up and then nothing was ever recorded: no evidence left to
        # bound it with, so fall back to the boot window rather than the clock.
        ceilings.append((boot_timeout_seconds, "boot_timeout"))
    max_seconds, bound_basis = min(ceilings, key=lambda bound: bound[0])

    # The floor is only what we can PROVE it billed, and it uses the same
    # completeness rule: a sighting beats a boot, a boot beats nothing. With
    # no evidence at all, zero is honest — it may have died immediately.
    if last_seen is not None:
        min_seconds, floor_basis = (_span_seconds(launched, last_seen) or 0.0,
                                    "last_seen_at")
    elif active is not None:
        min_seconds, floor_basis = (_span_seconds(launched, active) or 0.0,
                                    "active_at")
    else:
        min_seconds, floor_basis = 0.0, "active_at"
    if min_seconds > max_seconds:
        # A PROVEN floor outranks a weaker ceiling: it demonstrably billed
        # that long, so the ceiling rises to meet it rather than the floor
        # being quietly lowered to preserve the ordering.
        max_seconds, bound_basis = min_seconds, floor_basis

    return result("unresolved", seconds=None, usd=None,
                  usd_low=_usd(billable_seconds(min_seconds), rate_cents),
                  usd_high=_usd(billable_seconds(max_seconds), rate_cents),
                  capped=(bound_basis == "boot_timeout"),
                  bound_basis=bound_basis)


# -- idle spend (report only) -------------------------------------------------

# Where an idle-spend window can END. Never `active_at`, and never a bare
# clock for a launch we know stopped.
IDLE_WINDOW_BASES = ("terminated_at", "resolved_at", "now")

# The sentence every idle-spend surface must carry. It says the two things a
# reader would otherwise assume wrongly: that idle is not the same as wasted,
# and that unmeasured time is not idle time.
IDLE_SPEND_DISCLAIMER = (
    "Idle spend is the share of this instance's bill during which its GPUs "
    "reported near-zero average utilization. It is not proof that nothing "
    "useful was happening: a memory-bound job and a served model between "
    "requests both look idle. Time we could not sample is reported as "
    "unmeasured, never as idle."
)


def idle_window(row: dict, *, now_iso: str) -> dict | None:
    """The span an idle-spend judgement may cover, or None if there is none.

    `[launched_at, COALESCE(terminated_at, resolved_at, now)]`, and each
    endpoint is chosen the same way the cost model chooses one:

      START is `launched_at` ONLY. Not `active_at`, which looks like the
      better anchor (billing starts nearer to it) but is NULL on every row
      the orphan repair touched — Phase 76a leaves it NULL deliberately
      rather than fabricating a boot it never observed. Anchoring on it
      would therefore drop exactly the abandoned instances this accounting
      exists to find. Boot time lands in the window as UNMEASURED time,
      which is what it is: no sidecar is answering yet.
      END prefers an observed stop, then the sweep that concluded the
      instance was gone, and only then the clock — a running instance is the
      one case where "now" is the honest end.

    Returned as the row's own timestamp STRINGS so a caller can hand them
    straight to a windowed query without a format round trip.
    """
    launched = _parse(row.get("launched_at"))
    if launched is None or not row.get("launched_at"):
        return None
    for basis in ("terminated_at", "resolved_at"):
        end_iso = row.get(basis)
        if end_iso and _parse(end_iso) is not None:
            break
    else:
        basis, end_iso = "now", now_iso
    return {
        "start_iso": row["launched_at"],
        "end_iso": end_iso,
        "end_basis": basis,
        "seconds": _span_seconds(launched, _parse(end_iso)) or 0.0,
    }


def _idle_result(*, available: bool, reason: str, threshold_pct: float,
                 window: dict | None = None, measured: bool = False,
                 idle_seconds: float | None = None,
                 busy_seconds: float | None = None,
                 unknown_seconds: float | None = None,
                 rate_cents: int | None = None,
                 sample_count: int = 0) -> dict:
    """One idle-spend answer, with every key always present.

    A None seconds/usd field means "not measured" and must never be rendered
    as 0 or $0.00 — the same discipline `launch_cost` applies to `usd`.
    """
    window_seconds = None if window is None else window["seconds"]

    def usd(seconds: float | None) -> float | None:
        if seconds is None or rate_cents is None:
            return None
        return round(seconds / 3600.0 * rate_cents / 100.0, 2)
    measured_seconds = None if not measured else (idle_seconds or 0.0) + \
        (busy_seconds or 0.0)
    return {
        "available": available,
        # False means "we have no utilization evidence for this window at
        # all". The window and its cost are still reported, as unmeasured.
        "measured": measured,
        "reason": reason,
        "threshold_pct": threshold_pct,
        "window_seconds": None if window_seconds is None
        else round(window_seconds),
        "window_end_basis": None if window is None else window["end_basis"],
        "idle_seconds": None if idle_seconds is None else round(idle_seconds),
        "busy_seconds": None if busy_seconds is None else round(busy_seconds),
        "unknown_seconds": None if unknown_seconds is None
        else round(unknown_seconds),
        # What fraction of the window we actually have utilization evidence
        # for. Read `idle_usd` next to this: "$0.00 idle" over 2% coverage
        # says almost nothing, and a UI that hides coverage would present it
        # as if it did.
        "coverage": None if not measured or not window_seconds
        else round(measured_seconds / window_seconds, 4),
        "idle_usd": usd(idle_seconds),
        "unknown_usd": usd(unknown_seconds),
        "rate_known": rate_cents is not None,
        "sample_count": sample_count,
        "disclaimer": IDLE_SPEND_DISCLAIMER,
    }


def idle_spend(row: dict, samples: list[dict], *, now_iso: str,
               util_pct: float = 5.0,
               sample_interval_seconds: float = 30.0,
               min_window_seconds: float = 600.0) -> dict:
    """How much of one launch's bill ran with its GPUs essentially unused.

    Pure, like everything else here: `samples` are the telemetry rows the
    caller already read (`db.telemetry_samples_between` over `idle_window`),
    and `now_iso` is the caller's clock.

    REPORT ONLY. Nothing this returns may gate a termination or any other
    destructive decision. Low utilization is evidence about money, not proof
    that no work is happening, and idle auto-termination stays keyed on jobs
    and terminal activity precisely so a quiet-looking GPU can never be
    destroyed on the strength of a number from here.

    THREE RULES DECIDE EVERY NUMBER BELOW.

    1. IDLE READS `util_pct_mean`, NEVER `util_pct`. The stored `util_pct` is
       the MAX across the box's GPUs, and the right-size hint wants that (a
       max tightens the hint, which is the safe direction for an OOM). Idle
       accounting wants the opposite: with the max, one busy GPU out of eight
       hides seven idle ones and idle spend is systematically UNDER-reported,
       which is the single direction a spend-safety tool must never err in.
       The two never cross.

    2. A SAMPLING GAP IS UNKNOWN, NOT IDLE. Each sample speaks for at most
       `sample_interval_seconds` from its own timestamp (and never past the
       next sample, so spans cannot overlap when sampling runs early).
       Everything else in the window — before the first sample, after the
       last, and every gap in between — is `unknown_seconds`. An instance
       that went unreachable therefore accrues no idle time at all, because
       the alternative is a tool that penalises a box for being
       unmonitorable and then reports the penalty as money.

    3. NO EVIDENCE IS NOT ZERO IDLE. Zero rows is the NORMAL case for an
       adopted instance whose sidecar never came up and whose ssh fallback
       failed, and so is a window of samples that all predate `util_pct_mean`
       (an old sidecar frozen into a running box reports no such field).
       Both return `idle_seconds=None` with `unknown_seconds` covering the
       whole window, so a UI can say "not measured" and can never say
       "$0.00 idle" about an instance nothing was ever known about.

    `min_window_seconds` declines the judgement entirely for a short-lived
    instance: it is mostly boot, and an idle fraction of it means nothing.

    Costs are a SHARE of the bill at the row's hourly rate, deliberately not
    passed through `billable_seconds` — the minute round-up belongs to a
    whole launch's invoice line, and applying it to each fragment of one
    would inflate a share of a bill above the bill.
    """
    rate_cents = row.get("hourly_rate_cents")
    window = idle_window(row, now_iso=now_iso)
    if window is None:
        return _idle_result(
            available=False, threshold_pct=util_pct, rate_cents=rate_cents,
            reason="this launch never reached a running instance")
    if window["seconds"] < min_window_seconds:
        return _idle_result(
            available=False, threshold_pct=util_pct, window=window,
            rate_cents=rate_cents,
            reason=(f"ran for under {round(min_window_seconds / 60)} minutes, "
                    "which is mostly boot; too short to judge"))

    start, end = _parse(window["start_iso"]), _parse(window["end_iso"])
    points: list[tuple[datetime, float | None]] = []
    for sample in samples:
        at = _parse(sample.get("at"))
        if at is None or at < start or at > end:
            continue
        raw = sample.get("util_pct_mean")
        try:
            # None stays None: "this sample did not report utilization" is
            # not "the GPUs were doing nothing". A float() of it would be
            # the exact old-sidecar bug this whole column exists to avoid.
            points.append((at, None if raw is None else float(raw)))
        except (TypeError, ValueError):
            points.append((at, None))
    points.sort(key=lambda point: point[0])

    idle = busy = 0.0
    for index, (at, mean_util) in enumerate(points):
        horizon = points[index + 1][0] if index + 1 < len(points) else end
        span = min(sample_interval_seconds,
                   max(0.0, (horizon - at).total_seconds()))
        if mean_util is None:
            continue                      # unreported: leave the span unknown
        if mean_util <= util_pct:
            idle += span
        else:
            busy += span

    if idle + busy <= 0:
        return _idle_result(
            available=True, measured=False, threshold_pct=util_pct,
            window=window, rate_cents=rate_cents, sample_count=len(points),
            unknown_seconds=window["seconds"],
            reason=("no GPU utilization was recorded for this instance, so "
                    "its idle spend is not measured"))

    return _idle_result(
        available=True, measured=True, threshold_pct=util_pct, window=window,
        rate_cents=rate_cents, sample_count=len(points),
        idle_seconds=idle, busy_seconds=busy,
        unknown_seconds=max(0.0, window["seconds"] - idle - busy),
        reason=(f"at or below {util_pct:g}% mean GPU utilization, measured "
                f"across {len(points)} samples"))


def _cost_rows(rows: list[dict], *, now_iso: str,
               live_ids: set[str] | None = None,
               listed_providers: set[str] | None = None,
               boot_timeout_seconds: float = 2400.0) -> list[tuple[dict, dict]]:
    """Each row paired with its cost. `listed_providers` names the providers
    whose listing SUCCEEDED this sweep; None means "assume they all did"."""
    out = []
    for row in rows:
        provider = row.get("provider") or "lambda"
        listed = (True if listed_providers is None
                  else provider in listed_providers)
        out.append((row, launch_cost(
            row, now_iso=now_iso, live_ids=live_ids, provider_listed=listed,
            boot_timeout_seconds=boot_timeout_seconds)))
    return out


# -- aggregates ---------------------------------------------------------------

def summarize(rows: list[dict], *, now_iso: str, tz_offset_minutes: int = 0,
              live_ids: set[str] | None = None,
              listed_providers: set[str] | None = None,
              boot_timeout_seconds: float = 2400.0,
              monthly_budget_usd: float = 0.0) -> dict:
    """Headline spend: today, this week, month to date, all time.

    Windows are LOCAL to `tz_offset_minutes`, and a launch is attributed to
    the day it STARTED (a run across midnight lands wholly in its start day;
    splitting it would make every historical number depend on when you asked).
    "This week" is the trailing 7 local days including today.

    Only rows with a known point cost are added up. `unresolved` and
    `rate_unknown` are reported beside the totals, with their ids, so they
    can be chased rather than silently counted as free.

    `rate_unknown_count` counts every launch whose missing price cost us a
    number, whatever state it is in — including a live box with no rate,
    which reports as `billing`/`orphaned` because liveness outranks price.
    `live_burn_usd_per_hour` can only sum the rows that HAVE a price, so
    read it next to that count rather than as a complete burn rate.

    `lower_bound` is always True and says so out loud: Manifold can only
    account for launches IT started, so an instance adopted from the Lambda
    console has no launch row and no cost here.
    """
    now = _now(now_iso)
    today = _local_date(now, tz_offset_minutes)
    week_start = today - timedelta(days=6)
    month_start = today.replace(day=1)

    totals = {"today": 0.0, "week": 0.0, "month": 0.0, "all_time": 0.0}
    burn_per_hour = 0.0
    unresolved_ids: list[str] = []
    unresolved_low = unresolved_high = 0.0
    orphaned_ids: list[str] = []
    rate_unknown_count = 0

    for row, cost in _cost_rows(rows, now_iso=now_iso, live_ids=live_ids,
                                listed_providers=listed_providers,
                                boot_timeout_seconds=boot_timeout_seconds):
        state = cost["state"]
        if state == "unresolved":
            unresolved_ids.append(row.get("id"))
            unresolved_low += cost["usd_low"] or 0.0
            unresolved_high += cost["usd_high"] or 0.0
        if not cost["rate_known"] and cost["usd"] is None:
            # Counted by EVIDENCE, not by state: since liveness outranks
            # price, a running box with no price reports as `billing` /
            # `orphaned` and would otherwise drop out of this count entirely.
            # `never_started` is excluded because its $0 is known regardless
            # of price, so a missing rate costs us nothing there.
            rate_unknown_count += 1
        if state in ("billing", "orphaned"):
            burn_per_hour += (row.get("hourly_rate_cents") or 0) / 100.0
        if state == "orphaned":
            orphaned_ids.append(row.get("id"))

        usd = cost["usd"]
        if usd is None:
            continue
        totals["all_time"] += usd
        started = _parse(row.get("launched_at"))
        if started is None:
            continue                       # never_started: $0 in every window
        day = _local_date(started, tz_offset_minutes)
        if day == today:
            totals["today"] += usd
        if day >= week_start:
            totals["week"] += usd
        if day >= month_start:
            totals["month"] += usd

    return {
        "today_usd": round(totals["today"], 2),
        "week_usd": round(totals["week"], 2),
        "month_to_date_usd": round(totals["month"], 2),
        "all_time_usd": round(totals["all_time"], 2),
        "live_burn_usd_per_hour": round(burn_per_hour, 2),
        "unresolved": {
            "count": len(unresolved_ids),
            "usd_low": round(unresolved_low, 2),
            "usd_high": round(unresolved_high, 2),
            "launch_ids": unresolved_ids,
        },
        "orphaned": {"count": len(orphaned_ids), "launch_ids": orphaned_ids},
        "rate_unknown_count": rate_unknown_count,
        "lower_bound": True,
        "timezone_offset_minutes": tz_offset_minutes,
        "timezone_label": timezone_label(tz_offset_minutes),
        "disclaimer": DISCLAIMER,
        "budget": budget_status(
            month_to_date_usd=round(totals["month"], 2),
            burn_usd_per_hour=round(burn_per_hour, 2),
            monthly_budget_usd=monthly_budget_usd,
            now_iso=now_iso, tz_offset_minutes=tz_offset_minutes,
        ),
    }


# Thresholds a crossing is reported at, as a share of the monthly budget.
BUDGET_THRESHOLDS = (0.5, 0.8, 1.0)


def budget_status(*, month_to_date_usd: float, burn_usd_per_hour: float,
                  monthly_budget_usd: float, now_iso: str,
                  tz_offset_minutes: int = 0) -> dict:
    """Burn-down against the monthly wallet, or an honest "unset".

    ADVISORY. Nothing here refuses a launch, and the reason is in
    GuardrailPrefs: month-to-date is reconstructed from the launches Manifold
    started, so it is a lower bound, and refusing work on a number we know is
    short would cost the user a launch without protecting the wallet.

    `projected_month_end_usd` and `exhausted_on` both read "at the CURRENT
    burn rate", which is the question a user actually asks ("if I leave this
    running, when do I hit the cap?"). They are not a forecast of what you
    will choose to launch next, and with nothing running the projection is
    just month-to-date - correctly, because at a burn of zero nothing more
    is spent.
    """
    if monthly_budget_usd <= 0:
        return {"state": "unset", "monthly_budget_usd": 0.0,
                "month_to_date_usd": round(month_to_date_usd, 2),
                "remaining_usd": None, "used_pct": None,
                "projected_month_end_usd": None, "exhausted_on": None,
                "hours_left_in_month": None}

    now = _now(now_iso)
    local_today = _local_date(now, tz_offset_minutes)
    # First local midnight of next month, back in UTC terms: the wallet
    # resets there, so nothing is projected past it.
    if local_today.month == 12:
        next_month = local_today.replace(year=local_today.year + 1, month=1, day=1)
    else:
        next_month = local_today.replace(month=local_today.month + 1, day=1)
    offset = timedelta(minutes=tz_offset_minutes)
    month_end_utc = datetime.combine(
        next_month, datetime.min.time(), tzinfo=timezone.utc) - offset
    hours_left = max(0.0, (month_end_utc - now).total_seconds() / 3600.0)

    remaining = monthly_budget_usd - month_to_date_usd
    projected = month_to_date_usd + burn_usd_per_hour * hours_left

    exhausted_on = None
    if burn_usd_per_hour > 0 and remaining > 0:
        hours_to_go = remaining / burn_usd_per_hour
        if hours_to_go <= hours_left:      # only if it happens THIS month
            exhausted_on = (now + timedelta(hours=hours_to_go)).isoformat(
                timespec="seconds")

    used_pct = month_to_date_usd / monthly_budget_usd * 100.0
    state = "over" if remaining <= 0 else ("warn" if used_pct >= 80.0 else "ok")

    return {
        "state": state,
        "monthly_budget_usd": round(monthly_budget_usd, 2),
        "month_to_date_usd": round(month_to_date_usd, 2),
        "remaining_usd": round(remaining, 2),
        "used_pct": round(used_pct, 1),
        "projected_month_end_usd": round(projected, 2),
        "exhausted_on": exhausted_on,
        "hours_left_in_month": round(hours_left, 1),
    }


def series(rows: list[dict], *, now_iso: str, bucket: str = "day",
           days: int = 30, tz_offset_minutes: int = 0, **kw) -> list[dict]:
    """Spend over time, oldest first, gap-filled with zeros.

    The window is the trailing `days` LOCAL days ending today; `bucket` rolls
    those days up ("day" | "week" | "month"). Empty buckets are returned as
    zeros so a chart draws a flat line instead of joining two distant points.

    `launches` counts every launch that started in the bucket; `usd` and
    `seconds` only sum the ones whose cost is actually known, so a bucket can
    honestly read "2 launches, $0.00" when both are unresolved. Extra keyword
    arguments (live_ids, listed_providers, boot_timeout_seconds) go straight
    to launch_cost.
    """
    if bucket not in _BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r}; use one of {_BUCKETS}")
    now = _now(now_iso)
    today = _local_date(now, tz_offset_minutes)
    first_day = today - timedelta(days=max(1, days) - 1)

    buckets: dict[date, dict] = {}
    day = first_day
    while day <= today:
        buckets.setdefault(_bucket_start(day, bucket),
                           {"usd": 0.0, "seconds": 0.0, "launches": 0})
        day += timedelta(days=1)

    for row, cost in _cost_rows(rows, now_iso=now_iso, **kw):
        started = _parse(row.get("launched_at"))
        if started is None:
            continue          # nothing ever ran: it has no place on a time axis
        started_day = _local_date(started, tz_offset_minutes)
        if not first_day <= started_day <= today:
            continue
        entry = buckets[_bucket_start(started_day, bucket)]
        entry["launches"] += 1
        if cost["usd"] is not None:
            entry["usd"] += cost["usd"]
        if cost["seconds"] is not None:
            entry["seconds"] += cost["seconds"]

    return [
        {
            "bucket": _bucket_label(start, bucket),
            "start_iso": _bucket_start_iso(start, tz_offset_minutes),
            "usd": round(entry["usd"], 2),
            "seconds": round(entry["seconds"]),
            "launches": entry["launches"],
        }
        for start, entry in sorted(buckets.items())
    ]


def breakdown(rows: list[dict], *, now_iso: str, by: str = "instance_type",
              days: int | None = None, tz_offset_minutes: int = 0,
              **kw) -> list[dict]:
    """Where the money went, biggest first.

    `by` is instance_type | region | provider | status. `days` limits it to
    the trailing N local days (None = all time). Same counting rule as
    series(): every launch is counted, only known costs are summed.
    """
    key_of = _BREAKDOWN_KEYS.get(by)
    if key_of is None:
        raise ValueError(
            f"unknown breakdown {by!r}; use one of "
            f"{tuple(sorted(_BREAKDOWN_KEYS))}")
    now = _now(now_iso)
    today = _local_date(now, tz_offset_minutes)
    first_day = None if days is None else today - timedelta(days=max(1, days) - 1)

    groups: dict[str, dict] = {}
    for row, cost in _cost_rows(rows, now_iso=now_iso, **kw):
        started = _parse(row.get("launched_at"))
        if started is None:
            continue
        if first_day is not None:
            started_day = _local_date(started, tz_offset_minutes)
            if not first_day <= started_day <= today:
                continue
        entry = groups.setdefault(key_of(row),
                                  {"usd": 0.0, "seconds": 0.0, "count": 0})
        entry["count"] += 1
        if cost["usd"] is not None:
            entry["usd"] += cost["usd"]
        if cost["seconds"] is not None:
            entry["seconds"] += cost["seconds"]

    out = [
        {"key": key, "usd": round(entry["usd"], 2),
         "seconds": round(entry["seconds"]), "count": entry["count"]}
        for key, entry in groups.items()
    ]
    # Biggest spend first; alphabetical inside a tie so the order is stable.
    out.sort(key=lambda e: (-e["usd"], e["key"]))
    return out
