"""Phase 84: the distill loop v2 - teacher, judge, scorecard.

Four pieces ship together and this module holds all four to one standard.
llm-synthesize gains a teacher, a holdout and an env_file without
disturbing the call it already had; llm-judge curates what the teacher
wrote; llm-eval scores the student that came out; and POST /distill/config
lets a brain write the axolotl config a person used to hand-type.

The doctrine here was learned expensively: a rendered-string assertion
proves nothing about ARGV. A stray token shifted every slot in vllm-serve
for a month while the rendered command still held every right fragment in
the wrong position (DECISIONS.md 2026-08-14). So the new templates are
EXECUTED - the real bash body out of `command:`, the real script out of
`env:`, against a stub OpenAI endpoint on loopback - and the assertions
are on the files and the numbers that come out the far end.

Nothing here touches a network it did not start itself, a GPU, a real
model, or a key.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import distill
from app.config import IdleSettings, TaskSettings
from app.dispatcher import coerce_parameters, render_docker_command
from app.main import create_app
from app.model_catalog import STUDENT_PRESETS
from app.model_client import ModelClientError
from tests.conftest import make_settings, mock_connect_fn

BUNDLED = Path(__file__).resolve().parents[2] / "templates"


@pytest.fixture(scope="module")
def templates():
    from app.templates import load_templates
    loaded, errors = load_templates(BUNDLED)
    assert not errors, f"bundled templates failed to load: {errors}"
    return loaded


# -- running a template the way the container does -----------------------------
#
# Two mechanisms, because the templates have two halves and both have had
# bugs. run_script executes the program out of `env:` with a chosen argv:
# that is where the positional contract lives. run_container executes the
# bash body out of `command:` with the program in its env var, which is
# additionally where the $0 padding, the "$@" expansion and the env_file
# prologue live. Neither is a substitute for the other.

_ARG_TOKENS = re.compile(r"\{\{(\w+)\}\}")


def command_argv(template, params) -> list[str]:
    """The positionals the container receives, in COMMAND-LINE order.

    Declaration order is NOT positional order (llm-synthesize declares
    input_path, instruction, output_name but passes input_path,
    output_name, instruction), so the order is read off the command text
    itself. That is what makes these tests catch an argv shift rather than
    quietly encode one.
    """
    coerced = coerce_parameters(template, params)
    return [str(coerced[name])
            for name in _ARG_TOKENS.findall(template.command)]


def local_script(template, var: str, data: Path) -> str:
    """The template's embedded program, with the container's /data mount
    pointed at a temp dir. Everything else runs verbatim."""
    script = template.env[var]
    return script.replace('"/data', f'"{data}').replace('f"/data', f'f"{data}')


def run_script(template, var, data, argv, *, env_extra=None, timeout=90):
    """Execute the program out of `env:` exactly as `python -c` presents
    it: sys.argv[0] is '-c' and the values follow."""
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-c", local_script(template, var, data),
         *[str(a) for a in argv]],
        capture_output=True, text=True, timeout=timeout, env=env)


def container_body(template) -> str:
    """The bash program out of `bash -c '<body>' manifold <args>`.

    The `manifold` token after the closing quote is bash's $0, pure
    padding: drop it and $1 becomes the shell's name and the first real
    parameter is silently eaten. Anchoring the regex on it keeps that
    contract visible here too.
    """
    match = re.match(r"bash -c '(.*?)' manifold ", template.command, re.DOTALL)
    assert match, f"{template.name}: command is not `bash -c '<body>' manifold ...`"
    return match.group(1)


def _interpreter_shims(tmp_path: Path) -> Path:
    """A bin dir where `python` and `python3` are this interpreter.

    The container body ends in `exec python -c "$PYCODE" "$@"`; running
    that line verbatim is the point, so the name has to resolve here the
    way it does in the image.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name in ("python", "python3"):
        shim = bindir / name
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        shim.chmod(0o755)
    return bindir


def run_container(template, var, data, argv, *, tmp_path,
                  env_extra=None, timeout=90):
    """Execute the WHOLE container command: the bash body from `command:`
    with the program in its env var and the positionals after the $0
    padding, exactly as the instance shell hands it to docker."""
    body = container_body(template).replace("/data", str(data))
    env = {**os.environ,
           "PATH": f"{_interpreter_shims(tmp_path)}:{os.environ['PATH']}",
           var: local_script(template, var, data)}
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", "-c", body, "manifold", *[str(a) for a in argv]],
        capture_output=True, text=True, timeout=timeout, env=env)


# -- a stub teacher/judge on loopback ------------------------------------------


@contextlib.contextmanager
def stub_openai(reply, *, model_id="stub/teacher", models_ready_after=0):
    """An OpenAI-compatible endpoint on 127.0.0.1 with a kernel-assigned
    port. `reply(payload)` returns the assistant content for one request.

    Threading, not the plain HTTPServer the older stub uses: a serial
    script is fine either way, but a script that ever overlaps two calls
    would look like a hang rather than a failure. Both sockets are closed
    on the way out (the older stub leaks one fd per test).
    """
    state = {"models_calls": 0, "chats": [], "auth": []}

    class Stub(http.server.BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["models_calls"] += 1
            if state["models_calls"] <= models_ready_after:
                self._send(503, {"error": "loading"})
            else:
                self._send(200, {"data": [{"id": model_id}]})

        def do_POST(self):
            payload = json.loads(
                self.rfile.read(int(self.headers["Content-Length"])))
            state["chats"].append(payload)
            state["auth"].append(self.headers.get("Authorization"))
            self._send(200, {"choices": [{"message": {
                "content": reply(payload)}}]})

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], state
    finally:
        server.shutdown()
        server.server_close()


# -- the two new templates are batch jobs, and they load -----------------------


def test_the_distill_templates_load_and_stay_batch_jobs(templates):
    """`ports:` is the dispatcher's ONLY definition of a server
    (_is_server is bool(template.ports)). A server can never be a
    depends_on parent, so a stray port declaration here would silently cut
    the chain these templates exist to form."""
    for name in ("llm-synthesize", "llm-judge", "llm-eval"):
        t = templates[name]
        assert t.ports == [], f"{name} would be dispatched as a server"
        # Host networking is how a container dials the teacher another job
        # published on the instance's own loopback; it is also mutually
        # exclusive with ports at load, so the two assertions agree.
        assert t.network == "host", name
        assert t.entrypoint == "", name
    # llm-eval rides the axolotl image (it already carries torch and
    # transformers) and that tag floats, exactly as axolotl-finetune and
    # lora-merge do. The drift is accepted; it must stay VISIBLE.
    assert any("floating" in w for w in templates["llm-eval"].warnings)
    assert templates["llm-synthesize"].warnings == []
    assert templates["llm-judge"].warnings == []


def test_positional_slots_are_pinned(templates):
    """The argv contract, written down.

    Every one of these scripts unpacks by position, so inserting a
    parameter anywhere but the end binds every later value to the wrong
    name - silently, since the rendered command still looks right.
    env_file must stay LAST in particular: the shell prologue reads the
    last positional to find it.
    """
    assert _ARG_TOKENS.findall(templates["llm-synthesize"].command) == [
        "input_path", "output_name", "instruction", "port", "limit",
        "output_format", "teacher_base_url", "teacher_model", "holdout_pct",
        "env_file"]
    assert _ARG_TOKENS.findall(templates["llm-judge"].command) == [
        "input_name", "criteria", "threshold", "port", "limit",
        "judge_base_url", "judge_model", "env_file"]
    assert _ARG_TOKENS.findall(templates["llm-eval"].command) == [
        "eval_name", "student_path", "output_name", "instruction", "port",
        "limit", "max_new_tokens", "student_device", "judge_base_url",
        "judge_model", "teacher_base_url", "teacher_model", "env_file"]


def test_no_credential_parameter_on_any_distill_template(templates):
    """The rendered docker command is written verbatim to the job log and
    the parameters are stored in SQLite. A key must therefore never be a
    parameter; it rides the user's own .env on the filesystem, named by
    env_file, and is sourced inside the container."""
    banned = {"key", "apikey", "token", "secret", "password", "auth",
              "credential"}
    for name in ("llm-synthesize", "llm-judge", "llm-eval"):
        names = [p.name for p in templates[name].parameters]
        assert not [n for n in names if banned & set(n.split("_"))], name
        assert "env_file" in names, name


