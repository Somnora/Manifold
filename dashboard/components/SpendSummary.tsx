"use client";

import Link from "next/link";
import { api, type SpendSummary } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { formatMoney } from "@/lib/format";

// Every spend figure in the dashboard comes through here, and every one of
// them comes from the backend. The dashboard used to multiply a rate by
// "now minus launched_at", which meant a launch that failed AFTER its
// instance existed kept growing a cost that was never charged. The backend
// answers with a six-state model instead, so the pieces it cannot price
// arrive as counts and ranges and get rendered as counts and ranges.
//
// Poll slowly on purpose: totals move by cents per minute, and BurnChip
// already carries the live per-hour number at 10s.
const SPEND_POLL_MS = 30_000;

export function useSpendSummary() {
  // getTimezoneOffset() is minutes WEST of UTC; the backend wants EAST.
  return usePolling(
    () => api.spendSummary(-new Date().getTimezoneOffset()),
    SPEND_POLL_MS,
  );
}

// Mock mode has to travel with the numbers, not just sit in a banner
// somewhere above them: a screenshot of dollar figures with no marker is
// actively misleading.
export function DemoMarker() {
  return (
    <span
      title="Mock mode: demo figures from fixture data, not real spend."
      className="rounded border border-zinc-300 bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-zinc-600"
    >
      demo data
    </span>
  );
}

// The dangerous state: the cloud is listing an instance that this launch
// record calls failed. It is billing right now and no idle timer owns it.
export function OrphanedSpendAlert({
  summary,
  linkToInstances = true,
}: {
  summary: SpendSummary;
  linkToInstances?: boolean;
}) {
  const n = summary.orphaned.count;
  if (n === 0) return null;
  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
      <span className="font-medium">
        Still billing: {n} instance{n === 1 ? "" : "s"}
      </span>{" "}
      {n === 1 ? "is" : "are"} running on the cloud behind a launch record
      that says it failed. Nothing in Manifold will auto-terminate{" "}
      {n === 1 ? "it" : "them"}.{" "}
      {linkToInstances ? (
        <Link href="/" className="font-medium underline">
          Open the instances page
        </Link>
      ) : (
        <span className="font-medium">
          Look for {n === 1 ? "it" : "them"} under Running instances below
        </span>
      )}
      .
    </div>
  );
}

// Compact form for a page header: the all-time total, its disclaimer on
// hover, and anything the total deliberately excludes right underneath it.
export function SpendTotalLink({ summary }: { summary: SpendSummary }) {
  const { unresolved } = summary;
  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-2">
        <Link
          href="/history"
          title={summary.disclaimer}
          className="text-zinc-500 hover:text-zinc-900"
        >
          Total spend:{" "}
          <span className="font-medium tabular-nums text-zinc-900 underline decoration-zinc-500 underline-offset-2">
            {formatMoney(summary.all_time_usd)}
          </span>
        </Link>
        {summary.mock && <DemoMarker />}
      </div>
      {unresolved.count > 0 && (
        <span
          className="text-xs text-amber-700"
          title="These instances stopped at a time Manifold never observed, so their cost is a range, not a number. Folding a range into a total would make the total a guess."
        >
          plus {unresolved.count} launch{unresolved.count === 1 ? "" : "es"} in
          an unknown state ({formatMoney(unresolved.usd_low)} to{" "}
          {formatMoney(unresolved.usd_high)})
        </span>
      )}
      {summary.lower_bound && (
        <span className="text-[11px] text-zinc-400">
          Covers launches Manifold started.
        </span>
      )}
    </div>
  );
}

// Full form for the Activity page: the four windows, everything the totals
// leave out, and the disclaimer as visible text rather than a tooltip.
export function SpendSummaryPanel() {
  const { data, error, stale, lastSuccess } = useSpendSummary();

  return (
    <div className="space-y-3">
      {error && (
        <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
          {stale && lastSuccess && (
            <span className="mt-1 block font-medium">
              The totals below are a snapshot from{" "}
              {lastSuccess.toLocaleTimeString()}, not live.
            </span>
          )}
        </p>
      )}

      {!data && !error && (
        <p className="rounded-lg border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500">
          Loading spend...
        </p>
      )}

      {data && (
        <>
          {data.mock && (
            <div className="rounded-lg border border-zinc-300 bg-zinc-100 px-4 py-3 text-xs text-zinc-600">
              Mock mode: every figure below is demo data from the fixture
              catalog, not real spend. Real numbers need the backend started
              without MANIFOLD_MOCK=1.
            </div>
          )}

          <OrphanedSpendAlert summary={data} />

          <div className="rounded-lg border border-zinc-200 bg-white p-4">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
                Spend
              </h2>
              {data.mock && <DemoMarker />}
            </div>

            <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 md:grid-cols-4">
              <Window label="Today" usd={data.today_usd} />
              <Window label="Last 7 days" usd={data.week_usd} />
              <Window label="Month to date" usd={data.month_to_date_usd} />
              <Window label="All time" usd={data.all_time_usd} />
            </dl>

            <p className="mt-3 text-xs text-zinc-500">
              Windows are local to {data.timezone_label}. Burning right now:{" "}
              <span className="font-medium tabular-nums text-zinc-700">
                {formatMoney(data.live_burn_usd_per_hour)}/hr
              </span>
              .
            </p>

            {(data.unresolved.count > 0 || data.rate_unknown_count > 0) && (
              <div className="mt-3 space-y-1 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                {data.unresolved.count > 0 && (
                  <p>
                    <span className="font-medium">
                      Plus {data.unresolved.count} launch
                      {data.unresolved.count === 1 ? "" : "es"} in an unknown
                      state:
                    </span>{" "}
                    <span className="tabular-nums">
                      {formatMoney(data.unresolved.usd_low)} to{" "}
                      {formatMoney(data.unresolved.usd_high)}
                    </span>
                    . Manifold never saw{" "}
                    {data.unresolved.count === 1 ? "it" : "them"} stop, so the
                    cost is a range. It is kept out of the totals rather than
                    guessed at.
                  </p>
                )}
                {data.rate_unknown_count > 0 && (
                  <p>
                    <span className="font-medium">
                      {data.rate_unknown_count} launch
                      {data.rate_unknown_count === 1 ? "" : "es"} ran at a
                      price Manifold never recorded.
                    </span>{" "}
                    Their runtime is known, their cost is not, and it is not
                    counted as zero.
                  </p>
                )}
              </div>
            )}

            {data.lower_bound && (
              <p className="mt-3 text-xs text-zinc-500">
                These totals are a lower bound: they cover launches Manifold
                started. Instances launched elsewhere (the Lambda console, the
                API) have no launch record here and are not counted.
              </p>
            )}

            <p className="mt-2 text-[11px] text-zinc-400">{data.disclaimer}</p>
          </div>
        </>
      )}
    </div>
  );
}

function Window({ label, usd }: { label: string; usd: number }) {
  return (
    <div>
      <dt className="text-xs text-zinc-400">{label}</dt>
      <dd className="mt-0.5 text-lg font-medium tabular-nums text-zinc-900">
        {formatMoney(usd)}
      </dd>
    </div>
  );
}
