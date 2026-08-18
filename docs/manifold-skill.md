# Manifold skill: how an AI agent drives GPUs through Manifold

You are working on a machine that runs Manifold, a local orchestrator for
Lambda Cloud GPU instances. This document teaches you to use it well. Read
it once at the start of a session; it is short on purpose.

## The one rule that matters

Go THROUGH Manifold, never around it. Do not call the Lambda API with curl,
do not launch instances from the Lambda console, do not open your own raw
SSH sessions for long-running work. Everything you do through Manifold gets:

- budget and concurrency guards (you cannot accidentally burn money)
- an audit trail the user reviews (raw API calls are invisible to them)
- a supervised SSH connection with auto-reconnect
- data rescue on termination (files are saved before anything is destroyed)
- job supervision that survives backend restarts and dropped connections
- GPU telemetry, cost tracking, and idle protection

Work done around Manifold has none of that. Real failure from a real
session: an agent drove the raw Lambda API, its instance looked orphaned,
its own retry harness silently terminated two boots mid-setup, and hours
were lost. Every one of those failures is impossible through Manifold.

## You are probably not the only agent on this account

Several agents and projects may share this Manifold. The rules that keep
that safe, each learned from a real incident:

- **Pass `purpose` on every launch.** It is required, and it is what every
  other agent sees in `list_instances`. A box with no purpose reads as
  unexplained, and an unexplained box once got terminated by an agent
  trying to be helpful - it was another project's model, mid-load.
- **Never terminate an instance you did not launch.** `list_instances`
  shows `created_by` and `purpose` for every box; termination refuses
  another principal's instance unless you pass `confirm_owner`, and the
  right move is to ask the human instead.
- **A box that looks idle is the case to be MOST careful about.** Read
  `activity` in `list_instances` before concluding anything: `state`
  (loading | serving | gpu_busy | detached_running | booting | ...) and
  `busy` carry the idle sweep's own verdict, in words. `busy: null` means
  "could not tell" - never treat it as false. A model loading weights has
  no visible processes, writes nothing, and is sixty seconds from serving.

## How to connect

MCP (preferred): the `manifold` MCP server exposes every tool named below.
If it is not configured yet, ask the user to run ONE of these, then start a
new session:

    # installed desktop app
    claude mcp add manifold --scope user -- "/Applications/Manifold.app/Contents/MacOS/manifold-backend" --mcp

    # dev checkout
    claude mcp add manifold --scope user -- uv run --directory <repo>/backend manifold-mcp

`--scope user` matters: the default scope registers Manifold for sessions
started in one directory only, so from anywhere else it looks like it was
never installed. That exact gap once cost a session, with a GPU billing
throughout.

If you cannot tell whether Manifold is installed at all, look for
`~/.config/manifold/manifold.json` before concluding it is missing: the
backend writes it on every boot with the API URL and the registration
command for this machine. `manifold-backend --doctor` (or `uv run
manifold-doctor` in a dev checkout) reports what is wired and what is not.

Plain HTTP: the same operations exist on http://localhost:8000 (the
desktop app or a dev backend must be running). GET /health confirms it.

## Mental model, 60 seconds

- **Instance**: a rented GPU box. Costs money every hour it exists.
  Manifold maintains one managed SSH connection to each.
- **Persistent filesystem**: NFS storage that survives termination. It is
  region-locked: an instance can only mount a filesystem in its own region.
  Anything not on it (home dir, /workspace/ephemeral) dies with the box.
- **Job**: a Docker container run from a template (vllm-serve,
  whisper-batch, axolotl-finetune, sdxl-generate, script-run,
  llm-synthesize, gpu-smoke, ...). Jobs stream logs, record exit codes,
  and survive backend restarts. Long-running work belongs in a job, not
  in an SSH command.
- **Auto-manage**: a job mode where Manifold rents a GPU just for the job:
  launch, run, sync outputs, terminate, all automatic.
- **Guards**: max hourly spend and max concurrent instances live in the
  backend. If a launch is rejected, tell the user what the guard said; do
  not look for a way around it.
- **Idle protection**: instances with no visible activity for 30 minutes
  are terminated (after data rescue) unless keep-alive is on. "Visible"
  is broad: jobs, terminal traffic, the proxy, a GPU above ~10%
  utilization in the window, or a running detached command all count as
  activity. What it cannot see is CPU/IO-only work started outside
  Manifold - for that, `set_keep_alive` or a per-launch idle timeout.
  Externally launched boxes that Manifold adopted default to keep-alive.
- **Two ceilings**: `max_lifetime_seconds` (from launch acceptance, boot
  included - the absolute outer bound) and `max_active_seconds` (from
  health-check pass - budget the run you control; boot never spends it).
  Either firing terminates with rescue first.

