"""Phase 78: the local API-token layer.

Auth lives in one place (app/auth.py, plus the /v1 scheme in main.py);
these tests pin its contract: conditional enforcement, the exact exempt
list, nonce-authenticated downloads, the /v1 dual credential, generation
only on production wiring, and - the drift guard - that every route an
app exposes is default-deny.
"""

import inspect
import re

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth import NonceStore, is_exempt_path
from app.image_checker import MockImageChecker
from app.main import create_app
from tests.conftest import make_settings, mock_connect_fn

TOKEN = "test-api-token-abc123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def auth_app(tmp_path, mock_client, mock_storage, mock_sidecar, **overrides):
    """The standard test wiring with a token SET, so the middleware runs."""
    settings = make_settings(tmp_path, api_token=TOKEN, **overrides)
    return create_app(
        settings,
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        image_checker=MockImageChecker(),
        env_path=tmp_path / ".env",
        custom_templates_dir=tmp_path / "custom-templates",
    )


@pytest.fixture
def auth_client(tmp_path, mock_client, mock_storage, mock_sidecar):
    app = auth_app(tmp_path, mock_client, mock_storage, mock_sidecar)
    with TestClient(app) as client:
        yield client


# -- enforcement on a spend route ----------------------------------------------------


def test_spend_route_requires_token(auth_client, mock_client):
    body = {"instance_type": "gpu_1x_a10", "region": "us-east-1",
            "filesystem": "manifold-data"}
    assert auth_client.post("/instances", json=body).status_code == 401
    assert auth_client.post(
        "/instances", json=body,
        headers={"Authorization": "Bearer wrong-token"}).status_code == 401
    # The 401 happened BEFORE the orchestrator: nothing reached the cloud.
    assert mock_client.launch_calls == []
    # The right token goes through to the real launch pipeline.
    assert auth_client.post(
        "/instances", json=body, headers=AUTH).status_code == 202


def test_401_detail_points_at_env_not_value(auth_client):
    resp = auth_client.get("/instances")
    assert resp.status_code == 401
    assert "MANIFOLD_API_TOKEN" in resp.json()["detail"]
    assert ".env" in resp.json()["detail"]
    assert TOKEN not in resp.text


def test_token_comparison_is_constant_time():
    """compare_digest, asserted via source: behavior cannot distinguish a
    constant-time comparison from ==, and this is the one property that
    silently degrades if someone 'simplifies' the helper."""
    import app.auth as auth_mod
    import app.main as main_mod
    assert "compare_digest" in inspect.getsource(auth_mod.token_matches)
    # Both the middleware and the /v1 proxy route through the helper.
    assert "token_matches" in inspect.getsource(main_mod)


# -- websockets ----------------------------------------------------------------------


def test_websocket_refused_without_credentials(auth_client):
    # Accept-then-close(4401), not a pre-accept denial: a browser can read
    # close.code but sees a handshake 403 as an opaque network error.
    with auth_client.websocket_connect("/instances/i-x/terminal") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 4401


def test_websocket_accepts_authorization_header(auth_client):
    with auth_client.websocket_connect("/instances/i-x/terminal",
                                       headers=AUTH) as ws:
        # Reached the route: it answers (no SSH connection here) and closes.
        assert "no SSH connection" in ws.receive_text()


def test_websocket_accepts_query_token(auth_client):
    # Browser WebSockets cannot set headers; ?token= is their spelling.
    with auth_client.websocket_connect(
            f"/instances/i-x/terminal?token={TOKEN}") as ws:
        assert "no SSH connection" in ws.receive_text()


# -- the exempt list -----------------------------------------------------------------


def test_exempt_list_is_exact_not_prefix():
    # The two collisions that motivated exact matching: the /settings and
    # /storage PAGES are open, the API routes under them are not.
    assert is_exempt_path("/settings")
    assert not is_exempt_path("/settings/lambda-key")
    assert is_exempt_path("/storage")
    assert not is_exempt_path("/storage/files")
    assert is_exempt_path("/")
    assert not is_exempt_path("/instances")
    assert is_exempt_path("/_next/static/chunks/app.js")
    # Next's client-navigation payloads, narrowly: page parents only.
    assert is_exempt_path("/jobs/__next.jobs.txt")
    assert is_exempt_path("/__next._tree.txt")
    assert not is_exempt_path("/instances/__next.sneaky.txt")


