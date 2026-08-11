"use client";

import { useMemo, useState } from "react";
import type { SpendBucket } from "@/lib/api";
import { formatMoney } from "@/lib/format";

// A spend-over-time chart, hand-rolled in SVG.
//
// The app has no charting library and this does not justify adding one: the
// bundle ships inside a Tauri desktop app, and everything else here (the
// telemetry sparklines, the progress bars) is drawn the same way. What this
// needs that Sparkline cannot do is a REAL domain: Sparkline hardcodes 0-100
// because it draws percentages, and money has no ceiling.
//
// Bars, not a line. A line implies a continuous quantity sampled over time;
// spend is a sum per bucket, and a bucket with no launches is a true zero,
// not a gap to interpolate through.

const HEIGHT = 160;
const PAD_LEFT = 52;   // room for a "$1,234" tick label
const PAD_BOTTOM = 22; // room for a date label
const PAD_TOP = 8;

/** A "nice" axis maximum: 1, 2, or 5 times a power of ten, at or above max. */
function niceCeiling(max: number): number {
  if (max <= 0) return 1;
  const pow = 10 ** Math.floor(Math.log10(max));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (pow * step >= max) return pow * step;
  }
  return pow * 10;
}

function axisLabel(usd: number): string {
  if (usd >= 1000) return `$${(usd / 1000).toFixed(usd >= 10000 ? 0 : 1)}k`;
  if (usd >= 1) return `$${usd.toFixed(0)}`;
  return `$${usd.toFixed(2)}`;
}

/** "2026-08-11" -> "Aug 11". Week and month buckets pass through mostly as-is. */
function bucketLabel(bucket: string): string {
  const day = /^\d{4}-\d{2}-\d{2}$/.exec(bucket);
  if (day) {
    const d = new Date(`${bucket}T00:00:00`);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  const month = /^(\d{4})-(\d{2})$/.exec(bucket);
  if (month) {
    const d = new Date(`${bucket}-01T00:00:00`);
    return d.toLocaleDateString(undefined, { month: "short", year: "2-digit" });
  }
  return bucket;
}

export function SpendChart({
  series,
  currency = "USD",
}: {
  series: SpendBucket[];
  currency?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);

  const { top, width, barWidth, gap } = useMemo(() => {
    const max = series.reduce((m, b) => Math.max(m, b.usd), 0);
    const n = Math.max(series.length, 1);
    // Fixed geometry, scaled by CSS: preserveAspectRatio="none" would skew
    // the text, so the viewBox width tracks the bucket count instead.
    const g = n > 45 ? 1 : n > 20 ? 2 : 4;
    const bw = Math.max(3, Math.min(28, Math.round(520 / n) - g));
    return {
      top: niceCeiling(max),
      width: PAD_LEFT + n * (bw + g) + 8,
      barWidth: bw,
      gap: g,
    };
  }, [series]);

  if (series.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500">
        No spend in this window.
      </div>
    );
  }

  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const y = (usd: number) => PAD_TOP + plotH - (usd / top) * plotH;
  const ticks = [0, top / 2, top];
  // Label roughly six buckets, whichever window is showing.
  const labelEvery = Math.max(1, Math.ceil(series.length / 6));
  const active = hover === null ? null : series[hover];

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-4">
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Spend over time
        </h3>
        <p className="font-mono text-xs tabular-nums text-zinc-500">
          {active
            ? `${bucketLabel(active.bucket)} · ${formatMoney(active.usd)} · ${active.launches} launch${active.launches === 1 ? "" : "es"}`
            : `${series.length} buckets · ${currency}`}
        </p>
      </div>

      <div className="overflow-x-auto">
        <svg
          viewBox={`0 0 ${width} ${HEIGHT}`}
          className="h-40 w-full min-w-[420px]"
          role="img"
          aria-label="Spend per bucket over the selected window"
          onMouseLeave={() => setHover(null)}
        >
          {ticks.map((t) => (
            <g key={t}>
              <line
                x1={PAD_LEFT}
                x2={width - 4}
                y1={y(t)}
                y2={y(t)}
                stroke="var(--color-zinc-200)"
                strokeWidth="1"
              />
              <text
                x={PAD_LEFT - 8}
                y={y(t) + 3}
                textAnchor="end"
                className="fill-zinc-500"
                style={{ fontSize: 9, fontVariantNumeric: "tabular-nums" }}
              >
                {axisLabel(t)}
              </text>
            </g>
          ))}

          {series.map((b, i) => {
            const x = PAD_LEFT + i * (barWidth + gap);
            const h = b.usd > 0 ? Math.max(1, plotH - (y(b.usd) - PAD_TOP)) : 0;
            const isHover = hover === i;
            return (
              <g key={b.bucket} onMouseEnter={() => setHover(i)}>
                {/* Full-height hit area: a $0.02 bar is 1px and unhoverable. */}
                <rect
                  x={x}
                  y={PAD_TOP}
                  width={barWidth + gap}
                  height={plotH}
                  fill="transparent"
                />
                {h > 0 && (
                  <rect
                    x={x}
                    y={y(b.usd)}
                    width={barWidth}
                    height={h}
                    rx={barWidth > 6 ? 2 : 0}
                    fill={
                      isHover ? "var(--color-amber-700)" : "var(--color-amber-300)"
                    }
                  />
                )}
                {i % labelEvery === 0 && (
                  <text
                    x={x + barWidth / 2}
                    y={HEIGHT - 6}
                    textAnchor="middle"
                    className="fill-zinc-500"
                    style={{ fontSize: 9 }}
                  >
                    {bucketLabel(b.bucket)}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}
