"""Phase 97: a ceiling anchored where the work starts.

max_lifetime_seconds runs from launch ACCEPTANCE, so boot, a driver
reboot, and a ten-minute model load all came out of the user's budget: 35
minutes of a 3-hour ceiling spent before the first token, and agents
sizing every ceiling as "run + 40 minutes" by hand. Folklore a platform
exists to absorb.

max_active_seconds is anchored at active_at - health-check pass - so it
bounds the thing the user actually controls. The absolute ceiling remains
the outer bound; either firing terminates through the same
rescue-files-first flow, with the honest label in the audit detail.

A box that has not reached active yet has NO active clock: breach None,
countdown None - never 0, because "no clock yet" and "0 seconds left" are
different facts on a destructive control.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.orchestrator import LaunchRejected, validate_max_active
from tests.conftest import make_settings
from tests.test_idle_matrix import TIMEOUT, Harness


def iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def harness(tmp_path, db):
    return Harness(tmp_path, db)


def with_active_ceiling(harness, instance_id, *, limit, active_ago,
                        launched_ago=None):
    launch_id = harness.add_instance(instance_id, idle_for=10)
    harness.db.update_launch(
        launch_id, max_active_seconds=limit,
        launched_at=iso_ago(
            launched_ago if launched_ago is not None
            else (active_ago + 600) if active_ago is not None else 3600),
        active_at=iso_ago(active_ago) if active_ago is not None else None)
    return launch_id


# -- validation ---------------------------------------------------------------


def test_validation_needs_no_boot_budget(tmp_path):
    """THE point: the clock starts after boot, so a 10-minute active
    ceiling is legal where a 10-minute lifetime ceiling is rejected."""
    settings = make_settings(tmp_path)
    assert validate_max_active(settings, 600.0) == 600.0


def test_validation_still_has_a_floor_and_a_roof(tmp_path):
    settings = make_settings(tmp_path)
    with pytest.raises(LaunchRejected):
        validate_max_active(settings, 30.0)
    with pytest.raises(LaunchRejected):
        validate_max_active(settings, 10_000_000.0)
    assert validate_max_active(settings, None) is None


# -- the breach arithmetic ----------------------------------------------------


async def test_boot_time_never_spends_the_active_budget(harness):
    """Launched 90 minutes ago, active 30 minutes ago, 1h active ceiling:
    under the OLD anchor this box would already be dead. It must live."""
    with_active_ceiling(harness, "i-slow-boot", limit=3600.0,
                        active_ago=1800.0, launched_ago=5400.0)

    await harness.dispatcher._check_idle()

    assert harness.terminated == []


async def test_a_box_past_its_active_ceiling_dies_with_the_honest_label(
        harness):
    with_active_ceiling(harness, "i-overran", limit=600.0, active_ago=700.0)

    await harness.dispatcher._check_idle()

    assert [i for i, _ in harness.terminated] == ["i-overran"]
    _id, kwargs = harness.terminated[0]
    # The audit detail names WHICH ceiling fired: "100s past max_active",
    # not the absolute ceiling's label.
    # (_terminate_for passes detail through; the harness records kwargs of
    # orchestrator.terminate, so read the audit row instead.)
    rows = [r for r in harness.db.list_audit(limit=10)
            if "max_active" in (r["detail"] or "")]
    assert rows, "the reap did not name the active ceiling"


async def test_not_yet_active_means_no_clock(harness):
    """A booting box with a 10-minute active ceiling and 3 hours since
    launch acceptance: no active_at, no clock, no breach."""
    with_active_ceiling(harness, "i-booting", limit=600.0,
                        active_ago=None, launched_ago=10_800.0)

    await harness.dispatcher._check_idle()

    assert harness.terminated == []


async def test_the_absolute_ceiling_remains_the_outer_bound(harness):
    """Both set, absolute breached, active not: the box still dies, under
    the absolute ceiling's label."""
    launch_id = with_active_ceiling(harness, "i-outer", limit=7200.0,
                                    active_ago=60.0)
    harness.db.update_launch(launch_id, max_lifetime_seconds=1.0,
                             launched_at=iso_ago(3600.0))

    await harness.dispatcher._check_idle()

    assert [i for i, _ in harness.terminated] == ["i-outer"]
    rows = [r for r in harness.db.list_audit(limit=10)
            if "max_lifetime" in (r["detail"] or "")]
    assert rows, "the absolute ceiling must keep its own label"


async def test_a_batch_job_still_defers_the_active_ceiling(harness):
    """Same deferral rules as the absolute ceiling: a fine-tune at 90% is
    the trade this project refuses to make."""
    with_active_ceiling(harness, "i-finetune", limit=600.0, active_ago=700.0)
    harness.pin_task("i-finetune", "axolotl-finetune")

    await harness.dispatcher._check_idle()

    assert harness.terminated == []


# -- the countdown the card reads ---------------------------------------------


async def test_status_counts_down_from_active_not_launch(harness):
    with_active_ceiling(harness, "i-count", limit=3600.0,
                        active_ago=600.0, launched_ago=3000.0)
    status = harness.dispatcher.ceiling_status("i-count")
    assert status["max_active_seconds"] == 3600.0
    remaining = status["active_seconds_remaining"]
    # ~3000s left (3600 - 600); the launch-anchored number would be ~600.
    assert 2900 <= remaining <= 3050, remaining


async def test_status_has_no_countdown_before_active(harness):
    with_active_ceiling(harness, "i-preboot", limit=3600.0, active_ago=None)
    status = harness.dispatcher.ceiling_status("i-preboot")
    assert status["max_active_seconds"] == 3600.0
    assert status["active_seconds_remaining"] is None, (
        '"no clock yet" must not render as a number')


def test_unset_stays_null_everywhere(harness):
    harness.add_instance("i-plain", idle_for=10)
    status = harness.dispatcher.ceiling_status("i-plain")
    assert status["max_active_seconds"] is None
    assert status["active_seconds_remaining"] is None


# -- end to end through the agent surface -------------------------------------


def test_launch_carries_the_active_ceiling(client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data", "max_active_seconds": 3600,
    })
    assert resp.status_code == 202, resp.text
    assert resp.json()["launch"]["max_active_seconds"] == 3600.0


def test_a_too_short_active_ceiling_is_rejected_not_clamped(client):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data", "max_active_seconds": 30,
    })
    assert resp.status_code == 400
    assert "max_active_seconds" in resp.json()["detail"]
