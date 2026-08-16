"""The freeze must leave evidence next time.

An intermittent freeze was reported three times and investigated with
nothing to go on: uvicorn logs to the terminal it was launched in, so each
occurrence erased its own record. These pin the three things that change
that, and the most important assertion in the file is the last one - that
the heartbeat actually fires when the loop is blocked, because a detector
that cannot detect is worse than none.
"""

import asyncio
import logging
import time

import pytest

from app.diagnostics import (
    loop_lag_monitor,
    measure_request,
    setup_file_logging,
)


# -- file logging ---------------------------------------------------------------


def test_logs_land_in_a_file_under_the_data_dir(tmp_path):
    path = setup_file_logging(tmp_path)
    assert path == tmp_path / "logs" / "manifold.log"
    logging.getLogger("manifold.test").warning("a freeze happened here")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "a freeze happened here" in path.read_text()


def test_setting_it_up_twice_does_not_double_every_line(tmp_path):
    """--reload re-runs the entry point. Stacked handlers would write every
    line N times and quietly multiply the file's size."""
    root = logging.getLogger()
    before = len(root.handlers)
    setup_file_logging(tmp_path)
    setup_file_logging(tmp_path)
    setup_file_logging(tmp_path)
    assert len(root.handlers) == before + 1


def test_an_unwritable_home_never_stops_the_backend(tmp_path):
    """A diagnostic that can prevent the app from starting is worse than no
    diagnostic."""
    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o400)
    try:
        assert setup_file_logging(blocked / "sub") is None
    finally:
        blocked.chmod(0o700)


@pytest.fixture(autouse=True)
def _drop_file_handlers():
    """Keep test log handlers out of the other tests (and out of the
    developer's own log file)."""
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_manifold_file_handler", False):
            root.removeHandler(h)
            h.close()


# -- slow requests --------------------------------------------------------------


class FakeRequest:
    def __init__(self, path="/instances", method="GET"):
        self.method = method
        self.url = type("U", (), {"path": path})()


async def test_a_slow_request_is_logged_with_its_path(caplog):
    async def slow(_request):
        await asyncio.sleep(0.05)
        return "response"

    with caplog.at_level(logging.WARNING):
        result = await measure_request(slow, FakeRequest(), 0.01)

    assert result == "response", "the response must pass through untouched"
    assert any("slow_request" in r.message and "/instances" in r.message
               for r in caplog.records)


async def test_a_fast_request_says_nothing(caplog):
    """Silence is the OTHER half of the answer: no slow_request line during
    a freeze means the backend was fine and the browser stalled."""
    async def fast(_request):
        return "ok"

    with caplog.at_level(logging.WARNING):
        await measure_request(fast, FakeRequest(), 5.0)

    assert [r for r in caplog.records if "slow_request" in r.message] == []


async def test_a_failing_request_still_raises_its_own_error(caplog):
    """Timing must never swallow or replace the real failure."""
    async def boom(_request):
        raise ValueError("the actual bug")

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match="the actual bug"):
            await measure_request(boom, FakeRequest(), 0.0)


# -- the event-loop heartbeat ---------------------------------------------------


async def test_a_blocked_loop_is_reported(caplog):
    """THE assertion. A fake clock reproduces the condition exactly: sleep
    was asked for 1s and 9s of wall time passed, so the loop had no chance
    to run for 8 of them."""
    ticks = iter([100.0, 109.0])

    async def instant(_seconds):
        return None

    with caplog.at_level(logging.WARNING):
        await loop_lag_monitor(threshold_seconds=1.0, interval_seconds=1.0,
                               clock=lambda: next(ticks), sleep=instant,
                               iterations=1)

    blocked = [r for r in caplog.records if "event_loop_blocked" in r.message]
    assert len(blocked) == 1
    assert "8.0s" in blocked[0].message


async def test_a_healthy_loop_stays_quiet(caplog):
    ticks = iter([100.0, 101.01])

    async def instant(_seconds):
        return None

    with caplog.at_level(logging.WARNING):
        await loop_lag_monitor(threshold_seconds=1.0, interval_seconds=1.0,
                               clock=lambda: next(ticks), sleep=instant,
                               iterations=1)

    assert [r for r in caplog.records if "event_loop_blocked" in r.message] == []


async def test_the_monitor_really_catches_a_real_blocking_call(caplog):
    """No fake clock: a genuine synchronous sleep inside the loop, caught by
    the real timer. This is the exact shape of the bug it exists to find -
    one blocking call, every request stalled."""
    async def blocking_sleep(seconds):
        await asyncio.sleep(seconds)
        time.sleep(0.15)          # synchronous: nothing else can run

    with caplog.at_level(logging.WARNING):
        await loop_lag_monitor(threshold_seconds=0.05, interval_seconds=0.01,
                               sleep=blocking_sleep, iterations=1)

    assert any("event_loop_blocked" in r.message for r in caplog.records)


def test_diagnostics_settings_have_usable_defaults():
    """Off by default would recreate the exact situation this fixes."""
    from app.config import DiagnosticsSettings
    d = DiagnosticsSettings()
    assert d.log_to_file is True
    # Must fire well before the dashboard's own 30s client timeout, or it
    # cannot explain the message the user actually sees.
    assert 0 < d.slow_request_seconds < 30
    assert d.loop_lag_seconds > 0