## Recipes

### Launch a GPU

1. `list_launch_options` FIRST. It returns only {type, region, filesystem}
   combinations with capacity right now, ranked best first (co-located
   with existing data beats empty beats scratch). Never guess a region.
2. `launch_gpu` with a target copied from that list, a `purpose`
   (required - say what the box is for), and optionally a `name` (the
   label humans see) and a ceiling sized to the run (`max_active_seconds`
   - boot does not spend it).
3. `wait_for_launch` with the returned launch id. One blocking call; do
   not poll in a loop. Boots take 2 to 10 minutes for PCIe cards and
   15 to 40 minutes for SXM/multi-GPU boxes. That is Lambda, not a hang.
4. Know that a box can legitimately GO AWAY AND COME BACK mid-setup: a
   driver upgrade reboots the instance, SSH drops, and both return on
   their own. A sequence that does not expect this will read a normal
   reboot as a dead box - the second most expensive misread on this
   platform after "idle". Wait for SSH to return before concluding
   anything.

### Serve a model (vLLM)

1. Check fit before paying the boot tax:
   GET /estimate/model-fit?model=<id>&instance_type=<type>.
   Rules of thumb: A10 24 GB serves up to ~14B 4-bit or ~7B fp16 with
   room to breathe. A 27B model, even 4-bit, wants an A100 40 GB.
2. `run_job` with template `vllm-serve` and the model id. The template
   handles CUDA, drivers, and loopback binding; do not hand-roll a venv
   or install drivers - and if the model needs tuning flags, pass
   `extra_args` (e.g. "--max-num-seqs 8 --gpu-memory-utilization 0.90").
   Flags come from an allowlist the template API publishes; anything
   outside it is refused with the list in the error. One inexpressible
   flag once forced a hand-rolled server that lost proxy routing,
   activity visibility, and log streaming in a single move. Tool calling / structured output works out of the
   box (the template passes --enable-auto-tool-choice with the hermes
   parser, which fits Qwen and Hermes models; set the tool_call_parser
   parameter to mistral or llama3_json for those families).
3. `get_job_status` until running, then poll readiness: the model needs
   minutes to download and load after the container starts.
4. Talk to it at http://localhost:8000/v1 (OpenAI-compatible proxy on the
   user's machine, riding the managed SSH tunnel). Never expose a port on
   the instance itself; nothing on an instance may listen non-loopback.
5. If no template can express your serve command, start the server by
   hand (loopback only) and `register_endpoint(instance_id, port,
   model_id)`. It becomes a first-class citizen: proxied, listed, and its
   traffic counts as activity. A hand-started server that is NOT
   registered is invisible to the proxy and the idle sweep alike - the
   most expensive misread this platform has had.

### Run batch work or custom code

- `run_job` with `script-run` for one-off scripts, or `save_template` to
  turn a proven workflow into a reusable recipe with parameters.
- Chain jobs with `depends_on`: pass earlier task ids and the job waits
  until ALL of them succeed, settling as `skipped` if any fails. No
  polling loop needed to sequence a pipeline; enqueue the whole chain up
  front. Servers cannot be parents (they never exit); to use a live
  server, just run the batch job - server and batch coexist per instance.
- `upload_file` puts local files on the instance (relative paths land on
  the persistent filesystem). `download_file` brings results back.
- Outputs you care about belong on the persistent filesystem. Check
  `sync_outputs` before terminating if anything lives in scratch. The
  rescue scope is `/workspace/ephemeral` - NOT the home directory. State
  in $HOME dies with the box, and "files_found: 0" means nothing in
  scope, not that nothing was lost.

### Train a model from scratch (the Foundry)

Three bundled recipes (see docs/train-your-own-model.md): `lerobot-act`
trains a robot policy from random weights on episodes under
`<filesystem>/datasets/<name>`; `smolvla-finetune` makes the same
episodes language-conditioned (public base, no token); `nanogpt-pretrain`
pretrains a small GPT from zero and samples from it into the job log.
Chain data-fetch and training with `depends_on`; suggest a short
`steps=2000` proof run before a full one - it validates the dataset for
cents. Trained artifacts land under `<filesystem>/outputs/<run_name>`;
`download_file` brings them home, and inference runs on the USER'S
hardware, never through the cloud (control loops need milliseconds).

### Fine-tune / distill

