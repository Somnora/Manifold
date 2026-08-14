"""Phase 85: the model comes home.

Two halves, held to the same standard as Phase 84's.

The gguf-quantize template is a BASH script, not a python one, so the
executing-test doctrine extends: the real script out of `command:` is run
by a real bash, with `convert_hf_to_gguf.py` and `llama-quantize` replaced
by PATH shims that record the argv they were handed. Nothing here needs
llama.cpp, a GPU, or a model - only that the script calls the right tools
with the right arguments in the right order, which is exactly the class of
bug that shipped twice in this repo (the vllm argv shift, then Phase 84's
four).

backend/app/localmodels.py is pure, so it is tested directly. Its job is
path confinement on a write the backend performs, so the traversal cases
are not decoration.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from app import localmodels as lm
from app.dispatcher import coerce_parameters, render_docker_command
from app.templates import TemplateError, load_templates, parse_template

BUNDLED = Path(__file__).resolve().parents[2] / "templates"


@pytest.fixture(scope="module")
def quantize():
    loaded, errors = load_templates(BUNDLED)
    assert not errors, errors
    return loaded["gguf-quantize"]


# -- the bash script, EXECUTED ------------------------------------------------

_SHIM = """#!/usr/bin/env python3
import json, os, sys
log = os.environ["SHIM_LOG"]
calls = json.load(open(log)) if os.path.exists(log) else []
calls.append(sys.argv)
json.dump(calls, open(log, "w"))
# The converter is expected to produce its outfile; make it real so the
# script's own `mv` and `ls` see a file rather than failing on absence.
if "--outfile" in sys.argv:
    out = sys.argv[sys.argv.index("--outfile") + 1]
    open(out, "w").write("GGUF-F16")
elif len(sys.argv) >= 3 and sys.argv[2].endswith(".gguf"):
    open(sys.argv[2], "w").write("GGUF-QUANT")
