"use client";

import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type GateableAction,
  type NotificationKind,
  type Preferences,
  type PreferencesPatch,
} from "@/lib/api";

// The three policies that decide how Manifold behaves when nobody is
// watching: what pauses for you, what pings you, and what happens to your
// files when a GPU is torn down.
export function PolicySettings() {
  const [prefs, setPrefs] = useState<Preferences | null>(null);
  const [defaults, setDefaults] = useState<{
    max_concurrent_instances: number;
    max_hourly_spend_usd: number;
  } | null>(null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api
      .preferences()
      .then((r) => {
        setPrefs(r.preferences);
        setDefaults(r.guardrail_defaults);
      })
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : String(err)),
      );
  }, []);

  // Optimistic: flip the switch immediately, then persist. A settings toggle
  // that lags behind the click feels broken even when it is working.
  async function save(patch: PreferencesPatch) {
    if (!prefs) return;
    const optimistic: Preferences = {
      approvals: { ...prefs.approvals, ...patch.approvals },
      notifications: { ...prefs.notifications, ...patch.notifications },
      data_safety: { ...prefs.data_safety, ...patch.data_safety },
      guardrails: { ...prefs.guardrails, ...patch.guardrails },
      worklog: { ...prefs.worklog, ...patch.worklog },
    };
    setPrefs(optimistic);
    setError("");
    try {
      setPrefs(await api.updatePreferences(patch));
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      const fresh = await api.preferences().catch(() => null);
      if (fresh) setPrefs(fresh.preferences);
    }
  }

  if (!prefs) {
    return (
      <section className="rounded-lg border border-zinc-200 bg-white p-4 text-sm text-zinc-500">
        Loading policies...
      </section>
    );
  }

  const ds = prefs.data_safety;

  return (
    <>
      {error && (
        <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
        </p>
      )}

      {/* -- spending guardrails --------------------------------------------- */}
      <section className="rounded-lg border border-zinc-200 bg-white p-4">
        <SectionHead title="Spending guardrails" note={saved ? "saved" : ""} />
        <p className="mt-1 text-xs text-zinc-500">
          Hard limits every launch is checked against - yours, an agent&apos;s,
          or Autopilot&apos;s. A launch over either limit is refused outright,
          never queued. Blank uses the config.yaml default.
        </p>

        <div className="mt-3 flex flex-wrap gap-6">
          <label className="block text-xs font-medium text-zinc-600">
            Max instances at once
            <input
              type="number"
              min={0}
              step={1}
              value={prefs.guardrails.max_concurrent_instances || ""}
              placeholder={String(defaults?.max_concurrent_instances ?? 1)}
              onChange={(e) =>
                save({
                  guardrails: {
                    max_concurrent_instances: Math.max(
                      0,
                      Math.floor(Number(e.target.value) || 0),
                    ),
                  },
                })
              }
              className="mt-1 block w-28 rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
            />
          </label>
          <label className="block text-xs font-medium text-zinc-600">
            Max hourly spend (USD)
            <input
              type="number"
              min={0}
              step={0.5}
              value={prefs.guardrails.max_hourly_spend_usd || ""}
              placeholder={String(defaults?.max_hourly_spend_usd ?? 4)}
              onChange={(e) =>
                save({
                  guardrails: {
                    max_hourly_spend_usd: Math.max(
                      0,
                      Number(e.target.value) || 0,
                    ),
                  },
                })
              }
              className="mt-1 block w-28 rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
            />
          </label>
        </div>
        {prefs.guardrails.max_concurrent_instances > 4 && (
          <p className="mt-2 rounded border border-amber-300/40 bg-amber-50 px-2 py-1 text-[11px] text-amber-700">
            {prefs.guardrails.max_concurrent_instances} simultaneous instances
            can bill fast - the burn chip in the header shows the live total.
          </p>
        )}
      </section>

      {/* -- approvals ------------------------------------------------------ */}
      <section className="rounded-lg border border-zinc-200 bg-white p-4">
        <SectionHead
          title="Ask me before Autopilot..."
          note={saved ? "saved" : ""}
        />
        <p className="mt-1 text-xs text-zinc-500">
          These pause the agent until you approve or deny. Everything else runs
          freely inside your budget and concurrency guards.
        </p>

        <div className="mt-3 space-y-2.5">
          <Toggle
            checked={prefs.approvals.launch_gpu}
            onChange={(v) => save({ approvals: { launch_gpu: v } })}
            label="starts a GPU instance"
            hint="The one that costs money to say yes to. Recommended."
          />
          <Toggle
            checked={prefs.approvals.run_job}
            onChange={(v) => save({ approvals: { run_job: v } })}
            label="runs a job"
            hint="The GPU is already paid for while this waits."
            warn={
              prefs.approvals.run_job
                ? "A GPU is already billing while this approval sits unanswered."
                : ""
            }
          />
          <Toggle
            checked={prefs.approvals.terminate_instance}
            onChange={(v) => save({ approvals: { terminate_instance: v } })}
            label="terminates an instance"
            hint="Not recommended: see below."
            warn={
              prefs.approvals.terminate_instance
                ? "An unanswered approval auto-denies after 10 minutes, and a denied shutdown means the GPU keeps billing. Gating this costs you money exactly when you are away from the keyboard, which is when Autopilot runs."
                : ""
            }
          />
        </div>

        <p className="mt-3 border-t border-zinc-100 pt-3 text-xs text-zinc-400">
          Why the shutdown is not gated by default: an approval nobody answers
          auto-denies. Refusing a launch costs nothing. Refusing a shutdown
          keeps the meter running.
        </p>
      </section>

      {/* -- notifications -------------------------------------------------- */}
      <section className="rounded-lg border border-zinc-200 bg-white p-4">
        <SectionHead title="Ping me when..." note={saved ? "saved" : ""} />
        <p className="mt-1 text-xs text-zinc-500">
          A notification appears in the bell, and (unless you switch it off
          below) as a real system notification so it reaches you in another
          app.
        </p>

        <div className="mt-3 space-y-2.5">
          {(
            [
              ["approval_requested", "Autopilot needs my approval", "The run is paused until you answer."],
              ["job_succeeded", "A job succeeds", ""],
              ["job_failed", "A job fails", ""],
              ["run_finished", "An Autopilot run ends", ""],
              ["data_transferred", "Files are saved off an instance", "Or could not be saved, which is the one you want to hear about."],
              ["capacity_available", "A capacity watch finds its GPU", "Without auto-launch, this notification IS the watch."],
              ["instance_idle", "An instance is billing while idle", "Once per instance, with what the idle time has cost so far. It never terminates anything."],
              ["instance_ceiling", "An instance is near, or past, its max lifetime", "The heads-up before Manifold terminates on a lifetime ceiling, and the cases where it hit the ceiling and deliberately did not destroy the box."],
            ] as [NotificationKind, string, string][]
          ).map(([kind, label, hint]) => (
            <Toggle
              key={kind}
              checked={prefs.notifications[kind]}
              onChange={(v) => save({ notifications: { [kind]: v } })}
              label={label}
              hint={hint}
            />
          ))}
        </div>

        <div className="mt-3 border-t border-zinc-100 pt-3">
          <Toggle
            checked={prefs.notifications.desktop}
            onChange={(v) => save({ notifications: { desktop: v } })}
            label="Use system notifications"
            hint="Off means the bell only. The bell keeps the history either way."
          />
        </div>
      </section>

      {/* -- data safety ---------------------------------------------------- */}
      <section className="rounded-lg border border-zinc-200 bg-white p-4">
        <SectionHead
          title="When an instance shuts down"
          note={saved ? "saved" : ""}
        />
        <p className="mt-1 text-xs text-zinc-500">
          An instance&apos;s scratch disk is destroyed with the instance. Before
          any shutdown, Manifold saves what is on it.
        </p>

        <p className="mt-3 text-xs font-medium text-zinc-600">Save it where?</p>
        <div className="mt-2 space-y-2.5">
          <Toggle
            checked={ds.to_filesystem}
            onChange={(v) => save({ data_safety: { to_filesystem: v } })}
            label="To my Lambda filesystem"
            hint="A copy inside the datacenter. Fast, free, and it covers everything."
          />
          <Toggle
            checked={ds.to_local}
            onChange={(v) => save({ data_safety: { to_local: v } })}
            label="Download to this machine"
            hint="Comes down the SSH connection while the GPU is still billing, so it is budgeted below."
          />
        </div>

        {ds.to_local && (
          <div className="mt-3 space-y-3 rounded border border-zinc-200 bg-zinc-50 p-3">
            <label className="block text-xs font-medium text-zinc-600">
              Save to
              <input
                type="text"
                value={ds.local_dir}
                onChange={(e) =>
                  setPrefs({
                    ...prefs,
                    data_safety: { ...ds, local_dir: e.target.value },
                  })
                }
                onBlur={(e) =>
                  save({ data_safety: { local_dir: e.target.value.trim() } })
                }
                className="mt-1 block w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
              />
              <span className="mt-1 block text-[11px] font-normal text-zinc-400">
                Files land in{" "}
                <span className="font-mono">{ds.local_dir}/&lt;instance&gt;/</span>
              </span>
            </label>

            <label className="block text-xs font-medium text-zinc-600">
              Transfer at most
              <span className="mt-1 flex items-center gap-2">
                <input
                  type="number"
                  min={0}
                  step={5}
                  value={ds.max_local_gib}
                  onChange={(e) =>
                    save({
                      data_safety: { max_local_gib: Number(e.target.value) },
                    })
                  }
                  className="w-24 rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
                />
                <span className="text-xs font-normal text-zinc-500">
                  GiB per instance. Anything that does not fit is left behind
                  and reported, never dropped silently.
                </span>
              </span>
            </label>
          </div>
        )}

        <p className="mt-4 text-xs font-medium text-zinc-600">Save what?</p>
        <div className="mt-2 space-y-2">
          <Radio
            checked={ds.scope === "all"}
            onChange={() => save({ data_safety: { scope: "all" } })}
            label="Everything on the scratch disk"
            hint="Model weights, checkpoints, datasets, results."
          />
          <Radio
            checked={ds.scope === "outputs"}
            onChange={() => save({ data_safety: { scope: "outputs" } })}
            label="Only what the job produced"
            hint="Files under outputs/. Leaves multi-GB checkpoints and caches behind."
          />
        </div>

        <p className="mt-4 text-xs font-medium text-zinc-600">
          And if a file cannot be saved?
        </p>
        <div className="mt-2 space-y-2">
          <Radio
            checked={ds.if_unsaveable === "block"}
            onChange={() => save({ data_safety: { if_unsaveable: "block" } })}
            label="Keep the instance running and tell me"
            hint="The GPU keeps billing, but nothing is lost. Data loss is permanent; a billing hour is not."
          />
          <Radio
            checked={ds.if_unsaveable === "terminate"}
            onChange={() =>
              save({ data_safety: { if_unsaveable: "terminate" } })
            }
            label="Shut it down anyway"
            hint="Stops the billing. Those files are gone. Recorded in the audit log."
          />
        </div>
      </section>

      {/* -- worklog ---------------------------------------------------------- */}
      <section className="rounded-lg border border-zinc-200 bg-white p-4">
        <SectionHead title="Worklog" note={saved ? "saved" : ""} />
        <p className="mt-1 text-xs text-zinc-500">
          Every finished job and Autopilot run is written as a markdown entry
          to worklog.md in the Manifold data folder. Mirror those entries
          into a folder of yours - an Obsidian vault, a project repo - so
          other tools and agents can read what Manifold accomplished.
        </p>
        <label className="mt-3 block text-xs font-medium text-zinc-600">
          Mirror folder (blank = no mirror)
          <input
            type="text"
            placeholder="/Users/you/ObsidianVault/Manifold"
            value={prefs.worklog.mirror_dir}
            onChange={(e) =>
              setPrefs({
                ...prefs,
                worklog: { mirror_dir: e.target.value },
              })
            }
            onBlur={(e) =>
              save({ worklog: { mirror_dir: e.target.value.trim() } })
            }
            className="mt-1 block w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
          />
          <span className="mt-1 block text-[11px] font-normal text-zinc-400">
            Entries append to{" "}
            <span className="font-mono">manifold-worklog.md</span> in that
            folder.
          </span>
        </label>
      </section>
    </>
  );
}

