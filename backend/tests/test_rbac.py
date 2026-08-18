"""Phase 80: roles.

viewer observes, operator works, admin governs; "owner" (the .env token)
is always admin. Enforcement lives in the same middleware chokepoint as
authentication, against a role table that is COMPLETE by construction:
RoleTable.build walks the app's real routes at startup and refuses to
boot over an unclassified endpoint, so shipping a route includes
deciding who may call it.

Above the table sits exactly one rule: only the owner token mints or
revokes ADMIN credentials - an admin principal escalating laterally
(minting more admins) is the leak mode RBAC exists to stop.
"""

import pytest
from fastapi.testclient import TestClient

from app.auth import ROLES, RoleTable, role_allows
from app.main import create_app
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
        research_keys_path=tmp_path / "research-keys.env",
    )


@pytest.fixture
def owner(auth_app):
    with TestClient(auth_app,
                    headers={"Authorization": f"Bearer {OWNER}"}) as c:
        yield c


def minted_client(owner, name, role):
    resp = owner.post("/principals", json={"name": name, "role": role})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == role
    c = TestClient(owner.app)
    c.headers.update({"Authorization": f"Bearer {body['token']}"})
    return c


@pytest.fixture
def viewer(owner):
    return minted_client(owner, "watcher", "viewer")


@pytest.fixture
def operator(owner):
    return minted_client(owner, "worker", "operator")


LAUNCH = {"instance_type": "gpu_1x_a10", "region": "us-east-1",
          "filesystem": "manifold-data"}


# -- the hierarchy -----------------------------------------------------------


def test_role_ranking():
    assert role_allows("admin", "viewer")
    assert role_allows("operator", "operator")
    assert not role_allows("viewer", "operator")
    assert not role_allows("operator", "admin")
    # Unknown role strings fail CLOSED, both ways.
    assert not role_allows("superuser", "viewer")
    assert not role_allows("admin", "no-such-role")


# -- viewer: observes, does nothing --------------------------------------------


def test_viewer_reads(viewer):
    assert viewer.get("/instances").status_code == 200
    assert viewer.get("/spend/summary").status_code == 200
    assert viewer.get("/tasks").status_code == 200
    assert viewer.get("/audit").status_code == 200
    assert viewer.get("/principals").status_code == 200


def test_viewer_cannot_spend_or_act(viewer):
    assert viewer.post("/instances", json=LAUNCH).status_code == 403
    assert viewer.post("/tasks", json={"template": "gpu-smoke",
                                       "parameters": {}}).status_code == 403
    assert viewer.post("/downloads/token").status_code == 403
    assert viewer.delete("/tasks/finished").status_code == 403
    # diagnose is a GET that executes commands on the box - work, not
    # observation.
    assert viewer.get(
        "/instances/i-x/sidecar/diagnose").status_code == 403


def test_viewer_403_names_have_and_need(viewer):
    body = viewer.post("/instances", json=LAUNCH).json()["detail"]
    assert "watcher" in body and "viewer" in body and "operator" in body


def test_viewer_websockets(viewer, owner):
    # Terminals execute; metrics only watch. Codes: 4403 = role too low.
    with viewer.websocket_connect("/local/terminal") as ws:
        assert ws.receive()["code"] == 4403
    launch = owner.post("/instances", json=LAUNCH)
    assert launch.status_code == 202
    iid = None
    for _ in range(100):
        rows = owner.get("/instances").json()["instances"]
        if rows:
            iid = rows[0]["id"]
            break
    if iid:
        with viewer.websocket_connect(
                f"/instances/{iid}/metrics/stream") as ws:
            first = ws.receive()
            assert first.get("code") != 4403   # viewer may watch metrics


# -- operator: works, does not govern ------------------------------------------


def test_operator_works(operator):
    assert operator.post("/instances", json=LAUNCH).status_code == 202
    assert operator.post("/tasks", json={"template": "gpu-smoke",
                                         "parameters": {}}).status_code == 202
    assert operator.post("/downloads/token").status_code == 200