The pipeline, end to end (see docs/distill-your-own-model.md):
`vllm-serve` a teacher, `llm-synthesize` a dataset from it (set
`output_format=alpaca` and `holdout_pct=10`), `llm-judge` to score and
keep only the good rows, `axolotl-finetune` a student LoRA, `lora-merge`
to fold it into the base, `llm-eval` for a blind scorecard against the
held-out rows. All are templates on one instance. Chain the batch ones
up front with `depends_on`: each holds until its parent succeeds and is
skipped if it fails. The teacher is NOT a valid parent (a server never
exits); start it first and point the batch jobs at that box with
`target_instance_id` instead. `generate_training_config` asks a brain
for the axolotl YAML and validates it, but returns it for the user to
review: it saves nothing and starts nothing. Teacher/judge API keys ride
a `.env` on the persistent filesystem named in `env_file`, never a
parameter (parameters are logged verbatim).

### Run a long command (not a job)

`run_command` is capped at 50 seconds. For anything longer that does not
fit a template - an rsync, a compile, a bootstrap - use `run_detached`:
it returns immediately with a handle, records the exit code, and the
running pid COUNTS AS ACTIVITY, so the box protects itself from the idle
sweep with no polling and no keep-alive. `detached_status(handle)` gives
state and the log tail. Read the states literally: `vanished` means it
ended and how is unknowable; `unreachable` is a state of the connection,
not the command - retry rather than concluding it stopped. Do not
hand-roll `nohup ... &`; that dance is exactly what this replaces.

### Browse files

`list_persistent_files` works whenever an instance mounting the
filesystem is connected, no S3 keys needed. It rides the SSH connection.

### Clean up

`terminate_instance` with force=false. Manifold rescues unsaved files
first and refuses if something cannot be saved; that refusal is the
safety system working. Read the reply, fix what it says (usually
`sync_outputs`), and retry. Use force=true only when the user explicitly
accepts losing the listed files.

## Research keys: one vault, every agent

Third-party research API keys (YouTube, X, congress.gov, whatever the
user's pipelines call) live in one audited vault behind the backend, not
in per-agent dotfiles.

1. Need a key? `list_research_keys` first: names, presence, length,
   never values. Then `get_research_key(name, purpose)` for the one you
   need; purpose is required and lands in the audit log.
2. Given a key by the user? `set_research_key(name, value)` so the next
   agent (any CLI, any model) inherits it instead of asking again.
   Names are lowercase snake_case: `congress_gov`, not `Congress-GOV`.
3. Handle fetched values like the secrets they are: use them, never
   re-print them. Not into chat, not into files or code, not onto a
   command line that gets logged; pass them to subprocesses as
   environment variables.
4. The vault holds RESEARCH keys only. Manifold's own credentials and
   LLM provider keys have their own homes and are structurally
   unreachable through these tools; asking for them is a 404.

## Habits of a good Manifold agent

- Start with `get_work_log`: it lists what previous sessions (yours,
  other agents', local models') already accomplished - jobs, runs,
  costs, output locations - so you build on their work instead of
  redoing it.
- Pass a short `note` on every MCP call. It lands in the audit log the
  user reads; "probing why the sidecar is down" beats a blank.
- If the manifold tools vanish from your tool list mid-session, the MCP
  bridge process died (backend restarts do NOT cause this - the bridge
  reports "backend unreachable" instead). Ask the user to reconnect the
  server (/mcp in Claude Code) or restart the client.
- Before hunting dotfiles or asking the user for an API key, check
  `list_research_keys`; before letting a key die with your session,
  deposit it with `set_research_key`.
- Prefer `wait_for_launch` and job status over sleep-and-poll loops.
- Check `get_job_logs` before concluding anything about a failure; exit
  codes and the last 50 log lines usually name the real cause.
- If a readiness check you wrote can exit 0 on timeout, it will, and you
  will build on a server that is not there. Fail loudly instead.
- Costs are real. Say what an instance costs per hour when you launch it,
  and terminate what you are done with.
- Check `get_spend` before anything expensive, and again before you stop:
  it gives today / this week / month to date / all time plus the $/hour
  burning right now, which is also how you notice a box you forgot. Quote
  its two limits honestly - it counts only what Manifold launched (console
  instances and filesystem storage are outside it), and what it does count
  is an upper bound, since the clock starts when the cloud accepted the
  launch rather than when billing did. Costs it cannot know come back as
  `unresolved` (a range) or `rate_unknown`; report those as unknown, never
  as $0. `get_spend_breakdown` splits spend by principal or by purpose
  ("what did MY project cost" on a shared account), and the summary
  carries a `storage_estimate` block - filesystems bill per GB-month
  outside the launch totals, at a rate the user configures.
- A sub-minute backend restart (an app upgrade) is absorbed: refused
  connections retry for ~40s, and in-flight instance work is untouched -
  observed mid-boot and mid-transfer. A parked `wait_for_launch` that
  errors during one should be retried, not read as a failed launch.
- If results carry a `bridge_version_note`, your MCP bridge predates the
  backend: tools exist that you cannot see. Finish what is in flight,
  then restart your session to refresh the tool schema.