# -- the new templates survive the INSTANCE shell ------------------------------
#
# test_template_quoting.py runs this check off a hand-maintained CASES list
# that the two new templates are not on, so they would otherwise ship with
# zero coverage of the bug that class exists for: if the command referenced
# $PYCODE outside single quotes, the shell that runs `docker run ...` would
# expand it to EMPTY (the var is set in the CONTAINER, not on the instance)
# and the container would run an empty program, exit 0, and report success.

_FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, sys
json.dump(sys.argv[1:], open(os.environ["DOCKER_ARGV_OUT"], "w"))
"""


def host_parse(rendered: str, tmp_path: Path) -> list[str]:
    """Run the rendered command through a real shell with a fake `docker`,
    returning the argv docker actually received. The script env vars are
    NOT in the environment, exactly like the instance shell."""
    bindir = tmp_path / "hostbin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(_FAKE_DOCKER)
    docker.chmod(0o755)
    out = tmp_path / "argv.json"
    subprocess.run(["/bin/bash", "-c", rendered], check=True,
                   env={"PATH": f"{bindir}:{os.environ['PATH']}",
                        "DOCKER_ARGV_OUT": str(out)})
    return json.loads(out.read_text())


@pytest.mark.parametrize("name,image,var,params,multiword", [
    ("llm-judge", "python:3.11-slim", "PYCODE",
     {"input_name": "kept-tags.jsonl", "criteria": "names the shot type"},
     "names the shot type"),
    ("llm-eval", "axolotlai/axolotl:main-latest", "EVAL_PY",
     {"eval_name": "eval-tags.jsonl", "student_path": "distilled",
      "instruction": "tag the shot in one line"},
     "tag the shot in one line"),
])
def test_new_scripts_survive_the_instance_shell(
        name, image, var, params, multiword, templates, tmp_path):
    template = templates[name]
    rendered = render_docker_command(
        template, coerce_parameters(template, params),
        filesystem="manifold-data", task_id="t")
    argv = host_parse(rendered, tmp_path)
    index = argv.index(image)
    flags, container_cmd = argv[:index], argv[index + 1:]

    # (a) the script body really is set on the container via -e VAR=<body>
    assert any(a.startswith(f"{var}=") and len(a) > len(var) + 8
               for a in flags), f"{name}: -e {var}=<body> missing"
    # (b) the container command keeps the LITERAL $VAR: the instance shell
    #     did not expand it to empty (that was the silent no-op bug).
    assert any(f"${var}" in a for a in container_cmd), \
        f"{name}: ${var} was host-expanded to empty (silent no-op regression)"
    # (c) a multi-word parameter arrives as ONE intact argument.
    assert multiword in container_cmd, \
        f"{name}: multi-word param '{multiword}' was split apart"
    # (d) the $0 padding is still there: drop it and bash binds the first
    #     real parameter to the shell's name, eating it silently.
    assert container_cmd[:2] == ["bash", "-c"]
    assert container_cmd[3] == "manifold"


# -- llm-synthesize v2: the call it already had -------------------------------


OLD_PARAMS = {"input_path": "research/raw.jsonl",
              "instruction": "Extract name and district as JSON.",
              "limit": 5}

# The pre-84 command was, verbatim:
#   bash -c 'exec python -c "$PYCODE" "$@"' manifold
#   {{input_path}} {{output_name}} {{instruction}} {{port}} {{limit}} {{output_format}}
# so this is exactly what the old six slots rendered to for OLD_PARAMS.
PRE_84_TAIL = ("manifold research/raw.jsonl synthesized "
               "'Extract name and district as JSON.' 8080 5 records")


def test_the_old_six_slots_render_unchanged(templates):
    """Byte-for-byte identity is NOT what shipped and must not be claimed:
    the command gained a shell prologue and four trailing args. What IS
    guaranteed is that the six original slots render exactly as they did,
    in the same order, and the four new ones are APPENDED empty - so a
    pre-84 job card and a v2 one bind the same values to the same names."""
    t = templates["llm-synthesize"]
    cmd = render_docker_command(t, coerce_parameters(t, OLD_PARAMS),
                                filesystem="manifold-data", task_id="syn1")
    assert PRE_84_TAIL in cmd
    # ...and nothing between the old tail and the end but the four new
    # defaults: empty URL, empty model, no holdout, no env file.
    assert cmd.endswith(PRE_84_TAIL + " '' '' 0 ''")
    # The prologue is the only other change, and it is inside the same
    # single-quoted bash -c so $PYCODE still reaches the CONTAINER's shell
    # unexpanded (a host-expanded $PYCODE runs an empty program and
    # "succeeds": the month-long silent no-op).
    assert 'exec python -c "$PYCODE" "$@"' in cmd
    assert "--network host" in cmd and "-p 127.0.0.1" not in cmd


def test_pre_84_argv_still_behaves_exactly_as_it_did(tmp_path):
    """The old six-argument call, run against a stub teacher: local
    loopback, model auto-discovered, one output file, no eval file."""
    from app.templates import load_templates
    t = load_templates(BUNDLED)[0]["llm-synthesize"]
    data = tmp_path / "data"
    (data / "research").mkdir(parents=True)
    (data / "research" / "raw.jsonl").write_text(
        json.dumps({"name": "Jane", "district": "TX-07"}) + "\n")

    with stub_openai(lambda p: json.dumps({"d": "TX-07"}),
                     model_id="stub/qwen-7b") as (port, state):
        result = run_script(t, "PYCODE", data, [
            "research/raw.jsonl", "points", "Extract name and district.",
            port, "0", "records"])

    assert result.returncode == 0, result.stderr
    assert "synthesizing with stub/qwen-7b" in result.stdout
    assert "1 synthesized, 0 failed" in result.stdout
    assert not (data / "synthesized" / "eval-points.jsonl").exists()
    # Auto-discovery still happens (the local teacher is unnamed), and no
    # Authorization header is invented when there is no key.
    assert state["models_calls"] == 1
    assert state["auth"] == [None]
    row = json.loads((data / "synthesized" / "points.jsonl").read_text())
    assert row["record"] == {"name": "Jane", "district": "TX-07"}
    assert row["synthesis_json"] == {"d": "TX-07"}


def _synthesize(tmp_path, rows, params, *, reply=None, env_extra=None,
                model_id="stub/teacher", teacher_port=None):
    """Run the FULL container command for llm-synthesize (bash prologue
    included) against a stub teacher, and hand back the temp filesystem."""
    from app.templates import load_templates
    t = load_templates(BUNDLED)[0]["llm-synthesize"]
    data = tmp_path / "data"
    (data / "research").mkdir(parents=True, exist_ok=True)
    (data / "research" / "raw.jsonl").write_text(rows)
    reply = reply or (lambda p: "answer for " + p["messages"][1]["content"])

    fixed = {"input_path": "research/raw.jsonl", "instruction": "Tag it."}
    with stub_openai(reply, model_id=model_id) as (port, state):
        argv = command_argv(t, {**fixed, "port": port, **params})
        if teacher_port is not None:
            # A remote teacher is named by URL; the loopback port must then
            # be ignored entirely.
            argv = command_argv(t, {
                **fixed, "port": 1,
                "teacher_base_url": f"http://127.0.0.1:{port}/v1", **params})
        result = run_container(t, "PYCODE", data, argv, tmp_path=tmp_path,
                               env_extra=env_extra)
    return result, data, state


def _lines(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_holdout_zero_writes_no_eval_file(tmp_path):
    rows = "".join(json.dumps({"i": i}) + "\n" for i in range(6))
    result, data, _ = _synthesize(tmp_path, rows, {"output_name": "d"})
    assert result.returncode == 0, result.stderr
    assert len(_lines(data / "synthesized" / "d.jsonl")) == 6
    assert not (data / "synthesized" / "eval-d.jsonl").exists()
    assert "rows held back" not in result.stdout


def test_holdout_ten_splits_deterministically(tmp_path):
    """Every Nth GENERATED row, not every Nth input row and never random:
    the same dataset must produce the same split on a re-run, or the
    scorecard is comparing against a moving target. With 20 rows and
    holdout_pct=10 that is rows 10 and 20, and the held rows keep the
    teacher's answer so llm-eval has something to grade against without
    paying to generate it twice."""
    rows = "".join(json.dumps({"i": i}) + "\n" for i in range(1, 21))
    result, data, _ = _synthesize(
        tmp_path, rows,
        {"output_name": "d", "holdout_pct": 10, "output_format": "alpaca"})
    assert result.returncode == 0, result.stderr

    train = _lines(data / "synthesized" / "d.jsonl")
    held = _lines(data / "synthesized" / "eval-d.jsonl")
    assert len(train) == 18 and len(held) == 2
    assert "2 rows held back (every 10th generated row, 10%)" in result.stdout
    # Exactly the 10th and 20th records, and no held row leaked into
    # training (that leak is what would make the scorecard a lie).
    assert [json.loads(r["input"])["i"] for r in held] == [10, 20]
    assert 10 not in [json.loads(r["input"])["i"] for r in train]
    # The teacher's answer travels with the held row.
    assert held[0]["output"].startswith("answer for ")


def test_holdout_never_empties_either_side(tmp_path):
    """Two rows and a 10% holdout means the every-10th rule never fires:
    an empty eval file would let llm-eval score 0/0 and print a
    meaningless 0%, so the job fails naming both counts instead."""
    rows = "".join(json.dumps({"i": i}) + "\n" for i in range(2))
    result, _, _ = _synthesize(tmp_path, rows,
                               {"output_name": "d", "holdout_pct": 10})
    assert result.returncode != 0
    assert "unusable split" in result.stderr
    assert "2 training rows and 0 eval rows" in result.stderr


def test_holdout_over_fifty_is_refused(tmp_path):
    """A holdout big enough to starve the training file is refused where
    it is free, not discovered as an axolotl failure on a paid GPU."""
    rows = json.dumps({"i": 1}) + "\n"
    result, _, _ = _synthesize(tmp_path, rows,
                               {"output_name": "d", "holdout_pct": 90})
    assert result.returncode != 0
    assert "holdout_pct must be between 0 and 50" in result.stderr


def test_named_remote_teacher_skips_discovery(tmp_path):
    """A named model on a public API is already up: dialling /v1/models
    first would be a wasted round trip against someone else's rate
    limit."""
    rows = json.dumps({"i": 1}) + "\n"
    result, data, state = _synthesize(
        tmp_path, rows,
        {"output_name": "d", "teacher_model": "vendor/big-model"},
        teacher_port=True)
    assert result.returncode == 0, result.stderr
    assert state["models_calls"] == 0
    assert state["chats"][0]["model"] == "vendor/big-model"
    assert "synthesizing with vendor/big-model" in result.stdout


def test_teacher_url_carrying_a_credential_is_refused(tmp_path):
    """The rendered docker command lands in the job log verbatim, so a key
    in the URL would be published. Refuse it and name the alternative."""
    from app.templates import load_templates
    t = load_templates(BUNDLED)[0]["llm-synthesize"]
    data = tmp_path / "data"
    (data / "research").mkdir(parents=True)
    (data / "research" / "raw.jsonl").write_text(json.dumps({"i": 1}) + "\n")
    argv = command_argv(t, {
        "input_path": "research/raw.jsonl",
        "instruction": "x",
        "teacher_base_url": "https://api.example.com/v1?key=sk-not-real"})
    result = run_container(t, "PYCODE", data, argv, tmp_path=tmp_path)
    assert result.returncode != 0
    assert "must carry no credentials" in result.stderr
    assert "MANIFOLD_TEACHER_API_KEY" in result.stderr


def test_env_file_carries_the_teacher_key_and_the_log_does_not(tmp_path,
                                                               templates):
    """The whole point of env_file: the key reaches the request as a
    Bearer header, having travelled only as a PATH on the command line."""
    secret = "sk-test-not-a-real-key-123"
    rows = json.dumps({"i": 1}) + "\n"
    data_env = tmp_path / "data" / "research"
    data_env.mkdir(parents=True, exist_ok=True)
    (data_env / ".env").write_text(f"MANIFOLD_TEACHER_API_KEY={secret}\n")

    result, _, state = _synthesize(
        tmp_path, rows,
        {"output_name": "d", "env_file": "research/.env",
         "teacher_model": "vendor/big-model"},
        teacher_port=True)
    assert result.returncode == 0, result.stderr
    assert state["auth"] == [f"Bearer {secret}"]
    # Neither the job log (the rendered command) nor the job's own output
    # ever sees the value - only the path.
    t = templates["llm-synthesize"]
    cmd = render_docker_command(
        t, coerce_parameters(t, {"input_path": "a.jsonl", "instruction": "x",
                                 "env_file": "research/.env"}),
        filesystem="manifold-data", task_id="t1")
    assert "research/.env" in cmd
    assert secret not in cmd
    assert secret not in result.stdout and secret not in result.stderr


def test_named_env_file_that_is_not_there_fails_before_python(tmp_path):
    """Absent (the default) and named-but-missing are deliberately
    different: the first is silence, the second is exit 2 with the path,
    before the script or a single teacher token is spent."""
    from app.templates import load_templates
    t = load_templates(BUNDLED)[0]["llm-synthesize"]
    data = tmp_path / "data"
    data.mkdir(parents=True)
    argv = command_argv(t, {"input_path": "a.jsonl", "instruction": "x",
                            "env_file": "research/nope.env"})
    result = run_container(t, "PYCODE", data, argv, tmp_path=tmp_path)
    assert result.returncode == 2
    assert "env file not found" in result.stderr
    assert "research/nope.env" in result.stderr


# -- llm-judge: the curation pass, EXECUTED ------------------------------------


def alpaca_rows(n, *, start=1):
    return "".join(json.dumps({
        "instruction": "Tag the shot.",
        "input": json.dumps({"i": i}),
        "output": f"teacher answer {i}",
    }) + "\n" for i in range(start, start + n))


def score_by_index(scores):
    """A judge whose verdict is decided by the row index in the task text,
    so a test can put a chosen score on a chosen row."""
    def reply(payload):
        user = payload["messages"][1]["content"]
        index = int(re.search(r'"i": (\d+)', user).group(1))
        score = scores[index]
        if isinstance(score, str):
            return score
        return json.dumps({"score": score, "reason": f"row {index}"})
    return reply


def _judge(tmp_path, rows, params, *, reply, env_extra=None,
           model_id="stub/judge", remote=False):
    from app.templates import load_templates
    t = load_templates(BUNDLED)[0]["llm-judge"]
    data = tmp_path / "data"
    (data / "synthesized").mkdir(parents=True, exist_ok=True)
    (data / "synthesized" / "train.jsonl").write_text(rows)

    with stub_openai(reply, model_id=model_id) as (port, state):
        base = {"input_name": "train.jsonl", "port": port}
        if remote:
            base = {"input_name": "train.jsonl", "port": 1,
                    "judge_base_url": f"http://127.0.0.1:{port}/v1"}
        argv = command_argv(t, {**base, **params})
        result = run_container(t, "PYCODE", data, argv, tmp_path=tmp_path,
                               env_extra=env_extra)
    return result, data, state


def test_judge_writes_scored_and_kept_files(tmp_path):
    """scored-* keeps every row plus the verdict; kept-* keeps only the
    rows that cleared the bar, in the SHAPE THE INPUT HAD so
    axolotl-finetune can train on it with no reshaping step."""
    scores = {1: 10, 2: 9, 3: 7, 4: 6, 5: 3, 6: 8}
    result, data, _ = _judge(tmp_path, alpaca_rows(6),
                             {"criteria": "names the shot type"},
                             reply=score_by_index(scores))
    assert result.returncode == 0, result.stderr

    scored = _lines(data / "synthesized" / "scored-train.jsonl")
    kept = _lines(data / "synthesized" / "kept-train.jsonl")
    assert len(scored) == 6
    assert [r["judge_score"] for r in scored] == [10, 9, 7, 6, 3, 8]
    assert scored[0]["judge_reason"] == "row 1"
    # Every scored row still carries the original keys beside the verdict.
    assert scored[0]["instruction"] == "Tag the shot."

    # Threshold defaults to 7 and the comparison is >=: a 7 is KEPT, a 6 is
    # dropped. That boundary is the whole meaning of the parameter.
    assert [json.loads(r["input"])["i"] for r in kept] == [1, 2, 3, 6]
    assert set(kept[0]) == {"instruction", "input", "output"}
    assert "judge_score" not in kept[0]


def test_judge_prints_a_histogram_and_the_counts(tmp_path):
    scores = {1: 10, 2: 9, 3: 7, 4: 6, 5: 3, 6: 8}
    result, _, _ = _judge(tmp_path, alpaca_rows(6),
                          {"criteria": "names the shot type"},
                          reply=score_by_index(scores))
    assert "score histogram (n=6):" in result.stdout
    assert "  10 | # 1" in result.stdout
    assert "   6 | # 1" in result.stdout
    assert "   5 |  0" in result.stdout
    assert ("kept 4 of 6 rows scoring >= 7, dropped 2, unscored 0"
            in result.stdout)
    # The kept file is alpaca-shaped, so it says so: that is the one thing
    # that decides whether axolotl can train on it at all.
    assert "kept rows are alpaca-shaped" in result.stdout


def test_judge_threshold_is_honoured_at_the_boundary(tmp_path):
    """threshold=9 with a 9 and an 8 present: the 9 survives, the 8 does
    not. Same data as above, so only the parameter moved."""
    scores = {1: 10, 2: 9, 3: 7, 4: 6, 5: 3, 6: 8}
    result, data, _ = _judge(tmp_path, alpaca_rows(6),
                             {"criteria": "c", "threshold": 9},
                             reply=score_by_index(scores))
    assert result.returncode == 0, result.stderr
    kept = _lines(data / "synthesized" / "kept-train.jsonl")
    assert [json.loads(r["input"])["i"] for r in kept] == [1, 2]
    assert "kept 2 of 6 rows scoring >= 9, dropped 4" in result.stdout


def test_judge_reads_fences_prose_and_refuses_to_guess(tmp_path):
    """Models fence their JSON however firmly you ask them not to, and
    sometimes answer in a sentence. Both parse. A reply that is neither is
    UNSCORED: never silently kept, never silently dropped."""
    scores = {
        1: '```json\n{"score": 9, "reason": "fenced"}\n```',
        2: "I would give this an 8 out of 10.",
        3: "the vibes are immaculate",
    }
    result, data, _ = _judge(tmp_path, alpaca_rows(3), {"criteria": "c"},
                             reply=score_by_index(scores))
    assert result.returncode == 0, result.stderr
    scored = _lines(data / "synthesized" / "scored-train.jsonl")
    assert [r["judge_score"] for r in scored] == [9, 8, None]
    kept = _lines(data / "synthesized" / "kept-train.jsonl")
    assert len(kept) == 2
    assert "kept 2 of 3 rows scoring >= 7, dropped 0, unscored 1" \
        in result.stdout
    assert "  unscored: 1" in result.stdout


def test_judge_warns_when_the_kept_file_is_not_trainable(tmp_path):
    """A records-shaped file carries {record, synthesis}; axolotl's
    `type: alpaca` will not train on it. Say so in the log instead of
    letting it be discovered inside a GPU job."""
    rows = "".join(json.dumps({"record": {"i": i}, "synthesis": "s"}) + "\n"
                   for i in (1, 2))
    result, _, _ = _judge(tmp_path, rows, {"criteria": "c"},
                          reply=score_by_index({1: 9, 2: 9}))
    assert result.returncode == 0, result.stderr
    assert "will NOT train on them" in result.stdout
    assert "output_format=alpaca" in result.stdout


def test_judge_refuses_a_file_it_cannot_read(tmp_path):
    """A file that is neither shape fails fast naming its keys, rather
    than judging garbage and reporting a confident number."""
    rows = json.dumps({"foo": 1}) + "\n"
    result, _, _ = _judge(tmp_path, rows, {"criteria": "c"},
                          reply=lambda p: "9")
    assert result.returncode != 0
    assert "its keys are ['foo']" in result.stderr


def test_judge_fails_when_nothing_clears_the_bar(tmp_path):
    """An empty kept file is a training job that dies later. Fail here,
    with the histogram above the message showing what the judge gave."""
    result, _, _ = _judge(tmp_path, alpaca_rows(3),
                          {"criteria": "c", "threshold": 10},
                          reply=score_by_index({1: 4, 2: 5, 3: 6}))
    assert result.returncode != 0
    assert "no row scored >= 10" in result.stderr
    assert "   4 | # 1" in result.stdout


def test_judge_missing_input_names_the_path(tmp_path):
    from app.templates import load_templates
    t = load_templates(BUNDLED)[0]["llm-judge"]
    data = tmp_path / "data"
    data.mkdir(parents=True)
    argv = command_argv(t, {"input_name": "nope.jsonl", "criteria": "c"})
    result = run_container(t, "PYCODE", data, argv, tmp_path=tmp_path)
    assert result.returncode != 0
    assert "input file not found" in result.stderr
    assert "synthesized/nope.jsonl" in result.stderr


def test_judge_remote_key_rides_env_file_only(tmp_path):
    """Same rule as the teacher: a named remote judge skips discovery, and
    its key travels in the user's own .env, never on the command line."""
    secret = "sk-judge-not-real-456"
    (tmp_path / "data" / "research").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "research" / ".env").write_text(
        f"MANIFOLD_JUDGE_API_KEY={secret}\n")
    result, _, state = _judge(
        tmp_path, alpaca_rows(2),
        {"criteria": "c", "judge_model": "vendor/judge",
         "env_file": "research/.env"},
        reply=score_by_index({1: 9, 2: 9}), remote=True)
    assert result.returncode == 0, result.stderr
    assert state["models_calls"] == 0
    assert state["chats"][0]["model"] == "vendor/judge"
    assert state["auth"] == [f"Bearer {secret}"] * 2
    assert secret not in result.stdout and secret not in result.stderr


