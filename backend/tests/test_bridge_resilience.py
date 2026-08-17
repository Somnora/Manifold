"""Phase 95, the bridge half: facts move out of agents' memories.

Three changes with one shape. `purpose` becomes required at the agent
surface (agents are the population that both causes and suffers the
unattributed-box problem). A connection-refused call retries quietly
through a backend restart, because a refused request never reached the
backend and so cannot double an effect - and the restart itself provably
does not touch instance work. And a bridge running an older binary than
the backend says so on every result, because twice a tool shipped that a
running agent provably needed and could not call, and nothing told it a
newer surface existed.

Plus the storage estimate: filesystem billing was invisible in every
number this product could report (~$50/month found only by manual audit).
Lambda publishes no rate, so it is an ESTIMATE at the user-written rate in
config.yaml, in its own block, never folded into the launch totals - and
None, not $0, when switched off or unreadable.
"""

import httpx
import pytest

from app import mcp_server
from app.config import StorageSettings
from tests.test_mcp import mcp_wired, wired_app  # noqa: F401 - fixtures


# -- purpose is required at the agent surface ---------------------------------


async def test_a_purposeless_launch_is_refused_before_any_http(mcp_wired,
                                                               mock_client):
    result = await mcp_server.launch_gpu(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", purpose="   ", note="no purpose test",
    )
    assert "purpose is required" in result["error"]
    assert mock_client.launch_calls == [], (
        "the launch reached the backend despite having no purpose")


async def test_a_purposeful_launch_carries_it_through(mcp_wired):
    result = await mcp_server.launch_gpu(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data",
        purpose="whisper batch for the interview set", note="purpose test",
    )
    launch_id = result["launch"]["id"]
    resp = await mcp_server._http().get(f"/launches/{launch_id}")
    assert resp.json()["purpose"] == "whisper batch for the interview set"


# -- connection-refused retries through a restart window ----------------------


class _RefusingClient:
    """request() refuses N times, then delegates. get/post pass through."""

    def __init__(self, inner, refuse: int):
        self._inner = inner
        self.refuse = refuse
        self.attempts = 0

    async def request(self, *a, **k):
        self.attempts += 1
        if self.attempts <= self.refuse:
            raise httpx.ConnectError("connection refused (backend restarting)")
        return await self._inner.request(*a, **k)

    async def get(self, *a, **k):
        return await self._inner.get(*a, **k)

    async def post(self, *a, **k):
        return await self._inner.post(*a, **k)


async def _instant(_s):
    return None


async def test_refused_calls_retry_through_a_restart(mcp_wired, monkeypatch):
    """A 24-41s app upgrade must read as one slow call, not an error - the
    request never reached the backend, so a retry cannot double anything."""
    real = mcp_server._client
    refusing = _RefusingClient(real, refuse=3)
    monkeypatch.setattr(mcp_server, "_client", refusing)
    monkeypatch.setattr(mcp_server.asyncio, "sleep", _instant)

    result = await mcp_server.list_instances(note="poll across restart")

    assert refusing.attempts == 4, "did not retry through the refusals"
    assert "instances" in result
    assert not result.get("unreachable")


async def test_a_restart_longer_than_the_window_still_errors(mcp_wired,
                                                             monkeypatch):
    """The retry is bounded well inside the MCP client's ~60s kill: an
    answer nobody is listening for is not an answer."""
    refusing = _RefusingClient(mcp_server._client, refuse=10_000)
    monkeypatch.setattr(mcp_server, "_client", refusing)
    monkeypatch.setattr(mcp_server.asyncio, "sleep", _instant)

    result = await mcp_server.list_instances(note="backend actually gone")

    assert result.get("unreachable") is True
    # ~40s at 3s per retry: a bounded handful, not forever.
    assert refusing.attempts <= 16


async def test_timeouts_are_never_retried(mcp_wired, monkeypatch):
    """A timed-out request MAY HAVE LANDED. Replaying it could launch a
    second GPU. This is the line between absorbing a restart and
    double-spending through one."""

    class _TimingOut:
        def __init__(self):
            self.attempts = 0

        async def request(self, *a, **k):
            self.attempts += 1
            raise httpx.ReadTimeout("no answer in time")

        async def get(self, *a, **k):
            raise httpx.ReadTimeout("no answer in time")

        async def post(self, *a, **k):
            raise httpx.ReadTimeout("no answer in time")

    timing_out = _TimingOut()
    monkeypatch.setattr(mcp_server, "_client", timing_out)
    monkeypatch.setattr(mcp_server.asyncio, "sleep", _instant)

    result = await mcp_server.list_instances(note="timeout test")

    assert timing_out.attempts == 1, "a timed-out request was replayed"
    assert result.get("unreachable") is True


# -- the bridge knows when it is behind ---------------------------------------


async def test_health_names_the_backend_version(mcp_wired):
    resp = await mcp_server._http().get("/health")
    assert "version" in resp.json()


async def test_a_drifted_bridge_says_so_on_every_result(mcp_wired,
                                                        monkeypatch):
    """The exact failure: set_keep_alive shipped, and the session that
    needed it could not see it and was told nothing."""
    monkeypatch.setattr(mcp_server, "_drift_note", None)
    monkeypatch.setattr(mcp_server, "_bridge_version", lambda: "0.2.1")
    result = await mcp_server.list_instances(note="drift test")
    assert "bridge_version_note" in result
    assert "restart your session" in result["bridge_version_note"].lower()


async def test_a_current_bridge_adds_no_noise(mcp_wired, monkeypatch):
    monkeypatch.setattr(mcp_server, "_drift_note", None)
    resp = await mcp_server._http().get("/health")
    backend_version = resp.json()["version"]
    monkeypatch.setattr(mcp_server, "_bridge_version",
                        lambda: backend_version)
    result = await mcp_server.list_instances(note="no-drift test")
    assert "bridge_version_note" not in result


# -- the storage estimate -----------------------------------------------------


def test_storage_rate_is_actually_readable_from_config(tmp_path):
    """The busy_util_pct lesson, applied at birth: a config key missing
    from the loader's explicit list keeps its default whatever the file
    says. This one must be born readable."""
    from app.config import load_settings
    (tmp_path / "config.yaml").write_text(
        "storage:\n  rate_usd_per_gb_month: 0.35\n")
    settings = load_settings(config_path=tmp_path / "config.yaml",
                             env_path=tmp_path / ".env")
    assert settings.storage.rate_usd_per_gb_month == 0.35


def test_spend_carries_a_labelled_storage_estimate(client):
    body = client.get("/spend/summary").json()
    est = body["storage_estimate"]
    assert est is not None
    assert est["rate_usd_per_gb_month"] == 0.20
    assert est["usd_per_month_estimate"] == round(
        est["gb_used"] * est["rate_usd_per_gb_month"], 2)
    assert "config.yaml" in est["note"], (
        "the estimate must say where its rate came from")


def test_rate_zero_means_absent_not_a_zero_dollar_claim(tmp_path, mock_client,
                                                        mock_storage,
                                                        mock_sidecar,
                                                        mock_model):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import make_settings, mock_connect_fn
    app = create_app(
        make_settings(tmp_path,
                      storage=StorageSettings(rate_usd_per_gb_month=0)),
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        env_path=tmp_path / ".env",
    )
    with TestClient(app) as c:
        body = c.get("/spend/summary").json()
    assert body["storage_estimate"] is None, (
        "switched off must be absent, not $0.00")
