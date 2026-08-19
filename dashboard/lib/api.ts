// Typed client for the local Manifold backend. The dashboard is a thin
// consumer: no business logic here, just fetch + types + error surfacing.

import { API_BASE } from "./backend";
import { authHeaders, notifyUnauthorized } from "./token";

export class ApiError extends Error {
  status: number;
  body?: Record<string, unknown>;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

// Requests that ride the SSH connection to an instance (sidecar calls,
// file listings) can be slow when the instance is struggling; a timeout
// turns a silent hang into an honest error that names the real culprit.
const DEFAULT_TIMEOUT_MS = 30_000;

async function request<T>(
  path: string,
  init?: RequestInit & { timeoutMs?: number },
): Promise<T> {
  const timeoutMs = init?.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: ctrl.signal,
      headers: {
        "content-type": "application/json",
        ...authHeaders(),
        ...init?.headers,
      },
    });
  } catch {
    if (ctrl.signal.aborted) {
      // The backend accepted the connection but did not answer in time:
      // usually the instance/sidecar side of the call, not the backend.
      throw new ApiError(
        0,
        `No answer after ${Math.round(timeoutMs / 1000)}s (${path}). ` +
          "The backend is likely up but the instance or its sidecar is " +
          "slow or unreachable.",
      );
    }
    // Name the address it actually tried, not a hardcoded ":8000": with
    // NEXT_PUBLIC_API_URL set, or served same-origin by the desktop app,
    // this message pointed at a port nothing was using.
    throw new ApiError(
      0,
      `Backend unreachable at ${API_BASE || window.location.origin}. ` +
        "Is the Manifold app running?",
    );
  } finally {
    clearTimeout(timer);
  }
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    // 401 means the backend enforces its API token and this browser does
    // not hold it (or holds a stale one): raise the TokenGate.
    if (resp.status === 401) notifyUnauthorized();
    // FastAPI answers a request-model violation with detail as an ARRAY of
    // {loc, msg, type} objects, and handing that straight to ApiError put a
    // stringified array on screen ("[object Object]" territory) instead of a
    // sentence. Every route that takes a body can produce this shape, so it
    // is flattened here rather than at one call site. Found by the Phase 84
    // verifier on 2026-08-14.
    const err = new ApiError(resp.status, detailToMessage(body.detail, resp.status));
    err.body = body;
    throw err;
  }
  return body as T;
}

// A FastAPI error detail is either a plain string (everything Manifold
// raises by hand) or a validation array. Both have to read as one sentence.
function detailToMessage(detail: unknown, status: number): string {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => {
      const item = d as { loc?: unknown[]; msg?: string };
      // loc is ["body", "<field>"]; the field name is what the user needs.
      const field = Array.isArray(item.loc)
        ? item.loc.filter((x) => x !== "body").join(".")
        : "";
      const msg = item.msg ?? "is invalid";
      return field ? `${field}: ${msg}` : msg;
    });
    if (parts.length) return parts.join("; ");
  }
  return `HTTP ${status}`;
}

export type UnpersistedFile = {
  path: string;
  size_bytes: number;
  modified: string;
};

export type InstanceTypeInfo = {
  description: string;
  gpu_description: string;
  price_usd_per_hour: number;
  // Present when the price is a dated list price rather than a live meter
  // (GCP v1). The string IS the honesty label; show it near the number.
  price_basis?: string;
  // The region price_basis is quoted in ("us-central1"), as a field so no
  // screen has to parse it out of that sentence. GCP only.
  price_basis_region?: string;
  // The provider quota metric a launch of this shape is gated on
  // ("NVIDIA_T4_GPUS"). GCP only, and absent for a shape whose gating
  // metric is unknown - never guessed from the GPU name.
  quota_metric?: string;
  specs: { vcpus: number; memory_gib: number; storage_gib: number; gpus: number };
  regions_with_capacity: string[];
};

export type Region = { code: string; name: string };

// One provider quota row, verbatim: `scope` is "global" or the region
// Google reported it for, and limit/usage are Google's own numbers.
export type GcpQuotaRow = {
  metric: string;
  limit: number;
  usage: number;
  scope: string;
};

export type GcpQuota = {
  quotas: GcpQuotaRow[];
  project: string;
  // Absent when the backend has no project id to name in the link. A quota
  // console URL without one opens whichever project the browser used last,
  // which is not the project these rows describe.
  request_url?: string;
  mock?: boolean;
};

export type ModelPreset = {
  label: string;
  model_id: string;
  vram_gib: number;
  tier: string;
  note: string;
  // Extra vllm-serve parameters the preset needs (e.g. tensor_parallel: 8
  // for models that shard across a whole 8-GPU cluster).
  parameters?: Record<string, unknown>;
};

export type SidecarDiagnosis = {
  cause: string;
  summary: string;
  port: number;
  checks: { label: string; command: string; output: string }[];
};

export type Filesystem = {
  name: string;
  region: string;
  mount_point: string;
  is_in_use: boolean;
  bytes_used: number;
};

