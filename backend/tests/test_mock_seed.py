"""The mock-mode history seeder: the two safety gates that stand between a
fixture and the real ledger, and the shape of what it fabricates.

Everything here writes to a throwaway `*-mock.db` under tmp_path. The
refusal tests deliberately point the seeder at a NON-mock database and
assert it wrote nothing.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db as db_module
from app.db import Database
from app.mock_seed import (
    AUDIT_ACTION,
    MAX_SEED_DAYS,
    SEED_ID_PREFIX,
    SEED_STATES,
    SeedRefused,
    connected_db_path,
    has_seeded_history,
    seed_mock_history,
)

NOW = "2026-08-10T12:00:00+00:00"
DAYS = 30


@pytest.fixture
def mock_db_path(tmp_path) -> str:
    """What main.py derives for MANIFOLD_MOCK=1: the real db name with a
    `-mock` stem, in its own file."""
    return str(tmp_path / "manifold-mock.db")


@pytest.fixture
def mock_db(mock_db_path):
    database = Database(mock_db_path)
    yield database
    database.close()


def seeded(database, path, *, days=DAYS, now=NOW) -> int:
    return seed_mock_history(database, days=days, now_iso=now,
                             mock=True, db_path=path)


def cost_state(launch: dict) -> str:
    """The spend feature's six-state model, read back off the row itself
    rather than trusted from the seeder's own labels."""
    if launch["launched_at"] is None:
        return "never_started"          # no instance, so a known-zero cost
    if launch["hourly_rate_cents"] is None:
        return "rate_unknown"           # it ran, but at what price?
    if launch["terminated_at"] is not None:
        return "billed"                 # start and end both known
    if launch["status"] == "active":
        return "billing"                # still accruing
    return "unresolved"                 # launched, never closed out


def mock_seed_audit(database) -> list[dict]:
    return [row for row in database.list_audit(limit=1000)
            if row["action"] == AUDIT_ACTION]


# -- the safety gates -------------------------------------------------------


def test_refuses_without_the_explicit_mock_flag(mock_db, mock_db_path):
    # Right database, wrong caller: a mock-named file is not on its own a
    # licence to invent spend.
    with pytest.raises(SeedRefused) as exc:
        seed_mock_history(mock_db, days=DAYS, now_iso=NOW,
                          mock=False, db_path=mock_db_path)
    assert "mock=True" in str(exc.value)
    assert mock_db.list_launches() == []
    assert mock_seed_audit(mock_db) == []


def test_refuses_a_truthy_non_boolean_mock_flag(mock_db, mock_db_path):
    # "0" is truthy. A gate that a stray string can open is not a gate.
    with pytest.raises(SeedRefused):
        seed_mock_history(mock_db, days=DAYS, now_iso=NOW,
                          mock="0", db_path=mock_db_path)
    assert mock_db.list_launches() == []


def test_refuses_a_database_that_is_not_the_mock_one(db, settings):
    # Right caller, wrong database: the conftest db is `test.db`, which is
    # exactly the shape of a real ledger.
    with pytest.raises(SeedRefused) as exc:
        seed_mock_history(db, days=DAYS, now_iso=NOW,
                          mock=True, db_path=settings.db_path)
    assert "-mock.db" in str(exc.value)
    assert db.list_launches() == []
    assert mock_seed_audit(db) == []


def test_refuses_a_lookalike_mock_database(tmp_path):
    # `-mock.db` has to be the END of the name, not a substring of it.
    path = str(tmp_path / "manifold-mock.db.bak")
    database = Database(path)
    try:
        with pytest.raises(SeedRefused):
            seed_mock_history(database, days=DAYS, now_iso=NOW, mock=True,
                              db_path=path)
        assert database.list_launches() == []
    finally:
        database.close()


def test_refuses_a_real_database_behind_a_mock_looking_path(db, settings):
    """Gate 2 is about the database being WRITTEN, not about a string the
    caller assembled. Pointing a real ledger's connection at a `db_path` that
    reads 'manifold-mock.db' used to sail straight through and fabricate nine
    launches into real history."""
    lookalike = str(Path(settings.db_path).with_name("manifold-mock.db"))

    with pytest.raises(SeedRefused) as exc:
        seed_mock_history(db, days=DAYS, now_iso=NOW, mock=True,
                          db_path=lookalike)

    # The refusal names the file it is actually open on, not the claim.
    assert settings.db_path in str(exc.value)
    assert db.list_launches() == []
    assert mock_seed_audit(db) == []


def test_refuses_when_the_callers_path_disagrees_with_the_connection(
        mock_db, tmp_path):
    """Both files are mock databases, so gate 2 is satisfied — but a caller
    that does not know which one it is writing to is exactly the wiring bug
    worth stopping."""
    with pytest.raises(SeedRefused) as exc:
        seed_mock_history(mock_db, days=DAYS, now_iso=NOW, mock=True,
                          db_path=str(tmp_path / "somewhere-else-mock.db"))
    assert "somewhere-else-mock.db" in str(exc.value)
    assert mock_db.list_launches() == []


