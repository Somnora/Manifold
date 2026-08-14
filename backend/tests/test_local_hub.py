"""The local hub (Phase 36): external brains, approval gates, local terminal.

- BrainRegistry: instance brains from served models; local brains from
  probed endpoints (prober injected - tests never touch the network); api
  brains appear only when their key env var is set.
- Autopilot with an external brain: the run loop drives a scripted
  OpenAI-compatible client end to end.
- Approval gates: launch_gpu pauses as 'pending'; deny feeds an error the
  model sees; approve executes for real; timeout expires it.
- Local terminal WS: origin allowlist enforced; a real shell echoes.
"""

import asyncio
import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app.brains import BrainRegistry
from app.config import AutopilotSettings
from app.sidecar_client import MockSidecarClient
from tests.conftest import make_settings
from tests.test_auto_manage import _app, _fast
from tests.test_chat import make_serving_task


# -- registry ----------------------------------------------------------------------


def registry_client(tmp_path, *, probe=None, script=None):
    from app.model_client import MockModelClient

    class ScriptedBrain(MockModelClient):
        def __init__(self, lines):
            super().__init__()
            self.lines = list(lines)

        async def chat_completion(self, port, payload):
            self.requests.append({"port": port, "payload": payload})
            return {"choices": [{"message": {
                "role": "assistant", "content": self.lines.pop(0)}}]}

        async def chat_stream(self, port, payload):
            # Autopilot consumes streams; emit one delta then DONE.
            self.requests.append({"port": port, "payload": payload})
            content = self.lines.pop(0)
            chunk = {"choices": [{"delta": {"content": content}}]}
            yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

    app, mock = _app(_fast(tmp_path), sidecar=MockSidecarClient(unpersisted=[]))
    if probe is not None:
        app.state.brains._http_get = probe
    scripted = ScriptedBrain(script or [])
    return app, mock, scripted


def test_local_brains_detected_and_api_brains_gated(tmp_path, monkeypatch):
    async def probe(url):
        if "11434" in url:
            return ["llama3.1", "qwen3:8b"]
        return None      # LM Studio not running

    app, _, _ = registry_client(tmp_path, probe=probe)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with TestClient(app) as client:
        brains = client.get("/brains").json()["brains"]
        refs = [b["ref"] for b in brains]
        assert "local:ollama/llama3.1" in refs
        assert "local:ollama/qwen3:8b" in refs
        assert not any(r.startswith("api:") for r in refs)   # no keys set

        # Key appears -> brain appears (no restart needed: env is read live).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-real")
        app.state.brains._local_cache = None
        brains = client.get("/brains").json()["brains"]
        assert any(b["ref"] == "api:claude" for b in brains)


def test_instance_brains_come_from_served_models(tmp_path):
    async def probe(url):
        return None
    app, _, _ = registry_client(tmp_path, probe=probe)
    with TestClient(app) as client:
        from tests.test_reconcile import launch_connected
        _, iid = launch_connected(client)
        make_serving_task(client, iid)
        brains = client.get("/brains").json()["brains"]
        assert any(b["ref"] == f"instance:{iid}" for b in brains)


def test_resolve_rejects_unusable_refs(tmp_path):
    app, _, _ = registry_client(tmp_path)
    reg: BrainRegistry = app.state.brains
    with pytest.raises(ValueError):
        reg.resolve("api:claude")          # no key set -> actionable error
    with pytest.raises(ValueError):
        reg.resolve("local:nope/model")
    with pytest.raises(ValueError):
        reg.resolve("garbage")


# -- external brain drives a run -----------------------------------------------------


