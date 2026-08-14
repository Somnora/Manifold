"""Distillation: plain words in, a training config a human reviews out.

Phase 84. The distill loop already exists as job templates (synthesize ->
judge -> axolotl-finetune -> lora-merge -> eval). The one thing a person
still has to hand-write is the axolotl YAML, and getting it wrong costs a
GPU hour before the mistake shows up. So a brain writes it and Manifold
checks it.

This module is the pure half: build the prompt, then decide whether what
came back is a config Manifold is willing to hand to a GPU. No I/O, no
brain calls, no clock - the route in main.py owns those. Keeping the
decisions here means the validator can be tested without a model, a
network, or a byte of spend.

VALIDATION IS A SECURITY BOUNDARY, NOT A LINT. axolotl-finetune runs
`accelerate launch -m axolotl.cli.train /data/config/<file>` on the GPU
box, so this file is EXECUTED. A config nobody vetted can set
trust_remote_code (arbitrary Python from a HuggingFace repo), point
base_model at any repo on the internet, or write outside the mounts. A
model wrote it; that is exactly why the checks below are an allowlist and
not a "does it have the right keys" glance.

Nothing here writes anything. The route returns the config for REVIEW;
saving it is the existing upload route and training is the existing
axolotl-finetune job, both of which a human triggers.
"""

from __future__ import annotations

import re

import yaml

# The generated config is small by nature (a page of scalars). The cap is a
# ceiling on what we hand to the YAML parser at all, applied BEFORE parsing:
# a chatty brain that returns an essay, or a hostile one that returns a
# megabyte, is refused without the parser ever touching it.
MAX_CONFIG_CHARS = 20_000

# Where the axolotl-finetune template mounts each directory INSIDE the
# container. These are the only paths a generated config may name, because
# they are the only paths that exist in the job: configs/ (read-only),
# synthesized/ (read-only, the training sets), outputs/ (writable), and the
# ephemeral scratch volume. Change these only when the template's volumes
# change - the two must agree or the training job dies on a missing file.
DATASET_DIR = "/data/synthesized"
OUTPUT_DIR = "/data/output"
SCRATCH_DIR = "/tmp/"
# lora-merge writes merged models to {persistent}/models, mounted here by
# vllm-serve and lora-merge. axolotl-finetune does NOT mount it (see the
# advisory in validate_config): a config that starts from a merged student
# needs a custom template.
MERGED_MODELS_DIR = "/data/models/"

# Keys a generated config may carry. Anything not on this list is not
# "unsupported" - it is "a human adds it by hand after reading it". The
# omissions are deliberate: no hub_model_id / wandb_* (they push data and
# want tokens), no dataset loader overrides, no trust_remote_code.
ALLOWED_KEYS = {
    # what to train
    "base_model", "model_type", "tokenizer_type",
    "load_in_8bit", "load_in_4bit", "strict",
    # what to train on
    "datasets", "dataset_prepared_path", "val_set_size",
    "sequence_len", "sample_packing", "pad_to_sequence_len",
    "train_on_inputs", "group_by_length",
    # how to train it
    "adapter", "lora_r", "lora_alpha", "lora_dropout",
    "lora_target_linear", "lora_target_modules", "lora_modules_to_save",
    "micro_batch_size", "gradient_accumulation_steps", "num_epochs",
    "learning_rate", "optimizer", "lr_scheduler",
    "warmup_steps", "warmup_ratio", "weight_decay", "max_grad_norm",
    "bf16", "fp16", "tf32", "gradient_checkpointing", "flash_attention",
    "seed", "special_tokens", "chat_template",
    # where it goes / what it says
    "output_dir",
    "logging_steps", "save_strategy", "save_steps", "saves_per_epoch",
    "evals_per_epoch", "eval_steps",
}

# Same rule one level down, for each entry of `datasets:`. `data_files` and
# `name` are absent on purpose: both can pull a different file (or a whole
# HuggingFace repo) than the one the user asked to train on.
ALLOWED_DATASET_KEYS = {
    "path", "type", "ds_type", "split",
    "field_instruction", "field_input", "field_output",
}

# Without these the job either cannot start or trains the wrong thing.
REQUIRED_KEYS = ("base_model", "datasets", "output_dir", "adapter")

