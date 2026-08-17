"use client";

import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type Instance,
  type ModelFit,
  type ModelPreset,
  type Task,
  type Template,
} from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { Badge, StatusBadge } from "@/components/Badge";
import { PollErrorBanner } from "@/components/PollErrorBanner";
import { ParameterForm } from "@/components/ParameterForm";
import { EstimateWidget } from "@/components/EstimateWidget";
import { TemplateEditor } from "@/components/TemplateEditor";
import { DistillConfigPanel } from "@/components/DistillConfigPanel";
import { OwnItLocallyPanel } from "@/components/OwnItLocallyPanel";
import {
  AutoManageControls,
  type AutoManageState,
} from "@/components/AutoManageControls";
import { LifecyclePipeline } from "@/components/LifecyclePipeline";
import { useTerminalDock } from "@/components/TerminalDock";
import { formatDate, formatDuration } from "@/lib/format";

// Accept a pasted HuggingFace URL or a bare id, and trim stray whitespace /
// trailing punctuation (a trailing ";" once caused a serve failure).
function normalizeModelId(raw: string): string {
  let v = raw.trim();
  const m = v.match(/huggingface\.co\/([^/\s]+\/[^/\s?#]+)/i);
  if (m) v = m[1];
  return v.replace(/[;,\s/]+$/g, "");
}

// A job is still "active" while its auto-managed lifecycle is in flight, even
// after the container itself has exited (it is still syncing/terminating).
const TERMINAL_LIFECYCLE = ["done", "failed", "cancelled", "skipped"];

// Serve templates cannot be dependency parents: they never exit on their
// own, so "after it succeeds" would mean never. The backend enforces this
// (422) for every template with ports; the picker filters the bundled two.
const SERVE_TEMPLATES = ["vllm-serve", "sglang-serve"];
function isActiveJob(t: Task): boolean {
  if (t.status === "queued" || t.status === "running") return true;
  return (
    t.auto_manage &&
    !!t.lifecycle &&
    !TERMINAL_LIFECYCLE.includes(t.lifecycle)
  );
}

export default function JobsPage() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [templateErrors, setTemplateErrors] = useState<Record<string, string>>({});
  const [presets, setPresets] = useState<ModelPreset[]>([]);
  const [selected, setSelected] = useState("");
  const [seed, setSeed] = useState<{ model_id: string; parameters?: Record<string, unknown> } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [auto, setAuto] = useState<AutoManageState>({
    enabled: false,
    gpu_type: "",
    region: "",
    filesystem: "",
  });

  // error/stale are taken, not discarded: `(tasks ?? [])` renders "No active
  // jobs." out of a request that never returned, which is a manufactured
  // claim during exactly the outage where a wrong "nothing is running"
  // costs money. See PollErrorBanner.
  const {
    data: tasks,
    error: tasksError,
    stale: tasksStale,
    lastSuccess: tasksLastSuccess,
    refresh,
  } = usePolling(() => api.tasks(), 2000);
  // Connected instances, for the "Run on" picker (manual jobs, multi-GPU).
  const { data: instances } = usePolling(() => api.instances(), 5000);
  const connected = (instances ?? []).filter(
    (i: Instance) => i.connection_state === "connected",
  );
  const [targetInstance, setTargetInstance] = useState("");

  // "Run after" (Phase 77): parents this job waits on. Only unsettled
  // non-server jobs are offered - a dep on an already-succeeded task is
  // legal in the API but pointless to click (it is already satisfied).
  const [dependsOn, setDependsOn] = useState<string[]>([]);
  const depCandidates = (tasks ?? []).filter(
    (t) =>
      (t.status === "queued" || t.status === "running") &&
      !SERVE_TEMPLATES.includes(t.template),
  );
  function toggleDep(id: string) {
    setDependsOn((prev) =>
      prev.includes(id) ? prev.filter((d) => d !== id) : [...prev, id],
    );
  }

  // Also called by the template editor after a save/delete, so a new custom
  // template appears in the picker immediately.
  function loadTemplates() {
    api
      .templates()
      .then((r) => {
        setTemplates(r.templates);
        setTemplateErrors(r.errors);
        if (r.templates.length > 0) setSelected((v) => v || r.templates[0].name);
      })
      .catch((e) => setError(e.message));
  }

  useEffect(() => {
    loadTemplates();
    api.modelPresets().then(setPresets).catch(() => {});
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const template = templates.find((t) => t.name === selected);
  const isVllm = selected === "vllm-serve";

  // Advisory model-vs-VRAM preflight. The GPU it checks against follows the
  // launch decision: auto-manage's chosen type, else the targeted (or first
  // connected) instance. Never blocks queueing; it just warns before the
  // boot + weight-download tax is paid on a model that cannot fit.
  const [fitModel, setFitModel] = useState("");
  const [fit, setFit] = useState<ModelFit | null>(null);
  const fitInstanceType = auto.enabled
    ? auto.gpu_type
    : ((connected.find((i: Instance) => i.id === targetInstance) ??
        connected[0])?.instance_type ?? "");
  useEffect(() => {
    if (!fitModel || !fitInstanceType) {
      setFit(null);
      return;
    }
    const timer = setTimeout(() => {
      api
        .modelFit(normalizeModelId(fitModel), fitInstanceType)
        .then(setFit)
        .catch(() => setFit(null));
    }, 500);
    return () => clearTimeout(timer);
  }, [fitModel, fitInstanceType]);

  async function enqueue(values: Record<string, unknown>) {
    setSubmitting(true);
    setError("");
    setNotice("");
    try {
      if (isVllm && typeof values.model_id === "string") {
        values = { ...values, model_id: normalizeModelId(values.model_id) };
      }
      const autoConfig =
        auto.enabled && auto.gpu_type && auto.region && auto.filesystem
          ? {
              gpu_type: auto.gpu_type,
              region: auto.region,
              filesystem: auto.filesystem,
            }
          : undefined;
      if (auto.enabled && !autoConfig) {
        setError("Auto-manage needs a GPU, region, and filesystem.");
        setSubmitting(false);
        return;
      }
      const task = await api.enqueueTask(
        selected,
        values,
        autoConfig,
        !autoConfig && targetInstance ? targetInstance : undefined,
        dependsOn,
      );
      const chained = dependsOn.length
        ? `; runs after ${dependsOn.length} job${dependsOn.length === 1 ? "" : "s"}`
        : "";
      setNotice(
        autoConfig
          ? `Queued ${task.id} (${task.template}): Manifold will rent a ${autoConfig.gpu_type} for it${chained}`
          : `Queued ${task.id} (${task.template})${chained}`,
      );
      setDependsOn([]);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function clearHistory() {
    // Confirmation is load-bearing, not politeness. This deletes task_logs
    // AND the succeeded-task rows that db.task_durations() reads, so one
    // click silently flips the EstimateWidget on this same page from
    // "measured · N runs" back to "rough · no history yet". "Up to":
    // finished jobs a queued job still depends on are spared.
    if (
      !window.confirm(
        `Clear up to ${history.length} finished job(s)?\n\n` +
          `This permanently deletes their logs, and the run history that ` +
          `makes cost estimates "measured" rather than "rough".`,
      )
    )
      return;
    setClearing(true);
    setError("");
    try {
      const { cleared } = await api.clearFinishedTasks();
      setNotice(`Cleared ${cleared} finished job(s)`);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setClearing(false);
    }
  }

  async function removeTask(id: string) {
    if (
      !window.confirm(
        `Remove job ${id}?\n\nIts logs are permanently deleted, and if it ` +
          `succeeded it no longer feeds the cost estimate for its template.`,
      )
    )
      return;
    setError("");
    try {
      await api.deleteTask(id);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function cancelTask(id: string) {
    setError("");
    try {
      await api.cancelTask(id);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  const active = (tasks ?? []).filter(isActiveJob);
  const history = (tasks ?? []).filter((t) => !isActiveJob(t));

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(360px,460px)_1fr]">
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Queue a job
        </h2>
        <div className="space-y-4 rounded-lg border border-zinc-200 bg-white p-5">
          <label className="block text-xs font-medium text-zinc-600">
            Template
            <select
              className="mt-1 w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
              value={selected}
              onChange={(e) => {
                setSelected(e.target.value);
                setSeed(null);
              }}
            >
              {templates.map((t) => (
                <option key={t.name} value={t.name}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          {template && (
            <>
              {/* The launch decision is made here: what this job does and
                  what GPU it needs, in a callout right under the picker. */}
              <div className="rounded border border-zinc-200 bg-zinc-50 p-3 text-xs text-zinc-600">
                <p>{template.description}</p>
                {template.gpu?.min_vram_gib ? (
                  <p className="mt-1 font-medium text-zinc-700">
                    Needs a GPU with ≥{template.gpu.min_vram_gib} GiB VRAM.
                  </p>
                ) : null}
                {template.warnings?.map((w) => (
                  <p key={w} className="mt-1 text-amber-700">
                    Warning: {w}
                  </p>
                ))}
              </div>

              {/* Rent a GPU just for this job (launch -> run -> sync ->
                  terminate), or leave off to run on a connected instance. */}
              <AutoManageControls value={auto} onChange={setAuto} />

              {/* Manual jobs: which running instance takes this job. With
                  one instance this is informational; with several it is the
                  multi-GPU router. */}
              {!auto.enabled && connected.length > 0 && (
                <label className="block text-xs font-medium text-zinc-600">
                  Run on
                  <select
                    className="mt-1 w-full min-w-0 rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
                    value={targetInstance}
                    onChange={(e) => setTargetInstance(e.target.value)}
                  >
                    <option value="">
                      first free instance ({connected.length} connected)
                    </option>
                    {connected.map((i: Instance) => (
                      <option key={i.id} value={i.id}>
                        {i.name} · {i.gpu_description || i.instance_type} ·{" "}
                        {i.region}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {/* Gated on instances != null: before /instances has answered
                  (or if it never does), "no instance is connected" is a
                  guess - and one that steers toward renting a SECOND GPU
                  while the first may be running fine. */}
              {!auto.enabled && instances != null && connected.length === 0 && (
                <p className="rounded border border-zinc-200 bg-zinc-100 px-3 py-2 text-xs text-zinc-500">
                  No instance is connected: this job will wait in the queue
                  until one is running (launch one on Instances), or turn on
                  auto-manage above to rent a GPU just for it.
                </p>
              )}

              {/* Run after (Phase 77): chain this job behind others. It
                  stays queued until every checked job succeeds, and settles
                  as "skipped" if one of them fails. For auto-manage jobs the
                  GPU is not rented until the parents finish. */}
              {depCandidates.length > 0 && (
                <div className="rounded border border-zinc-200 bg-zinc-50 p-3">
                  <p className="text-xs font-medium text-zinc-600">
                    Run after
                  </p>
                  <p className="mt-0.5 text-[11px] text-zinc-500">
                    Waits until every checked job succeeds; if one fails,
                    this job is skipped instead of run.
                    {auto.enabled &&
                      " The GPU is not rented until they finish."}
                  </p>
                  <div className="mt-2 space-y-1">
                    {depCandidates.map((t) => (
                      <label
                        key={t.id}
                        className="flex items-center gap-2 text-xs text-zinc-700"
                      >
                        <input
                          type="checkbox"
                          checked={dependsOn.includes(t.id)}
                          onChange={() => toggleDep(t.id)}
                          className="rounded border-zinc-300"
                        />
                        <span className="font-medium">{t.template}</span>
                        <span className="font-mono text-zinc-400">{t.id}</span>
                        <StatusBadge status={t.status} />
                      </label>
                    ))}
                  </div>
                </div>
              )}

              {/* Advisory pre-launch estimate: what a run of this template is
                  likely to cost. When auto-manage is on it follows that GPU. */}
              <EstimateWidget
                template={template.name}
                instanceType={
                  auto.enabled && auto.gpu_type ? auto.gpu_type : undefined
                }
              />

              {isVllm && presets.length > 0 && (
                <div className="mb-4 rounded border border-zinc-100 bg-zinc-50 p-2.5">
                  <p className="mb-2 text-xs font-medium text-zinc-600">
                    Presets (click to fill · ungated, no token needed)
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {presets.map((p) => (
                      <button
                        key={p.model_id}
                        type="button"
                        title={`${p.model_id}: ${p.note}`}
                        onClick={() => setSeed({ model_id: p.model_id, parameters: p.parameters })}
                        className={`rounded border px-2 py-1 text-left text-xs hover:bg-white ${
                          seed?.model_id === p.model_id
                            ? "border-zinc-900 bg-white"
                            : "border-zinc-300 bg-zinc-50"
                        }`}
                      >
                        <span className="font-medium text-zinc-800">
                          {p.label}
                        </span>
                        <span className="ml-1.5 text-zinc-400">{p.tier}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {fit && (fit.verdict === "no" || fit.verdict === "tight") && (
                <p
                  className={`rounded border px-3 py-2 text-xs ${
                    fit.verdict === "no"
                      ? "border-red-200 bg-red-50 text-red-700"
                      : "border-amber-200 bg-amber-50 text-amber-700"
                  }`}
                >
                  {fit.note} Estimated from the model name, so treat it as a
                  sanity check, not a guarantee.
                </p>
              )}

              <ParameterForm
                key={`${template.name}:${seed?.model_id ?? ""}`}
                template={template}
                onSubmit={enqueue}
                submitting={submitting}
                onModelChange={setFitModel}
                initialValues={
                  isVllm && seed
                    ? { model_id: seed.model_id, ...seed.parameters }
                    : undefined
                }
              />
            </>
          )}
          {notice && <p className="mt-3 text-xs text-emerald-700">{notice}</p>}
          {error && <p className="mt-3 text-xs text-red-700">{error}</p>}
          {Object.entries(templateErrors).map(([file, message]) => (
            <p key={file} className="mt-3 text-xs text-amber-700">
              {file}: {message}
            </p>
          ))}
        </div>

        <div className="mt-6">
          <TemplateEditor templates={templates} onChanged={loadTemplates} />
        </div>

        {/* The config a fine-tune reads, drafted from a plain-words spec
            (Phase 84). It sits beside the queue because the config is what
            axolotl-finetune above takes as its config_path. */}
        <div className="mt-6">
          <DistillConfigPanel connected={connected} />
          <OwnItLocallyPanel connected={connected} />
        </div>
      </section>

      <section className="min-w-0 space-y-6">
        <PollErrorBanner
          error={tasksError}
          stale={tasksStale}
          lastSuccess={tasksLastSuccess}
          what="job list"
        />
        {/* Stale = the poll is failing but old data exists: grey it and block
            interaction, same as the home page's instance cards, so a
            snapshot cannot be cancelled/removed as if it were live. */}
        <div
          className={`space-y-6 ${tasksStale ? "pointer-events-none opacity-40" : ""}`}
        >
        <div>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
            Active {active.length > 0 && `(${active.length})`}
          </h2>
          <div className="space-y-3">
            {active.map((t) => (
              <TaskCard
                key={t.id}
                task={t}
                onRemove={removeTask}
                onCancel={cancelTask}
              />
            ))}
            {/* "No active jobs." only once the list has actually loaded:
                before that it is not a fact, it is a hope. */}
            {active.length === 0 && tasks != null && (
              <p className="rounded-lg border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500">
                No active jobs.
              </p>
            )}
          </div>
        </div>

        <div>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
              History {history.length > 0 && `(${history.length})`}
            </h2>
            {history.length > 0 && (
              <button
                onClick={clearHistory}
                disabled={clearing}
                className="rounded border border-zinc-300 px-2 py-1 text-xs text-zinc-600 hover:bg-zinc-50 disabled:opacity-50"
              >
                {clearing ? "Clearing..." : "Clear history"}
              </button>
            )}
          </div>
          <div className="space-y-3">
            {history.map((t) => (
              <TaskCard
                key={t.id}
                task={t}
                onRemove={removeTask}
                onCancel={cancelTask}
              />
            ))}
            {history.length === 0 && tasks != null && (
              <p className="rounded-lg border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500">
                No finished jobs yet.
              </p>
            )}
          </div>
        </div>
        </div>
      </section>
    </div>
  );
}

const CANCELLABLE = ["queued", "waiting", "launching", "ready"];

function TaskCard({
  task,
  onRemove,
  onCancel,
}: {
  task: Task;
  onRemove: (id: string) => void;
  onCancel: (id: string) => void;
}) {
  const [showLogs, setShowLogs] = useState(false);
  const [failTail, setFailTail] = useState<string[] | null>(null);

  // A running serve job is reachable at the local OpenAI proxy; ServeStatus
  // (below) probes readiness and renders the chip + terminal button.
  const servedModel =
    task.status === "running" &&
    (task.template === "vllm-serve" || task.template === "sglang-serve") &&
    typeof task.parameters?.model_id === "string"
      ? (task.parameters.model_id as string)
      : "";
  const instanceId = task.instance_id;

  // Stopping a job on purpose is not a failure. The backend deliberately
  // writes error="cancelled by user" so the card would NOT show "a baffling
  // container exited 137" (dispatcher.py) - but its completion funnel still
  // settles the row as status="failed" with the container's real exit code,
  // so without this gate a click on Stop produced a red failed badge, a red
  // error line, an auto-opened post-mortem, and "exit 137".
  const wasCancelled =
    task.status === "failed" && task.error === "cancelled by user";

  const auto = task.auto_manage;
  const lc = task.lifecycle;
  // In-flight auto-managed jobs must not be removed (their instance is still
  // being managed); they can be cancelled instead while pre-run.
  const inFlightAuto = auto && !!lc && !TERMINAL_LIFECYCLE.includes(lc);
  // Any job that has not settled can be stopped: queued jobs settle as
  // cancelled; running jobs (servers included, which never exit on their
  // own) get their container stopped on the instance.
  const canCancel = auto
    ? !!lc && (CANCELLABLE.includes(lc) || lc === "running")
    : task.status === "queued" || task.status === "running";

  // A failed job must show WHY inline, not just "exit -1": pull the last 10
  // lines of its retained log automatically. Not for a cancel - the user
  // stopped it; there is no mystery to post-mortem.
  useEffect(() => {
    if (task.status !== "failed" || task.error === "cancelled by user") return;
    let cancelled = false;
    api
      .taskLogs(task.id, 10)
      .then((l) => {
        if (!cancelled) setFailTail(l.map((x) => x.line));
      })
      .catch(() => {
        if (!cancelled) setFailTail([]);
      });
    return () => {
      cancelled = true;
    };
  }, [task.id, task.status, task.error]);

  const finished = task.status !== "running" && task.status !== "queued";

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-4">
      {/* flex-wrap + min-w-0/shrink-0: this row packs badges, id, cost,
          buttons - wider than the column at every real window size. Without
          wrap the labels broke mid-word (the pattern to copy is the run
          card header on the Autopilot page). */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          {wasCancelled ? (
            <Badge label="cancelled" tone="zinc" />
          ) : (
            <StatusBadge status={task.status} />
          )}
          {auto && (
            <span
              className="rounded bg-sky-100 px-1.5 py-0.5 text-[11px] font-medium text-sky-800"
              title="Manifold rents and tears down a GPU just for this job"
            >
              auto-manage
            </span>
          )}
          <span className="text-sm font-medium">{task.template}</span>
          <span className="font-mono text-xs text-zinc-400">{task.id}</span>
          {task.created_by && (
            <span
              className="rounded bg-zinc-100 px-1.5 py-0.5 text-[11px] text-zinc-500"
              title="The API principal that enqueued this job (Phase 79). Older jobs predate attribution and show nothing."
            >
              by {task.created_by}
            </span>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-3 text-xs text-zinc-500">
          {finished && task.runtime_seconds !== null && (
            <span
              className="font-mono text-zinc-400"
              title={
                task.actual_cost_cents !== null
                  ? "What this job actually cost: run time at the instance's hourly rate. Compare against the pre-launch estimate."
                  : "Run time. No cost shown: this instance was not launched by Manifold, so its rate is unknown."
              }
            >
              {formatDuration(task.runtime_seconds)}
              {task.actual_cost_cents !== null &&
                ` · $${(task.actual_cost_cents / 100).toFixed(2)}`}
            </span>
          )}
          {/* No exit code on a cancel: 137 is just what SIGKILL looks like,
              and rendering it red re-frames the user's own click as a
              crash. The code is still in Logs for anyone who wants it. */}
          {task.exit_code !== null && finished && !wasCancelled && (
            <span
              className={`font-mono ${
                task.exit_code === 0 ? "text-zinc-400" : "text-red-600"
              }`}
              title="Container exit code (the honest signal; see Logs)"
            >
              exit {task.exit_code}
            </span>
          )}
          <span>{formatDate(task.created_at)}</span>
          {servedModel && instanceId && (
            <ServeStatus
              model={servedModel}
              instanceId={instanceId}
              taskId={task.id}
            />
          )}
          <button
            onClick={() => setShowLogs((s) => !s)}
            className="rounded border border-zinc-300 px-2 py-0.5 hover:bg-zinc-50"
          >
            {showLogs ? "Hide logs" : "Logs"}
          </button>
          {canCancel && (
            <button
              onClick={() => onCancel(task.id)}
              title={
                task.status === "running"
                  ? "Stop the container on the instance"
                  : "Cancel and tear down any instance it launched"
              }
              className="rounded border border-amber-200 px-2 py-0.5 text-amber-700 hover:bg-amber-50"
            >
              {task.status === "running" ? "Stop" : "Cancel"}
            </button>
          )}
          {task.status !== "running" && !inFlightAuto && (
            <button
              onClick={() => onRemove(task.id)}
              title="Remove from history"
              className="rounded border border-zinc-200 px-2 py-0.5 text-zinc-400 hover:bg-red-50 hover:text-red-600"
            >
              Remove
            </button>
          )}
        </div>
      </div>

      {auto && <LifecyclePipeline task={task} />}

      {/* Dependency chips: which jobs this one runs after, with each edge's
          live status. The queued card explains its own wait. */}
      {(task.deps ?? []).length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
          <span className="text-zinc-400">after:</span>
          {(task.deps ?? []).map((dep) => (
            <span
              key={dep.id}
              className="inline-flex items-center gap-1.5 rounded border border-zinc-200 bg-zinc-50 px-1.5 py-0.5"
              title={
                dep.status === "missing"
                  ? "This dependency's record was removed."
                  : `Runs after ${dep.template} (${dep.id}) succeeds.`
              }
            >
              <span className="font-medium text-zinc-700">
                {dep.template ?? "(removed)"}
              </span>
              <span className="font-mono text-zinc-400">{dep.id}</span>
              <StatusBadge status={dep.status} />
            </span>
          ))}
        </div>
      )}

      {Object.keys(task.parameters).length > 0 && (
        /* break-all: model ids and paths have no spaces to wrap at. */
        <p className="mt-1 break-all font-mono text-xs text-zinc-500">
          {Object.entries(task.parameters)
            .map(([k, v]) => `${k}=${v}`)
            .join("  ")}
        </p>
      )}
      {task.error && (
        /* A skipped job's "error" is an explanation, not a failure of this
           job: render it calm, not red. The red belongs on the parent.
           Same for a cancel: the user did it; red would call it a fault. */
        <p
          className={`mt-2 text-xs ${
            task.status === "skipped" || wasCancelled
              ? "text-zinc-500"
              : "text-red-700"
          }`}
        >
          {task.error}
        </p>
      )}
      {task.status === "failed" && !wasCancelled && failTail !== null && (
        <div className="mt-2">
          <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-zinc-400">
            Last log lines
          </p>
          {failTail.length > 0 ? (
            <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded bg-zinc-950 p-2.5 font-mono text-[11px] leading-relaxed text-zinc-800">
              {failTail.join("\n")}
            </pre>
          ) : (
            <p className="text-xs text-zinc-500">
              No log output was captured for this job.
            </p>
          )}
        </div>
      )}
      {task.status === "succeeded" && task.output_paths.length > 0 && (
        <p className="mt-2 text-xs text-zinc-500">
          Outputs:{" "}
          <span className="font-mono">{task.output_paths.join(", ")}</span>
        </p>
      )}

      {showLogs && (
        <TaskLogs
          taskId={task.id}
          live={task.status === "running" || task.status === "queued"}
        />
      )}
    </div>
  );
}

// The log tail, as its own component so it only exists while the panel is
// open. This replaces a raw setInterval(load, 1500) with no in-flight guard
// and no document.hidden check - the exact pattern usePolling was written
// to end (a 400-line payload every 1.5s piling up behind a 30s timeout is
// the freeze recipe). Mounting on expand also gives us usePolling's
// immediate first tick, so logs appear at once rather than an interval
// later. A finished job still gets that first fetch; the absurd interval
// just never fires meaningfully again.
function TaskLogs({ taskId, live }: { taskId: string; live: boolean }) {
  const { data, error } = usePolling(
    () => api.taskLogs(taskId, 400),
    live ? 1500 : 3_600_000,
  );
  const lines = data === null ? null : data.map((x) => x.line);
  return (
    /* pre-wrap + break-words: long docker/pip lines wrap instead of
       forcing the whole card into horizontal scroll; height stays capped. */
    <pre className="mt-3 max-h-72 overflow-y-auto whitespace-pre-wrap break-words rounded bg-zinc-950 p-3 font-mono text-xs leading-relaxed text-zinc-800">
      {lines === null
        ? // Not "(no output yet)": that is a claim the job produced nothing,
          // and before the first fetch lands (or if it fails) we do not know.
          error
          ? `(logs unavailable: ${error})`
          : "(loading logs...)"
        : lines.length > 0
          ? lines.join("\n")
          : "(no output yet)"}
    </pre>
  );
}

// Readiness chip + "Open in terminal" for a running serve job. Its own
// component for the same reason as TaskLogs: it polls, so it goes through
// usePolling, and it only exists while there is a served model to probe.
// "running" only means the container is up; the model API answers minutes
// later, once the weights finish downloading and loading.
function ServeStatus({
  model,
  instanceId,
  taskId,
}: {
  model: string;
  instanceId: string;
  taskId: string;
}) {
  const { openModelShell } = useTerminalDock();
  const { data } = usePolling(() => api.modelStatus(instanceId), 5000);
  // The endpoint reports the ONE serving task on the instance; only trust
  // its verdict when it is reporting on THIS task's card. A failed probe
  // (data null) reads as "loading", never as "ready".
  const mine = data != null && data.serving && data.task_id === taskId;
  const ready = mine && data.ready;
  const detail = (mine && data.status_detail) || "";
  return (
    <>
      <span
        className={`flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium ${
          ready
            ? "bg-emerald-100 text-emerald-800"
            : "bg-amber-100 text-amber-800"
        }`}
        title={
          ready
            ? `${model} is answering. The terminal button and in-instance chat are wired to it.`
            : `${model} is still starting on the GPU (downloading and loading the weights). The terminal button unlocks once it answers.${
                detail ? ` Last probe: ${detail}.` : ""
              }`
        }
      >
        <span
          className={`h-1.5 w-1.5 rounded-full ${
            ready ? "bg-emerald-500" : "bg-amber-500 motion-safe:animate-pulse"
          }`}
        />
        {ready ? "model ready" : "model loading"}
      </span>
      <button
        onClick={() => ready && openModelShell(model)}
        disabled={!ready}
        title={
          ready
            ? `Open a local shell wired to ${model}: OPENAI_BASE_URL points at the proxy, so any OpenAI-compatible CLI talks to this model`
            : `${model} is still loading. This unlocks once the model answers, so the CLI will not error the moment it connects.`
        }
        className={`rounded border px-2 py-0.5 ${
          ready
            ? "border-teal-300 text-teal-700 hover:bg-teal-50"
            : "cursor-not-allowed border-zinc-200 text-zinc-400"
        }`}
      >
        Open in terminal
      </button>
    </>
  );
}