"""


def docker_argv(template, params, *, filesystem="fs", task_id="t"):
    """The argv the CONTAINER actually execs: ENTRYPOINT + COMMAND.

    This is the seam that broke at the real gate. `--entrypoint bash` with
    a command that itself began `bash -c ...` produced `bash bash -c ...`,
    where the second `bash` is a script FILE argument - exit 126, "cannot
    execute binary file". The old harness ran the extracted script directly
    with its own `bash -c`, so it proved the script was right while saying
    nothing about how the script gets invoked, and the template shipped
    broken with a green test.

    So: render the real docker command, split it the way a shell does, and
    rebuild exactly what docker would exec.
    """
    rendered = render_docker_command(
        template, coerce_parameters(template, params),
        filesystem=filesystem, task_id=task_id)
    parts = shlex.split(rendered)
    entrypoint = parts[parts.index("--entrypoint") + 1] \
        if "--entrypoint" in parts else None
    # The image is the last token before the command payload; every arg
    # before it is a docker flag or its value.
    image_at = parts.index(template.image)
    return ([entrypoint] if entrypoint else []) + parts[image_at + 1:]


def run_script(template, params, tmp_path, *, tools=("convert_hf_to_gguf.py",
                                                     "llama-quantize"),
               make_model=True, tokenizer=None):
    """Execute the container's real ENTRYPOINT + COMMAND with fake tools.

    Not the script in isolation: the whole invocation, so a mismatch
    between `entrypoint:` and `command:` fails here rather than on a
    billed GPU.
    """

    root = tmp_path / "fs"
    models = root / "data" / "models"
    models.mkdir(parents=True, exist_ok=True)
    if make_model:
        src = models / str(params.get("model_dir", "student"))
        src.mkdir(parents=True, exist_ok=True)
        (src / "config.json").write_text("{}")
        if tokenizer is not None:
            (src / "tokenizer_config.json").write_text(json.dumps(tokenizer))

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "calls.json"
    for tool in tools:
        path = bindir / tool
        path.write_text(_SHIM)
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)

    argv = docker_argv(template, params)
    # /data and /tmp/quant are container paths; redirect them into tmp_path
    # so the invocation can run unprivileged without touching the real root.
    argv = [a.replace("/data/models", str(models))
             .replace("/tmp/quant", str(tmp_path / "quant")) for a in argv]

    result = subprocess.run(
        argv, capture_output=True, text=True,
        env={**os.environ, **template.env,
             "PATH": f"{bindir}:{os.environ['PATH']}",
             "SHIM_LOG": str(log)},
    )
    calls = json.loads(log.read_text()) if log.exists() else []
    return result, calls, models


def test_quantize_calls_convert_then_quantize_with_the_right_argv(quantize,
                                                                  tmp_path):
    """The whole point: the model the user named reaches the converter, and
    the quant the user chose reaches the quantizer, in that order."""
    result, calls, models = run_script(
        quantize, {"model_dir": "my-student", "output_name": "student",
                   "quant": "Q4_K_M"}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 2, calls

    convert = calls[0]
    assert convert[0].endswith("convert_hf_to_gguf.py")
    # The converter reads a directory of SYMLINKS to the model, not the
    # model directory itself, so the tokenizer fixup below can correct one
    # metadata file without touching the user's weights. What matters is
    # that it carries the user's model - and is not the literal "manifold"
    # that $0 holds, which is the vllm-serve bug in this template's shape.
    work = Path(convert[1])
    assert work.name == "src-student", convert[1]
    assert (work / "config.json").resolve() == \
        (models / "my-student" / "config.json").resolve()
    assert convert[convert.index("--outtype") + 1] == "f16"

    quant = calls[1]
    assert quant[0].endswith("llama-quantize")
    # llama-quantize <in> <out> <type>: positional and order-sensitive.
    assert quant[1].endswith("student-f16.gguf")
    assert quant[2] == str(models / "student.gguf")
    assert quant[3] == "Q4_K_M"
    assert (models / "student.gguf").exists()


def test_a_list_shaped_extra_special_tokens_is_normalised(quantize, tmp_path):
    """The trainer writes extra_special_tokens as a LIST; the transformers
    inside the llama.cpp image wants a MAPPING and dies on .keys() before
    conversion starts. Found at the 2026-08-14 real gate on a Qwen3
    student. The user's model must come through untouched."""
    result, calls, models = run_script(
        quantize, {"model_dir": "m", "output_name": "s"}, tmp_path,
        tokenizer={"extra_special_tokens": ["<|im_start|>", "<|im_end|>"],
                   "split_special_tokens": False})
    assert result.returncode == 0, result.stderr
    assert "normalised 2 extra_special_tokens" in result.stdout

    work = Path(calls[0][1])
    fixed = json.loads((work / "tokenizer_config.json").read_text())
    assert fixed["extra_special_tokens"] == {
        "extra_special_token_0": "<|im_start|>",
        "extra_special_token_1": "<|im_end|>"}
    assert fixed["split_special_tokens"] is False   # everything else kept

    # The model on the filesystem is untouched: still the original list.
    original = json.loads(
        (models / "m" / "tokenizer_config.json").read_text())
    assert original["extra_special_tokens"] == ["<|im_start|>", "<|im_end|>"]


def test_a_mapping_shaped_tokenizer_is_left_alone(quantize, tmp_path):
    result, calls, _ = run_script(
        quantize, {"model_dir": "m", "output_name": "s"}, tmp_path,
        tokenizer={"extra_special_tokens": {"pad": "<|pad|>"}})
    assert result.returncode == 0, result.stderr
    assert "normalised" not in result.stdout
    work = Path(calls[0][1])
    assert json.loads((work / "tokenizer_config.json").read_text())[
        "extra_special_tokens"] == {"pad": "<|pad|>"}


