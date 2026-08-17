"""Phase 94b: a GPU at 100% is not idle, whatever Manifold saw of the traffic.

THE INCIDENT, from Manifold's own tables.

  audit_log     2026-08-16T07:36:56  backend  idle_termination
                4718a91f... idle 1811s (limit 1800s)

  telemetry_samples, same instance, sampled every ~32s:
                07:36:57   36653/40960 MiB   util_pct 100
                07:36:23   36653/40960 MiB   util_pct 100
                07:35:51   36653/40960 MiB   util_pct 100
                07:34:48   36653/40960 MiB   util_pct 100

Manifold sampled a GPU pinned at 100% with 36GB of VRAM held, wrote it to
its own database, and terminated the instance for inactivity in the same
second. A 126-workflow extraction run failed at 07:42 with retry exhaustion,
about six minutes after its model endpoint disappeared.

The mechanism: `touch_activity` is called by jobs, the terminal, the chat
panel and the OpenAI proxy - everything that goes THROUGH Manifold. That
box was serving a model over the user's own SSH tunnel, so nothing ever
reached it, and "no traffic we can see" was read as "no work happening".
The same inference an agent made about a loading box the same night, one
layer down, with better evidence available and unread.

WHAT THIS IS AND IS NOT. It is a NARROWING of the reaper, not a loosening:
a genuinely abandoned box reads near-zero utilization and is still taken on
schedule, which is the Phase 90 case and is pinned below. It only adds
protection where there is positive evidence of work.
"""

import pytest

from tests.test_idle_matrix import NOW, TIMEOUT, Harness


@pytest.fixture
def harness(tmp_path, db):
    return Harness(tmp_path, db)


def sample(harness, instance_id, util, *, seconds_ago=10.0, vram=36653):
    """Lay down one telemetry row, stamped on the wall clock.

    Deliberately real timestamps: the sweep compares telemetry rows against
    utcnow(), while the harness drives a frozen monotonic _clock. A test
    that stamped rows with the fake clock would select nothing and pass for
    the wrong reason.
    """
    from datetime import datetime, timedelta, timezone
    at = (datetime.now(timezone.utc)
          - timedelta(seconds=seconds_ago)).isoformat()
    harness.db.record_telemetry_sample(
        instance_id, gpu_name="NVIDIA A100-SXM4-40GB",
        vram_used_mib=vram, vram_total_mib=40960,
        util_pct=util, util_pct_mean=float(util), gpu_count=1, at=at)


async def test_a_gpu_at_full_tilt_is_not_reaped(harness):
    """THE bug, in the shape it actually happened: idle past the limit by
    Manifold's reckoning, GPU at 100% by Manifold's own telemetry."""
    harness.add_instance("i-working")            # idle_for = TIMEOUT + 60
    sample(harness, "i-working", 100)

    await harness.dispatcher._check_idle()

    assert harness.terminated == []
    status = harness.dispatcher.activity_status("i-working")
    assert status["state"] == "gpu_busy"
    assert status["busy"] is True
    assert "100%" in status["reason"]


async def test_a_spiky_sample_run_still_counts_as_busy(harness):
    """Utilization is instantaneous. The real box read 100, 100, 0, 100,
    100, 0 across consecutive samples WHILE SERVING, so a rule that looked
    at the latest sample would reap it half the time - a coin flip on
    someone's job."""
    harness.add_instance("i-spiky")
    for i, util in enumerate([100, 100, 0, 100, 100, 0]):
        sample(harness, "i-spiky", util, seconds_ago=10 + i * 32)

    await harness.dispatcher._check_idle()

    assert harness.terminated == []


async def test_a_working_box_reads_busy_LONG_BEFORE_it_is_near_reaping(harness):
    """The defect the first version of this shipped with, caught against a
    live instance rather than by these tests.

    Protecting the box from the reaper is only half the job. An agent
    deciding whether to terminate reads `activity`, and the reap gate does
    not run until the idle window is nearly spent - so a box 30 seconds into
    a 7200s window reported state "idle_countdown", busy=false, while its
    GPU sat at 100% with 36GB held. That is the exact reading that destroyed
    a model server, being served by the field built to prevent it.

    The original tests all drove the sweep to the reap point and so never
    asked what a READER sees mid-countdown.
    """
    harness.add_instance("i-early", idle_for=30)      # nowhere near TIMEOUT
    sample(harness, "i-early", 100)

    await harness.dispatcher._check_idle()

    status = harness.dispatcher.activity_status("i-early")
    assert status["state"] == "gpu_busy"
    assert status["busy"] is True, (
        "a reader was told a GPU at 100% is not busy")
    assert harness.terminated == []


async def test_a_quiet_box_mid_window_still_reads_idle(harness):
    """The other half: if every countdown said busy, the field would be
    noise and a reader would learn to ignore it - which is how it failed
    the first time."""
    harness.add_instance("i-early-quiet", idle_for=30)
    sample(harness, "i-early-quiet", 0)

    await harness.dispatcher._check_idle()

    status = harness.dispatcher.activity_status("i-early-quiet")
    assert status["state"] == "idle_countdown"
    assert status["busy"] is False


