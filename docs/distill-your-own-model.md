# Distill your own model

Distillation is one idea: a big model that already knows how to do your task
writes the training data, and a small model learns from it. The big one is
the TEACHER, the small one is the STUDENT. What you end up with is a model
that is yours, does your one job well, and serves on a cheap GPU instead of
a rented frontier API.

The v2 loop is six jobs. Four of them are the ones a first-timer would build
anyway (serve, generate, train, merge). Two are the ones that decide whether
the result is real:

- **curation** (`llm-judge`): a second model scores every generated pair and
  throws the bad ones away. Raw synthetic data is mostly filler. This is the
  step beginners skip and it is usually the difference between a student that
  works and one that confidently makes things up.
- **the scorecard** (`llm-eval`): held-out prompts the student never trained
  on, answered by student and teacher, graded blind. Without it you have a
  loss curve and a feeling, not a result.

```
your raw data (JSONL/CSV on the filesystem)
   |
   v
llm-synthesize   holdout_pct=10   ->  synthesized/distill.jsonl
   |                                  synthesized/eval-distill.jsonl  (held out)
   v
llm-judge        threshold=7      ->  synthesized/kept-distill.jsonl
   |                                  synthesized/scored-distill.jsonl
   v
axolotl-finetune (config from the Training config panel)  ->  outputs/distill-v1/
   |
   v
lora-merge                        ->  models/distill-v1-merged/
   |
   v
llm-eval                          ->  outputs/scorecard.json
                                      "student matched or beat the teacher
                                       on 41/50 held-out tasks (82%)"
```

The teacher sits beside that chain rather than at the top of it. See
"Chaining it up front" below for why.

Everything runs through the same guarded gateway as every other job: budget
cap, concurrency limit, policy allowlists, lifetime ceiling, rescue on
termination, full audit.

## 0. What you need

- **Source data**: one JSONL or CSV file where each line or row is one
  example of the thing you care about (support tickets, shot lists,
  contracts, transcripts). Upload it with Browse on a connected instance, or
  MCP `upload_file`. Anything relative lands on the persistent filesystem.
- **A teacher you are allowed to learn from** (next section).
- **One GPU**. A 7B-class teacher and a 1.5B student both fit on a single
  A10 24GB, one after the other. A much larger teacher needs a much larger
  box, or an API.

## 1. Choosing a teacher: two rules that are not negotiable

**Licence.** Open-weights teachers (Qwen, Llama, Mistral) are the clean
path: their licences permit training on what they generate, subject to their
own terms. Frontier-API terms generally FORBID using the outputs to train a
competing model. Manifold labels this choice in the template descriptions
and then gets out of the way: it is your project and your call, and it is
not a technical limit, so nothing in the software will stop you.

Read the licence of the STUDENT too. The bundled student shelf says which
one each base carries, and the Qwen 3B is Qwen Research (non-commercial),
not Apache. A model distilled from a non-commercial base is not a
commercial product.

**Locality.** The teacher must be reachable FROM THE INSTANCE, not from your
laptop:

| Teacher | Reachable? |
|---|---|
| A `vllm-serve` job on the same instance | Yes, over the instance's own loopback |
| A public OpenAI-compatible API | Yes |
| Ollama or LM Studio on your laptop | **No**, and no setting can make it so |

That last row catches people. Manifold's own brain registry can talk to your
laptop's Ollama, because the BACKEND runs on your laptop. A job runs in a
container on a rented GPU in a datacenter, and your laptop is not on its
network.

### API keys, if the teacher is an API

Keys never go on a command line and never go in a job parameter: every
parameter is rendered into the docker command, and that command is written
verbatim into the job log. Instead, put a `.env` on the persistent
filesystem (upload it with Browse) and name it in the `env_file` parameter,
exactly as `script-run` has always done:

```
# research/.env  on the persistent filesystem
MANIFOLD_TEACHER_API_KEY=sk-...
MANIFOLD_JUDGE_API_KEY=sk-...
```

Then set `env_file: research/.env` (the path is relative to the filesystem
root). The file is sourced by the shell inside the container before Python
starts, so the key reaches the request headers and nothing else. Missing
file is a hard failure with the path named, before any work begins.
`OPENAI_API_KEY` works as a fallback for both. The file is sourced by a
shell, so it must be plain `KEY=value` lines, and anything else in it will
execute.

Also: `teacher_base_url` and `judge_base_url` are refused if they carry a
query string or a `user:pass@host`, because those end up in the job log too.