def test_quantize_honours_the_chosen_quant(quantize, tmp_path):
    _, calls, _ = run_script(
        quantize, {"model_dir": "m", "output_name": "s", "quant": "Q8_0"},
        tmp_path)
    assert calls[1][3] == "Q8_0"


def test_f16_skips_the_quantizer_entirely(quantize, tmp_path):
    """F16 is 'no quantization': converting and then quantizing to f16
    would be a slow copy."""
    result, calls, models = run_script(
        quantize, {"model_dir": "m", "output_name": "s", "quant": "F16"},
        tmp_path)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1, "the quantizer ran for an F16 request"
    assert (models / "s.gguf").read_text() == "GGUF-F16"


def test_an_unknown_quant_is_refused_before_any_work(quantize, tmp_path):
    result, calls, _ = run_script(
        quantize, {"model_dir": "m", "output_name": "s", "quant": "Q3_K_XL"},
        tmp_path)
    assert result.returncode == 2
    assert "quant must be one of" in result.stderr
    assert calls == [], "work started before the quant was validated"


def test_a_missing_model_fails_before_any_work(quantize, tmp_path):
    result, calls, _ = run_script(
        quantize, {"model_dir": "absent", "output_name": "s"}, tmp_path,
        make_model=False)
    assert result.returncode == 2
    assert "no model at" in result.stderr
    assert calls == []


@pytest.mark.parametrize("bad", ["../escape", "sub/dir", "", "a;rm -rf /"])
def test_path_shaped_names_are_refused(quantize, tmp_path, bad):
    """model_dir and output_name build container paths, so neither may
    carry a directory. Parameters are shlex-quoted into the docker command,
    so this is defence in depth, not the only guard."""
    result, calls, _ = run_script(
        quantize, {"model_dir": "m", "output_name": bad}, tmp_path)
    assert result.returncode == 2
    assert "output_name must use" in result.stderr
    assert calls == []


def test_keep_f16_is_off_by_default_and_works_when_asked(quantize, tmp_path):
    """The f16 intermediate is ~2 GB per billion parameters, so it is
    thrown away unless the user says otherwise."""
    _, _, models = run_script(
        quantize, {"model_dir": "m", "output_name": "s"}, tmp_path)
    assert not (models / "s-f16.gguf").exists()

    _, _, models2 = run_script(
        quantize, {"model_dir": "m", "output_name": "s", "keep_f16": True},
        tmp_path / "second")
    assert (models2 / "s-f16.gguf").exists()


def test_a_missing_toolchain_says_which_image_is_wrong(quantize, tmp_path):
    result, calls, _ = run_script(
        quantize, {"model_dir": "m", "output_name": "s"}, tmp_path, tools=())
    assert result.returncode == 3
    assert "llama.cpp full image" in result.stderr


# -- the rendered docker command ----------------------------------------------


def test_the_image_entrypoint_is_overridden(quantize):
    """llama.cpp's `full` image ships /app/tools.sh as ENTRYPOINT, which
    dispatches on --convert/--quantize and would eat this command as its
    own arguments - the vllm-serve failure, one image over."""
    assert quantize.entrypoint == "bash"
    cmd = render_docker_command(
        quantize, coerce_parameters(quantize, {"model_dir": "m"}),
        filesystem="fs", task_id="t1")
    assert "--entrypoint bash" in cmd
    assert "--user 0:0" in cmd          # NFS writes need uid 0
    assert "-v /lambda/nfs/fs/models:/data/models" in cmd
    # The $0 padding is present and is the literal, not a parameter.
    assert "' manifold " in cmd