async def test_an_abandoned_box_is_still_reaped_on_schedule(harness):
    """The Phase 90 case, and the one that pays for this product. Samples
    exist and they all say nothing is happening: that is evidence of no
    work, and it must still cost the user nothing."""
    harness.add_instance("i-abandoned")
    for i in range(6):
        sample(harness, "i-abandoned", 0, seconds_ago=10 + i * 32)

    await harness.dispatcher._check_idle()

    assert [i for i, _ in harness.terminated] == ["i-abandoned"]


async def test_a_loaded_but_unused_model_is_still_reaped(harness):
    """VRAM is residency, not work. An abandoned vLLM holds 30GB forever
    and answers nobody; protecting on memory would undo Phase 90 and
    recreate the hour-long bill it was written for."""
    harness.add_instance("i-parked")
    for i in range(4):
        sample(harness, "i-parked", 0, seconds_ago=10 + i * 32, vram=39000)

    await harness.dispatcher._check_idle()

    assert [i for i, _ in harness.terminated] == ["i-parked"]


async def test_no_telemetry_at_all_leaves_the_old_behaviour(harness):
    """A box with no samples - sidecar never came up, telemetry broken - is
    no evidence either way, and gets exactly the behaviour it got before
    this existed. Stated as a test so the limit is deliberate rather than
    discovered later: this check only ever ADDS protection."""
    harness.add_instance("i-silent")

    await harness.dispatcher._check_idle()

    assert [i for i, _ in harness.terminated] == ["i-silent"]


async def test_utilization_below_the_threshold_does_not_protect(harness):
    """An idle card still reports 1-2%. If noise counted as work, nothing
    would ever be reaped and the guard would be a billing bug."""
    harness.add_instance("i-noise")
    for i in range(4):
        sample(harness, "i-noise", 2, seconds_ago=10 + i * 32)

    await harness.dispatcher._check_idle()

    assert [i for i, _ in harness.terminated] == ["i-noise"]


async def test_old_samples_do_not_protect_forever(harness):
    """Work from before the window is history, not a reason to keep
    billing. A box busy an hour ago and silent since is abandoned."""
    harness.add_instance("i-was-busy")
    sample(harness, "i-was-busy", 100, seconds_ago=TIMEOUT + 600)

    await harness.dispatcher._check_idle()

    assert [i for i, _ in harness.terminated] == ["i-was-busy"]


async def test_the_deferral_is_audited_once_not_every_poll(harness):
    """A long job must leave a trace the user can find, without writing a
    row every 15 seconds for six hours."""
    harness.add_instance("i-long")
    sample(harness, "i-long", 100)

    for _ in range(3):
        await harness.dispatcher._check_idle()

    rows = [r for r in harness.db.list_audit(limit=50)
            if r["action"] == "idle_deferred_gpu_busy"]
    assert len(rows) == 1
    assert "i-long" in rows[0]["detail"]


async def test_the_ceiling_still_kills_a_busy_box(harness):
    """The money backstop, unchanged and load-bearing. GPU utilization
    defers the IDLE verdict only; a max-lifetime ceiling is the one guard
    that applies to a box doing real work, and if this broke it, an
    infinite training loop would bill forever."""
    launch_id = harness.add_instance("i-eternal")
    harness.db.update_launch(launch_id, max_lifetime_seconds=1.0,
                             launched_at="2020-01-01T00:00:00+00:00")
    sample(harness, "i-eternal", 100)

    await harness.dispatcher._check_idle()

    assert [i for i, _ in harness.terminated] == ["i-eternal"]


async def test_the_check_can_be_switched_off(harness):
    """busy_util_pct = 0 restores judging by Manifold-visible traffic
    alone, for anyone who would rather have the old bill than the old
    surprise."""
    from dataclasses import replace
    harness.dispatcher.settings = replace(
        harness.dispatcher.settings,
        idle=replace(harness.dispatcher.settings.idle, busy_util_pct=0))
    harness.add_instance("i-working")
    sample(harness, "i-working", 100)

    await harness.dispatcher._check_idle()

    assert [i for i, _ in harness.terminated] == ["i-working"]


def test_peak_and_sample_count_are_reported_separately(db):
    """No samples and all-zero samples are opposite findings. Collapsing
    them into a bare peak of 0 would rebuild, inside the fix, the inference
    the fix exists to remove."""
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    assert db.peak_util_since("i-nothing", old) == {"samples": 0,
                                                    "peak": None}

    db.record_telemetry_sample("i-quiet", gpu_name="A100",
                               vram_used_mib=100, vram_total_mib=40960,
                               util_pct=0, at=recent)
    assert db.peak_util_since("i-quiet", old) == {"samples": 1, "peak": 0}


def test_keep_alive_is_reachable_from_the_agent_surface():
    """The escape hatch was recommended three times before anyone noticed
    the MCP surface had no setter for it: the route and the dashboard
    button existed, the tool did not. An override an agent cannot call is
    not an override."""
    from app import mcp_server
    assert hasattr(mcp_server, "set_keep_alive")
    doc = mcp_server.set_keep_alive.__doc__
    assert "COSTS MONEY" in doc, "the billing consequence must be stated"
    assert "max_lifetime_seconds" in doc, "the backstop must be named"