export type Instance = {
  id: string;
  provider?: string;
  name: string;
  status: string;
  ip: string | null;
  region: string;
  instance_type: string;
  gpu_description: string;
  hourly_rate_usd: number;
  filesystems: string[];
  connection_mode: string | null;
  connection_state: string;
  connection_error: string;
  launch_id: string | null;
  idle: {
    idle_seconds: number;
    timeout_seconds: number;
    keep_alive: boolean;
  } | null;
  // The max-lifetime ceiling. Deliberately NOT inside `idle`, which is null
  // whenever the instance is not connected — a box that has dropped off SSH
  // past its ceiling is exactly the one whose limit needs showing.
  // All three are null when no ceiling is set (the default).
  max_lifetime_seconds: number | null;
  ceiling_seconds_remaining: number | null;
  // The ACTIVE-anchored ceiling (Phase 97): the bound on run time, boot
  // excluded. active_seconds_remaining is null until the box is active -
  // "no clock yet" is a different fact from "0 seconds left".
  max_active_seconds: number | null;
  active_seconds_remaining: number | null;
  // Why the ceiling fired without terminating: a running batch job, an
  // auto-managed teardown, or an unreachable instance.
  ceiling_deferred_by: string | null;
  // WHOSE box, and WHAT FOR. Several agents share one account and this list
  // was their only view of it; an instance another session was mid-way
  // through using read as "unexplained" and got terminated. Both are null for
  // an adopted box and for anything launched before they shipped — rendered
  // as unattributed, never guessed.
  created_by: string | null;
  purpose: string | null;
  // The idle sweep's own verdict. `busy` is the FACTUAL question (is work
  // loaded and running here), and is null — never false — when the sweep
  // could not tell. "No evidence of work" is not "evidence of no work", and
  // conflating them is what destroyed a model server that was still loading.
  activity: {
    state:
      | "up"
      | "loading"
      | "serving"
      | "gpu_busy"
      | "batch_running"
      | "auto_managed"
      | "keep_alive"
      | "idle_countdown"
      | "booting"
      | "unreachable"
      | "unknown";
    busy: boolean | null;
    reason: string;
    age_seconds: number | null;
  };
  // The last GPU sample the dispatcher recorded (every ~30s), served from
  // SQLite. null when this box has never been sampled — not a row of zeroes,
  // because "never measured" and "measured, idle" are different facts. `at`
  // is here so a stale reading is never drawn as a live one.
  telemetry: {
    at: string;
    gpu_name: string | null;
    vram_used_mib: number | null;
    vram_total_mib: number | null;
    // Busiest card on the box. util_pct_mean is the average across cards.
    util_pct: number | null;
    util_pct_mean: number | null;
    gpu_count: number | null;
  } | null;
  // The launch's bootstrap script, if it was given one AND has started.
  // Absent means either no script or not started yet - the field appears
  // the moment there is something real to say, and never guesses a state.
  // A nonzero exit never terminates the box; it is reported and left alone.
  bootstrap?: {
    state: "running" | "exited" | "vanished" | "unreachable";
    // Present only for "exited". Absent is not zero.
    exit_code?: number;
  };
};

export type Launch = {
  id: string;
  provider?: string;
  created_at: string;
  requested_type: string;
  launched_type: string | null;
  region: string;
  filesystem: string | null;
  connection_mode: string;
  hourly_rate_cents: number | null;
  status: string;
  attempts: number;
  error: string | null;
  lambda_instance_id: string | null;
  launched_at: string | null;
  // Phase 110: the first moment SSH answered. Not the same fact as
  // active_at - a stock-Ubuntu GCE box is reachable minutes before its
  // drivers and runtime are installed. Null means we never saw it connect.
  connected_at?: string | null;
  active_at: string | null;
  terminated_at: string | null;
  // Structured progress (backend-computed; see launch_progress).
  phase?: string;
  phase_detail?: string;
  settled?: boolean;
  // The boot countdown, present only while the instance is still coming up
  // on the provider. Once connected_at is set that deadline is past and
  // these stop being sent; phase_detail carries the setup step instead.
  boot_elapsed_seconds?: number;
  boot_timeout_seconds?: number;
  boot_remaining_seconds?: number;
  // Phase 79: the principal this launch is attributed to. Null on rows
  // from before attribution existed - shown as unattributed, not guessed.
  created_by?: string | null;
};

// Phase 79: a named API credential. The token value exists only in the
// mint response; list rows carry liveness, never secrets.
// Phase 80: each carries a role - viewer observes, operator works,
// admin governs. The .env token is always admin.
export type PrincipalRole = "viewer" | "operator" | "admin";

// Phase 82: one block of launch-policy rules; empty lists / zeros mean
// no opinion.
export type PolicyRules = {
  allowed_instance_types: string[];
  allowed_regions: string[];
  max_hourly_rate_usd: number;
  require_max_lifetime: boolean;
};

export type PolicyDoc = {
  active: boolean;
  source: string;
  launch: PolicyRules;
  roles: Record<string, PolicyRules>;
};

export type Principal = {
  id: string;
  name: string;
  role: PrincipalRole;
  created_at: string;
  created_by: string;
  last_used_at: string | null;
  revoked_at: string | null;
  // Phase 81: enforced hourly ceiling on this principal's attributed
  // burn. null = unlimited. A rate guard, not the advisory wallet.
  max_hourly_spend_usd: number | null;
};

export type StoredFile = {
  key: string;
  size_bytes: number;
  last_modified: string;
};

export type LaunchRequest = {
  provider?: string;
  instance_type: string;
  region: string;
  filesystem: string;
  // More filesystems to mount beside the primary one, for a run that reads
  // one dataset and writes another. Attach-only: jobs still mount the
  // primary, extras are reached at /lambda/nfs/<name>. Same region as the
  // launch, max 4; the backend enforces both.
  extra_filesystems?: string[];
  connection_mode: string;
  ssh_key_name?: string;
  name?: string;
  idle_timeout_seconds?: number;
  // Hard ceiling on total lifetime, from launch acceptance (boot included).
  // Omit for no ceiling, which is the default.
  max_lifetime_seconds?: number;
  // Ceiling on ACTIVE time, anchored at health-check pass: budget the run,
  // not the boot. The absolute ceiling above remains the outer bound.
  max_active_seconds?: number;
  // What this box is FOR, shown to every agent and page that lists
  // instances. An unattributed box is how someone else's loading model got
  // terminated as a stray.
  purpose?: string;
  // A setup script the instance runs once when it comes up. Omit for none.
  bootstrap?: string;
};

export type TemplateParameter = {
  name: string;
  type: "string" | "integer" | "number" | "boolean";
  description: string;
  default: string | number | boolean | null;
  required: boolean;
};

