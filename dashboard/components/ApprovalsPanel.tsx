"use client";

import { useState } from "react";
import { api, ApiError, type Approval } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";

// Actions an approval-gated autopilot run is waiting on. Each card is one
// paused agent action: the run holds until you decide. Renders nothing when
// the queue is empty - it earns space only when a decision is needed.
//
// The countdown is load-bearing, not decoration. An approval nobody answers
// AUTO-DENIES, so "how long do I have" is the single most important fact on
// the card - and for a shutdown, letting it expire means the GPU keeps
// billing. (That is why Settings does not gate shutdowns by default.)
export function ApprovalsPanel() {
  const { data, refresh } = usePolling(() => api.approvals(), 2000);
  const [decideError, setDecideError] = useState("");
  const pending = data?.approvals ?? [];
  const timeout = data?.timeout_seconds ?? 600;
  if (pending.length === 0) return null;

  async function decide(id: string, approve: boolean) {
    setDecideError("");
    try {
      await api.decideApproval(id, approve);
    } catch (e) {
      // Only a 4xx means "already decided or expired" - benign, the refresh
      // clears the card. A network failure or a 500 used to be swallowed by
      // the same catch, so the click looked accepted while the action was
      // STILL PENDING and its countdown ran toward auto-deny.
      if (e instanceof ApiError && e.status >= 400 && e.status < 500) {
        /* the refresh below clears it */
      } else {
        setDecideError(
          `${approve ? "Approve" : "Deny"} did not reach the backend (${
            e instanceof Error ? e.message : String(e)
          }). The action is STILL PENDING - retry before the countdown expires.`,
        );
      }
    }
    refresh();
  }

  return (
    <section className="rounded-lg border border-amber-300 bg-amber-50 p-4">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-amber-800">
        Waiting for your approval ({pending.length})
      </h2>
      {decideError && (
        <p className="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1 text-xs font-medium text-red-800">
          {decideError}
        </p>
      )}
      <div className="mt-3 space-y-2">
        {pending.map((a: Approval) => (
          <div
            key={a.id}
            className="flex flex-wrap items-center justify-between gap-3 rounded border border-amber-200 bg-white p-3"
          >
            <div className="min-w-0">
              <p className="text-sm">
                <span className="font-mono font-medium">{a.action}</span>{" "}
                <span className="break-all font-mono text-xs text-zinc-500">
                  {JSON.stringify(a.args)}
                </span>
              </p>
              {a.run_goal && (
                <p className="mt-0.5 truncate text-xs text-zinc-400">
                  run goal: {a.run_goal}
                </p>
              )}
              <Countdown createdAt={a.created_at} timeout={timeout} />
            </div>
            <div className="flex shrink-0 gap-2">
              <button
                onClick={() => decide(a.id, true)}
                className="rounded bg-emerald-600 px-3 py-1 text-xs font-medium text-zinc-900 hover:bg-emerald-500"
              >
                Approve
              </button>
              <button
                onClick={() => decide(a.id, false)}
                className="rounded border border-red-300 px-3 py-1 text-xs font-medium text-red-700 hover:bg-red-50"
              >
                Deny
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function Countdown({
  createdAt,
  timeout,
}: {
  createdAt: string;
  timeout: number;
}) {
  const elapsed = (Date.now() - new Date(createdAt).getTime()) / 1000;
  const left = Math.max(0, timeout - elapsed);
  const minutes = Math.floor(left / 60);
  const seconds = Math.floor(left % 60);
  const urgent = left < 120;

  return (
    <p
      className={`mt-1 font-mono text-[11px] ${
        urgent ? "font-medium text-red-600" : "text-zinc-400"
      }`}
    >
      auto-denies in {minutes}m {String(seconds).padStart(2, "0")}s
    </p>
  );
}
