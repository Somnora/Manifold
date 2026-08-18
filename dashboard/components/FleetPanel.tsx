"use client";

import type { Instance } from "@/lib/api";
import { formatMoney } from "@/lib/format";

/**
 * The fleet at a glance: one row per running box.
 *
 * REPLACES MultiGpuTelemetry, which called itself "multi-node cluster
 * aggregation" and aggregated nothing. It was a tab switcher that showed ONE
 * instance at a time using the same TelemetryChart the instance card already
 * renders below — so it duplicated the card, labelled each box by launch id
 * while the card labelled it by name, and with a single instance produced one
 * small tile in a half-width panel stretched to a taller sibling's height.
 *
 * WHY THIS READS FROM SQLITE AND NOT THE LIVE STREAM. TelemetryChart's numbers
 * arrive over a WebSocket the backend feeds by opening and tearing down an SSH
 * port forward every two seconds. That is affordable for ONE focused chart the
 * user is looking at. For a fleet list it is not: eight rows would be ~240 SSH
 * channels a minute to draw eight progress bars, duplicating work the
 * dispatcher already does on its own 30s telemetry loop. So each row shows the
 * last recorded sample, and says how old it is rather than pretending it is
 * live.
 *
 * NOTHING HERE POLLS. Instances are passed down from the page, which already
 * polls /instances every 2s. A second poller on the hottest route in the app is
 * exactly the shape of the pile-up Phase 93 had to undo.
 */

// Older than this and the row stops implying the number is current. Generous
// against a 30s sampling interval, so ordinary jitter never reads as stale.
const STALE_AFTER_SECONDS = 150;

type StateStyle = { label: string; dot: string; text: string };

// The idle sweep's vocabulary, in the user's words. Deliberately mirrors the
// backend's own reasoning rather than collapsing it: "busy" and "protected"
// and "we cannot tell" are three different situations and the whole point of
// the activity field is that they stopped being one.
const STATES: Record<string, StateStyle> = {
  gpu_busy: { label: "working", dot: "bg-emerald-500", text: "text-emerald-700" },
  serving: { label: "serving", dot: "bg-emerald-500", text: "text-emerald-700" },
  batch_running: { label: "job running", dot: "bg-emerald-500", text: "text-emerald-700" },
  auto_managed: { label: "job-managed", dot: "bg-emerald-500", text: "text-emerald-700" },
  loading: { label: "loading", dot: "bg-sky-500", text: "text-sky-700" },
  booting: { label: "booting", dot: "bg-sky-500", text: "text-sky-700" },
  keep_alive: { label: "keep-alive", dot: "bg-sky-500", text: "text-sky-700" },
  idle_countdown: { label: "idle", dot: "bg-zinc-400", text: "text-zinc-500" },
  unreachable: { label: "unreachable", dot: "bg-amber-500", text: "text-amber-700" },
  unknown: { label: "not yet judged", dot: "bg-zinc-400", text: "text-zinc-500" },
};

function stateStyle(state: string | undefined): StateStyle {
  return STATES[state ?? "unknown"] ?? STATES.unknown;
}

