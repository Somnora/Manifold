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

import contextvars
import hashlib
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


def hash_token(value: str) -> str:
    """sha256 hex of a token value - what the api_principals table stores.

    A stolen database row is a fingerprint, not a credential: the token
    itself is 256 bits of token_urlsafe, far beyond offline guessing."""
    return hashlib.sha256(value.encode()).hexdigest()


# -- who is asking (Phase 79) ----------------------------------------------

# The resolved principal for the CURRENT request. Set by the middleware,
# read anywhere downstream (routes, and helpers they call) without
# threading a parameter through every signature. Context propagates into
# FastAPI's threadpool for sync handlers and into asyncio tasks created
# during the request, so a launch pipeline spawned mid-request still knows
# who started it. Background loops (dispatcher, watches) never pass
# through the middleware and keep attributing explicitly.
_current_principal: contextvars.ContextVar[str] = contextvars.ContextVar(
    "manifold_principal", default="")

# Names no principal may take: the built-in actors that appear in the
# audit log with meanings of their own. "owner" is the .env token's name.
RESERVED_PRINCIPALS = frozenset(
    {"owner", "backend", "autopilot", "api", "anonymous"})

_PRINCIPAL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")


def valid_principal_name(name: str) -> bool:
    """Lowercase slug, 2-32 chars: it lands in audit rows, created_by
    columns, and UI chips, so it must be boring and unambiguous."""
    return bool(_PRINCIPAL_NAME.match(name)) and name not in RESERVED_PRINCIPALS


def current_principal() -> str:
    """The principal behind the current request.

    Falls back to "api" when no principal was resolved - an open backend
    (mock mode, harness, no token configured), where every caller is
    equally anonymous and the pre-79 actor name keeps audit history
    consistent."""
    return _current_principal.get() or "api"


def bind_principal(name: str | None) -> None:
    """Bind the principal for a long-lived background task acting on
    someone's behalf (an autopilot run loop). asyncio tasks inherit the
    request context they were created in, but a loop that persists its
    principal (the run row's created_by) and rebinds explicitly does not
    depend on WHERE it was created - which is exactly the property that
    breaks silently otherwise."""
    _current_principal.set(name or "")


# -- roles (Phase 80) -------------------------------------------------------

# Ordered least to most powerful. viewer observes (reads, estimates,
# spend pages); operator works (launches, jobs, terminals, files);
# admin governs (secrets, policy, credentials). "owner" - the .env
# token - is always admin: the bootstrap credential cannot be demoted,
# because demoting the recovery path locks you out of the lock.
ROLES = ("viewer", "operator", "admin")
_RANK = {role: i for i, role in enumerate(ROLES)}

_current_role: contextvars.ContextVar[str] = contextvars.ContextVar(
    "manifold_role", default="")


def current_role() -> str:
    """The role behind the current request. Falls back to "admin" when
    nothing resolved a role: an OPEN backend (no token configured) has no
    identities to rank, and background loops act for the system itself -
    neither may ever be blocked by a role check."""
    return _current_role.get() or "admin"


def role_allows(have: str, needed: str) -> bool:
    """Unknown role strings rank below viewer on purpose: a corrupted or
    future role value fails closed, not open."""
    return _RANK.get(have, -1) >= _RANK.get(needed, len(ROLES))


