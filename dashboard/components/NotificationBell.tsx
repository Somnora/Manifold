"use client";

import { useEffect, useRef, useState } from "react";
import { api, type Notification, type NotificationKind } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { formatDate } from "@/lib/format";

// The OS-level ping is raised by the BACKEND (macOS Notification Center,
// notify-send on Linux) so it reaches you with the window in the background,
// which is the whole point. This bell is the in-app record of the same
// events: what happened, when, and what is still unread.
const TONE: Record<NotificationKind, string> = {
  approval_requested: "text-amber-400",
  job_succeeded: "text-emerald-400",
  job_failed: "text-red-400",
  run_finished: "text-sky-400",
  data_transferred: "text-teal-400",
  capacity_available: "text-emerald-400",
  instance_idle: "text-amber-400",
  instance_ceiling: "text-orange-400",
  budget_threshold: "text-amber-400",
  // A shell was ended with whatever was inside it. Amber, not red: it is
  // the grace period doing its job, but it is never good news.
  terminal_reaped: "text-amber-400",
  // The setup script failed. Red, but the box is still there: nothing was
  // destroyed over it.
  bootstrap_failed: "text-red-400",
};

const LABEL: Record<NotificationKind, string> = {
  approval_requested: "approval",
  job_succeeded: "job",
  job_failed: "job",
  run_finished: "autopilot",
  data_transferred: "data",
  capacity_available: "capacity",
  instance_idle: "idle spend",
  instance_ceiling: "max lifetime",
  budget_threshold: "budget",
  terminal_reaped: "terminal",
  bootstrap_failed: "bootstrap",
};

export function NotificationBell() {
  const [open, setOpen] = useState(false);
  const panel = useRef<HTMLDivElement>(null);
  const { data, refresh } = usePolling(() => api.notifications(), 4000);
  const items: Notification[] = data?.notifications ?? [];
  const unread = data?.unread ?? 0;

  // Click-away and Escape close it, like every other menu on the planet.
  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (!panel.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  async function markAllRead() {
    await api.markNotificationsRead();
    refresh();
  }

  async function clear() {
    await api.clearNotifications();
    refresh();
  }

  return (
    <div className="relative" ref={panel}>
      <button
        onClick={() => setOpen((s) => !s)}
        aria-label={`Notifications${unread ? ` (${unread} unread)` : ""}`}
        className="relative flex h-8 w-8 items-center justify-center rounded border border-zinc-200 text-zinc-500 transition-colors hover:border-zinc-300 hover:text-zinc-800"
      >
        <svg
          viewBox="0 0 16 16"
          className="h-4 w-4"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.3"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M8 1.8a3.7 3.7 0 0 0-3.7 3.7c0 3-1.1 4-1.6 4.5h10.6c-.5-.5-1.6-1.5-1.6-4.5A3.7 3.7 0 0 0 8 1.8Z" />
          <path d="M6.6 12.4a1.5 1.5 0 0 0 2.8 0" />
        </svg>
        {unread > 0 && (
          <span className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-teal-400 px-1 font-mono text-[10px] font-semibold leading-none text-zinc-950">
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 z-50 mt-2 w-[22rem] overflow-hidden rounded-lg border border-zinc-200 bg-white shadow-xl shadow-black/40">
          <div className="flex items-center justify-between border-b border-zinc-200 px-3 py-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
              Notifications
            </span>
            <div className="flex gap-2 text-xs">
              {unread > 0 && (
                <button
                  onClick={markAllRead}
                  className="text-zinc-500 hover:text-zinc-800"
                >
                  Mark read
                </button>
              )}
              {items.length > 0 && (
                <button
                  onClick={clear}
                  className="text-zinc-400 hover:text-zinc-700"
                >
                  Clear
                </button>
              )}
            </div>
          </div>

          <div className="max-h-96 overflow-y-auto">
            {items.map((n) => (
              <div
                key={n.id}
                className={`border-b border-zinc-100 px-3 py-2.5 last:border-0 ${
                  n.read ? "" : "bg-zinc-50"
                }`}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <p className="flex items-baseline gap-1.5 text-sm">
                    <span
                      className={`font-mono text-[10px] uppercase ${TONE[n.kind]}`}
                    >
                      {LABEL[n.kind]}
                    </span>
                    <span className="font-medium text-zinc-800">{n.title}</span>
                  </p>
                  <span className="shrink-0 font-mono text-[10px] text-zinc-400">
                    {formatDate(n.at)}
                  </span>
                </div>
                {n.body && (
                  <p className="mt-0.5 whitespace-pre-wrap break-words text-xs text-zinc-500">
                    {n.body}
                  </p>
                )}
              </div>
            ))}
            {items.length === 0 && (
              <p className="px-3 py-8 text-center text-xs text-zinc-400">
                Nothing yet. Approvals, finished jobs, and rescued files land
                here.
              </p>
            )}
          </div>

          <p className="border-t border-zinc-200 px-3 py-2 text-[11px] text-zinc-400">
            Choose what pings you in{" "}
            <a href="/settings" className="text-teal-400 hover:underline">
              Settings
            </a>
            .
          </p>
        </div>
      )}
    </div>
  );
}
