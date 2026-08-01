"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { TelemetryChart } from "./TelemetryChart";

export function MultiGpuTelemetry() {
  const { data: launches } = usePolling(api.launches, 3000);
  const activeLaunches = (launches || []).filter(l => l.status === "active");
  const [activeTab, setActiveTab] = useState<string | null>(null);

  // If no tab is selected but we have active launches, select the first one
  const selectedLaunchId = activeTab || (activeLaunches.length > 0 ? activeLaunches[0].id : null);

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-950 p-6 shadow-2xl text-zinc-100 relative overflow-hidden">
      <div className="absolute -top-24 -right-24 w-48 h-48 bg-purple-600/10 rounded-full blur-3xl pointer-events-none" />
      <div className="absolute -bottom-24 -left-24 w-48 h-48 bg-indigo-600/10 rounded-full blur-3xl pointer-events-none" />
      
      <div className="relative z-10 flex items-center justify-between border-b border-zinc-800/80 pb-5 mb-5">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-3">
            <div className="relative">
              <div className="h-3 w-3 rounded-full bg-purple-500 animate-pulse" />
              <div className="absolute inset-0 bg-purple-500 rounded-full blur animate-pulse opacity-50" />
            </div>
            Live Cluster Telemetry
          </h2>
          <p className="text-sm text-zinc-400 mt-1 font-medium">
            Real-time multi-node cluster aggregation & GPU monitoring
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className="rounded-full bg-zinc-900 border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 font-mono shadow-inner">
            {activeLaunches.length} Node{activeLaunches.length !== 1 && 's'} Online
          </span>
        </div>
      </div>

      {activeLaunches.length === 0 ? (
        <div className="relative z-10 rounded-xl border border-dashed border-zinc-800 bg-zinc-900/20 p-10 text-center text-sm text-zinc-500">
          <div className="mx-auto w-12 h-12 rounded-full bg-zinc-900 flex items-center justify-center mb-3">
            <svg className="w-6 h-6 text-zinc-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M5 12h14M12 5l7 7-7 7" />
            </svg>
          </div>
          No active GPU nodes streaming telemetry. Launch an instance to view live metrics.
        </div>
      ) : (
        <div className="relative z-10 space-y-4">
          <div className="flex overflow-x-auto gap-2 pb-2 scrollbar-hide">
            {activeLaunches.map((launch) => (
              <button
                key={launch.id}
                onClick={() => setActiveTab(launch.id)}
                className={`flex-shrink-0 px-4 py-2 rounded-lg border text-sm font-medium transition-all ${
                  selectedLaunchId === launch.id
                    ? "bg-zinc-800 border-zinc-600 text-white shadow-md"
                    : "bg-zinc-900/50 border-zinc-800/50 text-zinc-400 hover:bg-zinc-800/80 hover:text-zinc-200"
                }`}
              >
                <div className="flex items-center gap-2">
                  <div className={`h-1.5 w-1.5 rounded-full ${selectedLaunchId === launch.id ? "bg-green-400" : "bg-zinc-600"}`} />
                  {launch.id.slice(0, 8)}...
                  <span className="ml-1 text-xs font-mono opacity-60">
                    ({launch.requested_type})
                  </span>
                </div>
              </button>
            ))}
          </div>

          <div className="pt-2">
            {selectedLaunchId && <TelemetryChart instanceId={selectedLaunchId} />}
          </div>
        </div>
      )}
    </div>
  );
}
