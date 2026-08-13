"""Phase 81: team mode.

Two walls and a ledger. The NetworkGuardMiddleware refuses to serve a
network the deployment is not ready for (no token, or plaintext without
the explicit tailnet opt-in) - judged per request on the interface the
connection actually arrived at, so it holds however uvicorn was started.
The per-principal hourly ceiling is a real orchestrator guard: a launch
that would push a principal's ATTRIBUTED burn past their cap is refused,
and chain attribution makes auto-manage jobs and watches count against
whoever caused them. Spend gains a by-principal grouping so the team
question - whose work is the money - has an answer.
"""

import pytest
from fastapi.testclient import TestClient

from app.auth import NetworkGuardMiddleware, _is_nonloopback_ip
from app.main import create_app
from tests.conftest import make_settings, mock_connect_fn

OWNER = "owner-token-for-tests"


# -- the network guard, at the ASGI level ------------------------------------
# TestClient reports server=("testserver", 80) - a hostname, treated as
# local - so these tests drive the middleware directly with crafted
# scopes, the same shape uvicorn produces.


class Sink:
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self):
        return next(m["status"] for m in self.messages
                    if m["type"] == "http.response.start")

    @property
    def body(self):
        return b"".join(m.get("body", b"") for m in self.messages
                        if m["type"] == "http.response.body").decode()


async def _passthrough(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def scope_for(host, scheme="http"):
    return {"type": "http", "path": "/instances", "method": "GET",
            "scheme": scheme, "server": (host, 8000), "headers": []}


async def test_loopback_always_passes():
    mw = NetworkGuardMiddleware(_passthrough, token_configured=False,
                                allow_plaintext=False)
    for host in ("127.0.0.1", "::1", "testserver"):
        sink = Sink()
        await mw(scope_for(host), None, sink)
        assert sink.status == 200, host


async def test_network_without_token_is_refused():
    mw = NetworkGuardMiddleware(_passthrough, token_configured=False,
                                allow_plaintext=True)
    sink = Sink()
    await mw(scope_for("192.168.1.5"), None, sink)
    assert sink.status == 403
    assert "no API token" in sink.body


async def test_network_plaintext_needs_the_optin():
    mw = NetworkGuardMiddleware(_passthrough, token_configured=True,
                                allow_plaintext=False)
    sink = Sink()
    await mw(scope_for("100.64.0.7"), None, sink)
    assert sink.status == 403
    assert "allow_plaintext_lan" in sink.body        # the fix is named
    # The opt-in (a tailnet, already encrypted below http) opens it.
    mw = NetworkGuardMiddleware(_passthrough, token_configured=True,
                                allow_plaintext=True)
    sink = Sink()
    await mw(scope_for("100.64.0.7"), None, sink)
    assert sink.status == 200


async def test_network_tls_needs_no_optin():
    mw = NetworkGuardMiddleware(_passthrough, token_configured=True,
                                allow_plaintext=False)
    sink = Sink()
    await mw(scope_for("192.168.1.5", scheme="https"), None, sink)
    assert sink.status == 200


async def test_websocket_refusal_closes_4403():
    mw = NetworkGuardMiddleware(_passthrough, token_configured=False,
                                allow_plaintext=False)
    sink = Sink()

    async def receive():
        return {"type": "websocket.connect"}

    await mw({"type": "websocket", "path": "/local/terminal",
              "scheme": "ws", "server": ("10.0.0.9", 8000), "headers": []},
             receive, sink)
    assert {"type": "websocket.close", "code": 4403} in sink.messages


def test_nonloopback_ip_classifier():
    assert _is_nonloopback_ip("192.168.1.5")
    assert _is_nonloopback_ip("100.64.0.7")
    assert not _is_nonloopback_ip("127.0.0.1")
    assert not _is_nonloopback_ip("::1")
    assert not _is_nonloopback_ip("testserver")   # hostname = test client
    assert not _is_nonloopback_ip("localhost")


# -- the per-principal ceiling -------------------------------------------------


def _owner_client(tmp_path, mock_client, mock_storage, mock_sidecar,
                  **settings_overrides):
    app = create_app(
        make_settings(tmp_path, api_token=OWNER, **settings_overrides),
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        env_path=tmp_path / ".env",
    )
    return TestClient(app, headers={"Authorization": f"Bearer {OWNER}"})


@pytest.fixture
def owner(tmp_path, mock_client, mock_storage, mock_sidecar):
    with _owner_client(tmp_path, mock_client, mock_storage,
                       mock_sidecar) as c:
        yield c


@pytest.fixture
def roomy_owner(tmp_path, mock_client, mock_storage, mock_sidecar):
    """Global guardrails opened wide, so the PRINCIPAL ceiling is the
    binding constraint under test (with the defaults, the global
    concurrency guard of 1 fires first and proves nothing about 81)."""
    from app.config import Guardrails
    with _owner_client(
            tmp_path, mock_client, mock_storage, mock_sidecar,
            guardrails=Guardrails(max_concurrent_instances=5,
                                  max_hourly_spend_usd=10.0)) as c:
        yield c


def capped_client(owner, name, usd):
    resp = owner.post("/principals", json={
        "name": name, "role": "operator", "max_hourly_spend_usd": usd})
    assert resp.status_code == 201, resp.text
    assert resp.json()["max_hourly_spend_usd"] == usd
    c = TestClient(owner.app)
    c.headers.update(
        {"Authorization": f"Bearer {resp.json()['token']}"})
    return c


LAUNCH = {"instance_type": "gpu_1x_a10", "region": "us-east-1",
          "filesystem": "manifold-data"}   # $1.29/hr in the mock catalog


def test_ceiling_refuses_the_launch_that_would_cross_it(roomy_owner):
    worker = capped_client(roomy_owner, "worker", 2.00)
    assert worker.post("/instances", json=LAUNCH).status_code == 202
    # 1.29 committed; another 1.29 would make 2.58 > 2.00.
    resp = worker.post("/instances", json=LAUNCH)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "worker" in detail and "$2.00" in detail
    assert "ceiling" in detail.lower()


def test_ceiling_is_per_principal_not_global(owner):
    """One capped principal must not eat anyone else's headroom - and the
    global guards still bind above everyone (concurrency=1 in the test
    settings, so the owner's launch is refused by the GLOBAL guard, not
    the principal's)."""
    worker = capped_client(owner, "worker", 2.00)
    assert worker.post("/instances", json=LAUNCH).status_code == 202
    resp = owner.post("/instances", json=LAUNCH)
    assert resp.status_code == 409
    assert "Concurrency guard" in resp.json()["detail"]


def test_uncapped_principal_and_owner_are_unlimited(owner):
    resp = owner.post("/principals", json={"name": "free",
                                           "role": "operator"})
    assert resp.json()["max_hourly_spend_usd"] is None
    # Owner has no row at all; the ceiling code must simply not apply.
    assert owner.post("/instances", json=LAUNCH).status_code == 202


def test_pending_launches_count_against_the_ceiling(roomy_owner):
    """The double-admit window: a launch that is admitted but not yet
    cloud-visible already spends the principal's budget."""
    owner = roomy_owner
    db = owner.app.state.orchestrator.db
    worker = capped_client(owner, "worker", 2.00)
    db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129, created_by="worker")   # pending: no iid
    resp = worker.post("/instances", json=LAUNCH)
    assert resp.status_code == 409
    assert "principal" in resp.json()["detail"].lower() or \
        "worker" in resp.json()["detail"]


