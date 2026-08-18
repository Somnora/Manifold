# Decisions

A running log of non-obvious architectural and implementation choices: what
was decided, what the alternatives were, and why. Written for someone
learning backend development. Newest entries at the bottom.

---

## 2026-07-10 — Verify the Lambda API from its OpenAPI spec, not blog posts

**Decided:** Implement `RealLambdaClient` against the machine-readable spec at
`https://cloud.lambda.ai/api/v1/openapi.json` (v1.10.0), and record the facts
we depend on in `lambda_api.py`'s docstring.

**Alternatives:** Trust tutorials/HOWTOs, or the older
`cloud.lambdalabs.com` domain (it now 301-redirects; the spec marks it
deprecated).

**Why:** API details drift, and this project's error handling hangs on exact
strings — e.g. capacity failures are identified by the error code
`instance-operations/launch/insufficient-capacity` inside an
`{"error": {code, message, suggestion}}` envelope. Reading the source of
truth once beats debugging a mystery later. Lesson: when a vendor publishes
an OpenAPI spec, treat it as the contract.

## 2026-07-10 — Interfaces + injected dependencies at every external boundary

**Decided:** Three seams, each an abstract interface with a real and a mock
implementation: `LambdaClient` (Lambda API), `StorageClient` (S3 adapter),
and the `connect_fn` hook inside `ManagedConnection` (SSH dialing).
`create_app()` accepts any of them as arguments.

**Alternatives:** Call `httpx`/`boto3`/`asyncssh` directly where needed and
monkeypatch in tests; or use a mocking library like `responses`/`moto`.

**Why:** The project rule is "no live spend during development" — the whole
test suite and the dashboard must run against fakes. An explicit interface
makes the fake a first-class citizen (the mock dashboard mode uses the same
`MockLambdaClient` as the tests) instead of test-only patch magic. It also
documents exactly which slice of each vendor API we use.

## 2026-07-10 — Launch endpoint returns 202 immediately; the pipeline runs in the background

**Decided:** `POST /instances` does validation and guard checks synchronously
(so rejections are instant and explicit), then persists a `launches` row and
runs retry → boot-wait → SSH-connect as an `asyncio` background task. Clients
poll `GET /launches/{id}` to watch `launching → retrying → booting → active`
(or `failed`, with the error message preserved).

**Alternatives:** (a) Block the HTTP request until the instance is live —
but capacity retries plus booting can take many minutes, and browsers/proxies
time out. (b) A job queue like Celery/Redis — banned by the stack rules and
overkill for a single-user local tool.

**Why:** The status lives in SQLite, so retry progress survives page reloads
and is trivially renderable by the dashboard ("never fail silently"). This is
the standard "accepted for processing" pattern behind HTTP 202.

## 2026-07-10 — Guards compute against LIVE Lambda state, not our database

**Decided:** The concurrency and budget guards list instances from the Lambda
API at request time and sum `price_cents_per_hour` over everything still
billable (`booting`/`active`/`unhealthy`).

**Alternatives:** Track running instances in SQLite and check that.

**Why:** Our DB only knows about launches made through Manifold. If an
instance was launched from Lambda's own console (or a previous Manifold run
crashed), a DB-based guard would happily overspend. The API is the source of
truth for money; our DB is just history. Costs one extra API call per launch.

## 2026-07-10 — Plain stdlib sqlite3 behind a lock, not an ORM or async driver

**Decided:** `db.py` uses `sqlite3` with `check_same_thread=False`, one
`threading.Lock`, WAL mode, and hand-written SQL.

**Alternatives:** SQLAlchemy (ORM), aiosqlite (async driver).

**Why:** This is a single-user local tool writing a few rows per launch;
every statement runs in microseconds, so briefly blocking the event loop is
harmless. An ORM adds a dependency and a layer of indirection to learn for
five queries. If contention ever appears, swapping in aiosqlite is contained
in one file. Boring and readable wins.

## 2026-07-10 — ConnectionManager = "where to dial" and nothing else

**Decided:** The `ConnectionManager` interface has exactly one method,
`dial_target(instance) -> host`. `direct-ssh` returns the public IP;
`tailscale` (Phase 3) will return the tailnet IP. Everything above the dial —
`ManagedConnection`, terminals, forwards, rsync — is shared code that never
branches on mode.

**Alternatives:** Subclass the whole connection stack per mode, or sprinkle
`if mode == "tailscale"` through the codebase.

**Why:** The spec demands the mode be "a swap point only". Making the
interface one method wide makes violations impossible to hide: if a feature
needs anything mode-specific beyond the address, the design is wrong and the
compiler-of-code-review catches it. Small interfaces are how you keep a swap
point honest.

## 2026-07-10 — One supervisor task per SSH connection; reconnect forever with capped backoff

**Decided:** `ManagedConnection.start()` spawns a supervisor coroutine:
connect, wait for the connection to close, reconnect with exponential backoff
(base 1s doubling to a 30s cap), repeat until deliberately closed. Instance
lifecycle ("active" per Lambda) and connection state ("connected" per us) are
tracked and displayed separately.

**Alternatives:** Reconnect only on demand when a command fails; give up
after N reconnect attempts.

**Why:** GPU boxes reboot, networks blip, and sshd comes up a beat after the
instance reports "active". A supervisor gives one place where state
transitions happen, which makes the dashboard's connection badge trustworthy.
We never give up because the fix for "instance is really gone" is
termination, which closes the connection object — not a silent timeout that
leaves a zombie card. The capped backoff keeps a dead host from being hammered.

## 2026-07-10 — Retry semantics: one attempt = one pass through (type + fallbacks)

**Decided:** Per attempt, try the requested type, then each fallback in
order; only `insufficient-capacity` moves to the next candidate. Between
attempts, exponential backoff (5s base, 120s cap), max 5 attempts. Any
non-capacity error fails the launch immediately. Fallbacks that would break
the budget guard are dropped at admission time, before the row is created.

**Alternatives:** Exhaust all 5 retries on the primary type before touching
fallbacks (slower to get a GPU); treat all errors as retryable (hides real
problems like quota or bad parameters behind minutes of pointless retries).

**Why:** The user's intent is "give me a usable GPU soon, prefer this type" —
cycling candidates each round gets capacity fastest while preserving
preference order. Budget-filtering fallbacks up front keeps the guard
absolute: no code path launches an over-budget type. The launched type and
its real hourly rate are recorded separately from the requested type, so cost
history stays honest.

## 2026-07-10 — `known_hosts=None` for instance SSH (trade-off, revisit)

**Decided:** The managed connection accepts the host key of a freshly booted
instance without verification.

**Alternatives:** Pin host keys by fetching them via the Lambda API (not
offered), or TOFU-persist the first-seen key per instance.

**Why:** A brand-new cloud instance has a brand-new host key, so there is
nothing to check it against on first contact; strict checking would just
break every launch. Persisting the first-seen key per instance id (proper
TOFU) is cheap hardening once instance identity matters — noted for Phase 3
when cloud-init could report the key out-of-band.

**Closed 2026-07-11:** see the TOFU host-key pinning entry below.

## 2026-07-11 — TOFU host-key pinning (closes the `known_hosts=None` debt)

**Decided:** `HostKeyStore` (`host_keys.json` next to the database, gitignored)
pins the host key presented on the FIRST connect to each host; every
reconnect must match the pin or the connect fails with an explicit
"host key changed" error. The orchestrator forgets a host's pin whenever the
instance is terminated (both Manifold-initiated and external terminations
detected at reconcile).

**Alternatives:** Fetching keys out-of-band via cloud-init (more moving
parts, and the sidecar channel itself rides SSH — circular); pinning per
instance id instead of per host (asyncssh validates by host, and the
supervisor reconnect loop only knows the host).

**Why:** First contact is unavoidably trust-on-first-use for a fresh cloud
instance, but everything after it need not be — reconnects are where a
long-lived supervisor would silently accept a swapped identity. Forgetting
pins at termination matters because Lambda recycles public IPs: a stale pin
would wrongly reject the next tenant of the address. Backend shutdown does
NOT forget pins (the instances keep running), so a backend restart
re-verifies against the original keys.

## 2026-07-10 — Billing timestamps: `launched_at` vs `active_at`

**Decided:** The `launches` table records both when Lambda accepted the
launch (`launched_at` — billing starts here) and when the instance became
reachable (`active_at`). Cost history uses `launched_at → terminated_at`.

**Alternatives:** One timestamp for "started".

**Why:** Lambda bills from launch acceptance, including boot time. Computing
cost from `active_at` would systematically undercount by a few minutes per
launch. Small thing, but the History page's numbers should survive
comparison with the real invoice.

## 2026-07-10 — App is built by a factory; uvicorn runs it with `--factory`

**Decided:** No module-level `app` object. `create_app()` takes settings and
injected clients; `create_default_app()` reads `MANIFOLD_MOCK` and is run as
`uvicorn app.main:create_default_app --factory`.

**Alternatives:** The common `app = FastAPI()` at module scope.

**Why:** A module-level app would construct the real Lambda client at import
time, so merely importing `app.main` (as every test does) would demand
credentials. The factory pattern keeps construction explicit, makes tests
first-class (each test builds its own app with mocks), and gives mock mode a
clean switch. Rule of thumb: side effects at import time eventually bite.

## 2026-07-10 — S3 adapter specifics worth writing down

**Decided:** `S3AdapterStorage` dials `https://files.<region>.lambda.ai`,
uses the filesystem's UUID (`id`) as the bucket name, and sets boto3's
checksum calculation/validation to `when_required`.

**Why:** All three facts come from Lambda's S3-adapter docs and are easy to
get wrong: the bucket is NOT the filesystem's human name, and without the
checksum settings newer boto3 versions send checksum headers the adapter
answers with `NotImplemented`. Recording them here saves the next debugging
session.

## 2026-07-10 — Dashboard polls the backend; no websockets, no data library

**Decided:** Client components fetch from the backend on a 2-5s interval via
one small `usePolling` hook. No SWR, no react-query, no websocket layer for
page state. Plain `fetch` with typed wrappers in `lib/api.ts`.

**Alternatives:** SWR/react-query (dependency for caching we don't need on a
localhost API with tiny payloads); server-sent events or websockets (real
push, but a second transport to build and debug before Phase 3 actually
needs one for telemetry streaming).

**Why:** The backend already persists every state transition, so polling
`GET /instances` + `GET /launches` renders the truth within two seconds —
good enough for a human watching a launch. Fewer moving parts now; the
websocket work arrives in Phase 3 where it pays for itself (live GPU
telemetry). Rule applied: add realtime transport when the data is realtime,
not for status badges.

## 2026-07-10 — The launch form contains zero rules

**Decided:** The form does not pre-validate region matches or budgets. It
auto-fills the region when a filesystem is picked (pure convenience, still
editable) and submits whatever the user chose; backend rejection messages
are displayed verbatim.

**Alternatives:** Duplicate the guard logic client-side for instant feedback.

**Why:** Project rule — guards live beneath all clients, and clients contain
no business logic. Duplicated validation drifts: the day the backend guard
changes, a client-side copy would lie. Showing the backend's own message
also proves at demo time that the dashboard and any future MCP agent hit the
identical wall.

## 2026-07-10 — Capacity-retry demo via env var, not a demo endpoint

**Decided:** `MANIFOLD_MOCK_CAPACITY_FAILURES=N` (mock mode only) scripts N
insufficient-capacity errors into the mock client at startup.

**Alternatives:** A `/debug/fail-next-launch` endpoint; a magic instance
name that always fails.

**Why:** Failure injection stays in process wiring, not in the API surface —
a debug endpoint would be one more thing clients could hit and one more
branch in production code. The env var reuses the same `scripted_launch_errors`
mechanism the tests use, so the demo exercises the exact code path the test
suite covers.

## 2026-07-10 — SSH key is chosen per launch, validated against Lambda's registry

**Decided:** `GET /ssh-keys` lists the account's registered key names; the
launch form offers them in a dropdown, `POST /instances` takes an optional
`ssh_key_name`, and `config.yaml`'s `ssh.key_name` is only the fallback
default. The orchestrator rejects (400) any key name not registered with
Lambda before calling the launch API.

**Alternatives:** Config-file-only (original Phase 1 design — user feedback:
"nowhere to enter the SSH information"); free-text input (invites typos that
would surface minutes later as a launch failure).

**Why:** The key must exist in Lambda's registry for the launch call to
succeed, so the honest UI is a dropdown of exactly those names. Validating
membership at admission keeps failures at the cheap end of the pipeline.
Note the private key path for the backend's own SSH client remains in
config.yaml; only the key NAME travels with a launch.

## 2026-07-10 — No prices on the Instances page (user decision)

**Decided:** The launch form and instance cards show GPU identity
(description like "1x A10 (24 GB PCIe)") and no hourly prices. The budget
guard still runs on live API prices; the History page keeps its cost column
(a spec deliverable) computed from Lambda-reported rates.

**Why:** Owner feedback at Gate 2: the mock-mode canned prices read as wrong
data, and the cards' job is tracking which GPU is which, not accounting.
Prices remain in the API responses so guards and history stay honest; the
dashboard just stops advertising them where they add noise.

## 2026-07-10 — Post-mortem: orphaned dev servers and a stale .next cache

**What happened:** Demo servers started in the background were never
actually killed (shell job tables do not survive between tool invocations),
so port 8000 was still taken when the owner ran the backend ("address
already in use"), and a `next build` ran while the orphaned dev server had
`.next` open — the mixed cache produced "module not found in the React
Client Manifest" errors on every page.

**Rule going forward:** kill dev processes by port (`lsof -ti :8000 :3000 |
xargs kill`), never by job id; and if dev-server behavior looks impossible,
`rm -rf .next` before deeper debugging.

## 2026-07-11 — Sidecar ships inside cloud-init, not fetched at boot

**Decided:** `build_user_data()` embeds the sidecar's source verbatim in the
cloud-init script (heredoc into /opt/manifold), runs it under systemd as a
loopback-only service.

**Alternatives:** Fetch from a URL at boot (needs somewhere public to host
it, plus a supply-chain surface); scp it after SSH comes up (adds a
provisioning step that can race jobs).

**Why:** The sidecar is one file far under Lambda's 1 MB user-data cap. What
an instance runs is exactly what this commit contains, the instance needs no
extra credentials or network fetch, and the version question ("which sidecar
is on that box?") answers itself: the one the launching backend shipped.

## 2026-07-11 — Tailscale dial target is the MagicDNS hostname

**Decided:** A tailscale-mode launch names the instance `manifold-<launch_id>`,
cloud-init joins the tailnet with that hostname (`tailscale up --ssh
--hostname=...`), and `TailscaleConnectionManager.dial_target()` returns the
instance name. The contract test asserts both managers expose exactly
{mode, dial_target} — nowhere for mode-specific logic to hide.

**Alternatives:** Query the tailnet for the node's 100.x.y.z IP via the
local `tailscale` CLI or Tailscale's API (extra dependency and credentials;
the IP is just what MagicDNS resolves anyway).

**Why:** The orchestrator host is on the tailnet, so the hostname resolves
like any other address — dialing a name keeps the swap point one line and
zero new dependencies. asyncssh does not care whether it dials an IP or a
name; everything above the dial stays byte-identical.

## 2026-07-11 — Safety hook is evidence, not a lock

**Decided:** `terminate(force=False)` asks the sidecar for unpersisted
ephemeral files and blocks with the file list (HTTP 409) if any exist. But
if the sidecar is unreachable — instance still booting, connection down,
orphan instance launched outside Manifold — termination proceeds.

**Alternatives:** Refuse to terminate whenever the check cannot run.

**Why:** The hook's job is preventing accidental data loss, not preventing
termination. A hard requirement would make an unhealthy or half-booted
instance unkillable from the dashboard while it bills by the minute — a
worse failure than losing scratch files the user was warned are ephemeral.
force=true remains the explicit override either way, and sync-then-terminate
is the safe path the dashboard offers first.

## 2026-07-11 — Telemetry: WS to the browser, polling over the SSH forward

**Decided:** The browser gets a real WebSocket from the backend
(`/instances/{id}/metrics/stream`). Behind it, `RealSidecarClient` polls the
sidecar's GET /metrics through a per-call SSH local port forward every 2s,
rather than holding a second long-lived WS through the tunnel.

**Alternatives:** Proxy the sidecar's own WS end-to-end through the forward
(same data rate, but a long-lived forward + WS client to supervise through
every SSH reconnect).

**Why:** The payload is a few hundred bytes every 2 seconds; polling over
the already-supervised managed connection delivers identical freshness with
one less stateful thing to babysit. The sidecar keeps its WS endpoint (it
costs nothing and a future client may want it); the browser-facing contract
is a WS either way, so swapping the internals later touches one class.

## 2026-07-11 — Template placeholders validated at load, ports forced to loopback

**Decided:** Templates are validated when loaded, not when run: every
`{{placeholder}}` in a command must be a declared parameter, every host
mount must start with `/workspace/ephemeral` or `{persistent}`, and declared
ports are published on 127.0.0.1 by the dispatcher regardless of what the
template says. Broken templates are surfaced in GET /templates' `errors`
map instead of silently vanishing.

**Why:** Load-time failure puts the error in front of the person editing
YAML, not the person dispatching a job hours later. The loopback rule is
enforced in the dispatcher (one place) rather than trusted to each template,
consistent with "nothing on the instance listens publicly except sshd."

## 2026-07-11 — Tasks validate twice: at enqueue and at dispatch

**Decided:** `POST /tasks` runs the template's parameter validation
immediately (bad requests fail with 422 at the door), and the dispatcher
re-runs it before rendering the docker command.

**Alternatives:** Validate only at dispatch (a typo sits silently in the
queue until an instance connects, maybe minutes later); only at enqueue
(the template YAML can change between enqueue and dispatch).

**Why:** The person is present at enqueue time — that is when an error is
cheap. The dispatch-time recheck covers the gap where a template was edited
or deleted while tasks were queued. Same principle as the launch guards:
fail at the earliest moment the failure is knowable.

## 2026-07-11 — One task at a time; a running task pins the instance

**Decided:** The dispatcher runs a single task at once, and the idle loop
skips entirely while any task is running. Idle = connected + no running
task + no activity (job or terminal) for the timeout, with the clock seeded
at connection time and reset by every dispatch/completion.

**Alternatives:** Concurrent tasks per instance (GPU contention chaos for
no benefit at max_concurrent_instances=1); counting time-since-last-log as
activity (a long quiet training epoch would read as idle — wrong).

**Why:** Serialized tasks match the one-GPU-instance reality and make logs,
idle logic, and failure attribution trivially understandable. The
"running task = alive" rule is the conservative one: better to keep a box
an hour too long than kill a fine-tune at 90%.

## 2026-07-11 — Idle termination reuses the safety hook, then syncs, then forces

**Decided:** Idle auto-termination calls the standard `terminate(force=False)`.
If the Phase 3 hook blocks (unpersisted files), the dispatcher syncs
ephemeral → persistent and retries with force=true; every step lands in the
audit log (demonstrated live at the gate: idle_termination → idle_sync →
terminated in one trace).

**Alternatives:** Idle-terminate with force=true directly (defeats the whole
point of the hook — unattended termination is exactly when data loss
happens); block and wait for a human (the machine bills all night).

**Why:** Unattended is when the safety hook matters most, and sync-then-
terminate is the only resolution that needs no human. The files end up in
`<filesystem>/ephemeral-backup/`, the box stops billing, and the audit log
tells the story next morning.

## 2026-07-11 — Capacity watches: notify by default, auto-launch double-gated

**Decided:** (James's feature request at Gate 3.) `POST /watches` registers
an instance-type + region watch; the dispatcher polls the catalog and flips
the watch to "available" when capacity appears. Auto-launch requires BOTH
`auto_launch` on the watch AND `watches.auto_launch_enabled: true` in
config.yaml, and goes through `request_launch` — budget, concurrency, and
region guards all apply (test-proven: an over-budget auto-launch watch sees
capacity but is refused, with the rejection audited).

**Alternatives:** Notify-only (capacity at 3am is gone by 8am — the user
called this "a game changer" precisely because reacting manually loses the
race); auto-launch by default (spending money unattended should need two
deliberate switches, not one checkbox).

**Why:** The double gate splits "what I want" (per watch) from "what I
permit" (global config), so an experimenting user cannot accidentally
arm unattended spending. Routing through the normal launch pipeline means a
watch can never become a guard bypass.

## 2026-07-11 — Terminal protocol: JSON control frames in, raw text out

**Decided:** The browser terminal WS sends JSON messages
(`{type: "input"|"resize", ...}`) and receives raw text frames of terminal
output. The backend bridges to an asyncssh PTY session
(`create_process(term_type=..., term_size=...)`) on the managed connection.

**Alternatives:** Binary frames both ways with a framing byte (what ttyd
does — more efficient, more code); running ttyd/gotty on the instance
(banned: nothing may listen publicly except sshd, and a web terminal
service is exactly the kind of thing that gets left running).

**Why:** Two message kinds do not justify a binary protocol on a localhost
link. Raw-text-out means the server needs no envelope parsing on the hot
path, and xterm.js consumes it directly. The mock shell sits behind the
identical bridge code — only the dialed connection object differs — so the
gate demo exercises the real WS handler, not a lookalike.

## 2026-07-11 — Mock shell instead of a local Docker container for the gate demo

**Decided:** `MockSSHConnection.create_process()` returns a tiny scripted
shell (prompt, echo, canned nvidia-smi/claude outputs). The Gate 5 demo and
tests drive the real WS bridge against it.

**Alternatives:** Run a local sshd container and have the backend really
SSH into it (closer to production, but adds a Docker dependency to the test
suite and still would not have a GPU, so nvidia-smi would fail anyway).

**Why:** The thing worth testing is Manifold's bridge: WS handling, PTY
plumbing, resize propagation, activity touching, teardown. That code is
byte-identical in mock and real mode. What a real sshd would additionally
prove (asyncssh's own PTY support) is upstream-tested. Real-instance
verification happens at the manual phase gate like every other phase.

## 2026-07-11 — Recent-files view: bounded sidecar walk, not inotify

**Decided:** (For James's "see files being added/moved/produced" ask.) The
sidecar's GET /storage/recent walks ephemeral + persistent roots, returns
files modified in the last N hours (default 24), newest first, hard-capped
at 20k entries scanned / 50 returned, with a `truncated` flag. Dashboard
polls it every 5s while the Files panel is open.

**Alternatives:** inotify/watchdog for true event streaming (persistent
storage is NFS, where inotify does not see remote writes — it would
silently miss exactly the files jobs produce); rsync --list-only diffing
(stateful, more moving parts).

**Why:** A bounded mtime walk is stateless, works identically on NFS, and
5-second freshness is plenty for "is my job producing outputs?". The scan
cap keeps a million-file HF cache from wedging the sidecar; the truncated
flag keeps the cap honest instead of silent.

## 2026-07-11 — TAO support is a template, not a feature

**Decided:** NVIDIA TAO Toolkit support ships as `templates/tao-train.yaml`
(task entrypoint + spec file + results dir, all on persistent storage), not
as dedicated backend/frontend code.

**Why:** This is the Gate 4 design paying rent: "TAO made easy" required
zero code because templates are data. The same holds for the next toolkit
James wants — the answer is a YAML file. If a workflow ever genuinely needs
new capability (e.g. multi-node), that is when code gets written.

## 2026-07-11 — MCP thinness is enforced by an import-allowlist test, not review

**Decided:** `mcp_server.py` may import exactly `os`, `typing`, `httpx`,
and the MCP SDK — a test parses the module's AST and fails on anything
else. The module lives in the same package as the backend for packaging
convenience (`manifold-mcp` console script), but structurally it can only
speak HTTP to the backend.

**Alternatives:** A separate package/venv for true physical isolation
(more honest still, but adds a second lockfile and install step to a
single-user local tool); code review as the enforcement mechanism (decays
the first time someone adds "just one" convenience import).

**Why:** "The MCP server is a thin client with no path around guards" is
the spec's hard rule; a rule is only real if a machine checks it. The AST
test turns an architectural intention into a failing build. Guard parity
is additionally proven behaviorally: the same over-budget launch through
the dashboard's HTTP path and the MCP tool returns byte-identical
rejection text.

## 2026-07-11 — MCP tools return errors as data, not protocol exceptions

**Decided:** Backend rejections come back to the agent as
`{"error": <the backend's exact detail>}` (plus `blocked` +
`unpersisted_files` for the termination hook) rather than raised MCP
errors. Every tool takes an optional `note`, and every call — success or
rejection — is POSTed to `/audit/agent` and shown on the Agent Activity
page. Audit posting is best-effort: an unreachable backend already failed
the real call, so a failed audit write must not mask the real error.

**Alternatives:** Raise protocol-level errors (clients render them
inconsistently, and several truncate the message — the guard's dollar math
is the most useful part); make audit writes mandatory (turns a logging
hiccup into a tool failure the agent then retries, double-logging).

**Why:** An agent that can read "Budget guard: … would bring hourly spend
to $22.32, over the $4.00 limit" can explain to its human exactly why it
stopped, or pick a cheaper GPU. Error-as-data with the backend's own words
keeps agents and humans looking at the same truth; the audit trail makes
the agent's whole session reviewable after the fact.

## 2026-07-11 — Phase 7: first-run setup through the dashboard

**What happened:** James started real mode with no .env; the backend
crashed at import of the real client and the dashboard showed blank
dropdowns with no explanation. Root cause: credentials were file-only and
the failure mode was silent.

**Decided:** Three pieces. (1) Real mode without a key now boots into an
`UnconfiguredLambdaClient` — every Lambda-backed endpoint returns 503 with
"No Lambda API key configured. Open the dashboard's Settings page…", and
the Instances page shows a banner linking to Settings. (2) A Settings page
accepts the key once, the backend VALIDATES it against the live Lambda API
before saving (an invalid key is rejected with Lambda's own message and
never persisted), writes it to .env preserving comments, and (3) hot-swaps
the running client through a `SwappableLambdaClient` wrapper — the launch
form goes live without a restart.

**Alternatives:** Keep .env-only setup with better docs (still fails the
"someone without Lambda knowledge" test); store secrets in SQLite or
browser localStorage (violates the secrets-live-in-.env rule and secret
hygiene — the browser never holds the key, it passes through one POST and
is never echoed back); require a restart after saving (simpler, but the
first-run experience should end with a working launch form, not another
terminal step).

**Why:** The dashboard is the product's front door; the first five minutes
should not require knowing what dotenv is. Validation-before-save turns
"why are my dropdowns empty an hour later" into "Lambda rejected this key"
at paste time. Secret hygiene held: booleans and counts in /settings/status,
key never logged, never audited, never returned.

## Phase 8 — reconnect-on-startup (2026-07-11)

**What:** On startup the backend calls `orchestrator.adopt_running_instances()`,
which lists live Lambda instances and re-establishes a `ManagedConnection` to
any that are `active` with an IP and not already tracked. Connection mode comes
from the launch history row, falling back to `default_connection_mode` for
instances launched outside Manifold.

**Why:** Before this, restarting the backend orphaned every running instance —
it kept billing on Lambda but the dashboard showed it `disconnected` with no way
to reconnect, forcing a terminate-and-relaunch. Surfaced live when a wrong SSH
key (config pointed at `id_ed25519` instead of the launch key) left an instance
stuck `reconnecting`; the only recovery was a restart, which then orphaned it.

**Design:** Best-effort — an unconfigured or unreachable Lambda client logs and
returns 0 rather than blocking boot (a startup hook must never crash the app).
Reuses the exact launch-path connection code via a shared `_open_connection`
helper, so an adopted connection is byte-identical to a freshly launched one
(same terminal, telemetry, idle detection). Adoptions are audited.

**Alternatives considered:** Persisting connection objects across restarts
(impossible — SSH sockets don't survive a process). Storing "last known
instances" in SQLite and trusting it (rejected: Lambda is the source of truth;
an instance may have been terminated out-of-band, so we must re-list live).

## Phase 9 — guided launch form + full region catalog (2026-07-11)

**What:** The launch form now walks GPU -> region -> filesystem. GPUs list
available types first (cheapest to priciest), out-of-capacity ones greyed and
unselectable. Region options are driven by the chosen GPU: regions with
capacity for it are selectable (a region where you already have a filesystem
wins ties), the rest are greyed with "not available for this type". Filesystem
narrows to the selected region. Added the full 12-region NA catalog with human
names (Virginia, Arizona, ...) and a `GET /regions` endpoint serving the whole
region universe so the form can grey out what a GPU can't use.

**Why:** James kept building invalid combinations (a us-east-1 filesystem with a
region the GPU wasn't in, or picking a region blindly) and hitting backend
rejections after the fact. Mirroring Lambda's own console flow — pick the GPU,
then see only the regions that GPU can actually run in — makes the invalid
combination unrepresentable in the UI. The backend guards stay the final
authority; this just stops the user reaching them by accident.

**Design:** `/instance-types` shape is unchanged (WatchPanel still consumes it);
region names live in a separate `/regions` endpoint so nothing breaks. Native
`<select>` with `disabled` options does the greying — no custom dropdown,
matches the console, stays readable. Auto-selection fills sensible defaults
(cheapest available GPU; a region where a filesystem exists) but never fights
an explicit choice.

## Phase 10 — in-dashboard chat with a served model (2026-07-11)

**What:** A Chat button on connected instance cards. `GET
/instances/{id}/model` reports whether a model server is live (a running
task whose template publishes a port — vllm-serve today) and which model.
`POST /instances/{id}/chat` relays an OpenAI-style chat completion to the
model's loopback port over an SSH local port forward and streams the SSE
response straight through to the browser. New `ModelClient` seam
(real = per-call port forward + httpx streaming, mock = canned SSE chunks),
mirroring `SidecarClient` exactly.

**Why:** James's original vision: download a HuggingFace model and talk to
it inside the dashboard. vllm-serve already served the model on the
instance's loopback; this adds the one missing hop, browser -> backend ->
SSH forward -> vLLM, without opening any new listener anywhere.

**Design choices:**
- Discovery, not registration: "a model is being served" is derived from
  the task queue (running task + template with ports), so there is no
  separate serving state to drift out of sync. Kill the job, chat closes.
- The relay passes vLLM's SSE bytes through untouched instead of
  re-encoding: the browser parses the standard OpenAI chunk format, and
  mid-stream failures are surfaced as a data: {"error": ...} event rather
  than a silently truncated reply.
- Chat traffic counts as activity (touch_activity per chunk), and a
  serving task already pins the instance alive via the running-task rule,
  so a conversation can't be idle-terminated mid-reply.
- Every chat call is audited (instance, message count, model) — message
  CONTENT is deliberately not logged.
- Known quirk: vllm-serve's `port` parameter changes the container port
  while the loopback mapping stays 8080:8080; chat uses the host side of
  the mapping, which is what the dispatcher actually publishes, so it
  works regardless. Cleaning up that parameter is cosmetic backlog.

## Phase 11 — Autopilot: a self-hosted model drives Manifold (2026-07-11)

**What:** Agent runs. Pick a brain (any instance serving a model via
vllm-serve), give a goal, and the backend runs the loop: send conversation
to the brain over the managed SSH connection -> expect ONE JSON action ->
execute it against Manifold's own guarded operations -> feed the observation
back. GPU A literally manages GPU B. New Autopilot page shows every run and
step live, with cancel; steps also land in Agent Activity under actor
"autopilot". Also: instances terminated out-of-band are now reconciled away
(card dropped, SSH supervisor reaped, history row closed), and the dashboard
marks stale data as stale when the backend stops answering (James hit both).

**Why this shape (the honest version):**
- A strict one-JSON-action-per-turn protocol instead of OpenAI tool-calls:
  vLLM's native tool-call support varies wildly by model; plain JSON is the
  thing 7B-class open models can reliably produce, and parse errors are
  bounced back as correction hints (3 consecutive failures end the run).
- The loop lives IN the backend, next to the guards, not in a client:
  launch_gpu IS orchestrator.request_launch, so budget/concurrency/region
  guards bind the autopilot with zero new enforcement code. Test-proven:
  an over-budget launch comes back as {"error": "Budget guard: ..."} data
  the model reads and adapts to.
- Caps everywhere: hard step ceiling (config autopilot.max_steps_cap),
  wait cap, per-turn chat timeout, MAX_CONSECUTIVE_FAILURES, fixed action
  allowlist (no shell, no arbitrary HTTP, no self-modification). Runs are
  cancellable; orphaned runs are marked failed at startup.
- Honest limits, stated in docs: a 7B open model is a mediocre long-horizon
  agent. The harness compensates (tiny action space, errors-as-data, hard
  caps), and the same guarded surface is what a heavyweight brain (Claude
  via MCP) uses for hard jobs. Autopilot is the self-sufficient tier, not
  the only tier.

**Alternatives:** LangChain/agent frameworks (dependency ban, and the loop
is ~200 lines); letting the agent shell into instances (unbounded blast
radius — refused); OpenAI-native tool calling (model-dependent, brittle on
small models).

## Phase 12 — File Bridge: SFTP over the managed connection, not S3 (2026-07-11)

**What:** Upload/download between this machine and an instance, everywhere:
POST /instances/{id}/files/upload (multipart) and GET .../files/download
(streamed), riding SFTP on the managed SSH connection. Dashboard: Upload
button + per-file Download links in the Files panel. MCP: upload_file /
download_file tools (auto-select when exactly one instance is connected),
so agents can round-trip artifacts. Paths are jailed to /lambda/nfs/ and
/workspace/ephemeral/ (normpath, then prefix check — traversal rejected);
relative paths land on the instance's persistent filesystem. Transfers are
audited and count as activity for idle detection.

**Why SFTP, not the S3 adapter:** the adapter exists only in a few regions
(James's Virginia filesystem has none), needs separate keys, and the SSH
connection is already supervised. SFTP works in every region with zero new
credentials. The S3-based Storage page stays for browse/delete without an
instance; the bridge requires a connected instance, which is honest — the
persistent filesystem is only reachable through one.

**Also:** sdxl-generate reference template. Its Python lives in a static
PYCODE env var and parameters arrive as argv, keeping the dispatcher's
shell-quoting at the top level of the command — the pattern for future
script-in-container templates (nested quoting is where injection bugs
breed). A 404 on download is detected by pulling the first SFTP chunk
BEFORE the response starts, so missing files are a real 404 rather than a
broken 200 stream.

## Phase 13 — OpenAI-compatible /v1 proxy (2026-07-11)

**What:** GET /v1/models and POST /v1/chat/completions (streaming +
non-streaming) at localhost:8000. Any OpenAI client — the openai SDK,
OpenClaw, IDE assistants — points its base_url at Manifold and talks to a
model served on an instance. Routes by the request's `model` (instance id,
then exact model id, then a lenient single-model fallback) to the serving
instance; the completion rides the managed SSH connection. Verified live
against the real `openai` Python SDK (models.list, chat, streaming).

**Why non-streaming got its own ModelClient method:** for stream=false we
POST to vLLM with stream=false and return its response object verbatim —
real `choices` and `usage` — rather than reassembling from SSE chunks
(which drops usage and risks a lossy reconstruction). stream=true relays
vLLM's SSE bytes untouched. Passing every other OpenAI param straight
through means temperature/top_p/stop/max_tokens all just work; the only
field we rewrite is `model`, forced to the real served id so vLLM accepts
it (this is what makes the lenient single-model route work for tools with a
hardcoded model name).

**Why the proxy launches nothing / has no budget guard of its own:** it
only reaches models ALREADY running, whose vllm-serve launch already
cleared the budget and concurrency guards. Cost is the instance's hourly
rate, already governed; there is no per-token spend to guard. Proxy use
touches idle-activity so a model in use isn't idle-terminated.

**Auth:** optional bearer via MANIFOLD_PROXY_KEY (.env, secret). Empty =
open, correct for the localhost-only default; set it before exposing the
backend past localhost. Errors are OpenAI-shaped ({"error": {message,
type, code}}) so real clients render them properly.

**Alternatives:** a full pydantic model for the request (rejected — the
OpenAI surface is wide and evolving; a permissive pass-through of the raw
JSON is more compatible and less brittle); reassembling non-streaming
responses from the stream (rejected — loses usage, more code, faithful
pass-through is simpler and correct).

## Post-13 — Model readiness probe: "serving" vs "ready" (2026-07-11)

**Problem (found in a next-move audit):** chat, the OpenAI proxy, and
autopilot all treated a model as usable the instant its vllm-serve task was
'running'. But that task goes running when the CONTAINER launches, while
vLLM then spends minutes pulling the image, downloading weights, and
loading the GPU before its API answers. On real hardware the dashboard
would advertise a model as available and every call would get
connection-refused for minutes — three features looking broken on first
real use.

**Fix:** `Dispatcher.model_ready(instance_id, task_id, port)` probes GET
/v1/models on the instance (via the previously-unused
`ModelClient.model_info`) and caches the verdict with a TTL — short (3s)
while loading so the UI flips promptly, long (30s) once ready. Every
model-using path now gates on it: `/instances/{id}/model` reports
serving + ready + status_detail; chat returns 503 "still loading" instead
of a connection error; `/v1/models` lists only ready models (a client
picking from the list can always use it); `/v1/chat/completions` returns a
clean 503 model_loading; autopilot refuses to start on a loading brain; the
chat panel shows a loading state and the autopilot brain picker only offers
ready models.

**Why a TTL cache, not a background loop:** the probe opens an SSH forward,
so doing it on every request (the chat panel polls every 5s) would be
wasteful; a background loop would probe instances nobody is looking at. On-
demand with a TTL probes only what's actually being used, at most once per
window. Keyed by task_id so a fresh serve gets a fresh verdict.

## Phase 14 — File Navigator: browse/sizes/delete on the sidecar, archive over SFTP (2026-07-11)

**What:** A real file browser on the instance card (Browse button): breadcrumb
navigation over both volumes, a Sizes lens (recursive per-child totals,
heaviest first — the "what is eating my filesystem" cleanup view James asked
for), delete with a type-of-guard (directories require recursive=true and a
UI confirmation; roots are never deletable), upload-into-this-folder, per-file
download, and whole-directory download as one .tar.gz.

**Where the logic lives, and why:** listing/usage/delete are SIDECAR endpoints
(/fs/list, /fs/usage, /fs/delete) rather than SFTP walks from the backend. The
sidecar runs on the box: os.scandir against local disk/NFS is orders of
magnitude faster than per-entry SFTP round trips from a laptop, the recursive
usage walk is bounded (MAX_SCAN_ENTRIES + truncated flag, same pattern as
/storage/recent), and the real implementation gets unit-tested against a temp
directory instead of a dict pretending to be a filesystem. Path jailing is
enforced INSIDE the sidecar (resolve + parent check against its roots), so the
backend relay cannot be tricked into escaping even if its own checks regressed.
Trade-off: new sidecar endpoints only exist on instances launched after this
commit — acceptable because instances are ephemeral by design.

**Archive:** tar.gz runs ON the instance (tar czf to a hashed temp under
/workspace/ephemeral/.manifold-archives), streams down over the existing SFTP
read path, temp removed after — compression happens where bandwidth is cheap,
and one click fetches a whole outputs directory instead of N file downloads.

**Gotcha recorded:** with `from __future__ import annotations`, a pydantic
model defined INSIDE create_app silently becomes a query parameter (FastAPI
resolves annotation strings via module globals) — request models in the
sidecar must live at module level. Found by the 422 in tests.

## Phase 15 — Data pipeline: script-run + llm-synthesize (2026-07-11)

**What:** Two templates that compose into James's scrape->synthesize
workflow. `script-run`: run any Python script from <filesystem>/scripts
with the whole persistent filesystem mounted rw at /data (requirements.txt
auto-installed; args passed as ONE shell-quoted string — argv[1] — keeping
the dispatcher's injection guard intact). `llm-synthesize`: map an
instruction over every JSONL/CSV record using the model served on the SAME
instance, writing {"record", "synthesis"} lines to synthesized/<name>.jsonl,
with a `limit` param for cheap quality checks before full runs. Plus
docs/data-pipeline.md (the candidate-research worked example).

**The enabling change — `network: host` for templates:** a synthesize
container must call vLLM, which another job publishes on the HOST's
127.0.0.1 — unreachable from Docker's default bridge (host-gateway only
reaches 0.0.0.0 binds). Templates may now declare `network: host`,
validated at load (only "" or "host"; mutually exclusive with `ports`,
since host networking has no mappings). Consistent with the hard rule:
host networking lets a container DIAL loopback; it creates no listener.
The synthesize->vLLM hop never leaves the box.

**Why stdlib-only PYCODE:** the synthesize script uses urllib/csv/json, so
python:3.11-slim starts in seconds with no pip step, and the model id is
auto-discovered from /v1/models rather than asked of the user twice.

**Never-run-template guard:** test_llm_synthesize_pycode_actually_runs
executes the template's embedded Python for real against a stub OpenAI
server (JSONL in, structured JSONL out, progress lines checked). The
sdxl-generate lesson: a template whose script has never executed is a bug
that ships silently.

## 2026-07-11 — Job exit codes: `set -o pipefail` in the dispatch wrapper

**Decided:** The remote command wrapper (`wrap_remote_command`) sets
`set -o pipefail` before piping container output through `tee`.

**Alternatives:** Drop the tee (lose the persistent on-instance log copy);
capture `PIPESTATUS[0]` after the fact (bash-only anyway and more moving
parts).

**Why:** A pipeline's exit code is the LAST command's. Ours ended in `tee`,
which always exits 0, so every job reported "succeeded" regardless of what
the container did. Found at the first real-hardware gate: two vllm-serve
jobs that crashed in seconds (GGUF repo, unsupported by vLLM) showed green,
and the llm-synthesize that then had no model to call showed green too.
Mock SSH always returns exit 0, which is exactly why the tests never caught
it — so the regression test executes the real wrapper in a real bash
(`test_wrap_remote_command_propagates_container_exit_code`), same lesson as
the never-run-template guard.

## 2026-07-11 — Idle auto-termination: 30 min default + per-instance switch

**Decided:** `idle.timeout_seconds` default moves 300 -> 1800. The instance
card shows the idle countdown, and a per-instance "Keep alive" switch
(persisted on the launch row, `keep_alive` column) disables idle
auto-termination entirely until switched back.

**Alternatives:** Keep 300s (cheap but hostile to interactive sessions); a
global on/off toggle (all-or-nothing loses the cost protection); pausing
the timer on dashboard polling (would make merely LOOKING at the dashboard
keep instances alive — too magical).

**Why:** During live testing the 5-minute timeout terminated the instance
mid-session between two manual steps; the user experienced it as data loss
("the instance totally disappeared"). Cost protection stays on by default,
but the user can now SEE the countdown before it acts and opt an instance
out explicitly. Terminal and job activity still reset the clock; the audit
log records both the switch and every idle termination.

## 2026-07-11 — llm-synthesize: preflight wait + resilient mapping (Phase 17)

**Decided:** The synthesize script (a) validates the input path up front,
(b) polls /v1/models until the served model actually answers (bounded by
MANIFOLD_SYNTH_READY_TIMEOUT, default 300s) instead of calling it once, (c)
retries a transient per-record error twice, (d) tolerates a malformed input
line by skipping+counting it rather than dying, and (e) parses JSON replies
(including ```json fences) into a `synthesis_json` field, flagging non-JSON
with `parse_error`.

**Alternatives:** Keep the thin one-shot script and rely on the operator to
sequence serve→ready→synthesize perfectly by hand (this is exactly what
failed at the first live gate — synthesize was queued against a model that
had crashed, and it died on a raw urllib traceback); parse JSON downstream
on the user's machine (defeats "synthesize into usable points seamlessly").

**Why:** The pipeline's value is that a cloud GPU feels self-sufficient; a
stage that crashes the instant timing is imperfect, or that hands back
double-encoded strings, breaks that. Every branch is covered by executing
the REAL embedded script against a configurable stub vLLM (never-run-guard
extended to eight cases: happy path, fenced JSON, prose, wait-for-ready,
retry, malformed input, missing input, no-model-fail-fast).

## 2026-07-11 — script-run: runner in an env var (fixes a quoting collision)

**Decided:** script-run's logic moved into a RUNNER env var invoked as
`bash -c "$RUNNER" manifold {{script}} {{args}}`, receiving script and args
as positional params ($1, $2). It also preflights that the script exists
(fail fast, exit 2, clear message) and caches pip downloads under
/data/.cache/pip on persistent storage.

**Alternatives:** Keep the inline `bash -c 'cd /data && ... python
scripts/{{script}} {{args}}'` wrapper.

**Why:** The inline wrapper was a latent bug. render_docker_command
shlex-quotes each {{param}} for a TOP-LEVEL shell context, but the params
were substituted INSIDE a single-quoted `bash -c '...'` string — so
`{{args}}`'s own single quotes collided with the wrapper's, e.g.
`bash -c '... python ... '--state TX''` fractures the argument. The mock SSH
only echoes, so no test caught it; the moment a real scraper passed args
with a space, argv would split wrong. The env-var/positional pattern (the
same one llm-synthesize already uses for PYCODE) keeps every substituted
value at the top level where shlex-quoting is correct. Caught by the new
execute-the-real-runner test, which asserts args-with-spaces arrive as one
argv[1]. Same never-run-template lesson, now applied to the scrape stage.

## 2026-07-11 — Sidecar diagnosis over the SSH channel

**Decided:** A read-only diagnostic (`app/diagnostics.py`,
`GET /instances/{id}/sidecar/diagnose`, "Diagnose" button on the telemetry
panel) probes the instance over the managed SSH connection when the sidecar
HTTP is silent, and classifies the cause: cloud-init still running,
cloud-init error, sidecar crashed (with the journal tail), sidecar starting
(up but not yet listening on 9411), or a transient forward failure (healthy
on the instance).

**Alternatives:** Leave the dead-end "sidecar not reachable yet" message;
add more retries to the forward (treats the symptom, not the cause).

**Why:** At the first live gate telemetry showed "not reachable yet" 13
minutes after boot with no way to tell whether cloud-init, the service, or
the SSH forward was at fault. The managed SSH connection is known-good when
this happens (the card shows "connected"), so the instance can be asked
directly. The probe is pure and injectable — classification is unit-tested
against canned probe outputs, so the logic is verified without hardware;
the live session confirms the root cause. Read-only shell only; it opens no
new listener and rides the one channel already trusted.

## 2026-07-11 — Model presets + model-id normalization (vllm-serve UX)

**Decided:** A curated catalog (`app/model_catalog.py`, `GET /model-presets`)
of ungated, VRAM-tiered models shown as click-to-fill chips under the
vllm-serve model_id field. The dashboard also normalizes model_id on submit:
a pasted `huggingface.co/owner/model` URL is reduced to `owner/model`, and
trailing whitespace/punctuation is trimmed.

**Alternatives:** Live HuggingFace API (browse trending) — a network
dependency and far more surface for a first version; free-text id only (the
status quo, which let a stray trailing ";" reach vLLM as part of the repo
id and fail the serve).

**Why:** "Is the model id all we need?" plus a fat-fingered `;` in the field
showed the id box is the friction point. Presets remove the typo path for
common models and answer "recommend by GPU" via the tier badge (A10 24GB vs
H100 80GB) without heavy plumbing. Presets are ungated on purpose so a first
serve needs no HuggingFace token; gated models (Llama, Gemma) need token
passthrough, deferred. The URL/trim normalization directly answers "would
pasting the URL be easier?" — now both work.

## 2026-07-11 — Job History: active/finished split with removal

**Decided:** The Jobs page splits Active (queued/running) from History
(succeeded/failed). Finished jobs can be removed one at a time
(`DELETE /tasks/{id}`, refused for a running job) or cleared in bulk
(`DELETE /tasks/finished`); both drop the task and its logs. Route order
puts the literal `/tasks/finished` before `/tasks/{task_id}`.

**Alternatives:** Keep one flat queue (what shipped) — finished jobs from
past sessions accumulate forever with no way to clear; auto-expire old
tasks (surprising, and history is sometimes worth keeping).

**Why:** Tasks persist in SQLite across instances and sessions (correct), but
the flat "Queue" list mixed a fresh failure with week-old successes and had
no clear affordance. Splitting active from history matches how the user
reasons ("what's running now" vs "what happened"), and explicit removal
keeps deletion a deliberate act. A running job cannot be removed, so history
cleanup can never orphan a live container.

## 2026-07-11 — Sidecar deps must target the service's interpreter

**Decided:** cloud-init installs the sidecar's deps with
`/usr/bin/python3 -m pip install --break-system-packages fastapi uvicorn
pynvml` (with a bare-pip retry and a non-fatal fallback), and ensures pip
for that interpreter first — matching the `ExecStart=/usr/bin/python3` the
systemd unit uses.

**Alternatives:** The old `python3 -m pip install ...` (bare); a virtualenv
for the sidecar (more moving parts on a single-file service); shipping the
sidecar as a container (heavier, and it needs host pynvml/NVML anyway).

**Why:** Strongly suspected root cause of the recurring "sidecar not
reachable yet" seen on every instance. On Lambda ML images `python3` in
root's PATH is often conda's, so a bare `pip install` puts fastapi/uvicorn
where `/usr/bin/python3` cannot import them; the service then crash-loops
(Restart=always) and never listens on 9411, so telemetry AND the file
browser (both sidecar-backed) fail together. Targeting /usr/bin/python3
explicitly, plus PEP 668 handling for newer Ubuntu, closes all three
plausible failure modes at once. Cannot be exercised without live spend, so
the guard is an invariant test: the fastapi install line must start with
`/usr/bin/python3 -m pip` and never regress to a bare `python3`. The new
Diagnose button confirms it on the next launch (service active + listening).

## 2026-07-11 — NVIDIA runtime configured every boot (fixes 126 on all jobs)

**Decided:** cloud-init runs `nvidia-ctk runtime configure --runtime=docker`
+ `systemctl restart docker` UNCONDITIONALLY (was gated inside
`if ! command -v nvidia-ctk`), adds `ubuntu` to the docker group, and runs a
boot self-test (`docker run --rm --gpus all nvidia/cuda ... nvidia-smi -L`)
whose verdict lands in /var/log/manifold-init.log.

**Alternatives:** Keep the gate (what shipped); configure the runtime at
image-build time (we do not build the image).

**Why:** Every job — even the trivial gpu-smoke — failed with exit 126
("OCI runtime create failed") once exit codes became honest. Root cause: the
runtime-configure step lived inside the toolkit-install guard, but Lambda
images SHIP the toolkit, so the guard was skipped and a freshly
get.docker.com-installed docker was never wired to the NVIDIA runtime;
`docker run --gpus all` then failed on every GPU job. The pre-pipefail tee
bug had masked this as "succeeded" since the beginning — GPU jobs never
actually ran. Configure is idempotent, so running it unconditionally is
safe; the boot self-test makes the next diagnosis instant (read the init log
from the in-app Terminal).

## 2026-07-11 — script-run env_file: API keys for scrapers

**Decided:** script-run takes an optional `env_file` param (a path on the
filesystem, e.g. `research/.env`); the runner sources it (`set -a; . "$ef";
set +a`) before the script runs, failing fast if the named file is absent.

**Alternatives:** Bake keys into the script (leaks into git); pass keys as
job parameters (they would show in the audit log and job card); a secrets
store (over-engineered for a single-user local tool).

**Why:** Research scrapers need API keys (news, FEC, etc.). Uploading a .env
to the persistent filesystem via Browse and naming it keeps secrets on the
instance's NFS, out of git and out of the job record, while the script reads
them from the environment as usual. Verified by executing the real runner
with a temp .env and asserting the variable reaches the script.

## 2026-07-11 — Connection reliability: keepalive + per-command timeout

**Decided:** The managed SSH connection sets `keepalive_interval=15s,
keepalive_count_max=3` (drop a silent link in ~45s), and
`ManagedConnection.run()` enforces `ssh.command_timeout_seconds` (default
120s) via `asyncio.wait_for`, with sync/archive passing a longer 600s bound
and job dispatch passing `timeout=None` (it streams for hours).

**Alternatives:** Rely on the OS TCP timeout (~15 min to notice a dead
path); no command ceiling (a stalled NFS mount wedges the request until the
client aborts).

**Why:** Best explanation for "backend errors appearing periodically" in
live testing. Without keepalive, a silently-dropped TCP path leaves the
supervisor showing CONNECTED for ~15 min while every sidecar/model/file call
hangs then 30s-aborts on the dashboard. Keepalive turns that into a ~45s
detect-and-reconnect. Separately, a command with no ceiling can hang
forever on a stalled mount; a bounded run fails just that call and leaves
the supervised connection up (a truly dead link is caught by keepalive, not
by wedged commands). A timeout raises ConnectionError, which callers already
handle as "couldn't run it."

## 2026-07-11 — Short-TTL cache on list_instances, with a guard bypass

**Decided:** `SwappableLambdaClient` caches `list_instances` for 2s,
invalidated on any launch/terminate WE initiate and on credential swap. The
concurrency/spend guard calls `list_instances(fresh=True)`, which always
hits the API and refreshes the cache.

**Alternatives:** No cache (every 2s dashboard poll — times N tabs, plus MCP
and capacity watches — hits Lambda's rate-limited API, a plausible source of
periodic 429s); cache without a guard bypass (two launches ~1s apart could
both read a stale "0 running" and both pass, doubling spend under a
max_concurrent=1 cap).

**Why:** The read path (dashboard view, reconcile) tolerates ≤2s staleness —
it already polls at that cadence — and invalidation-on-mutation means any
action taken through Manifold shows up immediately; only out-of-band console
changes wait out the TTL. The spend guard is the one caller where staleness
costs money, so it bypasses the cache unconditionally. The bypass is a
`fresh` kwarg on the LambdaClient interface (ignored by non-caching
implementations), keeping the guard's data source explicit at the call site.

## 2026-07-11 — Instance panels survive transient reconnects (no more flap)

**Decided:** InstanceCard latches `everConnected` once the SSH state first
reaches "connected", and gates the action buttons AND the terminal/files/
browse/chat/telemetry panels on that latch instead of the live
`connection_state`. The card still disappears when the instance leaves the
list (terminated).

**Alternatives:** Keep gating on the live state (what shipped); add
auto-reconnect to each panel's socket (more code, and a fresh shell loses
state anyway).

**Why:** During a heavy load (downloading a ~15 GB model), the supervisor
can briefly flip CONNECTED → reconnecting → CONNECTED. Gating on the live
value unmounted and remounted the whole control row on every blip — the
"terminal kept disappearing and reappearing" the user reported. Latching
keeps the UI stable; each panel already surfaces its own connection status,
so a real drop is still visible without tearing the card apart. Complements
the Phase 20 keepalive, which makes those flips rarer in the first place.

## 2026-07-11 — Claude CLI on PATH; honest model-loading copy

**Decided:** cloud-init adds `~/.local/bin` to PATH via
`/etc/profile.d/manifold-path.sh` and `.bashrc`, so `claude` resolves in a
fresh Open Terminal shell. The chat panel's loading state reframes the
readiness-probe error as expected-while-downloading and points to the job
Logs for real progress.

**Why:** The Claude installer warned "~/.local/bin is not in your PATH", so
the CLI it just installed wasn't runnable without manual PATH surgery. And
the chat panel surfaced the raw probe error ("Server disconnected without
sending a response") while a model was merely still downloading (VRAM 0.4/22
GiB confirms it never loaded), which reads as a crash. Both are honesty/UX
fixes, not behavior changes: PATH makes the pre-installed tool usable, and
the copy tells the user what's actually happening instead of alarming them.
(Interactive Claude sign-in on a headless box remains manual — that is
inherent, not something cloud-init can pre-solve.)

## 2026-07-11 — Finding: reconnect_on_startup is genuine restarts, not over-logging

**Investigated (Prompt A):** Agent Activity showed dozens of near-identical
`reconnect_on_startup` rows, ~one per minute.

**Finding:** NOT over-logging. The event is emitted in exactly one place —
`Orchestrator.adopt_running_instances()` (orchestrator.py), guarded by
`if adopted:` — and that method is called from exactly one place: the
FastAPI `lifespan` startup handler (main.py), once per process start. There
is no loop and no repeated call; grep confirms a single call site. Each row
therefore corresponds to a real backend restart that genuinely re-adopted a
running instance.

**Root cause of the frequency:** the dev server runs with `--reload` (see
CLAUDE.md). During active development every save to a `backend/app/*.py`
file restarts the process, and each restart legitimately re-adopts the
still-running instance and writes exactly one audit row. With a live
instance and a burst of edits (shipping several phases), that is dozens of
honest restarts. In production (no `--reload`) it fires once per real start.

**Decision:** do NOT change the emit — it is correct (once per actual
startup, only when something was adopted). Two changes instead: (1) the
Agent Activity UI collapses consecutive identical events into one counted
row with a time range, so N restarts read as "reconnect_on_startup ×N,
9:34–9:42", and (2) a note in CLAUDE.md that `--reload` restarts are
expected during development. Behavior unchanged; only the display and the
docs.

## 2026-07-11 — Cost estimation + right-size hint: median history, VRAM-keyed threshold

**Context (Prompt C):** show a pre-launch cost/runtime estimate for a job,
and a post-run utilization verdict with an optional "you could use a smaller
GPU" hint. Everything here is presentational and advisory: it reads existing
SQLite (launches, tasks) plus a new lightweight telemetry table, and never
touches the launch/termination path. It recommends; it never overrides a GPU
choice.

**Estimate — median of same template + same GPU type.** `estimate_job`
(estimates.py, a pure function) takes the durations of past *succeeded* runs
of this template on this instance type and reports the **median** minutes,
priced at the type's hourly rate (`minutes/60 * rate`).

- Median, not mean: run times are right-skewed (one stuck run at 4x the
  norm shouldn't drag the estimate up). The median is robust to that.
- Confidence tiers, surfaced in the UI so the number is never oversold:
  - `>= 3` matching runs -> **measured** (`MEASURED_MIN_RUNS = 3`).
  - `1-2` runs -> **rough** ("still learning"): real data, but too little
    to trust as a median.
  - `0` runs -> **rough**, falling back to a coarse per-template default
    (`DEFAULT_MINUTES`) explicitly labeled "no history yet".
  - Server templates (vllm-serve) have no fixed runtime -> **none**: we show
    "runs until you stop it, $X/hr" instead of a fake total.
- Timing is *already* persisted (task started_at/finished_at), so estimates
  sharpen automatically as history accrues. Nothing new to record for this.

**Right-size hint — keyed on PEAK VRAM, not average utilization.** The single
most important safety property: **a false "downsize" that OOMs the next run
destroys trust**, so the hint is deliberately hard to trigger.

- We key on **peak VRAM used / total VRAM**, because VRAM is what actually
  OOMs a job. Average SM utilization can look low on a memory-bound job that
  still needs every GB, so utilization is *shown* but never gates the hint.
- Threshold `RIGHT_SIZE_VRAM_FRACTION = 0.45`: the hint fires only if peak
  VRAM stayed at or below 45% of the card. Rationale: at <=45% peak, the job
  fits with room to spare on a card roughly half the size, so a smaller tier
  is genuinely plausible. A **gray zone** of 0.45-0.65 says "some headroom"
  but makes **no** downsize call, because a run peaking near 60% could exceed
  a half-size card once inputs grow. Above 0.65 the card was well used and we
  say nothing.
- Minimum evidence `MIN_SAMPLES_FOR_HINT = 5`: with fewer than 5 telemetry
  samples we refuse to call it ("limited telemetry"). A job could spike VRAM
  in a window we didn't sample; a handful of readings can't rule that out.
- The hint always names the observed peak and stays advisory ("you *could*
  try a smaller GPU"), never an instruction. Manifold does not change the
  selection.

**Telemetry persistence.** The verdict needs history, and metrics were
previously live-only. Added a `telemetry_samples` table and a dispatcher
sampler loop that records one sample per connected instance every
`telemetry.sample_seconds` (default 30s) by reading the existing sidecar over
the managed SSH connection. This is additive and off the launch path: no
guard, launch, or termination logic changed. Nothing new listens on the
instance (still sidecar-on-loopback only).

**Alternatives considered:** (1) gate the hint on average utilization —
rejected, it OOMs memory-bound jobs. (2) Mean instead of median — rejected,
skewed by stuck runs. (3) A single confidence-free number — rejected, it
would present a 1-run guess and a 20-run median as equally trustworthy. (4)
Auto-selecting the smaller GPU — rejected outright per the brief: recommend,
never override.

## 2026-07-12 — Queue-then-launch (auto-manage): v1 is sequential, sharing deferred

**Context (Prompt B):** let a user queue a job with NO instance running and
have Manifold own the whole lifecycle: wait for a slot, launch, run, sync
outputs, terminate. The zero-waste headline: a GPU exists only while there is
work for it.

**Decided:** ship v1 as a **sequential per-job lifecycle**, and defer
instance *sharing* (reusing one box across several compatible jobs) to a
follow-up. Each auto-managed job drives its own instance through:

    waiting -> launching -> ready -> running -> syncing -> terminating -> done

At most one auto-managed job holds the single-instance slot at a time; the
next waits its turn. A new dispatcher loop (`_auto_manage_loop`) advances one
job per tick and is **stateless across ticks** (it reads the job's lifecycle
from SQLite), so a backend restart resumes wherever the job left off.

**No guard is duplicated or bypassed — the queued path calls the SAME
functions the dashboard does.** Concretely:
- launch = `orchestrator.request_launch(...)` (budget, concurrency,
  region-filesystem match, capacity retry all apply, unchanged);
- dispatch = the existing `_task_loop` / `_run_task` (the job binds to its own
  instance, see below);
- sync = `orchestrator.sync_ephemeral(...)`;
- terminate = `orchestrator.terminate(..., force=False)` (the Phase 3 safety
  hook still runs).
The lifecycle loop is glue that sequences these; it contains no guard logic.

**Wait-vs-fail without re-deriving the guards.** `LaunchRejected` now carries
a `reason_code`. On a rejected launch the lifecycle classifies:
- `concurrency` (the single slot is busy, e.g. a manual instance is up) ->
  stay in `waiting`, retry next tick. Never fails the job.
- `budget` / `validation` / `mode` -> can never be admitted -> fail the job
  with the guard's own message (Gate B: an over-budget job is rejected with a
  clear reason and NO instance is ever created).
The classification reads only which check refused; it does not recompute the
budget or concurrency math (single source of truth stays in the orchestrator).

**Dispatch binding.** The dispatcher previously ran any queued task on the
first connected instance. That is now `_pick_dispatchable`, which binds:
- an auto-managed job runs ONLY on the instance its own lifecycle launched,
  and only once that instance is `ready` (connected);
- a manual job runs on any connected instance NOT owned by an auto-managed
  lifecycle.
So a manual job never lands on a box about to be torn down, and an
auto-managed job never hijacks a manually launched box. Scanning (not just
the oldest task) stops a waiting job of one kind from head-of-line-blocking a
ready job of the other.

**Every state transition is audited** with the job id and (once it exists) the
instance id: `auto_manage_launching/ready/running/syncing/terminating/done`,
plus `auto_manage_waiting`, `auto_manage_failed`, `auto_manage_cancelled`,
`auto_manage_terminate_blocked`.

**Alternatives:** concurrent tasks per instance (already rejected,
DECISIONS.md 2026-07-11 "One task at a time"); a separate dispatch path for
auto jobs (would duplicate the well-tested `_run_task`); failing a job the
moment a slot is busy (wrong: the slot frees, so waiting is correct).

**Why sequential first:** under `max_concurrent_instances = 1` the two
existing invariants (one instance, one task at a time) already serialize
everything. Sequential per-job lifecycle drops onto that cleanly and is
provably safe. Sharing needs a compatibility + drain + ownership model and a
few semantic calls (below), so it was split out deliberately (James approved
this split).

## 2026-07-12 — Auto-manage vs idle-termination and keep-alive: the lifecycle owns teardown

**Decided:** an instance an in-flight auto-managed job owns is **exempt from
the idle loop and the manual keep-alive switch**. `_check_idle` skips every
instance in `db.auto_managed_instance_ids()` (jobs whose lifecycle is
launching/ready/running/syncing/terminating), keep-alive or not.

**Why:** the auto-manage lifecycle already owns teardown (sync -> terminate).
If the idle loop also tried to terminate the box, the two would race during
the windows where no task is "running" (between `ready` and dispatch, and
during `syncing`/`terminating`). Skipping owned instances makes the lifecycle
the single terminator. The idle loop remains the **backstop**: the moment a
job reaches a terminal lifecycle it drops out of the owned set, so a box that
somehow outlives its lifecycle still gets reaped by idle. Keep-alive stays a
manual-instance concept; an ephemeral auto-managed box is not something you
keep alive.

**Termination blocked = surface, never force (differs from the idle flow).**
The idle loop, being fully unattended, does sync-then-force. Auto-manage does
NOT: per the brief, after the intended sync it calls `terminate(force=False)`,
and if the safety hook still finds unpersisted files it **surfaces the block
and leaves the box running for review** (audited as
`auto_manage_terminate_blocked`), exactly like the manual flow. The lifecycle
loop keeps retrying `force=False`, so the instant the user syncs/clears the
files (or terminates manually) the job completes on its own. A false
force-terminate here would be an unattended data-loss path, which is exactly
what the hook exists to prevent.

## 2026-07-12 — Recorded answers for the instance-SHARING follow-up (pending confirmation)

The sharing optimization (reuse one box across compatible auto-managed jobs,
terminate after the last) was deferred from v1. James asked to record the
answers to the three semantic questions here so the follow-up has a spec.
These are my recommended defaults, consistent with this codebase's
conservative posture; confirm or amend before building sharing.

- **(a) What is "compatible"?** Byte-identical: same instance_type AND region
  AND filesystem. It is the only definition that is provably safe without a
  GPU-substitutability matrix (is an A100 an acceptable stand-in for an A10
  job? memory, price, and availability all differ). Looser matching can come
  later; exact-match is the safe first step.
- **(b) May auto-manage reuse or tear down a MANUALLY launched instance?**
  No. An auto-managed job only ever reuses or terminates instances its own
  lifecycle launched (ownership tracked on the launch row). Tearing down a
  human's box would be surprising and destructive; silently commandeering one
  blurs ownership. v1 already behaves this way (an auto job waits for a slot
  rather than touching a manual box).
- **(c) A compatible job arrives while the box is DRAINING toward teardown —
  reclaim it?** Yes, but only in the narrow window before `terminate()` is
  actually in flight: if a compatible job appears while the instance is still
  up and teardown has not been called, cancel the teardown and reclaim the box
  (that is the whole efficiency win). Once `terminate()` has been invoked, let
  it complete and the new job launches fresh, rather than racing an in-flight
  termination.

Implementing sharing on these answers still requires: a compatibility match in
the dispatcher, an ownership flag + a drain/reference-count so teardown fires
only after the LAST compatible job, and idle-loop reconciliation for the
shared case. That is the "more than trivial work" that kept it out of v1.

## 2026-07-12 — Template audit: verify every image against its registry, findings per template

**Context (Phase 25):** whisper-batch failed live — `docker pull
ghcr.io/speaches-ai/faster-whisper-server:latest-cuda` returned "denied"
(exit 125), meaning the GPU booted and billed just to discover the image was
gone. vllm-serve had also failed live (exit -1 AFTER a successful pull).
James asked for a full audit of all 8 templates and a rewrite of anything
unverifiable.

**Method:** no docker daemon was available locally, so every image was
checked against its registry's OCI/Docker v2 HTTP API (manifest GET with
anonymous token exchange) — which is the stronger check anyway, because it
verifies exactly what an instance's anonymous `docker pull` sees. For
vllm-serve, the image's config blob was fetched to read its REAL entrypoint
rather than guessing from docs.

**Findings, per template (audited 2026-07-12):**
- axolotl-finetune / `axolotlai/axolotl:main-latest` — EXISTS, kept.
- gpu-smoke / `nvidia/cuda:12.4.1-base-ubuntu22.04` — EXISTS, kept.
- llm-synthesize / `python:3.11-slim` — EXISTS; but the template had the
  env-script expansion bug (next entry): it was a SILENT NO-OP.
- script-run / `python:3.11-slim` — EXISTS; same silent no-op bug.
- sdxl-generate / `huggingface/transformers-pytorch-gpu:latest` — EXISTS;
  script survived but multi-word prompts were split apart (same entry).
- tao-train / `nvcr.io/nvidia/tao/tao-toolkit:5.5.0-tf2.11.0` — MISSING
  (nvcr 404). The live tag list shows the 5.5.0 TF2 image is `5.5.0-tf2`;
  the `-tf2.11.0` naming stopped at 5.0.0. Fixed to `5.5.0-tf2` (verified).
- vllm-serve / `vllm/vllm-openai:latest` — image EXISTS; the command was
  wrong. The image config's entrypoint is `["vllm", "serve"]`, and
  `vllm serve` takes the model as a POSITIONAL argument. The template passed
  `--model <id>`, which the CLI rejects — hence exit -1 after a good pull.
  Fixed: model is now positional; `--max-model-len/--port` stay as flags.
- whisper-batch / `ghcr.io/speaches-ai/faster-whisper-server:latest-cuda` —
  NOT ANONYMOUSLY PULLABLE (ghcr denied), matching the live failure, and its
  `python -m scripts.batch_transcribe` entrypoint was never verifiable.
  REWRITTEN (next-but-one entry).

**Rule going forward:** never reference an image (or an image's internal
script) that has not been verified against its registry. The backend now
enforces the image half automatically (preflight entry below).

## 2026-07-12 — Env-script templates: the host shell was eating $PYCODE/$RUNNER

**Found during the audit (worse than the image bugs):** three templates ship
their program in an env var and run it via the container shell. The rendered
`docker run ... -e 'PYCODE=...' image python -c "$PYCODE" args` is executed
by the INSTANCE's shell — where PYCODE is not set. The double-quoted
`"$PYCODE"` was expanded to empty ON THE HOST, so:
- llm-synthesize ran `python -c '' ...` — a SILENT NO-OP that exits 0 and
  reports "succeeded" while synthesizing nothing;
- script-run ran `bash -c '' ...` — same silent no-op;
- sdxl-generate kept its script (it was inside single quotes) but had
  `{{prompt}}` INSIDE those quotes, so a multi-word prompt was word-split
  into separate argv entries (prompt "a red cat" generated images of "a").

**Fix — one pattern for all env-script templates:**
    command: bash -c '<script using $VAR and "$@">' argv0 {{param}} {{param}}
The single quotes stop the host shell from touching `$VAR` (the container
shell expands it, where the env IS set); parameters stay at the top level
where the dispatcher's shlex-quoting is correct, and reach the script as
intact positional args via `"$@"`. script-run uses `eval "$RUNNER"` inside
the quotes (eval sees the same positional params).

**Enforcement:** tests/test_template_quoting.py simulates the instance shell
with a fake `docker` that records its argv, then asserts (a) the `-e VAR=`
body is present, (b) the container command still contains the LITERAL $VAR
(host did not expand it), and (c) a multi-word parameter arrives as ONE
argument. Any regression to the unquoted form fails CI.

**Why the old form looked fine:** the previous script-run comment claimed
putting params inside `bash -c '...'` "collides the quotes" — true — but the
"fix" moved the VAR reference outside the single quotes, which is exactly
what handed it to the host shell. The empirical fake-docker simulation is
what caught it; eyeballing quoting did not.

## 2026-07-12 — whisper-batch rewritten: verified base + a transcriber Manifold owns

**Decided:** whisper-batch now runs on
`pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime` (manifest verified; cuDNN 9
is what CTranslate2, faster-whisper's backend, needs on CUDA 12), does
`pip install faster-whisper` at container start (~30s), and runs a ~65-line
transcriber script that LIVES IN THE TEMPLATE (PYCODE env), not inside a
third-party image. Same contract as before: reads /data/input, writes
<name>.srt + <name>.json to /data/output (persistent transcripts/), HF cache
on persistent storage, per-file failures logged but never fatal, exits
nonzero if nothing transcribed.

**Alternatives:** another prebuilt whisper image (same third-party risk that
just burned us — the speaches image vanished/went private under ghcr);
building and hosting our own image (infrastructure Manifold does not have);
WhisperX (heavier deps for no requirement).

**Why:** the base image is an official, verifiable artifact; the script is
in-repo where it can be reviewed, tested (its syntax is ast-checked in CI
via the quoting test loading the template) and fixed without waiting on
anyone's registry. The ~30s pip install per run is the explicit price of
owning the moving part; pin a custom image later if whisper becomes hot.

## 2026-07-12 — Image preflight: never boot a GPU to discover a missing image

**Decided:** before an auto-managed job launches (and before ANY job is
dispatched), the backend verifies the template's image manifest exists in
its registry (`app/image_checker.py`, Registry v2 API with anonymous token
exchange — exactly what the instance's anonymous `docker pull` does). A
definitively-missing image (404, or 403/denied where registries hide
missing-vs-private) fails the job immediately as
"image not found: <image>" with ZERO launches; the manual path fails it
before any docker command reaches the instance, audited as
`task_image_missing`.

**Fail-open on anything undetermined** (network blip, a registry that will
not answer anonymous existence queries): the job proceeds and, worst case,
dies at docker pull on the instance — exactly the pre-preflight behavior, no
worse. A flaky checker must never become a wall in front of every launch.
Known limit: ghcr.io returns the same opaque denial for missing and private
repos AND refuses anonymous token exchange for nonexistent ones, so ghcr
images are usually "undetermined" (fail-open). docker.io and nvcr.io — every
image the templates use today — give definitive answers. Results are cached
5 minutes per image.

**Wiring:** mock mode injects MockImageChecker (offline, approves everything,
overridable per test); production wiring gets RealImageChecker; a test
harness that injects a lambda_client without a checker gets preflight OFF so
the suite can never touch the network by accident.

**Also fixed while wiring:** a dispatch-time failure (missing image, bad
parameters) finishes an auto-managed job's task WITHOUT it ever reaching
'running'. The lifecycle previously idled at 'ready' waiting for dispatch —
leaving the launched box up forever, and the idle loop deliberately skips
auto-owned instances. 'ready' now runs the same settled-check as 'running',
so the lifecycle still syncs and terminates the box (test:
test_auto_job_torn_down_after_dispatch_time_failure).

## 2026-07-12 — Chat tools: the served model gets guarded arms

**Context:** the in-dashboard chat relayed text only, so the model (which is
stateless text-in/text-out) could not see the filesystem or start work —
James expected "one synergetic system where the instance runs the model and
the model can talk to the instance and filebase".

**Decided:** the chat endpoint gains a tools mode (`tools: true`, the panel's
default). The backend runs the loop, Autopilot-style: the model replies with
one JSON action, the backend executes it through EXISTING guarded paths and
feeds the observation back; plain text ends the loop as the final answer.
Tool surface (chat_tools.py): list_files / read_file (sidecar + managed SSH,
confined to the file-navigator roots, 16 KB head-read cap), list_templates /
run_job (the same coerce + queue path as everyone else), get_job_status /
get_job_logs. No shell, no HTTP, no launch/terminate from chat — that stays
Autopilot's job with its run ledger and step caps. Every tool call is
audited (actor "chat"). Max 8 tool calls per user message.

**Trade-off:** tools mode answers arrive turn-at-once (the backend must see
the full reply to detect a tool call); the Tools toggle off restores pure
token streaming. UI also gained: tool-call progress lines, a vertically
resizable conversation area (CSS resize-y), and image attach via drag/drop
or button — sent as OpenAI image_url content parts, which only vision models
(e.g. Qwen2.5-VL) can read; the panel says so next to pending images.

## 2026-07-12 — Model presets refreshed (July 2026) + tensor_parallel for cluster serves

**Decided:** replaced the Qwen2.5-era preset catalog with current popular
open-weight models, every repo id verified against the HF API on 2026-07-12
(exists, gated=False, so vllm-openai pulls with no token): Qwen3-4B/8B/14B,
openai/gpt-oss-20b/120b, Qwen3.6-27B (+FP8), Qwen3.6-35B-A3B-FP8,
tencent/Hy3-FP8, zai-org/GLM-5.2-FP8. Tiers map to Lambda's actual GPUs:
A10 24GB, A100 40GB, H100 80GB, 8x H100 (640GB), 8x B200 (1.4TB).

**Sizing corrections vs the request:** Qwen3.6 does fit a single H100
(27B bf16) and even an A100 40GB (27B-FP8) — as asked. But Hy3 is a 295B
MoE: it does NOT fit one H100; the FP8 checkpoint (~300GB) needs the 8x H100
cluster. GLM-5.2 is 744B (~750GB FP8): not one B200, the 8x B200 cluster.
Presets say so in their tier/notes rather than offering a serve that OOMs.

**Enabler:** vllm-serve gained a `tensor_parallel` parameter (default 1 —
existing single-GPU serves unchanged) appended as --tensor-parallel-size;
cluster presets carry {"tensor_parallel": 8} and the Jobs page seeds a
preset's extra parameters into the form alongside the model id.

## 2026-07-12 — Night hardening pass: dark theme as tokens, distillation loop closed, SGLang added

**Dark theme via palette remap, one file.** The dashboard was authored light
and users saw it through the browser's forced dark mode (muddy, accidental).
Now globals.css IS the theme: Tailwind v4 @theme re-points the palette the
components already use, so class names keep their ROLE (white = card
surface, zinc-50 = canvas, zinc-900 = primary text / inverted buttons,
zinc-950 = terminal blocks; accent -50/-100/-200 tints become dark glass,
-700/-800/-900 text lightened). Zero component sweep; the ~10 places where
the role genuinely flips (light text on log blocks, white text on saturated
buttons, decorative separators) were hand-fixed. Fonts: Space Grotesk (UI) +
JetBrains Mono (terminal surfaces, section headers, wordmark) via next/font.
One brand accent (teal phosphor) used only for selection, focus, the canvas
glow, and the wordmark cursor. Alternatives: hand-editing every component
(hundreds of class changes, drift forever) or CSS filter inversion (breaks
images and shadows). The remap centralizes taste per the design-tokens rule.

**Distillation loop closed (teacher -> data -> student).** llm-synthesize
gained output_format=alpaca ({"instruction","input","output"} rows) and
axolotl-finetune now mounts synthesized/ read-only at /data/synthesized, so
the teacher's output trains the student with zero file shuffling. The whole
walk (serve teacher, synthesize, LoRA fine-tune, use the adapter) is
docs/distill-your-own-model.md, with honest costs and caveats (gated
students need an HF token Manifold does not pass yet).

**sglang-serve template.** SGLang is the other major OpenAI-compatible
serving engine (LMSYS); its RadixAttention reuses shared prompt prefixes
automatically, making it faster than vLLM for agent/RAG workloads that
resend long system prompts (vLLM stays the default for one-off prompts and
has the broader hardware support). Because both expose the same API, the
template is a sibling of vllm-serve: same loopback-only publish, same HF
cache mount, same ports block - so chat, the /v1 proxy, Autopilot, and
llm-synthesize work against it unchanged (find_serving_task keys on
ports + model_id, both present). Image + entrypoint verified against the
registry (nvidia passthrough entrypoint; full launch command supplied).

## 2026-07-12 — Desktop packaging: Tauri shell around ONE frozen process

**Context:** turn localhost Manifold into a downloadable .dmg/.msi. James
chose Tauri v2 over pywebview (no installer/updater story) and Electron
(~200MB Chromium for no gain).

**Shape (two layers, one process of substance):**
- The dashboard already prerendered every route statically, so
  `output: "export"` makes it plain files that FastAPI serves at `/`
  (mounted LAST; API routes win). No Node at runtime.
- PyInstaller freezes backend + templates/ + sidecar + config default + the
  exported UI into one ~39MB binary (`backend/desktop.py` entrypoint,
  loopback-only). This binary alone IS the product; the shell is chrome.
- Tauri spawns it as a sidecar, shows a themed splash until the port
  answers, navigates the native window to it, and kills it on exit. If the
  port already serves (dev backend running), it reuses instead of spawning
  a duplicate.

**Path split that makes packaging safe** (config.py): RESOURCE_ROOT
(read-only bundle assets; sys._MEIPASS when frozen) vs DATA_ROOT (mutable
state: .env, config.yaml, manifold.db, host_keys.json) which moves to
~/Library/Application Support/Manifold (mac) / %APPDATA%\Manifold (win).
First run scaffolds the dir and seeds config.yaml from the bundled default.
Development behavior is byte-identical (both roots = repo root), which is
why the whole suite passes untouched.

**Frontend URL detection** (`dashboard/lib/backend.ts`): one source of
truth. Same-origin when served by the backend, localhost:8000 under the
:3000 dev server, NEXT_PUBLIC_API_URL overrides. Replaced four scattered
copies of the localhost fallback (api.ts, ChatPanel, TerminalPanel,
TelemetryChart) - the desktop app breaks without this, since its origin is
127.0.0.1:8000 itself.

**Receipts:** frozen binary booted standalone: every dashboard route 200,
all 9 templates loaded from the bundle, fresh DATA_ROOT scaffolded with
seeded config + empty db. CI (.github/workflows/desktop.yml) builds dmg
(macos-14) + msi (windows-2022) on v* tags. Local dmg note: Tauri's
bundle_dmg.sh drives Finder via AppleScript and fails headless; plain
`hdiutil create` produces the same artifact without the styled window.

**Orphan bug found at the gate:** quitting the app killed only PyInstaller's
bootloader; the real server survived and held :8000 forever. Fix: the shell
sets MANIFOLD_PARENT_WATCHDOG=1 and the backend self-terminates on stdin
EOF (its stdin is a pipe from the shell, so EOF = the shell died, however
it died). Opt-in via env so terminal runs never self-terminate. Retested:
launch -> serve -> quit -> zero processes, port released.

**Honest limits:** bundles are UNSIGNED until Apple Developer ($99/yr) /
Authenticode accounts exist - Gatekeeper/SmartScreen will warn; the
workflow has the hook points. No auto-updater until signing lands (unsigned
updates are unsafe). Windows build is CI-defined but untested on real
hardware. MCP stays a dev-checkout feature for now.

## 2026-07-12 — Sharing the desktop app: GitHub Releases on a version tag, not a committed binary

**Decided:** the `.dmg`/`.msi` are shared via a GitHub Release (a new
`release` job in `.github/workflows/desktop.yml`, gated on `refs/tags/v*`
and running after both platform builds), not by committing them into the
repo. Pushing a tag (`git tag v0.1.0 && git push origin v0.1.0`) makes CI
attach both installers to one Release, giving a stable public URL
(`/releases/latest`) that needs no GitHub login - unlike the existing
`upload-artifact` step, which is login-gated and expires in 90 days.

**Why not commit the binaries:** git tracks line diffs; a 40MB+ binary has
none, so every commit that touches it (and every future clone of the repo)
carries the full weight forever, with no way to shrink history later
short of a rewrite. Releases are the purpose-built mechanism - versioned,
downloadable, outside the tree that `git clone` pulls by default.

**Repo visibility check (2026-07-12):** confirmed `Somnora/Manifold-` is
already public, and confirmed no secret ever entered git history — `.env`,
`manifold.db`, `host_keys.json` are gitignored and were never tracked.
Sharing the repo link was already safe before this change.

## 2026-07-13 — Per-instance parallel dispatch (supersedes "one task at a time")

**Found at James's mock test pass.** Three compounding problems: (1) the
dispatcher awaited each job INLINE and refused to dispatch while anything
was running — so a server job (vllm-serve streams for its lifetime) froze
every other job forever, contradicting the documented serve+synthesize
pipeline; (2) with several GPUs there was no way to say which box a job
should run on; (3) the mock SSH process exited instantly for server jobs, so
vllm-serve went 'succeeded' in a second and chat/autopilot had no brain to
find — masking bug (1) in every demo and test.

**Decided:**
- Dispatch spawns each job as its own asyncio task (`_dispatching` map
  guards the queued->running gap and lets stop() cancel). Instances run
  their work independently.
- Per-instance concurrency rule: one BATCH task at a time (GPU contention),
  one SERVER at a time (its port), but server+batch coexist — that is the
  sanctioned pipeline. The 2026-07-11 "one task at a time" entry is
  superseded; its rationale (serialized batch work per GPU) survives as the
  per-instance batch rule.
- Manual jobs accept `target_instance_id` (Jobs page "Run on" picker);
  untargeted jobs take the first free non-auto-owned instance. Auto-managed
  jobs still bind only to their own launched box.
- Idle: a running task pins ITS OWN instance only — a job on box A no
  longer keeps an idle box B billing (previously any running task blocked
  ALL idle termination). Auto-owned instances stay lifecycle-governed.
- Mock fidelity: mock server processes (commands publishing ports) stay
  RUNNING until the connection closes (`MockSSHConnection.close()` now EOFs
  open streams so nothing hangs); and mock mode always forces its own
  registered ssh key — a real key name in config.yaml made every
  auto-manage launch fail in the packaged demo.

**Multi-GPU how-to:** raise `guardrails.max_concurrent_instances` (and mind
`max_hourly_spend_usd`) in config.yaml; the guard stays deliberate.

**Known mock-demo quirk (spec-correct):** the demo sidecar reports two
canned unpersisted files, so an auto-manage teardown parks at 'terminating'
with the reason on the job card — the safety hook doing exactly what
Prompt B specified (never force). Resolve from the instance card (sync /
terminate) and the job completes.

## 2026-07-13 — The local hub: external brains, approval gates, local terminal

**Context:** James's north star - "one synergetic system": local models,
frontier APIs, and GPU-served models all first-class drivers of Manifold,
plus a terminal on the local machine, plus approval-gated agent spending.

**Brains registry (brains.py).** One abstraction, three kinds:
`instance:` (served on a Manifold GPU, the original), `local:` (Ollama /
LM Studio on this machine, auto-detected by probing /v1/models on their
standard loopback ports), `api:` (Anthropic/OpenAI/Gemini via their
OpenAI-compatible endpoints; offered ONLY when the key env var is set, so
there is never a selectable-but-broken option). All three expose the same
chat interface (ExternalBrainClient duck-types ModelClient), so the agent
loop is brain-agnostic: Autopilot.start_run takes a client factory, and
the run's brain ref is stored in the existing brain_instance_id column
(strings like "local:ollama/llama3.1" - no schema migration).
**The safety model is deliberately unchanged by the brain:** same action
allowlist, same guards, same caps, same audit - a frontier model gets no
more power than a 4B local one.

**Approval gates.** Runs started with require_approval pause launch_gpu /
run_job / terminate_instance as a `pending` row in a new approvals table;
the agent loop polls until a human decision. Deny returns "DENIED by the
user" AS DATA (the model adapts - test-proven, and nothing executes);
approve falls through to the normal guarded execution; a timeout
(autopilot.approval_timeout_seconds, 600s) auto-denies so an unattended
run never spends. decide_approval uses a status='pending' WHERE guard, so
a double-click or race decides exactly once. Gated set choice: the three
actions that spend money or destroy state; reads stay free because an
approval prompt per get_job_status would make the feature unusable.

**Local terminal.** WS /local/terminal forks a login shell in a pty and
speaks the exact wire protocol of the instance terminal (one generalized
TerminalPanel drives both). Threat model: the backend is loopback-only,
but browsers allow cross-origin WebSockets that CORS middleware does NOT
cover - so the endpoint enforces a strict Origin allowlist (localhost /
127.0.0.1) before accepting, plus a config kill switch
(hub.local_terminal). POSIX-only for now; Windows says so instead of
half-working. Audited on open. Alternatives: no local terminal (but the
hub's whole point is one pane of glass), an allow-any-origin socket
(would let any website you visit run shell commands - rejected).

**Hub page.** The meeting point: local terminal, live brains list with
kind badges, pending approvals. Autopilot's picker now reads the same
/brains registry instead of probing instances itself.

## 2026-07-13 — Subscription brains via CLI delegation, not spoofed OAuth

**Asked:** OAuth login for frontier models instead of API keys.

**Decided:** `cli:` brains. The user logs into the provider's own CLI once
(claude / codex / gemini - each ships its own official OAuth flow), and
Manifold invokes that CLI as a subprocess per turn (CliBrainClient: argv
list, no shell, cwd = an empty scratch dir so an agentic CLI has nothing
to poke at, hard timeout, stderr surfaced with an "is it logged in?"
hint). Detection = executable on PATH; the registry offers what exists.
Invocations verified against the installed CLIs' actual flags: claude
`-p --output-format json` (.result), codex `exec --skip-git-repo-check
-s read-only --output-last-message <tmp>`, gemini `-p -o text`.

**Why not real OAuth:** the providers' subscription OAuth client ids
belong to their own apps; a third-party impersonating Claude Code's or
Codex's client id violates provider ToS and risks the user's ACCOUNT.
The sanctioned third-party programs (Anthropic's and OpenAI's "sign in
with your subscription") are preview/waitlist and require registering the
app for its own client id - noted as the future replacement. CLI
delegation gives the same UX today (log in once with the provider's own
flow, no API key, subscription billing) with zero ToS exposure and zero
token handling in Manifold.

**Unchanged:** the brain safety model. A CLI brain gets the same action
allowlist, guards, caps, approval gates, and audit as every other brain.

## 2026-07-13 — Unattended safety: per-action approvals, notifications, data rescue

Three asks, one theme: make a run that nobody is watching SAFE. Autopilot
already had guards; what it lacked was a way to be away from the keyboard
without either losing money or losing data.

### Approvals are now per-action, and the default gates launches only

**Decided:** `ApprovalPrefs{launch_gpu: true, run_job: false,
terminate_instance: false}` (preferences.py), overridable per run via
`approve_actions` on POST /autopilot/runs. The old boolean
`require_approval` still works (true = gate everything) and old runs still
read back correctly (`agent_runs.approval_policy` is additive; the boolean
column is kept as the derived "is anything gated" flag).

**Why launch-only, and why this is not timidity:** an approval nobody
answers AUTO-DENIES after `autopilot.approval_timeout_seconds`. So gating an
action means "if I am away from my desk, this action does not happen":

| gated action | what a no-answer denial costs |
|---|---|
| `launch_gpu` | nothing. No GPU starts, $0 spent. |
| `terminate_instance` | **the GPU keeps billing** while the approval rots. |
| `run_job` | a GPU you are already paying for sits idle. |

Gating a shutdown therefore burns money exactly when you are away — which is
when autopilot runs. It is off by default, the UI warns when you switch it
on, and `test_default_policy_does_not_gate_termination` exists so nobody
"helpfully" flips it later. The launch is the decision that needs a human;
the shutdown is the one that must not wait for one.

**Alternative rejected:** making an expired approval AUTO-APPROVE for
terminate (so the wallet is safe either way). Rejected: an approval gate
whose failure mode is "does it anyway" is not a gate. Better to not gate the
action than to pretend to.

### Notifications: a pause nobody hears about is a stall, not a safeguard

**Decided:** a `notifications` table + `NotificationCenter`, with five
independently-toggled kinds (approval_requested, job_succeeded, job_failed,
run_finished, data_transferred). Two channels: an in-app bell (always
recorded, so history survives) and a real OS notification (macOS
`osascript`, Linux `notify-send`) so it reaches you in another app — which
is the entire point. The OS sender is INJECTED (`notification_sender`), so
tests record instead of spraying the developer's Notification Center, and
mock mode is silent.

Every job completion in the dispatcher was funnelled through one
`_finish_task`, so no completion path — bad parameters, missing image, lost
SSH, container exit, auto-manage failure — can finish silently. That funnel
is the feature; the notification is just what hangs off it.

**Preferences live in SQLite, not config.yaml.** config.yaml is a file a
human edits, with comments and ordering; a UI that rewrote it would eat
both. So config.yaml supplies the DEFAULTS and the `preferences` table holds
what the user changed in Settings. `preferences_from_dict` ignores unknown
keys and clamps illegal enums, so neither a hand-edited YAML nor a hostile
PUT can produce an unstartable app.

### Termination now RESCUES data instead of refusing

**Changed a Phase 3 contract deliberately.** The safety hook used to REFUSE
to terminate while valuable files sat on the instance's ephemeral disk. That
is the right answer with a human watching and the wrong one at 3am: an
unattended run hits the 409, the GPU keeps billing, and nobody sees it. Each
caller had also reimplemented its own sync-then-force dance (the idle loop
did; the MCP agent had to be taught to).

`Orchestrator.terminate(force=False)` now: asks the sidecar what is on the
scratch disk → RESCUES it per the data-safety policy → and refuses only if
something could not be saved. `TerminationBlocked.files` therefore changed
meaning, from "files that exist" to "files still at risk", and it now carries
the report of what WAS saved. `force=true` is unchanged: the explicit burn-it.

The user's proposed menu was four options ("all files to local", "all to
filebase", "synthesized only to local", "synthesized only to filebase").
That is a cross-product of two independent questions, so it is modelled as
two:

- **WHERE**: `to_filesystem` (rsync to the Lambda volume — datacenter-local,
  so fast, free, and it covers the whole scratch dir at once) and/or
  `to_local` (SFTP down the managed connection to this machine, which costs
  real transfer time while the GPU bills, so it is off by default and
  budgeted by `max_local_gib`, smallest-file-first).
- **WHAT**: `scope: all | outputs` (outputs = files under `outputs/`, the
  deliverable convention — pull the results, leave the 40 GB checkpoint).

And the question the menu missed, which is the one that actually matters:
**what if a file cannot be saved?** `if_unsaveable: block | terminate`.
Default `block`: keep the instance alive with the data intact and ping the
user. Data loss is permanent; a billing hour is not. `terminate` is
available for people who mean it, and is recorded in the audit log
(`terminate_data_lost`) and the notification — never silent.

**Honest reporting is a requirement, not a nicety.** A rescue that quietly
drops files is worse than no rescue, because it lies. Anything skipped
(scope, budget, transfer failure) is reported with its reason and counted as
unsaved, which is what the block keys on. Downloads go to a `.part` file and
are renamed on completion, so an interrupted rescue cannot leave a truncated
file that looks saved. Paths come FROM the instance, so both the remote and
local sides are normalized and confined (`data_safety.remote_path` /
`local_path`) — a hostile sidecar cannot traverse out of the scratch root or
out of the rescue directory.

**The decisions are pure.** `data_safety.py` does no I/O: scope selection,
transfer budgeting, and path confinement are testable without an instance, an
SSH server, or a byte of network. The transport lives in the orchestrator,
which owns the connections.

## 2026-07-14 — Phase 38: nav consolidation + ambient burn rate

**Problem:** 8 top-level pages for this scope, with two overlapping pairs.
Hub and Autopilot both showed brains and both rendered ApprovalsPanel; Agent
Activity and History were both "what happened" pages (audit trail vs cost
ledger). And the hourly burn — the single most important number in the
product — was visible on exactly one page.

**Decided (frontend only, zero backend changes):**

- **Hub merged into Autopilot.** Brains and approvals live where runs start.
  The Hub's third feature, the local terminal, is a TOOL, not a PLACE: it
  became a bottom drawer toggled by the `>_` header button, available on
  every page. Once opened it stays MOUNTED and is only hidden with CSS, so
  closing the drawer does not kill the shell — navigate anywhere, reopen,
  and the session (history, cwd, running command) is where you left it.
- **Agent Activity merged into History as the "Activity" page** with
  Spend / Audit tabs. The audit table moved verbatim into
  components/AuditLog.tsx; deep link `/history?tab=audit`.
- **Old URLs keep working**: /hub and /agents are client redirect stubs
  (static export cannot do server redirects), so desktop-app bookmarks and
  doc links don't break.
- **BurnChip in the header** next to the bell on every page: sum of running
  instances' hourly rates, amber + pulsing when > $0, click-through to
  Instances where the terminate buttons are. Renders nothing while the
  backend is unreachable — an unknown must not display as a reassuring $0.

Nav went 8 -> 6: Instances · Jobs · Storage | Autopilot | Activity ·
Settings. Deliberately NOT touched: the Jobs page's density is earned (one
coherent workflow); Storage's region limitation is a separate problem.

## 2026-07-14 — Phase 39: power without training wheels

Four asks from live testing, one theme: advanced users (and their agents)
should never hit a wall that exists only for ceremony.

**Guardrail NUMBERS moved to Settings; the guards did not move.** The
concurrency/budget guards stay in orchestrator.request_launch (hard rule),
but the limits they enforce now read through the preference store:
Settings -> Spending guardrails, 0/blank = config.yaml default. Raising the
instance limit no longer needs a YAML edit + restart. Guard rejection
messages point at Settings instead of config.yaml.

**Filesystem is optional at launch.** filesystem="" launches a scratch-only
instance in ANY region with capacity - previously a region without one of
your filesystems was unlaunchable. Consequences fall out of existing
machinery, deliberately: jobs mounting {persistent} fail with a clear
reason; sync has nowhere to go, so the rescue reports sync_error and the
data-safety policy decides (default: block termination while unsaved files
exist; to_local download is the net). The launch form says all of this in
amber BEFORE the click. No new code path touches the guards.

**Custom job templates - the "skills" model.** User/agent-authored YAML in
custom-templates/ under the data dir, loaded alongside the bundled set into
ONE shared dict that reloads in place (dispatcher/autopilot/brains all see
new templates with no restart). Validated by the SAME parser and mount jail
as bundled templates: a custom template is a recipe, not an escape hatch
(test: a template mounting /etc is rejected with 422). User templates win
name collisions; deleting one restores the bundled original. Files, not DB
rows - portable, committable, backupable. Editor on the Jobs page; agents
get MCP save_template/delete_template. The design goal is agent-as-
scaffolding: prove a workflow with the agent once, save it, rerun it
forever as a form - no tokens, no re-explaining.

**run_command: SSH parity, audited.** The honest answer to "do agents get
the same tools as SSH?" used to be "no". Now the difference is visibility,
not capability: POST /instances/{id}/run (MCP run_command) runs one shell
command over the managed connection, hard-timeout (<=600s), output capped,
audited with exit code, idle-clock touched. Guards still bind everything.
The instruction to give agents stays: use the manifold tools, not ssh -
same power, but on the record.

Docs: codex + gemini MCP registration blocks and the parity section in
mcp-setup.md; new custom-templates.md authoring guide.

## 2026-07-14 — Phase 39 additions from the live test pass

**The dock generalized to all instance panels.** Chat, recent Files, and the
file Browser open in the dock (tabs or split, bottom or right) instead of
unrolling inside the instance card - one surface for everything
instance-scoped, and it survives page navigation. Multiple shells: a "+"
on any terminal tab duplicates it (fresh pty, numbered label), and a
"+ >_" button adds more Local Machine tabs. Local tab renamed
"Local Machine".

**Instance rename is a local overlay.** Lambda fixes an instance's name at
launch; instance_names in SQLite overlays it everywhere Manifold shows
names. Empty restores Lambda's. No Lambda API call involved.

**Unlimited autopilot steps: max_steps=0.** The unlimited toggle stores 0
and the loop switches to itertools.count - the run ends only via
done/cancel/failure. Deliberately NOT a bigger cap: the money is already
bounded by guards + approval gates + the wait cap + consecutive-failure
kill; the step cap only bounded TURNS, and for long unattended goals that
was the artificial wall. Finite runs keep the 50-step hard cap.

**Autopilot can author templates mid-run (save_template action).** Same
validated path as the Jobs page and MCP (one save_custom_template_text
helper in the app factory feeds all three), so the mount jail binds the
agent identically. What a run saves persists for the user - agent as
scaffolding, again.

**Template editor contrast bug:** the dark theme remaps the zinc scale, so
text-zinc-100 on bg-zinc-950 was ink-on-ink. Terminal-style editors now use
the terminal's own literal hex palette, not remapped tokens.

---

## 2026-07-14 — Phase 40: field hardening for slow real boots

From a field report by an agent orchestrator that ran the sprite-to-3D
workflow on real GPUs. Five backend fixes, all against the same failure:
large SXM4 instances take much longer to boot than the code assumed.

**Boot timeout 900s -> 2400s (config default AND config.yaml).** 15 minutes
cut off real launches that were still booting on Lambda's side; SXM4/large
multi-GPU boots were observed at 15-30+ min. 2400s (40 min) covers the worst
case with headroom. This is a ceiling, not a wait we always incur.

**The boot waiter now survives a backend restart (--reload).** The launch
pipeline runs in an in-memory asyncio task; a `--reload` restart (every
backend file save in dev) killed it mid-boot. The instance kept booting to
'active' on Lambda, but nothing dialed SSH or closed the launch record: it
hung in 'booting' forever while it billed. Fix: `resume_pending_launches()`
runs at startup (after adopt), finds every 'booting' launch, and either marks
it active (if adopt already reconnected the now-live instance) or spawns a
fresh wait-then-connect task. Fresh timeout window on resume is deliberate -
a restart must never SHORTEN a genuine boot, which was the whole bug.
Alternative (persist the coroutine / seed elapsed from launched_at) was
rejected: it risks instantly timing out an instance that is still booting,
reintroducing the failure.

**Server-side long-poll: wait_for_launch (MCP) / GET /launches/{id}/wait.**
An agent polling get_launch_status every few seconds burned ~40 round-trips
(and their tokens) per slow boot. `wait_until_settled` parks server-side up
to a capped timeout (<=300s/call) and returns when the launch settles. It
polls the DB, not the in-memory task, so it also serves resumed launches.
The cap keeps a single HTTP request from hanging forever; a still-booting
caller just calls again.

**Structured launch phase (launch_progress, pure).** Every launch record now
carries a stable `phase` (requesting_capacity | retrying_capacity |
waiting_for_active | ready | failed | terminated), a human `phase_detail`,
`settled`, and while booting a `boot_elapsed/timeout/remaining_seconds`
countdown. Replaces the empty connection_error a poller used to see mid-boot.
The dashboard's Pending-launch card renders the countdown with a note that a
long boot is normal, so a 40-min boot doesn't look frozen.

**Log progress-bar collapse (collapse_progress).** Tools redraw progress bars
with `\r`; captured by newline only, all the intermediate frames arrive glued
into one multi-KB line that a terminal never shows and that burns agent
tokens on read. We now store only the segment after the last `\r` (the
terminal-visible frame), at the single append_log chokepoint. Chose write-
time over a read-time clean=true flag: the raw frames have zero value stored,
and every reader stays clean without opting in.

**Not done: no forced fallback instance type.** The report suggested adding
gpu_1x_a100_sxm4 as a default fallback. Left fallback_instance_types empty:
substituting a different (pricier) type than the user asked for should be
their opt-in, not a shipped default. The config comment shows the example.

**wait_for_launch absorbs a backend restart mid-park (client-side retry).**
Field follow-up: a --reload restart during the long-poll dropped the socket
and the tool surfaced "backend unreachable" for a launch that was actually
fine (the backend resumes it on startup). The MCP tool now distinguishes
transport failure (`unreachable: true` from _call) from backend rejection,
reconnects, and keeps parking inside its own timeout window; if the backend
never answers it returns a calm structured `phase: backend_restarting`
record that says to call again. This retry is transport resilience in a
read-only poll, NOT client-side business logic - no guard is involved. Same
fix also raised the per-request socket timeout above the server park time
(the shared client's 60s default would have cut off a 120s park).

**Terminal sessions survive a page refresh (terminal_sessions.py).** The
dock already kept shells alive across navigation, but the WS handler OWNED
the pty/SSH process, so a refresh (the freeze-then-reload case) killed the
shell and any Claude session running in it. Now the process, its output
pump, and a ~200k-char scrollback buffer live in a TerminalSessionManager
keyed by a client-chosen session id; the dock persists its tabs/layout in
sessionStorage and reconnects with the same ids, so a refresh reattaches
every shell with scrollback replayed. Lifecycle: a bare socket drop
DETACHES (shell keeps running); the tab's x sends {"type":"close"} which
kills; the shell exiting ends it; a detached session is reaped after
hub.terminal_grace_seconds (default 900) so closed-for-good tabs don't
leak shells; backend shutdown kills all. sessionStorage (per-tab, dies
with it) was chosen over localStorage deliberately: refresh = restore,
closing the app = a fresh start, matching the requested scope. No ?session=
param keeps the old ephemeral contract for any other client. Security
posture unchanged: same origin allowlist, same loopback-only listen, the
session layer is transport glue below those checks.

**First-job GPU preflight (dispatcher._ensure_gpu_ready).** Field case: an
A100 SXM4 job dispatched 2.5 min after cloud-init finished died with "No
CUDA GPUs are available" and burned ~5 billed minutes. Cause: on SXM boxes
CUDA cannot initialize until nvidia-fabricmanager finishes starting -
minutes after boot - while nvidia-smi already looks healthy, so every
hand-check passes. The dispatcher now runs `nvidia-smi -q` before the FIRST
job on each instance and waits until the Fabric State line settles
(Completed / absent on PCIe boxes), bounded by
tasks.gpu_ready_timeout_seconds (180s) polling every 10s. Fail-open on
purpose: a probe error or timeout logs an honest line and dispatches anyway
- a wrong probe must never brick dispatch; pre-preflight behavior is the
floor. Readiness is cached per instance in memory only: a backend restart
re-probes once (seconds), which also re-covers an instance that was
mid-boot during the restart. Parsing lives in a pure gpu_readiness()
function tested against captured nvidia-smi output from both phases.

## 2026-07-15 — sdxl-generate uses python3; storage-browse errors made legible

Two field findings from dogfooding the templates on a real A10 (us-west-1),
both on the phase-40 field-hardening branch.

**sdxl-generate ran `python`, but its image only ships `python3`.** The
template's command was `... && exec python -c "$PYCODE" ...`. The
`huggingface/transformers-pytorch-gpu:latest` image has `/usr/bin/python3`
and no `python` symlink, so every run died with `exec: python: not found`
(exit 127) AFTER pulling the multi-GB image and running pip - real GPU
minutes for a guaranteed failure. Fixed to `python3`. Verified end-to-end on
the A10: two 1024x1024 PNGs written to the persistent filesystem, exit 0.
Only this template was affected - whisper-batch uses `pytorch/pytorch`
(has `python`, proven by the sprite-to-3d field run) and the python:3.11-slim
templates have both. **Alternative considered:** pin the image to a digest so
`:latest` cannot drift again. Deferred - the interpreter fix is the correct
minimal change, and a pinned tag has its own staleness cost (older CUDA); the
comment already flags `:latest` as a pin-later hot-path candidate.

**list_persistent_files crashed with "Expecting value: line 1 column 1".**
The S3 "Files" API keys (separate from the Lambda API key) were empty in
.env, so the storage factory raised ValueError; the `/storage/files` route
let that become a Starlette 500 *plain-text* page; and the MCP `_call` helper
ran `resp.json()` on that plain text, raising an opaque JSON-decode error
that surfaced to the agent. Hardened at both layers: `_storage_for` now
catches the credential ValueError and returns a clear 503 ("...credentials
are not configured in .env"), and `_call` wraps the JSON decode so ANY
non-JSON body (a 500 page, a proxy error) degrades to `{"error": <status /
body text>}` instead of crashing. The decode guard helps every tool, not
just this one. Underlying cause is config, not code: filling the S3 keys in
.env turns the persistent-file browser back on. **Why surface, not silence:**
a guiding LLM that gets "credentials are not configured" can tell the user
what to fix; "Expecting value: line 1 column 1" tells no one anything.

## 2026-07-15 — Launch-target discovery, actionable capacity failures, quieter pull logs

Repair pass on the friction found while dogfooding a launch over MCP: I
guessed a region with no A10 (5 failed attempts), then guessed one where the
user had no filesystem. The backend already knew both facts — capacity per
region (`regions_with_capacity` on each instance type) and region per
filesystem — but nothing put them together or exposed them to an agent.

**`launch_options(types, filesystems)` — a pure cross-reference.** Returns
launchable `{instance_type, region, filesystem}` targets Lambda can satisfy
NOW, ranked: co-located with EXISTING data first (a filesystem with bytes in
that region), then co-located with an empty filesystem, then scratch-only
(capacity but no filesystem there), cheaper first within each band; plus an
`unavailable` list of types with no capacity anywhere. Exposed as
`GET /launch-options` and the MCP tool `list_launch_options`, whose docstring
(and `launch_gpu`'s) tell an agent to call it FIRST and copy a target. A
launch needs type+region+filesystem to line up (types are capacity-gated per
region; filesystems are region-locked), so handing back only combinations
that already line up removes the blind guess. **Why a pure function + thin
route:** same pattern as `launch_progress` / `gpu_readiness` — the ranking is
unit-tested against the mock catalog with no I/O, and the route/tool are
one-liners. **Not changed:** `launch_gpu` still takes explicit args (a spend
action must not auto-pick); discovery informs the choice, it doesn't make it.
The dashboard already greys out impossible regions from `/instance-types`;
wiring a co-located "recommended" picker into the form is a possible
follow-up, not done here.

**Capacity exhaustion now names where to go.** The final "no capacity"
message used to end with "add fallback types in config.yaml" — useless to an
agent that can't edit YAML. `_capacity_hint` (best-effort; a catalog error
just yields no hint, never masks the failure) now appends the regions where
the requested types DO have capacity right now, e.g. "Available right now:
gpu_1x_a10 in us-west-2. Relaunch there ... or call list-launch-options".

**Docker pull churn dropped from stored job logs.** A `docker pull` in
captured (non-TTY) output emits one line per layer per state — dozens of
`<hash>: Waiting / Downloading / Pull complete` that buried the real job
output and burned agent tokens on every `get_job_logs`. `is_docker_pull_noise`
(pure, regex on the `<12-hex>: <verb>` shape) drops them in `append_log`. The
lines that carry signal — `Pulling from ...`, `Digest:`, `Status: Downloaded
...`, and all job output — are NOT matched, and the full docker output still
lands in the per-task log file archived on the instance. Chose drop-at-store
over a stateful collapser: stateless, testable, and the archived file is the
escape hatch if the raw pull is ever needed.

## 2026-07-15 — Keyless agent file browsing: SSH first, S3 only as fallback

The MCP `list_persistent_files` tool browsed ONLY through Lambda's S3 "Files"
API, so an account with no S3 keys in .env (a real user case: they don't have
and won't add them) could not browse persistent files from an agent at all —
even with a box up. But the dashboard's per-instance Files panel browses the
same files over the managed SSH connection through the sidecar, needing no
keys. The tool now uses that path first: if a connected instance mounts the
target filesystem, it browses via `/instances/{id}/files/list` (sidecar, local
-disk speed, no keys) and returns `{source, filesystem, root, path, entries}`;
only when nothing suitable is connected does it fall back to `/storage/files`
(S3, which CAN browse with no instance running but needs keys). When even that
fails for lack of keys, the error carries a `hint` pointing at the keyless
route. `prefix` stays filesystem-relative on both paths — the sidecar's
persistent root is `/lambda/nfs` (the filesystem's parent), so the tool
prepends the filesystem name to the sidecar path to match the S3 semantics.

**Why the tool, not the /storage/files route:** the route is a thin S3 shim
and routes hold no business logic (project rule); the tool is already the
place that composes multiple backend calls (it picks a filesystem, picks an
instance), and choosing WHICH existing guarded endpoint to hit keeps the MCP
client thin without importing backend internals. The dashboard Storage page
(the standalone no-instance browser) still needs S3 keys and now returns the
clean 503 added earlier; wiring its UI toward the per-instance panel when a
box is up is a possible follow-up.

## 2026-07-15 — Terminal freeze: stop forcing a reflow per output chunk

The in-dock terminal froze under output volume (Claude streaming, build logs)
and janked while resizing. All three causes were front-end, not the (sound)
backend session layer:

1. **Per-chunk scrollToBottom.** `ws.onmessage` did
   `term.write(data, () => term.scrollToBottom())` — a synchronous reflow on
   EVERY WebSocket frame, including on hidden tabs. Under a firehose that is
   hundreds of forced layouts a second, which pins the main thread. xterm
   already auto-scrolls on write when the viewport is at the bottom, so the
   callback was redundant; dropping it fixes the freeze AND lets a user scroll
   up to read without being yanked back down.
2. **Unthrottled fit.** The ResizeObserver ran `doFit` (a full `fit.fit()`
   reflow + a PTY resize send) on every animation frame during a resize.
   `doFit` now coalesces to one run per frame and skips the PTY resize unless
   the cols/rows grid actually changed — no more change_terminal_size spam.
3. **Unthrottled drag.** The dock resize handle called setHeight/setWidth on
   every pointermove, re-rendering the dock and firing every terminal's
   ResizeObserver each time. The drag now coalesces to one state update per
   animation frame.

4. **DOM renderer.** xterm's default renderer lays out every cell in the DOM.
   Added `@xterm/addon-webgl` (0.19.0, pairs with xterm 6.0), loaded after
   `term.open()` so it hooks the live renderer, to draw glyphs on the GPU —
   the throughput ceiling-raiser under heavy output. It degrades safely: the
   import is `.catch(() => null)` (a missing/blocked chunk never breaks the
   shell), construction is wrapped in try/catch (no usable WebGL context ->
   stay on the DOM renderer), and `onContextLoss` disposes the addon so a lost
   GPU context reverts to DOM instead of going blank.

## 2026-07-16 — Terminal flow control: the real fix for freeze-under-output

The earlier terminal-perf pass (dropping the per-chunk scrollToBottom reflow,
throttling fit, WebGL) helped but did not stop the freeze: with a full-screen
TUI (Claude Code) streaming, output outran xterm and its write buffer grew
without bound until the tab choked. Diagnosis confirmed by the user: JUST the
terminal froze (rest of the app responsive), under heavy output, not the dev
server / drive. So the missing piece was backpressure.

Watermark flow control end to end:
- The browser acks how many chars it has actually RENDERED (xterm's write
  callback is the "parsed" signal), batched ~8 KB to avoid a message per
  chunk, carrying no scrollToBottom so the reflow stays gone.
- TerminalSession tracks outstanding (sent - acked) chars and a `_writable`
  event: cleared at FLOW_HIGH_WATER (128 KB behind), set again at
  FLOW_LOW_WATER (16 KB). feed() records scrollback FIRST (a reattach always
  replays everything), then sends and accounts — only delivery is paced.
- Each pump calls await_writable() BEFORE reading more, so the pause lands at
  the source: the SSH channel window fills and the remote shell throttles; the
  local pty pump uses a bounded queue that, when full, stops reading the fd so
  the kernel pty buffer backpressures the local shell.
- await_writable() is bounded by FLOW_WAIT_TIMEOUT (5 s): a client that never
  acks (an old cached tab) degrades to unpaced output, never a stalled shell.
  attach/detach reset the accounting so a fresh or absent viewer starts clean.

Chose true backpressure over dropping/coalescing output: a TUI's escape
stream can't be dropped without corrupting the screen, so the producer must
be slowed, not the bytes thinned.

## 2026-07-16 — Rolling-tag drift: surface it, don't force a pin

sdxl-generate broke because its `:latest` base silently dropped the `python`
symlink — image drift, not an authoring error. Several templates ride
floating tags (vllm/sglang `:latest`, axolotl `main-latest`), so the same
class of failure can recur and only shows up after a job burns GPU minutes.

`floating_tag_warning(image)` (pure) flags any unpinned tag: a tag counts as
PINNED when it is a @sha256 digest or contains a digit (a version, e.g.
2.4.0-cuda12.4 or 5.5.0-tf2); a digit-less tag (latest, main, nightly) or no
tag at all floats. It parses the ref carefully so a registry:port on the host
is not mistaken for a tag. The result rides a new non-fatal `warnings` field
on JobTemplate, surfaced three ways: logged at load, returned by
`to_api()` (so `/templates` and MCP `list_templates` carry it), and shown as
an amber advisory under the Jobs-page template picker.

**Surface, not pin:** hard-pinning every image to a digest trades drift for
staleness — a frozen base misses CUDA/security fixes and falls out of step
with the packages a template pip-installs at start — so it stays the author's
call. As a recovery breadcrumb, sdxl-generate records the digest of the image
we VERIFIED on a real A10 (2026-07-15) in a comment, so if a future pull
breaks, pinning to that digest restores a known-good state immediately.

## 2026-07-16 — File-navigator delete on root-owned job outputs

Found while cleaning up the whisper-batch test: job containers write outputs
as root (uid 0) into root-owned directories on the NFS, but the sidecar runs
as `User=ubuntu` (cloud_init.py), so its `/fs/delete` (the dashboard file
navigator's delete) hit "permission denied" on exactly the files a user wants
to clean up — checkpoints, outputs, caches. Silent, confusing break.

Fix: `fs_delete` tries the normal unprivileged remove first (least privilege
for ubuntu-owned files), and only on PermissionError falls back to
`_privileged_remove` — `sudo -n rm -rf -- <path>`. The path is the same
jail-resolved absolute path the handler already validated, passed as a single
argv (no shell) with `--` to stop option parsing, so the escalation stays
confined to the sanctioned roots. `sudo -n` (non-interactive) relies on the
instance's passwordless sudo (Lambda's default, already used by run_command);
if sudo is missing or refuses, the user gets a clear 500, never a hang.

Chose sudo-on-demand over running the whole sidecar as root (keeps least
privilege for everything else) and over running job containers as the host
user (which would break the many templates that need root in-container for
pip/apt and /root/.cache). Ships with the sidecar at launch, so it applies to
newly launched instances.

## 2026-07-16 — "Directory not empty" really means "a job still has it open"

Found live while cleaning the vllm test's HF cache: deleting a directory a
RUNNING job holds open fails on NFS with `rm: cannot remove '.../xet/logs':
Directory not empty`. NFS turns "unlink a file another process still has
open" into a hidden .nfsXXXX placeholder, so the parent then refuses — an
error that reads like a bug rather than "stop the job first".

`_busy_hint()` recognizes that shape (not empty / resource busy / .nfs) and
fs_delete returns 409 (a conflict, retryable) saying what is actually wrong
and what to do, instead of surfacing the raw rm text. Applied on BOTH paths:
the privileged retry and the plain remove — the latter raises OSError, not
PermissionError, so it would otherwise have escaped as a generic 500.

The raw detail is still appended in parens: the hint explains, it never hides
what the OS said.

## 2026-07-16 — Terminal still froze: show the renderer, fix the flow-control valve

The freeze survived the flow-control fix (confirmed with a restarted backend
and a hard-refreshed tab, so the fix WAS live). Two problems, both mine:

**1. The safety valve defeated the mechanism.** `await_writable()` timed out
after 5s and resumed sending. That valve exists for a client that CANNOT ack
(an old tab predating flow control) — but a browser choking on render also
stops acking, so the valve fired exactly when the pause was needed, and the
backend resumed flooding mid-choke. Now the budget depends on evidence: a
client that has NEVER acked gets FLOW_WAIT_TIMEOUT (5s, then stream unpaced —
no stall); a client that HAS acked demonstrably speaks the protocol and is
merely busy, so it gets FLOW_BUSY_TIMEOUT (60s), long enough for a real
render backlog. The long bound still exists only so a browser that dies
without closing its socket cannot wedge the shell forever; a timeout there
now logs a warning instead of passing silently. attach() resets the ack count
because a new viewer's protocol support is unknown again.

**2. A silent fallback hid the likeliest cause.** WebGL init was wrapped in a
bare `except`/`.catch(() => null)`, so if the GPU renderer never took (no
context, blocklisted GPU, too many live contexts across dock tabs), the panel
quietly ran xterm's much slower DOM renderer — indistinguishable from "the
fix didn't work". The active renderer is now state, shown in the panel header
(`webgl` grey / `dom` amber) with the reason logged to the console. Version
pairing was checked and is correct (xterm 6.0 / fit 0.11 / webgl 0.19), so
this is instrumentation to END the guessing, not a suspected mismatch.

Lesson: an error path that swallows its reason turns one bug into two.

## 2026-07-16 — The terminal glitch was a resize dedup that never sent

Symptom: typed text wrapped back over the start of its own line, the input
box kept stale text, and stretching the dock ("jiggling") fixed it. Not the
renderer: it reproduced identically with ?renderer=dom, which exonerated
WebGL after the header confirmed webgl was live.

Cause, introduced by my own resize-throttling pass: doFit updated
lastCols/lastRows BEFORE checking that the socket was open. The first fit
runs while the WebSocket is still CONNECTING (ResizeObserver fires on
observe), so it recorded the size and skipped the send; when ws.onopen ran
doFit again, the dims matched lastCols/lastRows and it returned early. The
resize was therefore NEVER sent, leaving the pty at its 80x24 default while
the view was much wider. The app wrapped at column 80 and overwrote its own
line; any real resize sent a fresh size and resynced it, which is exactly why
jiggling "fixed" it.

Fix: lastCols/lastRows mean "the size the pty has actually been TOLD", so
they are only updated on a successful send - the readyState check now comes
BEFORE the dedup. Lesson: a cache of "what the peer knows" must never be
written on a path that did not tell the peer.

## 2026-07-17 — Restart-proof jobs: detached containers + task re-adoption

Live hardening pass found the worst backend bug yet: a backend restart
(--reload on every file save) mid-job first ORPHANED the task (logs frozen,
'running' forever), and on the second live test actually KILLED the
container (exit 141, SIGPIPE): the docker client piped into the SSH channel,
so the channel's death took the job with it.

Fix, two layers:
- wrap_remote_command now runs the container DETACHED (nohup, output to the
  persistent task log, never the SSH pipe) and writes its exit code to
  task-logs/<id>.exit; the streaming session just tails the log and waits
  for the exit file. Any session death only kills the tail. nohup over
  setsid because macOS has no setsid and the wrapper tests execute in a
  real shell.
- Dispatcher._readopt_running_tasks() (startup): every 'running' task is
  re-adopted; poll the exit file (fallbacks: docker inspect for old-wrap
  containers, honest 'result unknown' if both are gone) and finish with the
  real code. Verified live: restart at tick 12/60, container survived all
  60 ticks, task landed succeeded/exit 0.

Also verified live in the same pass: idle termination fired at exactly the
configured limit and its rescue synced 22 MB of planted valuable files to
ephemeral-backup/ before terminating (audit: idle_termination ->
sync_ephemeral -> data_rescue), and the auto-manage lifecycle ran
launch -> run -> sync -> terminate with zero human input.

## 2026-07-17 — Terminal UX pass: lost cursor, Shift+Enter, font size, overflow

Four issues from field use inside the dock terminal (screenshots in the
user's Mani-Terminal-Bugs folder):

- **Typing over the current line / lost cursor.** Output arriving while the
  viewport was scrolled up left the user typing "blind" below the fold;
  typing now snaps the view to the cursor (term.scrollToBottom in onData,
  once per keystroke, cheap), and a full term.refresh follows every real
  grid change: the manual "jiggle the handle" fix, automated.
- **Shift+Enter sends instead of newline.** Terminals cannot distinguish
  Shift+Enter from Enter on the wire, so the key handler sends
  backslash+CR: the escaped-newline form the Claude CLI understands, and
  plain line continuation in every shell.
- **Font size.** Cmd/Ctrl +/-/0 while the terminal is focused (8-24px,
  persisted in localStorage, refit + PTY resize after each change).
- **Instance-card buttons flying off the card.** When the action row
  overflows, the four dock buttons collapse into a ">>" menu (Terminate
  always stays visible). Hysteresis - remember the width the full row
  needed, expand only when it is back - prevents collapse/expand flicker.

## 2026-07-17 — Second GPU boot race: container runtime, not the host

Gamemaker field pass: a job dispatched ~100s after active died with "No CUDA
GPUs are available" DESPITE the fabric-manager preflight - host nvidia-smi
was fine; the NVIDIA container toolkit wasn't serving GPUs yet. Two layers:

- GPU_PROBE_COMMAND now also runs `nvidia-container-cli info` (the library
  docker's --gpus path uses), guarded by `command -v` so a box without the
  toolkit stays fail-open.
- Last resort: a container that exits nonzero with a CUDA-race signature
  ("No CUDA GPUs are available", "could not select device driver", ...) is
  retried ONCE after re-running the readiness gate. Ordinary failures are
  never retried (exit code preserved).

Also from that pass: the no-S3-keys 503 for /storage/files now teaches the
keyless route (instance Files panel / list_persistent_files over SSH), so
"no instance = blind filesystem" at least explains itself.

## 2026-07-17 — Capacity watches: full region map, and the notification that wasn't

Field QA on the watches panel found two real problems and a research answer:

**The region picker showed ~5 regions.** It was built from regions with
CURRENT capacity (plus filesystem regions) - exactly backwards for a watch,
whose point is a region with no capacity right now. It now offers the full
region universe from /regions, annotated per selected GPU with "has capacity
now". REGION_NAMES gained the international regions from the console picker
(Germany, Israel, India, Osaka, Tokyo, Sydney); NA_REGIONS renamed
KNOWN_REGIONS to match.

**A watch without auto-launch notified nobody.** The dispatcher's
on_capacity_available hook existed but was never wired to anything, so
capacity flipped the card to "available" silently. _check_watches now posts
a real notification (new kind: capacity_available, togglable in Settings,
default on) saying whether it auto-launched or the user should hurry.

**"Which regions can ever carry which GPU?"** Researched: Lambda publishes
no static per-type region roster - even their status page labels regions
inconsistently - and the API only reports CURRENT capacity per type. So we
deliberately do NOT hardcode a matrix (a guess presented as fact); the
picker says what is true now and the form copy says a watch in a region
that never carries the type will never fire.

Known flake noted: test_full_task_and_idle_lifecycle intermittently fails
under full-suite load only (timing); passes in isolation every time.

## 2026-07-17 — Cancel any job (servers included) + stale-default migration

Two gaps found while running the distill loop live:

**Cancel was auto-manage-only.** /tasks/{id}/cancel rejected manual jobs, so
a vllm-serve started from the Jobs page could not be stopped through
Manifold at all - the distill guide's own serve-then-train flow needed a
hand-rolled `docker stop` over SSH. dispatcher.cancel_task now covers every
pre-terminal state: queued settles as cancelled; running gets its container
stopped on the instance (`docker rm -f`, with a bracket-trick pkill for jobs
still in image-pull where no container exists yet); auto-managed pre-run
routes to the existing guarded teardown, and a running auto-managed job's
lifecycle sees the settle and proceeds to sync + terminate on its own. The
completion funnel labels a requested stop "cancelled by user" (no failure
ping) instead of a baffling "container exited 137". Jobs-page button now
shows Stop on running jobs.

**Shipped-default fixes never reached existing installs.** The packaged app
seeds DATA_ROOT/config.yaml once and never overwrites it (user-owned), so
the 900->2400 boot-timeout fix silently did not apply to the desktop app -
found live when a distill launch ran under a 900s window that a slow SXM
boot could overrun. CONFIG_MIGRATIONS rewrites a value ONLY while it still
exactly equals the old shipped default (a user-chosen value never matches),
via line-level regex so comments survive; applied and persisted in
load_settings with a log line per migration. Alternative considered: a
defaults-overlay (load bundled config underneath the user file) - rejected
because the seeded file is a full copy, so every key would read as a user
choice and nothing would ever migrate.

**Instance adoption runs on a sweep, not just at startup.** An instance
launched outside Manifold (Lambda console, a raw API script, an agent with
its own credentials) appeared in Running Instances - the list comes from
Lambda's API - but had no managed SSH connection, so Files, model chat, and
jobs were all dead for it until the backend restarted. Found live when an
agent drove a launch with curl and then sat stuck. The dispatcher now calls
adopt_running_instances every launch.adopt_poll_seconds (default 30, 0
disables); the call already skips tracked ids, so steady state is one
list_instances per tick. Mid-session adoptions audit as "instance_adopted"
(reconnect_on_startup stays what it says). Alternative considered: a manual
Connect button on the instance card - rejected because the user cannot know
a connection is missing before clicking around a dead Files panel, which is
exactly how this was found.

**Adopted external instances default to keep-alive.** The adoption sweep
made externally-launched boxes fully usable (Files/chat/jobs) - and thereby
put them on the idle termination clock, where they are guaranteed to look
idle: their owner's activity happens over their own SSH, which the idle
tracker cannot see. Found live 25 minutes before Manifold would have
rescued-and-terminated an agent's box mid-extraction. Rule: no launch row
(Manifold did not launch it) -> keep-alive defaults ON at adoption, audited,
visible on the instance card, user can switch it off; the default applies
once per instance id so that choice is never overridden by the next sweep
tick. A backend restart re-applies the default (errs toward keeping an
externally-owned box alive; the cost of a wrong guess is a few $/hr, the
cost of the other wrong guess is someone's running job). Alternative
considered: exempting external instances from the idle loop entirely -
rejected because it removes the user's ability to opt a forgotten external
box INTO cost protection.

**GPU telemetry falls back to nvidia-smi over SSH.** The telemetry chart
and sampling loop rode the sidecar exclusively - which only exists on
instances Manifold launched, because OUR cloud-init installs it. Adopted
external boxes therefore showed "telemetry unavailable" forever. Now the
sidecar is tried first (richer, cheaper, streaming), and when it raises,
metrics come from nvidia-smi --query-gpu over the managed SSH connection
in the same payload shape (marked source: "ssh"), in all three consumers:
the 30s sampling loop, GET /metrics, and the chart's WS relay (3s poll).
Alternative considered: installing the sidecar onto adopted boxes over
SSH - rejected for now because mutating a machine Manifold does not own
is a bigger decision than reading nvidia-smi from it.

**Model-fit preflight estimates from the name, not the weights.** The
Jobs page now warns before launch when a model's weights plausibly exceed
the chosen GPU's VRAM (born from a 27B GPTQ-Int4 checkpoint OOMing a
24 GB A10 after the full boot + download tax was paid). Parameter count
and quantization are parsed from the model id (27B, 8x7B, GPTQ/AWQ/Int4,
fp8, q4...), VRAM from the instance type; verdict tiers fits / tight
(weights above 70% of VRAM leave little KV-cache room) / no (above 92%).
Advisory only, never blocks, and the copy says it was estimated from the
name. Alternative considered: querying the HF API for real safetensors
sizes - rejected for v1 because it adds a network dependency and auth
surface to a pure function, and the name heuristic catches the whole
class of mistake this exists to catch; unknown names simply say nothing.

**Agent onboarding is a served document, not tribal knowledge.** An agent
with every Manifold MCP tool available still drove the raw Lambda API and
lost a night to self-inflicted terminations, because nothing taught it the
product. docs/manifold-skill.md is the playbook (recipes + rules), served
at /skill, bundled into the frozen backend, exposed as the MCP get_skill
tool, and the MCP server instructions tell agents to read it first. One
source file, four delivery paths, so it cannot drift.

**CLI brain detection searches well-known dirs, not just PATH.** Finder
launches give a macOS app launchd's bare PATH, so shutil.which found no
frontier CLI and the packaged app showed "No brains found" to a user
logged into all three. Detection falls back to /opt/homebrew/bin,
/usr/local/bin, ~/.local/bin, ~/.npm-global/bin, ~/.bun/bin, ~/bin.
Invocation already used the resolved absolute path. Alternative
considered: spawning a login shell to read the user's real PATH -
rejected as slower, shell-dependent, and a bigger surface than a static
list of the six places these CLIs actually install to.

**MCP presence is shown as recent activity, not a connection.** The MCP
bridge is a stateless HTTP thin client, so a "connected" badge would be
a lie waiting to happen. Every MCP call already lands in the audit log
with actor "mcp"; the header chip derives from the newest such row: teal
within 5 minutes (an agent is working), grey within the hour, hidden
after that. Honest, zero new state, click-through to Activity.

**vllm-serve enables tool calling by default.** Agent frameworks
(pydantic-ai, OpenAI SDK tool use) request structured output via tool
calls, and vLLM 400s every such request unless started with
--enable-auto-tool-choice and a --tool-call-parser. Found by a field
agent who lost a debugging round to it. The template now passes both,
parser defaulting to hermes (matches the Qwen/Hermes models our presets
serve); tool_call_parser is a template parameter for mistral/llama3_json
families. Always-on is safe: the parser only interprets tool-call
markup, plain chat is untouched.

**Terminal quality pass: the three real defects behind "glitchy".**
(1) Shift+Return never worked: xterm consults the custom key handler for
keydown, keypress AND keyup, and the handler only blocked keydown - so
xterm's keypress path sent a plain Enter right behind our escaped
newline, submitting anyway. Owned combos now return false for every
phase. (2) WebGL contexts are scarce (WebKit caps them per page, evicts
the oldest) and the dock keeps every tab mounted - one context per
HIDDEN tab is how the visible terminal got evicted and silently fell to
the slow DOM renderer, which under a TUI repaint load reads as glitchy /
typing over itself. WebGL is now acquired on visibility and released on
hide (IntersectionObserver), with re-acquire after context loss instead
of permanent fallback. (3) xterm's default width tables are Unicode 6;
the spinners and glyphs Claude Code draws get the wrong cell width, so
the app and terminal disagree about the cursor column - unicode11 addon
fixes the tables. Also: macOptionIsMeta (Option+Enter newline,
Option+arrow word jumps), Cmd+K clear, shortcuts on the header tooltip.

**Filesystem creation in-app; deletion deliberately not.** Lambda's API
supports POST /filesystems (create is free; storage bills by GB-month
used), so the Storage page and MCP create_filesystem let a filebase be
created in any known region without the Lambda console - the missing
step when capacity appears in a region with no storage. Deletion stays
console-only for now: it destroys data, and doing it right in Manifold
means wiring the data-safety policy (what is on it, what would be lost)
first. Note the API quirk: list is GET /file-systems, create is POST
/filesystems.

**No in-app credits balance: Lambda has no billing API.** Verified
against the Cloud API docs: no endpoint exposes credits, invoices, or
balance; the console (Settings > Billing) is the only surface. Settings
now says exactly that and deep-links it, instead of showing a number we
cannot actually know (data honesty over decoration).

**Model presets follow Lambda's per-model benchmark pages.** Tiers for
the big MoE presets now cite lambda.ai/inference-models/<repo>,
constrained to instance types that exist ON-DEMAND (there is no 4x B200
type, so Hy3 maps to 8x H100). Wording preempts the HGX misread: "1x
NVIDIA HGX B200" on those pages is one 8-GPU SYSTEM (--tp 8), not one
card. Added (all verified ungated on HF 2026-07-17): Carbon-3B (A10),
LFM2.5-8B-A1B (A100), Nemotron-3-Ultra NVFP4 and Step-3.7-Flash (8x
H100), MiniMax-M3, Kimi-K2.6, Kimi-K2.7-Code (8x B200). DeepSeek-V4-Pro
deliberately excluded: Lambda serves it with data+expert parallelism,
which vllm-serve (tensor parallel only) cannot express; a custom
template is the path for it.

**"Open in terminal" wires a local shell to a served model via env, not
config files.** A running serve job's card opens a Local Machine dock tab
whose shell is spawned with OPENAI_BASE_URL/OPENAI_API_BASE pointed at
the OpenAI proxy, OPENAI_API_KEY set (proxy key or a placeholder), and
MANIFOLD_MODEL naming the model - so any OpenAI-compatible CLI started
there (aider, opencode, ...) talks to the user's own GPU with zero
setup. The client passes only ?model=<id>; the backend composes the env
itself (values land in the child's environment, never in a shell
command), and a banner recorded in scrollback explains the wiring.
Alternative considered: writing CLI-specific config files - rejected
because env vars are the one interface every OpenAI-compatible tool
already honors.

## 2026-07-17 — Serve readiness on the jobs card (model loading vs ready)

**The jobs card shows model-loading vs model-ready, and gates "Open in
terminal" on it.** A serve job goes "running" the moment its container
starts, but the OpenAI API does not answer until the weights finish
downloading and loading - minutes later for a large model. The shipped
"Open in terminal" button therefore had a known papercut: click it too
early and the CLI errors on connect, with no way to tell "still loading"
from "broken". The card now polls the existing
GET /instances/{id}/model every 5s (the same signal the chat panel uses;
dispatcher.model_ready caches the /v1/models probe at 3s while loading,
30s once ready, so the poll is cheap), shows an amber "model loading" or
green "model ready" chip, and disables the terminal button until the
probe answers. No new backend surface: the readiness verdict already
existed for chat and the autopilot; this only surfaces it a second place.

Keyed to the card, not just the instance: the endpoint reports the ONE
serving task per instance (ports are unique per box), so the chip trusts
its verdict only when the returned task_id equals this card's task id -
otherwise a second card would inherit a neighbour's "ready". A test now
locks that task_id into the serving response, since the UI depends on it.
Alternative considered: keep the button always enabled and let the CLI
fail loudly - rejected because the whole point of the button is
zero-setup, and a connect error on first use reads as "Manifold is
broken", not "the model is still warming up".

## 2026-07-17 — lora-merge template: the missing rung in the distill loop

**A first-class lora-merge template replaces the "hand-roll a peft
script" hand-wave.** distill-your-own-model.md step 5 told the user to
merge the LoRA adapter with a bespoke script-run job "and ask the chat
to queue it" - the one manual break in an otherwise one-click pipeline
(synthesize -> finetune -> merge -> serve). lora-merge folds an adapter
from outputs/ into its base weights and writes a standalone HF model to
models/. It is base-model-agnostic: the base repo is read from the
adapter's own adapter_config.json, so a plain axolotl-finetune output
needs only its directory name (base_model is an optional override).

**Merge via peft, not axolotl's own merge_lora.** Alternative
considered: `axolotl.cli.merge_lora <config>`, which reuses the finetune
config. Rejected because it only works for adapters axolotl produced and
needs the exact training config still present and consistent; the peft
path (AutoModelForCausalLM + PeftModel.merge_and_unload) merges ANY LoRA
adapter and derives the base from the adapter itself. Reuses the axolotl
image (torch/transformers/peft already there and already cached on the
box that just finetuned), so no fresh multi-GB pull.

**Injection-safe like script-run.** The merge program rides an env var
(MERGE_PY) and the user params arrive as POSITIONAL args ($1..$3) ->
python argv, never interpolated into the program text; render_docker_command
shlex-quotes every substituted value. Added to the env-script quoting
regression (test_template_quoting.py) so it can't silently regress to the
host-expands-to-empty no-op.

**vllm-serve now mounts models/ read-only.** A merged model is useless if
it cannot be served, and vllm-serve previously mounted only the HF cache.
It now also mounts {persistent}/models -> /data/models (read-only;
serving never writes), so a merged model is served by path with
model_id=/data/models/<name>. This makes the distill doc's long-standing
claim ("vllm-serve accepts a local HF-format model path") literally true.
A test asserts both templates agree on that shared mount.

## 2026-07-17 — MCP from the installed app: the binary doubles as the bridge

**`manifold-backend --mcp` runs the MCP stdio bridge instead of the
server.** MCP was a dev-checkout feature (uv run manifold-mcp), which
made "agents can drive Manifold" false for anyone who only has the .dmg.
The frozen sidecar binary now dispatches on argv: `--mcp` imports
app.mcp_server and speaks MCP on stdio, bridging to whatever backend
listens on MANIFOLD_PORT (normally the running app). Registration is one
line pointing at Manifold.app/Contents/MacOS/manifold-backend. Same
HTTP-only thin client, same guards, same audit trail - only the way it
is started changed. Alternative considered: a second frozen binary just
for MCP - rejected because the bundle already carries every dependency
(mcp is a main requirement), and one binary with an argv switch cannot
drift out of sync with itself.

**In --mcp mode, stdin/stdout are the protocol channel.** Two desktop
behaviors are suppressed there, by construction (the --mcp branch returns
before them): the parent watchdog (it reads stdin and would eat protocol
frames) and the startup banner (a stray stdout line breaks the client's
JSON-RPC parse). MANIFOLD_API_URL defaults to the app's own host:port
but an explicit value is never overridden (bridging to a remote/dev
backend stays possible). Verified against the actual frozen binary: real
stdio handshake, 20 tools listed, and a live list_instances round-trip
through the running backend.

**sglang-serve now mounts models/ read-only (parity papercut).** Found
during the live lora-merge verification: vllm-serve got the models/
mount in phase-53, so a merged model served by path worked there but
silently could not resolve on sglang-serve. Both engines now carry the
identical mount and the distill-loop test asserts parity for both.

## 2026-07-17 — Per-job actual cost: close the estimate feedback loop

**Finished jobs show what they actually cost.** The pre-launch estimate
already learns (median of past runtimes per template+GPU pair), but the
user never saw a job's real cost afterwards, so the loop never closed
visibly and estimate trust had nothing to stand on. GET /tasks now
annotates every finished task with runtime_seconds and
actual_cost_cents (wall time at the hourly rate of the launch its
instance came from, LEFT JOIN on launches), and the Jobs card shows
"12m . $0.26" next to the exit code. Computed at read time from
existing rows - no schema change, and history that predates this
feature gets costs retroactively.

**Attribution choice: wall time at the instance rate, per job.** On a
shared instance three jobs can overlap, so summed per-job costs can
exceed the instance bill. Accepted: the question each reading answers
is "what did holding the GPU for this job cost", which is also exactly
the quantity the pre-launch estimate predicts - comparability wins.
Tasks on adopted instances (no launch row, unknown rate) show runtime
but a null cost: unknown stays unknown rather than guessed, same
honesty rule as the billing page.

## 2026-07-17 — Capacity-queued jobs: scarcity parks, it never fails

**An auto-manage job queued against a full region waits instead of
failing.** The old behavior burned the launch retries into the capacity
wall and failed the job - the opposite of fire-and-forget. Two changes,
both in the dispatcher and both riding existing machinery:

1. A capacity PRE-CHECK before request_launch: the catalog snapshot
   (cached at watches.poll_seconds cadence so parked jobs never hammer
   the rate-limited API on the 5s auto-manage tick) parks the job in
   'waiting' with an honest detail line and one notification (the
   capacity_available kind - same toggle as capacity watches). The
   waiting state already re-enters _auto_launch each tick, so the job
   launches on the first tick after the snapshot shows capacity, then
   runs -> syncs -> terminates exactly as before.
2. A lost race re-parks: the catalog can lag reality, so a launch that
   exhausts its attempts on capacity errors ("No capacity" in the launch
   error - the contract test_capacity_wait locks) transitions back to
   waiting instead of failing, with that (gpu, region) pair denied until
   the next snapshot refresh so it does not thrash. Re-parking is quiet;
   only the first park notifies.

Unknown fails open: a catalog outage returns UNKNOWN and the job
proceeds to the real launch path, so a flaky catalog can never park work
forever. Cancel works while parked (zero launch attempts, zero spend).
Alternative considered: wiring parked jobs to the capacity-watch rows -
rejected because the job queue IS already a poll loop; a second wake
path adds coupling without adding responsiveness beyond the same poll
cadence. Scope note: Autopilot runs were left out deliberately - the
agent loop is interactive (a brain waiting on a launch that may come
hours later holds a conversation open); the agent can already queue an
auto-manage job, which now waits correctly.

## 2026-07-17 — Mock isolation: fixture state can never touch live state

Root cause of the day's worst incident: a mock backend started for README
screenshots on the shared port swapped fixture data under a live agent
session mid-launch, and because mock shared the real SQLite file, the
session's in-flight launch was reconciled against a catalog that had
never heard of it and marked terminated. The agent detected it only by
noticing a TEST-NET IP (192.0.2.10). Three guarantees now:

1. **Mock refuses to start over live state.** With launches in a
   non-settled status (launching/retrying/booting/active) in the REAL
   database, MANIFOLD_MOCK=1 exits with the launch ids and a plain
   explanation. MANIFOLD_MOCK_FORCE=1 overrides for the intentional
   case. The check reads the real db strictly read-only (sqlite
   mode=ro URI), tolerant of a missing file or schema.
2. **Mock gets its own database** (manifold-mock.db next to the real
   one): even when it runs, fixture state cannot read or rewrite real
   rows, so a real launch row can never again be "terminated" on paper
   by a demo backend.
3. **Fixture data is self-identifying.** /instances, /filesystems, and
   /launch-options now carry "mock": true (health and /settings already
   did, which only the dashboard banner used), and the MCP server
   instructions tell agents to report demo mode instead of acting on it
   as production state.

Alternative considered: a different default port for mock mode -
rejected because the dashboard and MCP bridge target one port by
convention, and the failure was state substitution, not port collision;
isolating the state removes the harm regardless of port.

## 2026-07-18 — MCP transfers and waits fit inside the client's timeout

Two reliability items from the Game Admin report (2026-07-17): tool
calls were dying at the MCP client's ~60s request timeout while the
underlying work succeeded server-side.

**download_file is resumable, in bounded chunks.** A 127MB batch output
required manual split -b 30m / reassemble on the instance. The download
route now serves byte ranges (offset/max_bytes, X-File-Size header, 416
when the offset outruns the file - i.e. the remote changed) via a seek
on the SFTP handle, and the MCP tool fetches 16MB chunks into
<local_path>.part, returning complete=false with progress before the
~60s client timeout can fire. Calling again with the same arguments
resumes from the .part size; a 416 restarts from zero (remote file was
regenerated); on completion the .part is renamed into place. The whole
transfer is restart-proof at every layer that can time out.

**wait_for_launch and run_command self-limit to 50s.** The old
wait_for_launch defaulted to 120s (cap 300) - guaranteed to outlive the
client timeout it cannot see, surfacing as MCP error -32001 on every
slow boot even though the launch was fine. Each call now parks at most
50s server-side and returns settled=false with boot progress: the
"still booting, call again" answer IS the poll token, and the docstring
says so. run_command gets the same 50s cap with explicit guidance:
longer work belongs in run_job or a detached nohup + follow-up check.
Alternative considered: raising the client's own timeout - not ours to
control; the server fitting inside the contract works with every client.

## 2026-07-18 — Worklog: cross-agent memory as a markdown file

**Every settled job and autopilot run writes one markdown entry.** The
platform vision item: work done through Manifold (including by local
models) should land "in the same basket" as work done with Claude or
Codex, so the next session - any agent - knows what already happened.
The canonical worklog.md lives next to the database (dev, tests, and
the frozen app each get their own); Settings > Worklog adds an optional
mirror directory that receives the same entries in manifold-worklog.md.
Pointing the mirror at an Obsidian vault IS the Obsidian integration -
vaults are just files - and pointing it at a repo makes GPU work visible
to every agent session in that repo. Entries carry template, GPU,
region, instance, runtime, actual cost, output paths, and errors; the
job funnel and the autopilot _finish hook write them, and a write
failure can never break the work it describes.

**get_work_log MCP tool + GET /worklog.** Agents on another machine (or
too lazy to read files) get the same entries over the guarded gateway;
the agent skill now says to call it FIRST, before re-deriving state.
Alternative considered: a structured JSON log - rejected because every
consumer here reads prose (humans, LLMs, Obsidian); the database
already holds the structured truth and to_dict'ing it again adds a
format nobody asked for.

## 2026-07-18 — Autopilot project brief: runs get project context

**One persistent brief, included in every run's system prompt.** Autopilot
goals were isolated commands; the user's ask was for the agent to know
"the overall job". A single project_brief row (Autopilot page textarea,
GET/PUT /project-brief, audited) is inserted into the system prompt
BEFORE the goal line, framed as "the goal below is one step in this
project". Read at run start so editing mid-run never shifts a live run;
a read failure degrades to no brief, never a failed run. Alternative
considered: per-run brief parameter - rejected because the whole point
is persistence across runs; the goal field already covers one-off
context. Note: agents driving Manifold through MCP (Claude, Codex in a
repo) already have their own project context and do not use this - the
brief is for the standalone Autopilot case.

## 2026-07-18 — Filesystem deletion in-app + exact model-fit via HF

**Filesystem deletion, behind a type-the-name guard.** The Storage page
could create filebases but not remove them (orphans bill by GB-month
forever, console-only cleanup). DELETE /filesystems/{name} follows the
termination philosophy adapted to storage: refuse while attached (409),
and refuse (428) until confirm_name repeats the exact name - the
response states the GiB and region that would be destroyed. Deliberately
NO force flag and NO MCP tool: an instance has a rescue path, a
filesystem does not, so the only honest options are "type the name" or
"keep it", and a whole-volume destroy stays a human action (agents can
still create). Client delete_filesystem implemented across the
interface/real/mock/swappable/unconfigured stack; route quirk matches
create (/filesystems/{id}, not /file-systems).

**Model-fit reads exact sizes from the HF API when it can.** The
name-parse heuristic misses renamed forks and gated repos. The model-fit
route now asks the HF model API for the safetensors parameter/dtype map
(exact bytes per dtype - fp32 shards count as 4, INT4 as 0.5) and falls
back to the name parse on any failure; HF_TOKEN in .env (new, optional,
read-only scope) extends coverage to gated repos the account accepted.
Follows the image-checker injection rule: real lookup only in production
wiring, so tests never touch the network. estimates.py stays pure - the
I/O lives in hf_lookup.py and arrives as an optional `exact` argument.

## 2026-07-18 — Phase 63: swarm-audit hardening (worklog, bridge death, terminal)

A concurrent Gemini Flash audit (run-flash-swarm over the google.antigravity
SDK, workers on Vertex via gcloud ADC) swept the worklog, bridge-recovery,
and terminal code; every finding was verified against the code before any
fix, and roughly half were rejected as hallucinated or already-handled
(startup reconciliation of stale agent runs already existed; the download
416 "infinite loop" is unreachable; WebSocket text frames cannot split
UTF-8 client-side; scrollback replay bypassing flow control is a
documented one-shot design). What was real and is now fixed:

**Connection loss re-adopts instead of failing (dispatcher).** A transient
SSH drop mid-stream marked the task failed while its container - detached
under nohup by design - kept running (and billing) unseen. The
ConnectionError path now hands off to the same exit-file poller a backend
restart uses, settling the task with the container's real result. The
re-adopt probe also demands a NON-EMPTY exit file (`[ -s ... ]`): a bare
`cat` racing the wrapper's `echo $? > file` create-then-write read empty
output as "container gone" and failed a task that had just finished fine.

**Reconnect survives long outages (connections).** `base * 2**attempt`
overflowed float after ~1000 retries (an instance offline overnight), and
the OverflowError - raised inside the retry handler - silently killed the
supervisor. Exponent clamped; the delay was already capped.

**Terminal reattach no longer drops live output (terminal_sessions).**
attach() bound the socket only AFTER the awaited scrollback send, so
output pumped during the replay was recorded but never delivered. The
socket is bound before the send; frame order is preserved because the
replay frame is submitted first. Local pty output also flows through an
incremental UTF-8 decoder (a 4096-byte read can split a multi-byte
sequence; per-chunk decode rendered the halves as U+FFFD), and the
pty.fork child _exits(127) if exec fails instead of returning into
FastAPI with inherited fds.

**Honest state over guessed state (mcp_server, diagnostics).**
_pick_instance propagates "backend unreachable" instead of reporting
"connected instances: (none)"; sidecar diagnosis classifies a mid-probe
connection loss as probe-error (and stops probing the dead channel)
instead of concluding "sidecar-starting" from partial answers; _audit
posts carry a 5s timeout so a wedged backend cannot stack a second 60s
wait past the MCP client's kill window; upload_file gets _call's
non-JSON-500 guard. Worklog: cancelled and crashed autopilot runs now
write entries too (the outcomes the next agent most needs), tail() no
longer doubles the first entry's mark, and the GET /worklog read runs
off-loop. Worklog writes stay synchronous ON PURPOSE: single-process
single-writer (the MCP bridge only reads over HTTP), so file locking
would be theater, and tests assert entries immediately after the funnel.

**Dashboard.** Dock session ids fold in a monotonic counter (two clicks in
one millisecond minted colliding React keys); setOpen/setSessions updaters
are pure again (StrictMode runs them twice); uploadFile surfaces a dead
backend as a typed ApiError with a 120s stall budget instead of a raw
TypeError.

## 2026-07-18 — Phase 64: foundation hardening (worklog schema, safety interlock lock)

**The worklog entry schema is now a written contract.** worklog.py's
docstring formalizes the exact block layout consumers parse (split on
"\n## "), and failed jobs now carry the two fields the next agent needs
to judge a crash with the instance gone: an explicit `exit code N` line
and `last output:` - the final three log lines as a crash signature
(OOM traceback, CUDA error, etc.). Fields stay best-effort: absence
means unknown, never success.

**The no-filesystem-delete-over-MCP interlock is test-locked.** The
phase-62 decision (whole-volume destroys have no rescue path, so they
stay a human type-the-name action) lived only in prose; a new test
enumerates the bridge's registered tools and fails the build if anyone
adds a filesystem-delete tool. terminate_instance's force flag stays
exposed deliberately - instances have a rescue path and force is the
documented single explicit burn.

**Live validation, zero spend.** Phase-63's fixes were validated against
the RUNNING real-mode backend: live Lambda volume telemetry (6
filesystems, real bytes_used, none in use, 0 instances) and a 20,000
multi-byte-glyph flood through /local/terminal's pty - 20,000/20,000
received, zero U+FFFD - proving the incremental decoder on the wire, not
just in unit tests. No GPU was launched: live-instance validation waits
for an instance that exists for real work, per the no-spend rule.

## 2026-07-18 — Phase 65: pty lifecycle (zombie reaping, process-group hangup)

**Every local shell is now reaped; hangups take the whole group.** Nothing
ever waitpid()ed the pty child, so every exited local shell sat as a
zombie until the backend itself exited; and close_pty signalled only the
shell leader, so children it left running (a backgrounded watcher, a hung
CLI) lingered. _end_shell_group killpg-HUPs the group (pty.fork made the
child a session leader, so pid == pgid), reaps with waitpid, and
escalates to SIGKILL after ~5s if the group ignores the hangup. Wired
into both exits: explicit close AND natural shell exit. Three regression
tests fork real processes and assert ps shows the pid fully gone;
live-verified against the running backend (open+close -> zero zombies).

**What the phase-65 prompt asked for that already existed (no churn):**
WebSocket backpressure (ack-based 128KB/16KB watermarks + bounded pty
queue, superior to the requested socket-buffer heuristic), session
reattachment with capped ring-buffer replay (200KB scrollback +
session-token hot-swap, race-fixed in phase 63), and crash signatures in
the worklog (exit code + last output, phase 64). The requested 60s grace
window was rejected: the refresh-proof dock exists precisely so a frozen
tab can be reopened at leisure; grace stays the configurable 900s
(hub.terminal_grace_seconds). Local shell exits stay OUT of the worklog:
it records units of work, not UI session churn.

## 2026-07-18 — Phase 66: pty master-fd lifecycle (leak fix, idempotent teardown)

**The detached-exit fd leak.** Tracing the master fd from pty.fork
showed the leak was NOT the attached case (a live WebSocket funnels
teardown through kill -> close_pty either way) but the detached one: a
shell that exited after a refresh-and-never-reattach was only reaped
(phase 65) - its master fd was never closed, and the registry dropped
the exited session without closing it. One leaked descriptor per such
shell for the life of the backend. Proven by A/B: the new detached-exit
test fails on pre-fix code, passes with it.

**One idempotent teardown for both halves.** teardown_once() guards fd
close AND process-group hangup behind a single flag, because both are
unsafe to repeat after the kernel recycles the identifier: a closed fd
number may be an unrelated descriptor, a reaped pid may lead an
unrelated process group. The second hazard was found by an independent
zero-trust audit pass (fresh-context subagent), which failed its
sign-off on exactly the killpg half after passing the fd half - the
double-teardown call ordering (pump EOF, then the WS funnel's kill) is
real and routine, only the recycle window is narrow. Audit areas that
came back clean: no bare os.close paths, no add_reader on a closed fd
(kill cancels the pump before closing), no parent-held slave fd, no
registry pop that outruns closure.

**Measurement note.** The fd-churn tests count /dev/fd synchronously; an
asyncio.run inside the measurement window opens its own kqueue +
socketpair and reads as a phantom 3-fd leak. (The prompt's target file
"backpressure_pty_wrapper.py" does not exist; the audited subsystem
lives in main.py local_terminal + terminal_sessions.py.)

## 2026-07-18 — Phase 67: teardown escalation telemetry (and two real bugs it flushed out)

**Telemetry, not Prometheus.** The escalation ladder itself (HUP -> ~5s
async waitpid verification -> SIGKILL) already existed from phase 65;
what was missing was knowing which rung ended each shell. Manifold has
no metrics stack and its observability IS the audit trail plus logs, so:
TERMINAL_TEARDOWNS counts sighup vs sigkill teardowns, an escalation
logs a warning and fires an on_escalation hook that writes an audit row
with the pgid and the session's last output (TerminalSession.tail_text)
- the best clue to what ignored the hangup. Grace is now a parameter so
the uncooperative case is testable in 0.5s instead of 5s. SIGHUP kept
over the requested SIGTERM: it is the idiomatic "your terminal went
away" signal for a shell group.

**Bug 1, found writing the tests: reap tasks could vanish.** create_task
keeps only a weak reference; the phase-65/66 reap tasks were unreferenced
and could be garbage-collected mid-flight - a vanished reap means
zombies return and escalations never fire. _REAP_TASKS now holds strong
refs (discarded on completion), and _end_shell_group returns the task so
tests await it deterministically.

**Bug 2, a fork race with setsid.** pty.fork's child calls setsid
child-side, so a teardown racing a just-forked shell (a tab opened and
instantly closed) could killpg BEFORE the group existed:
ProcessLookupError, swallowed, shell never signalled - it then lived to
the SIGKILL escalation. signal_group() now falls back to a direct
kill(pid) when the group is missing but the process is alive. Surfaced
as ~25% test flakiness whose +5s runtime signature (full grace loop)
pointed at undelivered SIGHUP; the test spawner also waits for the
child's pgid to equal its pid before tearing down, and keeps master fds
open for the child's lifetime like production does (closing them early
put children in racy orphaned-tty states).

## Phase 68 — Per-Instance Idle Timeout (2026-07-25)

**What:** Users can set a custom `idle_timeout_seconds` per instance, both at launch (in the LaunchRequest/UI) and later (via `POST /instances/{id}/idle-timeout` and the Instance card). The value is clamped to a configurable min/max (default 5 min to 4 hours) to prevent unbounded billing or aggressive auto-termination. The `Dispatcher._check_idle` loop queries the database for this specific value, falling back to the global default.

**Why:** Idle timeout needs to be flexible based on the task (e.g., long background process vs quick interactive shell) while maintaining financial safety. A user-configurable timeout allows balancing convenience against cost. Clamping provides a hard limit against mistakes.

**Design choices:**
- Clamping is centralized in the orchestrator (at launch) and API (at update) so the database always contains valid values.
- Updates are audited to provide transparency.
- A nullable `idle_timeout_seconds` column allows older records or defaults to fall back to the global settings dynamically.

### Phase 69: One-Click IDE Attach

Implemented one-click IDE attach feature for VS Code and Cursor. Generated SSH config block with `# >>> manifold managed <instance_id> >>>` delimiters. Detected active IDE processes (`vscode-server`, `cursor-server`, and interactive SSH sessions) in sidecar telemetry to prevent idle auto-termination while the user is actively working in an IDE. Added UI to copy the ssh command and click links to open VS Code/Cursor remotely.
## 2026-07-25: Multi-Cloud Provider Abstraction (GCP)
- Implemented an abstract `CloudProvider` base class and `ProviderRegistry`.
- Migrated existing Lambda functionality to `LambdaProvider` implementation.
- Added `MockGCPProvider` and `RealGCPProvider` for Google Cloud support.
- Updated Dashboard forms and components to support provider toggling and badging.
- Plumbed `provider` through database schemas (`launches` table) and endpoints.

## Phase 70 — The Config Rosetta Stone (2026-07-25)

**Decided:** Implemented `render_template` in the backend to share the exact same string substitution and quoting rules as the dispatch execution, and added a `POST /templates/{name}/render` endpoint. The frontend `ParameterForm` now uses a split-pane layout to preview the rendered YAML configuration live as parameters are typed, with line highlighting based on the focused field. Added an "Edit as config" promotion flow that shifts the rendered configuration into the TemplateEditor.

**Why:** Rendering the configuration server-side ensures the preview never drifts from what the dispatcher actually executes. The split pane builds trust by showing exactly what will run, and the promotion flow allows easy transition from parameter entry to custom template authoring.

## Phase 71: Structured Lifecycle Events
We added a `task_events` table to the database to record task lifecycle events (`queued`, `launched`, `started`, `checkpointed`, `interrupted`, `resumed`, `synced`, `finished`, `failed`) and the instance ID they occurred on, as well as `cost_cents_at_event`. 
This is critical for providing a rich timeline and audit trail for end-to-end task execution.
The `GET /tasks/{task_id}/events` route exposes these events to the frontend.

## Phase 72 — Multi-LoRA & SGLang Subagent Engine Upgrades (2026-07-28)

**Decided:** Upgraded `templates/vllm-serve.yaml` and `templates/sglang-serve.yaml` with Python launcher scripts to support multi-LoRA adapter serving, RadixAttention prefix caching, and speculative decoding draft models.

**Why:** Local subagent swarms (e.g. Qwen-2.5 Coder, DeepSeek-R1 Distill, Llama-3.3-70B personas) need to run concurrently on a single multi-GPU instance without multiplying VRAM overhead or token costs. Multi-LoRA allows dynamically serving dozens of specialized adapter personas on top of a single base model. SGLang's RadixAttention provides zero-token-cost prompt-prefix caching across subagent turns.

**Design choices:**
- In `vllm-serve.yaml`: Added `enable_lora`, `max_loras`, `max_cpu_loras`, `lora_modules`, `speculative_model`, and `num_speculative_tokens`.
- In `sglang-serve.yaml`: Added `lora_paths` and `mem_fraction_static`.
- Mounted `{persistent}/outputs` read-only under `/data/outputs` in both serve templates so finetuned LoRA adapters produced by `axolotl-finetune` can be served directly by path without requiring a full merge first.
- Replaced direct binary invocation in `command:` with an inline Python launcher script that parses parameters, constructs CLI arguments conditionally, logs the exact command line, and calls `os.execvp()` to pass process signals straight through to the underlying engine.

## Phase 73 — MCP 2.0 Async Event Streaming & Human-in-the-Loop Governance (2026-07-28)

**Decided:** Implemented real-time Server-Sent Events (SSE) streaming endpoints and approval governance tools across `main.py` and `mcp_server.py`.

**Why:** External AI agents (Claude Code, Google Antigravity / AGY SDK, Codex, OpenClaw, Hermes) need real-time feedback when executing long-running fine-tuning or batch inference tasks on Manifold virtual machines. Streaming logs and lifecycle events over SSE allows external agents to track progress token-by-token and event-by-event without polling. Cost-based approval gates ensure spend-heavy operations (e.g. launching multi-node clusters or high-hourly-rate instances) require human approval before execution.

**Design choices:**
- In `db.py`: Added `get_task_logs_after` and `get_task_events_after` for efficient indexed sequence/ID queries.
- In `main.py`: Added `GET /tasks/{task_id}/logs/stream` and `GET /tasks/{task_id}/events/stream` endpoints returning `StreamingResponse(media_type="text/event-stream")`. Added aliases `/approvals/pending` and `/approvals/{id}` for human-in-the-loop governance.
- In `mcp_server.py`: Added `@mcp.tool()` FastMCP primitives `stream_job_logs`, `stream_task_events`, `get_pending_approvals`, and `decide_approval`. Maintained strict thin-client architecture (zero direct imports of backend internal modules; all calls route over HTTP/SSE).
- Test coverage: Verified with 504 passing tests across the entire backend test suite.

## Phase 74 — Critical hardening of the Phase 68–73 feature set (2026-08-10)

**Context:** An independent fresh-eyes audit of the Phase 68–73 work (commit `beee486` and the multi-cloud/cluster range) found the guards, the cluster feature, and several templates had been implemented *around* the safety invariants rather than through them. This phase fixes the critical set. Verification was behavioral, not just the test suite, because the prior work passed 523 green tests while shipping broken features (several tests were written against simulated routers that never exercised the real endpoints).

**Decided & why:**

- **Spend guard reads fresh state again (was a stale-cache regression).** The concurrency/budget guard had been switched to `cloud_provider.list_instances()`, which serves the real Lambda client's *cache* — reopening the two-quick-launches race. Added `fresh: bool = False` to `CloudProvider.list_instances` (threaded through `LambdaProvider` to the client; GCP fetches live regardless) and the guard now calls it with `fresh=True`. A spend guard must never admit on a stale snapshot.

- **One atomic admission for the whole cluster (was a full guard bypass).** `launch_cluster` looped `request_launch` per node; each per-node check saw the same empty baseline (siblings launch in detached tasks *after* admission), so an N-node cluster sailed past a 1-instance / $4-hr limit. Extracted the guard math into `Orchestrator._guard_capacity(*, cloud_provider, instance_type, unit_price_cents, added_count)`, shared by `request_launch` (added_count=1, wording preserved) and `launch_cluster` (added_count=node_count, checked *before* any DB row is created). It also counts in-flight launch rows via the new `db.pending_launch_count()` (`status IN ('launching','retrying') AND lambda_instance_id IS NULL` — non-overlapping with cloud-visible instances), which closes the same race for single launches. `node_count` is capped at 16. A mid-loop failure rolls back already-launched nodes and marks the cluster `failed`.

- **Startup zombie-closer extended.** Counting pending rows meant a launch row orphaned by a killed process (e.g. `--reload`) would hold a guard slot forever. `resume_pending_launches` now also fails orphaned `launching`/`retrying` rows with no instance id and no live task, matching how it already closed orphaned `booting` rows.

- **Terminating a provisioning cluster no longer bills phantom nodes.** `terminate_cluster` now cancels the in-flight `_launch_tasks[…]` for a node that has no `lambda_instance_id` yet (await under `contextlib.suppress(CancelledError)`) and re-reads the row in case the task won the race; and `_launch_with_retry` re-checks launch status before each `launch_instance`, aborting if already `terminated`. Closes the window where a "terminated" node kept booting and billing.

- **Cluster templates are loopback-only (was a public RCE surface).** `ray-cluster`, `vllm-cluster`, `deepspeed-cluster` had `network: host` with services bound to `0.0.0.0` — exposing an unauthenticated Ray dashboard (job-submission = RCE), an open vLLM API, and a torchrun rendezvous port on the instance's public IP, violating the "nothing off-loopback but sshd" rule. Removed `network: host`; the Ray dashboard binds `127.0.0.1`; and because real multi-node rendezvous over the managed tunnel is not yet implemented, the worker/multi-node code paths now exit 1 with a clear "not yet supported" message instead of silently opening the network. Real cross-node bootstrap is deferred to a later phase.

- **Broken inline-Python launchers fixed (regression from Phase 72).** The Phase 72 change that replaced direct binary invocation with an inline `python3 -c` launcher shipped a YAML folded-scalar (`>`) defect: the script rendered with a leading space (`IndentationError` on line 1) and a backslash inside an f-string expression (`SyntaxError` pre-3.12), so `vllm-serve`, `sglang-serve`, and the three cluster templates would crash at container start — the launcher never actually ran. Rewrote all five so the embedded Python compiles (first statement on the `-c '` line, blank lines between top-level statements, `" ".join(cmd)` on its own line). Runtime argv is unchanged. The `deepspeed-cluster` image was also moved from the dead `winglian/axolotl:*-py3.11-*` pin to `axolotlai/axolotl:main-py3.12-*` (the namespace this repo already uses elsewhere).

- **Clients cannot silently force past the data-safety guard.** The dashboard cluster-terminate button called `terminateCluster(id, force=true)` under a dialog that promised files would be saved — a client path around the rescue-before-destroy rule. It now defaults to `force=false` and surfaces a blocked rescue with the same affordance `InstanceCard` uses (retry-then-terminate / explicit "terminate anyway" only after a block). `force` remains reachable only as a deliberate, post-block user choice.

- **Frontend GCP config reaches the backend.** `setGcpConfig` posted to `/settings/gcp` (404); corrected to `/settings/gcp-config` with the real `{valid, applied_live}` response shape.

- **Repo hygiene.** Removed the committed regex self-patching scripts (`patch_main.py`, `patch_mcp.py`, `patch_orchestrator.py`), a byte-identical duplicate asset, and the 45 MB packaged DMG from tracking; added `*.dmg`/`*.msi` to `.gitignore`. Two Claude session transcripts that leaked local paths and unrelated production infra identifiers were purged from history (the repo is public), and the leaked infra secrets should be rotated as a precaution.

- **Deferred to a follow-up phase (not in this critical set):** the non-functional Local Subagent Engine routes (`format_tool_call` arg-order bug + never-registered endpoints), the IDE-attach 500 (references non-existent `Settings` fields), the `RealGCPProvider` `NotImplementedError` that 500s the instances view when a project id is set, the events-SSE drop bug (`kind` vs `event`, second-precision collision), and the new panels' inverted-palette recolor. These are broken-but-contained and tracked for phase 75.

**Test coverage:** 6 new backend tests (atomic cluster over-budget/over-concurrency rejection, node-count cap, terminate-cancels-in-flight, launch-aborts-when-terminated, resume-fails-stale-pending); full suite 529 passing. Cluster guard rejection, no-ghost-row, template compilation, and the terminate response contract were additionally verified against a running mock backend.

## Phase 75 — Repair of the deferred Phase 68–73 feature set (2026-08-10)

**Context:** The features Phase 74 flagged as "broken but contained" and deferred. Each was shipped in `beee486` passing green tests that never exercised the real code path. Verified the same way as Phase 74: three parallel implementers on disjoint files, an independent critic on the full diff, then live behavioral probes against a running mock backend — not the test suite alone.

**Decided & why:**

- **Local Subagent Engine now works and rides the managed connection.** Two defects: (1) `POST /subagents/dispatch` 500'd on every call because the route passed `tools` into `format_tool_call`'s `role` positional — fixed to keyword args; the route now maps engine errors to honest statuses (`503` no healthy endpoint, `502` upstream failure, `422` bad role) instead of a raw 500. (2) The engine registered a bare `http://127.0.0.1:{port}` and hit it with raw httpx from the backend host, which cannot reach a model served on a GPU instance and violated the "instance comms ride the managed SSH forward" rule. Reworked the registry to hold a `SubagentEndpoint` that is either instance-served (a `ManagedConnection` + remote port, dispatched through `RealModelClient`'s per-call SSH forward, exactly like `model_client.py`) or local (a direct loopback URL, correct for Ollama/LM Studio on the backend host). The dispatcher registers a served model on task-ready and deregisters in the `_finish_task` funnel, keyed on `instance_id:port` (which also fixes a same-port collision that made deregistering one instance drop another). **Verification limit:** the SSH-forward *mechanics* are unit-tested with a fake connection (correct remote port forwarded, listeners closed, model name on the wire), but true end-to-end dispatch to a live vLLM/SGLang model over a real `asyncssh` forward can only be validated on real GPU hardware at the phase gate — mock mode has no real forward. Not claimed as e2e-verified. Dynamic LoRA hot-loading over the forward is left unimplemented (rare path; the served model is still selected via the payload `model` field).

- **One-Click IDE Attach works.** The route referenced `settings.ssh_key_path` / `settings.host_keys_path`, neither of which exists, so it 500'd on the happy path. Fixed to `os.path.expanduser(settings.ssh.private_key_path)` and the real `<db dir>/host_keys.json` that the ConnectionManager builds its `HostKeyStore` from. `SSH_CONFIG_PATH` stays a call-time module global so tests patch it to a temp file; verified live that attach writes a valid block and terminate removes it via `remove_ssh_config_block`.

- **Task events stream stops dropping events.** `stream_task_events` deduped on `ev.get('event')` — a field that doesn't exist (events use `kind`) — so with second-precision timestamps all events in one second collapsed to one. Rewritten to a row-id cursor via the existing `get_task_events_after` (mirroring the logs stream's `seq` cursor), with a 15s heartbeat and a max-duration cap. The MCP `stream_job_logs`/`stream_task_events` tools were switched from a 30s-timeout SSE socket (which failed on any quiet training run) to bounded cursor polling; the MCP thin-client AST guard still passes.

- **Enabling GCP no longer bricks the app.** `RealGCPProvider` raised `NotImplementedError` for everything once a project id was set, and the orchestrator's provider sweeps caught only `LambdaAPIError` — so setting `GCP_PROJECT_ID` 500'd `/instances` and silently disabled Lambda adoption too. Read methods now degrade to empty (warn once); write methods raise a typed, catchable `ProviderError`. The registry gained a public iteration API (`items()`/`all_providers()`), and `instances_with_state`/`adopt_running_instances` isolate each provider in its own try/except — one failing provider logs a warning and is skipped while the others still return. A partial-list failure now skips the connection-reap pass entirely, extending the existing "only reconcile on a fully successful list" invariant to the multi-provider case. (Known-latent: `ProviderError` is not yet caught by a caller because GCP launch isn't reachable in the normal flow — to be handled when GCP launch is implemented.)

- **Cluster telemetry/SSH resolve to real instances.** Cluster nodes stored a launch id where the dashboard needed the cloud instance id, so telemetry never showed data and the SSH-head button docked a dead terminal. `get_cluster_nodes` now resolves each node's `lambda_instance_id` (null until booting) and live `status` from its launch row (single source of truth; the dead `update_cluster_node_status` is left superseded). The dashboard consumes `lambda_instance_id` for telemetry/SSH/keys with a "provisioning…" fallback, and `MultiGpuTelemetry` feeds `launch.lambda_instance_id` (not `launch.id`) to the chart.

- **Agent context can't hoard credentials or grow unbounded.** Removed `session_tokens` from the model, update path, request schema, and MCP tool (secrets stay in .env). Added an idle TTL and a max-context cap with least-recently-seen eviction.

- **The new panels look like one product.** ClusterPanel, VisualTaskGraph, MultiGpuTelemetry, and TelemetryChart were written against stock Tailwind zinc, but the app inverts that scale, so they rendered ink-on-ink with light patches. Recolored to the app's role tokens (matching InstanceCard/LaunchForm), removed the neon/glow flourishes, and gated the panels' empty states so the default screenshot reads as intentional.

**Test coverage:** ~30 new/updated tests across the six areas, all hitting the real app (the old simulated-router test for subagents was deleted and rewritten). Full suite 547 passing; dashboard builds clean. Subagent 503/422 statuses, IDE attach happy path + config cleanup, GCP-enabled instances view, and same-second event delivery were additionally verified against a running mock backend.


## Phase 76a — Spend accounting: an honest number, or an admitted unknown (2026-08-10)

**Context:** Manifold could start GPUs but could not say what they had cost. The gap is not arithmetic — rate x time is easy — it is *evidence*: which launches actually billed, for how long, and what we genuinely do not know. An estimate that is wrong is advice; an accounting number that is wrong is a lie, so every choice below picks the direction that fails loudly rather than the one that looks tidy. The formula lives in exactly one place (`backend/app/spend.py`, pure functions, no I/O and no clock of its own); routes, clients, and the dashboard may only display what it returns.

**Decided & why:**

- **The billing anchor is `launched_at`, and it deliberately over-reports.** Verified Lambda facts: billing starts when the instance passes health checks, and time is charged in one-minute increments (so 45m01s bills as 46 minutes). `launches.launched_at` is stamped earlier — when Lambda *accepted* the launch — and boot is not free time on a big box (15–30+ minutes on multi-GPU SXM4), so the difference is real money. **Alternatives:** anchor on `active_at` (closest to the invoice, but null for every launch that never reached active — precisely the launches most likely to have billed unnoticed), or subtract a modelled boot time (a fabricated correction dressed as precision). **Why:** a spend-safety tool must err upward. We anchor on acceptance, round *up* to the minute, say so in the one `DISCLAIMER` string every spend surface carries, and expose `boot_seconds` per launch so the user can see exactly how much of the number is boot. The direction of the error is a design decision, not an accident, and it is written down where the user can read it.

- **`resolved_at` is a new column because `terminated_at` means "we SAW it stop".** The reconcile sweep now writes `last_seen_at` (the last sweep that listed the instance alive) and `resolved_at` (the sweep that concluded it was gone) — never a guessed `terminated_at`. **Alternatives:** stamp `terminated_at = now` when an instance disappears (one column, no new state), or backfill it from the last telemetry sample. **Why:** both fabricate an observation. A launch that boot-timed-out six weeks ago and was never reconciled would print a five-figure "final" cost that never happened, and nothing downstream could tell that total from a real one. Instead such a launch is `unresolved`: `usd = None` plus a range. Two of the six `COST_STATES` carry `usd = None` and are reported beside the totals with their launch ids, never folded in as $0 — a fabricated zero is the one number a spend page must never show.

- **An `unresolved` range is `rate x [min, max]` where BOTH edges come from evidence, and the ceiling is the MINIMUM of every applicable bound.** This took three corrections, each of which had shipped a confident number in place of an unknown, so the reasoning is recorded in full:
  1. The first cut used `now` as the ceiling. That grew without bound: a six-week-old boot timeout on an SXM4 box reported "$1.33 to $37,580", and the figure rose every day nobody looked at it. **Why that is provably wrong, not merely ugly:** `unresolved` is only reachable when the row's own provider listed successfully and did *not* list this instance — so we have positively confirmed it is not running now. Anything still alive classifies as `orphaned` by construction. "It might have billed right up to this moment" is therefore a claim we have already disproved.
  2. The second cut ranked the bounds and took the first match, with `resolved_at` first. That priced a real seeded row — never active, never sighted, `resolved_at` stamped five days later by the first sweep that looked — at **$218.93** for a box that never passed a health check. **Evidence of absence at time T does not imply presence until T.** Each bound is *independently* an upper bound, so the ceiling takes `min()` over all of them rather than the first one that applies.
  3. `active_at` gates the boot-window term. `boot_timeout_seconds` bounds a launch only when it was *never demonstrably alive* (no `active_at`, no `last_seen_at`); a row that came up and crashed six hours later may have run for days, and capping that at the boot window would under-report. The floor follows the same completeness rule: `last_seen_at`, else `active_at`, else **0.0** — with no sighting at all the honest minimum is zero, because it may have died immediately.
  Adding evidence about a row can only tighten its ceiling, and `bound_basis` names which bound won so a UI can explain the width instead of printing a bare pair of numbers. **Alternatives:** a point estimate at the midpoint (a fabrication with error bars filed off), or refusing to show unresolved rows at all (the money is real; hiding it is how it goes unnoticed).

- **`UNWATCHED_GRACE_SECONDS = 3600` is the module's one heuristic, and it is deliberately generous.** `last_seen_at` is written only by the reconcile sweep inside `instances_with_state()`, which runs when something asks for the instances view — a dashboard poll or autopilot. The dispatcher's 30s adoption loop does **not** reconcile. So sightings stop the moment nobody is looking and the real gap between the last sighting and the disappearance is unbounded: hours, overnight, a weekend. An earlier comment claimed the sweep ran every 30s and sized the grace at 60s on that false basis. **Alternatives:** one sweep interval (rests on a sweep that does not exist), or derive it from the actual gap between sightings (we store only the latest, so there is no gap to measure without a second column). **Why an hour:** under-reporting is the failure this module exists to prevent, and being generous here is safe by construction — the ceiling is a `min()`, so a wide grace simply loses to a tighter evidenced bound (`resolved_at`, stamped the moment a sweep concludes) instead of inflating anything past it.

- **Liveness outranks price.** `hourly_rate_cents` is nullable, and the first cut checked it before checking whether the instance was alive — so a running box with no recorded price classified as `rate_unknown`, which zeroed `orphaned.count`, dropped it out of `live_burn_usd_per_hour`, and silenced the "this is burning money right now" alarm for exactly the row that most needed it. A live row now reports `billing`/`orphaned` with `usd = None`: known burning, unknown cost. `rate_unknown` still outranks `unresolved` (without a rate there is no money axis to build a range on), and an observed stop still outranks liveness (`terminate()` stamps at *issue* while a provider can keep listing the box for seconds afterwards, and checking liveness first would re-open a settled cost). `rate_unknown_count` counts by evidence — every row whose missing price cost us a number — rather than by state, so the signal survives the reordering.

- **Accepted limitation: an `unresolved` ceiling bounds what we can EVIDENCE, not what the account was charged.** A boot timeout does not terminate anything; the orchestrator's own error text says the instance "may still be running and billing". So `capped=True` means "bounded by the last thing we could prove", never "proven maximum", and this is the one place the module does not strictly over-report. **Why it is acceptable:** the gap is narrow by construction — anything genuinely still running is listed by its provider and classifies as `orphaned`, never `unresolved` — so the residual unknown is only "how long did it run after everyone stopped watching". It is named in the code, in `launch_cost`'s docstring, and here rather than papered over, because a reader who mistakes that ceiling for a guarantee would draw exactly the wrong conclusion from `capped`.

- **`ProviderUnavailable` gates every conclusion, per provider.** Phase 75 made a broken provider degrade to an empty list instead of 500-ing the instances view. That traded a crash for a **silent-data hazard**: an empty list is indistinguishable from "nothing is running", so the next sweep would have written off every launch of an unreachable provider as stopped — and, with spend on top, quietly zeroed the bill for boxes that were still burning. The sweep now tracks which providers actually *answered* (`listed_providers`) and `_may_conclude()` judges a row only if its own provider is in that set; `spend.launch_cost` takes the same `provider_listed` flag and keeps a live row billing when the evidence is absent. `live_ids=None` (no snapshot at all) means "trust the row" for the same reason. A crash is a visible failure; a silently wrong dollar figure is not, and the fix for the first must not create the second.

- **Bucketing is local to a caller-supplied `tz_offset_minutes`, with an accepted ≤1h DST defect.** Without it, a PST user's 6pm launch lands in tomorrow every evening. The offset is a single fixed number, so a window that straddles a DST change mis-buckets launches near the boundary by up to one hour — a launch within an hour of local midnight on a changeover day can land in the adjacent day. **Alternatives:** a full IANA zone (correct, but pushes zoneinfo, a zone-name round trip, and per-row timezone math into what is otherwise pure arithmetic), or bucket in UTC only (wrong for everyone west of Greenwich, every single evening). **Why:** the defect is bounded, twice a year, and never changes a *total* — only which day a launch is attributed to. Every response echoes `timezone_offset_minutes` and `timezone_label` so the user can see which midnight the numbers used. Revisit if per-day attribution ever becomes contractual.

- **A launch is attributed wholly to the day it STARTED.** **Alternative:** split a multi-day run across the days it spanned. **Why:** splitting makes every historical bucket depend on when you asked, so yesterday's chart changes shape overnight. Whole-launch attribution is stable and explains the one-day spikes a 52-hour fine-tune produces.

- **The spend routes read the last sweep, never the cloud.** `Orchestrator.last_cloud_snapshot()` returns copies of the ids and providers from the most recent `instances_with_state()` sweep; `/spend/*` pass them straight to `spend.py`. **Alternatives:** a fresh `list_instances()` per request (these routes are polled — that is an API call per poll, and the page's load time becomes the cloud's latency), or no evidence at all (rows trusted blindly, so an orphaned instance never surfaces). Before the first sweep the snapshot is `(None, None)`, which the cost model reads as "trust the row" — the safe direction.

- **Route validation is a translation, not a second copy of the contract.** `bucket` and `by` are validated by `spend.py` (which owns the valid values); the routes catch its `ValueError` and return a 400 carrying that message, and clamp `days` to 1..365 so a hand-typed `days=999999` cannot gap-fill a million buckets. A re-listed enum in `main.py` would be free to drift from the module that means it.

- **Every spend response carries `"mock"`.** Same rule as `/instances` and `/settings/status`. A dollar figure in a screenshot with no demo marker is the worst artifact this project could publish, and agents act on this data.

- **Mock demo history is real-shaped, not contorted.** `MANIFOLD_MOCK_SEED_DAYS=N` (mock mode only; 0 = off) fabricates N days of launch history so the spend page has a past. The seeder is gated twice — an explicit `mock=True` *and* a `<stem>-mock.db` filename read back from the live connection via `Database.open_path()`, not taken from the caller's argument — and every row it writes is greppable (`seed-0001`, …) with one `mock_seed` audit entry each. That second gate was originally written against the caller-supplied path string, which meant the one case it claimed to catch (right path, wrong database) was the one case it could not: a review demonstrated it writing twelve fabricated launches into a real ledger. A gate on a function that fabricates money has to ask the connection where it is actually writing. The fixture's one still-running launch originally carried **no** instance id, purely to stop the reconcile sweep closing it; that made the demo report `unresolved` and a live burn of `$0.00`, and it was a row shape real data cannot produce (a launch reaches `active` only *after* its instance id is stored). **Decided:** give it a real instance id and have `register_live_instances()` put a matching instance into the mock cloud's listing, so the claim is true on both sides. **Alternatives:** special-case `seed-` rows in the sweep or in `spend.py` (fixture knowledge leaking into the code that must be trustworthy). **Consequence, accepted:** that instance is running as far as every guard is concerned, so it consumes the concurrency slot and its $4.29/hr counts against the hourly budget — exactly as a real box would. Seeding is opt-in for that reason.

- **`db.create_launch` gained optional `launch_id` / `created_at` instead of being monkeypatched from outside.** The seeder had been rebinding `app.db.uuid` and `app.db.utcnow` inside a context manager to mint `seed-` ids at fabricated times. Two default-`None` parameters leave every existing caller byte-identical and keep `db.py` the only writer of the launches table. A fixture's convenience must never become a live writer's hazard.

- **One read-only MCP tool, `get_spend`.** An agent could launch GPUs but could not ask what it had spent, so it could not self-limit. The tool routes through the same `_call()` helper as every other (the AST thin-client guard still passes: no new imports), and its docstring states plainly that the number is Manifold-observed and an upper bound, that console-launched instances and filesystem storage are outside it, and that unknown costs are unknown rather than free.

- **Telemetry retention is accepted debt.** `telemetry_samples` still grows without bound; this phase only added the `(instance_id, at)` composite index so the windowed reads do not scan the whole table as it does. A retention policy (or rollup) is deferred, and named here so it is a known cost rather than a surprise.

- **Localhost trust, unchanged and now more valuable.** The backend binds to localhost and has no auth; the spend surface adds no new authentication, so it inherits that posture. It is worth restating because the new endpoints expose a complete financial history of the account — anything that can reach the port can read it. That is the same trust boundary the audit log and `.env`-backed settings already sit behind, and it is acceptable only as long as Manifold stays a local, single-user tool. Any future remote/shared deployment must solve auth *before* exposing `/spend/*`.

**Test coverage:** `tests/test_spend.py` (the six states and their precedence, minute round-up, each bound and which one wins, the ceiling proved identical at 40/80/365 days of row age, `active_at` exempting a row from the boot cap, a live row with no price still reporting as burning, tz bucketing, the aggregates), `tests/test_reconcile.py` (observed vs inferred stops, `last_seen_at` stamping, orphan repair leaving `active_at` NULL, per-provider scoping), `tests/test_mock_seed.py` (both gates, fixture shape, the running instance surviving a real reconcile sweep), `tests/test_spend_routes.py` (route shapes, timezone echo, 400s, day clamp, the demo marker, the no-cloud-call rule, seeding on/off and never in real mode, the MCP tool + its audit row). Full suite 639 passing.

Two of the three range corrections above were caught only by probing a seeded backend end to end (`MANIFOLD_MOCK_SEED_DAYS=30`, then reading `/spend/summary`), while the unit suite stayed green throughout. Unit fixtures encode the shapes we thought of; the seeder produces the shapes the system actually makes. Probe the running product before believing a number.

## Phase 76b — Idle spend: report-only, and unmeasured is never idle (2026-08-11)

- **A telemetry sample now describes the WHOLE BOX, and that is a safety fix rather than a completeness one.** The sampler recorded `gpus[0]` and nothing else, so on a multi-GPU instance `vram_used_mib` was a GPU-0 figure: a run that filled GPU 3 and left GPU 0 nearly empty reported low peak VRAM, and the right-size hint keys on exactly that number. The failure mode is the one the hint's own design note calls out — **a false "downsize" that OOMs the next run destroys trust** — reached not through a bad threshold but through an under-reported input. One row per sample still, but `vram_used_mib` and `util_pct` are now the **MAX** across the box's GPUs (max tightens the hint, which is the safe direction), plus two new columns: `util_pct_mean` (the mean across the cards in that sample) and `gpu_count`. **Alternatives:** one row per GPU (correct, but rewrites every reader of `telemetry_samples` and the estimates contract with it, for a number the hint would immediately collapse back to a max), or keep GPU 0 and document the limitation (the limitation is an OOM).

- **`telemetry_summary`'s `AVG(util_pct)` changed meaning on multi-GPU boxes, and it was allowed to.** It used to average GPU 0's utilization; it now averages the per-sample **maxima**, i.e. "how busy was the busiest card, on average". That is a shipped, user-visible number on the History page whose definition moved, so it is recorded here rather than left for someone to discover. **Why the max and not the mean:** `utilization_summary` feeds the right-size hint, and a hint that downsizes a box because seven of its eight GPUs were quiet is the exact OOM above. **Why the change is acceptable:** on a single-GPU box the number is bit-identical to what it always was, and on a multi-GPU box the old number was GPU 0's utilization presented as the instance's — not a different definition of a right answer, but a wrong one. Historical rows sampled before this phase keep their old meaning and cannot be corrected; they are single-column data about one card, and nothing marks them, which is an accepted (small) discontinuity in the History page's average.

- **ABSENT is not ZERO: every telemetry metric column is nullable, and the sampler writes NULL rather than 0.** A sidecar is frozen into an instance's cloud-init at launch, so a box that has been running since before a field existed reports a payload without it. `int(gpu.get("utilization_pct", 0))` would have turned "this sidecar does not report utilization" into "the GPUs were doing nothing" — and idle-spend accounting would then have billed that instance's entire lifetime as idle. The sampler collects only the values actually present and records NULL when there are none; `spend.idle_spend` reads NULL as unmeasured. **Alternatives:** version the sidecar payload and branch (more moving parts to answer a question NULL already answers), or skip the sample entirely when a field is missing (throws away the VRAM reading, which the old sidecar *does* report).

- **`spend.idle_spend` is REPORT ONLY, and the rule is structural rather than a current limitation.** GPU utilization may report but must **never** gate a destructive decision: low utilization is not proof that no work is happening (a memory-bound job and a served model between requests both read as idle), so idle auto-termination stays keyed on jobs and terminal activity and is deliberately unaware of `idle_spend` and its settings. The one action idle spend takes is raising a notification, which asks a person.

- **Idle accounting reads `util_pct_mean`; the right-size hint reads `util_pct` (the max). The two never cross.** If idle read the max, one busy GPU out of eight would hide seven idle ones and idle spend would be **systematically under-reported** — the single direction a spend-safety tool must never err in. If the hint read the mean, it would downsize a box on the strength of the quiet cards and OOM the next run. Each number takes the aggregate whose error runs in its own safe direction, which is why both columns exist instead of one.

- **A sampling gap is UNKNOWN, not idle.** Each sample speaks for at most `telemetry.sample_seconds` from its own timestamp and never past the next sample (so spans cannot overlap when sampling runs early); everything else in the window — before the first sample, after the last, and every gap between — is `unknown_seconds`. **Why:** the alternative is that an instance which went unreachable accrues idle time for the whole outage, which bills a user for being unmonitorable and reports the penalty as money. It also means boot lands in the window as unmeasured, which is exactly what it is: no sidecar is answering yet.

- **No samples at all is UNKNOWN, not zero idle.** Zero rows is the *normal* case for an adopted instance whose sidecar never came up and whose ssh fallback failed, as is a window of samples that all predate `util_pct_mean`. Both return `idle_seconds=None` with `unknown_seconds` covering the whole window, so a UI can say "not measured" and can never print `$0.00` about an instance nothing was ever known about. Same discipline as `launch_cost`'s `usd=None`, and the responses carry `coverage` so "$0.00 idle" over 2% coverage cannot be read as "$0.00 idle".

- **The window is `[launched_at, COALESCE(terminated_at, resolved_at, now)]`, and `active_at` cannot start it.** `active_at` looks like the better anchor — billing starts nearer to it — but Phase 76a leaves it **NULL on every orphan-repaired row** rather than fabricating a boot it never observed, so anchoring there would silently drop exactly the abandoned instances this accounting exists to find. Boot time therefore sits inside the window as unmeasured time.

- **It is called "idle spend", never "wasted spend".** Low utilization may be a memory-bound job, a served model between requests, or a data-loading stall. "Wasted" is a claim about whether the money bought anything, and we cannot support it; "idle" is a claim about what the GPUs reported, and we can. Every surface carries `IDLE_SPEND_DISCLAIMER`, which says both that idle is not wasted and that unmeasured time is not idle.

- **No fleet-wide idle-spend total on `/spend/summary`, deliberately.** That route is polled and reads exactly one query (`db.list_launches()`). An aggregate would need either one windowed sample load per launch — one query becoming N — or a `GROUP BY` over `telemetry_samples`, which **cannot reproduce the same number**: the span math needs each sample's *next* sample and each launch's own window, and a count-based approximation would silently convert sampling gaps into measured time, which is the precise under-reporting `idle_spend` exists to prevent, in a second implementation of a number `spend.py` insists on owning once. A wrong fleet total is worse than no fleet total. Revisit with a rollup table, not with a clever query.

- **The `instance_idle` notification fires once per instance, gated on time AND money.** Both gates because either alone misfires: 30 idle minutes on a cheap box is not worth an interruption, and $1 of idle spend on an 8xH100 happens in three minutes. An unknown cost fails the gate outright — "idle for a while, value unknown" is not worth interrupting someone for, and the message would have no number in it. Dedupe is `db.notification_exists(kind, ref)` on `idle:<instance_id>`, i.e. in the **table**, not in memory: the condition is still true on the next telemetry tick, and an in-memory set would re-ping everything after a backend restart. `NotificationCenter.notify_once()` requires a `ref` rather than degrading to `notify()` without one, because that degradation is the every-tick bug it exists to prevent.

- **Adding a notification kind is seven coordinated touchpoints, and a missed one fails SILENTLY.** `NotificationPrefs.wants()` returns False for an unknown kind, so a kind listed in `NOTIFICATION_KINDS` without a matching bool field is dropped with no error anywhere — the feature ships dead and nothing says so. `tests/test_unattended_safety.py::test_every_notification_kind_has_a_toggle` now asserts the two lists agree, so the next kind that lands in only one of them fails a test instead of failing in the field.

- **`db.record_telemetry_sample` gained an optional `at`.** Same shape and same reason as `create_launch`'s optional `launch_id`/`created_at`: a test needs samples laid out along a timeline, and the alternative is monkeypatching `db.utcnow` or writing rows through `_execute` from outside. No production caller passes it.

**Test coverage:** `tests/test_idle_spend.py` (the window and its three end bases; no-samples and old-sidecar-samples both unmeasured rather than zero; a gap, the pre-first-sample span, and out-of-window samples all unknown; the idle/busy split and its dollar figure; **idle reading the mean while the max says 99%**; the short-window refusal; `idle_usd=None` on an unknown rate; spans not overlapping under fast sampling; the whole-box recording with max/mean/gpu_count; absent fields recording NULL; the windowed read; the ping firing once with money in it, staying quiet below the money gate, never firing on unmeasured time, and honouring config), `tests/test_estimate_routes.py` (idle spend beside the right-size hint on one response, and unmeasured without telemetry), `tests/test_unattended_safety.py` (every kind has a toggle).

## Phase 76b — The max-lifetime ceiling, and the loop that carries it (2026-08-11)

- **The ceiling is anchored on `launches.launched_at` and read with wall clock, never `_clock()`.** The dispatcher's `_clock` defaults to `time.monotonic`; `launched_at` is a UTC ISO string in SQLite. Subtracting one from the other produces a number with no meaning, and that number decides whether a paid instance is destroyed. Wall clock is also the only anchor that survives a backend restart — which is the entire reason the ceiling lives in the database rather than in a timer. **Accepted cost:** a forward system-clock jump fires the ceiling early. That is acceptable *because the ceiling terminates with `force=False`*, so the worst case is an early, data-safe teardown that rescued first. **Alternative rejected:** monotonic bookkeeping, which cannot survive the restart the feature exists to survive.

- **The ceiling fires THROUGH a served model and defers only to a BATCH job.** `_is_server` is true for any template that publishes ports, and a `vllm-serve` task never leaves `running`. A ceiling that deferred to "any running task" would therefore be permanently unreachable on the most expensive workload Manifold runs — a feature that does nothing, which is worse than not shipping it. A batch job has a 90%, and destroying a fine-tune at 90% to save a billing hour is the trade this project refuses to make; a daemon has no 90%. **The IDLE verdict was not touched:** it still protects the full `auto_owned` and `pinned` sets, so a served model is as safe from idle termination as it has ever been. Two verdicts, two protection sets, on purpose.

- **The auto-managed carve-out excludes `terminating`, but that state gets a notification rather than a destroy.** `_OWNING_LIFECYCLE` includes `terminating`, and a blocked auto-managed teardown deliberately parks there retrying `force=False` forever. Treating that as "the lifecycle has this covered" would make the ceiling unreachable in the one state where money burns without bound. But issuing our own terminate there is also wrong: the lifecycle is already retrying, the outcome would be the same block, and racing it re-enters `rescue()` twice. So the ceiling notifies, naming what is stuck, and destroys nothing. **The literal control-flow sketch in the phase design collapsed this case into the "else: terminate" arm; the prose in both the design and the phase brief required "no second terminate, but DO notify", and the prose won.**

- **Out-of-range ceilings are REJECTED, never clamped, and the floor is boot-aware.** `launched_at` is stamped when the provider *accepts* the launch, before boot, and boot is 15-40 minutes on a multi-GPU box (`launch.boot_timeout_seconds` is 2400). Reusing `idle.timeout_min_seconds` (300) would let someone set a 30-minute ceiling on an 8xH100 and have it destroyed the instant it first connected, after paying for the whole boot. The floor defaults to `boot_timeout_seconds + idle.timeout_seconds` (4200s) and the rejection message names the boot budget. Silently doubling a number the user typed into a control that destroys instances is its own kind of lie: they would believe the box dies at 30 minutes and find it alive at 70. One definition (`orchestrator.max_lifetime_bounds` / `validate_max_lifetime`) serves both write paths, because a bound two call sites compute separately is a bound with a hole in it.

- **An unreachable instance outlives its ceiling, and the copy says so.** `rescue()` returns an empty report when the connection is down, so terminating an unreachable box destroys its data behind a rescue that did nothing. The loop therefore refuses, records it once, and pings once. Every surface reads "Manifold terminates it then, **if it can reach it and save its files first**" rather than promising a bound that SSH can break.

- **Ceiling fields sit on the INSTANCE dict, not inside `inst["idle"]`.** `idle` is `None` whenever the instance is not connected — which is exactly the box whose ceiling the user most needs to see (see above). `test_hardening.py::test_instances_expose_idle_countdown` still asserts the `idle` key set exactly, now with a comment saying why the ceiling is not in it.

- **Deferrals and ceiling pings dedupe in memory; the blocked-termination retry backs off.** The idle loop runs every 15s: an audit row per pass is 5,760 rows a day per instance, and the shipped `_notify_blocked` fired an in-app row *and* an OS ping on every blocked retry — about four desktop notifications a minute, indefinitely, all saying the same thing. Now: one ping per blocked instance until the unsaved file **set** changes (paths only, since sizes and error strings jitter between attempts), and blocked retries back off 15s → 30s → 60s → … → 15 min per instance, because each retry re-runs the entire rescue (sidecar walk, whole-scratch rsync, per-file SSH downloads) against files that have not moved.

- **`_terminate_for(instance_id, kind, detail)` takes no `force` parameter and no `**kwargs`.** It is the single destructive call in the loop and it calls `terminate(force=False)`. The absent `**kwargs` matters as much as the absent `force`: it is how a `force` argument gets added by accident later. Its docstring says outright that adding one would be adding an automatic destroy path, which needs its own phase and its own gate.

- **Per-instance exception isolation in `_check_idle` (pre-existing bug, fixed here).** Only `TerminationBlocked` was caught, so a `ProviderError` out of `terminate()` escaped to `_idle_loop`'s blanket handler and **abandoned every instance later in the iteration** — one poison box at the front silently disabled idle termination for every other box, every cycle. 76b adds a second entry point into this loop, so it does not get to build on that.

- **`set_keep_alive`'s audit line no longer over-promises.** "idle auto-termination off" was the whole truth until an instance could also carry a ceiling, which keep-alive does not lift. The audit row and the card now both say so, on the instances that actually have one.

- **A new notification kind, `instance_ceiling`, separate from `instance_idle`.** Switching off idle-spend chatter must not also switch off the warning that a box is about to be terminated. The warning fires once per instance on a **fixed** 600s lead (90% of 30 days is three days of nagging; 90% of 70 minutes is seven minutes of notice) and is suppressed entirely when the ceiling is under 2x the lead, where it would land at launch and mean nothing.

**Test coverage:** `tests/test_idle_matrix.py` is a golden fence — the pre-76b decision matrix (auto-managed, auto-managed-in-`terminating`, pinned server, pinned batch, disconnected, keep-alive, under/over timeout, no launch row, blocked termination), written and made green against the *unmodified* `_check_idle` and passing unedited afterwards; any change to it is an unintended behaviour change. `tests/test_ceiling.py` covers the ceiling: not firing when unset (the default, and the most important test in the file), firing from `launched_at` rather than activity, surviving a restart, firing through `vllm-serve` while that same task still blocks idle termination, deferring to `whisper-batch`, notifying-but-not-destroying an auto-managed job stuck in `terminating`, overriding keep-alive, honouring `TerminationBlocked` and backing off, the unreachable case, NULL/garbage/naive `launched_at`, an orphan-repaired row firing within one poll, a `ProviderError` on one box not abandoning the next, the rejection at both write paths, the card fields, mock fixtures carrying no ceiling — and one test that drives every branch that can destroy and asserts on the **kwarg** that all four calls were `force=False`.

## Phase 76c — The money on screen, and a first run that says what this is (2026-08-11)

**Context:** 76a made the spend numbers true and 76b bounded what a running
instance can cost. Neither put the number anywhere a person meets it. This
phase is the surface: a burn-down against a monthly wallet, a chart, and the
walkthrough a brand-new install gets before it is offered a GPU.

**Decided & why:**

- **The monthly budget is advisory, and that is a deliberate refusal to
  enforce.** `max_concurrent_instances` and `max_hourly_spend_usd` are RATE
  ceilings the orchestrator checks before any provider call. `monthly_budget_usd`
  is a different kind of number: a cumulative wallet, reconstructed from the
  launches Manifold started, and therefore a **lower bound by construction**
  (an instance started from the provider's own console has no launch row and
  is invisible to it). **Alternatives:** enforce it in `_guard_capacity` beside
  the rate guards, or enforce it behind an opt-in "hard stop" flag. **Why not:**
  blocking a launch on a number we know is short refuses work without
  protecting the wallet, and it would decide from our own database — exactly
  the class of evidence `_guard_capacity` deliberately distrusts when it
  fetches `list_instances(fresh=True)`. A third guard that fires late and
  non-deterministically is spend theatre. So the budget warns loudly, in the
  burn-down, in a notification at 50/80/100%, and in copy on both the Settings
  field and the panel that says outright it does not block a launch.
  `test_a_monthly_budget_never_refuses_a_launch` pins it: 12x over the wallet,
  the launch is still admitted on the rate guards alone.

- **The projection answers "if I leave this running", not "what will I spend".**
  `projected_month_end_usd` and `exhausted_on` extrapolate the CURRENT burn to
  the month boundary. **Alternative:** extrapolate past spend linearly
  (month-to-date / days elapsed x days in month). **Why:** the linear version
  answers a question nobody asks and is wrong in both directions — it
  under-reports on the day you start a big run and over-reports for a week
  afterwards. The burn version answers the actionable one ("at this rate the
  budget runs out on the 13th"), and with nothing running it reports
  month-to-date unchanged, which is correct rather than pessimistic. Nothing
  is projected past the month boundary, because the wallet resets there.

- **The chart is hand-rolled SVG, and it is bars.** No charting library: the
  bundle ships inside a Tauri desktop app, everything else here (telemetry
  sparklines, progress bars) is drawn the same way, and the two dependencies
  already installed and unused are argument enough against adding a third.
  What it needed that `Sparkline` could not give is a real domain — that helper
  hardcodes 0-100 because it draws percentages, and money has no ceiling — so
  this one computes a "nice" axis maximum and labels it. **Bars rather than a
  line:** a line implies a continuous quantity sampled over time, but spend is
  a sum per bucket and an empty bucket is a true zero, not a gap to interpolate
  through. Hit areas are full bucket height, because a $0.02 bar is one pixel
  and otherwise unhoverable.

- **Onboarding cannot offer a demo BUTTON, so it offers a command.** Mock mode
  is decided when the backend process starts: it swaps every client, uses a
  different database file, and deliberately refuses to boot when the real
  database still has a live launch. A "Try the demo" button would therefore
  restart the backend and could leave the user with no backend at all, in the
  exact situation the mock-isolation guard exists to protect. The walkthrough
  shows the command instead and explains why it is a command. **Alternative:**
  a frontend fixture layer that fakes demo data client-side — rejected, because
  a dashboard that renders invented dollar figures is precisely what 76a spent
  its whole budget removing.

- **The walkthrough sets a spending cap before it offers a GPU.** Step three is
  the guardrails, not an afterthought in Settings. Every user then has a real
  hourly ceiling and a budget from minute one, and meets the product's actual
  premise during setup rather than after their first surprise.

- **Onboarding state lives in `Preferences`, not `localStorage`.** localStorage
  answers differently in the desktop shell and in a browser pointed at the same
  backend, so the same install would greet you twice or never. Adding the
  section surfaced the older bug and fixed it: `PreferencesPatch` in `main.py`
  omitted `worklog`, and `model_dump(exclude_none=True)` dropped the field
  before the handler saw it, so `PUT /preferences {"worklog": ...}` returned
  **200 with the value unchanged** — a silent success on a failed write.
  `worklog` and `onboarding` are both listed now, and
  `test_preferences_round_trip_every_section` is parametrised over every
  section so the NEXT one fails in the suite instead of in production. The
  frontend has the same guard for free: `Preferences` is a total type, so
  TypeScript refuses to compile `PolicySettings`' optimistic merge until a new
  section is spread there too.

**Test coverage:** `tests/test_budget.py` — the burn-down states (unset is not
$0, 80% warns, over reports how far over rather than clamping), the projection
with and without a live burn, an exhaustion date only when it lands inside the
month, December rolling into January, the budget never refusing a launch while
genuinely 12x over, `/spend/summary` reporting it, the negative-value clamp,
the notification toggle existing, and the every-section preferences round trip.
Full suite 723 passing; dashboard builds clean.

## Phase 77 — Task dependencies: real edges, guarded (2026-08-12)

- **A DAG by construction, not by detection.** `depends_on` is settable only at
  enqueue, may only reference tasks that already exist, and is immutable after.
  That one rule makes cycles impossible without a cycle detector: a new task
  cannot be depended on by an older one, because the older one's deps were
  frozen before the new task existed. Validation is therefore id lookup plus
  status checks, no graph traversal. **Alternative:** editable deps with a
  cycle check at write time — rejected; it buys a feature nobody asked for at
  the price of a whole class of scheduler deadlocks having to be provably
  impossible instead of trivially impossible.

- **`skipped` is a first-class task status, not a flavor of `failed`.**
  `failed` says the job ran and broke; a skipped job never ran, and pretending
  otherwise poisons everything downstream of the status — runtime/cost
  annotations, the failure card's log tail (there are no logs), the worklog,
  the "reuse this failed job's error" instinct. The status sweep touched every
  enumeration: badge tones (deliberately zinc, not red — the failure is the
  parent's, and red here would double-report one root cause down the chain),
  clear-finished, the SSE terminal checks (which also carried a phantom
  `"canceled"` state that never existed), the MCP `_task_settled` helper, and
  the agent-facing docs.

- **Two gates, because there are two moments money moves.** The dispatch scan
  (`_pick_dispatchable`) holds a manual child until every parent succeeded.
  Auto-manage promotion (`_next_ready_auto_job`) holds the LAUNCH: a child
  promoted early boots a billing GPU that then sits waiting for its parent.
  The child waits on a task, not on the slot, so a younger independent auto
  job is promoted past it and nothing starves. The A(auto) -> B(auto) pipeline
  comes out of this for free: A launches, runs, syncs to the filesystem,
  terminates; only then does B launch a fresh box and read A's outputs off the
  filesystem. Zero overlap; the filesystem is the data bus.

- **Failure cascades through the settle funnel, once, loudly.** `_finish_task`
  is already the single funnel for every completion path, so the cascade hooks
  there: any non-success settles every queued dependent (transitively) as
  skipped, with the dead edge named one hop at a time. The cascade runs even
  when the settle itself is notify=False (a user cancel) — children dying is
  not optional bookkeeping. Notification stays one ping: the root failure's
  existing job_failed message gains a "Skipped downstream" line instead of N
  skip pings. The dispatch scan re-checks doomed deps as a self-healing
  backstop (a crash between settle and cascade cannot strand a child queued
  forever), and a dep whose row is missing settles as skipped rather than
  waiting for a parent that no longer exists.

- **Deleting a parent a queued child still needs is a 409, not a cascade.**
  The dependency gate reads parent rows live, so removing the row would leave
  a dangling edge. Remove-single blocks with the dependents named;
  clear-finished silently keeps such parents (clearing history must stay a
  safe bulk action, so it skips rather than errors). **Alternative:** resolve
  dep status onto the child at settle time so parent rows become disposable —
  rejected as state duplication; two copies of a status will eventually
  disagree, and the guard is two small queries.

- **Server templates cannot be dependency parents (422 at enqueue).** A server
  never exits on its own, so "after it succeeds" would mean never — a job that
  waits forever by construction, hidden inside a legitimate-looking feature.
  The error teaches the working pattern instead: server and batch coexist on
  one instance by design, so a batch job that needs a live server just targets
  the server's instance. A "run after the server is UP" edge type is real and
  deferred; it is a different edge, not a different opinion about this one.

- **The task graph now draws only real edges.** The panel previously chained
  `tasks[i-1] -> tasks[i]` in list order — two unrelated jobs rendered as a
  pipeline, an invented dependency on the same screen that was scrubbed of
  invented dollars in 76a. Edges now come from `depends_on` only, layout is
  by dependency depth (independent jobs all in column 0, no arrows), a skipped
  child's edge renders severed (dashed), and the header claims exactly what
  the panel shows. Rider fix in the same spirit: the cluster launch modal's
  burn-rate preview invented $24.72/hr when the catalog had not answered; it
  now says "rate unavailable" — never a made-up number on the screen where the
  user decides to spend.

- **Spend-route tests no longer depend on the time of day.** Two 76c tests
  placed a launch "3 hours ago" and asserted it in today's bucket — false
  between 00:00 and 03:00 UTC, which is evening PDT, which is when this phase
  ran the suite. The fixture now anchors the caller's timezone so "now" is
  local noon: same-day at any wall-clock time, and the tz plumbing gets
  exercised for real instead of defaulting to UTC.

**Test coverage:** `tests/test_task_dependencies.py` — the dispatch gate
(queued/running parent holds the child, success releases it, dep on an
already-succeeded parent is satisfied, independent tasks flow past a waiting
child), the cascade (chain skip with per-hop reasons, mixed parents, cancel
cascades with notify off, exactly one ping naming the downstream, missing-row
self-heal), the auto-manage gate (a child is never offered the launch slot
while its parent is unfinished — the spend test — a younger independent job
takes the slot past it, a doomed auto child leaves the pending scan with a
terminal lifecycle), enqueue validation (unknown/dead/server parents,
dedupe), resolution on list/get, both delete guards, skipped reaping, and the
pipeline over HTTP (ordered by timestamps; cancelled root skips the chain
with the skip in the event trail). Full suite 745 passing; dashboard builds
clean; live probe against a running mock backend confirmed the 422s, the 409
guard, the transitive cascade, and A-before-B ordering on a real dispatch.

## Phase 78 — A local API token: loopback is not authorization (2026-08-12)

- **Enforcement is conditional on a token existing; generation is gated on
  production wiring.** `Settings.api_token` comes from `MANIFOLD_API_TOKEN`
  in .env. Empty means no middleware at all — which is what keeps mock mode
  a zero-credential demo and every existing TestClient app green untouched.
  create_app mints a token ONLY on the same production test the image
  checker already uses (not mock, no injected client), persists it via
  update_env_file, and REFUSES TO BOOT (SystemExit) if the write fails: a
  real backend is never silently open, and a memory-only token would strand
  every client at the next restart. **Alternative:** enforce always and
  seed tests with a token — rejected as ~100 fixture edits for no security
  gain, and mock mode would stop being zero-setup.

- **A .env we create is chmod 600.** The generated token (and, later, the
  Lambda key the Settings page writes into the same file) should not be
  world-readable. Only on creation: an existing .env's permissions are the
  user's own business to tighten or not.

- **Pure ASGI middleware, installed inside CORS.** BaseHTTPMiddleware never
  sees WebSocket scopes (they would sail past auth) and shims streaming
  responses. Ordering is load-bearing twice: CORS must answer the browser
  preflight OPTIONS (which never carries Authorization) before auth can
  401 it, and a 401 must pass out through CORS to gain
  Access-Control-Allow-Origin — without it the :3000 dashboard reads the
  401 as a network error and the token gate never appears. Both directions
  are pinned by tests.

- **Exact-path exemptions, default deny.** The dashboard's page routes,
  their .html/.txt export spellings, /icon.svg, and the /_next/ asset
  prefix are open so the shell can render and ask for the token; every
  API route 401s, including /settings/lambda-key and /storage/files which
  live UNDER exempt page paths — the reason the list is exact matches and
  not prefixes. A route-table walk in test_auth.py asserts every non-pinned
  route refuses unauthenticated, so adding a route without thinking about
  auth fails the suite.

- **WebSockets: accept, then close(4401).** A pre-accept denial surfaces in
  browser JS as an opaque handshake failure; accepting first exposes
  close.code to clients that want it. The dashboard deliberately does not
  key on 4401 — the code is a courtesy, not a contract. Browser WS cannot
  set headers, so ?token= is accepted on WebSocket routes only; on HTTP the
  master token NEVER rides a query string (uvicorn's access log records the
  request line).

- **Downloads use single-use nonces, not the token in the URL.** The two
  <a>-style download escapes mint a ~60s single-use nonce over the authed
  channel (POST /downloads/token) and pass it as ?nonce= — the only
  query-string credential, valid for exactly those two GETs. In-memory
  store, expire on use and TTL. Deliberately NOT bound to a specific path
  this phase (boring first); binding nonce→path is the obvious tightening
  if it ever matters.

- **/v1 keeps its own scheme: proxy key if set, else the API token, open
  only when neither exists.** The OpenAI error envelope stays (SDKs parse
  {"error": {...}}). "Open when neither" preserves
  test_openai_proxy.py::test_proxy_open_when_no_key AS-IS in the harness,
  while production — which always holds a token after first boot — is
  fail-closed. The old hardcoded "manifold" OPENAI_API_KEY injected into
  hub shells died with the open proxy; shells now get the active
  credential ("unused" only when the proxy is genuinely open, purely to
  satisfy SDKs that refuse empty strings).

- **The Tauri shell hands the token over as /?token=... .** It reads the
  packaged app's data-dir .env (the same file the backend generates into,
  which exists before the port answers) and navigates once; the dashboard
  stores the token and scrubs the URL via history.replaceState. No control
  protocol, no second handshake file — a second plaintext copy with its own
  lifecycle for zero benefit. The dev-reuse branch (port already serving
  from a checkout) navigates bare and the paste gate covers it.

- **mcp.json gets a plaintext copy of the token — deliberate secret
  sprawl.** MCP clients spawn the bridge with only the config's env block,
  so claude_integration emits MANIFOLD_API_TOKEN into it when set. The
  caveat is rotation: changing the token in .env does NOT update mcp.json;
  re-run the setup. The desktop `manifold-backend --mcp` avoids the copy
  entirely by reading the app's own .env at startup. The AGY skill format
  has no credential field at all, so it now says so loudly in the emitted
  YAML (mock mode or an explicitly empty token required) instead of
  breaking silently.

- **Two existing tests had to change, both because they built production
  apps.** test_ide_attach built create_default_app() inside the suite —
  under Phase 78 that GENERATES A TOKEN INTO THE DEVELOPER'S REAL .env
  (caught live when the repo .env changed checksum mid-run; restored
  byte-identically). It now uses harness wiring. test_desktop_mcp's
  default-mode test stubs the factory for the same reason, and its
  run_mcp tests pre-set MANIFOLD_API_TOKEN so the bridge's .env fallback
  never reads the developer's file.

**Test coverage:** `tests/test_auth.py` — 401/success on a spend route
(with the launch never reaching the cloud), constant-time comparison
pinned to source, WS refusal (4401) and acceptance via header and
?token=, exact-vs-prefix exemptions, the CORS preflight/readable-401
pair, the full route-table drift guard, the nonce lifecycle (mint needs
auth, single use, TTL expiry, not a general credential), /v1 dual
credential with the OpenAI envelope, mock mode open with zero .env
writes, harness apps writing nothing, real-mode generate+persist+chmod
600, preset token respected, SystemExit on persist failure, and
presence-only /settings/status. Full suite 769 passing; dashboard builds
clean; live uvicorn probe confirmed enforcement end to end (see phase
report).

## Phase 79 — Principals: who did what, before anyone asks who may (2026-08-12)

- **Attribution before authorization.** RBAC (Phase 80) decides what a caller
  may do; that is meaningless until rows record who DID things. So this phase
  is names, threaded everywhere money moves: launches, tasks, watches, agent
  runs each carry `created_by`, and every request-driven audit row's actor is
  the resolved principal instead of a hardcoded "api"/"dashboard" guess
  (which recorded the client kind someone assumed at write time, not the
  caller - any client can hit any route).

- **Tokens resolve to names; the database stores hashes.** `api_principals`
  keeps sha256(token), never the value: the mint response is the only place
  the token exists, and a stolen table is a set of fingerprints, not
  credentials. The .env token resolves to "owner" without a row - it is the
  bootstrap credential and deliberately cannot be revoked through the API
  (revoking the recovery path locks you out of the lock). Revocation keeps
  the row: created_by columns point at names, and history outlives the
  credential it came from. Revoked names stay taken for the same reason.

- **One authorization rule ships a phase early: only "owner" manages
  principals.** Without it, any minted token could mint more, and revoking a
  leaked credential would race against it re-issuing itself. This is
  explicitly the seed of Phase 80, not scope creep: roles will subsume the
  check.

- **Chain attribution, explicitly threaded.** The auto-manage loop, capacity
  watches, and autopilot runs launch GPUs from background tasks that never
  saw a request, so `request_launch` takes `created_by` as a parameter and
  each loop passes the chain's origin: the job's creator, the watch's
  creator, the run starter's name (rebound via `bind_principal` at run-loop
  start, so a run's attribution survives however the task object was
  created). The request context (`current_principal()`, a contextvar set by
  the auth middleware) is only trusted at request boundaries. Audit actors
  for loop EVENTS stay "backend"/"autopilot": what acted vs. who it acted
  for are different columns on purpose.

- **Historical rows read as unattributed, never guessed.** `created_by` is
  NULL on every pre-79 row and on anything created while auth is off; the UI
  shows nothing rather than inventing an owner. `current_principal()` falls
  back to "api" only for audit actors, keeping open-mode history consistent
  with what those rows always said.

- **Download nonces carry their minting principal**, so the one authenticated
  GET that cannot send a header still attributes to the person who clicked.

- **A closure-local Pydantic model silently becomes a query parameter.**
  `PrincipalRequest` defined inside create_app 422'd every POST: with
  `from __future__ import annotations`, FastAPI resolves the string hint
  against module globals and a closure-local class is not there. The model
  moved to module level with the comment; the failure mode is recorded here
  because it looks exactly like a client bug and diagnoses as one.

**Test coverage:** `tests/test_principals.py` - hash-only storage and the
show-once mint; minted tokens authenticating, attributing tasks/launches, and
naming their audit rows; the owner-only management rule (403 for minted
tokens, list still open); instant revocation (401 on next request, name kept,
409 on re-revoke and on name reuse); reserved/malformed names; management as
409 when auth is off; resolver unit behavior (owner without a row,
unknown/revoked rejection, throttled last_used_at); nonce-principal
round-trip; auto-manage chain attribution at the dispatcher level; NULL on
pre-attribution rows. Full suite 792 passing; dashboard builds clean; live
probe confirmed 401/200/403 behavior, named audit actors, chain attribution,
and instant revocation against a running backend, repo .env untouched.

## Phase 80 — Roles: viewer observes, operator works, admin governs (2026-08-13)

- **The role table is closed, and closing it is the design.** Every route's
  minimum role lives in one dict (`auth.ROUTE_ROLES`); `RoleTable.build`
  walks the app's REAL route table at startup and refuses to boot over an
  unclassified endpoint. Deciding who may call a route is part of shipping
  it, enforced the same way the exempt-path list is enforced: default deny,
  and a drift that compiles is a drift that cannot happen. Matching reuses
  Starlette's own compiled path regexes, so the middleware agrees with the
  router about which route a path is.

- **Three roles, one rule above them.** viewer reads and estimates (and a
  GET that executes on the instance - sidecar diagnose - is classified as
  work, not observation: the method is not the semantics); operator does
  everything that moves money or runs code; admin touches secrets, policy,
  and credentials. Above the table: only the OWNER token mints or revokes
  admin credentials, both directions - an admin minting admins is lateral
  escalation, the exact leak mode RBAC exists to stop. Owner itself is
  always admin and cannot be demoted (the recovery path again).

- **Approving a gated action is operator, not admin.** The approval IS the
  spend decision, and spend is operator's job; making it admin would turn
  every autopilot approval into an interruption for whoever holds policy.

- **/v1 is role-gated inside its own scheme, not by the middleware.** The
  middleware passes /v1 through untouched (its 403 shape would break
  OpenAI SDK clients) but still binds the caller's identity; the proxy
  then enforces viewer-for-models, operator-for-chat in the OpenAI error
  envelope. CONTRACT CHANGE from 78: the dedicated proxy key is no longer
  exclusive - minted principals are legitimate /v1 callers now that roles
  gate them, and exclusivity locked every principal out of the proxy the
  moment a proxy key existed while buying nothing (the api-token holder
  already had full power). The proxy key remains the no-principal
  credential for pure model tools.

- **Open mode ignores roles entirely.** No token = no identities to rank:
  `current_role()` falls back to admin so neither the harness, mock mode,
  nor background loops can ever be blocked by a rank check. Role checks
  bind only where an identity was actually resolved. Unknown role strings
  rank below viewer - a corrupted value fails closed.

- **Pre-80 principals default to operator** - exactly what a minted token
  could do before roles existed: act, but not manage credentials or
  policy. No stored row gets more powerful by upgrading.

**Test coverage:** `tests/test_rbac.py` - the ranking (unknown fails closed
both ways); viewer reading but not spending/acting (403 names have and
need), viewer WS split (terminal 4403, metrics allowed); operator working
but not governing; admin governing but blocked from admin credentials both
directions while owner is not; unknown role 422; a 79-era row acting as
operator; /v1 viewer/operator split in the OpenAI envelope; the builder
refusing an unclassified route; open mode unaffected. Full suite 806
passing; dashboard builds clean; live probe walked the ladder on a running
backend (viewer 200-read/403-launch, operator 202-launch/403-govern,
/v1 permission_error envelope), repo .env untouched.

## Phase 81 — Team mode: two walls, a ledger, and a database decision (2026-08-13)

- **The network policy is judged per request, not per boot.** The backend
  cannot know how uvicorn was started, but every connection knows the
  interface it arrived on (scope["server"], the listening socket's own
  address). NetworkGuardMiddleware - installed UNCONDITIONALLY, outermost,
  because its whole job is the case where auth is NOT configured - refuses
  non-loopback requests when no token exists, and refuses plaintext
  non-loopback requests without the explicit `server.allow_plaintext_lan`
  opt-in (a bearer token on an unencrypted LAN hop is a credential
  broadcast; the opt-in exists because a Tailscale/WireGuard tailnet
  already encrypts below http). A non-IP server host ("testserver") reads
  as local: real uvicorn reports numeric socket addresses, so a hostname
  means a test client. **Alternative:** a boot-time bind check - rejected,
  the app never reliably sees its bind; per-request judgment holds under
  any launcher, reverse proxy, or multi-interface host.

- **The per-principal ceiling is a real guard in the orchestrator, judged
  against the same live baseline as the global guards.** Same
  pending-launch double-admit protection, filtered to the principal; chain
  attribution makes it bind (an auto-managed job's launch counts against
  whoever enqueued the job). The refusal names the principal, its current
  burn, its ceiling, and the fix. "owner" and legacy actors have no row
  and no ceiling; a row without a ceiling is unlimited; the advisory
  monthly wallet stays global and advisory - one enforced RATE ceiling
  per principal is legible, a per-principal monthly wallet would be four
  more numbers explaining themselves.

- **Phase 79 missed cluster attribution; 81 needed it and fixed it.**
  launch_cluster never took created_by, so cluster nodes were
  unattributed - which would have made the ceiling trivially evadable by
  launching clusters. The whole cluster is now attributed to one
  principal and judged atomically against their ceiling.

- **SQLite stays, deliberately.** Team mode is one shared backend process,
  not N backends sharing a database: behind one process, WAL-mode SQLite
  covers a small team's write rate, and every guard remains an in-process
  transaction. What would force Postgres is backend REPLICAS - which would
  also need distributed guard state, a different product. The
  Database/TaskQueue interfaces remain the swap point; deciding now, in
  writing, beats deciding implicitly by never thinking about it.

- **Spend gains the team grouping.** breakdown by="created_by", with
  pre-attribution rows reading "unattributed" - a true statement, never a
  guess - and the dashboard's "Where it went" toggles hardware/principal.

**Test coverage:** `tests/test_team_mode.py` - ASGI-level network guard
(loopback always passes; no-token network refusal; plaintext refusal
naming the opt-in; opt-in honored; TLS needs none; WS closes 4403; the
IP classifier), the ceiling refusing the crossing launch with the numbers
in the message, per-principal isolation (global guards still bind above
everyone), owner/uncapped unlimited, pending launches counting against
the ceiling, 422 on nonpositive ceilings, breakdown by principal. Full
suite 818 passing; dashboard builds clean; live probe on a REAL 0.0.0.0
bind through the machine's LAN address confirmed both walls, the opt-in,
the ceiling refusal with exact numbers, and by-principal spend; repo
.env untouched.

## Phase 82 — Policy as code: the guardrail you review in a pull request (2026-08-13)

- **policy.yaml constrains WHICH; config.yaml keeps HOW MUCH.** The numeric
  dials (concurrency, total hourly budget) are the owner's own limits and
  stay where they were. The policy file is the half a TEAM reviews:
  instance-type and region allowlists (fnmatch, so `gpu_1x_*` reads the way
  an ops reviewer expects), a per-instance rate cap, a required max
  lifetime - globally and per role. Role blocks TIGHTEN the global block
  and can never widen it: both must pass. It binds every principal
  INCLUDING the owner; the way around policy.yaml is a commit to
  policy.yaml, which is the entire point.

- **Asymmetric failure semantics, on purpose.** Missing file = fully
  permissive (a fresh install is not policied). Present-but-invalid =
  refuse to boot, unknown keys included: a typo'd `alowed_regions` that
  loaded as "no opinion" would be a hole shaped exactly like a guard.
  This deliberately breaks the config-loader convention that garbage can
  never stop the backend - preferences are conveniences, policy is a
  guard, and the two failing the same way would be the wrong symmetry.

- **Deliberately not editable from the dashboard.** GET /policy (and the
  Settings card) report what is enforced and from which file; changing it
  is an edit plus a restart. An API that could rewrite the policy would
  collapse the one property that distinguishes it from preferences: that
  every change has a diff, an author, and a reviewer.

- **Enforced in the orchestrator, before any money-side guard.** A denial
  costs nothing, names its rule and its file, and lands in the audit
  trail with the principal's name (enterprise question: who ASKED for
  the thing policy refused). Fallback instance types pass the same
  policy the requested type passed - a fallback that policy denies is
  skipped, exactly like one the budget denies. Clusters are judged
  before the cluster row is created (no ghost rows), and a
  require_max_lifetime policy honestly denies clusters until clusters
  learn lifetime ceilings - recorded here rather than silently exempted.

**Test coverage:** `tests/test_policy.py` - the pure engine (patterns,
rate cap, lifetime requirement, role tightening incl. "a role block can
never re-allow"), loading (missing permissive; nine invalid shapes each
refusing with the offending fragment named), enforcement over HTTP (the
owner denied and audited, role rules biting a minted operator then
admitting with a lifetime, region denial isolated from the
filesystem-region validation via a scratch-only launch, cluster denied
with no row left behind), GET /policy and the settings flag. Full suite
839 passing; dashboard builds clean; live probe confirmed the owner
denial naming the file, the operator lifetime rule, a compliant 202,
and a typo'd policy refusing to boot with the unknown key named.

## Phase 83 — The Foundry: recipes, not platform (2026-08-14)

- **The whole phase is three YAML files and a doc, on purpose.** Everything
  "train your own model" needs was already built by earlier phases:
  auto-manage rents-trains-syncs-terminates, depends_on chains
  fetch-then-train, the rescue saves checkpoints from a dying box, the
  ceiling/policy/budget stack binds training runs identically, and the
  estimator honestly reports no history until the first real run seeds it.
  Adding a "training platform" module would have duplicated all of it.
  **Alternative:** a dedicated training API/UI — rejected until the Forge
  wizard (planned Phase 85) proves recipes alone are insufficient.

- **The robot-arm story leads because it is the honest from-scratch.** A
  desk-cleaning arm needs a policy network (~50-80M params) trained from
  random weights on the user's own demonstrations — genuinely "from
  scratch," hours on one A10, a few dollars. An LLM is the wrong tool and
  the doc says so. The walkthrough is executable by someone with NO robot
  (public pusht dataset), because a doc that requires hardware to follow
  is an ad.

- **Defaults are the cheap path.** nanogpt-pretrain's default run costs
  under a dollar and ends by SAMPLING from the model it trained — proof of
  life as generated text in the job log. The serious multi-hour pretrains
  are documented script-run recipes, not bundled defaults: a template whose
  default costs $100 is a trap, not a recipe. Same instinct: the doc leads
  with a steps=2000 proof run (~$0.30) before the full 100k.

- **Public bases only; version drift stated, not hidden.** smolvla-finetune
  pulls only the public lerobot/smolvla_base, so no secret-injection
  plumbing was built for gated models (script-run's .env convention already
  exists for those). The LeRobot image tag floats and its CLI moves: the
  loader's drift warning stays on both lerobot templates, the pinned
  pytorch tag keeps nanogpt warning-free, wandb is disabled explicitly (an
  unattended job must never sit waiting on a login prompt), and the doc
  tells users exactly where a flag-rename failure will surface and how to
  fix it. Exact CLI flags get re-verified against the image at each
  real-run gate; the golden render tests pin whatever the gate proves.

**Test coverage:** `tests/test_foundry_templates.py` — all three load
through the mount jail with no ports and no network; dataset templates
require exactly one parameter (nanogpt requires zero: the cheap path needs
no decisions); golden renders (from-scratch ACT invocation with wandb off,
smolvla pulling the public base, nanogpt cloning-training-sampling with
the pinned image); the drift warnings present on exactly the floating
images; and the walkthrough's pipeline shape over HTTP — gpu-smoke chained
to lerobot-act via depends_on against a mock instance, held-then-ordered,
rendered command and declared outputs verified in the job log. Real-GPU
gate pending (one A10, pusht, steps=2000, ~$0.50): it doubles as the
phase-74 subagent smoke test and the auth-stack shakeout.

## Phase 83 gate — what one real A10 taught, $0.92 all-in (2026-08-14)

The first real-hardware session since the auth stack landed, and the
Foundry's proving run. Everything below was learned live and encoded the
same hour.

- **The headline worked**: 2000 ACT steps from random weights on pusht,
  22 steps/s on an A10, checkpoints at 1000/2000 on the persistent
  filesystem, $0.04 of compute for the proof run - which extrapolates the
  full 100k run to ~$1.60, under the doc's original estimate even at the
  corrected $1.29/hr price. Task cost annotation, telemetry verdict
  ("peak VRAM 1.7/22 GB" - an A10 is generous for ACT), created_by=owner
  on the launch row, and the rescue hook finding zero unpersisted files
  (checkpoints were already safe) all behaved on real hardware.

- **Every failure was drift, and every fix is now code.** huggingface-cli
  was renamed hf (doc snippet now uses the rename-proof python API);
  lerobot's module path died in favor of the lerobot-train entry point;
  --policy.device is gone (auto-selected); current LeRobot DEFAULTS to
  pushing trained models to the Hub and its validation demands a repo_id
  for it (templates now pin push_to_hub=false + a local repo_id - an
  unattended job must never publish weights as a side effect). Flags in
  the templates are now the set proven against the live image's --help,
  and the golden tests pin exactly that.

- **`user: root` exists because the LeRobot image drops to uid 1001**,
  which can neither use the root-based HF cache nor write the NFS bind
  mounts - checkpoints died on permission. The knob accepts ONLY "root":
  it exists to undo an image's USER directive, not to become an identity
  switch; the security boundary stays the mount jail. Docker's default
  (root) is what every other bundled template already assumes.

- **Auth/attribution/adoption shakeout passed in passing**: first real
  boot generated the token into the real .env (breadcrumb = path only),
  bare requests 401, the generated token 200s, a backend restart
  re-adopted the live instance mid-session, and the phase-77 cascade
  fired for real (failed fetch -> training skipped, never half-run).

- **Two findings filed, not fixed on the meter.** (1) The bundled
  vllm-serve template has drifted: the current vllm-openai image's
  entrypoint consumes the template command as ARGUMENTS, so the python -c
  bootstrap lands in vllm's -c/--compilation-config; a pinned v0.6.3
  image fails the same way, so the repair is an --entrypoint knob or a
  command rework - local work, not meter work. (2) Consequently the
  phase-74 subagent-dispatch-over-real-forward smoke REMAINS OPEN; it
  needs the serve repair first. Minor notes: sidecar idle memory fields
  read None on a real A10, and files/list returned an empty shape where
  find showed files - both cosmetic, both logged.

Total session: ~43 minutes of instance time, $0.92, one instance,
terminated with the safety hook's blessing.

## vllm-serve repair — two bugs, one hiding behind the other (2026-08-14)

- **The entrypoint knob exists because images have opinions.** The current
  vllm-openai image's ENTRYPOINT is effectively `vllm serve`, so the
  template's `python3 -c <script>` command arrived as vllm's own
  arguments and `-c` landed in --compilation-config (proven at the real
  gate; a pinned v0.6.3 failed identically, so this was never a
  latest-tag regression). `entrypoint:` on a template forces the binary;
  validated to a single token because flags belong in `command`. Both
  serve templates now set `entrypoint: python3`.

- **Behind it hid an argv shift that made model_id the string
  "manifold".** Commit 8c34a79 (the Gemini-era feature drop the original
  audit existed for) prepended a stray token to both serve templates'
  argument lists while their scripts unpack from sys.argv[1:] - so every
  parameter bound one slot off, and the server would have tried to serve
  a model named "manifold". No test ever caught it because every test
  asserted the RENDERED STRING, and the string contained all the right
  fragments in all the wrong positions.

- **The fix for the test gap is tests that EXECUTE the bootstrap.**
  tests/test_serve_templates.py extracts each template's embedded script
  from the loaded yaml and runs it with the real argv protocol and a
  stubbed os.execvp, asserting the FINAL command the container would
  exec - model binding, lora and speculative branches, sglang's module
  path. Rendered-string goldens catch drift in the docker line; executed
  bootstraps catch drift in what actually runs. Both classes are now
  covered, and the 851-test suite passing UNCHANGED around this repair is
  the measure of the old gap.

- **Not yet re-verified on hardware.** The repair is mock- and
  unit-proven; a real serve (and with it the phase-74 subagent smoke,
  still the one open audit book) needs ~$0.30 of A10 time and a user-
  approved gate. sglang's inside-container --host 0.0.0.0 stays: the
  renderer's 127.0.0.1-only port publish is the jail, per the
  long-standing doctrine on that line.

## Mini-gate: the serve repair verified live, and phase-74 closed (2026-08-14)

~25 minutes of A10, ~$0.35. The repaired vllm-serve template served
Qwen2.5-0.5B-Instruct on real hardware: the bootstrap's own log line
shows `--entrypoint python3` in the production docker invocation, the
model reached ready through the managed forward, `/subagents/dispatch`
returned a real chat completion over the live SSH forward (THE phase-74
smoke, the last open item from the original audit), and the OpenAI proxy
round-tripped real inference under the api token's role gate. Teardown
exercised terminate-under-a-running-server; the hook found nothing
unpersisted and the box died clean.

One new finding, filed: the first serve attempt hit a DISPATCH-TOO-EARLY
race - the job started seconds after SSH connect, before the NVIDIA
container runtime finished coming up, so torch saw 0 devices inside the
container while host nvidia-smi was fine. The gpu-ready preflight checks
the HOST's CUDA state, not container passthrough; it should probe
`docker run --gpus all` visibility (or retry engine-init class failures
once) for jobs dispatched within the first minute of a connection.

Running total for the two real gates: ~$1.27, every audit book closed.

## Phase 84 - Distill v2: teacher, judge, scorecard (2026-08-14)

- **Recipes again, not a platform.** The whole distillation upgrade is two
  new template files, one modified one, and a single POST route. Curation
  and evaluation are jobs, so they inherit everything already built:
  depends_on chaining with skip-on-parent-failure, the mount jail, the
  budget/ceiling/policy stack, the rescue on termination, the cost
  annotation, the audit trail. **Alternative:** a "distillation service"
  module owning the pipeline end to end - rejected for the same reason as
  Phase 83. It would have re-implemented five subsystems to gain a progress
  bar, and every guard would have needed a second implementation.

- **Teacher-agnostic through a base URL, not a backend proxy.** llm-synthesize
  and llm-judge take `teacher_base_url` / `judge_base_url` and dial it
  themselves from inside the container. **Alternative:** route teacher calls
  through the backend's brain registry, so the dashboard's model picker
  would list them - rejected. The backend runs on the user's laptop; the job
  runs in a datacenter. Proxying would have put a laptop on the critical
  path of a thousand GPU-side requests, and would have made the backend a
  credential holder for a key that belongs on the instance. The rule the
  templates document instead is locality: the teacher must be reachable FROM
  THE INSTANCE, so a served model and a public API qualify and a laptop
  Ollama never can, whatever the UI implies.

- **API keys ride the user's own .env, because parameters are public.** The
  dispatcher writes the fully rendered docker command into the job log
  (dispatcher.py:1044), which is persisted and shown on the job card. Any key
  passed as a parameter is therefore echoed verbatim and stored in SQLite, so
  all three templates copy script-run's `env_file` convention instead: a
  KEY=value file on the persistent filesystem, sourced by the shell before
  python starts. `teacher_base_url` and `judge_base_url` additionally refuse a
  query string or a `user:pass@host`, which is the other way a key reaches
  that log line. Manifold never sees the key at all.

- **Curation is its own job, not a flag on synthesize.** llm-judge reads a
  synthesized file and writes `scored-<name>.jsonl` (evidence) and
  `kept-<name>.jsonl` (the training set), both back into `synthesized/`
  because that is the only dataset directory axolotl-finetune mounts.
  **Alternative:** a `min_score` parameter on llm-synthesize that filtered
  inline - rejected for three reasons. Generation and judging want different
  models (a judge that is also the teacher grades its own homework, which the
  template warns about by name), the score histogram is the artifact that
  teaches a beginner what their data is actually worth, and re-judging with a
  different threshold must not cost a second generation pass. Separating them
  also means a failed judge run leaves the generated data intact.

- **The holdout is deterministic and is capped at 50%.** Every Nth GENERATED
  row (not every Nth input row, so input failures cannot make the split
  lumpy) is written to `eval-<name>.jsonl` keeping the teacher's answer, so
  llm-eval grades against it later without paying to generate it twice.
  `holdout_pct` is bounded 0-50 and the job FAILS if either half comes out
  empty: a 100% holdout starves the trainer and a holdout that rounds to zero
  rows makes the scorecard print a meaningless 0%, and both are silent.

- **The config generator returns for REVIEW and can never train.** POST
  /distill/config asks a brain for an axolotl YAML, validates it, and hands
  it back as text. It writes no file and starts no job; saving is the
  existing upload route and training is the existing axolotl-finetune job,
  both human actions. **Alternative:** save-and-queue in one click - rejected
  outright. axolotl EXECUTES that config on the GPU box, so validation is a
  security boundary and not a lint: an allowlist of top-level and per-dataset
  keys, `trust_remote_code` refused by name, `base_model` restricted to the
  vetted student shelf, `datasets[0].path` required to EQUAL the file the
  user named (a glob would sweep in the held-out `eval-*.jsonl` and train the
  student on its own exam), `output_dir` confined to the writable mount, and
  anchors/aliases refused because a size cap alone does not stop an expansion
  bomb. A model wrote the file; a human reads it before it runs.

- **One seam, plus one static catalog read.** `GET /student-presets` is a
  second route, taken deliberately: the shelf of small open bases is how you
  CHOOSE a student, and a catalog that only arrives with the first generated
  answer is a catalog nobody can use. It mirrors GET /model-presets exactly
  (three lines, lazy import, viewer role). The seam that does work is still
  the single POST.

- **llm-eval loads the student in-process, and the VRAM arithmetic is in the
  template header.** transformers from the merged weights on the filesystem,
  not a second vllm-serve. **Alternative:** serve the student and compare two
  endpoints - rejected: one server per instance is a standing rule, a
  scorecard run is a batch job rather than a service, and a served student
  would need the card that the teacher is already holding. vLLM takes 90% of
  the GPU by default, so on a 24GB A10 a live teacher holds ~21.6GB and a 3B
  student beside it is a guaranteed OOM. The default path avoids the
  collision entirely: teacher answers are already stored in the holdout file,
  so the recommended run happens after the teacher is stopped, with
  `student_device=cpu` as the documented slow escape hatch.

- **The scorecard says what it is.** A judge picks blind between the two
  answers with the student at position A on even item indexes and B on odd,
  which cancels position bias and NOTHING else. Ties are a third outcome
  rather than folded into wins; `judge_model`, `teacher_model` and
  `judge_is_teacher` land in scorecard.json; and an equal judge and teacher
  print a warning above the headline number. It is a preference score from
  one model, not a benchmark, and the template and the doc both say so.

- **The chain is the batch tail; the teacher server is bound with Run on.**
  depends_on refuses a server parent ("a server that never exits on its own:
  'after it succeeds' would mean never"), which is correct and stays.
  synthesize -> judge -> finetune -> merge chains up front; the teacher is
  started separately and the batch jobs are pointed at that instance with
  target_instance_id. llm-eval only joins the chain when there is no local
  server to collide with, i.e. when teacher and judge are both APIs.

- **llm-synthesize's compatibility guarantee is behaviour, not bytes.** The
  advertised "byte-for-byte backward compatible" is not achievable and was
  not claimed: every declared parameter always renders, so four new ones
  append four quoted args to the docker command. What IS guaranteed is that
  defaults reproduce the old behaviour exactly. The mechanism: new
  parameters are only ever APPENDED, and the script reads them as
  `sys.argv[n] if len(sys.argv) > n else default`, so no existing slot moves.
  env_file forced the one structural change: the old
  `bash -c 'exec python -c "$PYCODE" "$@"' manifold` had no shell step in
  which to source anything, so a prologue now runs inside the same
  single-quoted wrapper (env_file is the LAST positional, which is why the
  prologue reads the last argument rather than $1). The env var stayed named
  PYCODE so test_pipeline.py keeps addressing it.

- **The `manifold` token is bash's $0, and belongs to `bash -c` alone.** This
  is the mechanism behind the bug fixed the day before this phase, recorded
  here because three new templates copy the pattern. `bash -c SCRIPT [name
  [args...]]`: the first operand after the script is assigned to $0 (the
  shell's name for its own error messages), and only the ones after it become
  $1, $2. So in llm-synthesize's wrapper the literal `manifold` is padding,
  and dropping it would silently eat the first real parameter. Commit
  8c34a79 cargo-culted the same token onto vllm-serve and sglang-serve, which
  have NO bash wrapper at all (`entrypoint: python3`, command starting at
  `-c`): python's -c consumes no argv0 operand, so `manifold` became a real
  argument, every parameter bound one slot off, and model_id was the string
  "manifold" for a month. Rule, now testable: the token appears after
  `bash -c '<script>'` and nowhere else.

**Test coverage:** the bundled registry loads clean with all three templates
(targeted runs: tests/test_templates.py, test_serve_templates.py,
test_foundry_templates.py, test_pipeline.py, test_template_quoting.py = 51
passed; test_mcp.py, test_rbac.py, test_auth.py = 51 passed, so the MCP
import allowlist and the closed ROUTE_ROLES table both still hold; the
dashboard builds clean). All three templates declare no `ports:`, so none is
misclassified as a server and all are legal chain links. distill.py's
validator was exercised against a stub brain over TestClient (fenced and
unfenced YAML, prose, unknown envelope, and sixteen rejection paths incl.
trust_remote_code, off-shelf base, glob path, held-out path, traversal, and
an alias bomb). The template scripts were executed for real against a
loopback stub OpenAI server after being rendered through a real shell, which
is the two-class rule the vllm-serve repair established: golden renders catch
drift in the docker line, executed scripts catch drift in what actually runs.
**Open, stated plainly:** those harnesses were throwaway, so permanent tests
for llm-judge and llm-eval (including CASES entries in
test_template_quoting.py and a floating-tag warning assertion for llm-eval's
axolotl image) are still owed; llm-eval's transformers calls have only run
against stub torch/transformers and need a real-hardware gate;
estimates.DEFAULT_MINUTES has no entry for the two new templates, so their
estimate degrades to the 15-minute fallback; and the STUDENT_PRESETS repo ids
are curated but not verified against the HuggingFace API the way
MODEL_PRESETS were. No real-GPU run has priced this loop, so every cost
figure in docs/distill-your-own-model.md is marked unverified arithmetic
except the $1.29/hr A10 rate.

## 2026-08-14 — Phase 84 shipped green and broken; what the verifier found

**What happened.** Phase 84 was merged to `main` on the strength of 919
passing tests and a clean dashboard build. Both adversarial verifiers then
returned FAIL. Neither had been read before the merge — that was the process
failure, and it is the one worth remembering: *green tests are evidence about
the tests, not about the product.* One test actively pinned a bug as correct.

**The four that mattered**, each found by EXECUTING the thing rather than
reading it:

1. **llm-synthesize could exit 0 with a zero-byte training file.** With an
   unreachable teacher every record died in the per-record `except`, the loop
   carried on, and the job went green. The only gate lived inside
   `if hold_every:` — so the DEFAULT path (holdout_pct=0) had no gate at all.
   Nothing failed until axolotl choked on the empty file an hour and a GPU
   later. The gate now sits outside the holdout branch.

2. **llm-judge scraped scores out of its own reason field.** A reply of
   `{"score": 0, "reason": "answered in 8 words"}` fell past the range check
   into a `re.search` over the raw text, scored **8**, and was **kept** — the
   one guard the template exists for, inverted by an incidental digit. Valid
   JSON is now answered from its `score` field alone. The prose fallback
   survives only for a reply that IS a number: digit-scraping read "worse
   than 10 others, I give it 2" as a 10. Prose now goes unscored, which drops
   the row rather than fabricating a verdict for it — the safe direction for a
   curation guard, and a deliberate tightening.

3. **llm-eval published `match_rate_pct: 0.0` when it had graded nothing.** An
   unreachable judge produced a scorecard indistinguishable from a student
   that genuinely lost every round, and exited 0. The rate is now `null` with
   a nonzero exit when `n_graded` is 0; the card is still written because its
   per-item replies are how you find out why. The test asserting the old
   behavior as "the honest output" was rewritten — it is the clearest example
   in this repo of a test that made a bug permanent.

4. **The dashboard Distill panel was dead on first click.** `DistillConfig`
   was declared with three invented field names (`config_yaml`/`student`/
   `notes`) against a backend returning `{"config": {yaml, base_model, ...}}`,
   so every field read `undefined`: the YAML pane rendered empty and Copy
   copied "undefined". It typechecked perfectly, because the type was wrong at
   both ends. The panel also posted `student` where the backend reads
   `student_model` (silently dropped) and omitted the required `dataset`
   (a 422 whose array-shaped detail rendered as a stringified array). Fixed by
   taking the field names from the backend verbatim, and `detailToMessage` now
   flattens FastAPI validation arrays for *every* route, not just this one.

**Also fixed:** a JSON `null` answer became the truthy string `"None"` and was
graded as if the teacher had written it; a hand-supplied `judge_model` skipped
the only reachability check, so a dead judge was discovered *after* torch
imported and a multi-gigabyte student loaded onto a billed GPU (now a
one-token confirm); `DEFAULT_MINUTES` had no entry for llm-judge, llm-eval or
lora-merge, so all three costed out at the 15-minute fallback; a fractional
judge score truncated (8.7 → 8) instead of rounding; the holdout summary read
"every 2th generated row"; and the reviewed `output_dir` is not binding —
axolotl-finetune's `--output_dir` FLAG overrides the YAML key, so the review
panel now prints the job parameter you must set to match, as an advisory.

**Still open and deliberately not fixed here:** llm-eval's transformers calls
have still only run against stub torch/transformers, and STUDENT_PRESETS' repo
ids are curated but unverified. Both need the real-hardware gate.

## 2026-08-14 — Phase 85: the model comes home

**Quantize on the instance, not on the laptop.** The merged student is a
directory of f16 safetensors; its Q4_K_M GGUF is roughly a third the size.
Converting on the box means the download is the small file, over a link
already being paid for, and the user's machine needs no torch, no CUDA and no
llama.cpp build. It is CPU work on a GPU box, which is only wasteful if you
boot for it — chained onto the instance that just merged, it is a couple of
minutes on a machine already running. *Alternative rejected:* pull the
safetensors and convert locally, which moves 3x the bytes and imposes a
Python/ML toolchain on a user whose whole reason for using Manifold is not
having one.

**`entrypoint: bash` and `user: root` on the template.** llama.cpp's `full`
image ships `/app/tools.sh` as its ENTRYPOINT, which dispatches on flags like
`--convert` and would consume the command as its own arguments — the exact
shape of the vllm-serve bug one image over. `user: root` is defence against
an image update adding a USER directive, since the .gguf is written to the
NFS mount that a uid-1001 process could not touch (the LeRobot lesson).
Neither knob is new; both existed because earlier gates burned a run.

**A backend pull route, not the browser download.** The existing download
route hands bytes to the browser, which puts them wherever the browser puts
things. The installer needs to *find the file again*, so the pull writes into
DATA_ROOT/models and the install reads from there. A file in ~/Downloads is
one Manifold can only talk about. Written to `.partial` and renamed on
completion, so a dropped transfer never leaves a plausible model behind.

**Ollama over a bundled runtime.** Shipping an inference server would mean
owning llama.cpp builds for three platforms. Ollama and LM Studio already do
that, and the payoff is that installing costs *zero new brain code*:
`127.0.0.1:11434` is already a probed local endpoint, so an installed model
turns up in the picker as `local:ollama/<name>` on its own. Absent Ollama the
feature degrades to "here is your file and the command", never a dead button.

**The Modelfile is one line.** `FROM "<path>"` and nothing else. A GGUF
converted from a HuggingFace model carries its own chat template in its
metadata; a TEMPLATE line guessed here would silently override a correct one,
and the symptom — a distilled model that babbles — reads as "distillation
failed" rather than "Manifold added a wrong prompt format".

**Testing:** the executing-script doctrine extends from `python -c` to
`bash -c`. The template's real script is run by a real bash with PATH shims
standing in for `convert_hf_to_gguf.py` and `llama-quantize` that record the
argv they were handed, so a shifted parameter fails loudly rather than
rendering plausibly. The routes are tested against a fake `ollama` on PATH,
so the suite passes on a machine that has never installed it and never
creates a model on one that has.

**Unverified until the real gate:** the pinned image tag actually converting a
Qwen student, and whether the chat template survives into GGUF metadata. Both
are hardware questions; neither can be answered in mocks.

## 2026-08-14 — The real A10 gate for Phases 84 + 85: $1.83, five findings

One A10 (gpu_1x_a10, $1.29/hr, us-east-1), the whole chain end to end:
30 seed records -> synthesize with a live Qwen2.5-3B teacher (24 rows + 6
held out) -> judge (22 kept, 2 dropped, 0 unscored) -> a brain wrote the
axolotl config -> train a Qwen3-0.6B LoRA (28 steps, 22s, loss 2.3 -> 0.2)
-> merge -> scorecard (student matched or beat the teacher on 3 of 4) ->
quantize to Q4_K_M (1137 MiB -> 379 MB) -> pull home -> install into Ollama
-> **the distilled student answered "Low Angle" as a brain in the picker.**

Everything below was invisible to 968 passing tests.

**1. An empty Ollama took down /brains entirely (500).** A freshly installed
Ollama with no models answers `{"object":"list","data":null}`, and
`.get("data", [])` does not default when the key EXISTS holding null - so
the probe iterated None and raised. That route is the brain picker, the
chat, Autopilot and the distill panel at once. Phase 85's own docs tell
users to install Ollama, so this feature created the conditions for its own
outage. Fixed by validating the BODY, not just the status code.

**2. `entrypoint: bash` + a command starting `bash -c` double up.** docker
execs ENTRYPOINT + COMMAND, so it ran `bash bash -c '<script>'`, where the
second `bash` is read as a script FILE: "cannot execute binary file", exit
126. The payload must start at `-c`, exactly as the serve templates do
under `entrypoint: python3`.

*The doctrine failure matters more than the bug.* The executing test ran
the extracted script under its own `bash -c` - proving the script was right
while saying nothing about how it gets invoked. **Executing the script is
not enough; the test must execute the ENTRYPOINT + COMMAND concatenation
the container actually runs.** The harness now renders the real docker
command, splits it as a shell would, and runs entrypoint + trailing args.
It also passes `template.env` the way docker's `-e` does, without which the
embedded fixer silently no-opped as an unset variable.

**3. The merged model's tokenizer is not portable between images.** The
trainer writes `extra_special_tokens` as a LIST of token strings; the
transformers inside the llama.cpp image wants a MAPPING and dies on
`.keys()` before conversion starts. Three of six shelf presets are Qwen3,
so this is the common path. Fixed without touching the user's model: the
script builds a directory of SYMLINKS to it and replaces only the one
offending link with a corrected real file.

**4. The teacher must be stopped before training.** Predicted exactly by
the Phase 84 gate agent, and confirmed live: vLLM holds ~21.6 GB of a 24 GB
A10, and `axolotl-finetune` died with `CUBLAS_STATUS_ALLOC_FAILED` before
its first step. Stopping it freed the card (0 MiB of 23028) and the same
config trained in 22 seconds. Now a warning in the doc at the step where it
bites, not only in llm-eval's header.

**5. A reasoning student returns an empty answer on a small token budget.**
Qwen3 emits a `<think>` block first; our student thought for ~90 words
before answering, so a short budget stopped it mid-thought and returned
`content: ""` with `done_reason: length`. That reads as "the distillation
failed" when the model is correct - it answered "Low Angle" given room.
Documented, with the advice to prefer a non-reasoning base for short-label
tasks. Also fixed: `POST /models/install` returned
`local:ollama/<name>` while the picker lists `local:ollama/<name>:latest`.

**Verified and NOT a problem:** the chat template survives conversion
intact - Ollama parsed the student's `<think>` blocks into its `thinking`
field, which only works if the template metadata came through.

**Known and unfixed:** the pull runs at roughly 0.6-0.7 MB/s over SFTP
(379 MB took about nine minutes). Fine for a 0.6B student, painful for a
3B one; the chunked `sftp_read` is the place to look. Also, a client
disconnect does not cancel a pull - the backend finished writing and
renamed the file correctly, which is the behaviour we want, but it is
undocumented.

## 2026-08-14 — Pre-launch audit: the pull could terminate the box it was pulling from

A six-agent audit ahead of the public launch post. The CI break has its own
entry above; these are the rest, in the order they matter.

**A model pull over ~1.2 GB was guaranteed to fail, delete itself, AND
terminate the paid instance.** The Phase 85 pull streams the whole file in
one request and only called `dispatcher.touch_activity` AFTER the last byte
landed. The idle timer defaults to 30 minutes and the transfer runs at
0.6-0.7 MB/s, so anything past roughly 1.2 GB tripped it mid-flight: the
instance was torn down under the download, the `.partial` was deleted by the
error path, and the user had paid for a boot and got nothing. A 7B student
(~4.4 GB Q4_K_M, ~113 minutes) could never be pulled at all. Three walls sat
at exactly the same 30-minute mark - the idle timeout, the dashboard's client
budget, and nothing else - so the failure read as a network problem rather
than a self-inflicted teardown. The gate on 2026-08-14 only passed because a
0.6B student is 379 MB, comfortably under the cliff.

Fixed by touching activity INSIDE the loop, throttled by bytes (32 MiB)
rather than chunks so the cost stays flat if the chunk size is tuned later,
and by raising the dashboard budget to four hours - a client timeout that
cannot fit the largest model on the shelf reports a false failure while the
backend is still working. The underlying 0.6-0.7 MB/s (sequential 64 KiB
request-response round trips in `sftp_read`) is still open and is the real
fix; this makes the slow path survivable rather than fast.

**An invented price survived on the one screen where it matters most.** The
cluster launch panel's Est. Burn Rate correctly says "rate unavailable"
before the catalog loads, but the GPU-type dropdown right above it still
carried hardcoded "$24.72/hr" literals in its fallback options. Half the fix
had been applied and the other half missed, on the screen where a user
decides to spend. Names only now.

**Two things a stranger would have hit immediately**, both from following
our own words: `docs/mcp-setup.md` shipped the author's absolute home path
inside a JSON block captioned "with YOUR absolute repo path", so a
copy-paste gives you a directory that does not exist; and the launch post's
quickstart omitted `cd backend`, `uv sync` and `npm install`, so pasting it
into a fresh clone produced `ModuleNotFoundError: No module named 'app'`.
The README was correct all along - the post had drifted from it.

**A number in the post had no receipt.** It claimed a "$1.01 verification
day ... per its own spend page", and $1.01 appears nowhere in this repo:
the documented figures are $0.92, $0.35 and $1.83. In a post whose thesis is
rigorous self-auditing, and which explicitly invites readers into
DECISIONS.md, an uncorroborated dollar figure is the one error that cannot
be afforded. Replaced with the itemised $3.10 the file actually backs.

**Added `.github/workflows/test.yml`.** Until today the only workflow ran on
tags, which is precisely why a broken lockfile survived three weeks. The
suite and the dashboard build now run on every push, and `npm ci` (never
`npm install`) is the step that proves the committed lockfile is installable
by someone who is not the author.

**Still open:** `releases/latest` is a 404 while `docs/desktop-build.md`
tells you to share that URL, and the only downloadable artifact is a stale
July prerelease - cutting a fresh tagged release is now unblocked because CI
is green, but publishing one is the owner's call. The dispatch-too-early GPU
race is still open (the readiness probe checks `nvidia-container-cli`, not
`docker --gpus`). The single-shot file-download route touches activity once
before streaming and so shares the pull's old shape for very large files.

## 2026-08-14 — Pre-launch audit, second pass: the claims and the last mile

**A false claim in the honesty paragraph.** The launch draft said "GCP
support is experimental". `backend/app/providers/gcp_provider.py:109`
describes a provider "whose live API is not wired up yet": the catalog
degrades to empty, liveness raises ProviderUnavailable, and every write
raises ProviderError. "Experimental" implies something a reader could try.
It launches nothing, and saying so belongs in the one paragraph where a
reader extends trust in exchange for candour.

**The README's 90-second quickstart began with a command most readers do
not have.** `uv sync` was step one, and the file contained no install link,
no prerequisite section and no Node version anywhere. Two auditors
independently reproduced `sh: uv: command not found`, exit 127, from a
fresh clone - the most likely first-run stumble for an arrival from a link,
landing before they see anything work.

**`/releases/latest` is a 404 and `docs/desktop-build.md` told you to share
exactly that URL**, asserting it "always resolves to the newest tag". It
resolves to the newest NON-prerelease release, and this repo's only release
is a prerelease, so the documented link is dead. Corrected with the caveat
rather than the assertion.

**Stale under-claims, all in tracked public files.** README said "430+
tests", CONTRIBUTING said "480+"; the suite is 973. These are UNDER-claims,
so there was no honesty exposure - but a project whose pitch is rigour
should not be casually wrong about its own strongest credential.

**`sftp_read` was latency-bound, not bandwidth-bound.** It requested 64 KiB
per round trip, and asyncssh only pipelines a read LARGER than the
negotiated block size (261120 against OpenSSH). So every chunk was one full
round trip: 0.6-0.7 MB/s measured against a real instance, which is what
made a 379 MB pull take nine minutes. Raised to 4 MiB, where asyncssh
parallelises; an auditor measured ~9.7 MB/s on the same path and proved the
ranged-download contract byte-identical (same request count, matching
sha256), which matters because the MCP `download_file` tool depends on it.
Memory stays bounded at one chunk per transfer: every consumer writes each
chunk out before asking for the next.

**estimates.DEFAULT_MINUTES was missing 7 of 19 templates**, including
lerobot-act and nanogpt-pretrain - the two the docs headline - all silently
costing out at the 15-minute fallback. This is the identical defect the
Phase 84 verifier fixed for llm-judge/llm-eval, reintroduced for Phase 83's
and 85's templates. Cluster templates get None (they run until stopped)
rather than a number nobody can stand behind.

**KNOWN FLAKE, pre-existing and now visible.**
`test_blocked_termination_notifies_once_until_the_files_change` (Phase 76b,
untouched since a5aa626) fails roughly one full-suite run in three, and
passes every time in isolation and in small groups. The test asserts exact
OS-ping counts while the app's background loops are live under
`TestClient`'s lifespan, so it is racing something. Adding CI today made
this visible rather than causing it - but on a public repo it now means an
occasional red build, so it needs a real fix (making the test deterministic
rather than loosening the assertion, since the exact count IS the property).

## 2026-08-14 — Removing what the AI agents made up

**Three vendor-integration modules were deleted: `agy_integration.py`
(Google Antigravity handshake), `openclaw_adapter.py` ("OpenClaw & Hermes
Agent Protocol"), `claude_integration.py`.** 222 lines. Nothing in
`backend/app/` imported any of them - the single apparent reference in
`auth.py` was a COMMENT naming an emitter that never ran. The only tests
were three that imported a module and asserted it returned its own
hardcoded values; they exercised nothing about Manifold and were deleted
with it. `claude_integration.py` wrote `~/.claude/mcp.json`, a path Claude
Code does not read.

These are the kind of thing an agent produces when asked to "integrate"
with something it has only heard of. Keeping them was the worst possible
choice for this repo specifically: the project's public claim is that AI
agents wrote much of it and the roadmap came from auditing their work, so
inert invented integrations sitting in `backend/app/` are the first thing
a sceptic finds and the strongest available argument that the audit was
rhetoric. The five real tests in `test_agent_handshake.py` - the
`/agent/handshake` routes and the context manager's TTL and eviction -
stay.

**Eleven startup WARNINGs became one INFO.** Every bundled template with a
floating image tag logged its own warning line at load, so the first thing
anyone saw from the product was eleven consecutive WARNINGs before the
first INFO - during the 90-second demo the README promises, that reads as
a broken install. The condition is real and still reported (one line
naming all eleven templates); the per-template detail moved to DEBUG.

**Mock mode no longer demands a credential it invented.** Token auth was
installed whenever a token existed, and real mode MINTS one and persists it
to `.env` - so a single accidental real-mode start permanently gated the
zero-credential demo for that clone. Now keyed on `not mock`. This is safe
by construction rather than by assertion: mock mode has no cloud client, no
spend and no secrets to protect, and `NetworkGuardMiddleware` is installed
unconditionally, so an untokened backend still refuses every non-loopback
caller. Both halves are pinned by tests - mock with a token set stays open,
real mode with a token still 401s.

## 2026-08-14 — The safety-hook flake: reproduced, narrowed, NOT fixed

`test_blocked_termination_notifies_once_until_the_files_change` fails about
one full-suite run in three and never in isolation. Written up because the
dead ends are the expensive part, and because CI (added today) will surface
it as an occasional red build on a public repo.

**The exact failure**, captured by looping the suite until it reproduced:

    assert len(blocked_pings()) == 2
    AssertionError: assert 1 == 2

So the SECOND OS ping is MISSING - the one that should fire after the user
"saves" one of two unsaved files and the unsaved SET therefore changes.
It is a missing notification, not a duplicate, which rules out the obvious
reading (a retry firing an extra ping).

**Ruled out, each by experiment rather than by argument:**

- *Background retry loops.* The obvious suspect: `TestClient` runs the app
  lifespan, so the idle and auto-manage loops really do retry blocked
  terminations underneath the assertions. Parking `_idle_loop` and
  `_auto_manage_loop` did not help; parking ALL SIX loops (`_task_loop`,
  `_idle_loop`, `_watch_loop`, `_telemetry_loop`, `_auto_manage_loop`,
  `_adopt_loop`) still failed 2 runs in 4 - the same rate. The scaffolding
  was reverted rather than left in place looking like a fix.
- *Stale unsaved state.* An instrumented replay of the exact sequence
  (block, three retries, trim the file list, block again) produces both
  pings every time. `terminate` calls `rescue` fresh, `rescue` reads the
  sidecar live, and the mock returns `list(self.unpersisted)` - no cache
  anywhere on that path.
- *A notifier cooldown.* There is none. `NotificationCenter.notify` records
  and pings unconditionally; `notify_once` is a different method this path
  does not use.

**Still live, unconfirmed:** `notify()` wraps its whole body in
`except Exception -> log and return None`, so a transient failure inside
`create_notification` would drop a ping silently and look exactly like
this. Three full-suite runs with ERROR logging captured did not reproduce
it, so this is a hypothesis, not a finding. If it IS the cause, the bug is
larger than the test: a notification the user needs would be lost in
production the same way, and the blanket except is worth narrowing.

**Not fixed - QUARANTINED.** CI (added the same day) went red on it twice
in three pushes, which on a public repo is a worse signal than no CI at
all, and it would have gated every future push behind a coin flip. So the
test carries `@pytest.mark.xfail(strict=False)` with the whole diagnosis in
its reason string. It still RUNS and still reports (XPASS when it passes,
XFAIL when it does not), so the evidence keeps accumulating; it just stops
blocking. `strict=False` is deliberate: a real fix should surface as an
XPASS to be noticed and removed, not as a new failure.

Loosening the ASSERTION was rejected outright - the exact count is the
property under test (one ping per distinct unsaved set), and a test allowed
to drift proves nothing. Quarantining a test while saying so is honest;
weakening one until it passes is not.

## 2026-08-14 — Phase 86: the hardware ladder (know your hardware)

**What:** `GET /gpu-guide` and a disclosure inside the launch form, right
under the GPU picker: every GPU type the provider offers, from a single
A10 to the 8x B200 node, each with what it is FOR, when to step up, what
fits on it, and the live price. Picking a rung fills the form field, so
learning and acting are one motion.

**The two rules it is built on**, both scar tissue from this same day:

*Data comes from the provider, words come from here.* Every number in the
guide is either passed through from the same catalog call /instance-types
serves (price, VRAM parsed from the provider's own description string,
capacity) or labelled arithmetic on those numbers. There is no price
literal in gpu_guide.py for a price change to strand - and a test enforces
that at the source level by refusing any dollar literal in the module. The
test caught its own module's docstring quoting the old cluster-screen bug
by amount; the docstring now tells the story without the number, which is
the rule working.

*Arithmetic is labelled as arithmetic.* "Serves ~30B at 4-bit" is VRAM
math, not a measurement, so every response carries the exact formula in
`fits_basis` and the UI prints it. The honesty rule for money (exact or
labelled) extends to capability claims.

**Alternatives rejected:** a static docs page (goes stale, and it is not
where the decision happens); putting the prose in the dashboard (then the
MCP/API surface never sees it, and prose in TSX is untestable); deriving
good-for text from model-fit calls per model (right tool for "does THIS
model fit", wrong shape for "what is this card for").

**Curated families:** A10, RTX 6000, A6000, V100, A100 40/80, GH200 (with
the ARM-host warning - the templates' images are x86), H100, B200. A card
the file has never met still gets true numbers: price passthrough, VRAM
parsed, fits arithmetic, and an honest "no curated notes yet" - never
another family's prose, never a dropped row. Multi-GPU rungs all carry the
one-box truth: tensor parallel for serving, and Manifold clusters
coordinate separate machines, not distributed training.

Also in this change: hn-post.md's stale counts corrected against measured
reality (over 970 tests, 37 MCP tools, ~50k tracked lines - the old "13k"
undercounted by 4x), and a README paragraph pointing at the guide.

## 2026-08-14 — The H100 gate: $2.00, three receipts and one honest miss

One gpu_1x_h100_sxm5 ($4.29/hr, us-south-2, Somnora-South colocated),
launched with a 2h lifetime ceiling. Manifold's own budget guard refused
the launch first - $4.29/hr against the $4.00 guardrail - which is the
guard binding its author's agent exactly as designed; the ceiling was
raised to $5.00 for the authorized gate and restored to $4.00 at teardown.

**1. The dispatch-too-early race fix held at T+0.** gpu-smoke was
dispatched the same second the SSH connection came up - the exact window
that killed the mini-gate job - and succeeded: the new probe's final stage
(`docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L`) held dispatch
until a real container could see the GPU. One clean pass cannot prove the
race extinct (it is a timing window), but the probe now exercises the
exact path that fails, which the two host-side checks never did.

**2. The pull fixes verified live, with an honest number.** A 2,048 MB
file pulled home completely (renamed only at the last byte) while
idle_seconds sampled 2-10s throughout - before the touch fix that counter
would have climbed to 1,800 and terminated the box mid-transfer.
Throughput: ~3 MB/s sustained cross-country. That is 4.5x the old 0.65
MB/s, and NOT the 9.7 MB/s the loopback benchmark promised - RTT to
us-south-2 is real, and the transfer still awaits each 4 MiB chunk in
sequence (asyncssh parallelises within a read, not across reads).
Cross-chunk pipelining is the next rung of this fix and is filed, not
claimed.

**3. The H100 ladder rung has a receipt.** Qwen2.5-7B fp16, single
stream, 256-token generations through the whole real path (vLLM behind
the managed SSH forward): 178/165/168 tok/s, median 168. Recorded in the
hardware guide's H100 note with its date, as a measurement - the fits
numbers beside it remain labelled arithmetic.

Termination clean (rescue hook ran, nothing unsaved), zero orphans, test
files removed from both ends. Gate total per the spend page: $2.00.
Running total for all real-hardware gates: $5.10.

## 2026-08-14 — The Launch Swarm dialog was trapped inside its own card

Reported from a screen recording: opening "Launch Swarm" showed a
horizontal sliver of a dialog inside the Elastic GPU Clusters card, and
scrolling made it disappear. Unusable.

**Cause, one line of CSS.** The panel is
`relative overflow-hidden ... backdrop-blur-md`. An element with a
`backdrop-filter` (like `transform`, `filter`, `perspective` or a
`will-change` naming them) becomes the CONTAINING BLOCK for its
`position: fixed` descendants. So the dialog - correctly written as
`fixed inset-0` - was positioned against the panel instead of the
viewport, and then `overflow-hidden` clipped it to the panel's box.
Measured in a real browser: `backdropFilter: blur(12px)`,
`overflow: hidden`, and the dialog sliding 140px when the page scrolled.

**Fix: a portal, not a style tweak.** Deleting the blur would have fixed
this one card and left the trap armed for the next component to walk into
- and this bug is invisible to every check the repo had, because `tsc` and
`next build` both pass while a dialog is unreachable. `ModalPortal` renders
through `createPortal` to `<body>`, so the dialog is no longer a descendant
of anything in the page and no ancestor styling can position or clip it,
whatever gets added to those cards later. It also owns the things every
modal needs and none should re-implement: Escape to close, backdrop click,
and a scroll lock on the page behind (scrolling under the old dialog is
what made it "disappear"). The dialog itself gained
`max-h-[calc(100vh-2rem)] overflow-y-auto` so a tall form scrolls ITSELF on
a short window rather than putting its buttons off-screen.

Hydration is detected with `useSyncExternalStore`, not
`useEffect(() => setMounted(true))`: the dashboard is a static export, so
the portal target does not exist at prerender time, and the effect version
schedules a cascading render that the lint rule correctly rejects.

**The check is real.** `dashboard/e2e/modal-portal.mjs` drives a headless
Chromium: opens the dialog, asserts it is not a descendant of the panel,
that its box fits the viewport, that it does not move when the page
scrolls, that the page behind is locked, and that Escape closes it. It was
run against the PRE-FIX component and FAILED 5 of them - a check that
cannot fail proves nothing. Now a CI job (`modal`).

Two things learned while writing it, both worth keeping: run it against the
static export, because `next dev` refuses cross-origin asset requests from
127.0.0.1 (bundles 403, React never hydrates, clicks silently do nothing,
and it looks exactly like a broken fix); and wait for framer-motion's
entrance to settle before measuring, or the animation's own tail reads as
scroll movement - the first "failure" on the fixed build was my
stopwatch, not the product.

**Only ClusterPanel was affected.** Onboarding and TokenGate also use
`fixed inset-0`, but their blur is on the fixed element itself (which does
not trap it) and both mount near the root, so they were left alone.

## 2026-08-14 — The Google toggle was showing Lambda's catalog under Google's name

Caught by the owner: flipping the provider toggle between Lambda and
Google Cloud changed nothing - same GPUs, same prices, same availability.
The dashboard has sent `?provider=` since the toggle appeared, and
`/instance-types` accepted no parameters at all, so every answer was
`lambda_client.list_instance_types()` regardless. In a product whose rule
is that a number on a spend screen is provider data or absent, relabelling
one provider's numbers as another's was the worst available bug - worse
than the stub itself, whose own design notes say "an empty catalog says
exactly that". The intent was right and the route never asked.

Fixed by routing both `/instance-types` and `/gpu-guide` through the
provider registry: Lambda keeps its original field-complete path
(unchanged days after launch on purpose); any other provider's catalog is
serialized from its own `CloudInstanceTypeSpec`s; unknown providers 422
naming the registered ones. Real-mode GCP therefore shows an EMPTY catalog
- the truth - and the launch form says why in an amber banner ("Manifold
cannot list, launch, or bill GCP machines... Lambda is the working
provider"). Mock mode shows the mock GCP catalog (a2/g2 machines), which
is now visibly DIFFERENT from Lambda's - the two lists disagreeing is how
a demo proves the seam is real. The hardware ladder follows the toggle and
refetches on change, because a cached ladder from the other provider is
the same mislabelling one component over.

Pinned by tests: real-mode GCP catalog == {}, the guide follows the
toggle, and an unknown provider is refused by name.

Known cosmetic gap, deliberately not half-fixed: in mock mode the REGION
dropdown still lists Lambda's regions under the GCP toggle (regions are
not provider-scoped yet). That belongs to the real GCP phase, where zones
replace regions properly.

## 2026-08-15 — Phase 87: Google, for real

**What ships:** `RealGCPProvider` implemented against google-cloud-compute
over ADC; a curated shelf (gcp_catalog.py) of a dozen machine shapes from
a $0.54/hr T4 to the 8x H100 a3 node, intersected at request time with ONE
live acceleratorTypes.aggregatedList call for zone availability; launches
on Ubuntu 22.04 with the SAME cloud-init rider Lambda boots (a new driver
block guarded by `command -v nvidia-smi` no-ops where drivers exist);
GET /gcp/quota and a launch-form strip showing GPU quota BEFORE the click;
provider-scoped /regions; ProviderCapacityError so GCE resource-pool
exhaustion retries exactly like Lambda capacity while quota errors fail
with the console link and metric name in the message.

**The decisions:**

- *ADC, never a pasted key.* `gcloud auth application-default login` is a
  browser OAuth into the user's own Google account; the SDK resolves it
  natively and Manifold never sees a credential - the CLI-brains rule
  applied to a cloud. Service-account files remain the headless fallback
  via GOOGLE_APPLICATION_CREDENTIALS. Auth expiry is a NORMAL state and
  maps to the one command that fixes it (hit live while building: the
  owner's 21-day-old ADC token had expired, and the raw RefreshError
  leaked through before the mapping existed).
- *Curated shelf x live zones.* GCE has no "GPU types with prices" list;
  inventing one dynamically means inventing prices. The shelf is code
  (reviewable, dated), availability is Google's own answer, and prices
  carry price_basis on every entry - the money rule extended to a second
  provider. An entry whose accelerator appears nowhere is shown out of
  capacity, never guessed.
- *Labels are the ownership boundary.* Every launch carries
  manifold=true and every list/get/terminate filters on it, so the
  reconcile sweep can never adopt - and terminate can never name - a VM
  Manifold did not create. In a project that also runs the owner's other
  workloads, this is a safety rule, not a convenience.
- *One instance id, no zone memory.* GCP ids are the instance NAME;
  list/terminate find the zone via aggregatedList, so backend restarts
  need no state Lambda rows do not already have.
- *Quota is a product surface.* Fresh projects hold ZERO GPU quota, which
  blocks first launches more often than anything code does. The form
  shows the number before the click; the refusal links the request page
  and names the metric.

**What the suite caught before the gate could pay to:** GAPIC's
aggregated_list does not promote `filter`/`project` to keywords - the
TypeError from the bare-keyword call read as a transient provider outage
and silently vetoed connection reaping (test_reconcile caught the
behaviour change). Request objects everywhere now.

**Awaiting the paid gate (blocked on a human browser step):** both gcloud
logins on this machine have aged out, so live verification stops at
"the error tells you exactly what to run". After `gcloud auth login` and
`gcloud auth application-default login`: free reads (live catalog zones,
real quota numbers), then - quota permitting - one g2-standard-4 L4
(~$0.71/hr listed) through launch -> driver install -> gpu-smoke ->
terminate. If quota is zero, the gate becomes proving the refusal chain:
the form's warning, the launch error's console link, and the request
flow. Compute API enablement on somnora-dev-01 also awaits that login.

## 2026-08-15 — The GCE gate: $0.18, the chain proven, three lessons encoded

One g2-standard-4 (1x L4, $0.71/hr listed) in us-central1-a, launched
through the guarded backend with a 1.5h ceiling, terminated clean, and
Google's own instance list confirmed empty afterwards. First real dollars
on the second provider: $0.18.

**Proven live:** the ADC auth path (after the owner's browser re-login);
the Compute API enable; the live catalog joining the curated shelf to 44
real zones for the L4; quota through the product (NVIDIA_L4_GPUS 0/16 -
this project has real quota, so the full launch could run); launch ->
boot -> managed SSH connection into a GCE box; and finally
`docker run --gpus all` printing "GPU 0: NVIDIA L4" from inside a
container - the exact path every Manifold job takes.

**Lesson 1: never pin a driver series.** The guessed 570 had no module
build for the running kernel while 535/565/580/595 all did - and the
meta-package fallback installed modules for the NEXT kernel, loadable
only after a reboot nobody asked for (modprobe: FATAL, silently
swallowed by `|| true`). The block now discovers the newest series with
prebuilt modules for `uname -r` exactly, with DKMS as the last resort.
Verified by repairing the live box with the discovered pair (580) and
watching the L4 appear.

**Lesson 2: group membership does not reach open sessions.** cloud-init
does `usermod -aG docker ubuntu`, but the managed connection is usually
established while the script is still running - on GCE (where the image
does not pre-add the group, unlike Lambda) that session got "permission
denied" on the docker socket forever, and every dispatched job would
have too. Fixed with an ACL on the socket, which is checked at open()
time rather than session start, applied after the docker restart that
recreates the socket. The residual: a docker-daemon restart mid-life
drops the ACL until the next boot; filed, not hidden.

**Lesson 3: the scratch-only refusal now explains itself.** gpu-smoke
mounts persistent storage, the GCE box was scratch-only, and the refusal
read "no filesystem recorded for instance ..." - correct behaviour
(Phase 39's rule doing its job) wearing an opaque message. It now names
the cause and both ways out. This also means the T+0 probe test could
not run through a template on this box - the container chain was proven
through the product's run route instead; a template-path GCE probe test
belongs to the Filestore phase, said plainly.

Also observed and filed: the instance card carries no `provider` field
(the gate's poll had to infer it), worth adding for a mixed fleet.

Manifold's first multi-cloud day ends with both providers real: Lambda
$5.10 across five gates, GCP $0.18 across one.

## 2026-08-15 — The job pipeline was showing a clipped sliver of a 6,150px column

Reported with screenshots: cards cut off top and bottom, a black rectangle
sitting over them, and scrolling made it worse. Five separate bugs, and
the live backend's own data explains the shape of all of them - 43 tasks,
41 of them with no `depends_on` at all.

**The geometry.** Independent jobs all landed in column 0, one per row, at
150px each: a 6,150px-tall column inside a 400px canvas. React Flow's
default `minZoom` is 0.5, so `fitView` could not shrink it (it needed
0.27) and silently gave up - leaving the middle band on screen. Fixed on
both sides: `minZoom={0.04}`, and isolated jobs are now GRIDDED rather
than stacked. That is honest rather than cosmetic: for a job with no
parents and no children, the x axis carries no dependency meaning, so
wrapping it into a grid states nothing false. Jobs that DO participate in
a chain keep strict `x = depth`, so "further right" still always means
"runs later".

**`fitView` is a mount-time prop, not a subscription.** The panel polls
every 4 seconds; nodes arriving later were never re-fitted. Now re-fitted
inside a rAF when the node/status SIGNATURE changes - not on object
identity, which changes every poll and would yank the viewport out from
under someone reading it.

**The black rectangle was the MiniMap**, 200x150 on a 400px canvas
(a quarter of the view) with `!bg-zinc-950` on a zinc-950 canvas. It now
appears only above 12 nodes, at 132x92, coloured by task status so a red
cluster is visible at a glance - and its background is a LITERAL hex,
because the dark theme remaps the zinc scale and `bg-zinc-900` rendered as
a light panel against the dark canvas (the trap the template editor
already documents).

**The label was untrue.** "An arrow means runs after" - the edges carried
no `markerEnd` at all, so nothing on screen was ever an arrow. They have
arrowheads now. Isolated nodes also no longer render a target handle: 41
dangling connector dots read as "these connect somehow", which is the
exact false impression this panel was rebuilt once already to stop making.

Verified in a real browser against the real 43-task backend, not by
reading the diff: every node inside the canvas (43/43), 6 columns, markers
present, minimap 5.4% of the canvas. `dashboard/e2e/job-pipeline.mjs`,
now a CI step beside the modal check. Note the harness must serve the
export on port 3000 exactly - on any other port the dashboard calls
ITSELF for the API and renders an empty graph, which looks identical to a
broken fix.

## 2026-08-15 — Phase 88: "installed" and "connected" stopped being indistinguishable

An agent in ANOTHER repo was told "Manifold is open for you to use",
found no manifold entry in its MCP registry, nothing on PATH, nothing in
~/.config, fell back to a stale hand-pasted instance IP, and lost the
session — while the app ran the whole time. Its forensic writeup is the
spec for this phase. Root cause found on OUR side: the Claude Code
registration was directory-scoped (`claude mcp add` defaults to local
scope), so from every other repo manifold read as "not installed". The
docs even said "in this project" — technically true, pragmatically a trap.

**What shipped, ranked by the writeup's own leverage ordering:**

1. **The dashboard says when NO agent has ever connected.** McpChip's
   empty state was `return null` — silent in exactly the state that
   burned the session. Zero all-time `mcp` audit rows is a knowable fact
   (the audit log is never pruned), so it now renders a dashed "no agent
   connected" chip linking to Settings → Connect an agent: live
   last-call status plus copy-able registration commands. Honesty
   constraint kept: the bridge is a stateless HTTP client, so we report
   activity facts, never a fake "connected" light. Staleness (>1h) still
   hides the chip — an agent HAS connected, quiet is noise; absence is a
   trap. Alternative considered: a real connection registry keyed by
   handshake. Rejected — it would claim liveness the stateless bridge
   cannot back.

2. **`--doctor`, self-verifying wiring** (`manifold-backend --doctor` /
   `uv run manifold-doctor`). Reports backend up (mock/real), token
   present AND accepted (presence/status only, value never printed),
   which agent configs register manifold at what scope — including the
   "every registration is directory-scoped" warning that names this
   incident's exact shape — instances running, breadcrumb present. Exits
   nonzero when an agent would be blocked. Lives in `app/doctor.py`, an
   outside-in HTTP client like the bridge; NOT wired into mcp_server.py,
   whose import allowlist (test_mcp.py) stays untouched.

3. **The discovery breadcrumb**: every backend boot best-effort writes
   `~/.config/manifold/manifold.json` — what Manifold is, the API URL,
   health-check, register + doctor one-liners. ~/.config on ALL
   platforms including macOS, deliberately: Application Support is where
   the app's state lives, ~/.config is where agents probe (the incident
   agent probed it and found nothing). Written by create_default_app
   only, so create_app stays side-effect-free for tests; no secrets;
   MANIFOLD_NO_BREADCRUMB=1 opts out. Alternative considered: a
   `manifold` CLI on PATH. Deferred — a macOS app cannot cleanly install
   to PATH without user action, and doctor + breadcrumb deliver most of
   the value free.

4. **Docs default to `--scope user`** with the incident as the stated
   reason.

Writeup item not shipped here: the stale hand-copied IP lived in the
owner's own rh3d skill files, outside this repo; Manifold already offers
the fix twice (live `list_instances`, and tailscale MagicDNS names).

## 2026-08-16 — The one state a real backend can never show you

Phase 88's "no agent connected" chip and its Settings card shipped in the
v0.2.2 installers verified by `npm run build` and nothing else. That is
exactly the failure mode this project distrusts: a typecheck proves the
component compiles, not that the state it exists for ever renders.

The state is unobservable in normal use. This machine's audit log has 251
`mcp` rows going back a month, and the chip's empty branch fires only at
ZERO rows all-time, so no amount of looking at the running product would
ever have shown it. It needed a backend with a virgin audit log.

`dashboard/e2e/agent-connection.mjs`: mock backend on a scratch
MANIFOLD_DATA_DIR, dashboard exported with `NEXT_PUBLIC_API_URL` pointing
at it, driven in real Chromium. 14 assertions, all passing - the chip
renders, links to Settings, the card states the fact in words, carries
`--scope user` and `--doctor`, the commands have Copy buttons, and then
the state FLIPS when a single mcp audit row is posted (the chip vanishes
and the live "MCP now" chip replaces it). The flip is the assertion that
matters: it proves the UI reports live audit truth, not a hardcoded
empty state.

**Two harness traps, both recorded in the file's header because both
looked like product bugs.** The export must be served on port 3000
exactly - the backend's CORS allowlist is localhost:3000 / 127.0.0.1:3000
and nothing else, so on any other port every call dies at the preflight,
`entries` stays null, and the chip renders nothing, which is
indistinguishable from the bug being hunted. And `npx playwright node
file.mjs` does not work: playwright's CLI has no `node` subcommand, so
the module must be resolvable by node itself.

**Not a bug, worth stating:** the chip renders nothing when the backend
is unreachable, because `entries` is null rather than empty. That is
correct - "no agent has ever connected" is a claim about data you have,
and an unreachable backend gives you none. Silence there, a chip only on
a confirmed empty log.

## 2026-08-16 — Autopilot's labels were exactly backwards

Reported as "Autopilot won't rent GPUs". It rents them fine: given a
compute-shaped goal it ran the whole lifecycle unattended in mock mode -
launch, recover from its own bad filesystem argument, poll to active, run
gpu-smoke, read logs, terminate with rescue. The complaint was real, but
it was three separate things wearing one symptom.

**1. Two of the goals were not compute tasks.** "Scan for tech news
highlights and synthesize a 1-sheet" has no action in the allowlist that
could serve it - Manifold cannot fetch a web page, and no GPU changes
that. The claude-brained run said so, at length, correctly. That refusal
was the product working.

**2. The local brain fabricated a result and scored green.** The only
model in the owner's Ollama is `shot-tagger:latest`, a fine-tune for
video shot tagging. As an Autopilot brain it called `list_instance_types`
with invented arguments (`region: "North America"`, `instance_type:
"Mid-Range"` - neither exists), then emitted `done` with a summary about
AI/cybersecurity/IoT news it had no way to know. Status: succeeded. That
hallucinated sentence then became the GOAL of the next run.

**3. The run that did the work was labelled a failure.** A serve goal
capped at 20 steps spent 11 of them on `wait` + poll cycles (a10 boot is
~4.5 min, then a 7B download and vLLM init) and 3 more on recoverable
errors. It ran out of turns at step 20 with vLLM just answering
`/v1/models` - one or two steps short of the test prompt and the
terminate. It was recorded `exhausted`, and it left an a10 billing.

**The fix: judge a run by what it DID.** `agent.run_effect` is pure and
computed at READ time - no migration, and it reclassifies the runs that
motivated it. An action counts only if it is in EFFECTFUL_ACTIONS
(launch_gpu, run_job, save_template, sync_outputs, terminate_instance)
AND its result carried no "error" key, because a guard rejection changes
nothing. Applied to the four real runs: all three `succeeded` rows are
`no_effect`, and the `exhausted` one is the only `acted`. The dashboard
now shows "no action taken" instead of a green badge for the first shape,
and "left an instance running" for the second.

**Deliberately not done: inferring whether the GOAL was achieved.** That
needs a judge and would be wrong sometimes in both directions. "Did this
run change anything" is decidable from data already recorded, and it is
enough to stop a fabricated summary from looking like a result.

**Also observed, not fixed here.** The idle sweep requires no running
task of any kind, and a `vllm-serve` task never exits - so an abandoned
served model is never idle, and with no max-lifetime ceiling set it bills
until someone notices. The ceiling is the guard that fires through a
server; nothing currently encourages setting one on a serve launch. Named
here as the known hole rather than left to be rediscovered on a bill.

## 2026-08-16 — Phase 90: an abandoned model server is idle

The hole named the night before, closed. Any running task used to make its
instance immune to the idle sweep, and a `vllm-serve` task never leaves
`running` - so an abandoned model server was never idle, and with no
ceiling set it billed until a human noticed. One did: an autopilot run ran
out of steps one turn short of its terminate, left an a10 serving Qwen,
and it ran for an hour ($1.29) with every guard working exactly as
designed.

**The idle verdict now pins on BATCH tasks only**, the same distinction
the ceiling has always used. A server is judged by the question that
actually defines idleness for one: is anyone using it?

- **Loading** (serving, but `/v1/models` not answering yet) counts as
  activity and RESTARTS the clock. Weights for a large model can outlast
  the idle window, and reaping a box seconds before it becomes useful is
  the worst possible moment. The window therefore runs from readiness, not
  from dispatch.
- **In use** needs no new machinery: `/v1/chat/completions`, the chat
  panel, and `/instances/{id}/run` all call `touch_activity` already. That
  is why this fix is small - the signal existed, nothing consulted it.
- **Ready and silent** for the full window is what an abandoned server IS,
  and it is now terminated through the standard flow, rescue included.

**Everything ambiguous fails safe.** No endpoint, an unreachable box, a
probe that errors or raises: all mean "leave it alone". This is the only
unattended code in Manifold that destroys a paid instance, so the bar is
not "probably idle", it is "answering and provably unused".

**A batch job still pins absolutely.** A fine-tune at 90% is destroyed by
no sweep, ready or not - the trade this project refuses to make. Server
and batch on one box: batch wins. keep-alive and auto-managed ownership
are checked before any of this and are unchanged.

**Cost of the probe:** `model_ready` is already cached (30s once ready, 3s
while loading), so a 15s idle poll adds at most one HTTP round trip over
an SSH forward that is already open.

**The golden matrix row changed, and that is stated in the file.**
`test_running_server_job_pins_its_instance` still passes - the harness has
no model client, so the probe says "not answering", and a server that is
not answering is still protected. But it no longer pins what its docstring
claimed. The real matrix (ready+silent terminates; loading, busy,
unprobeable, keep-alive, batch-beside-server do not) is
`tests/test_idle_serves.py`, where readiness is injected.

**Worth stating plainly:** the golden matrix row passed throughout this
change, for a different reason than it claimed - the harness has no model
client, so the probe says "not answering". The behaviour was really pinned
by an INTEGRATION test (test_parallel_dispatch), which failed immediately
and correctly, because there the probe answers. The unit fence went green
while the integration fence caught it: a reminder that a mocked harness
can only test the decision, never the wiring.

That integration row kept its Phase 35 intent (work on box A must not keep
idle box B alive) and changed only WHY box A survives: it is now kept alive
by being used, driving the same touch_activity path a real client does. A
second row was added beside it for the shape that cost the money - a ready
server nobody queries, reaped end to end through the app.

Alternative considered: default a max-lifetime ceiling on every launch
(`idle.default_max_lifetime_seconds`). Rejected as the primary fix - it is
coarse (a 4h default still bills ~$5 on an abandoned a10) and it changes
behaviour for every launch to work around a signal we already had. It
remains a reasonable BACKSTOP for boxes the idle sweep cannot see (an
unreachable instance is never idle-terminated by design), and is not
foreclosed.
## 2026-08-16 — Phase 91: a shell that dies says so, and says what survived

Reported as: "when I step away the app freezes, I have to kill the
terminal tab, and I lose my entire chat history with claude because my
chat isn't saved in Manifold. When I hot resume, it doesn't pick up right
where I left off."

Three separate things, and the first two were fixable immediately.

**The history was never lost.** Claude Code writes every conversation to
`~/.claude/projects/<cwd-with-slashes-as-dashes>/<session>.jsonl` on the
machine it ran on, and killing a shell does not touch those files. The
local terminal does `pty.fork()` + `execvp` with NO chdir, so its shells
inherit the backend's cwd - which is where the transcripts were, intact,
the whole time. Confirmed on the owner's machine: three conversations in
the repo directory, the newest 18.9 MB from that night.

**What actually killed the shell: `terminal_grace_seconds: 900`.** A
detached session waited 15 minutes for a reattach, then the reaper killed
its process group. The reasoning in the code was "a closed tab never comes
back" - but a FROZEN app is indistinguishable from a closed tab, and 15
minutes is shorter than a coffee break. Now 28800 (8 hours). A detached
shell holds a pty and, for an instance terminal, an SSH channel, and
nothing else; it cannot keep a GPU billing, because the idle sweep counts
terminal INPUT as activity and a detached shell produces none. The bound
exists only so a long-running machine does not accumulate dead shells.

**And the kill was silent.** No audit row, no notification - one log line
to a stdout nobody reads. In a product that will not terminate an instance
without first rescuing its files, the one destructive act that left no
trace was the one that destroyed an agent session. `on_reap` is injected
by main.py (terminal_sessions.py keeps its zero backend imports) and
writes both an audit row and a `terminal_reaped` notification, carrying
the tail of what was on screen so the record answers "what did I lose".
It reports BEFORE killing - after `kill()` there is nothing left to read -
and a callback that raises can never skip the kill.

**Reattach now explains itself.** Asking for a session id whose shell is
gone used to hand back a bare prompt: indistinguishable from "my work
vanished". Both terminals now print, into scrollback so it survives later
reattaches, that the previous shell had ended and that anything running in
it stopped. For the LOCAL terminal it also counts the Claude transcripts
recorded in that cwd and names the command: `claude --resume`. Only the
directory listing is read; a test asserts no transcript content can reach
the notice.

**The freeze itself is NOT diagnosed, and is not claimed to be.** Measured
after 23.5 hours of uptime: backend 70 MB RSS, 49 fds, 19 threads, /health
in 40 ms, /instances in 1.7 s; the WKWebView 57 MB. That rules out a slow
resource leak, and rules out nothing else - the backend's logs go to the
terminal uvicorn was launched in, so the 12:27 AM window left no record.
`scripts/capture-freeze.sh` exists to end that: one read-only command, run
DURING a freeze, that answers the only question worth asking first - are
the endpoints slow too (backend wedged), or fast while the UI is stuck
(the webview is, and the app's own 30s client timeout is firing against a
backend that answers instantly)?

Four bugs in that script's own first run, all fixed and noted in it,
because a diagnostic that lies at 1am is worse than none: an f-string with
a backslash in its expression (a syntax error before Python 3.12) printed
"(could not read)" over real data twice; `pgrep -f "uvicorn app.main"`
matched the `uv run` WRAPPER and reported its 15 fds as the backend's; and
a loose WebKit pattern reported Brave's renderers as Manifold's.

## 2026-08-16 — The notification kind that fired but could not be switched off

Phase 91 added `terminal_reaped` to `NotificationPrefs` and stopped there.
The kind fired, recorded, and pinged - and no user could turn it off,
because `NOTIFICATION_KINDS` is what `/preferences` advertises and the tuple
never learned about it. A notification you cannot silence is a worse
default than one that does not exist.

The fence that should have caught it had the right docstring and half the
assertion. `test_every_notification_kind_has_a_toggle` said it "catches the
next kind somebody adds to only one of the two lists" while checking one
direction only: every KIND has a toggle, never every TOGGLE is a kind. The
next kind went in the other list and sailed past. It checks both ways now
(`desktop` excluded by name - it gates delivery, not whether an event
notifies at all).

**Then TypeScript found the third place, and a bug that predates this.**
Completing the frontend `NotificationKind` union broke the build on
`NotificationBell`'s two `Record<NotificationKind, string>` maps - so
`budget_threshold`, shipped earlier, had been rendering its tone and label
as `undefined` in the bell the whole time. The incomplete union was hiding
it: an exhaustive Record cannot check exhaustiveness against a union that
is itself missing members. Both kinds now have a tone and a label, and both
have a Settings toggle with a hint.

Four places have to agree for one notification kind: the prefs dataclass,
NOTIFICATION_KINDS, the TS union (which drags in the bell's maps), and the
Settings list. The bidirectional test covers the first two; the type system
covers the third; the fourth is still prose in a component, which is a real
gap and named here rather than pretended away.

Verified against a live backend rather than by reading: the API advertises
10 kinds, `terminal_reaped` defaults on, PUT switches it off, and its
neighbours are unaffected.

## 2026-08-16 — Phase 92: the freeze now records itself

The freeze has been reported three times and investigated zero times, for
one boring reason: uvicorn logs to the terminal it was launched in, so
every occurrence erased its own evidence. Asked afterwards - "was the
backend actually slow, or did the browser stall?" - the honest answer was
always "no way to know now". That is not a hard bug, it is an
unobservable one, and the fix is observability, not a guess.

**The fork this answers.** The dashboard's client timeout is 30 seconds,
so "No answer after 30s (/instances)" has two opposite causes: the backend
was slow, or the backend was fine and the webview stalled and timed out
against a healthy server. Those need opposite fixes and looked identical.
Now: a `slow_request` line names the endpoint and its seconds, and the
ABSENCE of one is equally informative.

**The mechanism this catches.** If everything goes slow at once, the event
loop is blocked - one synchronous call in an async handler stalls every
request in the process, while each handler still looks innocent on its
own. A heartbeat that measures its own oversleep detects precisely that:
`await sleep(1)` returning after 9 seconds means the loop had no chance to
run for 8. One timer per second.

**Proved against a real freeze, not just unit tests.** A live mock backend
was suspended with SIGSTOP for six seconds (the closest thing to a laptop
sleep) and caught itself on resume:

    event_loop_blocked for 5.2s (slept 6.2s, expected 1.0s) -
    every request was stalled during this window

That is the line that did not exist at 12:27 AM. Normal traffic produced
zero warnings, so it is not a false-positive machine.

**Everything is best-effort and additive.** A read-only home directory
returns None rather than refusing to boot; the timing wrapper returns the
response untouched and re-raises the handler's own exception unchanged;
`setup_file_logging` is idempotent, because --reload re-runs the entry
point and stacked handlers would write every line N times. File logging
and the breadcrumb are both wired into `create_default_app` ONLY, so the
hundreds of apps the test suite builds never touch the user's log.

**And a bug in the capture script, found by running it.** With no log file
anywhere the new section printed NOTHING, which reads as "no freezes
recorded" when it actually means "I could not tell". Those are different
facts, and conflating them is how three freezes went uninvestigated. It
now says so, and names the reason (a backend older than this change).

Not claimed: a diagnosis. The freeze is still unexplained. What changed is
that the next one leaves a record whether or not anyone is at the keyboard.

## 2026-08-16 — Phase 93: the freeze had a mechanism, and it was ours

Three reports of the same thing: step away from the app, come back, the UI
is wedged. Tabs will not change, the terminal is a dead rectangle, a restart
is the only way out, and the Claude session that was running in that
terminal is gone. Phase 92 shipped the instruments to catch it in the act.
This is the cause, found by reading instead of waiting.

**It is two independent defects that produce one symptom.**

### One: polling had no in-flight guard

`usePolling` ran `setInterval(tick, intervalMs)` and `lib/api.ts` waits 30
SECONDS before it gives up on a request. Those two numbers are the whole
bug. While the backend is slow, every hook queues a fresh fetch every
interval and none of them finish, so arrivals outrun completions and the
queue grows without bound. It is an unstable queue in the textbook sense: it
does not recover on its own, which is exactly why a restart was the only
exit anyone found.

A browser opens about six connections per origin over HTTP/1.1, and uvicorn
speaks HTTP/1.1. Past six, the overflow waits in the browser's own network
queue - and the fetch behind a click on a tab waits there too, behind the
polls. "I cannot even change any of the tabs" is not a UI freeze at all. It
is a network queue, and the pile-up is what makes the next request slow
enough to deepen it.

Measured on the Jobs page against a backend stalled by 8s, over 24s:

|                              | before | after |
|------------------------------|--------|-------|
| requests issued              | 27     | 13    |
| peak in flight               | 12     | 5     |
| worst single endpoint        | 5      | 2     |
| backlog when it recovered    | 10     | 5     |
| issued while hidden (12s)    | 13     | 0     |
| reloads within 1.2s of reveal| 0      | 7     |

Ten outstanding against a limit of six is the freeze, as a number.

Two rules fix it. **One poll at a time per hook**: a tick that lands while
the previous is still out is dropped, or deferred if someone actually asked
for it - `refresh()` after a mutation still runs, at most one extra poll is
ever owed, and the queue cannot grow. **A hidden document does not poll**:
background timers are throttled and coalesced, then fire together on wake,
which is the moment the backend is slowest. Stepping away was building the
pile-up that greeted you on return. Coming back now polls once, at once.

Rejected: raising the interval (slower UI, same unbounded queue under a
stall), and lowering the 30s timeout (it exists for calls that ride the SSH
connection and are legitimately slow; cutting it would break file listings
to make a symptom quieter).

### Two: the terminal never went back for its shell

Phase 91 parked a dropped shell on the backend for 8 hours so a frozen tab
would stop costing an agent session. Nothing ever reattached. `ws.onclose`
set status "closed" and that was the end of the panel's involvement, so the
grace window was real and completely invisible: the user saw a dead
rectangle, killed the tab, and lost the conversation the parked shell was
still holding. The backend half shipped; the half a user could see did not.

The panel reconnects now, with a capped backoff and an immediate retry when
the window becomes visible again - which is the reported case exactly.

**Reconnecting turns a close code into behavior, so the backend had to
start sending one.** Three closes must NOT be returned from, and all three
used to be indistinguishable from a dropped network:

- `4410` the shell ended. Coming back would resurrect a shell the user just
  ended with `exit`, handing them a fresh prompt as if nothing happened.
- `4409` another view took this session. Coming back would steal it, kick
  the other tab, and the two would trade the shell forever.
- `4403` the origin was refused. Retrying cannot help.

Everything else is a lost socket, and the shell is parked and waiting.

Two details that only appear once a reconnect exists. `attach()` replays the
entire scrollback, which is correct after a refresh (empty terminal) and
paints a **second copy** of the session in place (full terminal), so a
reattach resets the terminal first. And `lastCols/lastRows` mean "the size
the PTY has been TOLD", which a new socket has not told it anything - left
alone, the first refit is deduped away and the shell wraps at the width it
had before the window changed.

### Both are proven by measurement, not by typecheck

`tsc` and `next build` pass on every version of this bug, and the only way
to see either defect is to watch behavior over time in a real browser. So
both get an e2e, and both were run against the code they replace:
`poll-pileup.mjs` fails 6 of 9 checks on v0.2.3, `terminal-reconnect.mjs`
fails 4 of 12.

The first run of the pile-up check passed everything while measuring
**nothing** - a static export served by a plain file server has no directory
rewrite, so `/jobs` is a directory listing, and every assertion in that file
is an upper bound satisfied by zero traffic. It asserts its own preconditions
now. That is the second vacuous test caught in three phases (the Phase 90
golden row was the first), and the pattern is the same both times: a check
whose subject never ran.

Two deliberate stubs, both stated in the files. The visibility half overrides
`document.hidden`, because headless Chromium will not reliably background a
tab. The terminal drop closes the socket from the page, because Playwright's
offline switch does not reach loopback - measured: the socket stayed open and
every assertion passed vacuously. Neither stub touches what is being tested;
Chrome's own event dispatch and network detection are not this repo's code.
The `exit` path stubs nothing at all.

**Not claimed: that this was the freeze.** It is a mechanism that predicts
every reported symptom - wedged after stepping away, tabs unresponsive,
terminal dead, restart the only exit - from code that is now fixed. Phase
92's logging stays exactly as valuable: if it happens again, the log says
whether the backend was ever slow at all, and if it was not, this was not it.

## 2026-08-16 — The notice that reported lost work that was never lost

Found by watching a browser during Phase 93's terminal work: every
brand-new dock tab opened to

    [manifold] The previous shell for this session had ended, so this is a
    new one.
    [manifold] Anything that was running in it (an agent, a job) stopped
    when it ended; see Activity for when and why.

when there had never been a previous shell. Phase 91 fed that notice on
every new session id, and a new tab always has one. So the one screen whose
entire job is being honest about lost work opened by reporting some.

Worse than a stray line: it is recorded through `feed()`, so it lands in
the scrollback and is replayed on every later reattach. The false report
became permanent for the life of the shell.

**Only the browser knows.** The backend cannot tell a fresh session id from
one whose shell it reaped - and after a restart it remembers neither, which
is precisely the case where the notice IS correct. So the client says which
it is, with `?resume=1`, and it means "I expected a shell to already be
here". Two things set it: a dock tab restored from sessionStorage, and any
reconnect after the first socket (new in Phase 93). The notice now fires
only when the client expected a shell AND the backend had to build a new
one, which is exactly when the statement is true.

Rejected: having the backend remember ids it has seen. It fails at the one
moment that matters - a backend restart kills every shell and forgets every
id, so the notice would go silent exactly when the user most needs it.

Rejected: dropping the notice. The report it answers ("I lose my entire
chat history") was real; the fix is to say it when true, not to stop saying
it.

`resumed` is read through a ref rather than passed into the effect's
dependency array. Re-running that effect tears the panel down, and its
cleanup sends `{"type": "close"}`, which really ends the shell - a prop
that only decides the wording of a banner must not be able to kill a
running agent session.

Proven both ways: four backend tests (three fail on the old behavior, the
fourth is the guard that resuming a genuinely dead session still says so),
and a browser assertion that fails against a backend reverted to Phase 91.

## 2026-08-17 — An instance says whose it is, and whether it is really idle

**Decided:** `GET /instances` carries three new things — `created_by` (which
principal launched it), `purpose` (what they said it is for), and `activity`
(the idle sweep's own verdict, with a state, a `busy` flag, and the reason in
words). `DELETE /instances/{id}` refuses an instance launched by a different
principal, and the override is `confirm_owner=<that principal>`, not `force`.

**The incident this comes from.** Three agent sessions shared this account
through one MCP token. One listed instances, found an A100 it had not
launched, and checked it every way it could: `uptime`, logged-in users,
running processes, writes to the NFS. All of them said idle. It was a vLLM
box six minutes into loading a 27B model from the shared HF cache — no users,
no obvious processes, nothing written, 30GB of VRAM held — and it was
terminated about sixty seconds before it would have served. The audit note
reads: "Verified idle before terminating: up 6 min, 0 users, no user
processes, nothing written to the NFS."

**A correction, because the first version of this entry got it wrong.** It
said that termination also cost a multi-hour extraction run. It did not, and
the mistake is worth recording because it nearly buried a worse bug. The two
boxes killed here rescued `files_found: 0` and were single-purpose and
relaunchable. The 126-workflow extraction run that died at 07:42 with retry
exhaustion had a different cause entirely, found by looking instead of
inferring: at 07:36:56 Manifold's OWN idle sweep terminated instance
4718a91f for `idle 1811s (limit 1800s)` — while its own telemetry table,
sampled every 32 seconds, recorded that same box at 36653 MiB of 40960 used
and GPU utilization of 100%, including a sample written at 07:36:57. The
model was being served over a hand-rolled SSH tunnel, so no request ever
reached `touch_activity`, and "no Manifold-visible traffic" was read as "no
work". Manifold measured a GPU pinned at 100%, wrote it down, and terminated
the instance for inactivity in the same second. That is the same error as the
agent's, one layer down and with better evidence available — see the entry
that follows this one.

Nothing in that reasoning was careless. Every question it asked, the API
answered, and every question was the wrong one.

**Why the payload, not a convention.** The list an agent sees carried
`launch_id` and nothing else about origin, so a box in use and a box
abandoned were byte-for-byte indistinguishable. Attribution existed — Phase
79 resolves a principal per request, Phase 81 built `api_principals`, and
`launches.created_by` has been a column since 79 — it just never reached the
one view everybody reads. The alternative was asking six sessions to each
remember to call `get_launch_status` per instance before acting. Conventions
that must be independently rediscovered by every reader are not guards.

**Why `activity` is the interesting half.** Phase 90 already taught the idle
sweep the exact distinction that was missed: a server not yet answering
`/v1/models` counts as ACTIVITY, precisely so a 70B downloading for 40
minutes is not reaped at minute 30. The dispatcher computed that verdict
every sweep, used it for one decision, and discarded it. Readers got
`idle_seconds` and reconstructed the judgment by hand from shell commands.
So the sweep now records its reasoning and `activity_status()` hands it out.
It is a cache, deliberately: answering "is it loading?" live means probing
the instance, and this feeds a list the dashboard polls every few seconds.
Each entry carries its age so a stale verdict can be recognised.

`busy` is the FACTUAL question (is work loaded and running here), not the
policy question (may Manifold reap this). A serving box that has been quiet
for a while is `busy: true` and still subject to the idle timeout; the
arithmetic stays visible in `idle_seconds`/`timeout_seconds`. Conflating the
two would have produced exactly the original bug in a new field.

Unknown is its own answer. An unreachable box, and an instance no sweep has
judged yet, both report `busy: null` — never `false`. "No evidence of work"
is not "evidence of no work", and that inference is the whole bug.

**Why `confirm_owner` and not `force`.** `force` means "burn it, I accept the
data loss" and skips the rescue entirely. Had it also waived ownership, the
single call for taking another principal's box would have been the one call
that destroys their unsaved files without looking. Two different admissions,
two different keys. `confirm_owner` follows `delete_filesystem`'s
`confirm_name`: you can only supply it by reading the record first, and a
guard you can clear without looking is a guard that gets cleared without
looking. The refusal returns the owner, the purpose and the override, because
a refusal that withholds whose box it is sends the caller back to try again
rather than to ask.

**What is deliberately NOT guarded.** The idle sweep and the ceiling call
`terminate()` too, and they pass no caller. They act for the system, not a
principal; had they inherited one, idle auto-termination would have silently
stopped working the moment a second token was issued — the product's central
feature, disabled by a safety feature. `caller` is opt-in for that reason,
the same way `request_launch` takes `created_by` explicitly.

An instance with no recorded owner is not guarded either. NULL `created_by`
means adopted, or launched before this shipped; refusing on an unknown owner
would teach callers to pass `confirm_owner` reflexively, training away the
pause the guard exists to create.

**Inert until team mode is on.** With one shared token every launch and every
caller resolve to the same principal, so nothing changes for a single-user
install. The guard starts protecting when per-principal tokens are issued.
That is a configuration step, not a code one, and it is the user's to take.

Proven both ways: 22 tests, 18 of which fail against the code they replace.
The 4 that pass on both sides are the regression guards — own-box
termination, the unattributed box, and the sweep never being ownership-checked
— and they are the ones that would catch this guard being over-applied.

## 2026-08-17 — A GPU at 100% is not idle, whatever Manifold saw of the traffic

**Decided:** Before the idle sweep terminates a box, it consults the GPU
telemetry Manifold is already collecting. Any sample above
`idle.busy_util_pct` (default 10) within the idle window defers the reap,
restarts the window, and writes one audit row. `set_keep_alive` joins the MCP
tool surface as the manual override.

**The evidence, from two of Manifold's own tables.**

```
audit_log          2026-08-16T07:36:56  backend  idle_termination
                   4718a91f... idle 1811s (limit 1800s)

telemetry_samples  07:36:57   36653/40960 MiB   util_pct 100
                   07:36:23   36653/40960 MiB   util_pct 100
                   07:35:51   36653/40960 MiB   util_pct 100
```

Manifold sampled a GPU pinned at 100% with 36GB held, wrote it down, and
terminated the instance for inactivity in the same second. A 126-workflow
extraction run failed at 07:42 with retry exhaustion, six minutes after its
endpoint vanished.

`touch_activity` is called by jobs, the terminal, the chat panel and the
OpenAI proxy — everything that goes THROUGH Manifold. That box served a model
over the user's own SSH tunnel, so nothing ever reached it. "No traffic we
can see" became "no work happening": the same inference an agent made about a
loading box the same night, one layer down, with better evidence available
and unread.

**Why this narrows the reaper rather than loosening it.** The distinction
matters because "make the reaper more reluctant" and "stop the reaper killing
boxes at 100% util" sound like one request and are not. A genuinely abandoned
box reads near-zero utilization and is still terminated on schedule — the
Phase 90 case, the one that pays for this product, pinned by four tests here.
What this removes is only the case where the box is provably working and the
sweep cannot see why.

**Why a window, not the latest sample.** Utilization is instantaneous and
spiky: the instance above read 100, 100, 0, 100, 100, 0 across six
consecutive samples *while serving*. A rule reading the newest sample would
have reaped it on roughly half of all passes — a coin flip on someone's job.
The question asked is "did any sample show work during the period we are
calling idle", over exactly that period.

**Why peak and not mean.** One busy card out of eight is a working box. The
mean is the idle-SPEND figure, where under-reporting busyness is the safe
direction; here it is the reverse, and a mean would let seven idle cards vote
a real job to death.

**Why VRAM is not consulted.** A loaded model sitting at 0% is precisely the
abandoned server Phase 90 was written to reap: it holds 30GB forever and
answers nobody. Protecting on memory would undo that and rebuild the
hour-long bill. Utilization is work; VRAM is only residency.

**What this deliberately does not cover,** recorded so nobody trusts it
further than it goes. A CPU-bound phase — CUDA extension builds, weights
streaming off NFS — shows little GPU utilization, so a long *setup* is not
protected; a second project's 15-25 minute detached bootstrap has exactly
that shape. Neither is a box whose telemetry never arrives, where there are
no samples at all. In both cases the sweep behaves as it did before. This
only ever ADDS protection on positive evidence of work.

That is also why `peak_util_since` returns the sample COUNT alongside the
peak. "No samples" and "samples, all zero" are opposite findings — no
evidence versus evidence of no work — and collapsing them into a bare peak of
0 would have rebuilt, inside the fix, the exact inference the fix exists to
remove.

**The money backstop is untouched.** Nothing here bounds a job that stays
busy forever; the max-lifetime ceiling is checked before the idle verdict,
never defers to a server, and remains the only guard that applies to a box
doing real work. A test pins that a ceiling still kills a box at 100%.

**`set_keep_alive` on the MCP surface.** The escape hatch was recommended to
two agent sessions three times before anyone checked whether they could call
it: the route and the dashboard switch had existed since Phase 5, and the MCP
server never got a setter. An override an agent cannot call is not an
override. Its docstring states the billing consequence and names the ceiling
as the backstop, because a tool that only says "this keeps your box alive"
is a tool that leaves boxes alive.

Proven both ways: 12 tests, 6 of which fail against the code they replace.
The other 6 are the regression guards — abandoned reaped, loaded-but-idle
reaped, below-threshold reaped, stale samples reaped, no-telemetry unchanged,
ceiling still fatal — and they are the ones that would catch this being
turned into a licence to bill.

## 2026-08-17 — What the GPU-utilization gate does not cover, in the words of the lane it fails

**Recorded because the gap is structural, not incidental.** The idle sweep now
spares a box whose GPU shows work (see the entry above). The session running the
Red Hope 3D lane took its whole run apart afterwards and produced the sharpest
statement of what that still misses:

> the shape of the exposed job is not exotic. It is "provision an env, then use
> the GPU" — the normal opening of every batch either of us runs, and the quiet
> half is always at the front, before any burst exists to spare it.

That is the important sentence. The gate is evidence-based, and evidence
accumulates *after* the risky window rather than during it.

**The measured case.** A TRELLIS.2 bootstrap: stage 4 of 6 alone — flash_attn,
flex_gemm, cumesh, o-voxel — ran CPU-and-ninja bound at `build procs: 5` with the
GPU polled four times and never above 0%. It fit inside the 1800s window only
because the 16 GB model download collapsed to 37 seconds off a warm NFS cache.
On a cold cache, that stage plus the download exceeds the window, the gate finds
nothing above the bar, and the box is reaped mid-provision. Nothing in the
telemetry would have hinted at it, and the person hitting it would blame the box.

By contrast the same lane's actual work — thirteen bakes at ~3 minutes each,
peaks of 64% and 97% between 0% troughs — held the window comfortably. The gate
spares real GPU work on merit. It is provisioning it cannot see.

**Why the fix is not "also protect a quiet new box".** Three reasons, and none of
them is that the gap does not matter:

- A blanket grace period after first connection is a licence to bill for a box
  that boots, does nothing, and is forgotten — the failure Phase 90 exists to
  prevent, reintroduced at the front of the lifecycle instead of the back.
- The honest existing answer is per-launch: `idle_timeout_seconds` sized to the
  job, or `set_keep_alive` around the provisioning stretch. Both are explicit,
  both are already there, and both put the decision with whoever knows how long
  their bootstrap takes.
- Guessing "this box is probably still provisioning" from boot age is the same
  species of inference — reasoning from a proxy instead of evidence — that this
  whole sequence of entries exists to remove.

So the gate stays narrow and this stays documented. What would change the
calculus is a signal that is actual evidence of provisioning rather than a proxy
for it: the sidecar already reports processes, and a build running under the
job's own working directory is a fact rather than a guess. That is a larger
change than tonight's and it is not made here.

**The operational advice, until then:** size `idle_timeout_seconds` to cover the
cold-cache bootstrap, not the warm one. The warm number is the one you will
measure and the cold one is the one that kills you.

## 2026-08-17 — Manifold reports its own death, and deliberately cannot cure it

**Decided:** A new `backend/app/liveness.py` that observes, classifies and
says — `up | lagging | wedged | app_gone | backend_died | unknown` — plus a
tombstone (`backend_started` / `backend_stopped` audit rows) so the next boot can
tell a quit from a death. Exposed as `manifold-watch`, folded into
`manifold-doctor`, and summarised in one sentence the MCP bridge hands any agent
that gets ECONNREFUSED. **It never restarts, kills or starts anything.**

**The incident, and the wrong turn.** The desktop app stopped at 23:21:25 and
came back at 23:26:56 — 331 seconds — while a $1.99/hr A100 billed and five MCP
bridges got connection-refused. Nobody was told; another agent session found it
by being blocked and asking a human.

Then the diagnosis went wrong, and that is the more useful half. No crash report,
no jetsam record, the machine never slept (`pmset`: *Total Sleep/Wakes since
boot: 0*), and the log stopped mid-line with no shutdown marker — so it read
exactly like a silent crash, and hours went into hunting one. It was almost
certainly a normal quit: `desktop.py`'s parent watchdog calls `os._exit(0)`,
which bypasses the FastAPI lifespan, and its only message goes to stdout rather
than the log. **A deliberate quit and a silent crash were indistinguishable in
the record.** That is the defect. Not that the app stops — it is allowed to stop
— but that stopping leaves no trace and announces nothing while paid work depends
on it.

A first pass at the same log also concluded a *three-hour* outage, by reading
`manifold.log` and never noticing `manifold.log.1`. A full gap scan over both
files (76,724 stamped lines) finds exactly one gap over 45 seconds, and it is
331s. Rotation is not an edge case in this codebase; it is the thing that
produced the wrong answer twice.

**Why a reporter and not a supervisor.** An auto-restarter was the obvious fix
and three independent attacks killed it:

- It would have fought a user pressing Cmd-Q. Tonight proves the deliberate quit
  is the common case, not the rare one.
- **A restart reseeds `dispatcher.last_activity`.** A restart loop therefore
  pins the idle countdown at zero and silently disables idle auto-termination —
  the product's central money guard, switched off by a safety feature. Two of the
  three proposals claimed the opposite until this was pointed out.
- A supervisor that quietly revives a crashing backend converts a loud failure
  into a quiet one, which is the direction this entire codebase spends its effort
  travelling away from.

So the strongest thing this module does is print a sentence. A test greps its own
source for `Popen`, `os.kill`, `SIGTERM` and friends, so a later contributor who
adds the ability to act has to argue with the reasoning rather than with nobody.

**Wedged is not dead, and the thresholds are measured.** The same log carries
nine `event_loop_blocked` warnings, worst 4.4s. A process that is alive and slow
must never be handled like one that is gone, because the correct responses are
opposite. So `lagging` starts at 2s — just above the worst observed stall — and
`wedged` at 10s, far enough beyond the distribution that the verdict means a real
wedge. Both are reported; neither is acted on. Guessing those numbers instead of
reading them off the record would have been the same species of error as the rest
of this file.

**`unknown` is a real answer.** Where `pgrep` does not exist (Windows, a stripped
container) `shell_running()` returns None, not False — answering False would
manufacture a `backend_died` verdict out of a question that was never asked. Same
rule as `busy: null` in the instances payload, and `previous_run_ended_cleanly`
returning None on a database with no history rather than reporting a crash.

**No invented numbers.** The message says *"3 instance(s) still running on Lambda
and still billing"* and never a dollar figure, because `live_launches()` selects
no rate column. On its first real run against the live database it reported three
while a stale `list_instances` in the same session said one — and the reporter
was right: two more A100s had launched four minutes earlier. The tool caught its
author being out of date, which is the job.

**What it does not cover.** It cannot see a backend that is up and wrong, only
one that is absent or unresponsive. `manifold-watch` runs for as long as someone
runs it; it is not a daemon and does not survive a logout — making it one is a
launchd/Task Scheduler decision the owner should make deliberately, not something
to inherit from a bug fix. And the tombstone only starts telling the truth after
the first relaunch that carries it; until then `previous_run_ended_cleanly`
reports None, which is honest rather than wrong.

## 2026-08-17 — The fleet panel, and why it does not stream

**Decided:** `MultiGpuTelemetry` is replaced by `FleetPanel` — one compact row per
running instance (name, purpose, GPU and VRAM bars, activity state, idle
countdown) with an aggregate footer. It reads the LAST RECORDED telemetry sample
from SQLite, served as a new `telemetry` key on each row of `GET /instances`, and
it opens no stream of its own.

**What it replaced.** The old panel called itself "Real-time multi-node cluster
aggregation" and aggregated nothing: it was a tab switcher that rendered ONE
instance at a time using the same `TelemetryChart` the instance card already shows
below. So it duplicated the card, labelled each box by launch id while the card
labelled it by name — two names for one box on one screen — and it did not use
Manifold's actual cluster concept at all, listing active launches instead.

**Why it looked broken, which was a third, separate thing.** It sits in a
half-width cell (`grid-cols-1 lg:grid-cols-2`), and CSS grid rows stretch to the
tallest sibling. One small tile inherited `VisualTaskGraph`'s height, leaving a
column of dead space. Fixed with `items-start` on the row and `self-start` on the
panel. The same squeeze is why the card's text wrapped mid-word: `TelemetryChart`
is built for a full-width instance card.

**Why SQLite and not the live stream.** This is the load-bearing decision.
`TelemetryChart` gets its numbers over a WebSocket that the backend feeds by
opening and tearing down an SSH local port forward **every two seconds**
(`sidecar_client.py`, `_request` → `_forward` → `finally: close`). That is
affordable for one focused chart. For a fleet list it is not: eight rows would be
roughly 240 SSH channels a minute to draw eight progress bars, duplicating work
the dispatcher already does on its own 30s telemetry loop. Nothing in that chain
multiplexes — `sidecar_for()` builds a fresh client per call.

So the panel reads `telemetry_samples`, which is already collected and paid for,
via one hoisted `db.latest_telemetry()` query for the whole fleet. Not one query
per instance inside the loop: this route is the hottest in the app (the home page
polls it every 2s) and an N+1 there is the shape of the pile-up Phase 93 had to
undo. The panel itself polls nothing — instances are passed down from the page,
which is already polling.

**The cost of reading a stored sample is that it can be stale, so the panel says
so.** Each row carries `at`; past 150s the bars grey out and the row is labelled
with the reading's age. The aggregate averages only over boxes with a current
reading and reports how many were excluded, because folding a stale or missing
box in as zero would drag a busy fleet toward idle — the same
absence-treated-as-a-zero mistake the rest of this file keeps unlearning. An
instance never sampled is absent from `latest_telemetry` entirely and renders as
"No GPU reading yet — not the same as idle", never as 0%.

**Two defects this surfaced, both mine, both from the release two hours earlier.**

- `Instance` in `lib/api.ts` did not declare `activity`, `created_by` or
  `purpose`. The backend had been sending them since v0.2.4 and TypeScript did
  not know they existed; there were zero consumers. Fields shipped for agents,
  invisible to the app.
- `idle.busy_util_pct` was documented in config.yaml and **unreadable**.
  `load_settings` names every idle field explicitly, so a setting missing from
  that list keeps its dataclass default whatever the file says — which made "set
  it to 0 to switch the check off", a sentence I wrote in that file, untrue.
  Also fixed: `_iso_seconds_ago` emitted microseconds while `db.utcnow()` writes
  `timespec="seconds"`, and SQLite compares these as strings, so a bound in the
  same second sorted after the stored value ('.' > '+') and silently dropped the
  boundary second from the window.

Both were found by standing the thing up against a mock backend and looking at
it, not by the tests — which is the same way the `busy: false` defect was found
two hours earlier, and is becoming the pattern worth naming: these tests are good
at proving the path they model and blind to the one the user takes.

## 2026-08-17 — The pages stop asserting what they do not know

**Decided:** Five fixes from an adversarially-verified audit of Jobs, Storage and
Autopilot, chosen because each one removed a FALSE CLAIM rather than adding
polish. Items that merely added information (instance names on job cards, unshown
backend fields, the server-template detection) were deliberately held.

**1. Outage honesty.** Jobs and Autopilot destructured only `data` from
`usePolling`, discarding `error`/`stale` — so `(tasks ?? [])` rendered "No active
jobs." and "No runs yet." out of a request that never returned, and Autopilot
showed the full "No brain available" setup tutorial for a backend that was simply
down. During the exact freeze this codebase spent Phase 93 fixing, the three
pages the owner would check all said *nothing is happening* while instances
billed. A shared `PollErrorBanner` now renders the same treatment the home page
already had (banner + snapshot timestamp + greyed, non-interactive content), and
every empty state is gated on `data != null`: "No active jobs." is only ever a
loaded fact, never a default. Storage had the INVERSE bug — `readOk` starts false
and only flips inside an effect that early-returns when nothing is selected, so a
healthy account with zero filesystems claimed "the backend or storage is
unreachable" forever, and every page load flashed it. A `fsLoaded` tri-state
splits loading / unreachable / genuinely-empty / no-match into four sentences,
because only one of them is bad news.

**2. Ceremony proportional to damage.** "Clear history" fired with no
confirmation and permanently deletes `task_logs` plus the succeeded-task rows
that `db.task_durations()` reads — one click silently flipped the EstimateWidget
on the same screen from "measured · N runs" to "rough · no history yet". The
per-file Storage delete confirm was the single word "Delete?" for a
possibly-16 GB checkpoint, while the whole-filesystem delete above it demands the
name typed back. Both now name what is destroyed; neither is a ritual.

**3. The last three raw `setInterval` loops.** The log tail (400 lines / 1.5s),
the readiness probe (5s), and the autopilot step poll (1.5s, the whole
untruncated step history each tick) — none with an in-flight guard or hidden-tab
check, the exact class Phase 93 eliminated everywhere else. Each is now a child
component that mounts only while relevant (logs open, run expanded, serve job
running), so `usePolling`'s mount-time tick doubles as the immediate first fetch.
That dodges both traps the naive conversions hit: wrapping the loader in a
conditional wipes loaded lines every tick, and swapping the hook in place shows
"No steps yet." for a full interval on expand.

**4. Outcomes the product reported that did not happen.** `dispatcher.py` writes
`error="cancelled by user"` precisely so a stop would not read as "a baffling
container exited 137" — and `task_queue.mark_finished` then maps any non-empty
error to `status="failed"`, keeping the container's real exit code. The author's
stated intent, defeated one function later; the frontend now honours it (zinc
`cancelled` badge, calm text, no exit code, no auto-opened post-mortem — the code
stays in Logs). Autopilot rendered every closing summary in the success tint,
including refusals ("No GPU launched; no spend incurred. Reason: …") — green now
requires `succeeded` AND `effect !== "no_effect"`. And a failed log fetch left
"(no output yet)" on screen, a confident claim the job produced nothing; unknown
now reads as unknown. Fixing the messages also un-hardcoded the ":8000" in
"Backend unreachable. Is it running on :8000?" — which this session watched name
the wrong port while the real backend listened on :8099, in the middle of the
new banner built to be trusted during outages.

**5. Text that existed to be read and could not be.** The rendered-config
preview — the thing you check before spending money — was 171px wide (~23
monospace characters, `white-space: pre`, no scroll): the Jobs page's 460px left
track, split again by the form's own `lg:grid-cols-2`. One deleted class; the
preview now gets the full 418px. Long S3 keys pushed Storage's delete-confirm
buttons off a viewport with `overflow-hidden` — a confirm that could not be
cancelled. Job-card headers packed ~718px into 586 with no wrap.

**Verified in a real browser, both directions:** with the mock backend up, the
seeded job renders; killed mid-view, the banner + greyed snapshot appear and "No
active jobs." does not; cold-loaded during the outage — the manufactured-claims
case — Jobs, Autopilot and Storage all decline to invent an empty state. 15
assertions, all of which fail against the previous code.

## 2026-08-17 — Observed: a backend restart does not touch in-flight instance work

**Recorded as an observed property, not a design intention** — the design intent
existed (instances outlive the backend; `resume_pending_launches()` and
`adopt_running_instances()` re-attach on boot), but tonight it was demonstrated
under both lifecycle phases, on the live account, by accident of timing rather
than by test:

- **Mid-boot** (v0.2.4 install): an A100 was 181 seconds into boot when the
  backend went down for 331s. The launch row persisted, the boot continued on
  Lambda's side, and the new backend resumed the wait and adopted the box.
- **Mid-transfer** (v0.2.6 install): a detached rsync — 34 GB, 11,690 files,
  running between two instances — started ~7 minutes before the restart and ran
  straight through the 24-second window. It never blinked, and a full `-c`
  checksum verification afterward reported zero mismatches across every file.

What actually breaks during a restart is exactly one thing: MCP/HTTP calls fail
for the duration, including parked `wait_for_launch` long-polls, which return an
error the caller should retry rather than read as a failed launch.

**Why this is worth a line:** it is the safety argument for making the bridge
retry connection-refused errors through a restart window (a planned change), and
it is the fact that makes "install the new backend" a routine operation rather
than a scheduled outage. The etiquette of asking active sessions for a go/no-go
remains good manners; this entry is why it is manners rather than necessity.

## 2026-08-17 — Phase 95: facts move out of agents' memories

**Source:** a product review by the heaviest agent user this platform has had —
six launches, two mistaken terminations, a filesystem migration, a live backend
upgrade mid-run. Seven findings; five built, each chosen because it moves a fact
an agent had to REMEMBER into something the platform KNOWS. Verification
sharpened the best one: the "45s cap" they resented on run_command is ours
(capped so responses beat the MCP client's ~60s kill), and the nohup-and-poll
boilerplate they called their most repeated friction was prescribed by that
tool's own docstring. The workaround was our documented advice.

**run_detached.** The command travels to the box as SFTP bytes — never through a
shell line, so quoting stops being a hazard and injection stops being a
category — and runs under setsid with a wrapper that records the exit code.
State lives on the box plus one registry row, so a backend restart changes
nothing (the property observed twice on this account). The liveness half is the
point: the telemetry loop probes open handles in one round trip, and a live pid
is EVIDENCE that keeps the idle sweep away — fresh evidence only, two sampling
intervals, because stale confirmation protecting forever would be keep-alive
wearing a lab coat. Four states read literally: running, exited, vanished
(ended, HOW unknowable — exit_code stays NULL), unreachable (a state of the
connection, never of the command). The explicit touch-file hint for work started
OUTSIDE Manifold was considered and deliberately not built: run_detached exists
precisely so that work is started through Manifold, and two protection channels
would give the sweep two stories to reconcile.

**The truthful-or-absent rule is now a hard rule in CLAUDE.md**, not a pattern
in commit messages. And `purpose` is required at the agent surface: the MCP tool
refuses a purposeless launch with the reason, while the backend stays permissive
for the dashboard and older bridges. The dashboard's own launch form gained the
field it never had — the audit that added `purpose` to the API left the app's
own button producing unattributed boxes.

**Connection-refused retries, timeouts never.** A refused request never reached
the backend, so replaying it cannot double an effect; a sub-minute upgrade now
reads as one slow call. Bounded at ~40s because the MCP client kills the request
at ~60s and an answer nobody is listening for is not an answer. A TIMED-OUT
request may have landed — replaying one could launch a second GPU — so timeouts
surface immediately, always.

**A drifted bridge says so.** /health now carries the backend version; the
bridge (stdlib-only, per the AST wall — it reads its own version from
importlib.metadata precisely because it may not import app modules) compares
once and appends one line to every result while behind. Twice a tool shipped
that a running agent provably needed and could not call, with nothing telling it
a newer surface existed. The bridge still cannot be refreshed mid-session — it
is a child of the agent's session, not of the app — but "you are behind, and
here is what that costs you" is the half Manifold can own.

**Storage is an estimate, and says whose.** Lambda publishes no filesystem rate,
so ~$50/month sat invisible in every reportable number until a manual audit.
The rate now lives in config.yaml where the user vouches for it; spend reports a
`storage_estimate` block computed FROM its own displayed figures (the two shown
numbers multiply to the shown estimate), never folded into launch totals, and
absent — not $0 — when the rate is 0 or the filesystems cannot be read. Born
readable: a test loads it from YAML, the busy_util_pct lesson applied at birth.

**Deferred:** multi-filesystem mounts (the provider layer already takes a list;
Manifold's request models narrow it to one — a launch-path change deserving its
own phase), and true mid-session bridge refresh (not Manifold's to fix).

## 2026-08-17 — Design constraint recorded for the vllm extra_args passthrough

Not built yet (queued for the next phase); written down now because it is the
kind of fact that dies in a chat scrollback. Both agent reviews rank a template
`extra_args` passthrough for vllm-serve as the highest-leverage missing piece —
one inexpressible flag (`--max-num-seqs`) forced a hand-rolled server that cost
proxy routing, activity visibility, log streaming, and restart supervision in
one move. The refinement, from the session that hit it: **the allowlist matters
more than the passthrough.** `--max-num-seqs` and `--gpu-memory-utilization`
are tuning knobs; `--download-dir` and `--trust-remote-code` are supply-chain
surface. A template that passes arguments through verbatim has traded an OOM
problem for a supply-chain one. A dozen NAMED flags covers every case either
reviewer hit; anything outside the list is refused with the list in the error.

## 2026-08-17 — Phase 96: the missing flag, and three cheap truths

**The flag.** Both agent reviews ranked the same root cause first: vllm-serve
could not express `--max-num-seqs`, so a model that OOMs a 40 GB A100 without it
forced a hand-rolled server — which then cost proxy routing, activity
visibility (the 07:42 reap), log streaming, and restart supervision, all
casualties of one inexpressible flag. The fix is `extra_args` on vllm-serve,
built to the constraint recorded before it: **the allowlist matters more than
the passthrough.** Twelve named tuning flags; `--trust-remote-code` and
`--download-dir` are deliberately absent (supply-chain surface, not knobs); the
backend refuses anything else at enqueue with the full list in the error, and
`to_api` publishes the list so the wall is discoverable without being hit.
Mechanically safe by construction: the template's bootstrap builds argv in
Python and execvp's it — no shell ever sees the value — so validation is purely
about which vLLM flags pass, with a conservative value charset anyway. The
mechanism (`arg_allowlist` on any string parameter, load-time-checked) is
generic; sglang-serve can adopt it when someone needs it.

**Three truths.** The launcher can now set the display `name` from MCP (a box
got hand-renamed in the UI mid-incident because a name was the only ownership
signal that existed; `LaunchRequest` had the field all along — the tool never
exposed it). Spend breaks down by `purpose` (and the Phase-81 `created_by`
grouping is finally surfaced to agents via `get_spend_breakdown` — it existed
for months with no agent-reachable reader, the unshown-field pattern again).
And the rescue hook's scope is stated where it matters: `/workspace/ephemeral`,
NOT `$HOME` — so "files_found: 0" reads as "nothing in scope", not "nothing was
lost", on the two tools whose descriptions imply safety.

**A caveat learned live:** Lambda's `bytes_used` counter lags real contents by
hours — a filesystem that had just served a 16 GB model read 0 bytes. The
storage estimate's note now says so; a single reading is not evidence either
way, in either direction.

## 2026-08-17 — Phase 97: a ceiling anchored where the work starts

**Decided:** `max_active_seconds`, a second per-launch ceiling anchored at
`active_at` (health-check pass) instead of launch acceptance. The absolute
`max_lifetime_seconds` remains the outer bound; either firing terminates
through the same rescue-files-first flow, and the audit detail names WHICH
ceiling fired.

**Why:** the folklore. An agent measured 35 minutes of a 3-hour ceiling spent
before the first token — boot, a driver reboot, a ten-minute model load — and
began sizing every ceiling as "run + 40 minutes" by hand. Sizing rules that
users must carry in their heads are exactly what a platform exists to absorb;
the anchor (`active_at`) had been recorded on every launch row all along.

**The rules it inherits and the one it does not.** Deferral is identical to the
absolute ceiling (a batch job pins; unreachable refuses — you cannot rescue
what you cannot reach). The floor is NOT: `validate_max_lifetime`'s floor adds
the whole boot budget because its clock starts before boot; the active clock
starts after, so its floor is just the minimum idle window (a shorter bound
would out-race the idle sweep itself). Still reject-not-clamp — silently
rewriting a number that destroys instances is its own kind of lie.

**Truthful-or-absent, applied:** a box that has not reached active has no
clock. Breach None, countdown None — never 0 — because "no clock yet" and "0
seconds left" are different facts on a destructive control. The card renders
"(clock starts when the instance is active)" for that state, and the warning
path warns on whichever ceiling lands sooner, named.

Launch-time only for now: the per-instance edit route still edits only the
absolute ceiling. Deliberate scope cut, noted here so it is a decision rather
than an oversight.

## 2026-08-17 — Phase 97, parts 2-3: lifetimes reach the log, and Settings uses its page

**Instance-lifetime worklog entries.** get_work_log answered "what happened on
this account" with jobs and autopilot runs only, so days of raw GPU sessions —
launches, notes, durations, costs, terminations — left no trace, and "what are
these A100s?" became a whodunit. Every launch that settles now writes one entry
from data already held: purpose (or "(none stated)"), launcher (or
"unattributed"), active and total time, a cost upper bound carrying spend.py's
own disclaimer, and the REASON it ended — threaded from the sweep, so an idle
reap logs "idle: idle 1811s (limit 1800s)" and the reconcile path logs
"terminated outside Manifold". Best-effort by construction: a log entry must
never be able to break a termination, and unknowable durations are omitted,
never zeroed. Fixing the reason plumbing surfaced five tests asserting
terminate()'s kwargs EXACTLY (`== {"force": False}`); they now assert the claim
they guard — force is False, never unattended — instead of an incidental dict
shape.

**The Settings page.** Every card lived in a 672px column with no mx-auto —
left-hugging inside the centered 1104px layout, dead space growing with the
window. Now: the Status card spans the full container; everything below flows
in two PACKED column stacks. Not grid auto-placement — the first attempt showed
placement pairing a short card with a tall one and leaving a hole beneath it
(screenshotted, rejected) — two space-y flows pack each column tightly, credentials
entry down the left, the big reference panels down the right, single column
again on narrow windows. Verified by screenshot against the mock rig, not by
assumption.

**Process note, earned twice tonight:** two concurrent `uv run` invocations
against the same project can deadlock on the environment lock — a 4-minute
suite sat wedged for 1h40m while the visual rig's mock backend held uv's
attention. Run the rig or the suite, not both.

## 2026-08-18 — Phase 98: the front door tells the truth, and the hot table stops growing

**The agent onboarding doc was four phases stale.** `docs/manifold-skill.md` is
the first thing every agent reads (`get_skill`, ordered by the MCP server's own
instructions), and it described the pre-incident surface: no `purpose` (now
required — the doc walked new agents into a schema error), no `run_detached`,
one ceiling, and an idle-protection claim that had become false. Every peer
session this week learned the new surface from chat messages — the exact
failure class both reviews named, at the documentation layer: data that existed
and was not carried to the sentence agents act on first. The doc now opens its
multi-agent section with the three rules the incident taught, each with its
one-line origin story, because a rule with its incident attached survives
paraphrase. `mcp-setup.md`'s tool table matches the real signatures.

**Telemetry retention.** `telemetry_samples` gained a row per connected
instance every 30s forever, and since Phase 96 it is read on every `/instances`
poll. An hourly prune now rides the telemetry loop (no loop of its own).
Default 30 days, DELIBERATELY equal to `max_lifetime_max_seconds`: idle-spend
accounting reads samples across a launch's whole window, so no live launch may
outlive its own telemetry. 0 keeps everything. One audit row per prune that
deleted anything.

**`audit_log` is never pruned, and that is now pinned by a test** that greps
the db module for `DELETE FROM audit_log`. It is the forensic record:
reconstructing one night of terminations depended on rows nobody knew they
would need. Whoever adds a prune path someday argues with that test and that
night, not with nobody.