# -- llm-eval: the scorecard, EXECUTED -----------------------------------------
#
# The student is loaded in-process with transformers. Tests never have
# weights, a GPU, or the libraries, so torch/transformers are stubbed on
# PYTHONPATH: what is under test is the control flow, the blind A/B, the
# arithmetic and the scorecard, not HuggingFace. The real API calls need a
# hardware gate; this file cannot and does not claim them.

STUB_TORCH = '''
"""A torch that is enough for llm-eval's control flow, and nothing more."""
float32, float16, bfloat16 = "float32", "float16", "bfloat16"


class cuda:
    @staticmethod
    def is_available():
        return False

    @staticmethod
    def is_bf16_supported():
        return False


class no_grad:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
'''

STUB_TRANSFORMERS = '''
"""A transformers stub whose "tokens" are code points, so the script's
real slicing (generated[0][prompt_len:]) has to be right to recover the
answer. Every generate() call is logged so a test can prove the argv
reached it."""
import json, os


class _Ids(list):
    @property
    def shape(self):
        return (1, len(self[0]))


class _Encoded(dict):
    def to(self, device):
        return self


class AutoTokenizer:
    chat_template = None

    @classmethod
    def from_pretrained(cls, path, **kwargs):
        return cls()

    def __call__(self, text, return_tensors=None):
        return _Encoded(input_ids=_Ids([[ord(c) for c in text]]))

    def decode(self, ids, skip_special_tokens=False):
        return "".join(chr(i) for i in ids)


class AutoModelForCausalLM:
    @classmethod
    def from_pretrained(cls, path, **kwargs):
        model = cls()
        model.kwargs = kwargs
        return model

    def to(self, device):
        self.device = device
        return self

    def eval(self):
        return self

    def generate(self, input_ids=None, **kwargs):
        with open(os.environ["STUB_GEN_LOG"], "a") as log:
            log.write(json.dumps({
                "max_new_tokens": kwargs.get("max_new_tokens"),
                "do_sample": kwargs.get("do_sample"),
                "device": getattr(self, "device", None),
            }) + "\\n")
        answer = os.environ.get("STUB_STUDENT_ANSWER", "STUDENT_ANSWER")
        return [list(input_ids[0]) + [ord(c) for c in answer]]
'''