# Every route's minimum role, keyed by (METHOD, path template); "WS" for
# WebSockets. This table is CLOSED: RoleTable.build() walks the app's
# actual route table at startup and refuses to boot if any route is
# missing here, so a new endpoint cannot ship without someone deciding
# who may call it. The rule of thumb the table encodes:
#   viewer   reads and estimates; mutates nothing, executes nothing
#   operator everything that does work or moves money
#   admin    secrets, policy, and credentials
ROUTE_ROLES: dict[tuple[str, str], str] = {
    # -- open/observe ----------------------------------------------------
    ("GET", "/health"): "viewer",
    ("GET", "/skill"): "viewer",
    ("GET", "/settings/status"): "viewer",
    ("GET", "/preferences"): "viewer",
    ("GET", "/notifications"): "viewer",
    ("GET", "/agent/context/{session_id}"): "viewer",
    ("GET", "/instance-types"): "viewer",
    ("GET", "/launch-options"): "viewer",
    ("GET", "/regions"): "viewer",
    ("GET", "/ssh-keys"): "viewer",
    ("GET", "/instances"): "viewer",
    ("GET", "/instances/{instance_id}/metrics"): "viewer",
    ("GET", "/instances/{instance_id}/model"): "viewer",
    ("GET", "/instances/{instance_id}/files/list"): "viewer",
    ("GET", "/instances/{instance_id}/files/usage"): "viewer",
    ("GET", "/instances/{instance_id}/files/recent"): "viewer",
    ("WS", "/instances/{instance_id}/metrics/stream"): "viewer",
    ("GET", "/templates"): "viewer",
    ("GET", "/tasks"): "viewer",
    ("GET", "/tasks/{task_id}"): "viewer",
    ("GET", "/tasks/{task_id}/logs"): "viewer",
    ("GET", "/tasks/{task_id}/logs/stream"): "viewer",
    ("GET", "/tasks/{task_id}/events"): "viewer",
    ("GET", "/tasks/{task_id}/events/stream"): "viewer",
    ("GET", "/subagents/models"): "viewer",
    ("GET", "/subagents/swarm/status"): "viewer",
    ("GET", "/model-presets"): "viewer",
    ("GET", "/watches"): "viewer",
    ("GET", "/brains"): "viewer",
    ("GET", "/approvals/pending"): "viewer",
    ("GET", "/autopilot/approvals"): "viewer",
    ("GET", "/autopilot/runs"): "viewer",
    ("GET", "/autopilot/runs/{run_id}"): "viewer",
    ("GET", "/audit"): "viewer",
    ("GET", "/project-brief"): "viewer",
    ("GET", "/worklog"): "viewer",
    ("GET", "/launches"): "viewer",
    ("GET", "/launches/{launch_id}"): "viewer",
    ("GET", "/launches/{launch_id}/wait"): "viewer",
    ("GET", "/launches/{launch_id}/utilization"): "viewer",
    ("GET", "/estimate"): "viewer",
    ("GET", "/estimate/model-fit"): "viewer",
    ("GET", "/spend/summary"): "viewer",
    ("GET", "/spend/series"): "viewer",
    ("GET", "/spend/breakdown"): "viewer",
    ("GET", "/clusters"): "viewer",
    ("GET", "/clusters/{cluster_id}"): "viewer",
    ("GET", "/filesystems"): "viewer",
    ("GET", "/storage/files"): "viewer",
    ("GET", "/principals"): "viewer",
    ("GET", "/policy"): "viewer",
    ("GET", "/v1/models"): "viewer",
    # -- work ------------------------------------------------------------
    ("POST", "/instances"): "operator",
    ("POST", "/instances/{instance_id}/idle-timeout"): "operator",
    ("POST", "/instances/{instance_id}/max-lifetime"): "operator",
    ("POST", "/instances/{instance_id}/keep-alive"): "operator",
    ("POST", "/instances/{instance_id}/name"): "operator",
    ("DELETE", "/instances/{instance_id}"): "operator",
    ("POST", "/instances/{instance_id}/sync"): "operator",
    ("POST", "/instances/{instance_id}/rescue"): "operator",
    ("POST", "/instances/{instance_id}/ide-attach"): "operator",
    ("POST", "/instances/{instance_id}/run"): "operator",
    ("POST", "/instances/{instance_id}/chat"): "operator",
    ("WS", "/instances/{instance_id}/terminal"): "operator",
    ("WS", "/local/terminal"): "operator",
    ("POST", "/downloads/token"): "operator",
    ("POST", "/instances/{instance_id}/files/upload"): "operator",
    ("GET", "/instances/{instance_id}/files/download"): "operator",
    ("GET", "/instances/{instance_id}/files/archive"): "operator",
    ("DELETE", "/instances/{instance_id}/files"): "operator",
    # diagnose is a GET that RUNS commands on the instance - work.
    ("GET", "/instances/{instance_id}/sidecar/diagnose"): "operator",
    ("POST", "/templates/custom"): "operator",
    ("DELETE", "/templates/custom/{name}"): "operator",
    ("POST", "/templates/{name}/render"): "operator",
    ("POST", "/tasks"): "operator",
    ("POST", "/tasks/{task_id}/cancel"): "operator",
    ("DELETE", "/tasks/finished"): "operator",
    ("DELETE", "/tasks/{task_id}"): "operator",
    ("POST", "/subagents/dispatch"): "operator",
    ("POST", "/watches"): "operator",
    ("DELETE", "/watches/{watch_id}"): "operator",
    ("POST", "/autopilot/runs"): "operator",
    ("POST", "/autopilot/runs/{run_id}/cancel"): "operator",
    # Approving a gated action IS the spend decision.
    ("POST", "/approvals/{approval_id}"): "operator",
    ("POST", "/autopilot/approvals/{approval_id}"): "operator",
    ("POST", "/audit/agent"): "operator",
    ("PUT", "/project-brief"): "operator",
    ("POST", "/notifications/read"): "operator",
    ("DELETE", "/notifications"): "operator",
    ("POST", "/agent/handshake"): "operator",
    ("POST", "/agent/context/{session_id}/update"): "operator",
    ("POST", "/clusters/launch"): "operator",
    ("POST", "/clusters/{cluster_id}/terminate"): "operator",
    ("POST", "/filesystems"): "operator",
    ("DELETE", "/filesystems/{name}"): "operator",
    ("DELETE", "/storage/files/{key:path}"): "operator",
    ("POST", "/v1/chat/completions"): "operator",
    # -- govern ----------------------------------------------------------
    ("POST", "/settings/lambda-key"): "admin",
    ("POST", "/settings/gcp-config"): "admin",
    ("POST", "/settings/s3-keys"): "admin",
    ("PUT", "/preferences"): "admin",
    ("POST", "/principals"): "admin",
    ("DELETE", "/principals/{name}"): "admin",
}


