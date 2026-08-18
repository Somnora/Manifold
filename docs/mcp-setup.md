# Driving Manifold from an AI agent (MCP setup)

The MCP server lets any MCP-capable client — Claude Desktop, Claude Code,
or anything else that speaks the protocol — launch GPUs, run jobs, browse
storage, and shut instances down. Every tool call flows through the same
guarded backend as the dashboard: the budget cap, the concurrency limit,
the region check, and the termination safety hook all apply identically.
An agent cannot spend what you have not permitted.

## Prerequisites

The backend must be running (the desktop app, or in a dev checkout
`uv run uvicorn app.main:create_default_app --factory` from `backend/`,
or mock mode with `MANIFOLD_MOCK=1`). The MCP server is a thin bridge to
it; if the backend is down, every tool returns a clear "backend
unreachable" error.

## Authentication (MANIFOLD_API_TOKEN)

A real-mode backend requires its API token on every request. The bridge
sends it when the `MANIFOLD_API_TOKEN` env var is set; without it, every
tool against a real backend returns a 401 naming the `.env` to copy it
from.

- **Installed app** (`manifold-backend --mcp`): nothing to do. The bridge
  reads the token from the app's own `.env` (the same file the backend
  generated it into), so the configs below work as-is.
- **Dev checkout** (`uv run manifold-mcp`): the bridge does not load
  `.env` itself. Add the token to the MCP config's env block, copied from
  the repo root `.env`:

```json
"env": {"MANIFOLD_API_TOKEN": "<the value from .env>"}
```

**Better: give the agent its own token.** On the Settings page (API
access), mint a principal named for the agent, e.g. `claude-mcp`, and put
THAT token in the config instead of the owner token. Every launch, job,
and audit row the agent causes then carries its name instead of yours,
and revoking the agent's access is one click that does not touch your own
credential.

Mint it with role `operator` (the default): the agent can launch, run
jobs, and manage files, but cannot read or write secrets, change policy,
or mint more tokens. A monitoring-only agent gets `viewer` and can
observe everything while spending nothing.

Mock mode enforces nothing, so the variable is optional there. If you
rotate the token in `.env`, update it in any MCP config that carries it.

## From the installed desktop app (no dev checkout)

The app's bundled backend binary doubles as the MCP server: run it with
`--mcp` and it speaks MCP on stdio, bridging to the running app. On macOS
the binary lives inside the app bundle, so registering in Claude Code is
one command:

```bash
claude mcp add manifold --scope user -- "/Applications/Manifold.app/Contents/MacOS/manifold-backend" --mcp
```