# This seam exists to make LoRA students. A full fine-tune of even a 3B
# base needs far more VRAM than the A10 tier this shelf targets, so it is
# refused here rather than discovered as an OOM twenty minutes into a run.
ALLOWED_ADAPTERS = ("lora", "qlora")

# Held-out rows live beside the training set under the same synthesized/
# directory (llm-synthesize writes eval-<name>.jsonl when holdout_pct > 0).
# Training on them makes the scorecard a lie, so the name is refused here
# where the mistake is free.
HOLDOUT_PREFIX = "eval-"


class ConfigRejected(Exception):
    """What the brain returned is not a config Manifold will run.

    The message is the whole point: it is shown to the user verbatim, so it
    names the offending key, path or fragment rather than saying "invalid".
    """


class _NoAliasLoader(yaml.SafeLoader):
    """safe_load, minus YAML anchors and aliases.

    safe_load refuses to build arbitrary Python objects, but it still
    EXPANDS aliases: a few hundred bytes of nested `&a [*b, *b, *b]`
    becomes gigabytes in memory (the "billion laughs" bomb) and takes the
    backend down with it, which the MAX_CONFIG_CHARS cap alone cannot
    prevent. A training config has no use for anchors, so they are refused
    outright rather than given an expansion budget somebody has to guess.
    """

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ConfigRejected(
                "the config uses a YAML alias (*name). Manifold refuses "
                "anchors and aliases in a generated config: write the value "
                "out in full."
            )
        return super().compose_node(parent, index)


# -- user input ---------------------------------------------------------------


def validate_dataset_name(name: str) -> str:
    """The training file's name under <filesystem>/synthesized.

    A bare filename, never a path: the container path is built here, so a
    value like "../../etc/passwd" can never become part of it. Raises
    ValueError (a caller mistake -> 422), not ConfigRejected (the brain's
    mistake -> 502).
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("name the training file under synthesized/")
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(
            f"'{name}' must be a bare filename in <filesystem>/synthesized "
            f"(no directories), e.g. kept-tags.jsonl"
        )
    if not name.endswith(".jsonl"):
        raise ValueError(
            f"'{name}' must be a .jsonl file: llm-synthesize and llm-judge "
            f"both write JSON Lines"
        )
    if name.startswith(HOLDOUT_PREFIX):
        raise ValueError(
            f"'{name}' is a held-out evaluation set, not a training set. "
            f"Training on it would make the scorecard meaningless (the "
            f"student would be graded on rows it memorised). Train on the "
            f"kept-*.jsonl file instead."
        )
    return name


def dataset_container_path(dataset: str) -> str:
    """Where axolotl-finetune sees that file (its synthesized/ mount)."""
    return f"{DATASET_DIR}/{dataset}"


def config_filename(dataset: str) -> str:
    """A suggested name for the saved config: distill-<dataset stem>.yaml.

    Only [a-z0-9-] survives, so the suggestion is always a safe filename to
    put in an upload destination.
    """
    stem = dataset[: -len(".jsonl")] if dataset.endswith(".jsonl") else dataset
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "student"
    return f"configs/distill-{slug}.yaml"


# -- the prompt ---------------------------------------------------------------


def _shelf_lines(students: list[dict]) -> str:
    return "\n".join(
        f"- {s['model_id']} ({s.get('params_b', '?')}B params, needs about "
        f"{s['vram_gib']} GB VRAM to LoRA-train): {s['note']}"
        for s in students
    )


def build_prompt(*, spec: str, dataset: str, students: list[dict],
                 student_model: str = "") -> str:
    """The whole instruction the brain gets. Pure: same inputs, same text.

    Two things it must land: the fixed paths (the brain cannot know the
    template's mounts, and a guessed path fails only after the GPU is
    running) and the allowlist (a config full of keys the validator will
    reject wastes a brain call and reads to the user as a broken feature).
    """
    pinned = (
        f"\nThe user has already chosen the student: use exactly "
        f"base_model: {student_model}\n" if student_model else ""
    )
    return f"""You are writing an axolotl LoRA fine-tuning config for a \
knowledge-distillation run. A larger teacher model has already generated \
and curated the training data; your job is only the training config for \
the small student model.

What the user wants:
{spec}

These values are FIXED by the job template's volume mounts. Use them \
exactly, character for character:
- datasets[0].path: {dataset_container_path(dataset)}
- output_dir: must start with {OUTPUT_DIR}/ (pick a short descriptive name)
- dataset_prepared_path: must start with {SCRATCH_DIR} (use \
/tmp/axolotl/prepared)

