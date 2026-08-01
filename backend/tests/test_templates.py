"""Template registry: schema validation and the volume-separation rule."""

from pathlib import Path

import pytest

from app.templates import (
    TemplateError,
    floating_tag_warning,
    load_templates,
    parse_template,
)

REPO_TEMPLATES = Path(__file__).resolve().parent.parent.parent / "templates"


def test_reference_templates_load_cleanly():
    templates, errors = load_templates(REPO_TEMPLATES)
    assert errors == {}
    # The registry grows over time; the three reference templates must exist.
    assert {"vllm-serve", "whisper-batch", "axolotl-finetune"} <= set(templates)
    # Every model-downloading reference template maps the HF cache to
    # persistent storage (gpu-smoke pulls no models, so it is exempt).
    for name in ("vllm-serve", "whisper-batch", "axolotl-finetune"):
        t = templates[name]
        hf = [v for v in t.volumes if v.container == "/root/.cache/huggingface"]
        assert hf, f"{name} must map the HuggingFace cache to persistent"
        assert hf[0].host.startswith("{persistent}")


def test_parameter_schema_is_served():
    templates, _ = load_templates(REPO_TEMPLATES)
    vllm = templates["vllm-serve"].to_api()
    model = next(p for p in vllm["parameters"] if p["name"] == "model_id")
    assert model["required"] is True          # no default
    ctx = next(p for p in vllm["parameters"] if p["name"] == "max_context")
    assert ctx == {
        "name": "max_context", "type": "integer",
        "description": "Maximum context length in tokens",
        "default": 8192, "required": False,
    }


def test_lora_merge_closes_the_distill_loop():
    """lora-merge reads the adapter from outputs/ and writes to models/, and
    vllm-serve mounts that same models/ dir so the result is servable by path.
    This is the contract the distill guide (step 5) now depends on."""
    templates, errors = load_templates(REPO_TEMPLATES)
    assert errors == {}
    merge = templates["lora-merge"]

    # Required input, defaulted output name, optional base override.
    params = {p.name: p for p in merge.parameters}
    assert params["adapter_dir"].required is True
    assert params["output_name"].default == "merged"
    assert params["base_model"].default == ""      # empty = read from adapter

    # The adapter source is mounted read-only (a merge never mutates it);
    # the merged model is written to the shared models/ directory.
    out = next(v for v in merge.volumes if v.container == "/data/models")
    assert out.host == "{persistent}/models"
    src = next(v for v in merge.volumes if v.container == "/data/output")
    assert src.host == "{persistent}/outputs" and src.read_only

    # BOTH serve engines must mount the SAME models/ dir, or
    # model_id=/data/models/<name> would not resolve inside the container.
    for engine in ("vllm-serve", "sglang-serve"):
        serve = templates[engine]
        mnt = next(v for v in serve.volumes if v.container == "/data/models")
        assert mnt.host == "{persistent}/models" and mnt.read_only, engine


ILLEGAL_MOUNT = """
name: evil
description: tries to read the host
image: alpine
command: cat /host-etc/passwd
volumes:
  - host: /etc
    container: /host-etc
"""


# -- floating image tags (drift guard) -------------------------------------------

def test_floating_tags_are_flagged():
    assert floating_tag_warning("vllm/vllm-openai:latest")
    assert floating_tag_warning("axolotlai/axolotl:main-latest")
    assert floating_tag_warning("repo:nightly")
    assert floating_tag_warning("alpine")               # no tag -> :latest
    assert floating_tag_warning("localhost:5000/img")   # port, still no tag


def test_pinned_tags_and_digests_are_clean():
    for image in (
        "pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime",
        "nvcr.io/nvidia/tao/tao-toolkit:5.5.0-tf2",
        "nvidia/cuda:12.4.1-base-ubuntu22.04",
        "repo@sha256:abc123",
        "localhost:5000/img:2.0",                       # port not mistaken
    ):
        assert floating_tag_warning(image) is None, image


def test_template_surfaces_floating_tag_warning():
    text = """
name: drifty
description: rides latest
image: some/image:latest
command: "true"
"""
    t = parse_template(text)
    assert any("floating" in w for w in t.warnings)
    assert any("floating" in w for w in t.to_api()["warnings"])


def test_reference_template_drift_flags():
    templates, _ = load_templates(REPO_TEMPLATES)
    # Version-pinned on purpose: no drift warning.
    assert templates["whisper-batch"].warnings == []
    # Rides :latest deliberately (documented breadcrumb) -> flagged.
    assert any("floating" in w for w in templates["sdxl-generate"].warnings)


def test_illegal_mount_rejected():
    with pytest.raises(TemplateError, match="illegal mount '/etc'"):
        parse_template(ILLEGAL_MOUNT)


