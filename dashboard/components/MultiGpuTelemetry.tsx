"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { TelemetryChart } from "./TelemetryChart";

export function MultiGpuTelemetry() {
  const { data: launches } = usePolling(api.launches, 3000);
  const activeLaunches = (launches || []).filter((l) => l.status === "active");
  // Telemetry streams from the REAL cloud instance id, which a launch only
  // carries once its node has booted. A launch that is "active" but still
  // resolving its instance id has nothing to stream yet — keep it out of the
  // selectable tabs and surface it as a pending count instead.
  const streamable = activeLaunches.filter((l) => l.lambda_instance_id);
  const pending = activeLaunches.length - streamable.length;

  // activeTab holds a launch.id (the stable record key); the chart is fed the
  // launch's lambda_instance_id.
  const [activeTab, setActiveTab] = useState<string | null>(null);
  const selectedLaunch =
    streamable.find((l) => l.id === activeTab) ?? streamable[0] ?? null;
  const selectedInstanceId = selectedLaunch?.lambda_instance_id ?? null;

  return (
    <div className="relative overflow-hidden rounded-xl border border-zinc-200 bg-white p-6 shadow-sm">
      <div className="absolute -top-24 -right-24 w-48 h-48 bg-emerald-500/10 rounded-full blur-3xl pointer-events-none" />
      <div className="absolute -bottom-24 -left-24 w-48 h-48 bg-sky-500/10 rounded-full blur-3xl pointer-events-none" />

      <div className="relative z-10 flex items-center justify-between border-b border-zinc-200 pb-5 mb-5">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-zinc-900 flex items-center gap-3">
            <span className="relative flex h-3 w-3">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex h-3 w-3 rounded-full bg-emerald-500" />
            </span>
            Live Cluster Telemetry
          </h2>
          <p className="text-xs text-zinc-500 mt-1">
            Real-time multi-node cluster aggregation & GPU monitoring
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className="rounded-full bg-zinc-100 border border-zinc-200 px-3 py-1.5 text-xs text-zinc-600 font-mono">
            {streamable.length} Streaming
            {pending > 0 && <span className="text-zinc-400"> · {pending} booting</span>}
          </span>
        </div>
      </div>

      {activeLaunches.length === 0 ? (
        <div className="relative z-10 rounded-xl border border-dashed border-zinc-300 bg-zinc-50 p-10 text-center text-sm text-zinc-500">
          <div className="mx-auto w-12 h-12 rounded-full bg-zinc-100 flex items-center justify-center mb-3">
            <svg className="w-6 h-6 text-zinc-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M5 12h14M12 5l7 7-7 7" />
            </svg>
          </div>
          No active GPU nodes streaming telemetry. Launch an instance to view live metrics.
        </div>
      ) : streamable.length === 0 ? (
        <div className="relative z-10 rounded-xl border border-dashed border-zinc-300 bg-zinc-50 p-10 text-center text-sm text-zinc-500">
          <div className="mx-auto w-12 h-12 rounded-full bg-zinc-100 flex items-center justify-center mb-3">
            <svg className="w-6 h-6 text-zinc-400 animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 3v3m0 12v3m9-9h-3M6 12H3m14.5-5.5l-2 2m-7 7l-2 2m11 0l-2-2m-7-7l-2-2" />
            </svg>
          </div>
          {activeLaunches.length} instance{activeLaunches.length !== 1 && "s"} booting;
          telemetry starts once the node comes online.
        </div>
      ) : (
        <div className="relative z-10 space-y-4">
          <div className="flex overflow-x-auto gap-2 pb-2 scrollbar-hide">
            {streamable.map((launch) => (
              <button
                key={launch.id}
                onClick={() => setActiveTab(launch.id)}
                className={`flex-shrink-0 px-4 py-2 rounded-lg border text-sm font-medium transition-all ${
                  selectedLaunch?.id === launch.id
                    ? "bg-zinc-100 border-zinc-300 text-zinc-900"
                    : "bg-white border-zinc-200 text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900"
                }`}
              >
                <div className="flex items-center gap-2">
                  <div className={`h-1.5 w-1.5 rounded-full ${selectedLaunch?.id === launch.id ? "bg-emerald-400" : "bg-zinc-400"}`} />
                  {launch.id.slice(0, 8)}...
                  <span className="ml-1 text-xs font-mono opacity-60">
                    ({launch.requested_type})
                  </span>
                </div>
              </button>
            ))}
          </div>

          <div className="pt-2">
            {selectedInstanceId && <TelemetryChart instanceId={selectedInstanceId} />}
          </div>
        </div>
      )}
    </div>
  );
}
