"""Phase 79: named principals and attribution.

Tokens resolve to NAMES; names land on everything a request causes. The
.env token is "owner" and is the only credential that may mint or revoke
others (the one authorization rule that exists before RBAC: without it, a
leaked token could re-issue itself faster than you can revoke it). The
database stores token HASHES only - the value exists once, in the mint
response.

Attribution follows the chain, not the loop: an auto-managed job's launch
carries the job creator's name, a watch's auto-launch the watch creator's,
an autopilot run's spend the run starter's.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.auth import (
    NonceStore,
    PrincipalResolver,
    current_principal,
    hash_token,
    valid_principal_name,
)
from app.lambda_api import MockLambdaClient
from app.main import create_app
from app.orchestrator import Orchestrator
from tests.conftest import make_settings, mock_connect_fn

OWNER = "owner-token-for-tests"


@pytest.fixture
def auth_app(tmp_path, mock_client, mock_storage, mock_sidecar):
    return create_app(
        make_settings(tmp_path, api_token=OWNER),
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        env_path=tmp_path / ".env",
    )


@pytest.fixture
def owner(auth_app):
    with TestClient(auth_app,
                    headers={"Authorization": f"Bearer {OWNER}"}) as c:
        yield c


def mint(owner, name="agent-1"):
    resp = owner.post("/principals", json={"name": name})
    assert resp.status_code == 201
    return resp.json()["token"]


def as_principal(owner, token):
    """A second client over the same app, authenticated as a minted
    principal instead of the owner."""
    c = TestClient(owner.app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


# -- minting and the hash rule ----------------------------------------------


def test_mint_returns_the_value_once_and_stores_only_a_hash(owner):
    token = mint(owner, "ci-runner")
    db = owner.app.state.orchestrator.db
    row = db.principal_by_name("ci-runner")
    assert row["token_hash"] == hash_token(token)
    assert token not in str(row)          # the value is not in the row
    listed = owner.get("/principals").json()["principals"]
    assert listed[0]["name"] == "ci-runner"
    assert "token_hash" not in listed[0]  # hashes never leave the backend


def test_minted_token_authenticates_and_attributes(owner):
    token = mint(owner, "agent-1")
    agent = as_principal(owner, token)

    resp = agent.post("/tasks", json={"template": "gpu-smoke",
                                      "parameters": {}})
    assert resp.status_code == 202
    assert resp.json()["task"]["created_by"] == "agent-1"
    # The audit row names the principal, not a generic client kind.
    db = owner.app.state.orchestrator.db
    actions = {r["action"]: r["actor"] for r in db.list_audit()}
    assert actions["task_enqueue"] == "agent-1"


def test_launch_is_attributed_to_its_principal(owner):
    token = mint(owner, "agent-1")
    agent = as_principal(owner, token)
    resp = agent.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    assert resp.status_code == 202
    assert resp.json()["launch"]["created_by"] == "agent-1"


def test_owner_resolves_without_a_db_row(owner):
    resp = owner.post("/tasks", json={"template": "gpu-smoke",
                                      "parameters": {}})
    assert resp.json()["task"]["created_by"] == "owner"


# -- the owner-only rule ------------------------------------------------------


def test_a_minted_token_cannot_mint_or_revoke(owner):
    token = mint(owner, "agent-1")
    agent = as_principal(owner, token)
    assert agent.post("/principals",
                      json={"name": "sneaky"}).status_code == 403
    assert agent.delete("/principals/agent-1").status_code == 403
    # ...but it can LIST (presence-only data, and the Settings page needs
    # it to render for everyone).
    assert agent.get("/principals").status_code == 200


def test_revoked_token_stops_working_immediately(owner):
    token = mint(owner, "agent-1")
    agent = as_principal(owner, token)
    assert agent.get("/tasks").status_code == 200
    assert owner.delete("/principals/agent-1").status_code == 200
    assert agent.get("/tasks").status_code == 401
    # Revoked, not deleted: the name survives for history.
    db = owner.app.state.orchestrator.db
    assert db.principal_by_name("agent-1")["revoked_at"]
    # Re-revoking is a 409, and the name stays taken.
    assert owner.delete("/principals/agent-1").status_code == 409
    assert owner.post("/principals",
                      json={"name": "agent-1"}).status_code == 409


@pytest.mark.parametrize("bad", ["owner", "backend", "autopilot", "api",
                                 "anonymous", "x", "Has Caps", "sp ace",
                                 "way-too-long-" + "x" * 40])
def test_reserved_and_malformed_names_are_422(owner, bad):
    assert owner.post("/principals", json={"name": bad}).status_code == 422


def test_principal_management_requires_auth_enabled(client):
    """The plain fixture app has no token: nothing to authenticate
    against, so minting is a 409 that says so, not a silent success."""
    resp = client.post("/principals", json={"name": "agent-1"})
    assert resp.status_code == 409
    assert "not enabled" in resp.json()["detail"]
    # Listing still answers (the Settings page renders in open mode).
    assert client.get("/principals").json()["auth_enabled"] is False


# -- resolver unit behavior ---------------------------------------------------


def test_resolver_touch_is_throttled(tmp_path, db):
    clock = {"t": 0.0}
    resolver = PrincipalResolver("", db, clock=lambda: clock["t"])
    db.create_principal(name="a", token_hash=hash_token("tok"),
                        created_by="owner")
    resolver.resolve("tok")
    first = db.principal_by_name("a")["last_used_at"]
    clock["t"] = 30.0                      # inside the throttle window
    resolver.resolve("tok")
    assert db.principal_by_name("a")["last_used_at"] == first
    clock["t"] = 61.0                      # past it
    time.sleep(0.001)                      # utcnow() tick safety
    resolver.resolve("tok")
    assert db.principal_by_name("a")["last_used_at"] >= first


def test_resolver_rejects_unknown_and_revoked(tmp_path, db):
    resolver = PrincipalResolver("env-token", db)
    assert resolver.resolve("env-token") == ("owner", "admin")
    assert resolver.resolve("nope") is None
    db.create_principal(name="a", token_hash=hash_token("tok"),
                        created_by="owner")
    assert resolver.resolve("tok") == ("a", "operator")
    db.revoke_principal("a")
    assert resolver.resolve("tok") is None


def test_nonce_carries_its_minting_principal():
    store = NonceStore(ttl_seconds=60.0, clock=lambda: 0.0)
    nonce = store.mint("agent-1")
    assert store.redeem(nonce) == "agent-1"
    assert store.redeem(nonce) is None     # single use


def test_valid_principal_names():
    assert valid_principal_name("claude-mcp")
    assert valid_principal_name("ci2")
    assert not valid_principal_name("owner")
    assert not valid_principal_name("-leading")


def test_current_principal_falls_back_to_api():
    assert current_principal() == "api"


# -- chain attribution --------------------------------------------------------


async def test_auto_manage_launch_carries_the_job_creator(tmp_path, db):
    """The lifecycle loop launches, but the launch row belongs to whoever
    enqueued the job - the loop is plumbing, not a principal."""
    from app.dispatcher import Dispatcher
    from app.task_queue import SQLiteTaskQueue

    settings = make_settings(tmp_path)
    orch = Orchestrator(settings, MockLambdaClient(), db,
                        connect_fn=mock_connect_fn)
    queue = SQLiteTaskQueue(db)
    d = Dispatcher(settings, orch, queue, {}, db, MockLambdaClient())
    job_id = queue.enqueue(
        template="gpu-smoke", parameters={}, auto_manage=True,
        gpu_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", created_by="agent-1")
    await d._auto_launch(queue.get(job_id))

    job = queue.get(job_id)
    assert job["launch_id"], "auto-launch did not attach a launch"
    assert db.get_launch(job["launch_id"])["created_by"] == "agent-1"


def test_pre_attribution_rows_read_as_null(owner):
    """Historical rows (and anything created without attribution) carry
    NULL, which the UI shows as nothing - never guessed, never 'owner'."""
    db = owner.app.state.orchestrator.db
    lid = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129)
    assert db.get_launch(lid)["created_by"] is None