def test_entrypoint_and_command_do_not_double_up(quantize):
    """ENTRYPOINT + COMMAND must form ONE valid invocation.

    `entrypoint: bash` plus a command starting `bash -c` execs
    `bash bash -c ...`, and bash reads the second `bash` as a script file:
    exit 126. The payload must therefore start at the flag, exactly as the
    serve templates start at `-c` under `entrypoint: python3`.
    """
    argv = docker_argv(quantize, {"model_dir": "m"})
    assert argv[0] == "bash"
    assert argv[1] == "-c", f"payload must start at -c, got {argv[1]!r}"
    assert argv[2].lstrip().startswith("set -e"), "argv[2] must be the script"
    # $0, then the four parameters in declared order.
    assert argv[3] == "manifold"
    assert len(argv) == 4 + len(quantize.parameters)
    # The binary must appear exactly once across the whole invocation.
    assert argv.count("bash") == 1


def test_quantize_declares_no_ports_so_it_is_not_a_server(quantize):
    """A template with ports is a server, and a server cannot be a
    depends_on parent. This one is meant to chain after lora-merge."""
    assert quantize.ports == []


# -- the local library (pure) -------------------------------------------------


def test_a_pulled_name_must_be_a_bare_gguf_filename():
    assert lm.validate_gguf_name("student.gguf") == "student.gguf"
    for bad in ("../../etc/passwd.gguf", "sub/dir.gguf", ".hidden.gguf",
                "student.safetensors", ""):
        with pytest.raises(lm.LocalModelError):
            lm.validate_gguf_name(bad)


def test_the_destination_never_leaves_the_library(tmp_path):
    library = tmp_path / "models"
    library.mkdir()
    assert lm.destination(library, "student.gguf").parent == library.resolve()
    for escape in ("../outside.gguf", "a/b.gguf"):
        with pytest.raises(lm.LocalModelError):
            lm.destination(library, escape)


def test_a_partial_pull_is_not_mistaken_for_a_model(tmp_path):
    """A transfer that dies halfway must not leave a plausible .gguf."""
    (tmp_path / "models").mkdir()
    final = lm.destination(tmp_path / "models", "student.gguf")
    assert lm.partial_path(final).name == "student.gguf.partial"
    assert lm.partial_path(final).parent == final.parent


def test_ollama_names_are_slugs_without_tags():
    assert lm.validate_ollama_name("My-Student") == "my-student"
    for bad in ("student:latest", "a", "", "Student/One", "-lead"):
        with pytest.raises(lm.LocalModelError):
            lm.validate_ollama_name(bad)


def test_the_suggested_name_always_passes_validation():
    for raw in ("student.gguf", "My Student v2.gguf", "----.gguf",
                "Qwen2.5-1.5B-distill.gguf"):
        assert lm.validate_ollama_name(lm.default_ollama_name(raw))


def test_a_too_short_stem_falls_back_rather_than_suggesting_an_invalid_name():
    """Ollama names need two characters, so a one-letter file cannot lend
    its name. The fallback is why "s.gguf" installs as "student"."""
    assert lm.default_ollama_name("s.gguf") == "student"
    assert lm.default_ollama_name("--.gguf") == "student"


def test_the_modelfile_names_the_file_and_nothing_else(tmp_path):
    """No TEMPLATE or PARAMETER lines: a .gguf carries its own chat
    template, and one guessed here would silently override a correct one -
    which reads to the user as 'the distilled model babbles'."""
    text = lm.modelfile_text(tmp_path / "student.gguf")
    assert text.strip() == f'FROM "{tmp_path / "student.gguf"}"'
    assert "TEMPLATE" not in text and "PARAMETER" not in text


def test_a_modelfile_cannot_be_injected_with_a_newline(tmp_path):
    with pytest.raises(lm.LocalModelError):
        lm.modelfile_text(Path("/tmp/x\nPARAMETER num_ctx 1"))


def test_install_is_an_argv_list_never_a_shell_string(tmp_path):
    argv = lm.install_argv("/usr/local/bin/ollama", "student",
                           tmp_path / "Modelfile")
    assert argv[:3] == ["/usr/local/bin/ollama", "create", "student"]
    assert argv[3] == "-f"
    assert all(isinstance(a, str) for a in argv)


