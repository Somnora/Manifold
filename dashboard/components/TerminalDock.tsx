"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { TerminalPanel } from "@/components/TerminalPanel";
import { ChatPanel } from "@/components/ChatPanel";
import { RecentFiles } from "@/components/RecentFiles";
import { FileNavigator } from "@/components/FileNavigator";

// The dock: one shared surface for every instance-scoped work panel -
// local shells, instance shells, chat, recent files, and the file browser -
// snappable to the BOTTOM or the RIGHT side of the viewport, with two
// arrangements:
//
//   tabs   one shell visible, the rest mounted-but-hidden behind tabs
//   split  all shells visible: side by side when the dock is at the bottom,
//          stacked when it is on the right (each pane keeps a usable width)
//
// Invariants carried over from the single-terminal drawer (learned the hard
// way in Phase 38):
//
// - Rendered through a portal to document.body. The header's backdrop-blur
//   creates a CSS containing block, which re-anchors position:fixed
//   descendants to the header instead of the viewport.
// - While open, the dock's exact footprint is reserved as padding on the
//   page body (bottom or right to match the snap), so the dock never hides
//   content: the page always scrolls fully clear of it.
// - Sessions stay MOUNTED while hidden (behind a tab, or with the dock
//   closed), so a shell - and anything running in it, codex included -
//   survives tab switches, dock hides, and page navigation. A session ends
//   only when its tab's x is clicked or the shell exits.
// - The dock ALSO survives a page refresh: the tab list and layout live in
//   sessionStorage, and each shell's process lives on the backend keyed by
//   its session id (terminal_sessions.py), so reloading a frozen app
//   reattaches every shell - scrollback and all - instead of starting
//   Claude setup over. sessionStorage is per-tab and dies with it, which is
//   exactly the wanted scope: refresh = keep, close the app = fresh start
//   (the backend reaps the detached shells after a grace window).

export type PanelKind = "local" | "instance" | "chat" | "files" | "browse";

type Session = {
  id: string;
  kind: PanelKind;
  instanceId?: string;
  label: string;
  // Local shells only: shell env pre-wired to this served model (the
  // "Open in terminal" button on a running serve job).
  model?: string;
  // This tab came back from sessionStorage, so a shell for it SHOULD
  // already exist on the backend. Only the browser knows that: to the
  // backend a restored id and a brand-new one look identical. It rides
  // the socket as ?resume=1 and decides whether "the previous shell for
  // this session had ended" is a true statement or a false alarm.
  resumed?: boolean;
};

type Position = "bottom" | "right";
type Arrangement = "tabs" | "split";

type DockState = {
  open: boolean;
  sessions: Session[];
  toggleLocal: () => void;
  // Another shell to the same place: a second Local Machine tab, or a
  // second SSH tab on the same instance.
  addLocal: () => void;
  dockInstance: (instanceId: string, name: string) => void;
  // Chat / recent files / file browser for an instance, as a dock tab.
  dockPanel: (kind: "chat" | "files" | "browse", instanceId: string,
              name: string) => void;
  // A local shell whose env is pre-wired to a served model (OPENAI_BASE_URL
  // at the proxy, MANIFOLD_MODEL set): any OpenAI-compatible CLI started in
  // it talks to the user's own GPU.
  openModelShell: (model: string) => void;
};

const DockContext = createContext<DockState | null>(null);

export function useTerminalDock(): DockState {
  const ctx = useContext(DockContext);
  if (!ctx) throw new Error("useTerminalDock outside TerminalDockProvider");
  return ctx;
}

const LOCAL: Session = {
  id: "local",
  kind: "local",
  label: "Local Machine",
};

const PANEL_TAG: Record<PanelKind, { tag: string; tone: string }> = {
  local: { tag: ">_", tone: "text-teal-400" },
  instance: { tag: "gpu", tone: "text-emerald-500" },
  chat: { tag: "chat", tone: "text-sky-500" },
  files: { tag: "files", tone: "text-indigo-400" },
  browse: { tag: "fs", tone: "text-amber-500" },
};

const DOCK_STORE = "manifold-dock";

const MIN_HEIGHT = 180;
const MIN_WIDTH = 360;
const DEFAULT_HEIGHT = 320;
const DEFAULT_WIDTH = 520;
// The right-snapped dock starts below the sticky header (py-3 + h-8 content
// + 1px border) so the nav, burn chip, and bell stay reachable.
const HEADER_PX = 57;

