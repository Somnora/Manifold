"""Phase 96: the missing flag, and three cheap truths.

Both agent reviews ranked the same root cause first: vllm-serve could not
express --max-num-seqs, one OOM-avoiding flag, so a hand-rolled server
cost proxy routing, activity visibility, log streaming and restart
supervision in a single move. The fix is a passthrough whose ALLOWLIST
matters more than the passthrough (DECISIONS.md): --max-num-seqs is a
tuning knob, --trust-remote-code is supply-chain surface, and a verbatim
passthrough trades an OOM problem for a worse one.

Plus: the launcher can set the display name (a box got hand-renamed in
the UI mid-incident because a name was the only ownership signal that
existed), spend breaks down by purpose, and the rescue hook's scope -
/workspace/ephemeral, NOT $HOME - is stated where "files_found: 0" would
otherwise read as "nothing was lost".
"""

from pathlib import Path

import pytest

from app.dispatcher import (ParameterError, _validate_arg_string,
                            coerce_parameters, render_docker_command)
from app.templates import load_templates
from tests.test_mcp import mcp_wired, wired_app  # noqa: F401 - fixtures

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"

ALLOW = ("--max-num-seqs", "--gpu-memory-utilization", "--enforce-eager")


# -- the grammar, pure --------------------------------------------------------


def test_tuning_flags_pass_in_both_spellings():
    assert _validate_arg_string("x", "--max-num-seqs 8", ALLOW) is None
    assert _validate_arg_string("x", "--gpu-memory-utilization=0.90",
                                ALLOW) is None
    assert _validate_arg_string(
        "x", "--enforce-eager --max-num-seqs 8", ALLOW) is None


def test_a_flag_outside_the_allowlist_is_refused_with_the_list():
    """The refusal carries the full allowlist, so the wall is discoverable
    without hitting it twice."""
    problem = _validate_arg_string("x", "--trust-remote-code", ALLOW)
    assert "--trust-remote-code" in problem
    assert "--max-num-seqs" in problem, "the error must name what IS allowed"


def test_values_are_charset_bound():
    assert _validate_arg_string("x", "--max-num-seqs 8;rm", ALLOW) is not None
    assert _validate_arg_string("x", '--gpu-memory-utilization="0.9"',
                                ALLOW) is not None


def test_a_bare_value_with_no_flag_is_refused():
    assert _validate_arg_string("x", "8 --max-num-seqs", ALLOW) is not None


# -- wired into the template --------------------------------------------------


def vllm():
    templates, errors = load_templates(TEMPLATES_DIR)
    assert not errors
    return templates["vllm-serve"]


def test_the_incident_flag_is_now_expressible():
    """THE root cause: --max-num-seqs on a 40GB A100. It must enqueue."""
    params = coerce_parameters(vllm(), {
        "model_id": "Qwen/Qwen3.5-27B-FP8",
        "extra_args": "--max-num-seqs 8 --gpu-memory-utilization 0.90",
    })
    assert params["extra_args"] == "--max-num-seqs 8 --gpu-memory-utilization 0.90"
    rendered = render_docker_command(
        vllm(), params, filesystem="fs", task_id="t1")
    assert "--max-num-seqs 8" in rendered


def test_supply_chain_flags_are_refused_at_enqueue():
    with pytest.raises(ParameterError) as err:
        coerce_parameters(vllm(), {
            "model_id": "m", "extra_args": "--trust-remote-code",
        })
    assert "--trust-remote-code" in str(err.value)
    assert "--max-num-seqs" in str(err.value)


def test_empty_extra_args_changes_nothing():
    params = coerce_parameters(vllm(), {"model_id": "m"})
    assert params["extra_args"] == ""


def test_the_allowlist_is_visible_in_the_template_api():
    """Agents and the UI must be able to SEE which flags are permitted
    instead of discovering the wall by hitting it."""
    api = vllm().to_api()
    by_name = {p["name"]: p for p in api["parameters"]}
    assert "--max-num-seqs" in by_name["extra_args"]["arg_allowlist"]
    assert "--trust-remote-code" not in by_name["extra_args"]["arg_allowlist"]
    assert "arg_allowlist" not in by_name["model_id"], (
        "ordinary parameters must not grow the field")


def test_an_allowlist_on_a_non_string_parameter_fails_at_load(tmp_path):
    """A template author's mistake surfaces at load time, not at dispatch."""
    (tmp_path / "bad.yaml").write_text(
        "name: bad\ndescription: x\nimage: i\ncommand: 'run {{n}}'\n"
        "parameters:\n"
        "  - name: n\n    type: integer\n    description: d\n    default: 1\n"
        "    arg_allowlist: ['--x']\n")
    _templates, errors = load_templates(tmp_path)
    assert "bad.yaml" in errors
    assert "only a string can carry flags" in errors["bad.yaml"]


# -- name at launch, from the agent surface -----------------------------------


async def test_the_launcher_can_name_the_box(mcp_wired):
    from app import mcp_server
    result = await mcp_server.launch_gpu(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", purpose="phase-96 name test",
        name="tally-A100-instance", note="name-at-launch test",
    )
    settled = await mcp_server.wait_for_launch(
        result["launch"]["id"], timeout=10, note="wait for name test")
    assert settled["status"] == "active"
    listed = await mcp_server.list_instances(note="check the name landed")
    names = [i["name"] for i in listed["instances"]]
    assert "tally-A100-instance" in names, (
        "the launch-time name did not reach the instance list")


# -- spend by purpose ---------------------------------------------------------


def test_breakdown_accepts_purpose_and_created_by(client):
    for by in ("purpose", "created_by"):
        resp = client.get(f"/spend/breakdown?by={by}")
        assert resp.status_code == 200, resp.text
        assert "breakdown" in resp.json()


def test_breakdown_still_refuses_nonsense(client):
    assert client.get("/spend/breakdown?by=phase_of_moon").status_code == 400


def test_unattributed_purpose_groups_truthfully():
    from app.spend import _BREAKDOWN_KEYS
    assert _BREAKDOWN_KEYS["purpose"]({"purpose": None}) == "no stated purpose"
    assert _BREAKDOWN_KEYS["purpose"]({"purpose": "Tally run"}) == "Tally run"


# -- the rescue hook says what it does not cover ------------------------------


def test_terminate_names_the_rescue_scope():
    """"files_found: 0" reads as "nothing was lost" to an agent who kept
    state in $HOME. The tool that implies safety must state its scope."""
    from app import mcp_server
    doc = mcp_server.terminate_instance.__doc__
    assert "/workspace/ephemeral" in doc
    assert "$HOME" in doc
    assert "not that nothing was lost" in doc


def test_sync_outputs_names_its_scope_too():
    from app import mcp_server
    doc = mcp_server.sync_outputs.__doc__
    assert "$HOME" in doc and "NOT in scope" in doc
