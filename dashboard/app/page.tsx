"use client";

import { useState } from "react";
import Link from "next/link";
import { api, type Launch } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { LaunchForm } from "@/components/LaunchForm";
import { InstanceCard } from "@/components/InstanceCard";
import { StatusBadge } from "@/components/Badge";
import { WatchPanel } from "@/components/WatchPanel";
import { ClusterPanel } from "@/components/ClusterPanel";
import { VisualTaskGraph } from "@/components/VisualTaskGraph";
import { MultiGpuTelemetry } from "@/components/MultiGpuTelemetry";
import {
  useSpendSummary,
  OrphanedSpendAlert,
  SpendTotalLink,
} from "@/components/SpendSummary";
import { formatMoney } from "@/lib/format";

const IN_FLIGHT = ["launching", "retrying", "booting"];
const RECENT_FAILURE_WINDOW_MS = 15 * 60 * 1000;

export default function InstancesPage() {
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());

  const { data, error, stale, lastSuccess, refresh } = usePolling(async () => {
    const [instances, launches] = await Promise.all([
      api.instances(),
      api.launches(),
    ]);
    return { instances, launches };
  }, 2000);

  const { data: setup } = usePolling(() => api.settingsStatus(), 10000);
  // Spend is the backend's number, polled slowly (see SpendSummary).
  const { data: spend } = useSpendSummary();
  // Cheap polls that only decide whether the view-only panels below are worth
  // mounting (see the gating note near the render). The panels do their own
  // finer-grained polling once they mount.
  const { data: taskList } = usePolling(api.tasks, 8000);

  const instances = data?.instances ?? [];
  const launches = data?.launches ?? [];

  // Gates for the pure-view panels: only render them when they have something
  // to show, so the default (empty/mock) screen isn't three big empty boxes.
  const hasTasks = (taskList?.length ?? 0) > 0;
  const hasTelemetry = launches.some(
    (l) => l.status === "active" && l.lambda_instance_id,
  );

  // Launches still working their way toward an instance card.
  const inFlight = launches.filter((l) => IN_FLIGHT.includes(l.status));
  // Recent failures stay visible until dismissed: never fail silently.
  const failed = launches.filter(
    (l) =>
      l.status === "failed" &&
      !dismissed.has(l.id) &&
      Date.now() - new Date(l.created_at).getTime() < RECENT_FAILURE_WINDOW_MS,
  );

  // What the instances listed right now cost per hour. The historical total
  // beside it is NOT derived here: it comes from /spend/summary, which is the
  // only place the cost formula lives.
  const hourlyBurn = instances.reduce((sum, i) => sum + i.hourly_rate_usd, 0);

  return (
    <div className="space-y-6">
      {setup && !setup.mock && !setup.lambda_configured && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          <span className="font-medium">Almost there:</span> no Lambda API
          key is configured, so the launch form has nothing to show.{" "}
          <Link href="/settings" className="font-medium underline">
            Add your key in Settings
          </Link>{" "}
          — it takes one paste.
        </div>
      )}
      {setup?.mock && (
        <div className="rounded-lg border border-zinc-300 bg-zinc-100 px-4 py-3 text-xs text-zinc-600">
          Mock mode: demo catalog, zero spend. Real GPUs need the backend
          started without MANIFOLD_MOCK=1 and a key in{" "}
          <Link href="/settings" className="underline">
            Settings
          </Link>
          .
        </div>
      )}
      {spend && <OrphanedSpendAlert summary={spend} linkToInstances={false} />}
      <div className="flex flex-wrap items-start justify-end gap-x-6 gap-y-2 text-sm">
        <span className="text-zinc-500">
          Current burn:{" "}
          <span className="font-medium tabular-nums text-zinc-900">
            {formatMoney(hourlyBurn)}/hr
          </span>
        </span>
        {/* Nothing while the backend has not answered: a reassuring $0 that
            turns out to be $400 is worse than a blank. */}
        {spend && <SpendTotalLink summary={spend} />}
      </div>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Launch an instance
        </h2>
        <LaunchForm onLaunched={refresh} />
      </section>

      {error && (
        <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
          {stale && lastSuccess && (
            <span className="mt-1 block font-medium">
              Everything below is a snapshot from{" "}
              {lastSuccess.toLocaleTimeString()} — NOT live. Instances may
              have changed (or been terminated) since; check the Lambda
              console for current billing truth until the backend is back.
            </span>
          )}
        </p>
      )}

      {(inFlight.length > 0 || failed.length > 0) && (
        <section className="space-y-3">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
            Pending launches
          </h2>
          {inFlight.map((l) => (
            <PendingLaunchCard key={l.id} launch={l} />
          ))}
          {failed.map((l) => (
            <FailedLaunchCard
              key={l.id}
              launch={l}
              onDismiss={() => setDismissed((prev) => new Set(prev).add(l.id))}
            />
          ))}
        </section>
      )}

      <section className="space-y-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Running instances
        </h2>
        {instances.length === 0 ? (
          <p className="rounded-lg border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500">
            No instances running. Nothing is billing.
          </p>
        ) : (
          // Stale = the backend stopped answering: grey the cards and block
          // interaction so a snapshot can't be mistaken for live instances.
          <div
            className={`space-y-3 ${stale ? "pointer-events-none opacity-40" : ""}`}
          >
            {instances.map((i) => (
              <InstanceCard key={i.id} instance={i} onChanged={refresh} />
            ))}
          </div>
        )}
      </section>

      {/* Elastic GPU Clusters — always shown: it carries the "Launch Swarm"
          entry point, and its own empty state is a single compact line. */}
      <section>
        <ClusterPanel />
      </section>

      {/* The task graph and live telemetry are pure views. Mount each only
          when it has data; otherwise show a compact single-line placeholder so
          the empty/mock screen reads as intentional, not broken. */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {hasTasks ? (
          <VisualTaskGraph />
        ) : (
          <p className="rounded-xl border border-dashed border-zinc-300 bg-zinc-50 px-4 py-3 text-xs text-zinc-500">
            Agent task graph appears here when jobs or cluster tasks run.
          </p>
        )}
        {hasTelemetry ? (
          <MultiGpuTelemetry />
        ) : (
          <p className="rounded-xl border border-dashed border-zinc-300 bg-zinc-50 px-4 py-3 text-xs text-zinc-500">
            Live GPU telemetry streams here once an instance is online.
          </p>
        )}
      </div>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Capacity watches
        </h2>
        <WatchPanel />
      </section>
    </div>
  );
}