export function TerminalDockProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [active, setActive] = useState<string>("local");
  const [position, setPosition] = useState<Position>("bottom");
  const [arrangement, setArrangement] = useState<Arrangement>("tabs");
  const [height, setHeight] = useState(DEFAULT_HEIGHT);
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  // Portals need document, and the save effect must not run until the
  // stored state has been restored (or it would clobber it with defaults).
  const [hydrated, setHydrated] = useState(false);

  // Restore the dock from sessionStorage after a refresh; each terminal
  // panel then reattaches to its still-running backend shell by id.
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(DOCK_STORE);
      if (raw) {
        const saved = JSON.parse(raw);
        if (Array.isArray(saved.sessions) && saved.sessions.length > 0) {
          // Every restored tab is resuming, whatever the stored object says:
          // a tab opened during the LAST page load was saved without the
          // flag, and it is just as much a reattach as any other.
          setSessions(saved.sessions.map(
            (s: Session) => ({ ...s, resumed: true })));
          setActive(saved.active ?? saved.sessions[0].id);
          setOpen(!!saved.open);
        }
        if (saved.position === "right" || saved.position === "bottom")
          setPosition(saved.position);
        if (saved.arrangement === "tabs" || saved.arrangement === "split")
          setArrangement(saved.arrangement);
        if (typeof saved.height === "number") setHeight(saved.height);
        if (typeof saved.width === "number") setWidth(saved.width);
      }
    } catch {
      // Bad/absent stored state: start clean.
    }
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    try {
      sessionStorage.setItem(
        DOCK_STORE,
        JSON.stringify({
          sessions,
          active,
          open,
          position,
          arrangement,
          height,
          width,
        }),
      );
    } catch {
      // Storage full/blocked: the dock still works, it just won't survive
      // a refresh.
    }
  }, [hydrated, sessions, active, open, position, arrangement, height, width]);

  // Monotonic per-mount counter folded into generated session ids: two
  // clicks inside the same millisecond used to mint the same Date.now() id,
  // and duplicate ids as React keys break the tab list outright.
  const idSeq = useRef(0);

  const toggleLocal = useCallback(() => {
    setSessions((s) => (s.some((x) => x.id === "local") ? s : [LOCAL, ...s]));
    // Branch on the rendered snapshot, not inside the setOpen updater:
    // updaters must stay pure (StrictMode runs them twice), so the
    // setActive side effect lives out here.
    if (open && active !== "local" && arrangement === "tabs") {
      // The dock is open but local is buried behind another tab: the
      // button surfaces it instead of closing the dock.
      setActive("local");
    } else {
      setOpen(!open);
      if (!open) setActive("local");
    }
  }, [open, active, arrangement]);

  const dockInstance = useCallback((instanceId: string, name: string) => {
    const id = `instance:${instanceId}`;
    setSessions((s) =>
      s.some((x) => x.id === id)
        ? s
        : [
            ...s,
            {
              id,
              kind: "instance",
              instanceId,
              label: name || instanceId.slice(0, 12),
            },
          ],
    );
    setActive(id);
    setOpen(true);
  }, []);

  const addLocal = useCallback(() => {
    const n = sessions.filter((x) => x.kind === "local").length + 1;
    const id =
      n === 1 ? "local" : `local-${Date.now()}-${++idSeq.current}`;
    setSessions((s) =>
      s.some((x) => x.id === id)
        ? s
        : [
            ...s,
            {
              ...LOCAL,
              id,
              label: n === 1 ? LOCAL.label : `${LOCAL.label} ${n}`,
            },
          ],
    );
    setActive(id);
    setOpen(true);
  }, [sessions]);

  // A second shell to the same target: duplicates get a fresh id (a fresh
  // WebSocket, a fresh pty) and a numbered label.
  const duplicate = useCallback(
    (s: Session) => {
      const id = `${s.id}~${Date.now()}-${++idSeq.current}`;
      const base = s.label.replace(/ \d+$/, "");
      const n =
        sessions.filter(
          (x) => x.kind === s.kind && x.instanceId === s.instanceId,
        ).length + 1;
      setSessions((all) => [...all, { ...s, id, label: `${base} ${n}` }]);
      setActive(id);
    },
    [sessions],
  );

  const openModelShell = useCallback((model: string) => {
    const short = model.split("/").pop() || model;
    const id = `model:${model}`;
    setSessions((s) =>
      s.some((x) => x.id === id)
        ? s
        : [...s, { id, kind: "local" as const, model, label: `${short} CLI` }],
    );
    setActive(id);
    setOpen(true);
  }, []);

  const dockPanel = useCallback(
    (kind: "chat" | "files" | "browse", instanceId: string, name: string) => {
      const id = `${kind}:${instanceId}`;
      setSessions((s) =>
        s.some((x) => x.id === id)
          ? s
          : [
              ...s,
              {
                id,
                kind,
                instanceId,
                label: name || instanceId.slice(0, 12),
              },
            ],
      );
      setActive(id);
      setOpen(true);
    },
    [],
  );

  const closeSession = useCallback(
    (id: string) => {
      // The filter runs functionally (correct under rapid closes); the
      // focus/open corrections use the rendered snapshot so the updater
      // itself stays pure (StrictMode runs updaters twice).
      setSessions((s) => s.filter((x) => x.id !== id));
      const next = sessions.filter((x) => x.id !== id);
      if (next.length === 0) setOpen(false);
      else if (active === id) setActive(next[next.length - 1].id);
    },
    [sessions, active],
  );

  // Reserve the dock's footprint at the matching page edge (see above).
  useEffect(() => {
    document.body.style.paddingBottom =
      open && position === "bottom" ? `${height}px` : "";
    document.body.style.paddingRight =
      open && position === "right" ? `${width}px` : "";
    return () => {
      document.body.style.paddingBottom = "";
      document.body.style.paddingRight = "";
    };
  }, [open, position, height, width]);

  // Drag the dock's inner edge to resize: top edge when snapped to the
  // bottom, left edge when snapped to the right. Clamped so it can neither
  // collapse nor swallow the window.
  const startDrag = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      // Coalesce pointer moves to one state update per frame. Each setHeight/
      // setWidth re-renders the dock and fires every terminal's ResizeObserver
      // (a fit reflow apiece), so an un-throttled drag was a jank multiplier.
      let raf = 0;
      let pending: PointerEvent | null = null;
      const apply = () => {
        raf = 0;
        const ev = pending;
        if (!ev) return;
        if (position === "bottom") {
          const h = window.innerHeight - ev.clientY;
          setHeight(
            Math.min(
              Math.max(h, MIN_HEIGHT),
              Math.round(window.innerHeight * 0.8),
            ),
          );
        } else {
          const w = window.innerWidth - ev.clientX;
          setWidth(
            Math.min(
              Math.max(w, MIN_WIDTH),
              Math.round(window.innerWidth * 0.7),
            ),
          );
        }
      };
      const move = (ev: PointerEvent) => {
        pending = ev;
        if (!raf) raf = requestAnimationFrame(apply);
      };
      const stop = () => {
        if (raf) cancelAnimationFrame(raf);
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", stop);
        document.body.style.userSelect = "";
      };
      document.body.style.userSelect = "none";
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop);
    },
    [position],
  );

  const bottom = position === "bottom";
  const split = arrangement === "split" && sessions.length > 1;

  return (
    <DockContext.Provider
      value={{ open, sessions, toggleLocal, addLocal, dockInstance, dockPanel,
               openModelShell }}
    >
      {children}

      {hydrated &&
        sessions.length > 0 &&
        createPortal(
          <div
            className={`fixed z-40 flex bg-zinc-50 ${
              bottom
                ? "inset-x-0 bottom-0 flex-col border-t border-zinc-300 shadow-[0_-8px_30px_rgba(0,0,0,0.35)]"
                : "bottom-0 right-0 flex-row border-l border-zinc-300 shadow-[-8px_0_30px_rgba(0,0,0,0.35)]"
            } ${open ? "" : "hidden"}`}
            style={
              bottom ? { height } : { width, top: HEADER_PX }
            }
          >
            {/* Resize handle on the inner edge. */}
            <div
              onPointerDown={startDrag}
              title="Drag to resize"
              className={`group flex shrink-0 items-center justify-center ${
                bottom ? "h-3 cursor-ns-resize" : "w-3 cursor-ew-resize"
              }`}
            >
              <span
                className={`rounded-full bg-zinc-300 transition-colors group-hover:bg-teal-400 ${
                  bottom ? "h-1 w-12" : "h-12 w-1"
                }`}
              />
            </div>

            <div
              className={`flex h-full min-h-0 w-full min-w-0 flex-col ${
                bottom ? "mx-auto max-w-6xl px-6" : "pr-3"
              } pb-3`}
            >
              {/* Tab strip + dock controls. */}
              <div className="flex shrink-0 flex-wrap items-center gap-1 pb-1.5 pt-0.5">
                {sessions.map((s) => (
                  <span
                    key={s.id}
                    className={`flex items-center overflow-hidden rounded border text-xs ${
                      !split && active === s.id
                        ? "border-zinc-900 bg-zinc-900 text-white"
                        : "border-zinc-300 bg-white text-zinc-600"
                    }`}
                  >
                    <button
                      onClick={() => setActive(s.id)}
                      className="px-2.5 py-1"
                      title={
                        s.kind === "local"
                          ? "Shell on this machine"
                          : `${PANEL_TAG[s.kind].tag} · ${s.label}`
                      }
                    >
                      <span
                        className={`mr-1.5 font-mono text-[10px] ${PANEL_TAG[s.kind].tone}`}
                      >
                        {PANEL_TAG[s.kind].tag}
                      </span>
                      {s.label}
                    </button>
                    {(s.kind === "local" || s.kind === "instance") && (
                      <button
                        onClick={() => duplicate(s)}
                        title="Open another shell here (new tab)"
                        className="px-1 py-1 font-mono text-[10px] opacity-60 hover:opacity-100"
                      >
                        +
                      </button>
                    )}
                    <button
                      onClick={() => closeSession(s.id)}
                      title={
                        s.kind === "local" || s.kind === "instance"
                          ? "Close this shell (ends the session)"
                          : "Close this panel"
                      }
                      className="px-1.5 py-1 opacity-60 hover:opacity-100"
                    >
                      ×
                    </button>
                  </span>
                ))}
                <button
                  onClick={addLocal}
                  title="Open another Local Machine shell"
                  className="rounded border border-dashed border-zinc-300 px-2 py-1 font-mono text-[11px] text-zinc-500 hover:bg-zinc-100"
                >
                  + {">_"}
                </button>

                <span className="ml-auto flex items-center gap-1">
                  {sessions.length > 1 && (
                    <DockButton
                      onClick={() =>
                        setArrangement(split ? "tabs" : "split")
                      }
                      title={
                        split
                          ? "One shell at a time, behind tabs"
                          : bottom
                            ? "Show all shells side by side"
                            : "Show all shells stacked"
                      }
                      label={split ? "Tabs" : "Split"}
                    />
                  )}
                  <DockButton
                    onClick={() => setPosition(bottom ? "right" : "bottom")}
                    title={
                      bottom
                        ? "Snap the dock to the right side"
                        : "Snap the dock to the bottom"
                    }
                    label={bottom ? "Snap right" : "Snap bottom"}
                  />
                  <DockButton
                    onClick={() => setOpen(false)}
                    title="Hide the dock - every shell keeps running"
                    label="Hide"
                  />
                </span>
              </div>

              {/* Sessions. All stay mounted; visibility is CSS only. In
                  split, panes run across a bottom dock and down a right
                  dock, so each keeps a usable shape. */}
              <div
                className={`min-h-0 min-w-0 flex-1 gap-2 ${
                  split ? (bottom ? "flex flex-row" : "flex flex-col") : ""
                }`}
              >
                {sessions.map((s) => (
                  <div
                    key={s.id}
                    className={
                      split
                        ? "min-h-0 min-w-0 flex-1"
                        : `h-full min-h-0 ${active === s.id ? "" : "hidden"}`
                    }
                  >
                    <SessionBody session={s} />
                  </div>
                ))}
              </div>
            </div>
          </div>,
          document.body,
        )}
    </DockContext.Provider>
  );
}

