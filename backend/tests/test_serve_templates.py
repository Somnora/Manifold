"""The serve templates' embedded bootstrap scripts, EXECUTED.

Two bugs lived in vllm-serve/sglang-serve because nothing ever ran their
`python3 -c` bootstrap code: a stray leading token (introduced by commit
8c34a79) shifted every argv slot by one, binding model_id to the literal
string "manifold" - and the current serve images' opinionated ENTRYPOINTs
consumed the whole command as their own arguments before the script could
even start (found at the 2026-08-14 real gate; the argv bug hid behind
the entrypoint bug). Rendered-string assertions can never catch either
class: these tests extract the script from the loaded template and run it
with a stubbed os.execvp, asserting the FINAL argv the container would
exec.
"""

import os
import re
import sys
from pathlib import Path

import pytest

from app.dispatcher import coerce_parameters, render_docker_command
from app.templates import load_templates, parse_template

BUNDLED = Path(__file__).resolve().parents[2] / "templates"


@pytest.fixture(scope="module")
def templates():
    loaded, errors = load_templates(BUNDLED)
    assert not errors, errors
    return loaded


def run_bootstrap(template, argv_values):
    """Execute the template's embedded `-c` script exactly as the
    container would: sys.argv[0] is '-c', the values follow, and
    os.execvp is stubbed to capture the final command."""
    m = re.match(r"-c '(.*)' \{\{", template.command, re.DOTALL)
    assert m, f"{template.name}: command is not `-c '<script>' {{{{args}}}}`"
    script = m.group(1)

    captured = {}
    real_argv, real_execvp = sys.argv, os.execvp
    sys.argv = ["-c"] + [str(v) for v in argv_values]
    os.execvp = lambda file, args: captured.update(file=file, args=list(args))
    try:
        exec(compile(script, f"<{template.name} bootstrap>", "exec"), {})
    finally:
        sys.argv, os.execvp = real_argv, real_execvp
    assert captured, f"{template.name}: bootstrap never reached execvp"
    return captured


def vllm_args(**overrides):
    """The template's parameter order, with defaults coerced the same way
    the dispatcher does."""
    base = {"model_id": "Qwen/Qwen2.5-7B-Instruct"}
    base.update(overrides)
    return base


def ordered_values(template, params):
    coerced = coerce_parameters(template, params)
    return [coerced[p.name] for p in template.parameters]


# -- vllm-serve ---------------------------------------------------------------


def test_vllm_bootstrap_serves_the_requested_model(templates):
    t = templates["vllm-serve"]
    got = run_bootstrap(t, ordered_values(t, vllm_args()))
    assert got["file"] == "vllm"
    # The argv-shift bug made this "manifold"; it must be the model.
    assert got["args"][:3] == ["vllm", "serve", "Qwen/Qwen2.5-7B-Instruct"]
    assert got["args"][3:5] == ["--max-model-len", "8192"]
    assert "--port" in got["args"] and "8080" in got["args"]
    assert "--enable-auto-tool-choice" in got["args"]
    # No lora/speculative flags unless asked.
    assert "--enable-lora" not in got["args"]
    assert "--speculative-model" not in got["args"]


def test_vllm_bootstrap_lora_and_speculative_branches(templates):
    t = templates["vllm-serve"]
    got = run_bootstrap(t, ordered_values(t, vllm_args(
        enable_lora=True, max_loras=4, lora_modules="p1=/a,p2=/b",
        speculative_model="Qwen/Qwen2.5-0.5B", num_speculative_tokens=5)))
    a = got["args"]
    assert "--enable-lora" in a
    assert a[a.index("--max-loras") + 1] == "4"
    assert a.count("--lora-modules") == 2
    assert a[a.index("--speculative-model") + 1] == "Qwen/Qwen2.5-0.5B"
    assert a[a.index("--num-speculative-tokens") + 1] == "5"


# -- sglang-serve -------------------------------------------------------------


def test_sglang_bootstrap_serves_the_requested_model(templates):
    t = templates["sglang-serve"]
    got = run_bootstrap(t, ordered_values(
        t, {"model_id": "meta-llama/Llama-3.1-8B-Instruct"}))
    assert got["file"] == "python3"
    a = got["args"]
    assert a[:3] == ["python3", "-m", "sglang.launch_server"]
    assert a[a.index("--model-path") + 1] == "meta-llama/Llama-3.1-8B-Instruct"
    assert "--port" in a


# -- the entrypoint override --------------------------------------------------


def test_serve_templates_override_the_image_entrypoint(templates):
    """Both serve images ship ENTRYPOINTs that would consume the command
    as their own arguments; the renderer must force python3."""
    for name in ("vllm-serve", "sglang-serve"):
        t = templates[name]
        assert t.entrypoint == "python3", name
        cmd = render_docker_command(
            t, coerce_parameters(t, {"model_id": "m"}),
            filesystem="fs", task_id="tX")
        assert "--entrypoint python3" in cmd, name
        # The command payload begins at -c: python3 receives the script.
        assert " vllm/vllm-openai" in cmd or " lmsysorg/sglang" in cmd
        assert re.search(r"--entrypoint python3 .*-c '", cmd, re.DOTALL), name
        # The poisoned token stays dead.
        assert "' manifold " not in cmd, name


def test_entrypoint_must_be_a_single_token():
    with pytest.raises(Exception) as exc:
        parse_template(
            "name: x\ndescription: d\nimage: i\n"
            "entrypoint: python3 -u\ncommand: c\n",
            source="x.yaml")
    assert "single binary" in str(exc.value)