def test_exempt_paths_open_without_token(auth_client):
    assert auth_client.get("/health").status_code == 200
    # Page routes and static assets: never 401 (the exact status depends on
    # whether the exported dashboard is present, which is not this test's
    # business - the middleware letting them through is).
    for path in ("/", "/agents", "/autopilot", "/history", "/hub", "/jobs",
                 "/settings", "/storage", "/icon.svg",
                 "/_next/static/anything.js"):
        assert auth_client.get(path).status_code != 401, path


def test_secret_writing_routes_under_page_paths_still_401(auth_client):
    assert auth_client.post("/settings/lambda-key",
                            json={"api_key": "x" * 12}).status_code == 401
    assert auth_client.get(
        "/storage/files?filesystem=manifold-data").status_code == 401


# -- CORS interplay ------------------------------------------------------------------


def test_preflight_options_passes_unauthenticated(auth_client):
    # A browser preflight never carries Authorization; CORS (outermost)
    # must answer it before auth can 401 it.
    resp = auth_client.options("/instances", headers={
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "POST",
    })
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == \
        "http://localhost:3000"


def test_401_is_readable_cross_origin(auth_client):
    # Without Access-Control-Allow-Origin on the 401, the :3000 dashboard
    # sees status 0 and can never show the token gate.
    resp = auth_client.get("/instances", headers={
        "Origin": "http://localhost:3000",
        "Authorization": "Bearer wrong-token",
    })
    assert resp.status_code == 401
    assert resp.headers["access-control-allow-origin"] == \
        "http://localhost:3000"


# -- the drift guard -----------------------------------------------------------------

# Every HTTP API route that may answer without credentials, pinned. Adding
# a route to this set is a deliberate security decision, not a default.
EXEMPT_HTTP_ROUTES = {"/health"}


def test_every_route_requires_auth(auth_client):
    """Iterate the app's real route table: every non-exempt HTTP route
    401s without credentials and every WebSocket route refuses. A new
    route added without thinking about auth fails here, not in the field."""
    from fastapi.routing import APIRoute, APIWebSocketRoute
    from starlette.routing import Mount, Route

    checked = 0
    for route in auth_client.app.routes:
        if isinstance(route, Mount):
            continue    # the static UI mount; covered by the exempt tests
        path = re.sub(r"{[^}]+}", "x", route.path)
        if isinstance(route, APIWebSocketRoute):
            with auth_client.websocket_connect(path) as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
            assert exc.value.code == 4401, route.path
            checked += 1
            continue
        if not isinstance(route, (APIRoute, Route)):
            continue
        if route.path in EXEMPT_HTTP_ROUTES:
            assert auth_client.get(path).status_code == 200
            continue
        method = next(m for m in sorted(route.methods or {"GET"})
                      if m != "HEAD")
        if path.startswith("/v1/"):
            # The proxy has its own credential scheme (tested below) and
            # its own error envelope; still a 401 without one here because
            # the app holds an api_token.
            assert auth_client.request(method, path).status_code == 401, \
                route.path
            checked += 1
            continue
        resp = auth_client.request(method, path)
        assert resp.status_code == 401, (
            f"{method} {route.path} answered {resp.status_code} without "
            f"credentials; every new route must authenticate (or be pinned "
            f"in EXEMPT_HTTP_ROUTES here, deliberately)")
        checked += 1
    # Sanity: the walk really visited the API surface.
    assert checked > 40


# -- download nonces -----------------------------------------------------------------


def test_download_nonce_flow(auth_client):
    # Minting requires the normal auth.
    assert auth_client.post("/downloads/token").status_code == 401
    minted = auth_client.post("/downloads/token", headers=AUTH)
    assert minted.status_code == 200
    nonce = minted.json()["nonce"]

    url = ("/instances/i-x/files/download"
           f"?path=/workspace/ephemeral/out.txt&nonce={nonce}")
    # First use passes auth and fails INSIDE the route (no connected
    # instance): 409-not-401 is the proof the middleware let it through.
    assert auth_client.get(url).status_code == 409
    # Second use: spent.
    assert auth_client.get(url).status_code == 401


def test_nonce_is_not_general_query_auth(auth_client):
    nonce = auth_client.post(
        "/downloads/token", headers=AUTH).json()["nonce"]
    # A nonce opens the two download GETs only, never an API route...
    assert auth_client.get(f"/instances?nonce={nonce}").status_code == 401
    # ...and ?token= is a WebSocket affordance, not an HTTP one: the master
    # token must never work from a query string (access logs record it).
    assert auth_client.get(f"/instances?token={TOKEN}").status_code == 401