function ageSeconds(iso: string): number | null {
  const then = Date.parse(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  if (Number.isNaN(then)) return null;
  return Math.max(0, (Date.now() - then) / 1000);
}

function gib(mib: number | null | undefined): string {
  return mib == null ? "—" : (mib / 1024).toFixed(1);
}

export function FleetPanel({ instances }: { instances: Instance[] }) {
  const fleet = instances.filter((i) => i.status === "active");
  const hourly = fleet.reduce((sum, i) => sum + (i.hourly_rate_usd || 0), 0);

  // Aggregate only over boxes we have a CURRENT reading for. Averaging in a
  // box whose telemetry is stale or missing would quietly drag the fleet
  // number toward zero and make a busy fleet look half-asleep — the same
  // absence-treated-as-a-zero mistake this whole area keeps having to unlearn.
  const measured = fleet.filter((i) => {
    const age = i.telemetry ? ageSeconds(i.telemetry.at) : null;
    return i.telemetry != null && age != null && age <= STALE_AFTER_SECONDS;
  });
  const usedMib = measured.reduce((s, i) => s + (i.telemetry?.vram_used_mib ?? 0), 0);
  const totalMib = measured.reduce((s, i) => s + (i.telemetry?.vram_total_mib ?? 0), 0);
  const meanUtil = measured.length
    ? Math.round(
        measured.reduce(
          (s, i) => s + (i.telemetry?.util_pct_mean ?? i.telemetry?.util_pct ?? 0),
          0,
        ) / measured.length,
      )
    : null;
  const gpus = measured.reduce((s, i) => s + (i.telemetry?.gpu_count ?? 1), 0);

  return (
    <div className="self-start rounded-xl border border-zinc-200 bg-white p-6 shadow-sm">
      <div className="flex items-baseline justify-between border-b border-zinc-200 pb-4">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-zinc-900">Fleet</h2>
          <p className="mt-1 text-xs text-zinc-500">
            Every running instance, and what its GPU is actually doing
          </p>
        </div>
        <span className="rounded-full border border-zinc-200 bg-zinc-100 px-3 py-1 text-xs font-mono tabular-nums text-zinc-600">
          {fleet.length} up · {formatMoney(hourly)}/hr
        </span>
      </div>

      {fleet.length === 0 ? (
        <p className="mt-4 rounded-xl border border-dashed border-zinc-300 bg-zinc-50 px-4 py-6 text-center text-xs text-zinc-500">
          Nothing running. Launch an instance and it appears here.
        </p>
      ) : (
        <ul className="mt-4 space-y-3">
          {fleet.map((inst) => {
            const style = stateStyle(inst.activity?.state);
            const age = inst.telemetry ? ageSeconds(inst.telemetry.at) : null;
            const stale = age != null && age > STALE_AFTER_SECONDS;
            const util = inst.telemetry?.util_pct ?? null;
            const vramPct =
              inst.telemetry?.vram_used_mib != null && inst.telemetry?.vram_total_mib
                ? (inst.telemetry.vram_used_mib / inst.telemetry.vram_total_mib) * 100
                : null;
            const minsLeft =
              inst.idle && inst.connection_state === "connected"
                ? Math.max(
                    0,
                    Math.ceil(
                      (inst.idle.timeout_seconds - inst.idle.idle_seconds) / 60,
                    ),
                  )
                : null;

            return (
              <li
                key={inst.id}
                className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium text-zinc-900">
                    {inst.name || inst.id}
                  </span>
                  <span
                    className={`flex flex-shrink-0 items-center gap-1.5 text-[11px] ${style.text}`}
                  >
                    <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
                    {style.label}
                  </span>
                </div>

                {/* Whose box, and what for. Unattributed says so rather than
                    leaving a reader to infer ownership from timing — which is
                    exactly how someone else's loading model got terminated. */}
                <p className="mt-0.5 truncate text-[11px] text-zinc-400">
                  {inst.purpose
                    ? inst.purpose
                    : inst.created_by
                      ? `${inst.created_by} · no stated purpose`
                      : "unattributed"}
                </p>

                {inst.telemetry == null ? (
                  <p className="mt-2 text-[11px] text-zinc-500">
                    No GPU reading yet. Not the same as idle.
                  </p>
                ) : (
                  <div className="mt-2 space-y-1.5">
                    <div className="flex items-center gap-2">
                      <span className="w-10 flex-shrink-0 text-[10px] uppercase tracking-wider text-zinc-500">
                        GPU
                      </span>
                      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-zinc-100">
                        <div
                          className={`h-1.5 rounded-full transition-all duration-300 ${
                            stale ? "bg-zinc-300" : "bg-sky-500"
                          }`}
                          style={{ width: `${Math.min(100, util ?? 0)}%` }}
                        />
                      </div>
                      <span className="w-9 flex-shrink-0 text-right font-mono text-[11px] tabular-nums text-sky-400">
                        {util == null ? "—" : `${util}%`}
                      </span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="w-10 flex-shrink-0 text-[10px] uppercase tracking-wider text-zinc-500">
                        VRAM
                      </span>
                      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-zinc-100">
                        <div
                          className={`h-1.5 rounded-full transition-all duration-300 ${
                            stale ? "bg-zinc-300" : "bg-indigo-500"
                          }`}
                          style={{ width: `${Math.min(100, vramPct ?? 0)}%` }}
                        />
                      </div>
                      <span className="w-16 flex-shrink-0 text-right font-mono text-[11px] tabular-nums text-indigo-400">
                        {gib(inst.telemetry.vram_used_mib)}/
                        {gib(inst.telemetry.vram_total_mib)}
                      </span>
                    </div>
                  </div>
                )}

                <div className="mt-1.5 flex items-center justify-between text-[11px] text-zinc-500">
                  <span>
                    {inst.idle?.keep_alive
                      ? "keep-alive on"
                      : minsLeft != null
                        ? `${minsLeft}m idle left`
                        : ""}
                  </span>
                  {/* A stale sample is labelled, never redrawn as current. */}
                  {stale && age != null && (
                    <span className="text-amber-700">
                      reading {Math.round(age / 60)}m old
                    </span>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {measured.length > 0 && (
        <div className="mt-4 flex items-baseline justify-between border-t border-zinc-200 pt-3 text-xs text-zinc-500">
          <span>
            {gpus} GPU{gpus === 1 ? "" : "s"}
            {measured.length < fleet.length && (
              <span className="text-zinc-400">
                {" "}
                · {fleet.length - measured.length} not measured
              </span>
            )}
          </span>
          <span className="font-mono tabular-nums">
            {meanUtil}% mean · {gib(usedMib)}/{gib(totalMib)} GiB
          </span>
        </div>
      )}
    </div>
  );
}