def test_ollama_list_parses_and_tolerates_a_reformat():
    out = ("NAME                ID           SIZE     MODIFIED\n"
           "llama3:latest       365c0bd3     4.7 GB   2 days ago\n"
           "my-student:latest   abc12345     1.9 GB   1 minute ago\n")
    assert lm.parse_ollama_list(out) == ["llama3", "my-student"]
    assert lm.parse_ollama_list("") == []
    assert lm.is_installed("My-Student", ["my-student"])
    assert not lm.is_installed("other", ["my-student"])


# -- the routes ---------------------------------------------------------------
#
# A fake `ollama` on PATH, never the real one: these tests must pass on a
# machine that has never installed it, and must not create models on a
# machine that has.

FAKE_OLLAMA = """#!/usr/bin/env python3
import json, os, sys
log = os.environ["OLLAMA_LOG"]
calls = json.load(open(log)) if os.path.exists(log) else []
calls.append(sys.argv[1:])
json.dump(calls, open(log, "w"))
if sys.argv[1:2] == ["list"]:
    print(os.environ.get("OLLAMA_LIST", "NAME  ID  SIZE  MODIFIED"))
elif sys.argv[1:2] == ["create"]:
    if os.environ.get("OLLAMA_FAIL"):
        print("Error: invalid GGUF magic", file=sys.stdout)
        sys.exit(1)
    print("success")
"""


@pytest.fixture
def library(client, tmp_path):
    """Point the app's model library at a temp dir for the whole test."""
    lib = tmp_path / "library"
    lib.mkdir()
    client.app.state.model_library = lib
    return lib


@pytest.fixture
def fake_ollama(monkeypatch, tmp_path):
    """Install a fake ollama and make brains.which_with_fallback find it."""
    bindir = tmp_path / "ollama-bin"
    bindir.mkdir()
    exe = bindir / "ollama"
    exe.write_text(FAKE_OLLAMA)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
    log = tmp_path / "ollama-calls.json"
    monkeypatch.setenv("OLLAMA_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return {"exe": exe, "log": log,
            "calls": lambda: json.loads(log.read_text()) if log.exists() else []}


def test_local_models_lists_an_empty_library_without_ollama(client, library,
                                                            monkeypatch):
    """No Ollama is a normal answer, not an error: the library is still
    real, and the UI degrades to a path and a command."""
    monkeypatch.setattr("app.brains.which_with_fallback", lambda name: None)
    body = client.get("/models/local").json()
    assert body["models"] == []
    assert body["ollama_available"] is False
    assert body["library_path"] == str(library)


def test_local_models_reports_size_and_installed_state(client, library,
                                                       fake_ollama,
                                                       monkeypatch):
    (library / "my-student.gguf").write_bytes(b"GGUF" * 100)
    (library / "notes.txt").write_text("not a model")
    monkeypatch.setenv(
        "OLLAMA_LIST",
        "NAME               ID        SIZE    MODIFIED\n"
        "my-student:latest  abc123    1.9 GB  1 minute ago")
    body = client.get("/models/local").json()
    assert [m["name"] for m in body["models"]] == ["my-student.gguf"]
    assert body["models"][0]["size_bytes"] == 400
    assert body["models"][0]["installed"] is True
    assert body["ollama_available"] is True


