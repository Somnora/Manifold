"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiError, type SidecarDiagnosis } from "@/lib/api";
import { wsBase } from "@/lib/backend";
import { getToken } from "@/lib/token";

type GpuSample = {
  name: string;
  vram_used_mib: number;
  vram_total_mib: number;
  utilization_pct: number;
  temperature_c: number;
};

const HISTORY = 60;

export function TelemetryChart({ instanceId }: { instanceId: string }) {
  const [gpus, setGpus] = useState<GpuSample[]>([]);
  const [history, setHistory] = useState<Record<number, { util: number[]; vram: number[] }>>({});
  const [state, setState] = useState<"connecting" | "live" | "unavailable">("connecting");
  const wsRef = useRef<WebSocket | null>(null);
  const [diag, setDiag] = useState<SidecarDiagnosis | null>(null);
  const [diagBusy, setDiagBusy] = useState(false);
  const [diagErr, setDiagErr] = useState("");

  async function runDiagnose() {
    setDiagBusy(true);
    setDiagErr("");
    try {
      setDiag(await api.diagnoseSidecar(instanceId));
    } catch (e) {
      setDiagErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setDiagBusy(false);
    }
  }

  useEffect(() => {
    // A falsy id means "no instance to stream" (e.g. a node still booting).
    // Don't open a malformed socket; just sit in the connecting state.
    if (!instanceId) return;

    // Reset per-instance state so a previous instance's sparkline history and
    // gauges never bleed into the next one when this component is reused
    // across a tab switch.
    setGpus([]);
    setHistory({});
    setState("connecting");
    setDiag(null);
    setDiagErr("");

    let closed = false;
    // Browser WebSockets cannot set headers; the API token rides as ?token=.
    const token = getToken();
    const qs = token ? `?token=${encodeURIComponent(token)}` : "";
    const ws = new WebSocket(
      `${wsBase()}/instances/${instanceId}/metrics/stream${qs}`,
    );
    wsRef.current = ws;

    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      if (!payload.available || !payload.gpus?.length) {
        setState("unavailable");
        return;
      }

      const gpusData: GpuSample[] = payload.gpus;
      setGpus(gpusData);
      setState("live");

      setHistory((h) => {
        const next = { ...h };
        gpusData.forEach((gpu, i) => {
          if (!next[i]) next[i] = { util: [], vram: [] };
          next[i] = {
            util: [...next[i].util, gpu.utilization_pct].slice(-HISTORY),
            vram: [...next[i].vram, (gpu.vram_used_mib / gpu.vram_total_mib) * 100].slice(-HISTORY)
          };
        });
        return next;
      });
    };
    ws.onerror = () => { if (!closed) setState("unavailable"); };
    ws.onclose = () => { if (!closed) setState("unavailable"); };
    return () => { closed = true; ws.close(); };
  }, [instanceId]);

  if (state === "unavailable") {
    return (
      <div className="mt-4 text-xs text-zinc-500">
        <div className="flex items-center gap-2">
          <span>Telemetry unavailable (sidecar not reachable).</span>
          <button
            onClick={runDiagnose}
            disabled={diagBusy}
            className="rounded border border-zinc-300 px-2 py-0.5 text-zinc-700 hover:bg-zinc-100 disabled:opacity-50"
          >
            {diagBusy ? "Diagnosing..." : "Diagnose"}
          </button>
        </div>
        {diagErr && <p className="mt-2 text-red-700">{diagErr}</p>}
        {diag && (
          <div className="mt-2 rounded border border-zinc-200 bg-zinc-50 p-3 text-zinc-600">
            <p className="font-medium text-zinc-900">{diag.summary}</p>
            <p className="mt-1 font-mono text-[11px] text-zinc-500">cause: {diag.cause}</p>
            <div className="mt-2 space-y-2">
              {diag.checks.map((c) => (
                <div key={c.label}>
                  <p className="font-medium text-zinc-500">{c.label}</p>
                  <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap rounded bg-zinc-950 border border-zinc-200 p-2 text-[11px] leading-relaxed text-zinc-600">
                    {c.output || "(no output)"}
                  </pre>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    );
  }

  if (state === "connecting" && gpus.length === 0) {
    return <div className="mt-4 text-xs text-zinc-500 font-mono animate-pulse">Connecting to telemetry stream...</div>;
  }

  return (
    // auto-fit, not a fixed column count: a hardcoded xl:grid-cols-2 kept a
    // second track reserved at full width, so a single-GPU box rendered one
    // tile with a dead right half (reported from a full-screen window,
    // 2026-08-17). auto-fit collapses empty tracks: one GPU spans the full
    // card, an 8x box tiles 3-4 per row, and the sparklines gain real
    // horizontal resolution. min(400px,100%) keeps narrow phones to one
    // column instead of overflowing.
    <div className="mt-4 grid gap-4 grid-cols-[repeat(auto-fit,minmax(min(400px,100%),1fr))]">
      {gpus.map((gpu, i) => {
        const hist = history[i] || { util: [], vram: [] };
        const vramPct = (gpu.vram_used_mib / gpu.vram_total_mib) * 100;

        return (
          <div key={i} className="rounded-xl border border-zinc-200 bg-zinc-50 p-4 relative overflow-hidden shadow-sm">
            <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-indigo-500/30 to-sky-500/30" />
            <div className="flex items-center justify-between mb-4">
              <h4 className="text-sm font-semibold text-zinc-900 flex items-center gap-2">
                <div className="h-2 w-2 rounded-full bg-emerald-500 animate-pulse" />
                GPU {i}: {gpu.name}
              </h4>
              <span className="font-mono text-xs text-zinc-500 flex items-center gap-1.5">
                <svg className="w-3 h-3 text-red-700" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17.657 18.657A8 8 0 016.343 7.343S7 9 9 10c0-2 .5-5 2.986-7C14 5 16.09 5.777 17.656 7.343A7.975 7.975 0 0120 13a7.975 7.975 0 01-2.343 5.657z" />
                </svg>
                {gpu.temperature_c}°C
              </span>
            </div>

            <div className="grid grid-cols-2 gap-4 mb-4">
              <div className="space-y-1">
                <div className="flex justify-between text-[10px] uppercase tracking-wider text-zinc-500">
                  <span>VRAM Usage</span>
                  <span className="font-mono text-indigo-400">{vramPct.toFixed(1)}%</span>
                </div>
                <div className="w-full bg-zinc-100 rounded-full h-1.5 overflow-hidden">
                  <div className="bg-indigo-500 h-1.5 rounded-full transition-all duration-300" style={{ width: `${vramPct}%` }} />
                </div>
                <div className="text-[10px] font-mono text-zinc-500 text-right">
                  {(gpu.vram_used_mib / 1024).toFixed(1)} / {(gpu.vram_total_mib / 1024).toFixed(1)} GiB
                </div>
              </div>

              <div className="space-y-1">
                <div className="flex justify-between text-[10px] uppercase tracking-wider text-zinc-500">
                  <span>Compute</span>
                  <span className="font-mono text-sky-400">{gpu.utilization_pct.toFixed(1)}%</span>
                </div>
                <div className="w-full bg-zinc-100 rounded-full h-1.5 overflow-hidden">
                  <div className="bg-sky-500 h-1.5 rounded-full transition-all duration-300" style={{ width: `${gpu.utilization_pct}%` }} />
                </div>
                <div className="text-[10px] font-mono text-zinc-500 text-right">
                  Utilization
                </div>
              </div>
            </div>

            <div className="flex gap-3 h-12">
              <Sparkline label="VRAM %" values={hist.vram} color="var(--color-indigo-400)" />
              <Sparkline label="Util %" values={hist.util} color="var(--color-sky-400)" />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Sparkline({ label, values, color }: { label: string; values: number[]; color: string; }) {
  const w = 220;
  const h = 40;
  const points = values.map((v, i) => {
    const x = (i / Math.max(values.length - 1, 1)) * w;
    const y = h - (Math.min(v, 100) / 100) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");

  return (
    <div className="flex-1 relative group">
      <svg viewBox={`0 0 ${w} ${h}`} className="h-full w-full" preserveAspectRatio="none" role="img" aria-label={`${label} history`}>
        <line x1="0" y1={h} x2={w} y2={h} stroke="var(--color-zinc-200)" strokeWidth="1" />
        {values.length > 1 && (
          <polyline points={points} fill="none" stroke={color} strokeWidth="1.5" className="drop-shadow-md" />
        )}
      </svg>
      <div className="absolute inset-0 bg-gradient-to-t from-zinc-950/50 to-transparent pointer-events-none" />
      <span className="absolute bottom-0 left-0 text-[9px] uppercase tracking-widest text-zinc-500 font-mono">
        {label}
      </span>
    </div>
  );
}