POISON = ('raise ImportError("{name} must not be imported before the '
          'preflights: a typo has to fail in seconds, not after a '
          '40-second import on a GPU that bills by the minute")\n')


def _stub_libs(tmp_path, *, poison=False) -> Path:
    libs = tmp_path / ("poison" if poison else "stubs")
    libs.mkdir(exist_ok=True)
    if poison:
        for name in ("torch", "transformers"):
            (libs / f"{name}.py").write_text(POISON.format(name=name))
    else:
        (libs / "torch.py").write_text(STUB_TORCH)
        (libs / "transformers.py").write_text(STUB_TRANSFORMERS)
    return libs


def blind_judge(pick):
    """A judge over the blind A/B. `pick(a, b)` sees the two answers and
    returns "A", "B", "TIE", or a raw string to answer with directly."""
    def reply(payload):
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]
        if not system.startswith("You compare"):
            return "DIALLED TEACHER ANSWER"
        a, b = re.search(r"ANSWER A\n(.*)\n\nANSWER B\n(.*)$", user,
                         re.DOTALL).groups()
        verdict = pick(a, b)
        if verdict not in ("A", "B", "TIE"):
            return verdict
        return json.dumps({"winner": verdict, "reason": "because"})
    return reply


def _eval(tmp_path, held_rows, params, *, pick=None, reply=None,
          poison=False, model_id="stub/judge", env_extra=None):
    from app.templates import load_templates
    t = load_templates(BUNDLED)[0]["llm-eval"]
    data = tmp_path / "data"
    (data / "synthesized").mkdir(parents=True, exist_ok=True)
    (data / "synthesized" / "eval-d.jsonl").write_text(held_rows)
    (data / "models" / "distilled").mkdir(parents=True, exist_ok=True)
    (data / "models" / "distilled" / "config.json").write_text("{}")

    gen_log = tmp_path / "generate.log"
    env = {"PYTHONPATH": str(_stub_libs(tmp_path, poison=poison)),
           "STUB_GEN_LOG": str(gen_log), **(env_extra or {})}
    reply = reply or blind_judge(pick or (lambda a, b: "TIE"))
    with stub_openai(reply, model_id=model_id) as (port, state):
        argv = command_argv(t, {"eval_name": "eval-d.jsonl",
                                "student_path": "distilled",
                                "port": port, **params})
        result = run_container(t, "EVAL_PY", data, argv, tmp_path=tmp_path,
                               env_extra=env)
    card_path = data / "outputs" / f"{params.get('output_name', 'scorecard')}.json"
    card = json.loads(card_path.read_text()) if card_path.exists() else None
    return result, card, state, gen_log


