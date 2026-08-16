"""Phase 90: an abandoned model server is idle; a busy or loading one is not.

Before this, ANY running task made its instance immune to the idle sweep,
and a vllm-serve task never leaves 'running'. The result was the one bill
this product exists to prevent: an autopilot run ran out of steps one turn
short of its terminate, left an a10 serving Qwen, and the idle guard could
never fire. It billed for an hour until a human looked.

The fix judges a server by whether anyone is using it. These rows pin all
three answers, and the two that fail SAFE matter most: this is unattended
code that destroys paid instances, so anything short of "ready and silent"
must leave the box alone.
"""

import pytest

from tests.test_idle_matrix import NOW, TIMEOUT, Harness


@pytest.fixture
def harness(tmp_path, db):
    return Harness(tmp_path, db)


def ready(harness, is_ready: bool, *, boom: bool = False):
    """Pin what the readiness probe says, without a real model."""
    async def _probe(instance_id, task_id, port):
        if boom:
            raise RuntimeError("probe exploded")
        return {"ready": is_ready, "error": "" if is_ready else "loading"}
    harness.dispatcher.model_ready = _probe


async def test_ready_and_silent_server_is_terminated(harness):
    """THE bug: answering /v1/models, nobody asking it anything, a full
    idle window gone by. That is an abandoned server, and it now dies."""
    harness.add_instance("i-abandoned")
    harness.pin_task("i-abandoned", "vllm-serve")
    ready(harness, True)

    await harness.dispatcher._check_idle()

    assert [i for i, _ in harness.terminated] == ["i-abandoned"]


async def test_loading_server_is_never_reaped(harness):
    """Weights for a large model can outlast the idle window. Reaping a box
    seconds before it becomes useful is the worst possible moment, so
    serving-but-not-ready counts as activity and RESTARTS the clock."""
    harness.add_instance("i-loading")
    harness.pin_task("i-loading", "vllm-serve")
    ready(harness, False)

    await harness.dispatcher._check_idle()

    assert harness.terminated == []
    # The clock was reset, so the window now runs from readiness, not from
    # dispatch: the next sweep does not immediately destroy it either.
    assert harness.dispatcher.last_activity["i-loading"] == NOW


async def test_a_probe_that_raises_protects_the_box(harness):
    """Fail safe. A probe error is not evidence that nobody is using the
    model, and the cost of being wrong here is a destroyed instance."""
    harness.add_instance("i-unprobeable")
    harness.pin_task("i-unprobeable", "vllm-serve")
    ready(harness, True, boom=True)

    await harness.dispatcher._check_idle()

    assert harness.terminated == []


async def test_a_server_in_use_is_left_alone(harness):
    """/v1/chat/completions and the chat panel already call touch_activity,
    so a model someone is talking to never reaches the timeout."""
    harness.add_instance("i-busy", idle_for=TIMEOUT - 60)
    harness.pin_task("i-busy", "vllm-serve")
    ready(harness, True)

    await harness.dispatcher._check_idle()

    assert harness.terminated == []


async def test_batch_job_still_pins_absolutely(harness):
    """Unchanged, and the reason the fix is narrow: a fine-tune at 90% is
    destroyed by no sweep, ready or not, silent or not."""
    harness.add_instance("i-finetune")
    harness.pin_task("i-finetune", "whisper-batch")
    ready(harness, True)

    await harness.dispatcher._check_idle()

    assert harness.terminated == []


async def test_batch_beside_a_server_still_pins(harness):
    """Server and batch coexist on one box. The batch job wins: the
    instance is protected exactly as if the server were not there."""
    harness.add_instance("i-both")
    harness.pin_task("i-both", "vllm-serve")
    harness.pin_task("i-both", "whisper-batch")
    ready(harness, True)

    await harness.dispatcher._check_idle()

    assert harness.terminated == []


async def test_keep_alive_still_wins_over_an_idle_server(harness):
    """The user's explicit escape hatch is checked BEFORE readiness, so
    turning it on means what it has always meant."""
    harness.add_instance("i-pinned-by-hand", keep_alive=True)
    harness.pin_task("i-pinned-by-hand", "vllm-serve")
    ready(harness, True)

    await harness.dispatcher._check_idle()

    assert harness.terminated == []


async def test_disconnected_server_is_not_probed_or_reaped(harness):
    """An unreachable box cannot be rescued, so it is never terminated by
    the idle path - and its clock is dropped rather than counted."""
    harness.add_instance("i-gone", connected=False)
    harness.pin_task("i-gone", "vllm-serve")

    def _explode(*a, **k):
        raise AssertionError("must not probe a disconnected instance")
    harness.dispatcher.model_ready = _explode

    await harness.dispatcher._check_idle()

    assert harness.terminated == []
