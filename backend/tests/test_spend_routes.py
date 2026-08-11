"""The /spend/* routes, the mock-history wiring behind them, and the MCP
tool that lets an agent ask what it has spent.

The arithmetic itself lives in test_spend.py (spend.py is pure). What is
proved here is the wiring: the routes are thin, they carry the demo marker,
they classify rows against the LAST instances sweep rather than a fresh
cloud call, and the seeder cannot run outside mock mode.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings, mock_connect_fn


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _finished_launch(db, *, hours_ago=4.0, hours=2.0, rate_cents=129):
    """One launch that ran and stopped, written the way the pipeline does."""
    launched = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    lid = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=rate_cents,
    )
    db.update_launch(
        lid, status="terminated", lambda_instance_id=f"i-{lid}",
        launched_type="gpu_1x_a10", launched_at=_iso(launched),
        active_at=_iso(launched + timedelta(minutes=4)),
        terminated_at=_iso(launched + timedelta(hours=hours)),
    )
    return lid


# -- the three routes -------------------------------------------------------


def test_summary_totals_a_finished_run_and_carries_the_disclaimer(client):
    db = client.app.state.orchestrator.db
    _finished_launch(db, hours_ago=3.0, hours=2.0)          # 2h at $1.29/hr

    body = client.get("/spend/summary").json()
    assert body["all_time_usd"] == 2.58
    assert body["today_usd"] == 2.58
    assert body["lower_bound"] is True
    assert "upper bound" in body["disclaimer"]
    assert body["timezone_label"] == "UTC+00:00"


def test_summary_buckets_in_the_callers_timezone(client):
    """A launch 3 hours ago is 'today' in UTC; for a caller 12 hours behind
    it can be yesterday. The window has to follow the caller's midnight."""
    db = client.app.state.orchestrator.db
    _finished_launch(db, hours_ago=3.0, hours=1.0)

    utc = client.get("/spend/summary", params={"tz_offset_minutes": 0}).json()
    behind = client.get("/spend/summary",
                        params={"tz_offset_minutes": -720}).json()
    assert utc["all_time_usd"] == behind["all_time_usd"]
    assert behind["timezone_label"] == "UTC-12:00"
    # Same money either way; only which day it lands in can differ.
    assert behind["today_usd"] <= utc["all_time_usd"]


def test_series_is_gap_filled_and_carries_the_mock_marker(client):
    db = client.app.state.orchestrator.db
    _finished_launch(db, hours_ago=2.0, hours=1.0)

    body = client.get("/spend/series", params={"days": 7}).json()
    assert body["mock"] is False
    assert len(body["series"]) == 7                 # zeros, not holes
    assert body["series"][-1]["usd"] == 1.29
    assert sum(p["launches"] for p in body["series"]) == 1


def test_breakdown_groups_by_the_requested_key(client):
    db = client.app.state.orchestrator.db
    _finished_launch(db, hours_ago=5.0, hours=1.0)

    body = client.get("/spend/breakdown",
                      params={"by": "region", "days": 30}).json()
    assert body["breakdown"][0]["key"] == "us-east-1"
    assert body["breakdown"][0]["count"] == 1


@pytest.mark.parametrize("path,params", [
    ("/spend/series", {"bucket": "fortnight"}),
    ("/spend/breakdown", {"by": "phase_of_the_moon"}),
])
def test_a_bad_grouping_is_a_clean_400(client, path, params):
    resp = client.get(path, params=params)
    assert resp.status_code == 400
    # The message names what IS allowed, so the caller can fix the call.
    assert "use one of" in resp.json()["detail"]


def test_days_is_clamped_rather_than_trusted(client):
    """A hand-typed days=999999 must not gap-fill a million buckets."""
    body = client.get("/spend/series", params={"days": 999999}).json()
    assert len(body["series"]) == 365
    assert client.get("/spend/series", params={"days": 0}).status_code == 200


@pytest.mark.parametrize("path", ["/spend/summary", "/spend/series",
                                  "/spend/breakdown"])
@pytest.mark.parametrize("offset,label", [(99999, "UTC+14:00"),
                                          (-99999, "UTC-12:00")])