// One dock tab's content, by kind. Terminals fill their pane exactly;
// chat/files/browse are ordinary flow components, so they get a scroll
// container - however tall the panel grows, the dock pane scrolls inside
// itself and never pushes the tab strip away.
function SessionBody({ session: s }: { session: Session }) {
  if (s.kind === "local" || s.kind === "instance") {
    return (
      <TerminalPanel
        fill
        instanceId={s.instanceId}
        wsPath={s.kind === "local" ? "/local/terminal" : undefined}
        model={s.model}
        // The dock tab id doubles as the backend session id, so a refresh
        // reattaches this panel to the same still-running shell.
        sessionId={s.id}
        resumed={s.resumed}
        label={
          s.model
            ? `Shell wired to ${s.model} (via the local proxy)`
            : s.kind === "local"
              ? "Shell on this machine (loopback-only)"
              : `${s.label} (SSH via the managed connection)`
        }
      />
    );
  }
  return (
    <div className="h-full overflow-y-auto rounded border border-zinc-200 bg-white px-3 pb-3">
      {s.kind === "chat" && <ChatPanel instanceId={s.instanceId!} />}
      {s.kind === "files" && <RecentFiles instanceId={s.instanceId!} />}
      {s.kind === "browse" && <FileNavigator instanceId={s.instanceId!} />}
    </div>
  );
}

function DockButton({
  onClick,
  title,
  label,
}: {
  onClick: () => void;
  title: string;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className="rounded border border-zinc-300 px-2 py-0.5 text-xs text-zinc-600 hover:bg-zinc-100"
    >
      {label}
    </button>
  );
}

// The header button: opens the dock with the local shell (or surfaces the
// local tab if the dock is already open on another shell).
export function TerminalDockToggle() {
  const { open, toggleLocal } = useTerminalDock();
  return (
    <button
      onClick={toggleLocal}
      aria-label={open ? "Hide terminal dock" : "Open local terminal"}
      title="Terminal on this machine"
      className={`flex h-8 w-8 items-center justify-center rounded border font-mono text-[11px] font-semibold transition-colors ${
        open
          ? "border-zinc-900 bg-zinc-900 text-white"
          : "border-zinc-200 text-zinc-500 hover:border-zinc-300 hover:text-zinc-800"
      }`}
    >
      {">_"}
    </button>
  );
}
