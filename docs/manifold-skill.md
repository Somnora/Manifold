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

## How to connect

MCP (preferred): the `manifold` MCP server exposes every tool named below.
If it is not configured yet, ask the user to run:

    claude mcp add manifold -- uv run --directory <repo>/backend manifold-mcp

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
- **Idle protection**: instances with no Manifold-visible activity for 30
  minutes are terminated (after data rescue) unless keep-alive is on.
  Externally launched boxes that Manifold adopted default to keep-alive.

## Recipes

### Launch a GPU

1. `list_launch_options` FIRST. It returns only {type, region, filesystem}
   combinations with capacity right now, ranked best first (co-located
   with existing data beats empty beats scratch). Never guess a region.
2. `launch_gpu` with a target copied from that list.
3. `wait_for_launch` with the returned launch id. One blocking call; do
   not poll in a loop. Boots take 2 to 10 minutes for PCIe cards and
   15 to 40 minutes for SXM/multi-GPU boxes. That is Lambda, not a hang.

### Serve a model (vLLM)

1. Check fit before paying the boot tax:
   GET /estimate/model-fit?model=<id>&instance_type=<type>.
   Rules of thumb: A10 24 GB serves up to ~14B 4-bit or ~7B fp16 with
   room to breathe. A 27B model, even 4-bit, wants an A100 40 GB.
2. `run_job` with template `vllm-serve` and the model id. The template
   handles CUDA, drivers, and loopback binding; do not hand-roll a venv
   or install drivers. Tool calling / structured output works out of the
   box (the template passes --enable-auto-tool-choice with the hermes
   parser, which fits Qwen and Hermes models; set the tool_call_parser
   parameter to mistral or llama3_json for those families).
3. `get_job_status` until running, then poll readiness: the model needs
   minutes to download and load after the container starts.
4. Talk to it at http://localhost:8000/v1 (OpenAI-compatible proxy on the
   user's machine, riding the managed SSH tunnel). Never expose a port on
   the instance itself; nothing on an instance may listen non-loopback.

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
  `sync_outputs` before terminating if anything lives in scratch.

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

The proven pipeline, end to end (see docs/distill-your-own-model.md):
`vllm-serve` a teacher, `llm-synthesize` a dataset from it, then
`axolotl-finetune` a student LoRA. All three are templates; all three
run on the same instance sequentially. The finetune step can be
enqueued up front with `depends_on` on the synthesize task: it holds
until the dataset job succeeds and is skipped if it fails.

### Browse files

`list_persistent_files` works whenever an instance mounting the
filesystem is connected, no S3 keys needed. It rides the SSH connection.

### Clean up

`terminate_instance` with force=false. Manifold rescues unsaved files
first and refuses if something cannot be saved; that refusal is the
safety system working. Read the reply, fix what it says (usually
`sync_outputs`), and retry. Use force=true only when the user explicitly
accepts losing the listed files.

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
  as $0.
