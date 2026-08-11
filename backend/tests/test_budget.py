"""The monthly budget: a cumulative wallet that REPORTS and never refuses.

The two guardrails above it (max_concurrent_instances, max_hourly_spend_usd)
are rate ceilings the orchestrator enforces before a launch. This one is a
different kind of number, and these tests pin the difference: month-to-date
is reconstructed from the launches Manifold started, so it is a lower bound,
and a lower bound must never be allowed to refuse work.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import spend
from app.preferences import NOTIFICATION_KINDS, GuardrailPrefs


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


# -- the pure burn-down ------------------------------------------------------

def test_no_budget_set_is_unset_not_zero():
    """0 means "no wallet", which is a different answer from "$0 left"."""
    s = spend.budget_status(month_to_date_usd=120.0, burn_usd_per_hour=4.29,
                            monthly_budget_usd=0.0, now_iso=_iso(NOW))
    assert s["state"] == "unset"
    assert s["remaining_usd"] is None
    assert s["used_pct"] is None
    assert s["projected_month_end_usd"] is None


def test_burn_down_reports_what_is_left():
    s = spend.budget_status(month_to_date_usd=120.0, burn_usd_per_hour=0.0,
                            monthly_budget_usd=500.0, now_iso=_iso(NOW))
    assert s["state"] == "ok"
    assert s["remaining_usd"] == 380.0
    assert s["used_pct"] == 24.0


def test_eighty_percent_is_a_warning_and_over_is_over():
    warn = spend.budget_status(month_to_date_usd=400.0, burn_usd_per_hour=0.0,
                               monthly_budget_usd=500.0, now_iso=_iso(NOW))
    assert warn["state"] == "warn"
    over = spend.budget_status(month_to_date_usd=620.0, burn_usd_per_hour=0.0,
                               monthly_budget_usd=500.0, now_iso=_iso(NOW))
    assert over["state"] == "over"
    assert over["remaining_usd"] == -120.0     # how far over, not clamped to 0


def test_projection_answers_if_i_leave_this_running():
    """The projection is CURRENT burn to month end, not a forecast of what
    you might launch next. With nothing running it is month-to-date, which
    is correct: at a burn of zero, nothing more is spent."""
    idle = spend.budget_status(month_to_date_usd=120.0, burn_usd_per_hour=0.0,
                               monthly_budget_usd=500.0, now_iso=_iso(NOW))
    assert idle["projected_month_end_usd"] == 120.0

    burning = spend.budget_status(month_to_date_usd=120.0, burn_usd_per_hour=1.0,
                                  monthly_budget_usd=500.0, now_iso=_iso(NOW))
    assert burning["projected_month_end_usd"] > 120.0


def test_exhaustion_date_only_when_it_happens_this_month():
    """The wallet resets at the month boundary, so a burn too slow to reach
    the cap before then has no exhaustion date rather than a fake one."""
    fast = spend.budget_status(month_to_date_usd=100.0, burn_usd_per_hour=10.0,
                               monthly_budget_usd=200.0, now_iso=_iso(NOW))
    assert fast["exhausted_on"] is not None

    slow = spend.budget_status(month_to_date_usd=100.0, burn_usd_per_hour=0.01,
                               monthly_budget_usd=100000.0, now_iso=_iso(NOW))
    assert slow["exhausted_on"] is None


def test_already_over_has_no_exhaustion_date():
    s = spend.budget_status(month_to_date_usd=600.0, burn_usd_per_hour=4.29,
                            monthly_budget_usd=500.0, now_iso=_iso(NOW))
    assert s["exhausted_on"] is None           # it already happened


def test_december_rolls_into_january_without_crashing():
    dec = datetime(2026, 12, 30, 12, 0, 0, tzinfo=timezone.utc)
    s = spend.budget_status(month_to_date_usd=10.0, burn_usd_per_hour=1.0,
                            monthly_budget_usd=100.0, now_iso=_iso(dec))
    assert s["state"] == "ok"
    assert s["hours_left_in_month"] > 0


# -- the guard must stay a RATE guard ---------------------------------------

def test_a_monthly_budget_never_refuses_a_launch(client, mock_client):
    """The whole point. Set a budget of $1, blow past it, and launching must
    still be governed only by the RATE guards."""
    db = client.app.state.orchestrator.db
    # A real, finished launch that already blew the wallet: 10h at $1.29.
    started = datetime.now(timezone.utc) - timedelta(hours=10)
    lid = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
    )
    db.update_launch(lid, status="terminated", launched_type="gpu_1x_a10",
                     lambda_instance_id="i-spent",
                     launched_at=started.isoformat(timespec="seconds"),
                     terminated_at=datetime.now(timezone.utc)
                     .isoformat(timespec="seconds"))
    client.put("/preferences", json={
        "guardrails": {"monthly_budget_usd": 1.0,
                       "max_hourly_spend_usd": 50.0,
                       "max_concurrent_instances": 5},
    })
    assert client.get("/spend/summary").json()["budget"]["state"] == "over"

    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    # 202: admitted, launching asynchronously. Being 12x over the wallet
    # changed nothing, because only the RATE guards may refuse a launch.
    assert resp.status_code == 202, resp.text


def test_summary_reports_the_budget(client):
    client.put("/preferences", json={"guardrails": {"monthly_budget_usd": 250.0}})
    body = client.get("/spend/summary").json()
    assert body["budget"]["monthly_budget_usd"] == 250.0
    assert body["budget"]["state"] in ("ok", "warn", "over")


def test_summary_says_unset_when_there_is_no_budget(client):
    body = client.get("/spend/summary").json()
    assert body["budget"]["state"] == "unset"


# -- preferences plumbing ----------------------------------------------------

def test_budget_threshold_has_a_toggle():
    """A kind missing from NotificationPrefs is silently dropped by wants(),
    so the feature would ship dead. Guards the seven-touchpoint dance."""
    from app.preferences import NotificationPrefs
    assert "budget_threshold" in NOTIFICATION_KINDS
    assert hasattr(NotificationPrefs(), "budget_threshold")


def test_a_negative_budget_is_clamped_not_honoured():
    from app.preferences import _validate
    fixed = _validate(GuardrailPrefs(monthly_budget_usd=-5.0))
    assert fixed.monthly_budget_usd == 0.0


@pytest.mark.parametrize("section,field,value", [
    ("approvals", "launch_gpu", False),
    ("notifications", "job_failed", False),
    ("data_safety", "scope", "outputs"),
    ("guardrails", "monthly_budget_usd", 42.0),
    ("worklog", "mirror_dir", "/tmp/manifold-worklog"),
    ("onboarding", "completed", True),
])
def test_preferences_round_trip_every_section(client, section, field, value):
    """Every section of Preferences must survive a PUT.

    A section missing from PreferencesPatch is dropped by
    model_dump(exclude_none=True) before the handler sees it, so the PUT
    returns 200 with the value unchanged: a silent success on a failed
    write. `worklog` sat in exactly that state until Phase 76c. This is
    parametrised so a NEW section fails here rather than in production.
    """
    resp = client.put("/preferences", json={section: {field: value}})
    assert resp.status_code == 200, resp.text
    stored = client.get("/preferences").json()["preferences"]
    assert stored[section][field] == value, (
        f"{section}.{field} did not persist - is '{section}' listed in "
        f"PreferencesPatch?"
    )
