# Implementation plan: IDE attach + per-instance idle timeouts

Hand this to the orchestrator as-is. Two phases, each on its own branch with a
hard gate. Both are buildable now with no dependency on the multi-provider work.

Scope was chosen to exclude spot-instance recovery and cross-provider price
comparison: both require a second provider to exist first (Lambda has no spot
tier), so they belong after the Google Cloud adapter lands.

Standing rules for both phases (from CLAUDE.md, restated so the orchestrator
does not have to infer them):

- Feature branch `phase-N-...`, merge to `main` only at an approved gate.
- All guards live in the backend. The dashboard and MCP server stay thin.
- Tests run against mocks only. No live spend. `uv run pytest -q` must pass.
- `npm run build` must typecheck clean.
- Every non-obvious choice gets a DECISIONS.md entry: what, alternatives, why.
- Prefer boring, readable code.

---

## Phase 68: per-instance idle timeout

Smaller, no new external surface, and it de-risks Phase 69. Build it first.

### Why

`config.yaml` has a single global `idle.timeout_seconds: 1800`. A long
fine-tune and a five-minute smoke test want different windows, and the current
answer is the blunt per-instance `keep_alive` switch, which turns the guard off
entirely. That is a spend guard with only an on/off setting. dstack's
`idle_duration` is per-run; this is the same idea inside our existing model.

### Changes

**`backend/app/db.py`**
- Add `idle_timeout_seconds REAL` (nullable) to the `launches` table. NULL
  means "use the configured default", so existing rows need no backfill.
- Additive `ALTER TABLE` in the existing migration path. Do not rewrite the
  table.
- Query helper to read the override for an instance.

**`backend/app/config.py`**
- Add `idle.timeout_max_seconds` to `IdleSettings`, default `14400` (4 hours).
- Add `idle.timeout_min_seconds`, default `300`.

**`backend/app/orchestrator.py`**
- `launch()` accepts an optional `idle_timeout_seconds`.
- **Clamp it in the orchestrator, not the caller**, to
  `[idle.timeout_min_seconds, idle.timeout_max_seconds]`. This is the whole
  point: an agent must not be able to pass `idle_timeout_seconds=9999999` and
  neutralise the spend guard. Clamping (not rejecting) keeps the launch path
  forgiving; record the clamp in the audit log when it fires.
- Persist the clamped value on the launch row.

**`backend/app/dispatcher.py`**
- `_check_idle()` resolves the effective timeout per instance: row override if
  set, else `settings.idle.timeout_seconds`.
- `idle_status()` returns the effective `timeout_seconds` so the instance card
  countdown is correct without any dashboard math.

**`backend/app/main.py`**
- `LaunchRequest` gains optional `idle_timeout_seconds`.
- New `POST /instances/{instance_id}/idle-timeout` to change it on a running
  instance, mirroring the existing `keep-alive` route. Audit it.

**`backend/app/mcp_server.py`**
- `launch_gpu` gains the optional parameter. HTTP-only, no logic; the
  AST check must still pass.
- Mention the clamp in the `get_skill` playbook so an agent knows the ceiling
  exists rather than discovering it by surprise.

**`dashboard/components/InstanceCard.tsx`**
- Show the effective timeout next to the countdown.
- Small control to change it, next to the existing Keep alive switch.

**`dashboard/components/LaunchForm.tsx`**
- Optional field, default blank meaning "use the default".

### Tests (`backend/tests/test_idle_timeout.py`, plus additions)

1. Override persists through launch and drives termination at the override, not
   the default.
2. NULL override falls back to the configured default.
3. Above-max clamps to max; below-min clamps to min; both write an audit row.
4. MCP `launch_gpu` with an absurd value is clamped identically. **This is the
   regression test that matters** — it is the agent-cannot-escape-the-guard case.
5. `idle_status()` reports the effective timeout.
6. `keep_alive` still wins over any override (off means off).
7. Existing `test_config_migrations.py` extended: a DB created before this
   phase opens and runs clean.

