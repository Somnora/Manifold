"""Local API-token authentication (Phase 78).

The backend binds to loopback, but loopback is not authorization: any
process or browser tab on this machine can reach :8000, and what it finds
there launches paid GPUs, terminates instances, and writes secrets to
.env. So real mode requires a bearer token on every request. The token is
generated on first boot and lives in .env next to the other secrets.

Enforcement is CONDITIONAL on a token being configured. Mock mode and the
test harness leave it empty and stay a zero-credential, fully open demo;
create_app's real path generates one when missing, so a production
backend is never silently open.

main.py stays routes-only: verification, the ASGI middleware, the exempt
list, and the download-nonce store all live here.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs

from .config import update_env_file

logger = logging.getLogger("manifold.auth")


def token_matches(candidate: str, expected: str) -> bool:
    """Constant-time comparison. A plain == short-circuits on the first
    wrong byte, which leaks the token one byte at a time through response
    timing to anything that can hammer the port."""
    return hmac.compare_digest(candidate.encode(), expected.encode())


def ensure_api_token(env_file: Path) -> str:
    """First real-mode boot: generate an API token and persist it to .env.

    Returns the token. If it cannot be persisted this REFUSES TO BOOT
    (SystemExit): a production backend must never silently serve open, and
    a token held only in memory would strand every configured client on
    the next restart. The .env path is logged; the value never is.
    """
    token = secrets.token_urlsafe(32)
    try:
        creating = not env_file.exists()
        update_env_file(env_file, {"MANIFOLD_API_TOKEN": token})
        if creating:
            # Owner-only: this file now holds the API token (and later the
            # Lambda key, once the Settings page writes it). Only when WE
            # create the file - an existing .env's permissions are the
            # user's own business.
            os.chmod(env_file, 0o600)
    except OSError as exc:
        raise SystemExit(
            f"manifold: could not save the generated API token to "
            f"{env_file} ({exc}). Refusing to start unauthenticated in "
            f"real mode; fix the file or set MANIFOLD_API_TOKEN in the "
            f"environment."
        ) from exc
    # Mirror what load_dotenv does on every LATER boot, so first-boot code
    # that reads the environment (claude_integration's mcp.json emitter)
    # sees the token now instead of after the next restart.
    os.environ["MANIFOLD_API_TOKEN"] = token
    # warning, not info: uvicorn's default log config drops app-level INFO,
    # and this one-time breadcrumb is how a headless install learns where
    # its credential landed. The value itself is never logged.
    logger.warning(
        "generated MANIFOLD_API_TOKEN and saved it to %s (value not logged)",
        env_file,
    )
    return token


# -- exempt paths ----------------------------------------------------------

# The dashboard's page routes. These (and only their static-export
# spellings, derived below) are reachable without a token: the HTML shell
# must be public so the TokenGate can render and ask for the token. Every
# piece of DATA the pages show still comes from the guarded API routes.
PAGE_PATHS = (
    "/",
    "/agents",
    "/autopilot",
    "/history",
    "/hub",
    "/jobs",
    "/settings",
    "/storage",
)


def _exact_exemptions() -> frozenset[str]:
    # Exact paths, default deny. Deliberately NOT prefixes: "/settings"
    # being exempt must never exempt /settings/lambda-key (which writes
    # secrets), nor "/storage" exempt /storage/files.
    exact = {"/health", "/icon.svg"}
    for page in PAGE_PATHS:
        stem = "index" if page == "/" else page.lstrip("/")
        exact.add(page)
        # The static export serves each page three ways: the route itself
        # (ExportedUI falls back to <page>.html), the .html file directly,
        # and a <page>.txt payload Next fetches on client-side navigation.
        exact.add(f"/{stem}.html")
        exact.add(f"/{stem}.txt")
        if page != "/":
            exact.add(page + "/")
    return frozenset(exact)


EXEMPT_EXACT = _exact_exemptions()

# /_next/ is the export's build assets (hashed JS/CSS) - all static files.
# /v1/ is NOT unauthenticated: the OpenAI proxy runs its own scheme (the
# dedicated MANIFOLD_PROXY_KEY, else the api token) with OpenAI-shaped
# errors that SDK clients can parse, so the global middleware stays out of
# its way. See _proxy_auth_ok in main.py.
EXEMPT_PREFIXES = ("/_next/", "/v1/")


def is_exempt_path(path: str) -> bool:
    """Is this HTTP path reachable without a token? Default deny."""
    if path in EXEMPT_EXACT:
        return True
    if any(path.startswith(prefix) for prefix in EXEMPT_PREFIXES):
        return True
    # Next's segment-cache payloads for the exempt pages: /__next.*.txt at
    # the root and /<page>/__next.*.txt under a page directory. Matched
    # narrowly (the basename spelling AND an exempt page as parent) so no
    # API route can ever collide with the shape.
    parent, _, name = path.rpartition("/")
    if name.startswith("__next.") and name.endswith(".txt"):
        return parent == "" or parent in PAGE_PATHS
    return False


# The two browser-download escapes. An <a href>/location navigation cannot
# carry the Authorization header, so exactly these GETs may authenticate
# with a single-use ?nonce= instead (minted by POST /downloads/token,
# which requires the normal auth). GET only, these two paths only: the
# nonce is a download credential, not a general query-token scheme.
_DOWNLOAD_PATH = re.compile(r"^/instances/[^/]+/files/(download|archive)$")


def is_nonce_path(method: str, path: str) -> bool:
    return method == "GET" and _DOWNLOAD_PATH.match(path) is not None


class NonceStore:
    """Single-use, short-lived download credentials.

    Why not the API token in the query string: uvicorn's access log
    records the request line, so a long-lived secret in a query string is
    a secret written to disk on every download. A nonce is worthless
    seconds later and dies on first use. In-memory on purpose (single
    process); a restart invalidating pending nonces costs one extra click.
    Not bound to a path this phase - boring first (see DECISIONS.md).
    """

    def __init__(self, ttl_seconds: float = 60.0, clock=time.monotonic):
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._expiry: dict[str, float] = {}

    def mint(self) -> str:
        self._prune()
        nonce = secrets.token_urlsafe(16)
        self._expiry[nonce] = self._clock() + self.ttl_seconds
        return nonce

    def redeem(self, nonce: str) -> bool:
        """True exactly once per minted, unexpired nonce."""
        self._prune()
        return self._expiry.pop(nonce, None) is not None

    def _prune(self) -> None:
        now = self._clock()
        for nonce in [n for n, exp in self._expiry.items() if exp <= now]:
            del self._expiry[nonce]


class TokenAuthMiddleware:
    """Require `Authorization: Bearer <api_token>` on every HTTP and
    WebSocket request that is not on the exempt list.

    Pure ASGI, not BaseHTTPMiddleware: BaseHTTP only sees "http" scopes
    (WebSockets would sail past it unauthenticated) and shims every
    response through a buffering wrapper that this gateway's SSE and
    download streams do not want.

    Installed INSIDE CORS (added before it in source order; Starlette's
    add_middleware prepends, so last-added CORS is outermost). The order
    matters twice: the browser preflight OPTIONS never carries
    Authorization, so CORS must answer it before this middleware can 401
    it; and a 401 must pass back OUT through CORS to gain
    Access-Control-Allow-Origin, or the :3000 dev dashboard reads it as a
    network error (status 0) and the token gate never shows.
    """

    def __init__(self, app, token: str, nonces: NonceStore,
                 env_path: str = ""):
        self.app = app
        self._token = token
        self._nonces = nonces
        self._env_path = env_path

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if (is_exempt_path(scope["path"]) or self._header_ok(scope)
                    or self._nonce_ok(scope)):
                await self.app(scope, receive, send)
                return
            await self._reject_http(send)
            return
        if scope["type"] == "websocket":
            # No exempt WebSocket routes. Header for non-browser clients;
            # ?token= because a browser WebSocket cannot set headers.
            if self._header_ok(scope) or self._query_token_ok(scope):
                await self.app(scope, receive, send)
                return
            await self._reject_websocket(receive, send)
            return
        await self.app(scope, receive, send)      # lifespan et al.

    def _header_ok(self, scope) -> bool:
        header = ""
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                header = value.decode("latin-1")
                break
        if not header.lower().startswith("bearer "):
            return False
        return token_matches(header[7:], self._token)

    def _query_token_ok(self, scope) -> bool:
        query = (scope.get("query_string") or b"").decode("latin-1")
        return any(token_matches(candidate, self._token)
                   for candidate in parse_qs(query).get("token", []))

    def _nonce_ok(self, scope) -> bool:
        if not is_nonce_path(scope.get("method", ""), scope["path"]):
            return False
        query = (scope.get("query_string") or b"").decode("latin-1")
        return any(self._nonces.redeem(candidate)
                   for candidate in parse_qs(query).get("nonce", []))

    async def _reject_http(self, send) -> None:
        # Points at where the token LIVES, never what it is.
        body = json.dumps({
            "detail": (
                "Missing or invalid API token. Send 'Authorization: "
                "Bearer <token>'; the token is the MANIFOLD_API_TOKEN "
                f"line in Manifold's .env ({self._env_path})."
            )
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b"Bearer"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def _reject_websocket(self, receive, send) -> None:
        # Accept, THEN close with an application code. A pre-accept denial
        # surfaces in browser JS as an opaque handshake failure (a 403 the
        # page cannot inspect); accept-then-close(4401) exposes close.code
        # to any client that wants it. The dashboard deliberately does NOT
        # key on 4401 (its handlers treat every failure generically), so
        # the code is a courtesy to non-browser clients, not a contract.
        await receive()                    # the websocket.connect event
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 4401})
