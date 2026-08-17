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

from tests.test_terminal import launch_connected, read_until


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


# -- and it only fires when it is TRUE (Phase 93) -----------------------------
#
# Shipped in Phase 91, the notice was fed on every new session id, so a
# brand-new dock tab opened to "The previous shell for this session had
# ended" - a false report of lost work on the one screen whose entire job is
# being honest about lost work. The backend cannot tell a fresh id from one
# whose shell it reaped (after a restart it remembers neither), so the client
# says which it is with ?resume=1.


def test_a_brand_new_tab_is_not_told_it_lost_a_shell(client):
    """The bug, named. No resume flag means the client never expected a shell
    here, so there is nothing to report the ending of."""
    instance_id = launch_connected(client)
    with client.websocket_connect(
            f"/instances/{instance_id}/terminal?session=brandnew") as ws:
        banner = read_until(ws, "$ ")
    assert "previous shell" not in banner
    assert "mock shell" in banner            # it is a working shell, just quiet


def test_resuming_a_session_whose_shell_is_gone_still_says_so(client):
    """The case the notice exists for, and it must survive the fix: the tab
    was restored (or the socket reconnected), and what it came back to is
    not there any more."""
    instance_id = launch_connected(client)
    url = f"/instances/{instance_id}/terminal?session=gone"
    with client.websocket_connect(url) as ws:
        read_until(ws, "$ ")
        ws.send_json({"type": "close"})      # the shell really ends
    with client.websocket_connect(f"{url}&resume=1") as ws:
        banner = read_until(ws, "$ ")
    assert "previous shell" in banner


def test_reattaching_to_a_LIVE_shell_says_nothing(client):
    """The everyday reattach. Nothing ended, so nothing is announced - and
    the scrollback that comes back must not have grown a notice in it."""
    instance_id = launch_connected(client)
    url = f"/instances/{instance_id}/terminal?session=alive"
    with client.websocket_connect(url) as ws:
        read_until(ws, "$ ")
    with client.websocket_connect(f"{url}&resume=1") as ws:
        replay = ws.receive_text()
    assert "previous shell" not in replay
    assert "mock shell" in replay             # same shell, replayed


def test_the_local_shell_follows_the_same_rule(client):
    """Both terminal routes feed this notice; the local one is where the
    user's Claude sessions actually live, so it is asserted separately
    rather than assumed to match."""
    with client.websocket_connect(
            "/local/terminal?session=fresh",
            headers={"origin": "http://localhost:3000"}) as ws:
        first = ws.receive_text()
    assert "previous shell" not in first


def test_terminal_reaped_is_a_notification_kind():
    """Off-by-default would recreate the silent kill; the toggle exists so
    the user can choose, and defaults to telling them."""
    from app.preferences import NotificationPrefs
    prefs = NotificationPrefs()
    assert prefs.terminal_reaped is True
    assert prefs.wants("terminal_reaped") is True