def test_bad_ceiling_is_422(owner):
    for bad in (-1, 0):
        resp = owner.post("/principals", json={
            "name": f"x{abs(bad)}", "max_hourly_spend_usd": bad})
        assert resp.status_code == 422


# -- spend by principal --------------------------------------------------------


def test_breakdown_groups_by_principal(owner):
    from datetime import datetime, timedelta, timezone
    db = owner.app.state.orchestrator.db
    now = datetime.now(timezone.utc)
    for who, hours_ago in (("worker", 5.0), ("worker", 3.0), (None, 8.0)):
        lid = db.create_launch(
            requested_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data", connection_mode="direct-ssh",
            hourly_rate_cents=129, created_by=who)
        start = now - timedelta(hours=hours_ago)
        db.update_launch(
            lid, status="terminated", lambda_instance_id=f"i-{lid}",
            launched_at=start.isoformat(timespec="seconds"),
            terminated_at=(start + timedelta(hours=1)).isoformat(
                timespec="seconds"))

    body = owner.get("/spend/breakdown",
                     params={"by": "created_by", "days": 30}).json()
    rows = {r["key"]: r for r in body["breakdown"]}
    assert rows["worker"]["count"] == 2
    assert rows["worker"]["usd"] == pytest.approx(2.58)
    # Pre-attribution rows are a true statement, not a guess.
    assert rows["unattributed"]["count"] == 1