def test_a_nonsense_timezone_offset_is_clamped(client, path, offset, label):
    """An offset nobody lives at does not fail loudly, it silently moves which
    midnight 'today' and 'month to date' are measured from: 99999 minutes is
    ~69 days. Real offsets stop at UTC-12:00 and UTC+14:00."""
    db = client.app.state.orchestrator.db
    _finished_launch(db, hours_ago=3.0, hours=2.0)

    body = client.get(path, params={"tz_offset_minutes": offset}).json()
    assert "detail" not in body
    if path == "/spend/summary":
        assert body["timezone_label"] == label
        assert body["all_time_usd"] == 2.58     # same money, either boundary


def test_every_spend_route_carries_the_demo_marker(client):
    """A dollar figure in a screenshot with no demo marker is the worst
    artifact this project could publish."""
    for path in ("/spend/summary", "/spend/series", "/spend/breakdown"):
        assert client.get(path).json()["mock"] is False


# -- the evidence the routes classify against -------------------------------


def test_a_running_instance_is_billing_and_shows_live_burn(client, mock_client):
    """The snapshot path: after an instances sweep, a launch whose instance
    the cloud lists is still billing, and its rate is the live burn."""
    from app.lambda_api import InstanceInfo

    db = client.app.state.orchestrator.db
    lid = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
    )
    db.update_launch(
        lid, status="active", lambda_instance_id="i-live",
        launched_type="gpu_1x_a10",
        launched_at=_iso(datetime.now(timezone.utc) - timedelta(hours=1)),
    )
    mock_client.instances["i-live"] = InstanceInfo(
        id="i-live", name="manifold-live", status="active", ip="192.0.2.10",
        region="us-east-1", instance_type="gpu_1x_a10", hourly_rate_cents=129,
    )
    assert client.get("/instances").status_code == 200   # the sweep

    body = client.get("/spend/summary").json()
    assert body["live_burn_usd_per_hour"] == 1.29
    assert body["all_time_usd"] > 0
    assert body["unresolved"]["count"] == 0


def test_a_launch_that_stopped_unobserved_is_a_range_never_a_zero(client):
    """The state a spend page must never round to $0: it launched, so it
    billed, and nobody saw it stop."""
    db = client.app.state.orchestrator.db
    lid = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
    )
    db.update_launch(
        lid, status="failed", lambda_instance_id="i-gone",
        launched_type="gpu_1x_a10",
        launched_at=_iso(datetime.now(timezone.utc) - timedelta(hours=6)),
        error="boot timed out",
    )
    client.get("/instances")        # the sweep: i-gone is not listed

    body = client.get("/spend/summary").json()
    assert body["unresolved"]["count"] == 1
    assert body["unresolved"]["launch_ids"] == [lid]
    # The RANGE is what must never be a zero. Its floor honestly is zero here:
    # no sweep ever saw this instance alive, so it may have died immediately,
    # and only evidence (last_seen_at) may raise the floor. See spend.py.
    assert body["unresolved"]["usd_high"] > 0
    assert body["unresolved"]["usd_high"] >= body["unresolved"]["usd_low"]
    # Unknown money is reported beside the total, never inside it.
    assert body["all_time_usd"] == 0.0


def test_the_routes_do_not_call_the_cloud(client, mock_client):
    """These get polled. A cloud call per poll would make the spend page a
    load on the Lambda API (and hostage to its latency)."""
    class _Refuses:
        async def list_instances(self, *, fresh: bool = False):
            raise AssertionError("spend routes must not call the cloud")

    db = client.app.state.orchestrator.db
    _finished_launch(db)
    orchestrator = client.app.state.orchestrator
    orchestrator.providers.register("lambda", _Refuses())

    for path in ("/spend/summary", "/spend/series", "/spend/breakdown"):
        assert client.get(path).status_code == 200


