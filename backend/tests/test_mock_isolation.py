"""Mock-mode isolation (incident 2026-07-17): a mock backend swapped
fixture state under a live agent session mid-launch. Three guarantees now:

1. Mock mode REFUSES to start while the real database records launches
   that may still have paying instances behind them.
2. Mock fixture state lives in its own database file - it can never read
   or rewrite real rows.
3. Fixture data is self-identifying: agent-facing listings carry
   "mock": true, so no one has to spot a TEST-NET IP to detect demo mode.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import Database, live_launches
from app.main import create_app
from tests.conftest import make_settings


def _real_db_with_launch(settings, *, status: str) -> str:
    """Seed the REAL database with a launch in `status`; returns launch id."""
    db = Database(settings.db_path)
    launch_id = db.create_launch(
        requested_type="gpu_1x_a100_sxm4", region="us-east-1",
        filesystem="Somnora-East", connection_mode="direct-ssh",
        hourly_rate_cents=199)
    db.update_launch(launch_id, status=status)
    db.close()
    return launch_id


def test_live_launches_reads_the_real_db_readonly(tmp_path):
    settings = make_settings(tmp_path)
    assert live_launches(settings.db_path) == []          # no file yet
    _real_db_with_launch(settings, status="active")
    _real_db_with_launch(settings, status="terminated")   # settled: ignored
    live = live_launches(settings.db_path)
    assert [l["status"] for l in live] == ["active"]


def test_mock_refuses_to_start_over_live_launches(tmp_path, monkeypatch):
    monkeypatch.delenv("MANIFOLD_MOCK_FORCE", raising=False)
    settings = make_settings(tmp_path)
    launch_id = _real_db_with_launch(settings, status="booting")
    with pytest.raises(SystemExit) as exc:
        create_app(settings, mock=True)
    assert "refusing to start in mock mode" in str(exc.value)
    assert launch_id in str(exc.value)


def test_mock_force_overrides_but_isolates_the_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MANIFOLD_MOCK_FORCE", "1")
    settings = make_settings(tmp_path)
    _real_db_with_launch(settings, status="active")
    app = create_app(settings, mock=True)
    with TestClient(app) as client:
        assert client.get("/health").json()["mock"] is True
        # The mock world sees NONE of the real state: its own db is empty.
        assert client.get("/launches").json()["launches"] == []
    # And the real database was not touched: the live launch is still there.
    assert len(live_launches(settings.db_path)) == 1


def test_mock_uses_its_own_database_file(tmp_path, monkeypatch):
    monkeypatch.delenv("MANIFOLD_MOCK_FORCE", raising=False)
    settings = make_settings(tmp_path)          # real db does not exist yet
    app = create_app(settings, mock=True)
    with TestClient(app) as client:
        client.get("/health")
    assert (tmp_path / "test-mock.db").exists()
    # The mock backend never created the REAL database.
    assert not Path(settings.db_path).exists()


def test_listings_are_self_identifying(tmp_path, monkeypatch):
    monkeypatch.delenv("MANIFOLD_MOCK_FORCE", raising=False)
    settings = make_settings(tmp_path)
    app = create_app(settings, mock=True)
    with TestClient(app) as client:
        assert client.get("/instances").json()["mock"] is True
        assert client.get("/filesystems").json()["mock"] is True
        assert client.get("/launch-options").json()["mock"] is True


def test_real_mode_listings_say_not_mock(client):
    assert client.get("/instances").json()["mock"] is False


def test_mock_stays_open_even_after_real_mode_minted_a_token(tmp_path):
    """The token outlives the mode; the zero-credential demo must not.

    Real mode mints a token and persists it to .env. Every later MOCK start
    then read it back and enforced it, so one accidental real-mode start
    permanently gated the README's "Try it in 90 seconds, no credentials"
    behind a credential the reader does not have. Mock mode has nothing to
    protect - mock client, no cloud, no spend - and NetworkGuardMiddleware
    still refuses any non-loopback caller regardless. Found by the
    2026-08-14 pre-launch audit.
    """
    from dataclasses import replace

    settings = replace(make_settings(tmp_path), api_token="a-real-mode-token")
    app = create_app(settings, mock=True)
    with TestClient(app) as client:
        # No Authorization header anywhere.
        assert client.get("/health").json()["mock"] is True
        assert client.get("/instances").status_code == 200
        assert client.get("/templates").status_code == 200


def test_a_token_is_still_enforced_outside_mock(tmp_path):
    """The other half of the rule: real mode with a token stays closed."""
    from dataclasses import replace

    from app.lambda_api import MockLambdaClient

    settings = replace(make_settings(tmp_path), api_token="a-real-mode-token")
    # lambda_client injected so no live calls; mock=False is the point.
    app = create_app(settings, lambda_client=MockLambdaClient())
    with TestClient(app) as client:
        assert client.get("/instances").status_code == 401
        assert client.get(
            "/instances",
            headers={"Authorization": "Bearer a-real-mode-token"},
        ).status_code == 200
