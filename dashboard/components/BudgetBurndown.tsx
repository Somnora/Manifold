"use client";

import type { BudgetStatus } from "@/lib/api";
import { formatMoney } from "@/lib/format";

// The monthly wallet, drawn as a burn-down.
//
// Two honesty rules run through this component. First, the budget NEVER
// blocks a launch, so the copy must not imply it will - a user who reads
// "budget" as "hard stop" and leaves a box running overnight has been
// misled by us. Second, month-to-date only counts launches Manifold
// started, so the figure is a floor and says so.

function tone(state: BudgetStatus["state"]) {
  if (state === "over") {
    return {
      bar: "bg-red-700",
      track: "bg-red-100",
      text: "text-red-800",
      border: "border-red-200",
    };
  }
  if (state === "warn") {
    return {
      bar: "bg-amber-700",
      track: "bg-amber-100",
      text: "text-amber-800",
      border: "border-amber-200",
    };
  }
  return {
    bar: "bg-emerald-700",
    track: "bg-emerald-100",
    text: "text-emerald-800",
    border: "border-zinc-200",
  };
}

export function BudgetBurndown({ budget }: { budget: BudgetStatus }) {
  if (budget.state === "unset") {
    return (
      <div className="rounded-lg border border-dashed border-zinc-300 px-4 py-3 text-xs text-zinc-500">
        No monthly budget set. Add one under{" "}
        <a href="/settings" className="underline hover:text-zinc-900">
          Settings
        </a>{" "}
        to see a burn-down here. It reports only, and never blocks a launch.
      </div>
    );
  }

  const t = tone(budget.state);
  const pct = Math.min(100, Math.max(0, budget.used_pct ?? 0));
  const over = budget.state === "over";

  return (
    <div className={`rounded-lg border ${t.border} bg-white p-4`}>
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Monthly budget
        </h3>
        <p className={`font-mono text-xs tabular-nums ${t.text}`}>
          {(budget.used_pct ?? 0).toFixed(0)}% used
        </p>
      </div>

      <p className="mt-2 text-2xl font-medium tabular-nums text-zinc-900">
        {formatMoney(budget.month_to_date_usd)}
        <span className="text-sm font-normal text-zinc-500">
          {" "}
          of {formatMoney(budget.monthly_budget_usd)}
        </span>
      </p>

      <div className={`mt-3 h-2 w-full overflow-hidden rounded-full ${t.track}`}>
        <div
          className={`h-2 rounded-full ${t.bar} transition-all duration-300`}
          style={{ width: `${pct}%` }}
        />
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
        <dt className="text-zinc-500">
          {over ? "Over by" : "Left this month"}
        </dt>
        <dd className={`text-right tabular-nums ${over ? t.text : "text-zinc-900"}`}>
          {formatMoney(Math.abs(budget.remaining_usd ?? 0))}
        </dd>

        <dt className="text-zinc-500">At the current burn</dt>
        <dd className="text-right tabular-nums text-zinc-900">
          {formatMoney(budget.projected_month_end_usd ?? 0)} by month end
        </dd>

        {budget.exhausted_on && (
          <>
            <dt className="text-zinc-500">Runs out</dt>
            <dd className={`text-right tabular-nums ${t.text}`}>
              {new Date(budget.exhausted_on).toLocaleDateString(undefined, {
                month: "short",
                day: "numeric",
              })}
            </dd>
          </>
        )}
      </dl>

      <p className="mt-3 text-[11px] leading-relaxed text-zinc-400">
        Reported only. Manifold does not block a launch on this figure, and it
        counts just the instances Manifold started, so treat it as a floor.
      </p>
    </div>
  );
}