class RoleTable:
    """The classification above, compiled against the app's REAL routes.

    build() walks app.routes and pairs each endpoint with its entry,
    reusing Starlette's own compiled path_regex so middleware-time
    matching agrees with the router. Any route without an entry aborts
    startup: default-deny for the table itself, in the same spirit as the
    exempt-path list."""

    def __init__(self, entries):
        self._entries = entries      # [(compiled_regex, method, role)]

    @classmethod
    def build(cls, app) -> "RoleTable":
        from fastapi.routing import APIRoute
        from starlette.routing import WebSocketRoute

        entries, missing = [], []
        for route in app.routes:
            if isinstance(route, APIRoute):
                for method in route.methods - {"HEAD", "OPTIONS"}:
                    role = ROUTE_ROLES.get((method, route.path))
                    if role is None:
                        missing.append(f"{method} {route.path}")
                    else:
                        entries.append((route.path_regex, method, role))
            elif isinstance(route, WebSocketRoute):
                role = ROUTE_ROLES.get(("WS", route.path))
                if role is None:
                    missing.append(f"WS {route.path}")
                else:
                    entries.append((route.path_regex, "WS", role))
            # Mounts (the static dashboard) carry no role: their paths are
            # either exempt or 404 downstream.
        if missing:
            raise RuntimeError(
                "routes with no role classification (add them to "
                "auth.ROUTE_ROLES - deciding who may call an endpoint is "
                "part of shipping it): " + ", ".join(sorted(missing)))
        return cls(entries)

    def role_for(self, path: str, method: str) -> str | None:
        """Minimum role for a concrete request path, or None when no route
        matches (the router will 404 it anyway)."""
        for regex, entry_method, role in self._entries:
            if entry_method == method and regex.match(path):
                return role
        return None


