"use client";

import { useEffect, useRef, useState } from "react";
import "@xterm/xterm/css/xterm.css";
import { wsBase } from "@/lib/backend";
import { getToken } from "@/lib/token";


// A real shell in the dashboard: xterm.js <-> backend WS. Two flavors of
// the same wire protocol: an instance shell (SSH session over the managed
// connection) or, with wsPath="/local/terminal", a shell on THIS machine
// (the local half of the hub).
//
// With a sessionId, the SHELL lives on the backend keyed by that id: a
// page refresh (the freeze-then-reload case) only drops the socket, and
// remounting with the same id reattaches to the same shell with its
// scrollback replayed - Claude keeps running through it. Unmounting the
// panel (the tab's x) sends an explicit close, which really ends the shell.
export function TerminalPanel({
  instanceId,
  wsPath,
  label,
  fill,
  sessionId,
  model,
}: {
  instanceId?: string;
  wsPath?: string;
  label?: string;
  // fill: size to the parent instead of the self-resizable h-80 box. Used
  // by the terminal drawer, whose own top-edge handle does the resizing.
  fill?: boolean;
  sessionId?: string;
  // Local shells only: pre-wire the shell's environment to this served
  // model via the OpenAI proxy (OPENAI_BASE_URL etc., set backend-side).
  model?: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  // "reconnecting" is a lost socket the panel is actively going back for;
  // "closed" means it has stopped trying, which now only happens when the
  // backend said the shell is really gone.
  const [status, setStatus] = useState<
    "connecting" | "open" | "closed" | "reconnecting"
  >("connecting");
  // Which renderer actually took: "webgl" draws glyphs on the GPU; "dom" is
  // xterm's fallback, which is MUCH slower under heavy output. Shown in the
  // header because a silent fallback is indistinguishable from a bug — if
  // the terminal is sluggish, this says whether the GPU path is even live.
  const [renderer, setRenderer] = useState<"webgl" | "dom">("dom");

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    let disposed = false;
    let cleanup: (() => void) | undefined;

    // Renderer is switchable for diagnosis: add ?renderer=dom to the URL to
    // force xterm's built-in DOM renderer instead of the GPU one. That is the
    // A/B for "is a display glitch the renderer's fault?" — if a symptom
    // survives on dom, the renderer is not the cause. (The canvas renderer is
    // not an option here: @xterm/addon-canvas still peers on xterm 5.)
    const wantDom =
      typeof window !== "undefined"
      && new URLSearchParams(window.location.search).get("renderer") === "dom";

    // xterm touches the DOM at import time in some builds; load it client-side.
    // The WebGL addon is optional: if the chunk or a WebGL context can't be
    // had (headless, blocklisted GPU), the terminal still runs on the DOM
    // renderer — so its import never blocks or breaks the shell.
    (async () => {
      const [{ Terminal }, { FitAddon }, webglMod, unicodeMod] =
        await Promise.all([
          import("@xterm/xterm"),
          import("@xterm/addon-fit"),
          wantDom
            ? Promise.resolve(null)
            : import("@xterm/addon-webgl").catch((err) => {
                console.warn("[manifold] WebGL addon failed to load:", err);
                return null;
              }),
          // Unicode 11 width tables. xterm's default tables are Unicode 6:
          // the spinners, box glyphs, and emoji a TUI like Claude Code
          // draws get the WRONG cell width there, so the app and the
          // terminal disagree about where the cursor is - which renders as
          // text drawing over itself. Optional: a load failure just keeps
          // the old tables.
          import("@xterm/addon-unicode11").catch(() => null),
        ]);
      if (disposed) return;

      // Font size is a user setting: Cmd+= / Cmd+- / Cmd+0 while the
      // terminal is focused, remembered across sessions.
      const FONT_STORE = "manifold-term-font";
      let fontSize = 13;
      try {
        const saved = parseInt(localStorage.getItem(FONT_STORE) || "", 10);
        if (saved >= 8 && saved <= 24) fontSize = saved;
      } catch {}

      const term = new Terminal({
        cursorBlink: true,
        fontSize,
        fontFamily:
          "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
        theme: { background: "#09090b", foreground: "#e4e4e7" },
        scrollback: 5000,
        // Option behaves like Meta (iTerm-style): Option+Enter inserts a
        // newline in the Claude CLI, Option+arrows jump words in shells.
        macOptionIsMeta: true,
      });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(el);

      if (unicodeMod) {
        try {
          term.loadAddon(new unicodeMod.Unicode11Addon());
          term.unicode.activeVersion = "11";
        } catch (err) {
          console.warn("[manifold] Unicode 11 tables unavailable:", err);
        }
      }

      // GPU-accelerated rendering, managed by VISIBILITY. WebGL is what
      // keeps the terminal responsive under heavy output, but live WebGL
      // contexts are a scarce browser resource (WebKit caps them per page,
      // evicting the oldest). The dock keeps every tab mounted, so holding
      // one context per HIDDEN tab is how the visible terminal gets its
      // context evicted and silently degrades to the slow DOM renderer -
      // which under a TUI's repaint rate reads as "the terminal is glitchy".
      // So: acquire WebGL when this panel becomes visible, release it when
      // hidden, and retry after a context loss instead of giving up forever.
      let webgl: { dispose(): void } | null = null;
      let webglFailures = 0;
      let retryTimer = 0;
      let panelVisible = false;
      const releaseWebgl = () => {
        if (!webgl) return;
        try {
          webgl.dispose();
        } catch {}
        webgl = null;
        setRenderer("dom");
      };
      const acquireWebgl = () => {
        if (!webglMod || webgl || webglFailures >= 3) return;
        try {
          const addon = new webglMod.WebglAddon();
          addon.onContextLoss(() => {
            // Evicted (too many contexts) or GPU reset. Release ours and,
            // if we are still the visible tab, try again in a moment.
            webglFailures += 1;
            console.warn("[manifold] WebGL context lost; will re-acquire");
            releaseWebgl();
            window.clearTimeout(retryTimer);
            retryTimer = window.setTimeout(() => {
              if (panelVisible) acquireWebgl();
            }, 1000);
          });
          term.loadAddon(addon);
          webgl = addon;
          setRenderer("webgl");
        } catch (err) {
          webglFailures += 1;
          console.warn(
            "[manifold] WebGL renderer unavailable, using the slower DOM "
            + "renderer:", err,
          );
        }
      };

      // Fit to the host element's real box, then keep the prompt in view.
      // The host has NO padding of its own (padding lives on the wrapper),
      // so FitAddon's row math is exact and the last row never clips.
      //
      // Coalesced to one run per animation frame: the ResizeObserver and the
      // dock's resize drag can both fire this dozens of times a second, and
      // an unthrottled fit.fit() (a full reflow) plus a resize send per tick
      // was a real source of jank. We also skip the PTY resize unless the
      // grid actually changed, so a drag no longer spams change_terminal_size.
      // lastCols/lastRows mean "the size the PTY has actually been TOLD", so
      // they may ONLY be updated on a successful send. Recording a size we
      // could not send (socket still connecting) made the next fit believe
      // the PTY already knew it, so the resize was never sent at all: the
      // shell stayed at its 80x24 default while the view was wider, and the
      // app wrapped at column 80 and typed back over its own line. Hence the
      // readyState check BEFORE the dedup, not after it.
      //
      // `ws` is null while we are between sockets (the backoff after a
      // dropped connection), so every use of it is guarded rather than
      // assumed live.
      let ws: WebSocket | null = null;
      let fitQueued = false;
      let lastCols = 0;
      let lastRows = 0;
      const doFit = () => {
        if (fitQueued) return;
        fitQueued = true;
        requestAnimationFrame(() => {
          fitQueued = false;
          try {
            fit.fit();
          } catch {
            return;
          }
          if (!ws || ws.readyState !== WebSocket.OPEN) return;
          if (term.cols === lastCols && term.rows === lastRows) return;
          lastCols = term.cols;
          lastRows = term.rows;
          term.scrollToBottom();
          ws.send(
            JSON.stringify({
              type: "resize",
              cols: term.cols,
              rows: term.rows,
            }),
          );
          // Full repaint after a grid change: the manual "jiggle the handle
          // until it reorganizes" fix, done automatically.
          term.refresh(0, term.rows - 1);
        });
      };

      // Keyboard niceties, handled BEFORE xterm's own key processing:
      // - Shift+Enter inserts a newline instead of sending the message.
      //   Terminals can't natively tell Shift+Enter from Enter, so we send
      //   backslash+CR, the escaped-newline form the Claude CLI (and every
      //   shell, as line continuation) understands.
      // - Cmd/Ctrl +/-/0 adjusts the font size (persisted), then refits so
      //   the PTY learns the new cols/rows.
      const setFont = (px: number) => {
        fontSize = Math.min(24, Math.max(8, px));
        term.options.fontSize = fontSize;
        try {
          localStorage.setItem(FONT_STORE, String(fontSize));
        } catch {}
        doFit();
      };
      // This handler runs for keydown, keypress AND keyup. A combo we own
      // must return false for EVERY phase: blocking only keydown left
      // xterm's keypress path free to emit its own key - which is why
      // Shift+Enter used to send our newline AND a plain Enter right
      // behind it, submitting the message anyway.
      term.attachCustomKeyEventHandler((ev) => {
        const down = ev.type === "keydown";
        if (ev.shiftKey && ev.key === "Enter") {
          if (down && ws && ws.readyState === WebSocket.OPEN) {
            // Terminals cannot natively tell Shift+Enter from Enter, so
            // send backslash+CR: the escaped newline the Claude CLI (and
            // every shell, as line continuation) understands.
            ws.send(JSON.stringify({ type: "input", data: "\\\r" }));
          }
          return false;
        }
        const mod = ev.metaKey || ev.ctrlKey;
        if (mod && (ev.key === "=" || ev.key === "+")) {
          if (down) setFont(fontSize + 1);
          return false;
        }
        if (mod && ev.key === "-") {
          if (down) setFont(fontSize - 1);
          return false;
        }
        if (mod && ev.key === "0") {
          if (down) setFont(13);
          return false;
        }
        // Cmd+K clears scrollback, like every macOS terminal. Cmd only:
        // Ctrl+K is kill-line in shells and must reach them untouched.
        if (ev.metaKey && !ev.ctrlKey && ev.key === "k") {
          if (down) term.clear();
          return false;
        }
        return true;
      });

      const path = wsPath ?? `/instances/${instanceId}/terminal`;
      // Rebuilt per attempt, not once: the API token can arrive AFTER the
      // first connect (the TokenGate unlocks mid-session), and a reconnect
      // that reused a stale query string would be refused forever.
      const socketUrl = () => {
        const params = new URLSearchParams();
        if (sessionId) params.set("session", sessionId);
        if (model) params.set("model", model);
        // Browser WebSockets cannot set headers, so the API token rides as
        // ?token= (the backend accepts either spelling on WS routes).
        const token = getToken();
        if (token) params.set("token", token);
        const qs = params.size > 0 ? `?${params.toString()}` : "";
        return `${wsBase()}${path}${qs}`;
      };

      // The backend says "do not come back" with a close code: the shell
      // ended (4410), another view stole this session (4409), or the origin
      // was refused (4403). See terminal_sessions.py. EVERY other close is
      // a lost socket - a slept laptop, a backend restart - and with a
      // session id the shell is still there, parked, waiting for exactly
      // this reattach. Phase 91 gave it an 8 hour grace to be parked IN;
      // nothing used to go back for it, so the grace bought nothing you
      // could see and a dead rectangle was the whole user experience.
      const FINAL_CLOSE = [4403, 4409, 4410];
      let attempt = 0;
      let reconnectTimer = 0;
      let reattaching = false;
      // Set by cleanup. `disposed` covers the same case, but an unmount
      // races the socket's own close event and reconnecting out of a
      // torn-down panel would resurrect a shell the user just closed.
      let closing = false;

      const connect = () => {
        if (disposed || closing) return;
        setStatus(reattaching ? "reconnecting" : "connecting");
        const sock = new WebSocket(socketUrl());
        ws = sock;
        // Flow control: ack how many chars we've actually rendered so the
        // backend can pause a firehose (or a full-screen TUI like Claude
        // Code) instead of letting xterm's write buffer grow without bound —
        // the remaining cause of the freeze under heavy output. The write
        // callback fires once xterm has parsed the chunk (the right
        // "rendered" signal) and carries NO scrollToBottom, so it doesn't
        // bring back the per-chunk reflow; xterm still auto-scrolls when the
        // viewport is at the bottom. Acks are batched (~8 KB) to avoid a
        // message per chunk. Counted per socket: attach() resets the
        // backend's side of this accounting for each new viewer.
        let unacked = 0;

        sock.onopen = () => {
          attempt = 0;
          setStatus("open");
          if (reattaching) {
            reattaching = false;
            if (sessionId) {
              // attach() replays the ENTIRE scrollback, which this terminal
              // is already displaying. Without the reset the reattach paints
              // a second copy of the session below the first.
              term.reset();
            } else {
              // No session id means no shell was kept: this is a new one,
              // and saying so beats a prompt that silently lost its state.
              term.write(
                "\r\n[manifold] reconnected. This is a NEW shell; the "
                + "previous one did not survive.\r\n",
              );
            }
          }
          // The PTY on the far side knows only the size it was last TOLD,
          // and lastCols/lastRows mean exactly that - so a fresh socket has
          // to forget them, or the first refit is deduped away and the shell
          // wraps at whatever width it had before the window was resized.
          lastCols = 0;
          lastRows = 0;
          // Fit once the socket is up so the initial resize reaches the PTY.
          requestAnimationFrame(doFit);
          term.focus();
        };
        sock.onmessage = (event) => {
          const data = event.data as string;
          term.write(data, () => {
            unacked += data.length;
            if (unacked >= 8192 && sock.readyState === WebSocket.OPEN) {
              sock.send(JSON.stringify({ type: "ack", bytes: unacked }));
              unacked = 0;
            }
          });
        };
        // onerror is always followed by onclose, so the decision lives in
        // one place rather than being made twice from half the information.
        sock.onerror = () => {};
        sock.onclose = (ev) => {
          if (ws === sock) ws = null;
          if (disposed || closing || FINAL_CLOSE.includes(ev.code)) {
            setStatus("closed");
            return;
          }
          reattaching = true;
          setStatus("reconnecting");
          // Backoff, capped at 10s: a backend that is genuinely down gets
          // retried patiently rather than hammered, but the first retry is
          // quick because the overwhelmingly common case is a socket that
          // died while the machine slept and a backend that is already fine.
          const delay = Math.min(10_000, 500 * 2 ** attempt);
          attempt += 1;
          window.clearTimeout(reconnectTimer);
          reconnectTimer = window.setTimeout(connect, delay);
        };
      };
      connect();

      // Coming back to the app should not mean waiting out a backoff that
      // was scheduled while nobody was looking. This IS the reported case:
      // step away, come back, find the terminal dead.
      const onVisible = () => {
        if (document.hidden || disposed || closing || ws) return;
        window.clearTimeout(reconnectTimer);
        attempt = 0;
        connect();
      };
      document.addEventListener("visibilitychange", onVisible);

      const dataSub = term.onData((data) => {
        // Typing always snaps the view to the cursor (a real terminal's
        // behavior). Without this, output that arrived while scrolled up
        // left the user typing "blind" below the fold, which reads as
        // text overwriting itself with no visible cursor.
        term.scrollToBottom();
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "input", data }));
        }
      });

      // Refit whenever the panel actually changes size (open animation,
      // window resize, sidebar toggles) — more reliable than a one-shot timer.
      const observer = new ResizeObserver(() => doFit());
      observer.observe(el);
      window.addEventListener("resize", doFit);
      requestAnimationFrame(doFit);

      // Visibility drives the GPU renderer (see acquireWebgl): the dock
      // hides inactive tabs with display:none, which IntersectionObserver
      // reports as not-intersecting. On show: take a WebGL context, refit
      // (the dock may have been resized while we were hidden), and repaint
      // the whole viewport - a canvas that was display:none comes back
      // stale otherwise. On hide: release the context for whoever IS
      // visible.
      const io = new IntersectionObserver((entries) => {
        const nowVisible = entries.some((e) => e.isIntersecting);
        if (nowVisible === panelVisible) return;
        panelVisible = nowVisible;
        if (nowVisible) {
          acquireWebgl();
          doFit();
          requestAnimationFrame(() => {
            try {
              term.refresh(0, term.rows - 1);
            } catch {}
          });
        } else {
          releaseWebgl();
        }
      });
      io.observe(el);

      cleanup = () => {
        // Before anything else: no close from here may schedule a reconnect.
        closing = true;
        window.clearTimeout(reconnectTimer);
        document.removeEventListener("visibilitychange", onVisible);
        io.disconnect();
        window.clearTimeout(retryTimer);
        releaseWebgl();
        observer.disconnect();
        window.removeEventListener("resize", doFit);
        dataSub.dispose();
        // Unmount = the user closed this tab (the dock never unmounts a
        // panel otherwise; a page refresh tears the socket without running
        // this). Tell the backend to really end the shell - a bare socket
        // close would leave it parked awaiting a reattach.
        if (sessionId && ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "close" }));
        }
        ws?.close();
        term.dispose();
      };
    })();

    return () => {
      disposed = true;
      cleanup?.();
    };
  }, [instanceId, wsPath, sessionId, model]);

  return (
    <div
      className={
        fill
          ? "flex h-full min-h-0 flex-col overflow-hidden rounded border border-zinc-300 bg-[#09090b]"
          : "mt-3 overflow-hidden rounded border border-zinc-300 bg-[#09090b]"
      }
    >
      <div className="flex shrink-0 items-center justify-between border-b border-zinc-200 px-3 py-1.5">
        <span
          className="text-xs text-zinc-400"
          title="Shortcuts: Shift+Return newline · Cmd +/- font size · Cmd+0 reset · Cmd+K clear"
        >
          {label ?? "Terminal (SSH via the managed connection)"}
          {fill ? "" : " · drag the bottom-right corner to resize"}
        </span>
        <span className="flex items-center gap-1.5 text-xs">
          {/* amber `dom` = the GPU renderer did not take, so heavy output
              will be slow; see the console for why. */}
          <span
            title={
              renderer === "webgl"
                ? "GPU (WebGL) renderer active"
                : "DOM renderer (slow under heavy output); WebGL unavailable, see console"
            }
            className={`font-mono ${
              renderer === "webgl" ? "text-zinc-500" : "text-amber-500"
            }`}
          >
            {renderer}
          </span>
          <span className="text-zinc-600">·</span>
          <span
            title={
              status === "reconnecting"
                ? "The socket dropped. The shell is still running on the backend; this panel is reattaching to it."
                : status === "closed"
                  ? "The shell has ended. Close this tab and open a new one."
                  : undefined
            }
            className={
              status === "open"
                ? "text-emerald-400"
                : status === "closed"
                  ? "text-red-400"
                  : status === "reconnecting"
                    ? "text-amber-400"
                    : "text-zinc-400"
            }
          >
            {status}
          </span>
        </span>
      </div>
      {/* Padding is on THIS wrapper; the xterm host below fills it exactly
          so FitAddon measures a clean, padding-free box. resize-y gives a
          native drag handle; the ResizeObserver refits rows on every drag
          (and on every drawer resize, in fill mode). */}
      <div
        className={
          fill
            ? "min-h-0 flex-1 overflow-hidden p-2"
            : "h-80 min-h-40 max-h-[85vh] resize-y overflow-hidden p-2"
        }
      >
        <div ref={containerRef} className="h-full w-full" />
      </div>
    </div>
  );
}
