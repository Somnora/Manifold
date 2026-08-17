"""Phase 94c: the backend stopping is allowed; stopping in silence is not.

THE INCIDENT. The desktop app stopped at 23:21:25 on 2026-08-16 and came
back at 23:26:56 - 331 seconds - while a $1.99/hr A100 billed and five MCP
bridges got ECONNREFUSED. Nobody was told; another agent session found it
by being blocked and asking a human.

Then the diagnosis went wrong, which is the half these tests exist for.
No crash report, no jetsam record, the machine never slept, and the log
stopped mid-line with no shutdown marker - so it read as a silent crash
and hours went into hunting one. It was almost certainly a normal quit:
desktop.py's watchdog calls os._exit(0), which bypasses the lifespan, and
its only message goes to stdout. A deliberate quit and a silent crash were
INDISTINGUISHABLE IN THE RECORD.

So two things are pinned here:
  1. the classifier can tell the five ways a backend can be absent or
     unwell apart, and refuses to guess when it cannot tell;
  2. a stop leaves a tombstone, so the NEXT boot can say which happened.

And one thing is pinned by its absence: nothing in this module restarts
anything. An auto-restarter would have fought a user pressing Cmd-Q, and
worse, a restart reseeds dispatcher.last_activity and silently disables
idle auto-termination - the money guard - which is the failure this
product exists to prevent.
"""

from datetime import datetime, timedelta

import pytest

from app.liveness import (APP_GONE, BACKEND_DIED, LAGGING, UNKNOWN, UP,
                          WEDGED, Probe, classify, describe,
                          last_log_timestamp, previous_run_ended_cleanly,
                          record_stop, watch)


# -- the classifier: five ways to be absent or unwell -------------------------


def test_a_fast_200_is_up():
    assert classify(Probe(True, 200, 0.007), shell_alive=True) == UP


def test_a_slow_200_is_lagging_not_dead():
    """The nine event_loop_blocked warnings in the log, worst 4.4s. A
    backend that is alive and stalling must never be classified with one
    that is gone - the whole point of separating these is that the
    responses are opposite."""
    assert classify(Probe(True, 200, 3.5), shell_alive=True) == LAGGING


def test_a_connection_that_never_answers_is_wedged():
    """Port open, /health silent. Alive and stuck."""
    assert classify(Probe(True, None, 10.0, "timeout"),
                    shell_alive=True) == WEDGED


def test_refused_with_no_app_is_a_quit_not_a_fault():
    """THE case that actually happened. The app was gone, so the backend
    going with it is by design. Reporting this as a malfunction would cry
    wolf on every normal Cmd-Q - and tonight proved the quit is the common
    case, not the rare one."""
    assert classify(Probe(False, None, 0.0, "refused"),
                    shell_alive=False) == APP_GONE


def test_refused_while_the_app_runs_is_a_real_malfunction():
    """The case a supervisor would exist for, and the one never yet
    observed. Distinguishing it is what makes the other four trustworthy."""
    assert classify(Probe(False, None, 0.0, "refused"),
                    shell_alive=True) == BACKEND_DIED


def test_an_unanswerable_question_is_unknown_not_a_death():
    """No pgrep (Windows, stripped container) means shell_alive is None.
    Answering False there would manufacture a BACKEND_DIED verdict out of
    a question that was never asked - the same absence-as-evidence slip
    the instances payload's busy=null exists to avoid."""
    assert classify(Probe(False, None, 0.0, "refused"),
                    shell_alive=None) == UNKNOWN


def test_something_else_on_the_port_is_not_reported_as_healthy():
    assert classify(Probe(True, 404, 0.01), shell_alive=True) == WEDGED


# -- what it says -------------------------------------------------------------


def test_a_quit_is_described_as_by_design():
    text = describe(APP_GONE)
    assert "not running" in text
    assert "by design" in text, "a normal quit must not read as a fault"


def test_a_real_death_is_described_as_a_malfunction():
    text = describe(BACKEND_DIED)
    assert "malfunction" in text


def test_a_wedged_backend_is_told_nothing_will_kill_it():
    """A reader who believes the tool might act will wait for it to. This
    module never acts, so it says so."""
    text = describe(WEDGED)
    assert "restart or kill it" in text


def test_the_billing_consequence_is_named_when_something_is_running():
    """The cost of the 2026-08-16 outage was not the downtime, it was a
    GPU nobody could reach. If instances are up, the sentence says so."""
    text = describe(APP_GONE, live_launches=1)
    assert "still billing" in text
    assert "1 instance" in text


def test_no_dollar_figure_is_invented():
    """live_launches() selects no rate column. A confident wrong number is
    worse than no number, and this codebase has already published two."""
    text = describe(APP_GONE, live_launches=3)
    assert "$" not in text


def test_a_healthy_backend_does_not_mention_billing():
    assert "billing" not in describe(UP, live_launches=2)


def test_the_time_of_death_is_named():
    since = datetime(2026, 8, 16, 23, 21, 25)
    now = since + timedelta(seconds=331)
    text = describe(APP_GONE, since=since, now=now)
    assert "23:21:25" in text
    assert "5m 31s ago" in text, "the real gap, in the units a human reads"


# -- reading the log ----------------------------------------------------------


