"use client";

import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type AgentRun,
  type AgentStep,
  type Brain,
  type GateableAction,
} from "@/lib/api";
import { ApprovalsPanel } from "@/components/ApprovalsPanel";
import { usePolling } from "@/lib/usePolling";
import { Badge, StatusBadge } from "@/components/Badge";
import { formatDate } from "@/lib/format";

const GATE_LABEL: Record<GateableAction, string> = {
  launch_gpu: "starting a GPU",
  run_job: "running a job",
  terminate_instance: "shutting one down",
};

const KIND_LABEL: Record<Brain["kind"], string> = {
  instance: "GPU instance",
  local: "this machine",
  api: "frontier API",
  cli: "your login",
};

const KIND_TONE: Record<Brain["kind"], string> = {
  instance: "bg-emerald-100 text-emerald-800",
  local: "bg-sky-100 text-sky-800",
  api: "bg-indigo-100 text-indigo-800",
  cli: "bg-amber-100 text-amber-800",
};

// Autopilot: a model (served on one of YOUR instances, running locally, or a
// frontier API) drives Manifold's guarded operations toward a goal. Every step
// is recorded below and on the Activity page's audit trail; budget and concurrency
// guards bind the autopilot exactly as they bind you.
export default function AutopilotPage() {
  const [brain, setBrain] = useState("");
  const [goal, setGoal] = useState("");
  const [maxSteps, setMaxSteps] = useState(20);
  const [unlimited, setUnlimited] = useState(false);
  // Which actions pause for approval. Seeded from the Settings policy, and
  // overridable for this one run.
  const [gates, setGates] = useState<GateableAction[] | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  // Persistent project brief: included in every run's system prompt so a
  // goal reads as one step in the project, not an isolated command.
  const [brief, setBrief] = useState("");
  const [briefSaved, setBriefSaved] = useState(false);

  useEffect(() => {
    api
      .projectBrief()
      .then((r) => setBrief(r.content))
      .catch(() => {});
  }, []);

  async function saveBrief(content: string) {
    try {
      await api.setProjectBrief(content);
      setBriefSaved(true);
      setTimeout(() => setBriefSaved(false), 1500);
    } catch {
      // Non-fatal: the run still works without the brief.
    }
  }

  const { data: runs, refresh } = usePolling(() => api.autopilotRuns(), 2000);
  // Every model that can drive Manifold: served on a GPU instance, running
  // locally (Ollama/LM Studio), or a frontier API with a key in Settings.
  const { data: brainList } = usePolling(() => api.brains(), 5000);
  const brains: Brain[] = brainList ?? [];

  useEffect(() => {
    if (brains.length > 0 && !brain) setBrain(brains[0].ref);
  }, [brains, brain]);

  useEffect(() => {
    api
      .preferences()
      .then((r) =>
        setGates(
          r.gateable_actions.filter((a) => r.preferences.approvals[a]),
        ),
      )
      .catch(() => setGates(["launch_gpu"]));
  }, []);

  function toggleGate(action: GateableAction, on: boolean) {
    setGates((g) =>
      on
        ? [...(g ?? []), action]
        : (g ?? []).filter((a) => a !== action),
    );
  }

  async function start(e: React.FormEvent) {
    e.preventDefault();
    setStarting(true);
    setError("");
    try {
      await api.startAutopilot({
        goal: goal.trim(),
        brain,
        max_steps: unlimited ? undefined : maxSteps,
        unlimited_steps: unlimited,
        approve_actions: gates ?? [],
      });
      setGoal("");
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="space-y-6">
      <section className="rounded-lg border border-zinc-200 bg-white p-4">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Start a run
        </h2>
        {brains.length === 0 ? (
          <p className="mt-3 text-sm text-zinc-500">
            No brain available. A brain can be: a model served on a running
            instance (queue{" "}
            <span className="font-mono text-xs">vllm-serve</span> on the Jobs
            page), a local model server on this machine (start Ollama or LM
            Studio and it appears here automatically), or a frontier API
            (add an Anthropic/OpenAI/Gemini key in Settings).
          </p>
        ) : (
          <form onSubmit={start} className="mt-3 space-y-3">
            <div className="flex flex-wrap gap-3">
              <label className="block min-w-0 text-xs font-medium text-zinc-600">
                Brain
                <select
                  className="mt-1 block w-full min-w-0 max-w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
                  value={brain}
                  onChange={(e) => setBrain(e.target.value)}
                >
                  {brains.map((b) => (
                    <option key={b.ref} value={b.ref}>
                      {b.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block text-xs font-medium text-zinc-600">
                Step limit
                <span className="mt-1 flex items-center gap-2">
                  <input
                    type="number"
                    min={1}
                    max={50}
                    disabled={unlimited}
                    className="block w-24 rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm disabled:opacity-40"
                    value={maxSteps}
                    onChange={(e) => setMaxSteps(Number(e.target.value))}
                  />
                  <label
                    className="flex cursor-pointer items-center gap-1.5 text-xs font-normal text-zinc-600"
                    title="The run ends only when the agent finishes, fails, or you cancel it. Spend is still bounded by your guardrails and approval gates - this only unbounds the number of turns."
                  >
                    <input
                      type="checkbox"
                      className="h-3.5 w-3.5 accent-teal-400"
                      checked={unlimited}
                      onChange={(e) => setUnlimited(e.target.checked)}
                    />
                    unlimited
                  </label>
                </span>
              </label>
            </div>
            <textarea
              className="w-full rounded border border-zinc-300 px-2.5 py-1.5 text-sm"
              rows={3}
              placeholder='e.g. "Run the gpu-smoke job on this instance and report whether the GPU is healthy, then stop."'
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              required
              minLength={4}
            />
            <p className="text-[11px] text-zinc-400">
              The goal IS the agent&apos;s briefing - it starts knowing your
              instances, templates, and guards, and nothing else. Good goals
              name the outcome, the data&apos;s location on the filesystem, and
              the bounds: &ldquo;Synthesize 500 Q&amp;A pairs from{" "}
              <span className="font-mono">datasets/scrape.jsonl</span> using
              the cheapest GPU that fits, save to{" "}
              <span className="font-mono">outputs/qa/</span>, terminate when
              done.&rdquo; If no template fits the work, the agent can author
              a custom one mid-run (it stays in your Jobs page afterwards).
            </p>
            <div className="rounded border border-zinc-200 bg-zinc-50 p-2.5">
              <div className="flex items-baseline justify-between">
                <p className="text-xs font-medium text-zinc-600">
                  Project brief (saved; included in every run)
                </p>
                {briefSaved && (
                  <span className="font-mono text-[11px] text-teal-500">
                    saved
                  </span>
                )}
              </div>
              <textarea
                className="mt-1.5 w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
                rows={2}
                placeholder='What you are working on overall, e.g. "Red Hope: a 3D colony game. Assets pipeline lives on Somnora-East under red_hope/; outputs go to models/."'
                value={brief}
                onChange={(e) => setBrief(e.target.value)}
                onBlur={(e) => saveBrief(e.target.value.trim())}
              />
              <p className="mt-1 text-[11px] text-zinc-400">
                Persistent context for the agent: the goal above becomes one
                step in this project instead of an isolated command. Leave
                blank for none.
              </p>
            </div>
            <div className="rounded border border-zinc-200 bg-zinc-50 p-2.5">
              <p className="text-xs font-medium text-zinc-600">
                Ask me before the agent...
              </p>
              <div className="mt-1.5 flex flex-wrap gap-x-5 gap-y-1.5">
                {(
                  Object.keys(GATE_LABEL) as GateableAction[]
                ).map((action) => (
                  <label
                    key={action}
                    className="flex cursor-pointer items-center gap-1.5 text-xs text-zinc-600"
                  >
                    <input
                      type="checkbox"
                      className="h-3.5 w-3.5 accent-teal-400"
                      checked={(gates ?? []).includes(action)}
                      onChange={(e) => toggleGate(action, e.target.checked)}
                    />
                    {GATE_LABEL[action]}
                  </label>
                ))}
              </div>
              {(gates ?? []).includes("terminate_instance") && (
                <p className="mt-2 rounded border border-amber-300/40 bg-amber-50 px-2 py-1 text-[11px] text-amber-700">
                  A shutdown approval you do not answer auto-denies, and the
                  GPU keeps billing while it waits. Leaving this off lets the
                  agent clean up after itself.
                </p>
              )}
              <p className="mt-1.5 text-[11px] text-zinc-400">
                Defaults come from Settings. Each gated action pauses here
                until you approve or deny it.
              </p>
            </div>
            <div className="flex items-center justify-between gap-4">
              <p className="text-xs text-zinc-400">
                The agent can launch GPUs (your budget and concurrency guards
                apply), run jobs, read logs, save outputs, and terminate
                instances. Every step is audited.
              </p>
              <button
                type="submit"
                disabled={starting || !goal.trim()}
                className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
              >
                {starting ? "Starting..." : "Start run"}
              </button>
            </div>
          </form>
        )}
        {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
      </section>

      <ApprovalsPanel />

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Runs
        </h2>
        <div className="space-y-3">
          {(runs ?? []).map((run) => (
            <RunCard key={run.id} run={run} onChanged={refresh} />
          ))}
          {(runs ?? []).length === 0 && (
            <p className="rounded-lg border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500">
              No runs yet.
            </p>
          )}
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Brains
        </h2>
        <div className="space-y-2">
          {brains.map((b: Brain) => (
            <div
              key={b.ref}
              className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-zinc-200 bg-white p-3"
            >
              <div className="flex min-w-0 items-center gap-2.5">
                <span
                  className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${KIND_TONE[b.kind]}`}
                >
                  {KIND_LABEL[b.kind]}
                </span>
                <span className="truncate text-sm font-medium">{b.model}</span>
              </div>
              <span className="truncate font-mono text-xs text-zinc-400">
                {b.detail || b.ref}
              </span>
            </div>
          ))}
          {brains.length === 0 && (
            <div className="rounded-lg border border-dashed border-zinc-300 p-6 text-sm text-zinc-500">
              <p className="font-medium text-zinc-600">No brains found yet.</p>
              <ul className="mt-2 list-inside list-disc space-y-1 text-xs">
                <li>
                  Serve a model on a GPU instance (Jobs page,{" "}
                  <span className="font-mono">vllm-serve</span>) - it appears
                  here once running.
                </li>
                <li>
                  Start Ollama or LM Studio on this machine - detected
                  automatically within seconds.
                </li>
                <li>
                  Log into a frontier CLI once (claude, codex, or gemini) -
                  it appears here via your own subscription, no API key
                  needed.
                </li>
                <li>
                  Or add an Anthropic / OpenAI / Gemini API key to .env
                  (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY) - the
                  frontier brain appears on the next refresh.
                </li>
              </ul>
            </div>
          )}
        </div>
        <p className="mt-2 text-xs text-zinc-400">
          Any brain here can drive a run or be the reasoning end of a
          pipeline: a local model orchestrating cloud GPUs, a frontier model
          reviewing a fine-tune, one instance directing another.
        </p>
      </section>
    </div>
  );
}

function RunCard({ run, onChanged }: { run: AgentRun; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [steps, setSteps] = useState<AgentStep[]>([]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const load = () =>
      api
        .autopilotRun(run.id)
        .then((r) => {
          if (!cancelled) setSteps(r.steps);
        })
        .catch(() => {});
    load();
    const id = run.status === "running" ? setInterval(load, 1500) : undefined;
    return () => {
      cancelled = true;
      if (id) clearInterval(id);
    };
  }, [open, run.id, run.status]);

  async function cancel() {
    try {
      await api.cancelAutopilot(run.id);
      onChanged();
    } catch {
      /* already finished */
    }
  }

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          {/* A run that only ever READ things did not accomplish anything,
              whatever its closing summary claims. Showing it as plain
              "succeeded" is how a fabricated answer came to wear the same
              green as a run that launched a GPU and cleaned up after
              itself. The status is still shown - it is true that the loop
              ended cleanly - but it no longer stands alone. */}
          {run.status === "succeeded" && run.effect === "no_effect" ? (
            <>
              <Badge label="no action taken" tone="zinc" />
              <span className="shrink-0 text-xs text-zinc-400">
                (ended cleanly)
              </span>
            </>
          ) : (
            <StatusBadge status={run.status} />
          )}
          {/* The expensive shape: it started a box and never stopped one.
              An idle sweep may have reaped it since - this states what the
              RUN did, which is the part nothing else recorded. */}
          {run.launched && !run.terminated && run.status !== "running" && (
            <Badge label="left an instance running" tone="amber" />
          )}
          <span className="truncate text-sm font-medium">{run.goal}</span>
        </div>
        <div className="flex shrink-0 items-center gap-3 text-xs text-zinc-500">
          <span>
            {run.steps_taken}/{run.max_steps === 0 ? "∞" : run.max_steps}{" "}
            steps
          </span>
          <span>{formatDate(run.created_at)}</span>
          {run.status === "running" && (
            <button
              onClick={cancel}
              className="rounded border border-red-200 px-2 py-0.5 font-medium text-red-700 hover:bg-red-50"
            >
              Cancel
            </button>
          )}
          <button
            onClick={() => setOpen((s) => !s)}
            className="rounded border border-zinc-300 px-2 py-0.5 hover:bg-zinc-50"
          >
            {open ? "Hide steps" : "Steps"}
          </button>
        </div>
      </div>

      <p className="mt-1 text-xs text-zinc-400">
        brain: <span className="font-mono">{run.brain_model}</span>{" "}
        <span className="text-zinc-300">·</span>{" "}
        <span className="font-mono">{run.brain_instance_id}</span>
      </p>
      {run.summary && (
        <p className="mt-2 rounded bg-emerald-50 px-3 py-2 text-sm text-emerald-900">
          {run.summary}
        </p>
      )}
      {run.error && (
        <p className="mt-2 rounded bg-red-50 px-3 py-2 text-xs text-red-800">
          {run.error}
        </p>
      )}

      {open && (
        <ol className="mt-3 space-y-2">
          {steps.map((s) => (
            <li
              key={s.seq}
              className="rounded border border-zinc-100 bg-zinc-50 p-2 text-xs"
            >
              <div className="flex items-center gap-2">
                <span className="font-mono text-zinc-400">#{s.seq}</span>
                <span className="font-mono font-medium text-zinc-800">
                  {s.action}
                </span>
                {"error" in s.result && (
                  <span className="rounded bg-red-100 px-1.5 text-red-800">
                    refused
                  </span>
                )}
              </div>
              {s.thought && (
                <p className="mt-1 italic text-zinc-500">{s.thought}</p>
              )}
              {Object.keys(s.args).length > 0 && (
                <p className="mt-1 break-all font-mono text-zinc-600">
                  {JSON.stringify(s.args)}
                </p>
              )}
              <p className="mt-1 max-h-24 overflow-y-auto break-all font-mono text-zinc-500">
                → {JSON.stringify(s.result).slice(0, 600)}
              </p>
            </li>
          ))}
          {steps.length === 0 && (
            <p className="text-xs text-zinc-400">No steps yet.</p>
          )}
        </ol>
      )}
    </div>
  );
}