class PrincipalResolver:
    """Token value -> principal name.

    The .env token resolves to "owner" (constant-time compare; it is the
    bootstrap credential and cannot be revoked through the API - revoking
    the recovery path locks you out of the lock). Everything else is a
    sha256 lookup in api_principals; revoked rows resolve to nothing.

    last_used_at is throttled to one write per principal per minute:
    the dashboard polls every few seconds, and a timestamp whose purpose
    is "is this credential dead or alive" does not need 20 writes a
    minute to answer that."""

    TOUCH_SECONDS = 60.0

    def __init__(self, owner_token: str, db, *, clock=time.monotonic):
        self._owner_token = owner_token
        self._db = db
        self._clock = clock
        self._last_touch: dict[str, float] = {}

    def resolve(self, candidate: str) -> tuple[str, str] | None:
        """(name, role) for a valid token, else None. Owner is always
        admin (Phase 80); stored rows carry their minted role."""
        if self._owner_token and token_matches(candidate, self._owner_token):
            return ("owner", "admin")
        row = self._db.principal_by_hash(hash_token(candidate))
        if row is None or row["revoked_at"]:
            return None
        self._touch(row["name"])
        return (row["name"], row.get("role") or "operator")

    def _touch(self, name: str) -> None:
        now = self._clock()
        last = self._last_touch.get(name)
        if last is None or now - last >= self.TOUCH_SECONDS:
            self._last_touch[name] = now
            self._db.touch_principal(name)


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
        # nonce -> (expiry, principal who minted it). The principal rides
        # along so the download itself is attributed to whoever asked for
        # it, not to a generic "api" (Phase 79).
        self._pending: dict[str, tuple[float, str]] = {}

    def mint(self, principal: str = "api") -> str:
        self._prune()
        nonce = secrets.token_urlsafe(16)
        self._pending[nonce] = (self._clock() + self.ttl_seconds, principal)
        return nonce

    def redeem(self, nonce: str) -> str | None:
        """The minting principal, exactly once per unexpired nonce; None
        for anything else. Truthy on success (principal names are
        non-empty), so boolean call sites keep working."""
        self._prune()
        entry = self._pending.pop(nonce, None)
        return entry[1] if entry else None

    def _prune(self) -> None:
        now = self._clock()
        for nonce in [n for n, (exp, _) in self._pending.items() if exp <= now]:
            del self._pending[nonce]


class NetworkGuardMiddleware:
    """Refuse to serve a network the deployment is not ready for.

    Installed UNCONDITIONALLY (unlike token auth, which only exists when
    a token does), because its whole job is the case where auth is NOT
    configured: a backend bound to 0.0.0.0 with no token must not answer
    the LAN with a launch console. Judged per request on the interface
    the connection actually arrived at (scope["server"], the listening
    socket's own address), so it holds however uvicorn was started:

    - non-loopback + no token configured -> 403, always;
    - non-loopback + token + plain http  -> 403 unless
      server.allow_plaintext_lan (a bearer token on a plaintext LAN hop
      is a credential broadcast; the opt-in exists because a
      Tailscale/WireGuard tailnet already encrypts below http).

    A server host that is not an IP literal (e.g. Starlette's
    "testserver") is treated as loopback: real uvicorn reports the
    socket's numeric address, so a hostname here means a test client,
    not a network."""

    def __init__(self, app, *, token_configured: bool,
                 allow_plaintext: bool):
        self.app = app
        self._token_configured = token_configured
        self._allow_plaintext = allow_plaintext

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        refusal = self._refusal(scope)
        if refusal is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 4403})
            return
        body = json.dumps({"detail": refusal}).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    def _refusal(self, scope) -> str | None:
        server = scope.get("server")
        if not server or not _is_nonloopback_ip(server[0]):
            return None
        if not self._token_configured:
            return (
                "This backend is reachable from the network but has no "
                "API token configured, so it refuses to serve non-local "
                "requests. Set MANIFOLD_API_TOKEN (or let a real-mode "
                "boot generate one) before exposing it."
            )
        if scope.get("scheme") in ("http", "ws") and not self._allow_plaintext:
            return (
                "Plain-HTTP request on a network interface refused: a "
                "bearer token on an unencrypted LAN hop is a credential "
                "broadcast. Serve TLS (uvicorn --ssl-keyfile/--ssl-"
                "certfile), or if the wire is already encrypted below "
                "http (a Tailscale/WireGuard tailnet), set "
                "server.allow_plaintext_lan: true in config.yaml."
            )
        return None


