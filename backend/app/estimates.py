"""Cost + utilization intelligence, derived from data Manifold already keeps.

Pure functions only — no I/O, no clock, no DB. Callers pass in the rows they
read from SQLite; these turn them into money-facing estimates and post-run
verdicts. Keeping this side-effect-free makes the estimation math and the
right-size threshold trivially testable (see test_estimates.py) and keeps it
safely off the launch path.

Two jobs:
  1. estimate_job(...)        — pre-launch "≈ 40 min · ~$0.85", confidence-tagged
  2. utilization_summary(...) — post-run "A10, 45 min · peak 9/24 GB · avg 14%"
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

# -- pre-launch estimate ----------------------------------------------------------

# Coarse per-template fallback runtimes (minutes) for when there is NO history
# yet on a given GPU. Deliberately rough, order-of-magnitude figures — every
# estimate built from these is tagged confidence="rough" so the UI can say so.
# None = "runs until you stop it" (servers), which we never guess a cost for.
DEFAULT_MINUTES: dict[str, float | None] = {
    "gpu-smoke": 2,
    "whisper-batch": 30,
    "sdxl-generate": 3,
    "script-run": 15,
    "llm-synthesize": 20,
    # Both are one model call per row, like llm-synthesize, but over a set
    # that has already been filtered - and llm-eval additionally loads the
    # student in-process and generates from it, which is the slow half (and
    # slower still on the documented student_device=cpu escape hatch).
    # Without these two the fallback `.get(template, 15)` quietly costed
    # them as 15-minute jobs. Found by the Phase 84 verifier, 2026-08-14.
    "llm-judge": 15,
    "llm-eval": 40,
    "axolotl-finetune": 120,
    "lora-merge": 10,
    "tao-train": 120,
    "vllm-serve": None,        # a server: no fixed runtime, so no fixed cost
    "sglang-serve": None,      # same: serves until stopped
}

# History rows needed before an estimate stops being "rough" and becomes
# "measured". Below this we still show the median but flag it as still-learning:
# a couple of runs can be unrepresentative, so we do not overclaim precision.
MEASURED_MIN_RUNS = 3


@dataclass
class Estimate:
    template: str
    instance_type: str
    minutes: float | None          # None when the template has no fixed runtime
    cost_usd: float | None
    confidence: str                # "measured" | "rough" | "none"
    basis: str                     # human sentence explaining where it came from
    sample_size: int

    def to_dict(self) -> dict:
        return {
            "template": self.template,
            "instance_type": self.instance_type,
            "minutes": None if self.minutes is None else round(self.minutes, 1),
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 2),
            "confidence": self.confidence,
            "basis": self.basis,
            "sample_size": self.sample_size,
        }


def estimate_job(
    template: str,
    instance_type: str,
    durations_seconds: list[float],
    hourly_rate_cents: int | None,
) -> Estimate:
    """Estimate runtime + cost for `template` on `instance_type`.

    durations_seconds: runtimes of PAST successful runs of exactly this
    (template, GPU) pair. Its length drives confidence:
      >= MEASURED_MIN_RUNS  -> "measured" (median of real runs here)
      1 .. MEASURED_MIN_RUNS-1 -> "rough"  (median, but still learning)
      0                     -> "rough"  (coarse per-template default), or
                               "none" when the template has no fixed runtime.
    """
    rate = (hourly_rate_cents or 0) / 100.0

    def cost(minutes: float | None) -> float | None:
        if minutes is None or not rate:
            return None
        return (minutes / 60.0) * rate

    n = len(durations_seconds)
    if n >= 1:
        minutes = statistics.median(durations_seconds) / 60.0
        if n >= MEASURED_MIN_RUNS:
            conf, basis = "measured", (
                f"median of {n} past {template} runs on {instance_type}"
            )
        else:
            conf, basis = "rough", (
                f"based on only {n} past run{'s' if n != 1 else ''} on "
                f"{instance_type}; still learning, treat as rough"
            )
        return Estimate(template, instance_type, minutes, cost(minutes),
                        conf, basis, n)

    # No history for this pair — fall back to the coarse default.
    default = DEFAULT_MINUTES.get(template, 15)
    if default is None:
        return Estimate(
            template, instance_type, None, None, "none",
            f"{template} runs until you stop it; no fixed runtime to estimate",
            0,
        )
    return Estimate(
        template, instance_type, default, cost(default), "rough",
        f"no history yet for {template} on {instance_type}; coarse default, "
        f"rough", 0,
    )


# -- post-run utilization ---------------------------------------------------------

# Right-size hint fires only when PEAK VRAM used stayed at or below this
# fraction of the card's capacity across the run. Rationale (see DECISIONS.md):
# right-sizing is a MEMORY question, not a utilization one — a memory-light but
# compute-heavy job still needs the card, and a false "use something smaller"
# that then OOMs destroys trust. At 0.45, the peak used less than half of VRAM,
# so a card with ~half the memory would still have left real headroom. We key
# strictly on peak (not average) VRAM, and never hint on thin telemetry.
RIGHT_SIZE_VRAM_FRACTION = 0.45
MIN_SAMPLES_FOR_HINT = 5


@dataclass
class Utilization:
    gpu_description: str
    runtime_seconds: float | None
    peak_vram_used_mib: int
    vram_total_mib: int
    avg_util_pct: float
    sample_count: int
    right_size_hint: bool
    verdict: str                   # the one-line summary
    hint: str                      # the right-size sentence, or ""

    def to_dict(self) -> dict:
        return {
            "gpu_description": self.gpu_description,
            "runtime_seconds": self.runtime_seconds,
            "peak_vram_used_mib": self.peak_vram_used_mib,
            "vram_total_mib": self.vram_total_mib,
            "avg_util_pct": round(self.avg_util_pct, 1),
            "sample_count": self.sample_count,
            "right_size_hint": self.right_size_hint,
            "verdict": self.verdict,
            "hint": self.hint,
        }


def _gib(mib: int) -> float:
    return mib / 1024.0


def utilization_summary(
    *,
    gpu_description: str,
    runtime_seconds: float | None,
    peak_vram_used_mib: int,
    vram_total_mib: int,
    avg_util_pct: float,
    sample_count: int,
) -> Utilization:
    """Turn aggregated telemetry into a one-line verdict + a conservative,
    memory-based right-size hint. Advisory only; never changes a selection."""
    mins = None if runtime_seconds is None else runtime_seconds / 60.0
    runtime_str = f"{mins:.0f} min" if mins is not None else "runtime unknown"
    gpu = gpu_description or "GPU"

    if sample_count == 0 or vram_total_mib <= 0:
        return Utilization(
            gpu, runtime_seconds, peak_vram_used_mib, vram_total_mib,
            avg_util_pct, sample_count, False,
            f"{gpu}, {runtime_str} · no telemetry captured", "",
        )

    verdict = (
        f"{gpu}, {runtime_str} · peak VRAM "
        f"{_gib(peak_vram_used_mib):.1f}/{_gib(vram_total_mib):.0f} GB · "
        f"avg util {avg_util_pct:.0f}%"
    )

    frac = peak_vram_used_mib / vram_total_mib
    enough = sample_count >= MIN_SAMPLES_FOR_HINT
    hint_fires = enough and frac <= RIGHT_SIZE_VRAM_FRACTION
    hint = ""
    if hint_fires:
        hint = (
            f"Peak VRAM was only {frac * 100:.0f}% of this card, so a "
            f"smaller/cheaper GPU likely would have fit. (Advisory: check a "
            f"real run before downsizing; peak, not average, is what OOMs.)"
        )
    elif enough and frac <= 0.65:
        # Close-but-not-clear: stay silent on downsizing, but note the headroom
        # honestly rather than implying the card was fully used.
        hint = (
            f"Peak VRAM was {frac * 100:.0f}% of capacity: some headroom, but "
            f"not clearly enough to recommend a smaller card."
        )
    elif not enough:
        hint = "Limited telemetry for this run; no right-size call made."

    return Utilization(
        gpu, runtime_seconds, peak_vram_used_mib, vram_total_mib,
        avg_util_pct, sample_count, hint_fires, verdict, hint,
    )


# -- model-fit preflight ------------------------------------------------------------

# Does this model plausibly fit on that GPU? Everything here is estimated
# from the MODEL NAME alone (parameter count and quantization markers), not
# from downloaded weights, so every verdict is advisory and says so. Born
# from a real night lost to it: a 27B GPTQ-Int4 checkpoint OOMing an A10
# during weight load, discovered only after boot + download + reboot.

# Effective bytes per parameter for the weights as loaded, including the
# usual buffers/overhead. fp16/bf16 is the default when no marker matches.
BYTES_PER_PARAM_FP16 = 2.1
_QUANT_MARKERS: list[tuple[tuple[str, ...], float, str]] = [
    (("int4", "4bit", "4-bit", "gptq", "awq", "nf4", "q4"), 0.65, "4-bit"),
    (("int8", "8bit", "8-bit", "fp8", "q8"), 1.15, "8-bit"),
    (("q2",), 0.45, "2-bit"),
    (("q3",), 0.55, "3-bit"),
    (("q5",), 0.80, "5-bit"),
    (("q6",), 0.90, "6-bit"),
]

# Weights-to-VRAM ratio tiers. Above FIT there is no room for KV cache and
# CUDA context on top of the weights; a server will be cramped well before
# that, hence the TIGHT band.
FIT_RATIO = 0.70
TIGHT_RATIO = 0.92

_MOE_RE = re.compile(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*b(?![a-z0-9])", re.I)
_PARAMS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b(?![a-z0-9])", re.I)
_VRAM_RE = re.compile(r"(\d+)\s*GB", re.I)
_GPU_COUNT_RE = re.compile(r"gpu_(\d+)x", re.I)


def parse_model_params_b(model_id: str) -> float | None:
    """Billions of parameters, read from the model name. None when the name
    carries no size (e.g. 'gpt2', 'whisper-large-v3')."""
    moe = _MOE_RE.search(model_id)
    if moe:
        return int(moe.group(1)) * float(moe.group(2))
    matches = _PARAMS_RE.findall(model_id)
    # Names put the size after the family version ("Llama-3.1-8B"), so when
    # several tokens look like sizes, the last one is the parameter count.
    return float(matches[-1]) if matches else None


def parse_quant_bytes(model_id: str) -> tuple[float, str]:
    """(bytes per parameter, human label) from quantization markers in the
    name. Default fp16/bf16 when nothing matches."""
    lowered = model_id.lower()
    for markers, bytes_pp, label in _QUANT_MARKERS:
        if any(m in lowered for m in markers):
            return bytes_pp, label
    return BYTES_PER_PARAM_FP16, "fp16/bf16"


def parse_vram_gb(instance_type_name: str, gpu_description: str) -> float | None:
    """Total VRAM of an instance type: per-GPU size from the description
    ('A10 (24 GB PCIe)') times the GPU count from the name ('gpu_8x_a100')."""
    m = _VRAM_RE.search(gpu_description or "")
    if not m:
        return None
    per_gpu = float(m.group(1))
    count = _GPU_COUNT_RE.search(instance_type_name or "")
    return per_gpu * (int(count.group(1)) if count else 1)


def model_fit(model_id: str, instance_type_name: str,
              gpu_description: str, exact: dict | None = None) -> dict:
    """Advisory pre-launch check: will this model's weights plausibly fit in
    that GPU's VRAM? Never blocks anything; the caller shows the note.

    `exact` (from hf_lookup, when the HF API answered) overrides the
    name-parse with the repo's real parameter count and dtype-accurate
    weight bytes - which also covers renamed forks and gated repos the
    name heuristic cannot read."""
    vram_gb = parse_vram_gb(instance_type_name, gpu_description)
    if exact:
        params_b: float | None = exact["params_b"]
        basis = "exact, from the HuggingFace repo metadata"
    else:
        params_b = parse_model_params_b(model_id)
        basis = "estimated from the model name, not from downloaded weights"
    base = {
        "model": model_id,
        "instance_type": instance_type_name,
        "params_b": params_b,
        "vram_gb": vram_gb,
        "basis": basis,
    }
    if params_b is None or not vram_gb:
        return {**base, "verdict": "unknown", "est_weights_gb": None,
                "note": ""}
    if exact:
        est_weights_gb, quant = exact["weights_gb"], "stored-dtype"
    else:
        bytes_pp, quant = parse_quant_bytes(model_id)
        est_weights_gb = round(params_b * bytes_pp, 1)
    ratio = est_weights_gb / vram_gb
    if ratio <= FIT_RATIO:
        verdict, note = "fits", ""
    elif ratio <= TIGHT_RATIO:
        verdict = "tight"
        note = (
            f"Tight fit: ~{est_weights_gb:.0f} GB of {quant} weights "
            f"(~{params_b:g}B params) in {vram_gb:.0f} GB VRAM leaves little "
            f"room for KV cache. Expect a small context window or an OOM; "
            f"a bigger GPU is safer."
        )
    else:
        verdict = "no"
        note = (
            f"Unlikely to fit: ~{est_weights_gb:.0f} GB of {quant} weights "
            f"(~{params_b:g}B params) will not load into {vram_gb:.0f} GB "
            f"VRAM. Pick a bigger GPU or a smaller/more quantized model."
        )
    return {**base, "verdict": verdict, "est_weights_gb": est_weights_gb,
            "note": note}
