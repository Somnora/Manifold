# Train your own model

Manifold's Foundry recipes turn "I want my own model" into a few jobs
with a visible price tag. Three bundled templates cover the three honest
meanings of that sentence:

| Template | What you get | Rough cost (A10, $0.75/hr) |
|---|---|---|
| `lerobot-act` | A robot policy trained FROM SCRATCH on your own demonstrations | $2-3 full run; ~$0.30 proof run |
| `smolvla-finetune` | The same episodes teaching a language-conditioned policy ("clear the desk") | $2-5 |
| `nanogpt-pretrain` | A GPT pretrained from zero that samples its own text into the job log | under $0.50 |

Costs are estimates from public community runs, not promises; Manifold's
estimator learns your real numbers after the first run, and every launch
still passes the budget, ceiling, and policy guards.

## The desk-robot walkthrough, end to end

The example everyone asks for: a stepper/servo arm that tidies a desk.
The model it needs is not an LLM - it is a small policy network learned
from YOUR demonstrations, and it trains from random weights in hours.

### 0. No robot? Start anyway

Use the public `pusht` dataset and the whole loop below works today,
arm optional. Fetch it onto the persistent filesystem with one
`script-run` job whose script does:

```python
# scripts/fetch_dataset.py  (upload via Browse, run with script-run)
import subprocess, sys
subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                "huggingface_hub[cli]"], check=True)
subprocess.run(["huggingface-cli", "download", "lerobot/pusht",
                "--repo-type", "dataset",
                "--local-dir", "/data/datasets/pusht"], check=True)
```

### 1. Record demonstrations (at your desk, no cloud involved)

Teleoperate the arm through the task ~50 times with LeRobot's recording
tools; each episode captures camera frames plus joint states. Reference
arms (SO-100/SO-101 class) work out of the box; a custom stepper build
needs its own small robot class - see LeRobot's custom-robot guide.
Manifold's scope starts once episodes exist.

### 2. Upload the episodes

Put the LeRobot-format dataset directory under
`<filesystem>/datasets/<name>` - the file navigator's upload, MCP
`upload_file` (relative paths land on the persistent filesystem), or the
fetch script above for community sets.

### 3. Train, with the meter visible

From the Jobs page (or MCP `run_job`): template `lerobot-act`,
`dataset=<name>`, auto-manage ON with a gpu_1x_a10. Manifold rents the
GPU, trains, syncs, and terminates without you. Two habits worth having:

- **Prove the loop first**: `steps=2000` costs ~$0.30 and confirms your
  dataset parses before you commit to the full 100k.
- **Chain, don't babysit**: enqueue the fetch job and the training job
  together with `depends_on` - the trainer holds until the data lands
  and is skipped (never half-run) if the fetch fails.

Checkpoints hit the persistent filesystem every `save_freq` steps, so an
idle-timeout or ceiling termination costs at most the steps since the
last save - the termination rescue covers the rest.

### 4. Bring the policy home

The trained policy is in `<filesystem>/outputs/<run_name>/` - download
it from the Files browser or MCP `download_file`. It is a few hundred
MB at most.

### 5. Run it ON the arm, not in the cloud

Inference runs at your desk: a control loop needs milliseconds, and a
cloud round-trip per step is unusable latency. ACT and SmolVLA policies
run on a laptop GPU or even CPU with LeRobot's local eval/record
tooling. Manifold's job was the training loop and the artifact; the loop
closes at home.

### 6. Iterate

More episodes -> better policy. Upload the new batch and enqueue the
next run chained after the upload job. Your job history becomes the
experiment log: every run's cost, duration, and outputs, attributed to
whoever launched it.

## Language-conditioned control: `smolvla-finetune`

Same dataset, one change: the template fine-tunes the public
`lerobot/smolvla_base` (~450M) instead of training from scratch, so the
policy conditions on instructions. No HF token needed - the base is
public, and it caches on the persistent filesystem after the first run.

## A GPT from zero: `nanogpt-pretrain`

The purist's from-scratch: the default run pretrains a char-level GPT on
Shakespeare in minutes and then SAMPLES from it, so the job log ends
with text your own model generated. Scale knobs are deliberately absent
from the template: serious pretrains (larger configs, multi-GPU boxes,
tens of dollars) belong in a `script-run` recipe you control, because a
bundled template's default should be the cheap path. The nanoGPT clone
rides master; pin your own fork for reproducibility if it matters.

## Version honesty

The LeRobot image tag floats and its CLI moves; Manifold's loader warns
about the drift, and the exact flags in the bundled templates are
re-verified against the image at each real-run gate. If a run fails on a
flag rename, the job log says so in the first lines, the failed-job card
shows it, and the fix is a one-line template edit on the Jobs page.