def held(n):
    """Held-out rows as llm-synthesize writes them, teacher answer kept."""
    return "".join(json.dumps({
        "instruction": "Tag the shot.",
        "input": json.dumps({"i": i}),
        "output": f"TEACHER_ANSWER {i}",
    }) + "\n" for i in range(1, n + 1))


def test_eval_scorecard_arithmetic_and_headline(tmp_path):
    """A judge that always prefers the student: 6 of 6, 100%, and the log
    ends on the number that matters."""
    result, card, _, gen_log = _eval(
        tmp_path, held(6), {},
        pick=lambda a, b: "A" if "STUDENT_ANSWER" in a else "B")
    assert result.returncode == 0, result.stderr
    assert card["student_wins"] == 6
    assert card["teacher_wins"] == 0 and card["ties"] == 0
    assert card["n_items"] == 6 and card["n_graded"] == 6
    assert card["matched_or_beat"] == 6 and card["match_rate_pct"] == 100.0
    assert ("student matched or beat the teacher on 6/6 held-out tasks "
            "(100%)" in result.stdout)
    # The scorecard is auditable: who judged, what it judged, how the
    # blind was assigned.
    assert card["judge_model"] == "stub/judge"
    assert card["student_device"] == "cpu"
    assert card["eval_file"].endswith("synthesized/eval-d.jsonl")
    assert card["ab_rule"].startswith("student is A on even")
    # The answer is the GENERATED tail, not the prompt read back: the
    # script slices generated[0][prompt_len:] itself, and getting that
    # wrong would feed the judge the question instead of the answer.
    assert card["results"][0]["student_answer"] == "STUDENT_ANSWER"
    assert card["results"][0]["teacher_answer"] == "TEACHER_ANSWER 1"
    # The generation budget and the greedy contract came off argv, not a
    # default: max_new_tokens is slot 7 and an argv shift would move it.
    logged = [json.loads(l) for l in gen_log.read_text().splitlines()]
    assert len(logged) == 6
    assert {e["max_new_tokens"] for e in logged} == {512}
    assert {e["do_sample"] for e in logged} == {False}


def test_eval_alternates_position_so_bias_cannot_decide_it(tmp_path):
    """A judge with pure position bias (always answers A) must land on
    exactly half. That is the only thing the A/B swap buys, and it is
    worth proving because it is the one bias the template claims to
    cancel."""
    result, card, _, _ = _eval(tmp_path, held(6), {},
                               pick=lambda a, b: "A")
    assert result.returncode == 0, result.stderr
    assert card["student_wins"] == 3 and card["teacher_wins"] == 3
    assert card["match_rate_pct"] == 50.0
    assert [r["student_position"] for r in card["results"]] == \
        ["A", "B", "A", "B", "A", "B"]
    # Deterministic by index: the same run twice gives the same assignment.
    assert all(r["student_position"] == ("A" if r["index"] % 2 == 0 else "B")
               for r in card["results"])


def test_eval_counts_ties_separately_but_credits_them(tmp_path):
    """"Matched or beat" includes ties by definition, so the card reports
    both numbers rather than folding one into the other."""
    result, card, _, _ = _eval(tmp_path, held(4), {}, pick=lambda a, b: "TIE")
    assert result.returncode == 0, result.stderr
    assert card["ties"] == 4 and card["student_wins"] == 0
    assert card["matched_or_beat"] == 4 and card["match_rate_pct"] == 100.0
    assert "student won 0, tied 4, lost 0, unscored 0" in result.stdout


def test_eval_never_invents_a_win_from_an_unreadable_judge(tmp_path):
    """A judge answering nonsense scores nothing. 0/0 and 0% is the honest
    output; crediting either side would be the failure mode."""
    result, card, _, _ = _eval(tmp_path, held(4), {},
                               pick=lambda a, b: "the vibes are immaculate")
    assert result.returncode == 0, result.stderr
    assert card["unscored"] == 4 and card["n_graded"] == 0
    assert card["student_wins"] == 0 and card["teacher_wins"] == 0
    assert card["match_rate_pct"] == 0.0
    assert "on 0/0 held-out tasks (0%)" in result.stdout


def test_eval_reuses_the_stored_teacher_answer(tmp_path):
    """The holdout rows already carry what the teacher said. Re-dialling
    would pay twice for the same tokens and, worse, would need a teacher
    still resident on a card the student now wants."""
    result, card, state, _ = _eval(tmp_path, held(3), {},
                                   pick=lambda a, b: "TIE")
    assert result.returncode == 0, result.stderr
    # Three judge calls and nothing else: no teacher chat, no discovery
    # for a teacher that was never needed.
    assert len(state["chats"]) == 3
    assert all(c["messages"][0]["content"].startswith("You compare")
               for c in state["chats"])
    assert card["teacher_model"] == "(stored in the held-out file)"
    assert card["results"][0]["teacher_answer"] == "TEACHER_ANSWER 1"


def test_eval_dials_the_teacher_only_for_rows_without_one(tmp_path):
    rows = (json.dumps({"instruction": "Tag it.", "input": "{}",
                        "output": "TEACHER_ANSWER 1"}) + "\n"
            + json.dumps({"instruction": "Tag it.", "input": "{}",
                          "output": ""}) + "\n")
    result, card, state, _ = _eval(tmp_path, rows, {}, pick=lambda a, b: "TIE")
    assert result.returncode == 0, result.stderr
    teacher_calls = [c for c in state["chats"]
                     if not c["messages"][0]["content"].startswith("You compare")]
    assert len(teacher_calls) == 1
    assert card["results"][0]["teacher_answer"] == "TEACHER_ANSWER 1"
    assert card["results"][1]["teacher_answer"] == "DIALLED TEACHER ANSWER"


def test_eval_says_so_when_the_judge_is_the_teacher(tmp_path):
    """Self-preference is the bias most likely to decide a run: a model
    grading its own answers flatters itself. The template cannot stop it,
    so it must refuse to let the number pass unlabelled."""
    result, card, _, _ = _eval(
        tmp_path, held(2),
        {"judge_model": "vendor/big", "teacher_model": "vendor/big"},
        pick=lambda a, b: "A" if "STUDENT_ANSWER" in a else "B")
    assert result.returncode == 0, result.stderr
    assert card["judge_is_teacher"] is True
    assert card["judge_model"] == card["teacher_model"] == "vendor/big"
    assert "grading its own answers" in result.stdout
    # ...and again immediately above the headline, where it cannot be
    # scrolled past.
    assert "the teacher grading itself" in result.stdout


def test_eval_preflights_fail_before_torch_is_imported(tmp_path):
    """torch and transformers are poisoned here. A missing held-out file
    must still fail with its own message, which is only possible if the
    import really is deferred past the preflights."""
    from app.templates import load_templates
    t = load_templates(BUNDLED)[0]["llm-eval"]
    data = tmp_path / "data"
    (data / "models" / "distilled").mkdir(parents=True)
    (data / "models" / "distilled" / "config.json").write_text("{}")
    env = {"PYTHONPATH": str(_stub_libs(tmp_path, poison=True)),
           "STUB_GEN_LOG": str(tmp_path / "unused.log")}
    argv = command_argv(t, {"eval_name": "nope.jsonl",
                            "student_path": "distilled"})
    result = run_container(t, "EVAL_PY", data, argv, tmp_path=tmp_path,
                           env_extra=env)
    assert result.returncode != 0
    assert "held-out file not found" in result.stderr
    assert "must not be imported" not in result.stderr