## 2. Serve the teacher (skip if it is an API)

Jobs -> `vllm-serve` -> pick a preset sized to your GPU. Wait until the chat
panel opens; that means the teacher is answering.

Leave this job RUNNING. It is a server, and servers do not finish. Every
other job in this guide is a batch job and coexists with it on the same
instance.

## 3. Generate the training set, with a holdout

Jobs -> `llm-synthesize`, with "Run on" set to the instance serving the
teacher:

- `input_path`: your raw file, e.g. `raw/shots.jsonl`
- `instruction`: the task you want the student to LEARN, written as if to a
  new employee. Example: "Tag this film shot. Reply with JSON only, keys:
  shot_size, camera_move, subject, mood."
- `output_format`: **`alpaca`**. This is the distillation switch. Rows come
  out as `{"instruction", "input", "output"}`, which axolotl trains on
  directly. The default `records` shape is for data-extraction jobs and
  cannot be trained on.
- `output_name`: `distill` (files land at `synthesized/distill.jsonl`)
- `holdout_pct`: **10**. One row in ten is held back into
  `synthesized/eval-distill.jsonl` instead of the training file, keeping the
  teacher's answer, so the scorecard in step 8 grades on prompts the student
  has never seen. Allowed range is 0 to 50; the job fails with a named error
  if either half comes out empty, because an empty training file or a 0/0
  scorecard is worse than no holdout at all.
- `limit`: `25` for the first run. Read the output in Browse, tighten the
  instruction, then re-run with `0` (all records).

Using an API teacher instead? Set `teacher_base_url` (e.g.
`https://api.example.com/v1`), `teacher_model`, and `env_file`. Left empty,
both fall back to the model this instance is serving, discovered from
`/v1/models`, which is exactly what the template did before v2.

Rough rate: about two seconds per record on a served 7B-class teacher. 1000
records is therefore roughly half an hour of teacher time. See "Costs" for
what that means in dollars.

## 4. Curate: the step everyone skips

Jobs -> `llm-judge`:

- `input_name`: `distill.jsonl` (a filename under `synthesized/`)
- `criteria`: what a GOOD pair looks like, in your words. Example: "The JSON
  parses, every key is present, shot_size is one of wide/medium/close, and
  the mood word is supported by something actually in the record."
- `threshold`: `7` (keep rows scoring 7 or better out of 10)
- `judge_base_url` / `judge_model` / `env_file`: same options as the teacher.

Two files land beside the input:

- `synthesized/scored-distill.jsonl`: every row plus `judge_score` and
  `judge_reason`. This is your evidence, and it is worth reading.
- `synthesized/kept-distill.jsonl`: the rows that cleared the threshold, in
  the SAME shape the input had, so the trainer reads it with no conversion.

The final log lines print a score histogram and `kept N of M rows`. Read the
histogram before you train:

- Everything scoring 9 or 10 usually means the judge is the same model that
  wrote the answers, and it is grading its own homework. Point `judge_model`
  at a different model if you can. The template warns about this and so does
  the scorecard.
- A flat spread means the criteria are vague. Rewrite them as a checklist,
  not an adjective.
- Nothing clearing the threshold fails the job on purpose, with the
  histogram above it: there is nothing to train on, and finding that out now
  is free.

Why bother at all: the student learns the teacher's mistakes just as
faithfully as its successes, and it has no capacity to spare on hedging.
Training on 800 good rows beats training on 1000 rows where 200 are wrong,
and it costs less.

Both files stay under `synthesized/` because that is the only dataset
directory `axolotl-finetune` mounts. Nothing needs moving between steps.

## 5. Write the training config (a brain writes it, you review it)

Jobs page -> the **Training config** panel. Describe what you want in plain
words, pick a brain, and read what comes back:

> distill film-shot tagging into a 3B LoRA that fits an A10, dataset
> kept-distill.jsonl

The backend sends that plus the fixed facts a model cannot know (the exact
container paths, the allowlist of settings, the shelf of student bases) to
whichever brain you chose, then CHECKS the answer before you ever see it.
The check is a security boundary, not a lint: `axolotl-finetune` executes
this file on the GPU box, so a config that sets `trust_remote_code`, points
at an unvetted repo, globs a whole directory, or writes outside the mounts
is refused with the offending key named. A brain that answers with prose
gets a 502 that quotes what it actually said.