def test_the_gate_reads_the_live_connection(mock_db, mock_db_path):
    assert (Path(connected_db_path(mock_db)).resolve()
            == Path(mock_db_path).resolve())


def test_rejects_a_nonsense_window(mock_db, mock_db_path):
    for days in (0, -3, 4000):
        with pytest.raises(ValueError):
            seeded(mock_db, mock_db_path, days=days)
    assert mock_db.list_launches() == []


def test_rejects_a_nonsense_anchor(mock_db, mock_db_path):
    with pytest.raises(ValueError):
        seeded(mock_db, mock_db_path, now="last tuesday")
    assert mock_db.list_launches() == []


# -- the happy path ---------------------------------------------------------


def test_seeds_rows_and_every_id_is_marked_as_a_fixture(mock_db, mock_db_path):
    created = seeded(mock_db, mock_db_path)
    launches = mock_db.list_launches()

    assert created > 0
    assert len(launches) == created
    assert all(launch["id"].startswith(SEED_ID_PREFIX) for launch in launches)
    # seed-0001 .. seed-000N, no uuids, no gaps.
    assert sorted(launch["id"] for launch in launches) == [
        f"{SEED_ID_PREFIX}{n:04d}" for n in range(1, created + 1)
    ]
    assert has_seeded_history(mock_db)


def test_covers_every_cost_state(mock_db, mock_db_path):
    seeded(mock_db, mock_db_path)
    states = {cost_state(launch) for launch in mock_db.list_launches()}
    assert set(SEED_STATES) <= states


def test_includes_a_multi_day_billed_run(mock_db, mock_db_path):
    seeded(mock_db, mock_db_path)
    spans = [
        datetime.fromisoformat(launch["terminated_at"])
        - datetime.fromisoformat(launch["launched_at"])
        for launch in mock_db.list_launches()
        if cost_state(launch) == "billed"
    ]
    assert max(spans) > timedelta(hours=24)


def test_prices_and_regions_come_from_the_mock_catalog(mock_db, mock_db_path):
    # A demo whose numbers do not match the catalog it demos is worse than
    # no demo: the cost arithmetic can no longer be checked by hand.
    from app.lambda_api import DEFAULT_MOCK_TYPES

    seeded(mock_db, mock_db_path)
    for launch in mock_db.list_launches():
        ran = launch["launched_type"] or launch["requested_type"]
        if launch["hourly_rate_cents"] is not None:
            assert (launch["hourly_rate_cents"]
                    == DEFAULT_MOCK_TYPES[ran].price_cents_per_hour)
        requested = DEFAULT_MOCK_TYPES[launch["requested_type"]]
        assert launch["region"] in requested.regions_with_capacity


def test_the_chart_has_variation_not_a_flat_line(mock_db, mock_db_path):
    seeded(mock_db, mock_db_path)
    per_day: dict[str, int] = {}
    for launch in mock_db.list_launches():
        if cost_state(launch) != "billed":
            continue
        day = launch["launched_at"][:10]
        per_day[day] = per_day.get(day, 0) + 1
    assert len(per_day) >= 5              # spread across the window
    assert len(set(per_day.values())) > 1  # and not the same every day


def test_every_span_sits_inside_the_window(mock_db, mock_db_path):
    seeded(mock_db, mock_db_path)
    now = datetime.fromisoformat(NOW)
    window_start = now - timedelta(days=DAYS)
    for launch in mock_db.list_launches():
        # Anchored to now_iso, so the fixture is always "the last N days"
        # and the right edge of a chart is never dead.
        assert window_start <= datetime.fromisoformat(launch["created_at"]) <= now
        for stamp in ("launched_at", "active_at", "terminated_at"):
            if launch[stamp]:
                assert window_start <= datetime.fromisoformat(launch[stamp]) <= now


def test_the_billing_row_looks_like_a_real_running_launch(mock_db, mock_db_path):
    # A launch only reaches 'active' AFTER its instance id is stored, so an
    # active row without one is a shape real data cannot produce. The
    # fixture carries the id; register_live_instances() below is what makes
    # the claim true.
    seeded(mock_db, mock_db_path)
    billing = [launch for launch in mock_db.list_launches()
               if cost_state(launch) == "billing"]
    assert billing
    assert all(launch["lambda_instance_id"] for launch in billing)


def test_the_mock_cloud_lists_the_instance_the_fixture_is_billing_for(
        mock_db, mock_db_path, mock_client):
    from app.mock_seed import register_live_instances

    seeded(mock_db, mock_db_path)
    registered = register_live_instances(mock_db, mock_client)
    billing = [launch for launch in mock_db.list_launches()
               if cost_state(launch) == "billing"]

    assert registered == [launch["lambda_instance_id"] for launch in billing]
    for launch in billing:
        instance = mock_client.instances[launch["lambda_instance_id"]]
        # Same box on both sides of the fixture: type, region and price all
        # match the ledger row, or the demo's arithmetic stops adding up.
        assert instance.status == "active"
        assert instance.instance_type == launch["launched_type"]
        assert instance.region == launch["region"]
        assert instance.hourly_rate_cents == launch["hourly_rate_cents"]
        assert instance.name == f"manifold-{launch['id']}"