def test_eval_missing_student_names_the_directory(tmp_path):
    from app.templates import load_templates
    t = load_templates(BUNDLED)[0]["llm-eval"]
    data = tmp_path / "data"
    (data / "synthesized").mkdir(parents=True)
    (data / "synthesized" / "eval-d.jsonl").write_text(held(1))
    env = {"PYTHONPATH": str(_stub_libs(tmp_path, poison=True)),
           "STUB_GEN_LOG": str(tmp_path / "unused.log")}
    argv = command_argv(t, {"eval_name": "eval-d.jsonl",
                            "student_path": "typo"})
    result = run_container(t, "EVAL_PY", data, argv, tmp_path=tmp_path,
                           env_extra=env)
    assert result.returncode != 0
    assert "no model at" in result.stderr and "models/typo" in result.stderr


def test_eval_cpu_escape_hatch_and_limit(tmp_path):
    """student_device=cpu is the documented way to run beside a live
    teacher that is holding 90% of the card; limit is how you smoke-test
    it for pennies."""
    result, card, _, gen_log = _eval(
        tmp_path, held(10),
        {"student_device": "cpu", "limit": 3, "max_new_tokens": 64,
         "output_name": "smoke"},
        pick=lambda a, b: "TIE")
    assert result.returncode == 0, result.stderr
    assert card["n_items"] == 3 and card["student_device"] == "cpu"
    logged = [json.loads(l) for l in gen_log.read_text().splitlines()]
    assert {e["max_new_tokens"] for e in logged} == {64}
    assert {e["device"] for e in logged} == {"cpu"}


def test_eval_rejects_a_bad_device_and_a_credentialled_url(tmp_path):
    result, _, _, _ = _eval(tmp_path, held(1), {"student_device": "tpu"},
                            pick=lambda a, b: "TIE", poison=True)
    assert result.returncode != 0
    assert "student_device must be auto, cpu or cuda" in result.stderr

    result, _, _, _ = _eval(
        tmp_path, held(1),
        {"judge_base_url": "https://api.example.com/v1?key=sk-not-real"},
        pick=lambda a, b: "TIE", poison=True)
    assert result.returncode != 0
    assert "judge_base_url must carry no credentials" in result.stderr


# -- the mount contract distill.py encodes ------------------------------------


def test_distill_paths_match_the_axolotl_template_mounts(templates):
    """distill.py validates generated configs against hard-coded container
    paths. They are only right because axolotl-finetune mounts them there;
    if the template's volumes move, every generated config points at a
    directory that does not exist inside the job."""
    mounts = {v.container: v.host for v in templates["axolotl-finetune"].volumes}
    assert mounts[distill.DATASET_DIR] == "{persistent}/synthesized"
    assert mounts[distill.OUTPUT_DIR] == "{persistent}/outputs"
    assert any(c.startswith(distill.SCRATCH_DIR) for c in mounts)


# -- distill.py: the validator is a security boundary -------------------------


GOOD_CONFIG = """
base_model: Qwen/Qwen2.5-1.5B-Instruct
adapter: lora
datasets:
  - path: /data/synthesized/kept-tags.jsonl
    type: alpaca
dataset_prepared_path: /tmp/axolotl/prepared
output_dir: /data/output/tags-lora
val_set_size: 0.05
sequence_len: 1024
micro_batch_size: 2
num_epochs: 3
learning_rate: 0.0002
lora_r: 16
lora_alpha: 32
"""

DATASET = "kept-tags.jsonl"


def validate(text, dataset=DATASET):
    return distill.validate_config(text, dataset=dataset,
                                   students=STUDENT_PRESETS)


def rejects(text, fragment, dataset=DATASET):
    with pytest.raises(distill.ConfigRejected) as exc:
        validate(text, dataset)
    assert fragment in str(exc.value), str(exc.value)
    return str(exc.value)


def test_dataset_name_must_be_a_bare_training_file():
    assert distill.validate_dataset_name(" kept-tags.jsonl ") == "kept-tags.jsonl"
    for bad, fragment in (
            ("", "name the training file"),
            ("../../etc/passwd", "bare filename"),
            ("sub/dir.jsonl", "bare filename"),
            ("tags.csv", "must be a .jsonl file"),
            # The held-out file is the student's exam. Training on it is
            # what makes a scorecard a lie, so the name is refused here
            # where the mistake costs nothing.
            ("eval-tags.jsonl", "held-out evaluation set")):
        with pytest.raises(ValueError) as exc:
            distill.validate_dataset_name(bad)
        assert fragment in str(exc.value)


def test_config_filename_is_always_a_safe_name():
    assert distill.config_filename("kept-tags.jsonl") == \
        "configs/distill-kept-tags.yaml"
    assert distill.config_filename("Shot Tags v2.jsonl") == \
        "configs/distill-shot-tags-v2.yaml"


def test_prompt_carries_the_fixed_paths_and_the_shelf():
    prompt = distill.build_prompt(
        spec="distill film-shot tagging into a 3B LoRA that fits an A10",
        dataset=DATASET, students=STUDENT_PRESETS)
    assert "/data/synthesized/kept-tags.jsonl" in prompt
    assert "/data/output/" in prompt and "/tmp/" in prompt
    # Every shelf entry is offered by id, so the brain does not invent a
    # base the validator will then reject (a wasted brain call reads to
    # the user as a broken feature).
    for student in STUDENT_PRESETS:
        assert student["model_id"] in prompt
    assert "lora or qlora" in prompt
    assert "trust_remote_code" in prompt        # named as rejected


def test_prompt_pins_a_chosen_student():
    prompt = distill.build_prompt(
        spec="x" * 20, dataset=DATASET, students=STUDENT_PRESETS,
        student_model="Qwen/Qwen3-1.7B")
    assert "use exactly base_model: Qwen/Qwen3-1.7B" in prompt


def test_code_fences_are_stripped_because_models_emit_them():
    assert distill.strip_code_fence("a: 1") == "a: 1"
    assert distill.strip_code_fence("```yaml\na: 1\n```") == "a: 1"
    assert distill.strip_code_fence(
        "Sure! Here you go:\n```\na: 1\n```\nHope that helps.") == "a: 1"
    # A fenced config is otherwise a perfectly good config: it must parse.
    assert validate(f"```yaml\n{GOOD_CONFIG}\n```")["parsed"]["adapter"] == "lora"


def test_a_good_config_comes_back_verbatim():
    result = validate(GOOD_CONFIG)
    # Verbatim, because the user reviews the exact text they would save.
    assert result["yaml"] == GOOD_CONFIG.strip()
    assert result["base_model"] == "Qwen/Qwen2.5-1.5B-Instruct"
    assert result["dataset_path"] == "/data/synthesized/kept-tags.jsonl"
    assert result["output_dir"] == "/data/output/tags-lora"
    assert result["advisories"] == []


def test_a_config_with_no_validation_split_is_advised_not_refused():
    text = GOOD_CONFIG.replace("val_set_size: 0.05\n", "")
    assert any("val_set_size" in a for a in validate(text)["advisories"])


def test_remote_code_is_refused_by_name():
    """The config is EXECUTED by axolotl on the GPU box, and
    trust_remote_code lets a model repo run its own Python there. A model
    wrote this file: that is exactly why the check is by name, so the
    message is the security one and not "unknown key"."""
    message = rejects(GOOD_CONFIG + "trust_remote_code: true\n",
                      "trust_remote_code")
    assert "run its own Python" in message


def test_only_vetted_keys_survive():
    rejects(GOOD_CONFIG + "hub_model_id: someone/leak\n", "hub_model_id")
    rejects(GOOD_CONFIG + "wandb_project: p\n", "wandb_project")
    rejects(GOOD_CONFIG.replace("    type: alpaca",
                                "    type: alpaca\n    data_files: other.jsonl"),
            "data_files")


def test_required_keys_and_lora_only():
    rejects(GOOD_CONFIG.replace("adapter: lora\n", ""), "adapter")
    rejects(GOOD_CONFIG.replace("adapter: lora", "adapter: full"),
            "LoRA students only")


def test_base_model_must_be_on_the_shelf():
    message = rejects(
        GOOD_CONFIG.replace("Qwen/Qwen2.5-1.5B-Instruct", "evil/backdoored-7b"),
        "not on the student shelf")
    assert "evil/backdoored-7b" in message
    # A merged model already on the filesystem is allowed, with the
    # missing-mount caveat attached rather than silently passed.
    text = GOOD_CONFIG.replace("Qwen/Qwen2.5-1.5B-Instruct",
                               "/data/models/distilled")
    advisories = validate(text)["advisories"]
    assert any("NOT models/" in a for a in advisories)