Choose base_model from this shelf of small open bases (or use the one the \
user pinned). These are the only base models accepted:
{_shelf_lines(students)}
{pinned}
Rules:
- adapter must be lora or qlora. This runs on one 24 GB card.
- Do NOT include trust_remote_code, hub_model_id, or any wandb key. They \
are rejected.
- Use only these top-level keys: {', '.join(sorted(ALLOWED_KEYS))}
- Inside each datasets entry, use only: \
{', '.join(sorted(ALLOWED_DATASET_KEYS))}
- The training rows are alpaca-shaped (instruction / input / output), so \
type: alpaca.
- No YAML anchors or aliases.
- Size the batch, sequence length and epochs so the run fits the card the \
user described.

Answer with the YAML document and nothing else: no explanation before or \
after it, no markdown code fence.
"""


# -- the answer ---------------------------------------------------------------

_FENCE_RE = re.compile(r"```[a-zA-Z]*[ \t]*\n(.*?)```", re.DOTALL)


def strip_code_fence(text: str) -> str:
    """The YAML out of a fenced reply, or the text unchanged.

    Models wrap code in ``` by reflex however firmly they are asked not to,
    and usually bracket it with a sentence either side. Taking the first
    fenced block is kinder than failing a whole generation (and a whole
    brain call) over punctuation, and it gives up nothing: whatever comes
    out of here still goes through every check below.
    """
    match = _FENCE_RE.search(text or "")
    return (match.group(1) if match else (text or "")).strip()


def validate_config(text: str, *, dataset: str,
                    students: list[dict]) -> dict:
    """Brain reply -> a config Manifold is willing to save, or ConfigRejected.

    Every branch below is a thing that would otherwise be discovered on the
    GPU, at cost: a missing file, an OOM, a repo that runs its own Python
    on load. The returned dict carries the config VERBATIM (fence removed)
    because the user reviews the exact text that would be saved, plus the
    handful of facts a review panel wants to show without re-parsing.
    """
    raw_text = text or ""
    if len(raw_text) > MAX_CONFIG_CHARS:
        raise ConfigRejected(
            f"the brain returned {len(raw_text)} characters; a training "
            f"config is a page of settings, not a document. Refused before "
            f"parsing (limit {MAX_CONFIG_CHARS})."
        )
    cleaned = strip_code_fence(raw_text)
    if not cleaned.strip():
        raise ConfigRejected(
            "the brain returned nothing. Try again, or pick a different "
            "brain in the picker."
        )

    try:
        parsed = yaml.load(cleaned, Loader=_NoAliasLoader)
    except yaml.YAMLError as exc:
        raise ConfigRejected(
            f"the brain did not return YAML ({exc.__class__.__name__}). It "
            f"answered: {_excerpt(cleaned)}"
        )
    if not isinstance(parsed, dict):
        raise ConfigRejected(
            f"the brain returned {type(parsed).__name__}, not a YAML "
            f"mapping of settings. It answered: {_excerpt(cleaned)}"
        )

    # Checked by name before the generic unknown-key sweep, so the message
    # is the security one rather than "unknown key".
    if parsed.get("trust_remote_code"):
        raise ConfigRejected(
            "the config sets trust_remote_code, which lets a model repo run "
            "its own Python on the GPU instance. Manifold never saves a "
            "generated config that asks for that."
        )

    unknown = sorted(set(parsed) - ALLOWED_KEYS)
    if unknown:
        raise ConfigRejected(
            f"the config carries key(s) Manifold does not vet: "
            f"{', '.join(unknown)}. Generated configs are limited to a "
            f"reviewed set of training settings; add anything else by hand "
            f"after reading it."
        )
    missing = [k for k in REQUIRED_KEYS if k not in parsed]
    if missing:
        raise ConfigRejected(
            f"the config is missing required key(s): {', '.join(missing)}. "
            f"Without them the training job cannot start."
        )

    adapter = str(parsed["adapter"]).strip().lower()
    if adapter not in ALLOWED_ADAPTERS:
        raise ConfigRejected(
            f"adapter is '{parsed['adapter']}'; this seam builds LoRA "
            f"students only ({' or '.join(ALLOWED_ADAPTERS)}). A full "
            f"fine-tune does not fit the card this shelf targets."
        )

    advisories: list[str] = []
    base_model = _check_base_model(parsed["base_model"], students, advisories)
    dataset_path = _check_datasets(parsed["datasets"], dataset)
    output_dir = _check_output_dir(parsed["output_dir"])
    _check_prepared_path(parsed.get("dataset_prepared_path"))

    if "val_set_size" not in parsed:
        advisories.append(
            "no val_set_size: the run will report training loss only, with "
            "no held-out loss to show overfitting.")

    return {
        "yaml": cleaned,
        "parsed": parsed,
        "base_model": base_model,
        "dataset_path": dataset_path,
        "output_dir": output_dir,
        "advisories": advisories,
    }


def _excerpt(text: str, limit: int = 200) -> str:
    """The first of what actually came back, so the error teaches.

    Newlines are folded out: this lands in an HTTP detail string and a
    one-line toast, where a multi-line excerpt reads as broken.
    """
    flat = " ".join(text.split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _check_base_model(value, students: list[dict],
                      advisories: list[str]) -> str:
    """The student. Either from the curated shelf or a model already on
    this filesystem - never an arbitrary repo id, which axolotl would
    download and execute the config of."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigRejected("base_model must be a model id string")
    base = value.strip()
    if base in {s["model_id"] for s in students}:
        return base
    if base.startswith(MERGED_MODELS_DIR) and ".." not in base:
        advisories.append(
            f"base_model is a merged model on the filesystem ({base}). "
            f"axolotl-finetune mounts configs/, synthesized/, outputs/ and "
            f"the HF cache, but NOT models/, so this config needs a custom "
            f"template that mounts it.")
        return base
    shelf = ", ".join(s["model_id"] for s in students)
    raise ConfigRejected(
        f"base_model '{base}' is not on the student shelf and is not a "
        f"merged model under {MERGED_MODELS_DIR}. Manifold only generates "
        f"configs for bases it has vetted: {shelf}"
    )


def _check_datasets(value, dataset: str) -> str:
    """Exactly the file the user named, and nothing else.

    An exact match, not a prefix: a glob like /data/synthesized/*.jsonl
    would sweep in the held-out eval-*.jsonl sitting in the same directory
    and train the student on its own exam.
    """
    if not isinstance(value, list) or not value:
        raise ConfigRejected("datasets must be a non-empty list")
    expected = dataset_container_path(dataset)
    if len(value) > 1:
        raise ConfigRejected(
            f"the config lists {len(value)} datasets; this seam trains on "
            f"the one file you named ({expected}). Combine sources into a "
            f"single JSONL first."
        )
    entry = value[0]
    if not isinstance(entry, dict):
        raise ConfigRejected("each datasets entry must be a mapping")
    unknown = sorted(set(entry) - ALLOWED_DATASET_KEYS)
    if unknown:
        raise ConfigRejected(
            f"datasets entry carries key(s) Manifold does not vet: "
            f"{', '.join(unknown)}"
        )
    path = str(entry.get("path", "")).strip()
    if path != expected:
        raise ConfigRejected(
            f"datasets[0].path is '{path}', but the training file you named "
            f"is mounted at '{expected}'. The job mounts synthesized/ "
            f"read-only at {DATASET_DIR}; no other path exists in the "
            f"container."
        )
    return path


def _check_output_dir(value) -> str:
    """Adapters must land on the persistent filesystem. Anywhere else in
    the container dies with the container, which is a training run bought
    and thrown away."""
    if not isinstance(value, str):
        raise ConfigRejected("output_dir must be a path string")
    out = value.strip().rstrip("/")
    if not out.startswith(OUTPUT_DIR + "/") or ".." in out:
        raise ConfigRejected(
            f"output_dir is '{value}'; it must be a directory under "
            f"{OUTPUT_DIR}/ (the job's only writable persistent mount). "
            f"Anything else is destroyed with the instance."
        )
    return out


def _check_prepared_path(value) -> None:
    """Tokenised-dataset scratch. Allowed to be absent; if present it must
    be the ephemeral volume, not a persistent mount it would fill up."""
    if value is None:
        return
    if not isinstance(value, str) or not value.strip().startswith(SCRATCH_DIR):
        raise ConfigRejected(
            f"dataset_prepared_path is '{value}'; it must live under "
            f"{SCRATCH_DIR} (the job's scratch volume), e.g. "
            f"/tmp/axolotl/prepared."
        )