Same thing from an agent: MCP `generate_training_config`.

**It is review only.** Manifold does not save the config and never starts a
training run from it. To use it: copy the YAML, save it locally, open Browse
on a connected instance, go to `configs/`, upload it there, then queue
`axolotl-finetune` pointing at that filename. Uploading needs a connected
instance, because the persistent filesystem is only reachable through one.

You can also just write the config yourself. A working starting point:

```yaml
base_model: Qwen/Qwen2.5-1.5B-Instruct   # from the student shelf, ungated
strict: false

datasets:
  - path: /data/synthesized/kept-distill.jsonl   # the CURATED file, not the raw one
    type: alpaca
dataset_prepared_path: /tmp/axolotl/prepared
val_set_size: 0.05
output_dir: /data/output/distill-v1

adapter: lora
lora_r: 16
lora_alpha: 32
lora_dropout: 0.05
lora_target_linear: true

sequence_len: 2048
micro_batch_size: 2
gradient_accumulation_steps: 8
num_epochs: 3
learning_rate: 0.0002
optimizer: adamw_torch
lr_scheduler: cosine
bf16: true
logging_steps: 5
save_strategy: epoch
```

**Never point `datasets[0].path` at a glob.** `/data/synthesized/*.jsonl`
would sweep in `eval-distill.jsonl`, train the student on its own exam, and
turn the scorecard into a lie you cannot detect. The generator refuses a
glob and refuses an `eval-` filename; a hand-written config is on you.

Path note: `axolotl-finetune` mounts `configs/` at `/data/config` (your
yaml), `synthesized/` at `/data/synthesized` read-only (everything
llm-synthesize and llm-judge wrote), and `outputs/` at `/data/output`. Those
four paths are the whole world the training job can see. It does NOT mount
`models/`, so a config whose `base_model` is a merged model on the
filesystem needs a custom template.

## 6. Train the student

Jobs -> `axolotl-finetune`:

- `config_path`: `distill-kept-distill.yaml` (whatever you named it in
  `configs/`)
- `output_dir`: `distill-v1`

Watch the loss in the job logs. The LoRA adapter lands in
`outputs/distill-v1/` on the persistent filesystem, so it survives the
instance dying.

## 7. Merge

Jobs -> `lora-merge`:

- `adapter_dir`: `distill-v1`
- `output_name`: `distill-v1-merged`

This folds the adapter into the base weights and writes a standalone
HuggingFace model to `models/distill-v1-merged/`. The base repo is read from
the adapter's own config, so you usually pass nothing else. From here you
can serve it (`vllm-serve` with `model_id: /data/models/distill-v1-merged`)
or download the folder and run it anywhere.

## 8. The scorecard

Jobs -> `llm-eval`:

- `eval_name`: `eval-distill.jsonl` (the holdout from step 3)
- `student_path`: `distill-v1-merged`
- `judge_base_url` / `judge_model` / `env_file`: your judge
- `output_name`: `scorecard`

For each held-out prompt it generates the student's answer, takes the
teacher's answer (already stored in the holdout file, so no teacher call is
needed and no tokens are paid for twice), and asks the judge to pick between
them without being told which is which. The student is placed at position A
on even item numbers and B on odd ones, so position bias cancels across the
run instead of deciding it.

Final log lines say the thing you actually wanted to know:

```
student won 38, tied 3, lost 9, unscored 0
student matched or beat the teacher on 41/50 held-out tasks (82%)
```

The full detail, including every answer pair and which position it took,
goes to `outputs/scorecard.json` along with `judge_model`, `teacher_model`,
and `judge_is_teacher`.

**What this number is not.** It is a preference score from one judge, not a
benchmark. Blind A/B cancels position bias and nothing else. Two biases
survive: a judge that IS the teacher flatters itself (the template prints a
loud warning and records the fact in the scorecard), and judges tend to
reward longer answers. Ties are counted separately rather than quietly
folded into wins. Treat 82% as "the student is in the same league", not as a
measurement.

### VRAM: read this before you queue llm-eval

`llm-eval` loads the student IN-PROCESS with transformers. It is
deliberately not a second server: one server per instance is a house rule,
and a scorecard run is a batch job, not a service.

That means it wants the card. vLLM takes 90% of the GPU by default, so on a
24GB A10 a live `vllm-serve` teacher is holding about 21.6GB, and loading a
3B student beside it is a guaranteed CUDA out-of-memory. Three ways through:

1. **Recommended: run it after the teacher is stopped.** Cancel the
   `vllm-serve` job from its job card (Manifold can stop a running server),
   then queue `llm-eval` with an API judge. The teacher's answers are
   already in the holdout file, so nothing is lost.
2. **Keep both judge and teacher on APIs.** Then nothing else wants the card
   and the student gets all of it.
3. **`student_device: cpu`.** Fits beside anything, and is slow. Good for a
   20-row smoke test, not a 500-row scorecard.

## Chaining it up front

The four batch jobs chain with "Run after" (`depends_on`), so you can
enqueue them all at once and walk away. A child holds until its parent
succeeds and is SKIPPED, never half-run, if the parent fails:

```
llm-synthesize  ->  llm-judge  ->  axolotl-finetune  ->  lora-merge
```

Two things the chain cannot include, and why:

- **The teacher server is not a parent.** Manifold refuses `depends_on` a
  server job with "a server that never exits on its own: 'after it
  succeeds' would mean never", and it is right. Start `vllm-serve` first,
  then bind the batch jobs to that box with the **Run on** picker instead.
  A server and a batch job coexist on one instance by design.
- **`llm-eval` only joins the chain if there is no local teacher.** Chained
  behind `lora-merge`, it would start while the teacher server was still
  holding the card (see the VRAM section). With an API teacher and an API
  judge there is no server at all, and all five chain cleanly.

## Costs, honestly

**Two numbers here are measured. The rest are arithmetic, and are marked
UNVERIFIED until a real distillation run seeds them.** Manifold's estimator
says so too: it reports no history rather than a guess, and starts learning
your real numbers after your first run.

Measured, published:

- An A10 24GB is **$1.29/hr**.
- A 2000-step from-scratch robot policy run on that A10 cost **$0.04**
  (2026-08-14 gate). Different workload, but it is the honest anchor for
  "small GPU jobs are cheap".

Rough shape of a distillation run, UNVERIFIED:

| Step | What drives it | Rough time |
|---|---|---|
| `llm-synthesize`, 1000 records | teacher speed, about 2s per record | ~35 min |
| `llm-judge`, 1000 rows | judge speed, replies are one line | ~20 min |
| `axolotl-finetune`, ~800 rows, 1.5B student, 3 epochs | the card | ~30 min |
| `lora-merge` | disk and download | a few minutes |
| `llm-eval`, 50 held-out items | student generation | ~10 min |

That is roughly 1.5 to 2 hours of instance time, so **roughly $2 to $3 on an
A10 at $1.29/hr**, plus a few one-time minutes for boot and image pulls.
Treat that as an order of magnitude, not a quote.

What actually moves the bill:

- **The meter is wall-clock instance time, not per job.** An instance
  sitting idle between your steps costs exactly as much as one working.
  Auto-manage, or the idle timeout, is how you stop paying for thinking
  time.
- **The teacher dominates.** A bigger teacher means a bigger box at a higher
  hourly rate, and slower generation per record. Check the launch options
  list for today's prices.
- **`limit: 25` first, always.** A bad instruction discovered after 1000
  records costs the whole generation twice.
- **Curation is not an extra cost, it is a discount.** Fewer, better rows
  train faster than more, worse ones.

## Honest caveats

- **Gated students (Llama, Gemma) will not pull**: Manifold does not pass a
  HuggingFace token yet. Every entry on the student shelf is ungated.
- **First axolotl run pulls a large image** (several GB). Expect the job to
  sit in image-pull for a few minutes before logs move. `llm-eval` reuses
  that same image on purpose, so it pulls once per instance.
- **`llm-synthesize` and `llm-judge` are stdlib-only** on a tiny Python
  image, so they start in seconds.
- **Quality is bounded by the teacher and your instruction.** Iterate on
  step 3 with `limit` before spending on the full set. The scorecard tells
  you where you landed, not how to get better; the instruction and the
  criteria are where the improvement lives.
- **The `llm-eval` template's transformers path has not been run on real
  hardware yet.** Its control flow, argv contract, blind A/B and scorecard
  are covered by tests against stubbed libraries. The generation call itself
  is verified at the next real-hardware gate.
- **Backward compatibility**: everything v2 added to `llm-synthesize`
  (`teacher_base_url`, `teacher_model`, `holdout_pct`, `env_file`) is
  optional and defaults to the old behaviour. An existing job or script that
  never sets them behaves exactly as it did.