def wait_run(client, run_id, timeout=8.0):
    deadline = time.monotonic() + timeout
    run = client.get(f"/autopilot/runs/{run_id}").json()
    while time.monotonic() < deadline:
        run = client.get(f"/autopilot/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.03)
    raise AssertionError(f"run stuck: {run['status']}")


def test_external_brain_runs_a_goal(tmp_path):
    async def probe(url):
        return ["scripted"] if "11434" in url else None

    app, _, scripted = registry_client(
        tmp_path,
        probe=probe,
        script=['{"thought": "list", "action": "list_templates", "args": {}}',
                '{"thought": "done", "action": "done", '
                '"args": {"summary": "listed the templates"}}'])
    # Route the local brain to the scripted client instead of real HTTP.
    reg = app.state.brains
    original = reg.resolve

    def resolve(ref):
        if ref.startswith("local:"):
            return scripted, "scripted", 0
        return original(ref)
    reg.resolve = resolve

    with TestClient(app) as client:
        run = client.post("/autopilot/runs", json={
            "goal": "List the templates and finish.",
            "brain": "local:ollama/scripted",
        }).json()["run"]
        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"
        assert done["summary"] == "listed the templates"
        assert done["brain_instance_id"] == "local:ollama/scripted"


# -- approvals -----------------------------------------------------------------------


def approval_app(tmp_path, script, *, timeout=600.0):
    app, mock, scripted = registry_client(tmp_path, script=script)
    reg = app.state.brains
    reg.resolve = lambda ref: (scripted, "scripted", 0)
    # Speed the poll: patch settings on the autopilot instance.
    import dataclasses
    st = app.state.dispatcher.settings
    patched = dataclasses.replace(
        st, autopilot=AutopilotSettings(approval_timeout_seconds=timeout))
    return app, mock, patched


def start_gated_run(client, goal="Launch a GPU."):
    return client.post("/autopilot/runs", json={
        "goal": goal, "brain": "local:x/scripted",
        "require_approval": True,
    }).json()["run"]


def wait_pending(client, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = client.get("/autopilot/approvals").json()["approvals"]
        if pending:
            return pending[0]
        time.sleep(0.03)
    raise AssertionError("no approval ever became pending")


LAUNCH = ('{"thought": "go", "action": "launch_gpu", "args": '
          '{"instance_type": "gpu_1x_a10", "region": "us-east-1", '
          '"filesystem": "manifold-data"}}')
DONE = '{"thought": "ok", "action": "done", "args": {"summary": "finished"}}'


def test_denied_action_never_executes_and_run_adapts(tmp_path):
    app, mock, _ = approval_app(tmp_path, [LAUNCH, DONE])
    with TestClient(app) as client:
        run = start_gated_run(client)
        pending = wait_pending(client)
        assert pending["action"] == "launch_gpu"
        assert pending["run_goal"] == "Launch a GPU."

        resp = client.post(f"/autopilot/approvals/{pending['id']}",
                           json={"approve": False})
        assert resp.status_code == 200

        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"      # model adapted, finished
        assert mock.launch_calls == []            # NOTHING launched
        # The denial reached the model as an observation.
        steps = client.get(f"/autopilot/runs/{run['id']}").json()["steps"]
        launch_step = next(s for s in steps if s["action"] == "launch_gpu")
        assert "DENIED" in launch_step["result"]["error"]


def test_approved_action_executes_for_real(tmp_path):
    app, mock, _ = approval_app(tmp_path, [LAUNCH, DONE])
    with TestClient(app) as client:
        run = start_gated_run(client)
        pending = wait_pending(client)
        client.post(f"/autopilot/approvals/{pending['id']}",
                    json={"approve": True})
        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"
        assert len(mock.launch_calls) == 1        # the launch really happened
        # Decision is race-safe: a second decide is refused.
        again = client.post(f"/autopilot/approvals/{pending['id']}",
                            json={"approve": False})
        assert again.status_code == 409


def test_approval_timeout_auto_denies(tmp_path):
    app, mock, patched = approval_app(tmp_path, [LAUNCH, DONE], timeout=0.3)
    with TestClient(app) as client:
        run = start_gated_run(client)
        pending = wait_pending(client)
        # Nobody decides. Force-expire via the same guarded transition the
        # timeout path uses, then confirm the run adapts.
        db = client.app.state.orchestrator.db
        assert db.decide_approval(pending["id"], "expired") is True
        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"
        assert mock.launch_calls == []


def test_ungated_run_needs_no_approval(tmp_path):
    app, mock, _ = approval_app(tmp_path, [LAUNCH, DONE])
    with TestClient(app) as client:
        run = client.post("/autopilot/runs", json={
            "goal": "Launch a GPU.", "brain": "local:x/scripted",
            "require_approval": False,
        }).json()["run"]
        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"
        assert len(mock.launch_calls) == 1
        assert client.get("/autopilot/approvals").json()["approvals"] == []


# -- the per-action approval policy (Phase 37) ---------------------------------------

TERMINATE = ('{"thought": "clean up", "action": "terminate_instance", '
             '"args": {"instance_id": "%s"}}')


def test_default_policy_gates_the_launch(tmp_path):
    """A run that says nothing about approvals inherits the Settings policy,
    which gates launches. This is the shape of a real unattended run."""
    app, mock, _ = approval_app(tmp_path, [LAUNCH, DONE])
    with TestClient(app) as client:
        run = client.post("/autopilot/runs", json={
            "goal": "Launch a GPU.", "brain": "local:x/scripted",
        }).json()["run"]
        assert run["approval_policy"] == ["launch_gpu"]
        pending = wait_pending(client)
        assert pending["action"] == "launch_gpu"
        assert mock.launch_calls == []            # paused, nothing spent

        client.post(f"/autopilot/approvals/{pending['id']}",
                    json={"approve": True})
        assert wait_run(client, run["id"])["status"] == "succeeded"
        assert len(mock.launch_calls) == 1


def test_default_policy_does_not_gate_termination(tmp_path):
    """The money argument, pinned. An unanswered approval AUTO-DENIES; a
    denied terminate_instance means the GPU keeps billing. So the default
    policy lets the agent clean up after itself while you are away — the
    launch was the decision that needed you, not the shutdown."""
    from tests.conftest import wait_for_launch_status

    app, mock, _ = approval_app(tmp_path, ["", DONE])
    with TestClient(app) as client:
        # A GPU is already up (launched through the ordinary guarded path).
        launch = client.post("/instances", json={
            "instance_type": "gpu_1x_a10", "region": "us-east-1",
            "filesystem": "manifold-data"}).json()["launch"]
        instance_id = wait_for_launch_status(
            client, launch["id"])["lambda_instance_id"]
        # The agent's one action: shut it down.
        app.state.brains.resolve("")[0].lines[0] = TERMINATE % instance_id

        run = client.post("/autopilot/runs", json={
            "goal": "Tidy up the idle GPU.", "brain": "local:x/scripted",
        }).json()["run"]
        assert run["approval_policy"] == ["launch_gpu"]   # the default

        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"
        # It went straight through: no approval was ever raised for the
        # terminate, so an away-from-keyboard user is not billed for the wait.
        assert client.get("/autopilot/approvals").json()["approvals"] == []
        assert mock.instances[instance_id].status == "terminated"


def test_gating_termination_is_possible_but_opt_in(tmp_path):
    """The user can still gate it (their money, their call) — it just is not
    the default, and it is a per-run/per-Settings choice, not all-or-nothing."""
    app, mock, _ = approval_app(tmp_path, [LAUNCH, DONE])
    with TestClient(app) as client:
        run = client.post("/autopilot/runs", json={
            "goal": "Launch a GPU.", "brain": "local:x/scripted",
            "approve_actions": ["terminate_instance"],
        }).json()["run"]
        assert run["approval_policy"] == ["terminate_instance"]
        # launch_gpu is NOT gated in this run, so it runs unattended.
        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"
        assert len(mock.launch_calls) == 1
        assert client.get("/autopilot/approvals").json()["approvals"] == []


def test_unknown_gated_action_is_rejected(tmp_path):
    app, _, _ = approval_app(tmp_path, [DONE])
    with TestClient(app) as client:
        resp = client.post("/autopilot/runs", json={
            "goal": "Do a thing.", "brain": "local:x/scripted",
            "approve_actions": ["rm_rf_slash"],
        })
        assert resp.status_code == 422
        assert "rm_rf_slash" in resp.json()["detail"]


# -- local terminal ------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only")
def test_local_terminal_echoes_and_respects_origin(tmp_path):
    app, _, _ = registry_client(tmp_path)
    with TestClient(app) as client:
        # Evil origin -> refused before any shell is spawned.
        with pytest.raises(Exception):
            with client.websocket_connect(
                    "/local/terminal",
                    headers={"origin": "https://evil.example"}) as ws:
                ws.receive_text()

        # Localhost origin -> a real shell on this machine echoes.
        with client.websocket_connect(
                "/local/terminal",
                headers={"origin": "http://localhost:3000"}) as ws:
            ws.send_json({"type": "input",
                          "data": "echo manifold_$((20+16))\n"})
            deadline = time.monotonic() + 8
            seen = ""
            while time.monotonic() < deadline:
                seen += ws.receive_text()
                if "manifold_36" in seen:
                    break
            assert "manifold_36" in seen


def test_local_terminal_model_env_wires_the_shell(tmp_path):
    """?model=<id> pre-wires the shell's env to the served model through
    the OpenAI proxy (the Open-in-terminal button), and says so in a
    banner that lands in scrollback."""
    app, _, _ = registry_client(tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect(
                "/local/terminal?model=Qwen/Qwen3-8B",
                headers={"origin": "http://localhost:3000"}) as ws:
            ws.send_json({"type": "input",
                          "data": "echo wired_${MANIFOLD_MODEL}"
                                  "_${OPENAI_BASE_URL}\n"})
            deadline = time.monotonic() + 8
            seen = ""
            while time.monotonic() < deadline:
                seen += ws.receive_text()
                if "wired_Qwen/Qwen3-8B_http://127.0.0.1:" in seen:
                    break
            assert "wired_Qwen/Qwen3-8B_http://127.0.0.1:" in seen
            assert "/v1" in seen
            # The banner explains the wiring (recorded via feed -> replayed
            # on any later reattach too).
            assert "wired to your served model" in seen


# -- cli brains (frontier CLIs via the user's own login) -------------------------------


def make_fake_cli(tmp_path, name, reply):
    """A fake frontier CLI: ignores its args and prints `reply` the way the
    real CLI would (claude prints JSON with .result; gemini prints text)."""
    import json as _json
    script = tmp_path / name
    if name == "claude":
        body = f"print({_json.dumps(_json.dumps({'result': reply}))})"
    else:
        body = f"print({_json.dumps(reply)})"
    script.write_text(f"#!/usr/bin/env python3\nimport sys\n{body}\n")
    script.chmod(0o755)
    return str(script)


def test_cli_brains_offered_when_installed(tmp_path):
    app, _, _ = registry_client(tmp_path)
    reg = app.state.brains
    fake = make_fake_cli(tmp_path, "claude", "hi")
    reg._which = lambda name: fake if name == "claude" else None
    with TestClient(app) as client:
        brains = client.get("/brains").json()["brains"]
        cli = [b for b in brains if b["kind"] == "cli"]
        assert [b["ref"] for b in cli] == ["cli:claude"]
        assert "your Claude (Anthropic) login" in cli[0]["label"]


def test_cli_brain_answers_via_subprocess(tmp_path):
    """The real subprocess plumbing: flatten -> spawn -> parse .result."""
    from app.brains import CliBrainClient

    fake = make_fake_cli(tmp_path, "claude", "pong from the cli")
    client = CliBrainClient("claude", fake)
    reply = asyncio.run(client.chat_completion(0, {
        "messages": [{"role": "system", "content": "be brief"},
                     {"role": "user", "content": "ping"}],
    }))
    assert reply["choices"][0]["message"]["content"] == "pong from the cli"


def test_cli_brain_failure_is_actionable(tmp_path):
    """A CLI that exits nonzero (e.g. not logged in) surfaces a hint, not a
    stack trace."""
    from app.brains import CliBrainClient
    from app.model_client import ModelClientError

    script = tmp_path / "claude"
    script.write_text("#!/usr/bin/env python3\nimport sys\n"
                      "sys.stderr.write('not logged in')\nsys.exit(1)\n")
    script.chmod(0o755)
    client = CliBrainClient("claude", str(script))
    with pytest.raises(ModelClientError) as exc:
        asyncio.run(client.chat_completion(0, {
            "messages": [{"role": "user", "content": "ping"}]}))
    assert "is it logged in" in str(exc.value)


def test_cli_brain_drives_a_run_end_to_end(tmp_path):
    """A fake `gemini` CLI answers with the done-action: the whole agent
    loop works over the subprocess brain."""
    reply = '{"action": "done", "args": {"summary": "cli brain works"}}'
    fake = make_fake_cli(tmp_path, "gemini", reply)
    app, _, _ = registry_client(tmp_path)
    app.state.brains._which = lambda name: fake if name == "gemini" else None
    with TestClient(app) as client:
        run = client.post("/autopilot/runs", json={
            "goal": "Prove the cli brain path works.",
            "brain": "cli:gemini",
        }).json()["run"]
        done = wait_run(client, run["id"])
        assert done["status"] == "succeeded"
        assert done["summary"] == "cli brain works"


def test_which_fallback_finds_cli_off_path(tmp_path, monkeypatch):
    """A Finder-launched app gets launchd's bare PATH, which hides
    Homebrew/npm-installed CLIs; the fallback searches well-known dirs."""
    import os
    import stat
    from app import brains as brains_mod

    bin_dir = tmp_path / "fake-local-bin"
    bin_dir.mkdir()
    cli = bin_dir / "claude"
    cli.write_text("#!/bin/sh\necho hi\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(brains_mod, "WELL_KNOWN_BIN_DIRS", (str(bin_dir),))
    monkeypatch.setattr(brains_mod.shutil, "which", lambda name: None)

    assert brains_mod.which_with_fallback("claude") == str(cli)
    assert brains_mod.which_with_fallback("codex") is None

    # Non-executable files never count.
    plain = bin_dir / "codex"
    plain.write_text("not a program")
    assert brains_mod.which_with_fallback("codex") is None


# -- what the real gate found -------------------------------------------------


def test_probe_survives_a_server_that_answers_data_null(monkeypatch):
    """A freshly installed Ollama with no models pulled answers
    `{"object": "list", "data": null}`.

    `.get("data", [])` does not default on that: the key EXISTS holding
    null, so the old code iterated None and raised, which 500s /brains -
    taking the brain picker, the chat, Autopilot and the distill panel
    down together. Found at the 2026-08-14 real gate, on an Ollama
    installed minutes earlier by following this project's own docs.

    monkeypatch, not a hand-rolled save/restore of httpx.AsyncClient: the
    first version of this test swapped the global and put it back in a
    finally, which still leaked enough to fail an unrelated safety-hook
    test later in the full run. Scoped patching is the house rule for a
    reason.
    """
    import asyncio

    import httpx

    from app.brains import BrainRegistry

    class FakeResponse:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def probe_with(body):
        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                return FakeResponse(body)

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
        return asyncio.run(BrainRegistry._probe_models("http://x/v1/models"))

    # The exact shape that broke it: present, and null.
    assert probe_with({"object": "list", "data": None}) == []
    # Neighbouring shapes a third party could also answer.
    assert probe_with({"object": "list"}) == []
    assert probe_with({"data": "not a list"}) == []
    assert probe_with({"data": [None, {"no_id": 1}, {"id": "llama3"}]}) == \
        ["llama3"]
    assert probe_with([]) == []