def _is_nonloopback_ip(host: str) -> bool:
    import ipaddress
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False    # hostname, not an address: a test client (see above)


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

    def __init__(self, app, resolver: PrincipalResolver, nonces: NonceStore,
                 env_path: str = "", roles: "RoleTable | None" = None):
        self.app = app
        self._resolver = resolver
        self._nonces = nonces
        self._env_path = env_path
        # A RoleTable that may still be EMPTY at construction: Starlette
        # builds the middleware stack lazily, but create_app finishes
        # registering routes (and populates the table) before the first
        # request can arrive.
        self._roles = roles

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            identity = self._header_identity(scope)
            # Exempt paths pass through BEFORE any role check - /v1 in
            # particular runs its own scheme with OpenAI-shaped errors,
            # and a middleware 403 in FastAPI's {"detail"} shape would
            # break SDK clients. A resolved identity still binds, so the
            # proxy's audit rows carry the caller's name.
            if is_exempt_path(scope["path"]):
                name, role = identity or (None, None)
                await self._run_as(name, role, scope, receive, send)
                return
            if identity is None:
                nonce_principal = self._nonce_principal(scope)
                if nonce_principal:
                    # The nonce IS the authorization: minting it already
                    # passed the role check on POST /downloads/token.
                    await self._run_as(nonce_principal, "operator",
                                       scope, receive, send)
                    return
                await self._reject_http(send)
                return
            name, role = identity
            needed = self._role_needed(scope, "http")
            if needed and not role_allows(role, needed):
                await self._reject_role_http(send, name, role, needed)
                return
            await self._run_as(name, role, scope, receive, send)
            return
        if scope["type"] == "websocket":
            # No exempt WebSocket routes. Header for non-browser clients;
            # ?token= because a browser WebSocket cannot set headers.
            identity = (self._header_identity(scope)
                        or self._query_identity(scope))
            if identity is None:
                await self._reject_websocket(receive, send, code=4401)
                return
            name, role = identity
            needed = self._role_needed(scope, "websocket")
            if needed and not role_allows(role, needed):
                await self._reject_websocket(receive, send, code=4403)
                return
            await self._run_as(name, role, scope, receive, send)
            return
        await self.app(scope, receive, send)      # lifespan et al.

    def _role_needed(self, scope, kind: str) -> str | None:
        if self._roles is None:
            return None
        method = scope.get("method", "") if kind == "http" else "WS"
        return self._roles.role_for(scope["path"], method)

    async def _run_as(self, principal: str | None, role: str | None,
                      scope, receive, send):
        """Run the app with the resolved identity on the request context.

        Exempt paths carry no identity and read back as the fallbacks
        ("api"/"admin"); that is fine, none of them audits, creates
        attributed rows, or checks a role downstream."""
        p_token = _current_principal.set(principal or "")
        r_token = _current_role.set(role or "")
        try:
            await self.app(scope, receive, send)
        finally:
            _current_role.reset(r_token)
            _current_principal.reset(p_token)

    def _header_identity(self, scope) -> tuple[str, str] | None:
        header = ""
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                header = value.decode("latin-1")
                break
        if not header.lower().startswith("bearer "):
            return None
        return self._resolver.resolve(header[7:])

    def _query_identity(self, scope) -> tuple[str, str] | None:
        query = (scope.get("query_string") or b"").decode("latin-1")
        for candidate in parse_qs(query).get("token", []):
            identity = self._resolver.resolve(candidate)
            if identity:
                return identity
        return None

    def _nonce_principal(self, scope) -> str | None:
        if not is_nonce_path(scope.get("method", ""), scope["path"]):
            return None
        query = (scope.get("query_string") or b"").decode("latin-1")
        for candidate in parse_qs(query).get("nonce", []):
            principal = self._nonces.redeem(candidate)
            if principal:
                return principal
        return None

    async def _reject_role_http(self, send, name: str, have: str,
                                needed: str) -> None:
        # 403, not 401: the credential is valid, the ROLE is short. Names
        # both, so the fix (ask an admin to re-mint with the right role)
        # is in the message.
        body = json.dumps({
            "detail": (
                f"'{name}' has role '{have}', but this operation needs "
                f"'{needed}'. Roles are set when a token is minted "
                f"(Settings, API access); an admin can mint a new token "
                f"with the role this caller actually needs."
            )
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

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

    async def _reject_websocket(self, receive, send, code: int = 4401) -> None:
        # Accept, THEN close with an application code. A pre-accept denial
        # surfaces in browser JS as an opaque handshake failure (a 403 the
        # page cannot inspect); accept-then-close exposes close.code to
        # any client that wants it (4401 no credential, 4403 role too
        # low). The dashboard deliberately does NOT key on these (its
        # handlers treat every failure generically), so the code is a
        # courtesy to non-browser clients, not a contract.
        await receive()                    # the websocket.connect event
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": code})
