"""The hardware ladder: what each GPU is FOR, joined to live prices.

Phase 86. The catalog screen shows a price per instance type and nothing
else, which serves the person who already knows what an SXM5 is and leaves
everyone else guessing. This module is the teaching layer: for each GPU
family Manifold has actually met, a few sentences of what it is good at and
when to step up - joined at request time to the PROVIDER's numbers.

Two rules, both learned the hard way in this repo:

DATA COMES FROM THE PROVIDER, WORDS COME FROM HERE. A price, a VRAM figure
or a capacity flag in the output is always parsed from the live catalog
entry, never written in this file. A hardcoded dollar figure survived on
the cluster launch screen for a month (DECISIONS.md 2026-08-14); the guide
must never be able to repeat that, and a test refuses any dollar literal
in this module - which is why this paragraph does not name the number.

ARITHMETIC IS LABELLED AS ARITHMETIC. "Serves ~30B at 4-bit" is a rule of
thumb computed from VRAM, not a measurement, and every payload carries the
formula in a `basis` string so the UI can say so. Manifold's honesty rule
for money (exact or labelled) applies to capability claims too.

Pure: no I/O, no client, no clock. The route in main.py fetches the
catalog; this module only joins and computes, so it tests without a
network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# What the fits arithmetic assumes, shipped with every response so the UI
# can show its work instead of asserting a capability.
FITS_BASIS = (
    "Rule of thumb from VRAM alone: fp16 weights need ~2 GB per billion "
    "parameters and 4-bit ~0.55 GB, with 20% held back for KV cache and "
    "overhead; LoRA fine-tuning divides VRAM by 7 and QLoRA by 3. Long "
    "contexts, big batches or fat KV caches shrink all of these. "
    "Estimates, not promises."
)


@dataclass(frozen=True)
class FamilyNotes:
    """The curated, human half of one ladder rung."""

    family: str          # display name, e.g. "A100 80 GB"
    era: str             # architecture + year, one phrase
    good_for: str        # what a person actually uses it for
    step_up_when: str    # the honest reason to spend more
    note: str = ""       # a caveat worth knowing before paying


# Matched IN ORDER against the provider's gpu_description; first hit wins.
# Keyed on description rather than instance_type so a new size of a known
# card (a future gpu_4x_b200) lands on the right notes automatically.
_FAMILIES: tuple[tuple[re.Pattern, FamilyNotes], ...] = tuple(
    (re.compile(pat, re.IGNORECASE), notes)
    for pat, notes in [
        (r"^A10 ", FamilyNotes(
            family="A10",
            era="Ampere, 2021",
            good_for=(
                "The workhorse. Serves a 7B model at fp16 or ~30B at 4-bit, "
                "LoRA-trains the small students on the distill shelf, and "
                "runs every recipe in this repo. Manifold's own "
                "real-hardware gates run on this card."
            ),
            step_up_when=(
                "your model is past ~13B at fp16, training slows to a "
                "crawl, or you keep hitting CUDA out-of-memory."
            ),
        )),
        (r"^RTX 6000", FamilyNotes(
            family="RTX 6000",
            era="Turing, 2018",
            good_for=(
                "The cheapest real GPU on the menu. Fine for small serves, "
                "Whisper batches and trying the workflow end to end for "
                "pocket change."
            ),
            step_up_when=(
                "you need bf16 or modern attention kernels - this card "
                "predates both, and some current images assume them."
            ),
            note="Oldest architecture here; check your image supports it.",
        )),
        (r"^A6000", FamilyNotes(
            family="A6000",
            era="Ampere, 2020",
            good_for=(
                "Twice the A10's memory for not much more money. Serves "
                "13B at fp16 or ~70B at 4-bit, and takes LoRA runs the "
                "A10 cannot fit."
            ),
            step_up_when=(
                "speed starts to matter: same era as the A10, so it holds "
                "more but does not go much faster."
            ),
        )),
        (r"^Tesla V100", FamilyNotes(
            family="V100",
            era="Volta, 2017",
            good_for=(
                "Legacy 8x nodes at a low price per GPU. Usable for "
                "embarrassingly parallel batch work split across cards."
            ),
            step_up_when=(
                "you are doing anything modern: no bf16, small memory, "
                "and many current images no longer test on it."
            ),
            note="Two generations old. Prefer an A10 unless the price is the point.",
        )),
        (r"^A100 \(40", FamilyNotes(
            family="A100 40 GB",
            era="Ampere, 2020, HBM2",
            good_for=(
                "The classic training card: much faster memory than an "
                "A10. Full fine-tunes of small models, fast LoRA on "
                "mid-size ones, ~60B serving at 4-bit."
            ),
            step_up_when=(
                "the model or its context stops fitting - the 80 GB "
                "version holds twice as much of both."
            ),
        )),
        (r"^A100 \(80", FamilyNotes(
            family="A100 80 GB",
            era="Ampere, 2020, HBM2e",
            good_for=(
                "Room for 70B-class work. As an 8x node it serves big "
                "models with tensor parallel across 640 GB, at the lowest "
                "price per GB of the large nodes."
            ),
            step_up_when=(
                "you want raw speed rather than room: an H100 is the "
                "same memory but a generation faster."
            ),
        )),
        (r"^GH200", FamilyNotes(
            family="GH200",
            era="Grace-Hopper, 2023",
            good_for=(
                "An H100-class GPU fused to a large ARM CPU with a fast "
                "link between them. Strong for single-card work that "
                "spills over into CPU memory."
            ),
            step_up_when="you need more than one GPU in the box.",
            note=(
                "The host CPU is ARM, and most docker images in the "
                "templates are built for x86. Check your image publishes "
                "an arm64 build before queueing anything here."
            ),
        )),
        (r"^H100", FamilyNotes(
            family="H100",
            era="Hopper, 2022",
            good_for=(
                "The current training standard: fp8, fast attention, "
                "big bandwidth. One card trains mid-size models quickly; "
                "the 8x node is 640 GB of tensor-parallel serving for "
                "100B-class models."
            ),
            step_up_when=(
                "memory is the wall even at 8x, or Blackwell capacity "
                "near you gets cheap enough to matter."
            ),
            note=(
                "Measured here 2026-08-14: Qwen2.5-7B fp16 generated 168 "
                "tokens/s single-stream on one SXM5 card, end to end "
                "through Manifold's managed SSH forward."
            ),
        )),
        (r"^B200", FamilyNotes(
            family="B200",
            era="Blackwell, 2024",
            good_for=(
                "The frontier card. One GPU holds what used to take a "
                "node: ~90B at fp16 or ~260B at 4-bit by the rule of "
                "thumb. The 8x node is 1.4 TB of VRAM in one box."
            ),
            step_up_when="there is nowhere further up to step.",
            note=(
                "Newest silicon means newest-kernel requirements; images "
                "and frameworks move fast here and rough edges are normal."
            ),
        )),
    ]
)

_GENERIC = FamilyNotes(
    family="",
    era="",
    good_for=(
        "No curated notes for this card yet. The numbers beside it are "
        "still real: price and capacity from the provider, capability "
        "from VRAM arithmetic."
    ),
    step_up_when="",
    note="",
)

_VRAM_RE = re.compile(r"\((\d+)\s*GB", re.IGNORECASE)


def parse_vram_gib(gpu_description: str) -> int | None:
    """VRAM per GPU out of the provider's own description string.

    "A100 (80 GB SXM4)" -> 80. Parsed rather than curated so a card this
    file has never heard of still gets a true number - and so a provider
    correction never waits on an edit here.
    """
    m = _VRAM_RE.search(gpu_description or "")
    return int(m.group(1)) if m else None


def _notes_for(gpu_description: str) -> FamilyNotes:
    for pattern, notes in _FAMILIES:
        if pattern.search(gpu_description or ""):
            return notes
    return _GENERIC


def _fits(vram_total_gib: int) -> dict:
    """Capability arithmetic. See FITS_BASIS for exactly what it assumes."""
    usable = vram_total_gib * 0.8
    return {
        "serve_fp16_b": int(usable / 2),
        "serve_4bit_b": int(usable / 0.55),
        "lora_bf16_b": int(vram_total_gib / 7),
        "qlora_4bit_b": int(vram_total_gib / 3),
    }


def build_guide(types: dict) -> dict:
    """The ladder: one entry per GPU instance type, cheapest-capable first.

    `types` is the serialized /instance-types payload (name -> description,
    gpu_description, price_usd_per_hour, specs, regions_with_capacity).
    CPU-only types are skipped: this is a GPU guide. Entries whose VRAM
    cannot be parsed from the provider string are still listed - with
    fits/vram null rather than guessed.
    """
    rungs = []
    for name, t in types.items():
        specs = t.get("specs") or {}
        count = int(specs.get("gpus") or 0)
        if count < 1:
            continue
        vram_per = parse_vram_gib(t.get("gpu_description", ""))
        vram_total = vram_per * count if vram_per else None
        notes = _notes_for(t.get("gpu_description", ""))
        price = t.get("price_usd_per_hour")
        regions = t.get("regions_with_capacity") or []
        rung = {
            "instance_type": name,
            "label": t.get("description", name),
            "family": notes.family or t.get("gpu_description", name),
            "era": notes.era,
            "gpu_count": count,
            "vram_per_gpu_gib": vram_per,
            "vram_total_gib": vram_total,
            # Provider numbers, passed through untouched.
            "price_usd_per_hour": price,
            "regions_with_capacity": regions,
            "available_now": bool(regions),
            # Comparison arithmetic on provider numbers only.
            "price_per_gib_hour": (
                round(price / vram_total, 4)
                if price and vram_total else None
            ),
            # The teaching layer.
            "good_for": notes.good_for,
            "step_up_when": notes.step_up_when,
            "note": notes.note,
            "fits": _fits(vram_total) if vram_total else None,
        }
        if count > 1:
            multi = (
                f"{count} GPUs in ONE machine. Serving shards a model "
                f"across them (vllm-serve's tensor_parallel={count}); "
                f"training on all {count} needs a framework that does "
                f"multi-GPU itself. Manifold clusters coordinate SEPARATE "
                f"machines and are not distributed training."
            )
            rung["note"] = f"{rung['note']} {multi}".strip()
        rungs.append(rung)

    # Cheapest-capable first: climb the ladder by total memory, then price.
    rungs.sort(key=lambda r: (r["vram_total_gib"] or 0,
                              r["price_usd_per_hour"] or 0,
                              r["instance_type"]))
    return {"rungs": rungs, "fits_basis": FITS_BASIS}