def test_the_newest_line_wins_across_ROTATED_files(tmp_path):
    """The first pass at this incident missed manifold.log.1 entirely and
    concluded a three-hour outage from the half of the record it could
    see. The real gap was 331 seconds. Rotation is not an edge case here;
    it is the thing that produced the wrong answer."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "manifold.log.1").write_text(
        "2026-08-16 20:35:56,248 INFO manifold.main: logging to ...\n"
        "2026-08-16 23:21:25,751 WARNING manifold.diagnostics: blocked\n")
    (logs / "manifold.log").write_text(
        "2026-08-17 00:38:08,712 INFO manifold.main: logging to ...\n")

    assert last_log_timestamp(logs) == datetime(2026, 8, 17, 0, 38, 8)


def test_a_missing_log_directory_is_not_an_error(tmp_path):
    assert last_log_timestamp(tmp_path / "nope") is None


def test_unstamped_junk_does_not_break_it(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "manifold.log").write_text("Traceback...\n  File x\nboom\n")
    assert last_log_timestamp(logs) is None


# -- the tombstone ------------------------------------------------------------


def _audit_db(tmp_path):
    import sqlite3
    path = tmp_path / "manifold.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY "
                 "AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, "
                 "action TEXT NOT NULL, detail TEXT)")
    conn.commit()
    conn.close()
    return path


def test_a_stop_is_recorded(tmp_path):
    path = _audit_db(tmp_path)
    assert record_stop(path, "shell gone (app quit)") is True

    import sqlite3
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT actor, action, detail FROM audit_log").fetchall()
    conn.close()
    assert rows == [("backend", "backend_stopped", "shell gone (app quit)")]


def test_recording_a_stop_never_raises(tmp_path):
    """It runs immediately before os._exit(0). A tombstone that could wedge
    or crash a shutdown is worse than no tombstone."""
    assert record_stop(tmp_path / "does-not-exist" / "x.db", "quit") is False


def test_a_clean_previous_run_is_recognised(tmp_path):
    path = _audit_db(tmp_path)
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO audit_log (at, actor, action) "
                 "VALUES ('t1','backend','backend_started')")
    conn.execute("INSERT INTO audit_log (at, actor, action) "
                 "VALUES ('t2','backend','backend_stopped')")
    conn.commit()
    conn.close()
    # A started row is written by the CURRENT boot before the check in real
    # life; here the newest row is the stop, so the previous run is clean.
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO audit_log (at, actor, action) "
                 "VALUES ('t3','backend','backend_started')")
    conn.commit()
    conn.close()
    assert previous_run_ended_cleanly(path) is True


def test_a_run_that_vanished_is_recognised(tmp_path):
    """Two starts in a row with no stop between them: the first run died
    without recording anything. THIS is the signal that was missing on
    2026-08-16."""
    path = _audit_db(tmp_path)
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO audit_log (at, actor, action) "
                 "VALUES ('t1','backend','backend_started')")
    conn.execute("INSERT INTO audit_log (at, actor, action) "
                 "VALUES ('t2','backend','backend_started')")
    conn.commit()
    conn.close()
    assert previous_run_ended_cleanly(path) is False


def test_no_history_is_None_not_a_crash_report(tmp_path):
    """A first boot, or a database predating the tombstone, must report
    "cannot tell" rather than "it crashed". Waking someone up over missing
    history is how a useful alarm becomes one people mute."""
    assert previous_run_ended_cleanly(_audit_db(tmp_path)) is None
    assert previous_run_ended_cleanly(tmp_path / "absent.db") is None


# -- the watcher ---------------------------------------------------------------


def test_the_watcher_speaks_only_when_the_answer_changes(tmp_path, monkeypatch):
    """A watcher that reports every poll is one nobody reads, and this
    exists because a real outage went unnoticed."""
    import app.liveness as mod
    states = iter([Probe(True, 200, 0.01), Probe(True, 200, 0.01),
                   Probe(False, None, 0.0, "refused"),
                   Probe(False, None, 0.0, "refused"),
                   Probe(True, 200, 0.01)])
    monkeypatch.setattr(mod, "probe_health", lambda *a, **k: next(states))
    monkeypatch.setattr(mod, "shell_running", lambda *a, **k: False)
    said = []

    watch(api_url="http://x", data_root=tmp_path, notify=said.append,
          sleep=lambda _s: None, iterations=5)

    assert [v.state for v in said] == [UP, APP_GONE, UP], (
        "one line per transition, not per poll")


def test_the_module_cannot_restart_anything():
    """The load-bearing absence. Attacking the alternatives found that a
    restart reseeds dispatcher.last_activity and thereby disables idle
    auto-termination - a safety feature switching off the money guard. If
    someone later adds a restart here, this fails and they must argue with
    the comment above rather than with nobody."""
    import inspect

    import app.liveness as mod
    src = inspect.getsource(mod)
    for forbidden in ("Popen", "os.system", "os.kill", "SIGKILL",
                      "SIGTERM", "terminate()", ".spawn("):
        assert forbidden not in src, (
            f"liveness.py grew the ability to act ({forbidden}); it is a "
            f"reporter by design")


def test_liveness_imports_nothing_that_could_act():
    """Mirrors the AST guard over mcp_server.py. The module may read the
    database and probe a port; it may not reach the orchestrator, the
    cloud, or SSH."""
    import ast
    import pathlib
    src = pathlib.Path(mod_path()).read_text()
    banned = {"orchestrator", "lambda_api", "connections", "dispatcher",
              "asyncssh", "sidecar_client"}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[-1] not in banned, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[-1] not in banned, alias.name


def mod_path():
    import app.liveness
    return app.liveness.__file__
