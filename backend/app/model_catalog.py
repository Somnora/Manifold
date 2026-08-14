"""Curated vLLM-serveable model presets, tiered by GPU VRAM.

Only ungated models that load in vllm/vllm-openai with no HuggingFace token
are listed, so clicking a preset and serving it "just works" on a first run.
Every repo id below was verified against the HF API (exists, gated=False),
last checked 2026-07-17. Gated models (Llama, Gemma) need a token Manifold
does not pass yet — noted in DECISIONS.md rather than offered as a preset
that fails.

Hardware tiers for the large MoE presets follow Lambda's own per-model
benchmark pages (lambda.ai/inference-models/<repo>), constrained to the
instance types Lambda actually offers ON-DEMAND: there is no 4x B200
on-demand type, so models Lambda benchmarks on 4x B200 map to the closest
on-demand fit here. "1x NVIDIA HGX B200" on those pages means one 8-GPU
HGX SYSTEM (their deploy commands use --tp 8), not a single B200 card.
DeepSeek-V4-Pro is deliberately absent: Lambda serves it with data+expert
parallelism, which the vllm-serve template (tensor parallel only) cannot
express; serve it via a custom template instead.

vram_gib is a practical floor (weights + KV cache at ~8k context, vLLM's
default 0.9 memory utilisation), not a theoretical minimum. Multi-GPU
presets carry `parameters` (e.g. tensor_parallel) that the Jobs page seeds
into the vllm-serve form alongside the model id.
"""

MODEL_PRESETS = [
    # -- A10 24GB ------------------------------------------------------------
    {
        "label": "Qwen3 8B",
        "model_id": "Qwen/Qwen3-8B",
        "vram_gib": 24,
        "tier": "A10 24GB",
        "note": "Solid general-purpose default; fits a single A10.",
    },
    {
        "label": "Qwen3 4B Instruct",
        "model_id": "Qwen/Qwen3-4B-Instruct-2507",
        "vram_gib": 12,
        "tier": "A10 24GB",
        "note": "Small and fast, with plenty of headroom on an A10.",
    },
    {
        "label": "gpt-oss 20B",
        "model_id": "openai/gpt-oss-20b",
        "vram_gib": 24,
        "tier": "A10 24GB",
        "note": "OpenAI's open-weight reasoner (MoE, MXFP4); strong for the tier.",
    },
    {
        "label": "Carbon 3B (DNA)",
        "model_id": "HuggingFaceBio/Carbon-3B",
        "vram_gib": 24,
        "tier": "A10 24GB",
        "note": "DNA language model (genomic sequences, not chat): "
                "variant-effect prediction and sequence analysis. Lambda "
                "benchmarks it on a single A10.",
    },
    # -- A100 40GB -----------------------------------------------------------
    {
        "label": "Qwen3 14B",
        "model_id": "Qwen/Qwen3-14B",
        "vram_gib": 40,
        "tier": "A100 40GB",
        "note": "More capable dense model; needs more than an A10.",
    },
    {
        "label": "Qwen3.6 27B (FP8)",
        "model_id": "Qwen/Qwen3.6-27B-FP8",
        "vram_gib": 40,
        "tier": "A100 40GB",
        "note": "Current Qwen flagship dense, FP8-quantized to fit a 40GB card.",
    },
    {
        "label": "LFM2.5 8B-A1B",
        "model_id": "LiquidAI/LFM2.5-8B-A1B",
        "vram_gib": 40,
        "tier": "A100 40GB",
        "note": "Liquid's efficiency MoE (8.3B total, ~1.5B active): very "
                "fast tokens, 128K context. Lambda benchmarks it on single "
                "A100/H100/B200 cards.",
    },
    # -- H100 80GB -----------------------------------------------------------
    {
        "label": "Qwen3.6 27B",
        "model_id": "Qwen/Qwen3.6-27B",
        "vram_gib": 80,
        "tier": "H100 80GB",
        "note": "Full-precision Qwen flagship dense; comfortable on an H100.",
    },
    {
        "label": "Qwen3.6 35B-A3B (FP8)",
        "model_id": "Qwen/Qwen3.6-35B-A3B-FP8",
        "vram_gib": 80,
        "tier": "H100 80GB",
        "note": "MoE (3B active): fast tokens, 262K context, vision input.",
    },
    {
        "label": "gpt-oss 120B",
        "model_id": "openai/gpt-oss-120b",
        "vram_gib": 80,
        "tier": "H100 80GB",
        "note": "OpenAI's large open-weight reasoner, sized for a single H100.",
    },
    # -- multi-GPU clusters ----------------------------------------------------
    {
        "label": "Hunyuan Hy3 (FP8)",
        "model_id": "tencent/Hy3-FP8",
        "vram_gib": 640,
        "tier": "8x H100 640GB",
        "note": "Tencent's 295B MoE (21B active), 256K context. Lambda "
                "benchmarks it on 4x B200 (not offered on-demand); on-demand "
                "the 8x H100 cluster carries it with tensor parallel 8.",
        "parameters": {"tensor_parallel": 8},
    },
    {
        "label": "Nemotron 3 Ultra (NVFP4)",
        "model_id": "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B-NVFP4",
        "vram_gib": 640,
        "tier": "8x H100 640GB",
        "note": "NVIDIA's 550B hybrid MoE (55B active), NVFP4 checkpoint "
                "(~330GB), 262K context. Lambda benchmarks it on HGX H100 "
                "with tensor parallel 8.",
        "parameters": {"tensor_parallel": 8},
    },
    {
        "label": "Step 3.7 Flash",
        "model_id": "stepfun-ai/Step-3.7-Flash",
        "vram_gib": 640,
        "tier": "8x H100 640GB",
        "note": "StepFun's 198B multimodal MoE (~11B active), 256K context. "
                "Lambda benchmarks 8x H100 and 8x A100 80GB; either "
                "on-demand cluster fits with tensor parallel 8.",
        "parameters": {"tensor_parallel": 8},
    },
    {
        "label": "GLM-5.2 (FP8)",
        "model_id": "zai-org/GLM-5.2-FP8",
        "vram_gib": 1440,
        "tier": "8x B200 1.4TB",
        "note": "Z.ai's 753B coding MoE (32B active): frontier-class. "
                "Lambda's benchmark hardware, one HGX B200 system, IS the "
                "8x B200 cluster (their deploy uses tensor parallel 8).",
        "parameters": {"tensor_parallel": 8},
    },
    {
        "label": "MiniMax M3",
        "model_id": "MiniMaxAI/MiniMax-M3",
        "vram_gib": 1440,
        "tier": "8x B200 1.4TB",
        "note": "MiniMax's 428B multimodal MoE (23B active) with 1M-token "
                "context. Lambda benchmarks one HGX B200 system (8x B200), "
                "tensor parallel 8.",
        "parameters": {"tensor_parallel": 8},
    },
    {
        "label": "Kimi K2.6",
        "model_id": "moonshotai/Kimi-K2.6",
        "vram_gib": 1440,
        "tier": "8x B200 1.4TB",
        "note": "Moonshot's 1.04T MoE (32B active), INT4 QAT experts, 256K "
                "context. Lambda benchmarks vLLM on one HGX B200 system "
                "(8x B200), tensor parallel 8.",
        "parameters": {"tensor_parallel": 8},
    },
    {
        "label": "Kimi K2.7 Code",
        "model_id": "moonshotai/Kimi-K2.7-Code",
        "vram_gib": 1440,
        "tier": "8x B200 1.4TB",
        "note": "Moonshot's 1T coding MoE (32B active) with vision input, "
                "256K context. Lambda recommends one HGX B200 system "
                "(8x B200), tensor parallel 8.",
        "parameters": {"tensor_parallel": 8},
    },
]


