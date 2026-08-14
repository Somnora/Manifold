"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type GpuGuide, type GpuRung } from "@/lib/api";
import { formatMoney } from "@/lib/format";

// Phase 86: the hardware ladder, from one A10 to an 8x B200 node, inside
// the launch form where the decision actually happens. Words are curated in
// the backend; every number on screen is the provider's own price or
// labelled arithmetic on it - this component adds no numbers of its own,
// because a hardcoded price on a spend screen is how this repo's worst UI
// bug happened.
export function HardwareGuide({
  onPick,
  current,
}: {
  // Selecting a rung IS choosing a GPU: the guide fills the form field so
  // learning and acting are the same motion.
  onPick: (instanceType: string) => void;
  current: string;
}) {
  const [open, setOpen] = useState(false);
  const [guide, setGuide] = useState<GpuGuide | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open || guide) return;
    api
      .gpuGuide()
      .then(setGuide)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [open, guide]);

  const fitsLine = (r: GpuRung) =>
    r.fits
      ? `serves ~${r.fits.serve_fp16_b}B fp16 / ~${r.fits.serve_4bit_b}B ` +
        `4-bit · LoRA ~${r.fits.lora_bf16_b}B / QLoRA ~${r.fits.qlora_4bit_b}B`
      : null;

  return (
    <div className="mt-1">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-[11px] text-zinc-500 underline decoration-dotted underline-offset-2 hover:text-zinc-800"
      >
        {open ? "Hide the hardware guide" : "Which GPU do I need?"}
      </button>

      {open && error && (
        <p className="mt-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">
          {error}
        </p>
      )}

      {open && guide && (
        <div className="mt-2 space-y-2">
          {guide.rungs.map((r) => (
            <div
              key={r.instance_type}
              className={`rounded border px-3 py-2 ${
                current === r.instance_type
                  ? "border-emerald-400 bg-emerald-50/40"
                  : "border-zinc-200 bg-white"
              }`}
            >
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs">
                <span className="font-semibold text-zinc-800">
                  {r.gpu_count > 1 ? `${r.gpu_count}x ` : ""}
                  {r.family}
                </span>
                {r.era && <span className="text-zinc-400">{r.era}</span>}
                {r.vram_total_gib && (
                  <span className="font-mono text-zinc-600">
                    {r.vram_total_gib} GB
                  </span>
                )}
                <span className="ml-auto font-mono text-zinc-800">
                  {formatMoney(r.price_usd_per_hour)}/hr
                </span>
                {r.price_per_gib_hour && (
                  <span className="font-mono text-[10px] text-zinc-400">
                    {formatMoney(r.price_per_gib_hour)}/GB·hr
                  </span>
                )}
                {r.available_now ? (
                  <button
                    type="button"
                    onClick={() => onPick(r.instance_type)}
                    className="shrink-0 rounded border border-zinc-300 px-1.5 py-0.5 text-[10px] text-zinc-700 hover:bg-zinc-50"
                  >
                    Use it
                  </button>
                ) : (
                  <span className="shrink-0 rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] text-zinc-400">
                    out of capacity
                  </span>
                )}
              </div>
              <p className="mt-1 text-[11px] leading-relaxed text-zinc-600">
                {r.good_for}
              </p>
              {fitsLine(r) && (
                <p className="mt-0.5 font-mono text-[10px] text-zinc-400">
                  {fitsLine(r)}
                </p>
              )}
              {r.step_up_when && (
                <p className="mt-0.5 text-[11px] text-zinc-500">
                  <span className="text-zinc-400">Step up when</span>{" "}
                  {r.step_up_when}
                </p>
              )}
              {r.note && (
                <p className="mt-0.5 text-[11px] text-amber-700">{r.note}</p>
              )}
            </div>
          ))}
          <p className="text-[10px] leading-relaxed text-zinc-400">
            {guide.fits_basis} Prices are the provider&apos;s, live. What a
            run really cost lands in Activity afterwards.
          </p>
        </div>
      )}
    </div>
  );
}
