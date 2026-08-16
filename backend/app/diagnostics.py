"""Make an intermittent freeze diagnose itself.

The owner reports the app freezing after being left alone: tabs stop
responding and the dashboard shows "No answer after 30s (/instances)".
Investigating it produced nothing, for one boring reason - the backend
logs to the terminal uvicorn was launched in, so by the time anyone looks,
the window is gone and the freeze left NO RECORD ANYWHERE. Every question
about it ("was the backend actually slow?", "which call hung?") was
unanswerable after the fact.

Three cheap things fix that, and together they answer the only question
worth asking first:

  THE FORK. The dashboard's own client timeout is 30s. So "no answer
  after 30s" has two completely different causes, and they need opposite
  fixes:
    - the BACKEND was slow  -> slow_request log lines name the endpoint
                               and the seconds it took
    - the backend was FINE  -> no slow_request lines at all, which means
                               the webview stalled and fired its own
                               timeout against a backend that answered

  THE MECHANISM. If everything is slow at once, the event loop is
  blocked - one synchronous call in an async handler stops every request
  in the process. A heartbeat that measures its own oversleep detects
  exactly that, and costs one timer per second.

Nothing here changes behaviour: it only writes things down. All of it is
best-effort - a diagnostic that can break the app it watches is worse than
no diagnostic.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import time
from pathlib import Path

logger = logging.getLogger("manifold.diagnostics")

# Bounded on purpose: this runs on a laptop for weeks. 5 MB x 3 is days of
# freezes at the volume below and can never eat a disk.
LOG_MAX_BYTES = 5_000_000
LOG_BACKUPS = 3


def setup_file_logging(data_root: Path) -> Path | None:
    """Mirror the app's logs into DATA_ROOT/logs/manifold.log.

    Returns the path, or None if it could not be set up - a read-only home
    directory must never stop the backend from starting. Idempotent: a
    reload that re-runs this will not stack handlers, which would otherwise
    write every line N times and quietly multiply the log's size.
    """
    try:
        log_dir = data_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "manifold.log"
        root = logging.getLogger()
        for existing in root.handlers:
            if getattr(existing, "_manifold_file_handler", False):
                return path
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        handler._manifold_file_handler = True     # type: ignore[attr-defined]
        root.addHandler(handler)
        # uvicorn's default config drops app-level INFO from the root
        # logger; without this the file would hold almost nothing.
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
        return path
    except OSError as exc:
        logger.debug("file logging not set up: %s", exc)
        return None


async def measure_request(call_next, request, threshold_seconds: float):
    """Time one request; log it if it crossed the threshold.

    The response is returned unchanged whatever happens, and a failure in
    the timing path can never turn a working request into an error.
    """
    started = time.monotonic()
    try:
        return await call_next(request)
    finally:
        try:
            elapsed = time.monotonic() - started
            if elapsed >= threshold_seconds:
                logger.warning(
                    "slow_request %s %s took %.1fs (threshold %.1fs)",
                    request.method, request.url.path, elapsed,
                    threshold_seconds)
        except Exception:   # noqa: BLE001 - never break a served request
            pass


async def loop_lag_monitor(threshold_seconds: float = 1.0,
                           interval_seconds: float = 1.0,
                           *, clock=time.monotonic, sleep=asyncio.sleep,
                           iterations: int | None = None) -> None:
    """Log whenever the event loop oversleeps by more than the threshold.

    `await sleep(1)` returning after 9 seconds means the loop had no chance
    to run for 8 of them: something synchronous held it. That is the one
    condition under which EVERY endpoint goes slow at once, and it is
    otherwise invisible - each individual handler looks innocent.

    `iterations` bounds the loop for tests; None runs forever.
    """
    count = 0
    while iterations is None or count < iterations:
        count += 1
        before = clock()
        await sleep(interval_seconds)
        lag = clock() - before - interval_seconds
        if lag >= threshold_seconds:
            # WARNING, not INFO: this is never normal, and it is the line
            # that says "the backend froze" rather than "a call was slow".
            logger.warning(
                "event_loop_blocked for %.1fs (slept %.1fs, expected %.1fs) - "
                "every request was stalled during this window",
                lag, lag + interval_seconds, interval_seconds)