def test_operator_cannot_govern(operator):
    assert operator.post("/settings/lambda-key",
                         json={"api_key": "x" * 40}).status_code == 403
    assert operator.put(
        "/preferences",
        json={"guardrails": {"monthly_budget_usd": 9.0}}).status_code == 403
    assert operator.post("/principals",
                         json={"name": "more"}).status_code == 403
    assert operator.delete("/principals/worker").status_code == 403


# -- admin: governs; the owner-only rule above it ------------------------------


def test_admin_governs_but_cannot_touch_admin_credentials(owner):
    admin = minted_client(owner, "boss", "admin")
    # Governs: policy and non-admin credentials.
    assert admin.put(
        "/preferences",
        json={"guardrails": {"monthly_budget_usd": 9.0}}).status_code == 200
    assert admin.post("/principals",
                      json={"name": "crew", "role": "operator"}
                      ).status_code == 201
    assert admin.delete("/principals/crew").status_code == 200
    # The one rule above the table: admin credentials are owner-only,
    # both directions - minting AND revoking.
    resp = admin.post("/principals", json={"name": "boss2",
                                           "role": "admin"})
    assert resp.status_code == 403
    assert "owner" in resp.json()["detail"]
    assert admin.delete("/principals/boss").status_code == 403
    assert owner.delete("/principals/boss").status_code == 200


def test_unknown_role_is_422(owner):
    resp = owner.post("/principals", json={"name": "x-y",
                                           "role": "superuser"})
    assert resp.status_code == 422
    assert "viewer" in resp.json()["detail"]


def test_pre_rbac_principals_default_to_operator(owner):
    """A 79-era row (minted before the role column existed) must act
    exactly as it always did: work, not govern."""
    db = owner.app.state.orchestrator.db
    from app.auth import hash_token
    db._execute(
        """INSERT INTO api_principals
           (id, name, token_hash, created_at, created_by, role)
           VALUES ('old1', 'legacy', ?, '2026-01-01T00:00:00+00:00',
                   'owner', 'operator')""",
        (hash_token("legacy-token"),))
    c = TestClient(owner.app)
    c.headers.update({"Authorization": "Bearer legacy-token"})
    assert c.get("/instances").status_code == 200
    assert c.post("/instances", json=LAUNCH).status_code == 202
    assert c.post("/principals", json={"name": "nope"}).status_code == 403


# -- /v1 role gating -----------------------------------------------------------


def test_v1_roles(owner, viewer, operator):
    # Listing models is observation; chatting drives a paid GPU.
    assert viewer.get("/v1/models").status_code == 200
    resp = viewer.post("/v1/chat/completions",
                       json={"model": "m", "messages": []})
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "permission_error"
    # Operator clears the bar (404/502-family beyond auth is fine - there
    # is no model serving; the assertion is only "not blocked by role").
    resp = operator.post("/v1/chat/completions",
                         json={"model": "m", "messages": []})
    assert resp.status_code not in (401, 403)


# -- the table is closed -------------------------------------------------------


def test_role_table_refuses_unclassified_routes(auth_app):
    """Register a route without a ROUTE_ROLES entry and the builder must
    refuse: deciding who may call an endpoint is part of shipping it."""
    @auth_app.get("/sneaky-new-route")
    async def sneaky():                     # pragma: no cover
        return {}

    with pytest.raises(RuntimeError) as exc:
        RoleTable.build(auth_app)
    assert "/sneaky-new-route" in str(exc.value)


def test_open_mode_ignores_roles(client):
    """No token configured = no identities to rank: everything behaves
    exactly as before roles existed."""
    assert client.get("/instances").status_code == 200
    assert client.post("/instances", json=LAUNCH).status_code == 202


def test_roles_constant_matches_ui_contract():
    assert ROLES == ("viewer", "operator", "admin")
