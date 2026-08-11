"""Fabricated launch history for mock mode, and ONLY for mock mode.

Mock mode (MANIFOLD_MOCK=1) has a working catalog, working launches and a
working dashboard, but it has no PAST: `db.create_launch` has exactly one
caller, the live launch path, so a freshly started demo backend shows a
spend page reading $0.00 over an empty ledger. Nothing to look at, nothing
to screenshot, nothing to check the cost arithmetic against.

This module fills that gap with fixture rows covering every cost state the
spend feature models. It writes invented dollar amounts into a ledger whose
entire value is being trustworthy, and there is no undo for that, so it is
gated TWICE and refuses unless BOTH gates agree:

  1. the caller passes `mock=True` explicitly, and
  2. the database THIS CONNECTION IS ATTACHED TO is named `<stem>-mock.db`.

One convention would be enough right up until the day it isn't. Two checks
you can read beat one you have to trust: gate 1 catches a caller who wired
the seeder into the wrong startup path, gate 2 catches a caller who wired
it into the right path but against the real database file.

Gate 2 asks SQLite (`PRAGMA database_list`) which file the connection it is
about to write through actually opened. It deliberately does not trust the
caller's `db_path` argument: a string that says `manifold-mock.db` next to a
connection open on `manifold.db` is precisely the wiring mistake this gate
exists to catch, and validating the string would wave it through. The
argument is still required, and must AGREE with the live file — a caller
that does not know where it is writing has no business writing here.

Everything it writes is identifiable by inspection:

  - launch ids are `seed-0001`, `seed-0002`, ... never a uuid, so `grep
    seed-` finds every fabricated row in the database, in a screenshot, or
    in a test assertion;
  - each row gets one `mock_seed` audit entry naming what was fabricated,
    so the Audit tab is a complete account of the invented spend.

Seeding is deterministic (fixed RNG seed) and idempotent (it no-ops if any
`seed-` row already exists). All spans are anchored to a `now_iso` passed
in by the caller, so the fixture is always "the last N days" and a chart
never grows a dead right edge as the fixture ages.

Two calls, not one: seed_mock_history() writes the ledger, and
register_live_instances() puts the instance behind its one still-running
launch into the mock cloud's listing. A demo that claims a box is running
must have a cloud that lists it, or the fixture is lying about the exact
number (live burn) it exists to show.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random

from .lambda_api import DEFAULT_MOCK_TYPES, InstanceInfo

# Every seeded launch id starts with this. Fixture data has to be findable
# by grep, not just by knowing which rows you put there.
SEED_ID_PREFIX = "seed-"

# Gate 2: the physically separate mock database (main.py derives this name
# from the real db path when MANIFOLD_MOCK=1).
MOCK_DB_SUFFIX = "-mock.db"

# The window the fixture can cover. One day is the smallest thing worth
# charting; a year matches the spend page's own history bound. Exported so
# the caller that reads MANIFOLD_MOCK_SEED_DAYS can clamp to the same range
# instead of keeping a second copy of it.
MIN_SEED_DAYS = 1
MAX_SEED_DAYS = 365

AUDIT_ACTOR = "backend"
AUDIT_ACTION = "mock_seed"

# The six-state cost model the spend feature reasons about; the fixture
# guarantees at least one launch in each of these five (the sixth, a launch
# still mid-flight, is what mock mode already produces on its own).
SEED_STATES = ("billed", "billing", "never_started", "unresolved",
               "rate_unknown")

# Fixed, so the same (days, now_iso) always draws the same picture.
_RNG_SEED = 76

# Namespace for the fake cloud instance ids, so those are deterministic too.
_INSTANCE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "manifold:mock-seed")

# Prices and regions mirror the mock catalog in lambda_api.DEFAULT_MOCK_TYPES,
# so the fixture's arithmetic matches what a mock launch would really cost.
# Repeats are the weighting: cheap A10s are the common case, an 8x box is the
# occasional expensive day, which is what gives a per-day chart its shape.
_FILLER_MIX = (
    ("gpu_1x_a10", 129, "us-east-1"),
    ("gpu_1x_a10", 129, "us-west-2"),
    ("gpu_1x_a10", 129, "us-east-1"),
    ("gpu_1x_a100_sxm4", 199, "us-east-1"),
    ("gpu_1x_a100_sxm4", 199, "us-west-3"),
    ("gpu_1x_h100_sxm5", 429, "us-east-2"),
    ("gpu_1x_h100_sxm5", 429, "us-south-1"),
    ("gpu_8x_a100_80gb_sxm4", 2232, "us-midwest-1"),
)

# How many ordinary runs happen on a given day. The zero is deliberate: a
# chart with quiet days reads as real history, a flat line reads as a stub.
_RUNS_PER_DAY = (0, 1, 1, 2)

# The mock catalog ships exactly one filesystem, in us-east-1. Attaching it
# only to launches in that region keeps the fixture consistent with the
# region-match guard.
_MOCK_FILESYSTEM = "manifold-data"
_MOCK_FILESYSTEM_REGION = "us-east-1"

# The long fine-tune that dominates a month's bill.
_LONG_RUN_HOURS = 52.0

# The address the fixture's running instance answers on. TEST-NET-1
# (RFC 5737), like the rest of mock mode, and a different host from the one
# a mock LAUNCH hands out so the two never share a host-key pin.
_MOCK_INSTANCE_IP = "192.0.2.20"


class SeedRefused(RuntimeError):
    """A safety gate said no. Nothing was written."""


@dataclass(frozen=True)
class _Row:
    """One fixture launch, decided in full before anything is written."""
    state: str                      # one of SEED_STATES
    requested_type: str
    launched_type: str | None       # None when it never launched
    region: str
    rate_cents: int                 # the catalog price for requested_type
    rate_known: bool                # False -> hourly_rate_cents ends up NULL
    status: str                     # active | failed | terminated
    attempts: int
    created_at: datetime
    launched_at: datetime | None    # None -> billing never started
    active_at: datetime | None
    terminated_at: datetime | None
    error: str | None
    with_instance: bool             # did a cloud instance id ever exist


# -- writing --------------------------------------------------------------


def connected_db_path(db) -> str:
    """The database file `db` is ACTUALLY open on, according to SQLite.

    `PRAGMA database_list` returns one row per attached schema; 'main' is the
    file the connection was opened with, resolved by SQLite itself. That is
    the only answer that cannot be stale or mistaken, because it comes from
    the same connection the writes will go through.

    Returns '' for a connection with no file behind it (`:memory:`), which
    gate 2 then refuses like any other non-mock database.
    """
    return db.open_path()


def seed_mock_history(db, *, days: int, now_iso: str, mock: bool,
                      db_path: str) -> int:
    """Fabricate `days` of demo launch history. Returns the number of rows
    created (0 if history was already seeded).

    Refuses unless mock is True AND the database `db` is actually attached to
    is named '<stem>-mock.db' (and `db_path` names that same file).
    """
    # Gate 1. `is not True` rather than `not mock`: a safety gate should not
    # be satisfiable by a truthy accident such as the string "0".
    if mock is not True:
        raise SeedRefused(
            f"mock seed: refusing to fabricate launch history without an "
            f"explicit mock=True (got {mock!r}); this writes invented "
            f"dollar amounts into the spend ledger."
        )
    # Gate 2. Independent of gate 1 on purpose: the two fail for different
    # reasons, so one of them being wrong is not enough to lose real history.
    # Asked of the connection, not of the argument — see the module docstring.
    live_path = connected_db_path(db)
    if not Path(live_path).name.endswith(MOCK_DB_SUFFIX):
        raise SeedRefused(
            f"mock seed: refusing to fabricate launch history in "
            f"{live_path!r} (the database this connection is open on): only "
            f"a database named '<stem>{MOCK_DB_SUFFIX}' may hold fixture "
            f"spend."
        )
    if Path(db_path).resolve() != Path(live_path).resolve():
        raise SeedRefused(
            f"mock seed: refusing to fabricate launch history: the caller "
            f"passed db_path={db_path!r} but the connection is open on "
            f"{live_path!r}. A caller that does not know which database it "
            f"is writing to must not write invented spend into one."
        )
    if not MIN_SEED_DAYS <= days <= MAX_SEED_DAYS:
        raise ValueError(f"mock seed: days must be {MIN_SEED_DAYS}.."
                         f"{MAX_SEED_DAYS}, got {days!r}")

    now = _parse(now_iso)
    if has_seeded_history(db):
        # Idempotency, by no-op rather than clear-and-reseed: there is no
        # delete-launch API, and inventing one so a demo can re-roll its
        # numbers is not a trade worth making.
        return 0

    rows = sorted(_plan(days, now, Random(_RNG_SEED)),
                  key=lambda r: r.created_at)
    for number, row in enumerate(rows, start=1):
        _write(db, f"{SEED_ID_PREFIX}{number:04d}", row)
    return len(rows)


def has_seeded_history(db) -> bool:
    """Is there already fixture history in this database?"""
    return any(launch["id"].startswith(SEED_ID_PREFIX)
               for launch in db.list_launches())


def _write(db, launch_id: str, row: _Row) -> None:
    """Persist one fixture launch through db.py's own writers.

    The id and the creation time are passed to create_launch() as ordinary
    parameters, so db.py stays the only writer of the launches table and
    this module stays a plain caller of it.
    """
    db.create_launch(
        requested_type=row.requested_type,
        region=row.region,
        filesystem=_filesystem_for(row.region),
        connection_mode="direct-ssh",
        hourly_rate_cents=row.rate_cents,
        launch_id=launch_id,
        created_at=_iso(row.created_at),
    )

    fields: dict = {"status": row.status, "attempts": row.attempts}
    if row.launched_type is not None:
        fields["launched_type"] = row.launched_type
    if row.launched_at is not None:
        fields["launched_at"] = _iso(row.launched_at)
    if row.active_at is not None:
        fields["active_at"] = _iso(row.active_at)
    if row.terminated_at is not None:
        fields["terminated_at"] = _iso(row.terminated_at)
    if row.error is not None:
        fields["error"] = row.error
    if row.with_instance:
        fields["lambda_instance_id"] = _instance_id(launch_id)
    if not row.rate_known:
        # The rate_unknown state: a run whose price was never recorded, so
        # the spend page has to say "unknown" instead of guessing.
        fields["hourly_rate_cents"] = None
    db.update_launch(launch_id, **fields)

    db.record_audit(AUDIT_ACTOR, AUDIT_ACTION, _audit_detail(launch_id, row))


def register_live_instances(db, client) -> list[str]:
    """Tell a MockLambdaClient about the fixture launches that claim to be
    running, so the mock cloud really lists what the ledger says is billing.
    Returns the instance ids registered.

    Both halves of the fixture have to agree. A launch row only reaches
    'active' AFTER its instance id is stored, so a running row whose
    instance the cloud does not list is a shape real data cannot produce —
    and the reconcile sweep would rightly close it seconds into the demo,
    leaving the spend page reporting no live burn at all. Registering the
    instance here fixes that at the source instead of teaching the cost
    model or the sweep to make an exception for fixture rows.

    A consequence, and the honest one: that instance is running as far as
    every guard is concerned, so it counts toward the concurrency limit and
    the hourly budget exactly as a real box would.
    """
    registered: list[str] = []
    for launch in db.list_launches():
        instance_id = launch["lambda_instance_id"]
        if not launch["id"].startswith(SEED_ID_PREFIX) or not instance_id:
            continue
        if launch["status"] != "active":
            continue
        type_name = launch["launched_type"] or launch["requested_type"]
        type_info = DEFAULT_MOCK_TYPES[type_name]
        client.instances[instance_id] = InstanceInfo(
            id=instance_id,
            # What the launch path names an instance: manifold-<launch id>.
            name=f"manifold-{launch['id']}",
            status="active",
            ip=_MOCK_INSTANCE_IP,
            region=launch["region"],
            instance_type=type_name,
            hourly_rate_cents=type_info.price_cents_per_hour,
            gpu_description=type_info.gpu_description,
            file_system_names=([launch["filesystem"]]
                               if launch["filesystem"] else []),
        )
        registered.append(instance_id)
    return registered


# -- the fixture ----------------------------------------------------------


def _plan(days: int, now: datetime, rng: Random) -> list[_Row]:
    """Decide every fixture row, relative to `now`. Pure: no writes."""
    window_hours = days * 24.0

    def ago(hours: float) -> datetime:
        """A moment `hours` before now, never earlier than the window."""
        return now - timedelta(hours=min(hours, window_hours))

    rows: list[_Row] = []

    # billing: the box that is running right now, cost still accruing.
    launched = ago(3.4)
    rows.append(_Row(
        state="billing",
        requested_type="gpu_1x_h100_sxm5",
        launched_type="gpu_1x_h100_sxm5",
        region="us-east-2", rate_cents=429, rate_known=True,
        status="active", attempts=1,
        created_at=launched - timedelta(seconds=40),
        launched_at=launched,
        active_at=launched + timedelta(minutes=4),
        terminated_at=None, error=None,
        # It carries a cloud instance id, like every real 'active' launch
        # does (a launch only reaches 'active' after its id is stored).
        # register_live_instances() then puts that instance into the mock
        # cloud's listing, so the row is genuinely running rather than
        # merely claiming to be: the sweep leaves it alone, and the spend
        # page shows real live burn.
        with_instance=True,
    ))

    # never_started: capacity ran out, no instance ever existed, so the cost
    # is a known zero rather than an unknown.
    failed_at = ago(40.0)
    rows.append(_Row(
        state="never_started",
        requested_type="gpu_8x_h100_sxm5", launched_type=None,
        region="us-south-1", rate_cents=3192, rate_known=True,
        status="failed", attempts=5,
        created_at=failed_at, launched_at=None, active_at=None,
        terminated_at=None,
        error="no capacity for gpu_8x_h100_sxm5 in us-south-1 after 5 "
              "attempts",
        with_instance=False,
    ))

    # unresolved: it launched, so it billed, but it never reached 'active'
    # and nothing ever wrote a terminated_at. The cost has a floor and no
    # ceiling, which is exactly the case a spend page must not silently
    # round to zero.
    launched = ago(110.0)
    rows.append(_Row(
        state="unresolved",
        requested_type="gpu_1x_a100_sxm4", launched_type="gpu_1x_a100_sxm4",
        region="us-west-3", rate_cents=199, rate_known=True,
        status="failed", attempts=1,
        created_at=launched - timedelta(seconds=25),
        launched_at=launched, active_at=None, terminated_at=None,
        error="instance never reached 'active' within the boot timeout",
        with_instance=True,
    ))

    # rate_unknown: a finished run whose hourly price was never recorded.
    launched = ago(190.0)
    rows.append(_Row(
        state="rate_unknown",
        requested_type="gpu_1x_a10", launched_type="gpu_1x_a10",
        region="us-east-1", rate_cents=129, rate_known=False,
        status="terminated", attempts=1,
        created_at=launched - timedelta(seconds=30),
        launched_at=launched,
        active_at=launched + timedelta(minutes=3),
        terminated_at=launched + timedelta(hours=3.5),
        error=None, with_instance=True,
    ))

    # billed, multi-day: the long fine-tune. Clamped so it still fits when
    # the demo window is short.
    long_hours = min(_LONG_RUN_HOURS, max(2.0, window_hours - 8.0))
    ended = ago(6.0)
    launched = ended - timedelta(hours=long_hours)
    rows.append(_Row(
        state="billed",
        requested_type="gpu_8x_a100_80gb_sxm4",
        launched_type="gpu_8x_a100_80gb_sxm4",
        region="us-midwest-1", rate_cents=2232, rate_known=True,
        status="terminated", attempts=1,
        created_at=launched - timedelta(seconds=55),
        launched_at=launched,
        active_at=launched + timedelta(minutes=6),
        terminated_at=ended, error=None, with_instance=True,
    ))

    # billed, after a fallback: asked for an H100, got an A100 after two
    # capacity retries, and is billed at the price of what actually ran.
    launched = ago(70.0)
    rows.append(_Row(
        state="billed",
        requested_type="gpu_1x_h100_sxm5", launched_type="gpu_1x_a100_sxm4",
        region="us-east-1", rate_cents=199, rate_known=True,
        status="terminated", attempts=3,
        created_at=launched - timedelta(minutes=2),
        launched_at=launched,
        active_at=launched + timedelta(minutes=4),
        terminated_at=launched + timedelta(hours=2.75),
        error=None, with_instance=True,
    ))

    # billed, filler: ordinary finished runs spread over the window.
    for day in range(1, days):
        for _ in range(rng.choice(_RUNS_PER_DAY)):
            start_hours_ago = day * 24 - rng.randrange(0, 20)
            requested, rate, region = rng.choice(_FILLER_MIX)
            # Never let a finished run spill past `now`.
            hours = min(round(rng.uniform(0.3, 5.5), 2),
                        max(0.25, start_hours_ago - 1.0))
            launched = ago(start_hours_ago)
            rows.append(_Row(
                state="billed",
                requested_type=requested, launched_type=requested,
                region=region, rate_cents=rate, rate_known=True,
                status="terminated", attempts=1,
                created_at=launched - timedelta(seconds=35),
                launched_at=launched,
                active_at=launched + timedelta(minutes=4),
                terminated_at=launched + timedelta(hours=hours),
                error=None, with_instance=True,
            ))

    return rows


# -- helpers --------------------------------------------------------------


def _parse(now_iso: str) -> datetime:
    """The anchor every fixture span is measured back from."""
    try:
        moment = datetime.fromisoformat(now_iso)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"mock seed: now_iso is not an ISO timestamp: {now_iso!r}"
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    """Same shape db.utcnow() writes, so fixture and live rows compare."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _filesystem_for(region: str) -> str | None:
    return _MOCK_FILESYSTEM if region == _MOCK_FILESYSTEM_REGION else None


def _instance_id(launch_id: str) -> str:
    """A deterministic stand-in for Lambda's 32-hex instance id, still
    obviously fixture data at a glance."""
    return "seed" + uuid.uuid5(_INSTANCE_NAMESPACE, launch_id).hex[4:]


def _audit_detail(launch_id: str, row: _Row) -> str:
    """The Audit tab's account of one fabricated launch."""
    rate = ("an unknown rate" if not row.rate_known
            else f"${row.rate_cents / 100:.2f}/hr")
    if row.launched_at is None:
        span = "never launched, so never billed"
    elif row.terminated_at is not None:
        span = f"ran {_iso(row.launched_at)} to {_iso(row.terminated_at)}"
    elif row.status == "active":
        span = f"still running since {_iso(row.launched_at)}"
    else:
        span = f"launched {_iso(row.launched_at)}, never closed out"
    return (f"{launch_id}: fabricated {row.state} launch, "
            f"{row.requested_type} in {row.region} at {rate}, {span}; "
            f"mock-mode demo data, not real spend")