function SectionHead({ title, note }: { title: string; note?: string }) {
  return (
    <div className="flex items-baseline justify-between">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
        {title}
      </h2>
      {note && (
        <span className="font-mono text-[11px] text-teal-400">{note}</span>
      )}
    </div>
  );
}

function Toggle({
  checked,
  onChange,
  label,
  hint,
  warn,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint?: string;
  warn?: string;
}) {
  return (
    <div>
      <label className="flex cursor-pointer items-start gap-2.5">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-teal-400"
        />
        <span className="min-w-0">
          <span className="text-sm text-zinc-800">{label}</span>
          {hint && (
            <span className="ml-1.5 text-xs text-zinc-400">{hint}</span>
          )}
        </span>
      </label>
      {warn && (
        <p className="ml-6 mt-1 rounded border border-amber-300/40 bg-amber-50 px-2 py-1 text-[11px] text-amber-700">
          {warn}
        </p>
      )}
    </div>
  );
}

function Radio({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: () => void;
  label: string;
  hint?: string;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-2.5">
      <input
        type="radio"
        checked={checked}
        onChange={onChange}
        className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-teal-400"
      />
      <span className="min-w-0">
        <span className="text-sm text-zinc-800">{label}</span>
        {hint && <span className="ml-1.5 text-xs text-zinc-400">{hint}</span>}
      </span>
    </label>
  );
}