export type Template = {
  name: string;
  description: string;
  image: string;
  command: string;
  parameters: TemplateParameter[];
  gpu: { min_vram_gib?: number; recommended_types?: string[] };
  // Non-fatal advisories (e.g. a floating image tag that may drift).
  warnings?: string[];
  // User-authored template (editable/deletable); yaml is its raw source.
  custom?: boolean;
  yaml?: string;
  // Pinned by the user; the backend already sorts favorites first.
  favorite?: boolean;
};

export type Lifecycle =
  | "queued"
  | "waiting"
  | "launching"
  | "ready"
  | "running"
  | "syncing"
  | "terminating"
  | "done"
  | "failed"
  | "cancelled"
  | "skipped";

// A dependency edge, resolved by the backend so cards can render chips
// without re-joining. status "missing" = the parent row was deleted.
export type TaskDep = {
  id: string;
  template: string | null;
  status: Task["status"] | "missing";
};

export type Task = {
  id: string;
  created_at: string;
  template: string;
  parameters: Record<string, unknown>;
  // "skipped": never ran and never will - a task it depends on did not
  // succeed. Distinct from "failed" (which means the job ran and broke).
  status: "queued" | "running" | "succeeded" | "failed" | "skipped";
  instance_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  error: string | null;
  output_paths: string[];
  // Auto-manage (Phase 24): Manifold owns this job's instance lifecycle.
  auto_manage: boolean;
  gpu_type: string | null;
  region: string | null;
  filesystem: string | null;
  launch_id: string | null;
  target_instance_id: string | null;
  lifecycle: Lifecycle | null;
  lifecycle_detail: string | null;
  lifecycle_events: Record<string, string>;
  launch_to_ready_seconds: number | null;
  // Set on finished tasks: wall time and its cost at the launch's hourly
  // rate (null on adopted instances where the rate is unknown).
  runtime_seconds: number | null;
  actual_cost_cents: number | null;
  // Phase 77: task ids this job runs after, plus the backend's resolution
  // of each edge. deps is absent on some payloads (single-task fetch older
  // than the list call) - treat undefined as [].
  depends_on: string[];
  deps?: TaskDep[];
  // Phase 79: who enqueued this job (null on pre-attribution rows).
  created_by?: string | null;
};

export type AutoManageConfig = {
  gpu_type: string;
  region: string;
  filesystem: string;
};

export type Watch = {
  id: string;
  created_at: string;
  instance_type: string;
  region: string;
  filesystem: string | null;
  auto_launch: number;
  status: "watching" | "available" | "launched" | "cancelled";
  last_checked: string | null;
  triggered_at: string | null;
};

export type Estimate = {
  template: string;
  instance_type: string;
  minutes: number | null;
  cost_usd: number | null;
  confidence: "measured" | "rough" | "none";
  basis: string;
  sample_size: number;
};

export type ModelFit = {
  model: string;
  instance_type: string;
  params_b: number | null;
  vram_gb: number | null;
  est_weights_gb: number | null;
  verdict: "fits" | "tight" | "no" | "unknown";
  note: string;
  basis: string;
};

export type Utilization = {
  available: boolean;
  reason?: string;
  gpu_description?: string;
  runtime_seconds?: number | null;
  peak_vram_used_mib?: number;
  vram_total_mib?: number;
  avg_util_pct?: number;
  sample_count?: number;
  right_size_hint?: boolean;
  verdict?: string;
  hint?: string;
};

// Spend accounting, computed entirely by the backend (backend/app/spend.py).
// The dashboard never re-derives cost. Every launch resolves to one of six
// states there, and the two that cannot be priced arrive here separately:
// `unresolved` as a count plus a range (the instance stopped at a time nobody
// observed), `rate_unknown_count` as a count (duration known, price not).
// Neither is ever folded into a total, because a fabricated $0 or a
// confident-looking point cost is the one thing a spend page must not show.
export type SpendSummary = {
  today_usd: number;
  week_usd: number;
  month_to_date_usd: number;
  all_time_usd: number;
  live_burn_usd_per_hour: number;
  unresolved: {
    count: number;
    usd_low: number;
    usd_high: number;
    launch_ids: string[];
  };
  // Alive on the cloud behind a launch row that reads as failed: money
  // burning right now that nothing in Manifold is going to stop.
  orphaned: { count: number; launch_ids: string[] };
  rate_unknown_count: number;
  // Totals only cover launches Manifold started; instances adopted from the
  // Lambda console have no launch row and no cost here.
  lower_bound: boolean;
  timezone_offset_minutes: number;
  timezone_label: string;
  disclaimer: string;
  mock: boolean;
  budget: BudgetStatus;
};

// The monthly wallet. Advisory: it never blocks a launch, because
// month_to_date only counts launches Manifold started and refusing work on
// a number we know is short would cost you a launch without saving money.
export type BudgetStatus = {
  state: "unset" | "ok" | "warn" | "over";
  monthly_budget_usd: number;
  month_to_date_usd: number;
  // All null when state is "unset".
  remaining_usd: number | null;
  used_pct: number | null;
  // Both read "at the CURRENT burn rate", not a forecast of what you might
  // launch next. exhausted_on is null when the cap is not reached this month.
  projected_month_end_usd: number | null;
  exhausted_on: string | null;
  hours_left_in_month: number | null;
};

export type SpendBucket = {
  bucket: string;        // "2026-08-11" | "2026-W32" | "2026-08"
  start_iso: string;
  usd: number;
  seconds: number;
  launches: number;
};

export type SpendBreakdownRow = {
  key: string;
  usd: number;
  seconds: number;
  count: number;
};

export type Brain = {
  ref: string; // "instance:<id>" | "local:<endpoint>/<model>" | "api:<name>"
  kind: "instance" | "local" | "api" | "cli";
  label: string;
  model: string;
  detail: string;
  ready: boolean;
};