def test_the_dataset_path_must_be_exactly_the_named_file():
    """The killer case: a glob would sweep in the eval-*.jsonl sitting in
    the same directory and train the student on its own exam."""
    rejects(GOOD_CONFIG.replace("kept-tags.jsonl", "*.jsonl"),
            "no other path exists")
    rejects(GOOD_CONFIG.replace("kept-tags.jsonl", "eval-tags.jsonl"),
            "no other path exists")
    rejects(GOOD_CONFIG.replace("/data/synthesized/kept-tags.jsonl",
                                "https://example.com/train.jsonl"),
            "no other path exists")
    two = GOOD_CONFIG.replace(
        "    type: alpaca",
        "    type: alpaca\n  - path: /data/synthesized/other.jsonl\n"
        "    type: alpaca")
    rejects(two, "lists 2 datasets")


def test_writes_must_land_on_the_persistent_mount():
    rejects(GOOD_CONFIG.replace("/data/output/tags-lora", "/root/steal"),
            "only writable persistent mount")
    rejects(GOOD_CONFIG.replace("/data/output/tags-lora",
                                "/data/output/../../etc"),
            "only writable persistent mount")
    rejects(GOOD_CONFIG.replace("/tmp/axolotl/prepared", "/data/synthesized"),
            "dataset_prepared_path")


def test_a_reply_that_is_not_a_config_teaches_instead_of_crashing():
    message = rejects("Sure! I can help you write that config.",
                      "not a YAML mapping")
    assert "Sure! I can help" in message      # the error quotes what came back
    rejects("- a\n- b\n", "not a YAML mapping")
    rejects("", "returned nothing")
    rejects("a: [1, 2\n", "did not return YAML")


def test_an_essay_is_refused_before_the_parser_sees_it():
    rejects("# " + "x" * distill.MAX_CONFIG_CHARS, "not a document")


def test_yaml_aliases_are_refused():
    """safe_load still EXPANDS aliases, so a few hundred bytes of nested
    anchors becomes gigabytes and takes the backend down - the size cap
    alone cannot prevent it."""
    bomb = ("a: &x [1, 1, 1, 1]\n"
            "b: &y [*x, *x, *x, *x]\n"
            "base_model: Qwen/Qwen3-0.6B\n")
    rejects(bomb, "alias")


def test_student_presets_are_a_usable_shelf():
    """Same key names as MODEL_PRESETS so the dashboard can reuse the
    type, plus the two facts that decide a student: how big it is and
    what its licence lets you do with what comes out."""
    assert STUDENT_PRESETS
    ids = [s["model_id"] for s in STUDENT_PRESETS]
    assert len(ids) == len(set(ids))
    for s in STUDENT_PRESETS:
        assert {"label", "model_id", "vram_gib", "tier", "note",
                "params_b", "license"} <= set(s)
        assert isinstance(s["vram_gib"], (int, float)) and s["vram_gib"] > 0
        assert s["note"].strip()
        # Gated repos would fail on first download: Manifold passes no
        # HuggingFace token.
        assert not any(s["model_id"].startswith(org)
                       for org in ("meta-llama/", "google/gemma"))
    # A 3B student on a 24GB card is the point of the shelf.
    assert any(1 <= s["params_b"] <= 4 for s in STUDENT_PRESETS)


# -- POST /distill/config ------------------------------------------------------


class ScriptedBrain:
    """A brain that answers with whatever the test queued, and remembers
    the prompt it was handed."""

    def __init__(self):
        self.replies: list = []
        self.prompts: list[str] = []

    async def chat_completion(self, port, payload):
        self.prompts.append(payload["messages"][0]["content"])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, dict):
            return reply
        return {"choices": [{"message": {"content": reply}}]}


def wire_brain(app, ref="api:stub") -> ScriptedBrain:
    """Put a scripted brain behind one ref. Every other ref still goes to
    the real registry, which is what makes the unknown-ref test mean
    something."""
    scripted = ScriptedBrain()
    registry = app.state.brains
    real_resolve = registry.resolve

    def resolve(r):
        if r == ref:
            return scripted, "stub-model", 0
        return real_resolve(r)

    registry.resolve = resolve
    return scripted


@pytest.fixture
def brain(client):
    return wire_brain(client.app)


REQUEST = {"spec": "distill film-shot tagging into a small LoRA for an A10",
           "dataset": DATASET, "brain": "api:stub"}


def test_generate_config_happy_path(client, brain):
    brain.replies.append(GOOD_CONFIG)
    resp = client.post("/distill/config", json=REQUEST)
    assert resp.status_code == 200, resp.text
    config = resp.json()["config"]
    assert config["yaml"] == GOOD_CONFIG.strip()
    assert config["base_model"] == "Qwen/Qwen2.5-1.5B-Instruct"
    assert config["brain"] == "api:stub"
    assert config["suggested_path"] == "configs/distill-kept-tags.yaml"
    # The spec the user typed reaches the brain, with the fixed paths.
    assert "film-shot tagging" in brain.prompts[0]
    assert "/data/synthesized/kept-tags.jsonl" in brain.prompts[0]
    # Which brain wrote it is in the audit trail: an api: brain spends the
    # user's money and a cli: brain acts under their login.
    actions = [e["action"] for e in client.get("/audit").json()["entries"]]
    assert "distill_config" in actions


def test_generate_config_writes_nothing_and_trains_nothing(client, brain):
    """Review only. The seam ends at a string on screen: saving is the
    existing upload route and training is the existing job, both human."""
    brain.replies.append(GOOD_CONFIG)
    before = client.get("/tasks").json()["tasks"]
    assert client.post("/distill/config", json=REQUEST).status_code == 200
    assert client.get("/tasks").json()["tasks"] == before


def test_a_fenced_reply_still_generates(client, brain):
    brain.replies.append(f"Here you go:\n```yaml\n{GOOD_CONFIG}\n```\n")
    resp = client.post("/distill/config", json=REQUEST)
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["parsed"]["adapter"] == "lora"


def test_prose_instead_of_yaml_is_a_teaching_502(client, brain):
    """A brain that answers in sentences is not a server error and not a
    stack trace: it is a 502 that quotes what actually came back, so the
    user can see whether to retry or change brains."""
    brain.replies.append("I'd be happy to help you fine-tune a model!")
    resp = client.post("/distill/config", json=REQUEST)
    assert resp.status_code == 502
    assert "I'd be happy to help" in resp.json()["detail"]


def test_a_dangerous_config_is_a_502_naming_the_key(client, brain):
    brain.replies.append(GOOD_CONFIG + "trust_remote_code: true\n")
    resp = client.post("/distill/config", json=REQUEST)
    assert resp.status_code == 502
    assert "trust_remote_code" in resp.json()["detail"]


def test_missing_required_keys_is_a_502_naming_them(client, brain):
    brain.replies.append("base_model: Qwen/Qwen3-0.6B\nadapter: lora\n")
    resp = client.post("/distill/config", json=REQUEST)
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "datasets" in detail and "output_dir" in detail


def test_a_brain_that_fails_transport_is_a_502(client, brain):
    brain.replies.append(ModelClientError("brain unreachable at http://x"))
    resp = client.post("/distill/config", json=REQUEST)
    assert resp.status_code == 502
    assert "brain unreachable" in resp.json()["detail"]


def test_an_unrecognised_reply_shape_is_a_502(client, brain):
    brain.replies.append({"not": "a completion"})
    resp = client.post("/distill/config", json=REQUEST)
    assert resp.status_code == 502
    assert "unexpected shape" in resp.json()["detail"]


def test_an_unknown_brain_is_refused_with_the_registry_message(client, brain):
    """The registry already writes a good message for every unusable ref;
    the route must pass it through rather than invent a worse one."""
    resp = client.post("/distill/config", json={**REQUEST, "brain": "api:nope"})
    assert resp.status_code == 409
    assert "unknown api brain 'nope'" in resp.json()["detail"]
    resp = client.post("/distill/config", json={**REQUEST, "brain": "wat"})
    assert resp.status_code == 409
    assert "bad brain ref" in resp.json()["detail"]


