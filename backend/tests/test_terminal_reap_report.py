"""Phase 91: reaping a shell is destructive, so it leaves a trace.

The owner reported it as "I lose my entire chat history with claude". The
mechanism: a frozen app is indistinguishable from a closed tab, the shell
detached, and 15 minutes later the reaper killed it - with an agent
session inside - logging one line to a stdout nobody reads. No audit row,
no notification, in a product that will not terminate an instance without
rescuing its files first.

These pin the two halves: the reaper reports before it kills, and the
grace period is no longer shorter than stepping away from the desk.
"""

import asyncio

import pytest

from app.terminal_sessions import TerminalSession, TerminalSessionManager


def make_session(session_id="local:t"):
    closed = []
    session = TerminalSession(
        session_id,
        write_input=lambda d: None,
        resize=lambda c, r: None,
        close=lambda: closed.append(True),
    )
    return session, closed


async def drive_reaper(manager, *, ticks=1):
    """Run the reap loop briefly, with sleep neutered so it does not wait
    30 real seconds per tick."""
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)
    import app.terminal_sessions as mod
    mod.asyncio.sleep = fast_sleep
    try:
        task = asyncio.create_task(manager._reap_loop())
        for _ in range(ticks + 2):
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        mod.asyncio.sleep = real_sleep


async def test_reaping_reports_before_it_kills():
    """The callback must see a LIVE session: after kill() there is nothing
    left to say what died, and the tail is the only answer to 'what did I
    lose'."""
    seen = []
    session, closed = make_session()
    session.feed_scrollback = None
    manager = TerminalSessionManager(
        grace_seconds=0.0,
        on_reap=lambda s, secs: seen.append(
            (s.id, secs, s.tail_text(80), s.exited)))
    manager.register(session)
    await session.feed("claude: analysing the launch path\r\n")
    session.detach()

    await drive_reaper(manager)

    assert len(seen) == 1, "the reap was not reported"
    sid, secs, tail, exited_at_report = seen[0]
    assert sid == "local:t"
    assert secs >= 0
    assert "analysing the launch path" in tail, (
        "the report must carry what was on screen")
    assert exited_at_report is False, "reported after the kill, too late"
    assert closed == [True], "the shell was not actually killed"
    assert manager.get("local:t") is None


async def test_a_failing_report_still_kills_the_shell():
    """A callback that raises must not leave the shell running forever -
    the reaper's whole job is bounding dead shells."""
    session, closed = make_session()
    def boom(_session, _secs):
        raise RuntimeError("notifier down")
    manager = TerminalSessionManager(grace_seconds=0.0, on_reap=boom)
    manager.register(session)
    session.detach()

    await drive_reaper(manager)

    assert closed == [True]
    assert manager.get("local:t") is None


async def test_an_attached_session_is_never_reaped():
    """Unchanged and load-bearing: only DETACHED sessions have a clock."""
    reported = []
    session, closed = make_session()
    manager = TerminalSessionManager(
        grace_seconds=0.0, on_reap=lambda s, secs: reported.append(s.id))
    manager.register(session)
    session.detached_at = None          # attached

    await drive_reaper(manager)

    assert reported == []
    assert closed == []
    assert manager.get("local:t") is not None


async def test_a_session_within_its_grace_is_left_alone():
    reported = []
    session, closed = make_session()
    manager = TerminalSessionManager(
        grace_seconds=10_000.0,
        on_reap=lambda s, secs: reported.append(s.id))
    manager.register(session)
    session.detach()

    await drive_reaper(manager)

    assert reported == []
    assert closed == []


def test_grace_default_survives_stepping_away():
    """15 minutes was shorter than a coffee break, and a frozen window looks
    exactly like a closed tab. A detached shell holds a pty and nothing
    else - it cannot keep a GPU billing, because the idle sweep counts
    terminal INPUT as activity and a detached shell produces none."""
    from app.config import HubSettings
    assert TerminalSessionManager().grace_seconds >= 8 * 3600
    assert HubSettings().terminal_grace_seconds >= 8 * 3600


# -- the replacement notice (Phase 91, part 2) --------------------------------


def test_a_replaced_shell_explains_itself():
    """Reattaching to a dead session used to hand back a bare prompt, which
    is indistinguishable from 'my work vanished'."""
    from app.main import _replaced_shell_notice
    notice = _replaced_shell_notice()
    assert "previous shell" in notice
    assert "stopped" in notice
    # Every line is prefixed so it cannot be mistaken for shell output.
    for line in notice.strip().splitlines():
        assert line.startswith("[manifold]")


def test_the_notice_points_at_surviving_claude_conversations(tmp_path,
                                                             monkeypatch):
    """Killing a shell does not delete Claude Code's transcripts: they are
    on disk, keyed by the cwd the agent ran in. Saying so is the difference
    between 'lost my history' and 'run claude --resume'."""
    from app.main import _replaced_shell_notice
    cwd = tmp_path / "work"
    cwd.mkdir()
    home = tmp_path / "home"
    encoded = str(cwd.resolve()).replace("/", "-")
    project = home / ".claude" / "projects" / encoded
    project.mkdir(parents=True)
    (project / "a.jsonl").write_text("{}\n")
    (project / "b.jsonl").write_text("{}\n")
    monkeypatch.setenv("HOME", str(home))

    notice = _replaced_shell_notice(local_cwd=str(cwd))
    assert "2 conversation(s)" in notice
    assert "claude --resume" in notice


def test_the_notice_promises_nothing_when_there_is_nothing(tmp_path,
                                                           monkeypatch):
    """No transcripts -> no resume line. A hint that leads to an empty
    picker is worse than no hint."""
    from app.main import _replaced_shell_notice
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    notice = _replaced_shell_notice(local_cwd=str(tmp_path))
    assert "claude --resume" not in notice
    assert "previous shell" in notice


def test_the_notice_never_reads_a_transcript(tmp_path, monkeypatch):
    """Only the directory listing is consulted: the conversations are the
    user's, and Manifold has no reason to open one."""
    from app.main import _replaced_shell_notice
    cwd = tmp_path / "w"
    cwd.mkdir()
    home = tmp_path / "h"
    encoded = str(cwd.resolve()).replace("/", "-")
    project = home / ".claude" / "projects" / encoded
    project.mkdir(parents=True)
    secret = "SECRET-CONVERSATION-CONTENT"
    (project / "a.jsonl").write_text(secret)
    monkeypatch.setenv("HOME", str(home))

    assert secret not in _replaced_shell_notice(local_cwd=str(cwd))


def test_terminal_reaped_is_a_notification_kind():
    """Off-by-default would recreate the silent kill; the toggle exists so
    the user can choose, and defaults to telling them."""
    from app.preferences import NotificationPrefs
    prefs = NotificationPrefs()
    assert prefs.terminal_reaped is True
    assert prefs.wants("terminal_reaped") is True