# The other end of the shelf: small open bases to distill INTO, not models
# to serve. A distilled student is only useful if it is cheap to run, so
# every entry here LoRA-trains on a single A10 (24GB) and serves on one
# afterwards. Same key names as MODEL_PRESETS so a client can reuse the
# same type, plus params_b (the size is the whole point when you are
# picking a student) and license (a distilled model inherits the base's
# terms, and the Qwen Research entries are not commercial-use).
#
# vram_gib here is the LoRA TRAINING floor at the modest sequence lengths
# these configs default to (weights in bf16 + activations + LoRA optimizer
# state), which is the number that decides whether the run OOMs. Serving
# the merged result needs roughly half of it. These are practical figures
# from the tier, not a fresh benchmark: treat them as "will it fit", not
# as a spec.
#
# Gated repos (Llama, Gemma) are absent for the same reason they are absent
# above: Manifold does not pass a HuggingFace token, so they would fail on
# first download rather than "just work".
STUDENT_PRESETS = [
    # -- tiny: minutes to train, narrow tasks -------------------------------
    {
        "label": "Qwen3 0.6B",
        "model_id": "Qwen/Qwen3-0.6B",
        "params_b": 0.6,
        "vram_gib": 8,
        "tier": "A10 24GB",
        "license": "Apache-2.0",
        "note": "The smallest student worth training. Good for one narrow "
                "job (tagging, routing, extraction); do not expect "
                "conversation.",
    },
    {
        "label": "Qwen2.5 1.5B Instruct",
        "model_id": "Qwen/Qwen2.5-1.5B-Instruct",
        "params_b": 1.5,
        "vram_gib": 12,
        "tier": "A10 24GB",
        "license": "Apache-2.0",
        "note": "Well-understood small instruct base; a safe default when "
                "the task needs a sentence back, not just a label.",
    },
    {
        "label": "SmolLM2 1.7B Instruct",
        "model_id": "HuggingFaceTB/SmolLM2-1.7B-Instruct",
        "params_b": 1.7,
        "vram_gib": 12,
        "tier": "A10 24GB",
        "license": "Apache-2.0",
        "note": "Trained for on-device use, so it stays fast on cheap "
                "hardware after you distill into it.",
    },
    {
        "label": "Qwen3 1.7B",
        "model_id": "Qwen/Qwen3-1.7B",
        "params_b": 1.7,
        "vram_gib": 12,
        "tier": "A10 24GB",
        "license": "Apache-2.0",
        "note": "Current-generation small Qwen. Start here if the teacher "
                "is also a Qwen model: the tokenizer and style already "
                "match.",
    },
    # -- 3B and up: the A10 training ceiling --------------------------------
    {
        "label": "Qwen2.5 3B Instruct",
        "model_id": "Qwen/Qwen2.5-3B-Instruct",
        "params_b": 3.0,
        "vram_gib": 22,
        "tier": "A10 24GB",
        "license": "Qwen Research (non-commercial)",
        "note": "The capable end of the A10 tier. Note the licence: the 3B "
                "is Qwen Research, not Apache, so a model distilled from it "
                "is not for commercial use.",
    },
    {
        "label": "Qwen3 4B Instruct",
        "model_id": "Qwen/Qwen3-4B-Instruct-2507",
        "params_b": 4.0,
        "vram_gib": 24,
        "tier": "A10 24GB",
        "license": "Apache-2.0",
        "note": "The A10 ceiling for LoRA: fits at short sequence lengths "
                "and micro_batch_size 1-2, and OOMs if you push either. "
                "Also servable straight from the serve presets.",
    },
]