def test_home_mount_rejected():
    text = ILLEGAL_MOUNT.replace("/etc", "/home/ubuntu/.ssh")
    with pytest.raises(TemplateError, match="illegal mount"):
        parse_template(text)


def test_sanctioned_roots_accepted():
    text = """
name: fine
description: uses only sanctioned roots
image: alpine
command: "true"
volumes:
  - host: /workspace/ephemeral/scratch
    container: /scratch
  - host: "{persistent}/outputs"
    container: /out
"""
    t = parse_template(text)
    assert [v.host for v in t.volumes] == [
        "/workspace/ephemeral/scratch", "{persistent}/outputs",
    ]


def test_undeclared_placeholder_rejected():
    text = """
name: typo
description: command references a parameter that is not declared
image: alpine
command: echo {{model_idd}}
parameters:
  - name: model_id
    type: string
    description: the model
"""
    with pytest.raises(TemplateError, match="model_idd"):
        parse_template(text)


def test_unknown_parameter_type_rejected():
    text = """
name: badtype
description: bad parameter type
image: alpine
command: "true"
parameters:
  - name: x
    type: float64
    description: nope
"""
    with pytest.raises(TemplateError, match="float64"):
        parse_template(text)


def test_broken_template_surfaces_in_errors(tmp_path):
    (tmp_path / "good.yaml").write_text("""
name: good
description: fine
image: alpine
command: "true"
""")
    (tmp_path / "bad.yaml").write_text(ILLEGAL_MOUNT)
    templates, errors = load_templates(tmp_path)
    assert "good" in templates
    assert "bad.yaml" in errors
    assert "illegal mount" in errors["bad.yaml"]


def test_templates_endpoint_serves_schemas(client):
    body = client.get("/templates").json()
    names = {t["name"] for t in body["templates"]}
    assert {"vllm-serve", "whisper-batch", "axolotl-finetune", "sglang-serve"} <= names
    assert body["errors"] == {}


def test_vllm_serve_multi_lora_and_speculative_rendering():
    templates, errors = load_templates(REPO_TEMPLATES)
    assert errors == {}
    vllm = templates["vllm-serve"]

    # Test parameter schemas
    param_names = {p.name for p in vllm.parameters}
    assert {"model_id", "enable_lora", "max_loras", "lora_modules", "speculative_model"} <= param_names

    # Test volumes include /data/outputs for LoRA adapters
    out_mount = next(v for v in vllm.volumes if v.container == "/data/outputs")
    assert out_mount.host == "{persistent}/outputs" and out_mount.read_only

    # Render template command
    from app.dispatcher import render_docker_command
    params = {
        "model_id": "Qwen/Qwen2.5-7B-Instruct",
        "max_context": 8192,
        "port": 8080,
        "tensor_parallel": 1,
        "tool_call_parser": "hermes",
        "enable_lora": True,
        "max_loras": 4,
        "max_cpu_loras": 8,
        "lora_modules": "coder=/data/outputs/distill-v1",
        "speculative_model": "Qwen/Qwen2.5-0.5B-Instruct",
        "num_speculative_tokens": 5,
    }
    docker_cmd = render_docker_command(vllm, params, filesystem="testfs", task_id="t1")
    assert "vllm-openai:latest" in docker_cmd
    assert "enable_lora" in docker_cmd
    assert "coder=/data/outputs/distill-v1" in docker_cmd
    assert "speculative_model" in docker_cmd


def test_sglang_serve_multi_lora_rendering():
    templates, errors = load_templates(REPO_TEMPLATES)
    assert errors == {}
    sglang = templates["sglang-serve"]

    # Test parameter schemas
    param_names = {p.name for p in sglang.parameters}
    assert {"model_id", "lora_paths", "mem_fraction_static"} <= param_names

    # Test volumes include /data/outputs for LoRA adapters
    out_mount = next(v for v in sglang.volumes if v.container == "/data/outputs")
    assert out_mount.host == "{persistent}/outputs" and out_mount.read_only

    # Render template command
    from app.dispatcher import render_docker_command
    params = {
        "model_id": "Qwen/Qwen2.5-7B-Instruct",
        "max_context": 8192,
        "port": 8080,
        "tensor_parallel": 1,
        "lora_paths": "coder=/data/outputs/distill-v1",
        "mem_fraction_static": 0.88,
    }
    docker_cmd = render_docker_command(sglang, params, filesystem="testfs", task_id="t2")
    assert "sglang:latest" in docker_cmd
    assert "lora_paths" in docker_cmd
    assert "coder=/data/outputs/distill-v1" in docker_cmd

