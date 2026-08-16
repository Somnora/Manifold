"""A finished run is judged by what it DID, not by whether it said "done".

Motivated by four real runs on the owner's machine. Two ended `succeeded`
having only listed things and then emitted a fabricated summary; the one
that launched a GPU, served a model and saved a template was labelled
`exhausted` for running out of turns. The green badge was on the runs that
did nothing.
"""

from app.agent import EFFECTFUL_ACTIONS, run_effect


def step(action, result=None):
    return {"action": action, "result": {"ok": True} if result is None else result}


def test_read_only_run_has_no_effect():
    """The shot-tagger run: one listing, then a summary it invented."""
    steps = [
        step("list_instance_types", {"instance_types": {}}),
        step("done"),
    ]
    assert run_effect(steps) == {
        "effect": "no_effect", "launched": False, "terminated": False}


def test_full_lifecycle_run_acted():
    steps = [
        step("list_instance_types", {"instance_types": {}}),
        step("launch_gpu", {"launch": {"id": "abc", "status": "launching"}}),
        step("run_job", {"task": {"id": "t1"}}),
        step("terminate_instance", {"terminated": True}),
        step("done"),
    ]
    assert run_effect(steps) == {
        "effect": "acted", "launched": True, "terminated": True}


def test_refused_action_is_not_an_effect():
    """A guard rejection changes nothing, so it must not read as action.
    This is the launch_gpu-with-a-guessed-filesystem case."""
    steps = [
        step("launch_gpu", {"error": "Unknown filesystem 'manifold-fs'."}),
        step("run_job", {"error": "no connected instance"}),
        step("done"),
    ]
    assert run_effect(steps)["effect"] == "no_effect"
    assert run_effect(steps)["launched"] is False


def test_launched_without_terminating_is_visible():
    """THE expensive shape: the run that exhausted its steps mid-serve and
    left an a10 billing. Nothing else in the payload said so."""
    steps = [
        step("launch_gpu", {"launch": {"id": "abc"}}),
        step("run_job", {"task": {"id": "t1"}}),
        step("get_job_logs", {"lines": []}),
    ]
    effect = run_effect(steps)
    assert effect == {
        "effect": "acted", "launched": True, "terminated": False}


def test_json_string_results_are_parsed():
    """Steps come back from SQLite with result as TEXT in some paths; an
    unparsed string must not be mistaken for a successful action."""
    steps = [{"action": "launch_gpu", "result": '{"launch": {"id": "a"}}'}]
    assert run_effect(steps)["launched"] is True
    steps = [{"action": "launch_gpu", "result": '{"error": "budget"}'}]
    assert run_effect(steps)["launched"] is False
    steps = [{"action": "launch_gpu", "result": "not json at all"}]
    assert run_effect(steps)["launched"] is False


def test_empty_and_missing_steps_are_safe():
    for steps in ([], None):
        assert run_effect(steps) == {
            "effect": "no_effect", "launched": False, "terminated": False}


def test_save_template_counts_as_action():
    """It writes a file the user keeps; a run that only did that is not
    a run that did nothing."""
    steps = [step("save_template", {"saved": "chat-probe"}), step("done")]
    assert run_effect(steps)["effect"] == "acted"


def test_wait_and_polling_are_never_effects():
    steps = [step("wait", {"waited_seconds": 60.0}),
             step("get_launch_status", {"status": "booting"}),
             step("get_job_status", {"status": "running"}),
             step("list_templates", {"templates": []}),
             step("done")]
    assert run_effect(steps)["effect"] == "no_effect"


def test_effectful_set_matches_the_prompt_allowlist():
    """Every effectful action must be one the model is actually told it
    can call - a typo here would silently downgrade real runs."""
    from app.agent import SYSTEM_PROMPT
    for action in EFFECTFUL_ACTIONS:
        assert f"- {action} " in SYSTEM_PROMPT, action