### DECISIONS.md entry

Why clamp rather than reject; why the ceiling is 4 hours; why the override
lives on the launch row rather than in preferences (it is per-instance state,
not a user policy).

### Gate

`uv run pytest -q` green, `npm run build` clean, and a manual mock-mode run
showing an instance terminate on a 300s override while the global default is
1800s.

---

## Phase 69: one-click IDE attach

### Why

There is already an "Open in terminal" button on serve jobs. The gap between
"I rented a GPU" and "I am working" is still an IDE. dstack emits a
`vscode://vscode-remote/ssh-remote+<host>/<path>` URL that opens VS Code or
Cursor already connected. We have managed SSH and host keys, so most of the
work is already done.

This is compliant with the hard rule that nothing on the instance listens on a
non-loopback interface: VS Code Remote-SSH installs a server on the instance
that binds loopback and tunnels over the SSH connection. **Verify this
explicitly during the phase and write the finding into DECISIONS.md.** If it
turns out otherwise, the feature does not ship.

### The two real problems

Solve these before writing UI code. They are the phase.

**Problem 1: VS Code opens its own SSH connection, outside the managed one.**
The backend supervises an asyncssh connection; VS Code will dial the instance
directly using the same private key. That is not a guard bypass (anyone holding
the key can already do this, and the spend guards live around the instance
lifecycle rather than the shell), but it does mean a second connection path
exists that the backend does not own. Decide and document: we accept this
because the alternative is proxying an IDE protocol we do not control, and the
lifecycle guards (budget, idle, terminate) are unaffected.

**Problem 2 — the one that will bite: an IDE session is invisible to the idle
tracker.** `_check_idle()` counts running jobs and terminal activity. A user
editing in VS Code for two hours registers as idle and the instance is
auto-terminated underneath them, mid-edit. That is precisely the failure this
product exists to prevent, so shipping the button without fixing it would be
worse than not shipping it.

Fix, in order of preference:

1. **Preferred:** extend the sidecar (`sidecar/manifold_sidecar.py`) with a
   cheap check for an active remote IDE server process (a `~/.vscode-server`
   or `~/.cursor-server` process, or an established sshd session that is not
   ours). The dispatcher's telemetry sampling already polls the sidecar, so
   fold the answer into that call and `touch_activity()` when it is true. No
   new polling loop.