def test_nonce_store_ttl_and_single_use():
    # Since Phase 79 redeem() returns the minting principal (truthy) or
    # None, so the nonce attributes the download it authorizes.
    now = [0.0]
    store = NonceStore(ttl_seconds=60.0, clock=lambda: now[0])
    stale = store.mint()
    now[0] = 61.0
    assert store.redeem(stale) is None         # expired unused
    fresh = store.mint("someone")
    assert store.redeem(fresh) == "someone"    # single use...
    assert store.redeem(fresh) is None         # ...and only once


# -- /v1 dual credential -------------------------------------------------------------


def test_v1_accepts_api_token_when_no_proxy_key(auth_client):
    resp = auth_client.get("/v1/models")
    assert resp.status_code == 401
    # The OpenAI envelope survives: SDK clients parse {"error": {...}}.
    body = resp.json()
    assert body["error"]["type"] == "authentication_error"
    assert "MANIFOLD" in body["error"]["message"]
    assert TOKEN not in resp.text
    assert auth_client.get("/v1/models", headers=AUTH).status_code == 200


def test_v1_proxy_key_wins_over_api_token(tmp_path, mock_client,
                                          mock_storage, mock_sidecar):
    app = auth_app(tmp_path, mock_client, mock_storage, mock_sidecar,
                   proxy_api_key="proxy-secret")
    with TestClient(app) as client:
        ok = client.get("/v1/models",
                        headers={"Authorization": "Bearer proxy-secret"})
        assert ok.status_code == 200
        # The dedicated key is THE /v1 credential once set; the global
        # token does not also open the proxy.
        assert client.get("/v1/models", headers=AUTH).status_code == 401
    # (Open-when-neither is pinned by test_openai_proxy.py's
    # test_proxy_open_when_no_key, unchanged.)


# -- generation is production-only ---------------------------------------------------


def test_harness_app_with_empty_token_writes_nothing(
        tmp_path, mock_client, mock_storage, mock_sidecar):
    env = tmp_path / ".env"
    create_app(
        make_settings(tmp_path),
        lambda_client=mock_client,           # injected client = harness
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        image_checker=MockImageChecker(),
        env_path=env,
        custom_templates_dir=tmp_path / "custom-templates",
    )
    assert not env.exists()


def test_mock_mode_stays_open_and_writes_nothing(tmp_path, mock_storage,
                                                 mock_sidecar):
    env = tmp_path / ".env"
    app = create_app(
        make_settings(tmp_path, lambda_api_key=""),
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        env_path=env,
        custom_templates_dir=tmp_path / "custom-templates",
        mock=True,
    )
    with TestClient(app) as client:
        # Fully open: the zero-credential demo, exactly as before.
        assert client.get("/instances").status_code == 200
        assert client.get("/settings/status").json()["auth_required"] is False
    assert not env.exists()


def test_real_mode_generates_persists_and_chmods(tmp_path):
    env = tmp_path / ".env"
    # Real path: no injected client, not mock. NOT entered as a client -
    # the lifespan would dial the real Lambda API.
    app = create_app(make_settings(tmp_path), env_path=env,
                     custom_templates_dir=tmp_path / "custom-templates")
    token = app.state.settings.api_token
    assert token and len(token) >= 32
    assert f"MANIFOLD_API_TOKEN={token}" in env.read_text()
    # Created by us, so owner-only: it holds this token and, later, keys.
    assert (env.stat().st_mode & 0o777) == 0o600


def test_real_mode_respects_preset_token(tmp_path):
    env = tmp_path / ".env"
    app = create_app(make_settings(tmp_path, api_token="preset-from-env"),
                     env_path=env,
                     custom_templates_dir=tmp_path / "custom-templates")
    assert app.state.settings.api_token == "preset-from-env"
    assert not env.exists()                  # nothing generated, no write


def test_real_mode_refuses_boot_when_persist_fails(tmp_path):
    env = tmp_path / ".env"
    env.mkdir()      # a directory where the file should be: unwritable
    with pytest.raises(SystemExit) as exc:
        create_app(make_settings(tmp_path), env_path=env,
                   custom_templates_dir=tmp_path / "custom-templates")
    # Production never silently serves open; the message says what broke.
    assert "API token" in str(exc.value)


# -- status --------------------------------------------------------------------------


def test_status_reports_auth_presence_only(auth_client):
    status = auth_client.get("/settings/status", headers=AUTH).json()
    assert status["auth_required"] is True
    assert TOKEN not in str(status)


def test_status_open_backend_reports_auth_off(client):
    # The shared conftest client has no token: enforcement is conditional.
    assert client.get("/settings/status").json()["auth_required"] is False