function mmss(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function PendingLaunchCard({ launch }: { launch: Launch }) {
  const booting =
    launch.phase === "waiting_for_active" &&
    launch.boot_elapsed_seconds != null &&
    launch.boot_timeout_seconds != null;
  const pct = booting
    ? Math.min(
        100,
        (launch.boot_elapsed_seconds! / launch.boot_timeout_seconds!) * 100,
      )
    : 0;
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <StatusBadge status={launch.status} />
          <span className="text-sm font-medium">
            {launch.requested_type} in {launch.region}
          </span>
        </div>
        <span className="text-xs text-zinc-500">attempt {launch.attempts}</span>
      </div>
      {booting && (
        <div className="mt-2">
          <div className="flex items-center justify-between text-xs text-amber-800">
            <span>Booting on Lambda</span>
            <span className="tabular-nums">
              {mmss(launch.boot_elapsed_seconds!)} /{" "}
              {mmss(launch.boot_timeout_seconds!)}
            </span>
          </div>
          <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-amber-200">
            <div
              className="h-full rounded-full bg-amber-500 transition-all"
              style={{ width: `${pct}%` }}
            />
          </div>
          <p className="mt-1 text-[11px] text-amber-700">
            Large GPU instances can take 15-40 minutes to boot. Safe to leave
            this running.
          </p>
        </div>
      )}
      {!booting && launch.phase_detail && (
        <p className="mt-2 text-xs text-amber-800">{launch.phase_detail}</p>
      )}
      {launch.error && (
        <p className="mt-2 text-xs text-amber-800">{launch.error}</p>
      )}
    </div>
  );
}

function FailedLaunchCard({
  launch,
  onDismiss,
}: {
  launch: Launch;
  onDismiss: () => void;
}) {
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <StatusBadge status="failed" />
          <span className="text-sm font-medium">
            {launch.requested_type} in {launch.region}
          </span>
          <span className="text-xs text-zinc-500">
            after {launch.attempts} attempt{launch.attempts === 1 ? "" : "s"}
          </span>
        </div>
        <button
          onClick={onDismiss}
          className="rounded border border-zinc-300 px-2 py-0.5 text-xs text-zinc-600 hover:bg-white"
        >
          Dismiss
        </button>
      </div>
      {launch.error && (
        <p className="mt-2 text-xs text-red-800">{launch.error}</p>
      )}
    </div>
  );
}