// Phase 84: a draft training config, written by a brain from a plain-words
// spec and checked by the backend before it comes back. It is text for the
// user to read: Manifold never saves it and never trains from it.
// These field names are the BACKEND's, verbatim (main.py returns
// {"config": {...}} and distill.validate_config builds the inner object).
// They were invented here once - config_yaml/student/notes - and every one
// of them read as undefined at runtime, so the YAML pane rendered empty and
// Copy copied "undefined" while the tests stayed green. Found by the Phase
// 84 verifier on 2026-08-14; if these ever drift again, the panel goes
// blank, not red.
export type DistillConfig = {
  yaml: string;
  // The base model the brain settled on (its own pick when the user left
  // the student field empty).
  base_model: string;
  dataset_path: string;
  output_dir: string;
  // Things worth knowing that are not refusals (a missing val_set_size, a
  // base the training template does not mount).
  advisories: string[];
  brain: string;
  suggested_path: string;
};

// Phase 85: a model that lives on THIS machine, in DATA_ROOT/models.
export type LocalModel = {
  name: string;
  path: string;
  size_bytes: number;
  suggested_ollama_name: string;
  installed: boolean;
};

export type LocalModelLibrary = {
  models: LocalModel[];
  library_path: string;
  ollama_available: boolean;
  ollama_models: string[];
};

// Phase 86: one rung of the hardware ladder. Numbers are the provider's
// (or arithmetic on them, with the formula in fits_basis); prose is the
// backend's curated teaching layer.
export type GpuRung = {
  instance_type: string;
  label: string;
  family: string;
  era: string;
  gpu_count: number;
  vram_per_gpu_gib: number | null;
  vram_total_gib: number | null;
  price_usd_per_hour: number;
  regions_with_capacity: string[];
  available_now: boolean;
  price_per_gib_hour: number | null;
  good_for: string;
  step_up_when: string;
  note: string;
  fits: {
    serve_fp16_b: number;
    serve_4bit_b: number;
    lora_bf16_b: number;
    qlora_4bit_b: number;
  } | null;
};

export type GpuGuide = { rungs: GpuRung[]; fits_basis: string };

export type StudentPreset = {
  model_id: string;
  label: string;
  params_b: number;
  vram_gib: number;
  tier: string;
  license: string;
  note: string;
};

export type Approval = {
  id: string;
  run_id: string;
  run_goal: string | null;
  seq: number;
  action: string;
  args: Record<string, unknown>;
  status: "pending" | "approved" | "denied" | "expired";
  created_at: string;
};

export type AgentRun = {
  id: string;
  created_at: string;
  goal: string;
  brain_instance_id: string;
  brain_model: string | null;
  status: "running" | "succeeded" | "failed" | "cancelled" | "exhausted";
  max_steps: number;
  steps_taken: number;
  summary: string | null;
  error: string | null;
  finished_at: string | null;
  // Which actions this run pauses on (frozen when the run started).
  approval_policy: GateableAction[];
  // What the run actually DID, derived by the backend from its own steps
  // (agent.run_effect). "no_effect" means every action it completed was a
  // read: a summary from such a run is the model talking, not a result.
  effect?: "acted" | "no_effect";
  launched?: boolean;
  terminated?: boolean;
};

export type GateableAction = "launch_gpu" | "run_job" | "terminate_instance";

export type NotificationKind =
  | "approval_requested"
  | "job_succeeded"
  | "job_failed"
  | "run_finished"
  | "data_transferred"
  | "capacity_available"
  | "instance_idle"
  | "instance_ceiling"
  | "budget_threshold"
  | "terminal_reaped"
  | "bootstrap_failed";

export type Preferences = {
  approvals: Record<GateableAction, boolean>;
  notifications: Record<NotificationKind, boolean> & { desktop: boolean };
  data_safety: {
    to_filesystem: boolean;
    to_local: boolean;
    scope: "all" | "outputs";
    local_dir: string;
    max_local_gib: number;
    if_unsaveable: "block" | "terminate";
  };
  // 0 = "use the config.yaml default" (shown as the placeholder).
  guardrails: {
    max_concurrent_instances: number;
    max_hourly_spend_usd: number;
    // A cumulative monthly wallet. Reported, never enforced. 0 = unset.
    monthly_budget_usd: number;
  };
  // Which cloud a launch that names no provider lands on. Agents and the
  // MCP bridge follow it, so one choice here moves the whole project.
  providers: {
    default_provider: string;
  };
  // Mirror every worklog entry into this folder (Obsidian vault, repo).
  worklog: {
    mirror_dir: string;
  };
  // First-run walkthrough state. Server-side rather than localStorage, so
  // the desktop shell and a browser on the same backend agree.
  onboarding: {
    completed: boolean;
    dismissed_at: string;
  };
  templates: { favorites: string[] };
};

export type PreferencesPatch = {
  approvals?: Partial<Record<GateableAction, boolean>>;
  notifications?: Partial<Record<NotificationKind | "desktop", boolean>>;
  data_safety?: Partial<Preferences["data_safety"]>;
  guardrails?: Partial<Preferences["guardrails"]>;
  providers?: Partial<Preferences["providers"]>;
  templates?: { favorites?: string[] };
  worklog?: Partial<Preferences["worklog"]>;
  onboarding?: Partial<Preferences["onboarding"]>;
};

export type Notification = {
  id: string;
  at: string;
  kind: NotificationKind;
  title: string;
  body: string;
  ref: string | null;
  read: boolean;
};

// What a rescue actually did. `unsaved` is the load-bearing field: it is only
// empty when the data is genuinely safe, and it is what a block keys on.
export type RescueReport = {
  instance_id: string;
  attempted: boolean;
  files_found: number;
  synced_to: string | null;
  sync_error: string;
  downloaded: { path: string; size_bytes: number; bytes_written: number }[];
  downloaded_bytes: number;
  skipped: { path: string; size_bytes: number; reason: string }[];
  unsaved: UnpersistedFile[];
  local_dir: string | null;
};

export type AgentStep = {
  seq: number;
  at: string;
  thought: string | null;
  action: string;
  args: Record<string, unknown>;
  result: Record<string, unknown>;
};