def test_no_sweep_yet_keeps_billing_rather_than_writing_a_box_off(
        settings, mock_client, mock_storage, mock_sidecar):
    """Before the first sweep there is no snapshot at all. An active launch
    must keep billing then: over-reporting a live box is the safe error."""
    from app.image_checker import MockImageChecker
    from app.db import Database

    database = Database(settings.db_path)
    lid = database.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
    )
    database.update_launch(
        lid, status="active", lambda_instance_id="i-unswept",
        launched_type="gpu_1x_a10",
        launched_at=_iso(datetime.now(timezone.utc) - timedelta(hours=2)),
    )
    database.close()

    application = create_app(
        settings, lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        image_checker=MockImageChecker(),
    )
    orchestrator = application.state.orchestrator
    assert orchestrator.last_cloud_snapshot() == (None, None)

    with TestClient(application) as unswept:
        body = unswept.get("/spend/summary").json()
    assert body["live_burn_usd_per_hour"] == 1.29
    assert body["all_time_usd"] > 0


# -- the mock-history wiring ------------------------------------------------


def _mock_app(tmp_path, mock_client, days: int):
    return create_app(
        make_settings(tmp_path), lambda_client=mock_client,
        custom_templates_dir=tmp_path / "custom-templates",
        mock=True, mock_seed_days=days,
    )


def test_mock_seed_days_fabricates_history_and_a_running_instance(
        tmp_path, mock_client):
    application = _mock_app(tmp_path, mock_client, days=30)
    with TestClient(application) as seeded_client:
        seeded_client.get("/instances")             # the sweep
        body = seeded_client.get("/spend/summary").json()
        series = seeded_client.get("/spend/series", params={"days": 30}).json()

    assert body["mock"] is True                     # fixture spend says so
    assert body["all_time_usd"] > 0
    # The demo's whole point: a box that is genuinely running, listed by the
    # mock cloud, so the live burn is a real number and not 0.00.
    assert body["live_burn_usd_per_hour"] > 0
    assert len(mock_client.instances) == 1
    assert sum(point["launches"] for point in series["series"]) > 1


def test_seeding_is_off_by_default(tmp_path, mock_client):
    application = _mock_app(tmp_path, mock_client, days=0)
    with TestClient(application) as unseeded:
        body = unseeded.get("/spend/summary").json()
    assert body["all_time_usd"] == 0.0
    assert body["mock"] is True
    assert mock_client.instances == {}


def test_real_mode_never_seeds(tmp_path, mock_client, mock_storage,
                               mock_sidecar):
    """The gate that matters: invented dollar amounts must never reach a
    real ledger, whatever the caller asks for."""
    from app.image_checker import MockImageChecker

    application = create_app(
        make_settings(tmp_path), lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        image_checker=MockImageChecker(),
        mock=False, mock_seed_days=30,
    )
    with TestClient(application) as real_mode:
        body = real_mode.get("/spend/summary").json()
        assert application.state.orchestrator.db.list_launches() == []
    assert body["mock"] is False
    assert body["all_time_usd"] == 0.0


# -- the MCP tool -----------------------------------------------------------


@pytest.fixture
async def mcp_spend_wired(tmp_path, mock_client, mock_storage, mock_sidecar):
    """The MCP module pointed at a real app, in-process (as test_mcp does)."""
    import httpx
    from asgi_lifespan import LifespanManager
    from app.image_checker import MockImageChecker
    import app.mcp_server as mcp_server

    application = create_app(
        make_settings(tmp_path), lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        image_checker=MockImageChecker(),
    )
    async with LifespanManager(application) as manager:
        old = mcp_server._client
        mcp_server._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app),
            base_url="http://manifold.test",
        )
        yield application
        await mcp_server._client.aclose()
        mcp_server._client = old


async def test_get_spend_answers_an_agent_and_is_audited(mcp_spend_wired):
    import app.mcp_server as mcp_server

    db = mcp_spend_wired.state.orchestrator.db
    _finished_launch(db, hours_ago=3.0, hours=2.0)

    result = await mcp_server.get_spend(note="checking budget before launch")
    assert result["all_time_usd"] == 2.58
    assert result["lower_bound"] is True
    assert "upper bound" in result["disclaimer"]
    assert result["mock"] is False
    # Every agent call lands in the audit log the user reviews.
    audit = [row for row in db.list_audit(limit=50) if row["actor"] == "mcp"]
    assert [row["action"] for row in audit] == ["get_spend"]
    assert "checking budget before launch" in audit[0]["detail"]