def test_pull_brings_the_file_home_and_audits_it(client, library):
    from tests.test_reconcile import launch_connected
    _, instance_id = launch_connected(client)
    store = client.app.state.orchestrator.connections[instance_id] \
        .ssh_connection().sftp_files
    store["/lambda/nfs/manifold-data/models/student.gguf"] = b"GGUF-BYTES" * 10

    resp = client.post("/models/pull", json={"instance_id": instance_id,
                                             "name": "student.gguf"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bytes"] == 100
    assert (library / "student.gguf").read_bytes() == b"GGUF-BYTES" * 10
    assert body["suggested_ollama_name"] == "student"
    # No .partial survives a completed pull.
    assert list(library.glob("*.partial")) == []
    assert any(e["action"] == "model_pull"
               for e in client.get("/audit").json()["entries"])


def test_pull_refuses_a_path_and_a_non_gguf(client, library):
    from tests.test_reconcile import launch_connected
    _, instance_id = launch_connected(client)
    for bad in ("../../../etc/passwd.gguf", "sub/dir.gguf",
                "student.safetensors"):
        resp = client.post("/models/pull",
                           json={"instance_id": instance_id, "name": bad})
        assert resp.status_code == 422, bad
    assert list(library.iterdir()) == []


def test_pull_says_where_to_look_when_the_file_is_not_there(client, library):
    from tests.test_reconcile import launch_connected
    _, instance_id = launch_connected(client)
    resp = client.post("/models/pull", json={"instance_id": instance_id,
                                             "name": "absent.gguf"})
    assert resp.status_code == 404
    assert "gguf-quantize writes" in resp.json()["detail"]


def test_pull_needs_a_connected_instance(client, library):
    resp = client.post("/models/pull", json={"instance_id": "i-nope",
                                             "name": "student.gguf"})
    assert resp.status_code == 409
    assert "managed SSH connection" in resp.json()["detail"]


def test_install_registers_the_model_and_returns_its_brain_ref(
        client, library, fake_ollama):
    (library / "my-student.gguf").write_bytes(b"GGUF")
    resp = client.post("/models/install", json={"name": "my-student.gguf"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The whole point of installing: it is now a brain, through machinery
    # that already existed. The ref must be spelled the way GET /brains
    # will list it - Ollama appends :latest, and returning the bare name
    # sent the user hunting for a picker entry under the wrong spelling.
    assert body["brain_ref"] == "local:ollama/my-student:latest"

    create = [c for c in fake_ollama["calls"]() if c[:1] == ["create"]]
    assert len(create) == 1
    assert create[0][1] == "my-student"
    assert create[0][2] == "-f"
    modelfile = Path(create[0][3])
    assert modelfile.read_text().strip() == \
        f'FROM "{library / "my-student.gguf"}"'


def test_install_without_ollama_hands_back_the_command(client, library,
                                                       monkeypatch):
    """A missing Ollama must not read as 'your model is broken'. It is
    already the user's file; say where it is and what to run."""
    (library / "my-student.gguf").write_bytes(b"GGUF")
    monkeypatch.setattr("app.brains.which_with_fallback", lambda name: None)
    resp = client.post("/models/install", json={"name": "my-student.gguf"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "already yours at" in detail
    assert "ollama create my-student -f" in detail


def test_install_refuses_to_overwrite_without_being_told(client, library,
                                                         fake_ollama,
                                                         monkeypatch):
    (library / "my-student.gguf").write_bytes(b"GGUF")
    monkeypatch.setenv("OLLAMA_LIST",
                       "NAME  ID  SIZE\nmy-student:latest  a  1 GB")
    resp = client.post("/models/install", json={"name": "my-student.gguf"})
    assert resp.status_code == 409
    assert "already has a model named" in resp.json()["detail"]

    ok = client.post("/models/install",
                     json={"name": "my-student.gguf", "overwrite": True})
    assert ok.status_code == 200


def test_install_surfaces_what_ollama_actually_said(client, library,
                                                    fake_ollama, monkeypatch):
    (library / "my-student.gguf").write_bytes(b"not really a gguf")
    monkeypatch.setenv("OLLAMA_FAIL", "1")
    resp = client.post("/models/install", json={"name": "my-student.gguf"})
    assert resp.status_code == 502
    assert "invalid GGUF magic" in resp.json()["detail"]


def test_install_needs_the_model_to_be_in_the_library(client, library,
                                                      fake_ollama):
    resp = client.post("/models/install", json={"name": "absent.gguf"})
    assert resp.status_code == 404
    assert "Pull it from the instance first" in resp.json()["detail"]
