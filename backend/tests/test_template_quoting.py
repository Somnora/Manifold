"""Regression: env-script templates must survive the INSTANCE shell.

Several templates ship their script in an env var ($PYCODE / $RUNNER) and run
it in the container. The bug (fixed 2026-07-12): if the command references the
var OUTSIDE single quotes, the instance shell that runs `docker run ...`
expands it to EMPTY (the var is set in the container, not on the instance),
so the container runs an empty program. llm-synthesize and script-run were
silent no-ops; sdxl mangled multi-word params.

This test simulates the instance shell with a fake `docker` that records the
argv it receives, then asserts the container command still holds the LITERAL
$VAR (host did not expand it) and that multi-word params arrive intact. It
fails loudly if any template regresses to the un-quoted form.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from app.dispatcher import coerce_parameters, render_docker_command
from app.templates import load_templates

REPO_TEMPLATES = Path(__file__).resolve().parent.parent.parent / "templates"
TEMPLATES, _ = load_templates(REPO_TEMPLATES)

_FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, sys
json.dump(sys.argv[1:], open(os.environ["DOCKER_ARGV_OUT"], "w"))
"""

# (template, image, env-var, params, a multi-word param value to track)
CASES = [
    ("whisper-batch", "pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime", "PYCODE",
     {"model_size": "large-v3", "language": "en"}, "large-v3"),
    ("llm-synthesize", "python:3.11-slim", "PYCODE",
     {"input_path": "d.jsonl", "instruction": "extract key facts"},
     "extract key facts"),
    ("script-run", "python:3.11-slim", "RUNNER",
     {"script": "run.py", "args": "a b c"}, "a b c"),
    ("sdxl-generate", "huggingface/transformers-pytorch-gpu:latest", "PYCODE",
     {"prompt": "a red cat on a mat"}, "a red cat on a mat"),
    ("lora-merge", "axolotlai/axolotl:main-latest", "MERGE_PY",
     {"adapter_dir": "distill-v1", "output_name": "merged",
      "base_model": "Qwen/Qwen3-8B"}, "Qwen/Qwen3-8B"),
    # The Phase 84 templates belong in the SHARED harness, not only in their
    # own module: this table is what a future template edit gets read
    # against, and a shape that is only covered elsewhere is a shape the next
    # editor will not know to preserve. llm-synthesize's entry above passes
    # only its two original parameters, so its four appended positionals are
    # exercised here through the templates that carry the same tail shape.
    ("llm-judge", "python:3.11-slim", "PYCODE",
     {"input_name": "kept-tags.jsonl", "criteria": "names the shot type"},
     "names the shot type"),
    ("llm-eval", "axolotlai/axolotl:main-latest", "EVAL_PY",
     {"eval_name": "eval-d.jsonl", "student_path": "distilled",
      "instruction": "tag the shot"}, "tag the shot"),
]


def _host_parse(rendered: str, tmp_path: Path) -> list[str]:
    """Run the rendered command through a real shell with a fake `docker`,
    returning the argv docker actually received. PYCODE/RUNNER are NOT in the
    env, exactly like the instance shell."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(_FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    out = tmp_path / "argv.json"
    env = {"PATH": f"{bindir}:{os.environ['PATH']}",
           "DOCKER_ARGV_OUT": str(out)}
    subprocess.run(["/bin/bash", "-c", rendered], env=env, check=True)
    return json.loads(out.read_text())


@pytest.mark.parametrize("name,image,var,params,multiword", CASES)
def test_env_script_survives_the_instance_shell(
        name, image, var, params, multiword, tmp_path):
    template = TEMPLATES[name]
    rendered = render_docker_command(
        template, coerce_parameters(template, params),
        filesystem="manifold-data", task_id="t")
    argv = _host_parse(rendered, tmp_path)

    idx = argv.index(image)
    flags, container_cmd = argv[:idx], argv[idx + 1:]

    # (a) the script body is actually set on the container via -e VAR=...
    assert any(a.startswith(f"{var}=") and len(a) > len(var) + 8 for a in flags), \
        f"{name}: -e {var}=<body> missing"
    # (b) the container command keeps the LITERAL $VAR: the instance shell did
    #     NOT expand it to empty (that was the no-op bug).
    assert any(f"${var}" in a for a in container_cmd), \
        f"{name}: ${var} was host-expanded to empty (silent no-op regression)"
    # (c) a multi-word parameter arrives as ONE intact argument.
    assert multiword in container_cmd, \
        f"{name}: multi-word param '{multiword}' was split apart"