export const api = {
  instanceTypes: (provider?: string) =>
    request<Record<string, InstanceTypeInfo>>(`/instance-types${provider ? `?provider=${provider}` : ""}`),

  regions: (provider?: string) =>
    request<{ regions: Region[] }>(
      `/regions${provider ? `?provider=${provider}` : ""}`,
    ).then((r) => r.regions),

  // Phase 87: the number that actually gates a first GCP launch. Fresh
  // projects hold ZERO GPU quota, so the form shows it before the click.
  // Every row the provider returned, unfiltered; the caller decides which
  // of them can gate the launch it is about to make.
  gcpQuota: (region?: string) =>
    request<GcpQuota>(`/gcp/quota${region ? `?region=${region}` : ""}`),

  createFilesystem: (name: string, region: string) =>
    request<Filesystem>("/filesystems", {
      method: "POST",
      body: JSON.stringify({ name, region }),
    }),

  filesystems: () =>
    request<{ filesystems: Filesystem[] }>("/filesystems").then(
      (r) => r.filesystems,
    ),

  deleteFilesystem: (name: string, confirmName: string) =>
    request<{ deleted: string; region: string; bytes_destroyed: number }>(
      `/filesystems/${encodeURIComponent(name)}?confirm_name=${encodeURIComponent(confirmName)}`,
      { method: "DELETE" },
    ),

  sshKeys: () =>
    request<{ ssh_keys: string[]; default: string }>("/ssh-keys"),

  instances: () =>
    request<{ instances: Instance[] }>("/instances").then((r) => r.instances),

  launches: () =>
    request<{ launches: Launch[] }>("/launches").then((r) => r.launches),

  launch: (body: LaunchRequest) =>
    request<{ launch: Launch }>("/instances", {
      method: "POST",
      body: JSON.stringify(body),
    }).then((r) => r.launch),

  // Termination rescues the instance's data first (sync to the persistent
  // volume and/or download here). That can move real bytes, so it gets a far
  // longer leash than an ordinary call - a 30s abort mid-rescue would look
  // like a failure while the transfer was still going fine.
  terminate: (instanceId: string, force = false) =>
    request<{ terminated: boolean; rescue: RescueReport | null }>(
      `/instances/${instanceId}${force ? "?force=true" : ""}`,
      { method: "DELETE", timeoutMs: force ? 30_000 : 15 * 60_000 },
    ),

  rescue: (instanceId: string) =>
    request<{ rescue: RescueReport }>(`/instances/${instanceId}/rescue`, {
      method: "POST",
      timeoutMs: 15 * 60_000,
    }),

  syncEphemeral: (instanceId: string) =>
    request<{ synced_to: string }>(`/instances/${instanceId}/sync`, {
      method: "POST",
      timeoutMs: 10 * 60_000,
    }),

  attachIDE: (instanceId: string) =>
    request<{
      vscode_url: string;
      cursor_url: string;
      ssh_alias: string;
      ssh_command: string;
    }>(`/instances/${instanceId}/ide-attach`, {
      method: "POST",
    }),

  renameInstance: (instanceId: string, name: string) =>
    request<{ name: string }>(`/instances/${instanceId}/name`, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  setKeepAlive: (instanceId: string, enabled: boolean) =>
    request<{ keep_alive: boolean }>(`/instances/${instanceId}/keep-alive`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),

  setIdleTimeout: (instanceId: string, timeoutSeconds: number | null) =>
    request<{ idle_timeout_seconds: number | null }>(`/instances/${instanceId}/idle-timeout`, {
      method: "POST",
      body: JSON.stringify({ idle_timeout_seconds: timeoutSeconds }),
    }),

  // Null clears the ceiling. The backend REJECTS a value below its minimum
  // rather than clamping it, so surface the error text rather than assuming
  // the request took.
  setMaxLifetime: (instanceId: string, maxLifetimeSeconds: number | null) =>
    request<{ max_lifetime_seconds: number | null }>(`/instances/${instanceId}/max-lifetime`, {
      method: "POST",
      body: JSON.stringify({ max_lifetime_seconds: maxLifetimeSeconds }),
    }),

  diagnoseSidecar: (instanceId: string) =>
    request<SidecarDiagnosis>(`/instances/${instanceId}/sidecar/diagnose`),

  templates: () =>
    request<{ templates: Template[]; errors: Record<string, string> }>(
      "/templates",
    ),

  tasks: () => request<{ tasks: Task[] }>("/tasks").then((r) => r.tasks),

  enqueueTask: (
    template: string,
    parameters: Record<string, unknown>,
    auto?: AutoManageConfig,
    targetInstanceId?: string,
    dependsOn?: string[],
  ) =>
    request<{ task: Task }>("/tasks", {
      method: "POST",
      body: JSON.stringify({
        template,
        parameters,
        ...(auto ? { auto_manage: true, ...auto } : {}),
        ...(!auto && targetInstanceId
          ? { target_instance_id: targetInstanceId }
          : {}),
        ...(dependsOn && dependsOn.length ? { depends_on: dependsOn } : {}),
      }),
    }).then((r) => r.task),

  cancelTask: (taskId: string) =>
    request<{ cancelled: string }>(`/tasks/${taskId}/cancel`, {
      method: "POST",
    }),

  taskLogs: (taskId: string, tail?: number) =>
    request<{ lines: { seq: number; at: string; line: string }[] }>(
      `/tasks/${taskId}/logs${tail ? `?tail=${tail}` : ""}`,
    ).then((r) => r.lines),

  deleteTask: (taskId: string) =>
    request<{ deleted: string }>(`/tasks/${taskId}`, { method: "DELETE" }),

  clearFinishedTasks: () =>
    request<{ cleared: number }>("/tasks/finished", { method: "DELETE" }),

  // -- api principals (Phase 79) ---------------------------------------------

  principals: () =>
    request<{ principals: Principal[]; auth_enabled: boolean }>("/principals"),

  createPrincipal: (
    name: string,
    role: PrincipalRole = "operator",
    maxHourlyUsd?: number,
  ) =>
    request<{ name: string; role: PrincipalRole; token: string; note: string }>(
      "/principals",
      {
        method: "POST",
        body: JSON.stringify({
          name,
          role,
          ...(maxHourlyUsd ? { max_hourly_spend_usd: maxHourlyUsd } : {}),
        }),
      },
    ),

  revokePrincipal: (name: string) =>
    request<{ revoked: string }>(`/principals/${name}`, { method: "DELETE" }),

  // Phase 82: the launch policy as ENFORCED. Read-only by design; the
  // policy changes by editing policy.yaml and restarting.
  policy: () => request<PolicyDoc>("/policy"),

  modelPresets: () =>
    request<{ presets: ModelPreset[] }>("/model-presets").then(
      (r) => r.presets,
    ),

  estimate: (template: string, instanceType: string) =>
    request<Estimate>(
      `/estimate?template=${encodeURIComponent(template)}` +
        `&instance_type=${encodeURIComponent(instanceType)}`,
    ),

  modelFit: (model: string, instanceType: string) =>
    request<ModelFit>(
      `/estimate/model-fit?model=${encodeURIComponent(model)}` +
        `&instance_type=${encodeURIComponent(instanceType)}`,
    ),

  launchUtilization: (launchId: string) =>
    request<Utilization>(`/launches/${launchId}/utilization`),

  // tzOffsetMinutes is minutes EAST of UTC, so the backend knows where the
  // user's "today" starts. A locale fact the browser owns, not a policy.
  spendSummary: (tzOffsetMinutes: number) =>
    request<SpendSummary>(
      `/spend/summary?tz_offset_minutes=${Math.round(tzOffsetMinutes)}`,
    ),

  spendSeries: (tzOffsetMinutes: number, bucket = "day", days = 30) =>
    request<{ series: SpendBucket[]; mock: boolean }>(
      `/spend/series?bucket=${bucket}&days=${days}` +
        `&tz_offset_minutes=${Math.round(tzOffsetMinutes)}`,
    ),

  spendBreakdown: (tzOffsetMinutes: number, by = "instance_type", days = 30) =>
    request<{ breakdown: SpendBreakdownRow[]; mock: boolean }>(
      `/spend/breakdown?by=${by}&days=${days}` +
        `&tz_offset_minutes=${Math.round(tzOffsetMinutes)}`,
    ),

  settingsStatus: () =>
    request<{
      mock: boolean;
      lambda_configured: boolean;
      s3_configured: boolean;
      gcp_configured: boolean;
      tailscale_available: boolean;
      // Presence only: IS a token enforced, never the token itself.
      auth_required: boolean;
      // Phase 82: is a policy.yaml loaded and enforcing.
      policy_active: boolean;
      env_path: string;
      // Bounds for the max-lifetime ceiling, so the launch form can state
      // the real minimum instead of letting the user discover it as a 400.
      max_lifetime_min_seconds: number;
      max_lifetime_max_seconds: number;
      boot_timeout_seconds: number;
    }>("/settings/status"),

  listResearchKeys: () =>
    request<{ keys: ResearchKey[] }>("/research-keys"),

  setResearchKey: (name: string, value: string, note: string) =>
    request<ResearchKey>(`/research-keys/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ value, note }),
    }),

  deleteResearchKey: (name: string) =>
    request<{ deleted: string }>(`/research-keys/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  setLambdaKey: (apiKey: string) =>
    request<{ valid: boolean; instance_types_visible: number; applied_live: boolean }>(
      "/settings/lambda-key",
      { method: "POST", body: JSON.stringify({ api_key: apiKey }) },
    ),

  setGcpConfig: (projectId: string, zone: string, credentialsPath: string) =>
    request<{ valid: boolean; applied_live: boolean }>("/settings/gcp-config", {
      method: "POST",
      body: JSON.stringify({
        project_id: projectId,
        default_zone: zone,
        credentials_file: credentialsPath,
      }),
    }),

  setS3Keys: (accessKeyId: string, secretAccessKey: string) =>
    request<{ saved: boolean; validated: boolean }>("/settings/s3-keys", {
      method: "POST",
      body: JSON.stringify({
        access_key_id: accessKeyId,
        secret_access_key: secretAccessKey,
      }),
    }),

  brains: () => request<{ brains: Brain[] }>("/brains").then((r) => r.brains),

  // A CLI brain (claude/codex/gemini) is given several minutes to answer, so
  // the default 30s abort would report a client failure while the backend was
  // still working, and the user would ask again and pay twice.
  // `dataset` is REQUIRED by the backend (Field(min_length=1)) and
  // `student_model` is its name for the pinned student - not `student`,
  // which the backend silently ignored while the UI thought it had pinned
  // one. Both spellings are the backend's; keep them in step with
  // DistillConfigRequest in main.py.
  distillConfig: (body: {
    spec: string;
    brain: string;
    dataset: string;
    student_model?: string;
  }) =>
    request<{ config: DistillConfig }>("/distill/config", {
      method: "POST",
      body: JSON.stringify(body),
      timeoutMs: 5 * 60_000,
    }).then((r) => r.config),

  localModels: () => request<LocalModelLibrary>("/models/local"),

  // Moves a whole model over SSH, so it gets a generous budget: the
  // default 30s would report a failure while the transfer was still
  // running, and the user would start it again.
  pullModel: (instanceId: string, name: string) =>
    request<{
      name: string;
      path: string;
      bytes: number;
      suggested_ollama_name: string;
    }>("/models/pull", {
      method: "POST",
      body: JSON.stringify({ instance_id: instanceId, name }),
      // Four hours, not 30 minutes. The old budget happened to equal the
      // default idle timeout, so a big model hit the client abort and the
      // instance teardown at the same moment and the failure looked like a
      // network problem. Transfers run at roughly 0.6-0.7 MB/s over the
      // managed connection, so a 4.4 GB 7B student needs ~2 hours; a budget
      // that cannot fit the largest model on the shelf is a budget that
      // reports a false failure while the backend is still working.
      timeoutMs: 4 * 60 * 60_000,
    }),

  installModel: (body: {
    name: string;
    ollama_name?: string;
    overwrite?: boolean;
  }) =>
    request<{ name: string; ollama_name: string; brain_ref: string }>(
      "/models/install",
      { method: "POST", body: JSON.stringify(body), timeoutMs: 10 * 60_000 },
    ),

  gpuGuide: (provider?: string) =>
    request<GpuGuide>(`/gpu-guide${provider ? `?provider=${provider}` : ""}`),

  studentPresets: () =>
    request<{ presets: StudentPreset[] }>("/student-presets").then(
      (r) => r.presets,
    ),

  approvals: () =>
    request<{ approvals: Approval[]; timeout_seconds: number }>(
      "/autopilot/approvals",
    ),

  decideApproval: (id: string, approve: boolean) =>
    request<{ approval: Approval }>(`/autopilot/approvals/${id}`, {
      method: "POST",
      body: JSON.stringify({ approve }),
    }),

  autopilotRuns: () =>
    request<{ runs: AgentRun[] }>("/autopilot/runs").then((r) => r.runs),

  autopilotRun: (runId: string) =>
    request<AgentRun & { steps: AgentStep[] }>(`/autopilot/runs/${runId}`),

  startAutopilot: (body: {
    goal: string;
    brain?: string; // full brain ref (instance:/local:/api:)
    brain_instance_id?: string; // legacy spelling for instance brains
    max_steps?: number;
    // No step cap (stored as max_steps 0): the run ends only via
    // done/cancel/failure. Guards and approval gates still bound the spend.
    unlimited_steps?: boolean;
    // Which actions pause for approval. Omit to inherit the Settings policy.
    approve_actions?: GateableAction[];
  }) =>
    request<{ run: AgentRun }>("/autopilot/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }).then((r) => r.run),

  projectBrief: () =>
    request<{ content: string; updated_at: string | null }>("/project-brief"),

  setProjectBrief: (content: string) =>
    request<{ content: string; updated_at: string | null }>("/project-brief", {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),

  preferences: () =>
    request<{
      preferences: Preferences;
      gateable_actions: GateableAction[];
      notification_kinds: NotificationKind[];
      // The clouds this backend registered: the legal values for
      // providers.default_provider, so the control never offers one the
      // launch path would refuse.
      registered_providers: string[];
      guardrail_defaults: {
        max_concurrent_instances: number;
        max_hourly_spend_usd: number;
      };
    }>("/preferences"),

  saveCustomTemplate: (yaml: string) =>
    request<{ template: Template }>("/templates/custom", {
      method: "POST",
      body: JSON.stringify({ yaml }),
    }).then((r) => r.template),

  deleteCustomTemplate: (name: string) =>
    request<{ deleted: string }>(`/templates/custom/${name}`, {
      method: "DELETE",
    }),

  renderTemplate: (
    name: string,
    parameters: Record<string, unknown>,
  ) =>
    request<{
      template_name: string;
      rendered: string;
      param_line_mapping: Record<string, number[]>;
    }>(`/templates/${encodeURIComponent(name)}/render`, {
      method: "POST",
      body: JSON.stringify(parameters),
    }),

  updatePreferences: (patch: PreferencesPatch) =>
    request<{ preferences: Preferences }>("/preferences", {
      method: "PUT",
      body: JSON.stringify(patch),
    }).then((r) => r.preferences),

  notifications: (limit = 30) =>
    request<{ notifications: Notification[]; unread: number }>(
      `/notifications?limit=${limit}`,
    ),

  markNotificationsRead: (ids?: string[]) =>
    request<{ marked: number }>("/notifications/read", {
      method: "POST",
      body: JSON.stringify({ ids: ids ?? null }),
    }),

  clearNotifications: () =>
    request<{ cleared: number }>("/notifications", { method: "DELETE" }),

  cancelAutopilot: (runId: string) =>
    request<{ cancelling: boolean }>(`/autopilot/runs/${runId}/cancel`, {
      method: "POST",
    }),

  audit: (actor?: string, limit = 200) =>
    request<{
      entries: {
        id: number;
        at: string;
        actor: string;
        action: string;
        detail: string;
      }[];
    }>(
      `/audit?limit=${limit}${actor ? `&actor=${encodeURIComponent(actor)}` : ""}`,
    ).then((r) => r.entries),

  modelStatus: (instanceId: string) =>
    request<{
      serving: boolean;
      ready: boolean;
      status_detail?: string;
      task_id?: string;
      template?: string;
      model_id?: string;
      port?: number;
    }>(`/instances/${instanceId}/model`),

  listDir: (instanceId: string, rootName: string, path: string) =>
    request<{
      root: string;
      path: string;
      entries: {
        name: string;
        is_dir: boolean;
        size_bytes: number;
        modified: string;
      }[];
    }>(
      `/instances/${instanceId}/files/list?root_name=${rootName}&path=${encodeURIComponent(path)}`,
    ),

  dirUsage: (instanceId: string, rootName: string, path: string) =>
    request<{
      children: {
        name: string;
        is_dir: boolean;
        total_bytes: number;
        file_count: number;
      }[];
      truncated: boolean;
    }>(
      `/instances/${instanceId}/files/usage?root_name=${rootName}&path=${encodeURIComponent(path)}`,
    ),

  deletePath: (
    instanceId: string,
    rootName: string,
    path: string,
    recursive: boolean,
  ) =>
    request<{ deleted: string }>(
      `/instances/${instanceId}/files?root_name=${rootName}&path=${encodeURIComponent(path)}&recursive=${recursive}`,
      { method: "DELETE" },
    ),

  archiveUrl: (instanceId: string, absolutePath: string) =>
    `${API_BASE}/instances/${instanceId}/files/archive?path=${encodeURIComponent(absolutePath)}`,

  uploadFile: async (instanceId: string, file: File, dest = "inbox/") => {
    const form = new FormData();
    form.append("file", file);
    form.append("dest", dest);
    // Hand-rolled fetch (multipart cannot ride request()'s JSON headers),
    // so it needs the same two guarantees request() gives everything else:
    // a dead backend surfaces as a typed ApiError instead of a raw
    // TypeError, and a stalled transfer cannot hang forever. The budget is
    // generous - it covers the whole upload, not a round-trip.
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 120_000);
    let resp: Response;
    try {
      resp = await fetch(`${API_BASE}/instances/${instanceId}/files/upload`, {
        method: "POST",
        body: form,
        headers: authHeaders(), // multipart sets its own content-type
        signal: ctrl.signal,
      });
    } catch {
      if (ctrl.signal.aborted) {
        throw new ApiError(
          0,
          "Upload made no progress for 120s; the instance or its " +
            "connection is likely stalled.",
        );
      }
      // Name the address it actually tried, not a hardcoded ":8000": with
    // NEXT_PUBLIC_API_URL set, or served same-origin by the desktop app,
    // this message pointed at a port nothing was using.
    throw new ApiError(
      0,
      `Backend unreachable at ${API_BASE || window.location.origin}. ` +
        "Is the Manifold app running?",
    );
    } finally {
      clearTimeout(timer);
    }
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      if (resp.status === 401) notifyUnauthorized();
      throw new ApiError(resp.status, body.detail ?? `HTTP ${resp.status}`);
    }
    return body as { path: string; bytes: number };
  },

  downloadUrl: (instanceId: string, absolutePath: string) =>
    `${API_BASE}/instances/${instanceId}/files/download?path=${encodeURIComponent(absolutePath)}`,

  // Browser downloads are plain <a>/location navigations that cannot carry
  // the Authorization header, and the long-lived token must stay out of
  // query strings (the backend's access log records the request line). So
  // a download click first mints a single-use ~60s nonce over the authed
  // channel, then navigates to the nonce URL. Against an open backend the
  // nonce is minted but never checked - one code path either way.
  startDownload: async (url: string) => {
    const { nonce } = await request<{
      nonce: string;
      expires_in_seconds: number;
    }>("/downloads/token", { method: "POST" });
    window.location.assign(`${url}&nonce=${encodeURIComponent(nonce)}`);
  },

  recentFiles: (instanceId: string, hours = 24, limit = 50) =>
    request<{
      files: { root: string; path: string; size_bytes: number; modified: string }[];
      truncated: boolean;
      hours: number;
    }>(`/instances/${instanceId}/files/recent?hours=${hours}&limit=${limit}`),

  watches: () =>
    request<{ watches: Watch[]; auto_launch_enabled: boolean }>("/watches"),

  createWatch: (body: {
    instance_type: string;
    region: string;
    filesystem?: string;
    auto_launch?: boolean;
  }) =>
    request<{ watch: Watch }>("/watches", {
      method: "POST",
      body: JSON.stringify(body),
    }).then((r) => r.watch),

  cancelWatch: (watchId: string) =>
    request<{ watch: Watch }>(`/watches/${watchId}`, { method: "DELETE" }),

  storageFiles: (filesystem: string, prefix = "") =>
    request<{ files: StoredFile[] }>(
      `/storage/files?filesystem=${encodeURIComponent(filesystem)}&prefix=${encodeURIComponent(prefix)}`,
    ).then((r) => r.files),

  deleteFile: (filesystem: string, key: string) =>
    request<{ deleted: string }>(
      `/storage/files/${encodeURI(key)}?filesystem=${encodeURIComponent(filesystem)}`,
      { method: "DELETE" },
    ),

  clusters: () =>
    request<{ clusters: Cluster[] }>("/clusters").then((r) => r.clusters),

  cluster: (clusterId: string) =>
    request<Cluster>(`/clusters/${clusterId}`),

  launchCluster: (body: ClusterLaunchRequest) =>
    request<Cluster>("/clusters/launch", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Safe by default: force=false runs the rescue-before-destroy hook on every
  // node. A blocked rescue comes back as terminated=false with the reason in
  // that node's report `error` — the cluster keeps running (and billing).
  terminateCluster: (clusterId: string, force = false) =>
    request<{
      cluster_id: string;
      terminated: boolean;
      reports: { instance_id?: string; terminated?: boolean; error?: string }[];
    }>(`/clusters/${clusterId}/terminate?force=${force}`, { method: "POST" }),
};

export type ClusterNode = {
  cluster_id: string;
  // The stable key: a launch id, present from the moment the node is queued.
  instance_id: string;
  // The REAL cloud instance id, resolved once the node boots (null until
  // then). Everything that dials the node — telemetry stream, SSH, the dock
  // terminal — needs THIS, never instance_id. Optional so the UI can code
  // defensively while the backend enrichment rolls out.
  lambda_instance_id?: string | null;
  role: "head" | "worker";
  node_index: number;
  ip?: string;
  status: string;
};

export type Cluster = {
  id: string;
  name: string;
  gpu_type: string;
  region: string;
  filesystem: string;
  node_count: number;
  head_instance_id?: string;
  head_ip?: string;
  status: string;
  created_at: string;
  cost_cents?: number;
  nodes?: ClusterNode[];
};

export type ClusterLaunchRequest = {
  instance_type: string;
  region: string;
  filesystem: string;
  node_count: number;
  connection_mode?: string;
  ssh_key_name?: string;
  name?: string;
  provider?: string;
};


// Phase 100: research-key vault metadata. The value itself never reaches
// the dashboard; `length` is null (not 0) when the value is absent.
export type ResearchKey = {
  name: string;
  present: boolean;
  length: number | null;
  note: string;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  last_used_at: string | null;
  last_used_by: string | null;
};