2. **Fallback if (1) proves unreliable:** attaching sets `keep_alive` on, and
   the UI says so plainly ("Idle auto-termination is off while an IDE is
   attached — remember to terminate"). Weaker, because it trades a data-loss
   risk for a spend risk, so only take it with the warning in the UI and a
   DECISIONS entry saying why.

Do not ship option 2 silently.

### Changes

**`backend/app/connections.py`**
- Expose the connection target (host/IP, username, key path) for an instance.
  Read-only accessor; no new dial logic.

**New `backend/app/ide_attach.py`**
- Pure functions, following the `data_safety.py` pattern: no I/O, so it is
  trivially testable.
- Generate a Manifold-managed block for `~/.ssh/config` with a stable alias per
  instance (`manifold-<instance_id>`), `HostName`, `User`, `IdentityFile`,
  and the pinned host key. Delimit it with
  `# >>> manifold managed >>>` / `# <<< manifold managed <<<` so it can be
  rewritten idempotently without touching anything the user wrote.
- Build the `vscode://` and `cursor://` URLs from the alias.
- Reuse `host_keys.json` for `StrictHostKeyChecking` rather than disabling it.
  Do not weaken host key verification for convenience.

**`backend/app/main.py`**
- `POST /instances/{instance_id}/ide-attach` → writes the managed SSH config
  block, returns `{vscode_url, cursor_url, ssh_alias, ssh_command}`. Audit it.
- Remove the block on terminate so stale aliases do not accumulate.

**`dashboard/components/InstanceCard.tsx`**
- "Open in VS Code" / "Open in Cursor" buttons next to "Open in terminal".
- Show the raw `ssh manifold-<id>` command too, for people who use neither.

**`backend/app/mcp_server.py`**
- Optional: `get_ide_attach` so an agent can hand its human a working link.
  Only if it falls out cleanly; not worth stretching for.

### Tests (`backend/tests/test_ide_attach.py`)

1. Config block generation is idempotent: writing twice yields one block.
2. Hand-written user content above and below the delimiters survives a rewrite.
3. Terminate removes the block; other instances' blocks survive.
4. URLs are well-formed for both editors, and the alias is shell-safe.
5. Host key from `host_keys.json` is pinned in the generated block.
6. **Idle interaction:** with a simulated IDE session present, the idle loop
   does not terminate the instance. With it absent, it does. This is the test
   that justifies the phase.
7. Attach on a non-active instance is refused with a clear error.

### DECISIONS.md entries

- Why a managed `~/.ssh/config` block rather than passing `user@ip` directly in
  the URL (host key pinning, and VS Code's own reconnect behaviour).
- The second-connection-path acceptance from Problem 1.
- Whichever idle-detection route was taken, and what was measured.

### Gate

Full suite green, `npm run build` clean, and a manual mock-mode demonstration:
launch, attach, confirm the config block, confirm the idle loop leaves an
"attached" instance alone, terminate, confirm the block is gone.

---

## Sequencing note

Phase 68 first. Phase 69's idle-detection work reads the effective timeout that
68 introduces, and doing them in the other order means touching `_check_idle()`
twice.

---

# The framing behind the next phases

SkyPilot and dstack expose their power through YAML and CLI: you declare intent
in a config file and read outcomes in a log stream. That is the layer a novice
cannot cross. Manifold's bet is that the same capabilities, surfaced as legible
visual state instead of YAML and logs, let someone learn infrastructure by
doing it safely.

So for each borrowed capability the question is not "can we do it" but "what is
the one view that turns this from opaque to understandable." That test is what
Phases 70+ are graded against.

---

## Phase 70: the config Rosetta Stone

Buildable now. No provider dependency. Highest value per unit of work of
anything currently on the roadmap.

### Why

Job templates already turn a YAML spec into a friendly form. Right now the YAML
is hidden, which makes the dashboard a ceiling: a user who outgrows the form
has nowhere to go but the raw template editor, with no bridge between the two.

Making the generated config visible turns the form into a teaching surface. The
user fills in fields, watches the real declarative spec assemble itself live,
and gradually learns to read it. The dashboard becomes a ramp rather than a
ceiling, and it answers the "this just hides the real tool" objection directly.

### Changes

**`dashboard/components/ParameterForm.tsx`**
- Two-pane layout: form on the left, generated config on the right.
- The config pane updates live on every keystroke and highlights the block
  corresponding to the field currently focused. That highlight is the actual
  teaching mechanism; without it this is just a preview pane.
- Collapsible on narrow viewports. The form must stay usable alone.

**`backend/app/templates.py` / `backend/app/main.py`**
- Endpoint that renders a template plus parameter values into the final config
  **without running it** (`POST /templates/{name}/render`).
- Render server-side, not in the dashboard. The dashboard must not reimplement
  template substitution, or the two will drift and the pane will start lying.
  This is the same thin-client rule as everywhere else.
- Mount rules and quoting are already enforced at load in `templates.py`;
  render must apply the identical path so the preview matches reality exactly.

**Progressive disclosure**
- "Edit as config" switch that promotes the rendered result into the existing
  `TemplateEditor` as a starting point, so a user can graduate mid-task.
- Round-trip is one-way by design: form to config, not back. Two-way binding on
  a hand-edited config is a large problem and not worth it here. Say so in the
  UI ("switching to config editing keeps your values but leaves the form").

### Tests

1. Render endpoint output is byte-identical to what dispatch would execute for
   the same template and parameters. **This is the test that matters** — if it
   can drift, the pane teaches something false.
2. Quoting: a parameter containing spaces, quotes, and a newline renders safely
   (extends `test_template_quoting.py`).
3. Mount rules rejected at load are equally rejected at render.
4. Render never dispatches, never touches an instance, never spends.
5. Rendering an unknown template, or one with missing required parameters,
   returns a clear error rather than a partial config.

### DECISIONS.md entry

Why render server-side; why the form-to-config direction is one-way; what the
highlight-on-focus mapping is meant to teach.

### Gate

Suite green, `npm run build` clean, and a mock-mode walkthrough: fill the
vllm-serve form, watch the config assemble, switch to config editing, run it.

---

## Phase 71 (groundwork only): structured lifecycle events

Small, boring, and it must land before the visual work in the deferred section
below is possible. Do it now while it is cheap.

### Why

The resilience timeline and the cost-over-time views described below can only
render events that were recorded when they happened. Today a job's history is
spread across `tasks` columns and the audit log, which is fine for auditing and
useless for drawing a timeline. If this is not recorded now, building the view
later requires a backfill that cannot recover data that was never written.

### Changes

**`backend/app/db.py`**
- New `task_events` table: `task_id`, `at`, `kind`, `detail` (JSON),
  `instance_id`, `cost_cents_at_event`.
- `kind` starts as: `queued`, `launched`, `started`, `checkpointed`,
  `interrupted`, `resumed`, `synced`, `finished`, `failed`. Define the full
  vocabulary now even though only some fire today; adding members later is
  fine, renaming them is not.
- Purely additive. Nothing reads it yet.

**`backend/app/dispatcher.py`**
- Write an event at each existing transition. No behaviour change.

### Tests

A completed mock job produces the expected ordered event sequence, with
monotonically non-decreasing timestamps and accumulating cost.

---

## Deferred: specs captured, do not build yet

These are blocked on the multi-provider work. The specs are recorded here so
the design is settled when the dependency clears, and so Phase 71 records the
right data in the meantime.

### Spot with auto-recovery, as a resilience timeline

Blocked: Lambda has no spot tier. Unblocks when GCP or EC2 lands.

Capability: run on interruptible instances; on reclaim, checkpoint, relaunch
elsewhere, resume.

The view: a horizontal timeline on the job card reading "ran 40 min on A100 →
preempted → checkpointed → relaunched → resumed → done", with cost accumulating
in one place and an explicit "your progress was saved here" marker at each
interruption. The point is emotional as much as informational: preemption
should read as routine and survivable rather than catastrophic. Competing tools
log preemption; none of them draw it.

Depends on: Phase 71 events, and the existing `sync_outputs` / data-safety path
for the checkpoint guarantee.

### Cross-provider comparison, as a pick-for-me panel

Blocked: requires a second provider.

The view: before launch, a comparison card — "for this job (needs ≥24GB VRAM):
provider A $1.19/hr available · provider B $1.29/hr out of stock · provider C
$0.88/hr spot, interruptible" — with the recommended row highlighted and a
one-line reason ("cheapest available that meets your VRAM need"). Showing the
reasoning is the feature; a bare price table teaches nothing. The user learns
the decision variables by watching the system weigh them.

Depends on: the provider adapter interface, and the existing `estimates.py`
pre-launch cost functions.

### Multi-node: recommend holding

Recorded for completeness, with a recommendation against building it soon.

The "make failures legible, not make it one-click" framing is the right posture
if it is ever built: show N nodes with live per-node status and an explicit
"node 3 unreachable, the run is waiting on a collective" instead of a frozen
terminal.

Reasons to hold: it serves funded teams rather than the single user Manifold is
positioned for; the surface is large (interconnect, per-node telemetry,
collective stall detection, rescue-on-terminate across N hosts); and it cannot
be verified without five figures of real spend, which breaks the no-live-spend
rule at the phase gate. Revisit only if real users ask for it.