`--scope user` matters: without it, Claude Code registers the server for
sessions started in the **current directory only**, which from every
other repo is indistinguishable from "not installed". That exact
confusion once cost a working session (an agent was told "Manifold is
open for you to use", found no manifold entry in its own registry, and
lost the session to filesystem archaeology while the app ran the whole
time). For a machine-wide tool like Manifold, user scope is the right
default; drop it only if you deliberately want per-project registration.

Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "manifold": {
      "command": "/Applications/Manifold.app/Contents/MacOS/manifold-backend",
      "args": ["--mcp"]
    }
  }
}
```

Keep the Manifold app running: the bridge talks to it on
localhost:8000 (or MANIFOLD_PORT if you changed it; set the same value in
the MCP server's env). Everything below about dev-checkout registration
still works and behaves identically - it is the same bridge.

## Registering in Claude Code

From the repo root:

```bash
claude mcp add manifold --scope user -- uv run --directory "$(pwd)/backend" manifold-mcp
```

Then in any Claude Code session — any directory, thanks to `--scope
user` (see the note above) — "launch a 1x A10 in us-east-1 with the
manifold-data filesystem" and watch it use the tools.

## Registering in Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(create the file if it does not exist), with YOUR absolute repo path:

```json
{
  "mcpServers": {
    "manifold": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/Manifold/backend", "manifold-mcp"]
    }
  }
}
```

Restart Claude Desktop; the tools appear under the hammer icon.

If the backend runs somewhere non-default, set the env var in the same
block: `"env": {"MANIFOLD_API_URL": "http://localhost:8000"}`.

## Registering in Codex

Add to `~/.codex/config.toml`, with YOUR absolute repo path:

```toml
[mcp_servers.manifold]
command = "uv"
args = ["run", "--directory", "/Users/you/Manifold/backend", "manifold-mcp"]
```

Then in any codex session: "use the manifold tools to launch an A10 and
run gpu-smoke". Tell it once per task: **use the manifold tools, not ssh**
- that is what keeps every action on the audit trail.

## Registering in Gemini CLI

Add to `~/.gemini/settings.json` (create it if needed):

```json
{
  "mcpServers": {
    "manifold": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/you/Manifold/backend", "manifold-mcp"]
    }
  }
}
```

`/mcp` inside gemini lists the tools once it connects.

## Is it actually connected? (doctor + breadcrumb)

"Manifold is installed" and "Manifold is connected to this agent
session" are different states. Two things make the difference visible:

**The doctor.** One command verifies the whole chain and exits nonzero
when an agent would be blocked:

```bash
"/Applications/Manifold.app/Contents/MacOS/manifold-backend" --doctor   # installed app
uv run manifold-doctor                                                  # dev checkout, from backend/
```

It reports: is a backend answering (mock or real)? does a token exist
and does the backend accept it (presence and status only — the value is
never printed)? which agent configs register manifold (Claude Code
including per-directory scopes, Claude Desktop, Codex, Gemini CLI), and
at what scope? what is running right now? Each FAIL line carries the
exact command that fixes it.

**The breadcrumb.** On every boot the backend writes
`~/.config/manifold/manifold.json` (all platforms — that is where agents
probe): what Manifold is, where the API answers, the health-check curl,
and the one-line register + doctor commands. An agent that has never
heard of Manifold finds it there in seconds. No secrets, ever;
`MANIFOLD_NO_BREADCRUMB=1` opts out.

The dashboard shows the same truth: until the first MCP call ever
reaches the backend, the header carries a "no agent connected" chip
linking to Settings → Connect an agent, which holds the copy-able
registration commands and the live last-call status.

## Troubleshooting: the client says the server timed out

Claude Desktop shows it as a red toast: *"MCP manifold: Couldn't start
this server ... Request timed out"*. Other clients word it differently,
but it always means one thing: the client spawned the bridge, waited for
its `initialize` reply until its own deadline, gave up, and killed the
process. It does not mean the backend is down and it does not mean the
registration is wrong. Every other check passes straight through this
failure, which is why the doctor learned to reproduce the handshake
itself.

Run the self-test. It spawns exactly the command your client is
configured to spawn, speaks real JSON-RPC over the same pipes, and times
each phase:

```bash
"/Applications/Manifold.app/Contents/MacOS/manifold-backend" --doctor --handshake   # installed app
uv run manifold-doctor --handshake                                                  # dev checkout, from backend/
```

A healthy machine answers in a couple of seconds:

```
manifold doctor: MCP handshake self-test (deadline 15s per spawn)
  --    probing the command a client spawns: /Applications/Manifold.app/Contents/MacOS/manifold-backend --mcp
  OK    mcp handshake: initialize 1180ms, tools/list 240ms (46 tools)
  OK    mcp handshake, 2 clients at once: initialize 1240ms and 1310ms (46 tools each)
  clean: a client that spawns this command gets the tools.