def test_caller_mistakes_are_422_not_502(client, brain):
    """The brain's fault is a 502; the caller's fault is a 422. Mixing
    them means a user retries a request that can never work."""
    resp = client.post("/distill/config",
                       json={**REQUEST, "dataset": "eval-tags.jsonl"})
    assert resp.status_code == 422
    assert "held-out evaluation set" in resp.json()["detail"]

    resp = client.post("/distill/config",
                       json={**REQUEST, "student_model": "evil/backdoored-7b"})
    assert resp.status_code == 422
    assert "not on the student shelf" in resp.json()["detail"]
    # The brain was never called for either: no spend on a doomed request.
    assert brain.prompts == []


def test_the_body_is_a_body_not_a_query_parameter(client, brain):
    """The 422-on-every-POST trap: `from __future__ import annotations`
    turns hints into strings that FastAPI resolves against MODULE globals,
    so a request model nested in create_app silently degrades to a query
    parameter. A 200 here is the proof it is declared at module level."""
    brain.replies.append(GOOD_CONFIG)
    assert client.post("/distill/config", json=REQUEST).status_code == 200
    # And the field constraints are real: a two-word spec is not a spec.
    assert client.post("/distill/config",
                       json={**REQUEST, "spec": "hi"}).status_code == 422


def test_student_presets_are_served_for_the_picker(client):
    resp = client.get("/student-presets")
    assert resp.status_code == 200
    presets = resp.json()["presets"]
    assert presets == STUDENT_PRESETS
    assert {"label", "model_id", "vram_gib"} <= set(presets[0])


# -- who may spend a brain call ------------------------------------------------


OWNER = "owner-token-for-distill-tests"


@pytest.fixture
def auth_app(tmp_path, mock_client, mock_storage, mock_sidecar):
    return create_app(
        make_settings(tmp_path, api_token=OWNER),
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        env_path=tmp_path / ".env",
    )


def _minted(owner, name, role):
    body = owner.post("/principals", json={"name": name, "role": role}).json()
    c = TestClient(owner.app)
    c.headers.update({"Authorization": f"Bearer {body['token']}"})
    return c


def test_generating_a_config_is_operator_work(auth_app):
    """It writes nothing, but it spends: an api: brain bills the user's
    key and a cli: brain acts under their login. Reading the shelf is
    observation and stays viewer."""
    scripted = wire_brain(auth_app)
    with TestClient(auth_app,
                    headers={"Authorization": f"Bearer {OWNER}"}) as owner:
        viewer = _minted(owner, "watcher", "viewer")
        operator = _minted(owner, "worker", "operator")

        assert viewer.get("/student-presets").status_code == 200
        resp = viewer.post("/distill/config", json=REQUEST)
        assert resp.status_code == 403
        assert "operator" in resp.json()["detail"]

        scripted.replies.append(GOOD_CONFIG)
        assert operator.post("/distill/config",
                             json=REQUEST).status_code == 200


# -- the chain, over HTTP ------------------------------------------------------


@pytest.fixture
def fast_app(tmp_path, mock_client, mock_storage, mock_sidecar):
    settings = make_settings(
        tmp_path,
        tasks=TaskSettings(poll_seconds=0.02),
        idle=IdleSettings(timeout_seconds=30.0, poll_seconds=0.5),
    )
    return create_app(
        settings,
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )


def wait_until(predicate, timeout=25.0, interval=0.02, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")


# The batch tail of the distill loop, each link depending on the one above.
CHAIN = [
    ("llm-synthesize", {"input_path": "research/raw.jsonl",
                        "instruction": "Tag the shot.",
                        "output_name": "distill",
                        "output_format": "alpaca", "holdout_pct": 10}),
    ("llm-judge", {"input_name": "distill.jsonl",
                   "criteria": "names the shot type"}),
    ("axolotl-finetune", {"config_path": "distill-kept-distill.yaml"}),
    ("lora-merge", {"adapter_dir": "lora-out"}),
    ("llm-eval", {"eval_name": "eval-distill.jsonl",
                  "student_path": "distilled"}),
]


def enqueue_chain(client) -> list[str]:
    ids: list[str] = []
    for template, parameters in CHAIN:
        body = {"template": template, "parameters": parameters}
        if ids:
            body["depends_on"] = [ids[-1]]
        resp = client.post("/tasks", json=body)
        assert resp.status_code == 202, resp.text
        ids.append(resp.json()["task"]["id"])
    return ids


def test_a_served_teacher_cannot_be_a_chain_parent(client):
    """This is expected behaviour, not a gap, and it is pinned so nobody
    "fixes" it: a server never exits, so "after it succeeds" would mean
    never. The teacher is started separately and the synthesize job binds
    to it with target_instance_id (server and batch coexist on one
    instance by design)."""
    server = client.post("/tasks", json={
        "template": "vllm-serve",
        "parameters": {"model_id": "Qwen/Qwen2.5-7B-Instruct"},
    }).json()["task"]["id"]
    resp = client.post("/tasks", json={
        "template": "llm-synthesize",
        "parameters": {"input_path": "a.jsonl", "instruction": "x"},
        "depends_on": [server],
    })
    assert resp.status_code == 422
    assert "coexist" in resp.json()["detail"]


def test_the_batch_tail_runs_in_order(fast_app):
    """synthesize -> judge -> finetune -> merge -> eval on one instance.
    Order proved by timestamps, not by luck: each link starts at or after
    its parent finished."""
    with TestClient(fast_app) as client:
        client.post("/instances", json={
            "instance_type": "gpu_1x_a10", "region": "us-east-1",
            "filesystem": "manifold-data",
        })
        ids = enqueue_chain(client)
        wait_until(
            lambda: client.get(f"/tasks/{ids[-1]}").json()["status"]
            == "succeeded",
            message="the distill chain to finish")

        tasks = [client.get(f"/tasks/{i}").json() for i in ids]
        assert [t["status"] for t in tasks] == ["succeeded"] * 5
        for parent, child in zip(tasks, tasks[1:]):
            assert child["started_at"] >= parent["finished_at"], \
                f"{child['template']} started before {parent['template']} ended"


def test_a_failed_judge_skips_everything_downstream(client):
    """Curation is the link most likely to fail honestly (nothing cleared
    the threshold). When it does, the training run, the merge and the
    scorecard must never start: three GPU jobs that would each produce
    something meaningless."""
    ids = enqueue_chain(client)
    synth, judge, train, merge, score = ids
    queue = client.app.state.queue
    dispatcher = client.app.state.dispatcher

    queue.mark_running(judge, "i-1")
    dispatcher._finish_task(
        judge, exit_code=1, output_paths=[],
        error="no row scored >= 7: nothing to train on")

    for task_id in (train, merge, score):
        task = client.get(f"/tasks/{task_id}").json()
        assert task["status"] == "skipped"
        # Never ran: no exit code, no instance, no spend.
        assert task["exit_code"] is None and task["instance_id"] is None
    # One hop at a time, so the reason names the edge that died.
    assert judge in client.get(f"/tasks/{train}").json()["error"]
    assert train in client.get(f"/tasks/{merge}").json()["error"]
    # The synthesized data is still there and its job is untouched.
    assert client.get(f"/tasks/{synth}").json()["status"] == "queued"


def test_the_chain_renders_the_paths_the_next_link_reads(client):
    """The templates hand files to each other by convention, not by a
    declared output. If they disagree about a directory the chain dies
    inside a GPU job, so the agreement is pinned here."""
    from app.templates import load_templates
    loaded, _ = load_templates(BUNDLED)

    synth = render_docker_command(
        loaded["llm-synthesize"],
        coerce_parameters(loaded["llm-synthesize"], dict(CHAIN[0][1])),
        filesystem="manifold-data", task_id="s1")
    judge = render_docker_command(
        loaded["llm-judge"],
        coerce_parameters(loaded["llm-judge"], dict(CHAIN[1][1])),
        filesystem="manifold-data", task_id="s2")
    score = render_docker_command(
        loaded["llm-eval"],
        coerce_parameters(loaded["llm-eval"], dict(CHAIN[4][1])),
        filesystem="manifold-data", task_id="s3")

    # All three mount the whole filesystem at /data, which is how
    # synthesized/ is shared; axolotl-finetune then reads the same
    # directory read-only at the same container path.
    for cmd in (synth, judge, score):
        assert "-v /lambda/nfs/manifold-data:/data" in cmd
        assert "--network host" in cmd
    assert "-v /lambda/nfs/manifold-data/synthesized:/data/synthesized:ro" in \
        render_docker_command(
            loaded["axolotl-finetune"],
            coerce_parameters(loaded["axolotl-finetune"],
                              dict(CHAIN[2][1])),
            filesystem="manifold-data", task_id="s4")
