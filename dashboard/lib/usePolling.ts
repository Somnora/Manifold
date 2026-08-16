"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Poll an async loader on an interval. Boring by design: the backend is
// local, payloads are tiny, and polling keeps the dashboard state honest
// without a websocket layer. `refresh()` forces an immediate reload.
//
// Two rules keep it from becoming the freeze it was. Both exist because
// `setInterval` fires on a schedule while a request takes however long it
// takes, and lib/api.ts waits 30 SECONDS before giving up:
//
//  1. One poll at a time. A tick that lands while the previous one is
//     still out is dropped (or deferred, if someone asked for it). Without
//     this, a slow backend makes every hook queue a new fetch every
//     interval while none of them finish - arrivals outrun completions,
//     so the queue grows without bound. A browser only opens ~6
//     connections per origin, so the rest sit in its network queue, and
//     the fetch behind a click on a tab sits there too. That is the
//     "app froze, I have to restart it" report: the queue never drains,
//     because the pile-up is what makes it slow.
//
//  2. A hidden document does not poll. Timers in a background window are
//     throttled and coalesced, then fire in a burst on wake, which is
//     precisely the moment the backend is slowest (stale connections, a
//     laptop that just resumed). Stepping away used to build the pile-up
//     that greeted you on return. Coming back polls once, immediately.
export function usePolling<T>(
  load: () => Promise<T>,
  intervalMs: number,
): {
  data: T | null;
  error: string;
  /** true when data exists but the LATEST poll failed: what's on screen is
      a snapshot from lastSuccess, not live state. Render it as such. */
  stale: boolean;
  lastSuccess: Date | null;
  refresh: () => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [lastSuccess, setLastSuccess] = useState<Date | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;
  const inFlight = useRef(false);
  const queued = useRef(false);

  const tick = useCallback(async () => {
    if (inFlight.current) {
      // Someone wants fresh data and a poll is already out. Remember it
      // and run once more when that one lands, rather than dropping the
      // request: refresh() after a mutation must not leave stale state on
      // screen for a whole interval. Interval ticks set the same flag, so
      // at most ONE extra poll is ever owed, however many pile up here.
      queued.current = true;
      return;
    }
    inFlight.current = true;
    try {
      do {
        queued.current = false;
        try {
          setData(await loadRef.current());
          setError("");
          setLastSuccess(new Date());
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e));
        }
      } while (queued.current);
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    tick();
    const id = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      tick();
    }, intervalMs);
    // Back from another tab, another app, or a sleeping laptop: reload now
    // instead of showing whatever was true when the window was hidden.
    const onVisible = () => {
      if (!document.hidden) tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [tick, intervalMs]);

  return { data, error, stale: !!(error && data), lastSuccess, refresh: tick };
}