def test_finished_launches_are_not_registered_as_running(
        mock_db, mock_db_path, mock_client):
    """Only the still-running fixture goes into the cloud listing. A
    terminated launch resurrected as a live instance would burn fake money
    forever and block the concurrency guard."""
    from app.mock_seed import register_live_instances

    seeded(mock_db, mock_db_path)
    registered = set(register_live_instances(mock_db, mock_client))
    for launch in mock_db.list_launches():
        if cost_state(launch) != "billing":
            assert launch["lambda_instance_id"] not in registered


def test_the_billing_row_is_left_alone_by_the_reconcile_sweep(
        mock_db, mock_db_path, mock_client, settings):
    """The point of registering the instance: the sweep that closes 'active'
    launches whose instance the cloud does not list now has no reason to
    touch this one."""
    import asyncio

    from app.mock_seed import register_live_instances
    from app.orchestrator import Orchestrator
    from tests.conftest import mock_connect_fn

    seeded(mock_db, mock_db_path)
    register_live_instances(mock_db, mock_client)
    orch = Orchestrator(settings, mock_client, mock_db,
                        connect_fn=mock_connect_fn)
    asyncio.run(orch.instances_with_state())

    billing = [launch for launch in mock_db.list_launches()
               if cost_state(launch) == "billing"]
    assert len(billing) == 1
    assert billing[0]["status"] == "active"
    assert billing[0]["terminated_at"] is None
    # And the sweep leaves the evidence the cost model wants.
    assert billing[0]["last_seen_at"] is not None


# -- determinism, idempotence, and the audit trail --------------------------


def test_two_runs_with_the_same_inputs_draw_the_same_picture(tmp_path):
    def fingerprint(name: str) -> list[tuple]:
        path = str(tmp_path / name)
        database = Database(path)
        try:
            seeded(database, path)
            return [
                (launch["id"], launch["requested_type"], launch["region"],
                 launch["hourly_rate_cents"], launch["status"],
                 launch["created_at"], launch["launched_at"],
                 launch["terminated_at"], launch["lambda_instance_id"])
                for launch in sorted(database.list_launches(),
                                     key=lambda row: row["id"])
            ]
        finally:
            database.close()

    assert fingerprint("a-mock.db") == fingerprint("b-mock.db")


def test_seeding_twice_does_not_duplicate(mock_db, mock_db_path):
    created = seeded(mock_db, mock_db_path)
    # Documented choice: no-op rather than clear-and-reseed.
    assert seeded(mock_db, mock_db_path) == 0
    assert len(mock_db.list_launches()) == created
    assert len(mock_seed_audit(mock_db)) == created


def test_one_audit_row_per_seeded_launch(mock_db, mock_db_path):
    created = seeded(mock_db, mock_db_path)
    audit = mock_seed_audit(mock_db)

    assert len(audit) == created
    assert {row["actor"] for row in audit} == {"backend"}
    ids = {launch["id"] for launch in mock_db.list_launches()}
    for row in audit:
        assert row["detail"].split(":")[0] in ids
        assert "not real spend" in row["detail"]
    # Every fabricated launch is accounted for, none twice.
    assert {row["detail"].split(":")[0] for row in audit} == ids


def test_seeding_leaves_db_module_untouched(mock_db, mock_db_path):
    # Trivially true since the seeder started passing launch_id/created_at
    # to create_launch() as ordinary parameters. Kept as the regression
    # test for the version that rebound these two names from the outside:
    # a fixture's convenience must never become a live writer's hazard.
    before = (db_module.uuid, db_module.utcnow)
    seeded(mock_db, mock_db_path)
    assert (db_module.uuid, db_module.utcnow) == before
    # Audit rows are stamped with the REAL clock: they record when the
    # fabrication happened, not when the fixture pretends it did.
    real_now = datetime.now(timezone.utc)
    for row in mock_seed_audit(mock_db):
        assert abs(datetime.fromisoformat(row["at"]) - real_now) \
            < timedelta(minutes=5)


def test_a_short_window_still_covers_every_state(mock_db, mock_db_path):
    # days=1 leaves no room for a filler loop; the guaranteed states must
    # not depend on one.
    created = seeded(mock_db, mock_db_path, days=1)
    assert created > 0
    states = {cost_state(launch) for launch in mock_db.list_launches()}
    assert set(SEED_STATES) <= states


# -- the demo knob itself ---------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("", 0),                # unset: no demo history
    ("   ", 0),
    ("0", 0),
    ("-5", 0),              # negatives are just "off", not an error
    ("abc", 0),             # unreadable: ignored, logged, NOT fatal
    ("1", 1),
    ("30", 30),
    ("400", MAX_SEED_DAYS),  # clamped into the seeder's window
])
def test_the_demo_knob_can_never_stop_the_backend_starting(raw, expected):
    """MANIFOLD_MOCK_SEED_DAYS=abc used to raise from int() and =400 from the
    seeder's own range check, either one killing the process at startup. A
    mock-mode demo knob must not be able to brick the app."""
    from app.main import mock_seed_days_from_env

    assert mock_seed_days_from_env(raw) == expected