```

The tool count comes from the server's own `tools/list` reply, so it is
the number your client will see. Two spawns at once is not paranoia:
Claude Desktop starts a main copy of the server and a shared-pool copy
within a couple of seconds of each other, and that pair is the case that
timed out. Any FAIL exits nonzero, so the command works in a script.
`--doctor` on its own runs this check last, after the wiring checks;
`--doctor --no-handshake` skips it when you want the fast answer.

**If the first run is slow and the next one is fast, that is the
unsigned binary, not Manifold.** The app ships as a single unsigned
one-file build of tens of megabytes: every spawn unpacks itself into a
temporary directory, and macOS assesses that fresh copy before letting
it run. The assessment is per extraction, so a second run right after is
normally quick, and keeping the Manifold app open keeps the system warm
(the app and the bridge are the same binary).

If it stays slow, or the self-test FAILs outright, re-register the
server and restart the client.

Claude Code:

```bash
claude mcp add manifold --scope user -- "/Applications/Manifold.app/Contents/MacOS/manifold-backend" --mcp
```

Claude Desktop, in `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "manifold": {
      "command": "/Applications/Manifold.app/Contents/MacOS/manifold-backend",
      "args": ["--mcp"]
    }
  }
}
```

Then quit Claude Desktop completely (Cmd-Q, not just closing the window)
and reopen it: it only reads that file at startup.

Codex, in `~/.codex/config.toml`:

```toml
[mcp_servers.manifold]
command = "/Applications/Manifold.app/Contents/MacOS/manifold-backend"
args = ["--mcp"]
```

Cursor and other clients take the same two fields (command plus
`["--mcp"]`) in whatever config file they use; if the self-test passes
and that client still times out, the deadline belongs to the client and
its logs are the next place to look.

## The tools

| Tool | What it does |
| --- | --- |
| `list_launch_options(provider?)` | Ranked {type, region, filesystem} targets that have capacity NOW, co-located with your data first - call before `launch_gpu`. Targets come from the account's default cloud and every row names it; pass `provider` only to look at another one |
| `launch_gpu(instance_type, region, filesystem, purpose, name?, max_active_seconds?, provider?, extra_filesystems?, ...)` | Launch through ALL guards; `purpose` is required (it is what other agents see); returns a launch id. Leave `provider` empty to use the account's default cloud; `extra_filesystems` mounts up to 4 more same-region filesystems alongside the primary, for your own commands and file access (jobs still use the primary) |
| `list_launch_options()` | Ranked {type, region, filesystem} targets that have capacity NOW, co-located with your data first — call before `launch_gpu` |
| `launch_gpu(instance_type, region, filesystem, purpose, name?, max_active_seconds?, bootstrap?, ...)` | Launch through ALL guards; `purpose` is required (it is what other agents see); returns a launch id. `bootstrap` is a bash script the box runs ONCE when it comes up (the clone, the install, the model pull), started detached so it survives a backend restart and its exit code is recorded; a nonzero exit is reported and never terminates the box. It counts as activity while it runs, so a bootstrap that HANGS holds the box as busy for as long as it hangs and the idle timeout will never fire: `max_active_seconds` / `max_lifetime_seconds` are the ceilings that do stop it |
| `get_launch_status(launch_id)` | One snapshot: phase + boot countdown while it boots |
| `wait_for_launch(launch_id, timeout=120)` | Block until active/failed instead of polling (best for slow SXM4 boots) |
| `list_instances()` | Live instances: SSH state, `created_by`, `purpose`, the idle sweep's `activity` verdict, and the last GPU telemetry sample |
| `get_spend()` | What the launches have cost (today / week / month to date / all time) + the current $/hour burn — call it before an expensive launch so the agent can limit itself |
| `terminate_instance(id, force=false, confirm_owner?)` | force=false returns the unsaved-file list instead of terminating; another principal's box is refused unless `confirm_owner` names them |
| `sync_outputs(instance_id)` | rsync ephemeral scratch → persistent filesystem |
| `list_templates()` | Job templates with parameter schemas |
| `run_job(template, parameters)` | Enqueue a job; validated immediately |
| `get_job_status(id)` / `get_job_logs(id, tail=100)` | Progress and live logs |
| `list_filesystems()` / `list_persistent_files(prefix)` | Persistent storage; browses over SSH (no S3 keys) when a box is up, else via the S3 Files API |
| `upload_file(local_path, remote_path)` | Push a file from this machine to the instance (SFTP) |
| `download_file(remote_path, local_path)` | Pull results back to this machine (SFTP) |
| `run_command(instance_id, command, timeout=45)` | ONE shell command, capped at 50s, audited with its exit code |
| `run_detached(instance_id, command, purpose)` / `detached_status(id, handle)` | Long work that outlives the call (and backend restarts); a running detached pid counts as activity, so the box protects itself from the idle sweep |
| `register_endpoint(instance_id, port, model_id)` / `deregister_endpoint(...)` | Adopt a hand-started model server into the OpenAI proxy: routed, listed, and its traffic counts as activity |
| `list_research_keys()` / `get_research_key(name, purpose)` / `set_research_key(name, value)` | The shared research-key vault: list is presence/length only, fetches are audited with a required purpose, deposits make a key every agent's key |
| `set_keep_alive(instance_id, enabled)` | Switch idle auto-termination off for one box (for long CPU/IO work started outside Manifold); the max-lifetime ceiling still applies |
| `get_spend_breakdown(by)` | Spend by `created_by`, `purpose`, type, region — "what did MY project cost" on a shared account |
| `save_template(yaml_text)` / `delete_template(name)` | Author a custom job template (see docs/custom-templates.md) |

Two honest limits on every number `get_spend` returns: it counts only
launches Manifold itself started (a box created in the Lambda console, and
filesystem storage, are outside it), and what it does count is an upper
bound, because the clock starts when the cloud accepted the launch rather
than when billing did. Costs it cannot know come back as `unresolved` (a
range) or `rate_unknown` — never folded into a total as $0.

Every tool takes an optional `note` — one line of intent that lands in the
audit log. Everything an agent does is visible live on the dashboard's
**Activity → Audit trail** page (filter: Agent actions), and any job it
queues appears on the Jobs page with streaming logs.

## Worked example

You say to the agent:

> Transcribe everything in /inbox with whisper-large, then shut down.

A well-behaved session looks like this (all of it visible on Agent
Activity):

1. `list_templates(note="find transcription template")` → sees
   `whisper-batch` with parameters `input_dir`, `model_size`, `language`.
2. `list_launch_options(note="where can I launch, near my data")` → the top
   target is `gpu_1x_a10` in `us-east-1` on `manifold-data` (co-located with
   the inbox, and available right now), so no region is guessed blind.
3. `launch_gpu(instance_type="gpu_1x_a10", region="us-east-1",
   filesystem="manifold-data", purpose="whisper batch for the interview set",
   note="GPU for whisper batch")` → launch id.
   If this had breached the budget or concurrency cap, the tool would have
   returned the guard's message and the agent would have to tell you no.
4. `get_launch_status(...)` polled until `active`.
5. `run_job("whisper-batch", {"input_dir": "inbox", "model_size":
   "large-v3"}, note="transcribe inbox")` → task id.
6. `get_job_status(...)` until `succeeded`; `get_job_logs(...)` to confirm;
   outputs recorded under `<filesystem>/transcripts`.
7. `terminate_instance(id, note="job done")` → **blocked**: the safety hook
   reports unsaved files in ephemeral scratch and the tool returns the list
   instead of terminating.
8. `sync_outputs(id, note="save outputs first")` →
   `terminate_instance(id, force=true, note="all synced")` → terminated.
   Billing stopped.

## Do agents get everything SSH would give them?

Yes — the difference is not capability, it is visibility.

- `run_command` is full shell parity: anything an agent could type over
  raw SSH, it can run through the tool. The difference is that every
  command lands in the audit log with its exit code, and activity resets
  the idle clock so the box is not reaped mid-task.
- Long-running work belongs in `run_job` (or a custom template): jobs
  stream logs to the dashboard, survive backend restarts, and record
  their outputs.
- What agents can NOT do through the tools: bypass a guard. Budget,
  concurrency, the mount jail, and the termination data rescue bind every
  tool identically. Raw SSH from your own terminal could sidestep the
  audit trail — which is exactly why the one instruction worth giving
  every agent is: **use the manifold tools, not ssh**.

## Non-MCP agents

Anything that can speak HTTP can use the backend directly — the API the
MCP tools wrap is plain REST on localhost:8000 (see `backend/app/main.py`).
The guards live in the backend, so they hold for those clients too.
