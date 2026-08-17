"""SQLite persistence for orchestrator metadata.

One table per concern; jobs and benchmarks tables arrive with Phase 4.
Uses the stdlib sqlite3 driver guarded by a lock — this is a single-user
local tool and every statement here runs in well under a millisecond.
(See DECISIONS.md: "Plain sqlite3 instead of an async driver".)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

# Launch rows in any of these states may have a REAL instance attached (or
# about to attach). Everything else is settled history.
LIVE_LAUNCH_STATUSES = ("launching", "retrying", "booting", "active")


def live_launches(db_path: str) -> list[dict]:
    """Launches in the database at `db_path` that may still have a real
    instance behind them. Read-only and tolerant of a missing file or
    table: used by the mock-mode startup guard, which must be able to
    inspect the REAL database without opening it for writing."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return []          # no database yet: nothing live
    try:
        conn.row_factory = sqlite3.Row
        marks = ",".join("?" for _ in LIVE_LAUNCH_STATUSES)
        rows = conn.execute(
            f"SELECT id, requested_type, region, status FROM launches "
            f"WHERE status IN ({marks})",
            LIVE_LAUNCH_STATUSES,
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []          # schema not created yet
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS launches (
    id                  TEXT PRIMARY KEY,
    provider            TEXT NOT NULL DEFAULT 'lambda',
    created_at          TEXT NOT NULL,          -- ISO 8601 UTC
    requested_type      TEXT NOT NULL,          -- what the user asked for
    launched_type       TEXT,                   -- what actually launched (may be a fallback)
    region              TEXT NOT NULL,
    filesystem          TEXT,
    connection_mode     TEXT NOT NULL,
    hourly_rate_cents   INTEGER,
    status              TEXT NOT NULL,          -- launching|retrying|booting|failed|active|terminated
    attempts            INTEGER NOT NULL DEFAULT 0,
    error               TEXT,                   -- last error message, for the dashboard
    lambda_instance_id  TEXT,
    -- When Lambda ACCEPTED the launch. NOT when billing starts: Lambda bills
    -- from the moment the instance passes health checks, which is later. So
    -- this is a deliberate UPPER BOUND on billing start (see spend.py).
    launched_at         TEXT,
    active_at           TEXT,                   -- when the instance reached "active"
    terminated_at       TEXT,                   -- a stop we OBSERVED
    -- The last sweep that saw this instance alive on the cloud, and the sweep
    -- that concluded it was gone without observing the stop. Together they
    -- bound the cost of a launch nobody watched die (spend.py "unresolved").
    -- resolved_at is never a substitute for terminated_at: inferring a stop
    -- time and writing it there would turn a visible unknown into a lie.
    last_seen_at        TEXT,
    resolved_at         TEXT,
    keep_alive          INTEGER NOT NULL DEFAULT 0,  -- idle auto-termination switched off
    idle_timeout_seconds REAL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,      -- a principal name, or "backend"/"autopilot"
    action      TEXT NOT NULL,
    detail      TEXT
);

-- Phase 79: named API principals. The row holds a HASH of the token, never
-- the token: the value is shown exactly once at mint time and cannot be
-- recovered from this table (secrets stay in .env / in the caller's hands,
-- the database stores only enough to recognize one). Revoked rows are kept,
-- not deleted - created_by columns elsewhere point at these names, and
-- attribution history must survive the credential it came from.
CREATE TABLE IF NOT EXISTS api_principals (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    token_hash   TEXT NOT NULL UNIQUE,  -- sha256 hex of the token value
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL,          -- which principal minted this one
    last_used_at TEXT,
    revoked_at   TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    template        TEXT NOT NULL,
    parameters      TEXT NOT NULL,      -- JSON of user-supplied values
    status          TEXT NOT NULL,      -- queued|running|succeeded|failed|skipped
    instance_id     TEXT,               -- where it ran
    started_at      TEXT,
    finished_at     TEXT,
    exit_code       INTEGER,
    error           TEXT,               -- dispatcher-level error, if any
    output_paths    TEXT,               -- JSON list of persistent paths
    -- Auto-manage (Phase 24): a job that owns its own instance lifecycle.
    -- When auto_manage=1 the dispatcher launches a dedicated instance for
    -- this job (gpu_type/region/filesystem), runs it, syncs, and terminates.
    auto_manage     INTEGER NOT NULL DEFAULT 0,
    gpu_type        TEXT,               -- requested instance type (auto-manage)
    region          TEXT,
    filesystem      TEXT,
    launch_id       TEXT,               -- the launch this job's lifecycle created
    lifecycle       TEXT,               -- queued|waiting|launching|ready|running|
                                        -- syncing|terminating|done|failed|cancelled
    lifecycle_detail TEXT,              -- human "why" for the current state
    lifecycle_events TEXT               -- JSON {state: iso-ts}, one stamp per state
);

CREATE TABLE IF NOT EXISTS task_logs (
    task_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,       -- ordering within a task
    at          TEXT NOT NULL,
    line        TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);

CREATE TABLE IF NOT EXISTS task_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    at                  TEXT NOT NULL,
    kind                TEXT NOT NULL,
    detail              TEXT,
    instance_id         TEXT,
    cost_cents_at_event INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_task_events_task_id
    ON task_events(task_id);


CREATE TABLE IF NOT EXISTS clusters (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    status              TEXT NOT NULL,   -- provisioning|active|degraded|failed|terminated
    gpu_type            TEXT NOT NULL,
    region              TEXT NOT NULL,
    filesystem          TEXT NOT NULL,
    node_count          INTEGER NOT NULL,
    head_instance_id    TEXT,
    head_ip             TEXT,
    cost_cents          INTEGER NOT NULL DEFAULT 0,
    terminated_at       TEXT
);

CREATE TABLE IF NOT EXISTS cluster_nodes (
    cluster_id          TEXT NOT NULL,
    instance_id         TEXT NOT NULL,
    role                TEXT NOT NULL,   -- head|worker
    node_index          INTEGER NOT NULL,
    ip                  TEXT,
    status              TEXT NOT NULL,   -- provisioning|running|failed|terminated
    created_at          TEXT NOT NULL,
    PRIMARY KEY (cluster_id, instance_id)
);
CREATE INDEX IF NOT EXISTS idx_cluster_nodes_cluster_id
    ON cluster_nodes(cluster_id);


CREATE TABLE IF NOT EXISTS agent_runs (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    goal                TEXT NOT NULL,
    brain_instance_id   TEXT NOT NULL,   -- instance serving the model
    brain_model         TEXT,            -- model id driving the run
    status              TEXT NOT NULL,   -- running|succeeded|failed|cancelled|exhausted
    max_steps           INTEGER NOT NULL,
    steps_taken         INTEGER NOT NULL DEFAULT 0,
    summary             TEXT,            -- the agent's own closing summary
    error               TEXT,
    finished_at         TEXT
);

CREATE TABLE IF NOT EXISTS agent_steps (
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    at          TEXT NOT NULL,
    thought     TEXT,
    action      TEXT NOT NULL,
    args        TEXT NOT NULL,           -- JSON
    result      TEXT NOT NULL,           -- JSON observation fed back
    PRIMARY KEY (run_id, seq)
);

-- Human approval gates for autopilot actions (Phase 36): a run with
-- require_approval pauses spend/destructive actions here until decided.
CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    action      TEXT NOT NULL,
    args        TEXT NOT NULL,          -- JSON
    status      TEXT NOT NULL,          -- pending|approved|denied|expired
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);

-- Notifications (Phase 37): the ping an unattended run owes you. One row
-- per event; the dashboard polls unread ones and raises a toast + an OS
-- notification. Kinds are toggled individually in Settings.
CREATE TABLE IF NOT EXISTS notifications (
    id          TEXT PRIMARY KEY,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- see preferences.NOTIFICATION_KINDS
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    ref         TEXT,                   -- task/approval/run/instance id
    read        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notifications_unread
    ON notifications(read, at);

-- User preferences (Phase 37): approval policy, notification toggles, and
-- the data-safety policy, as one JSON blob. config.yaml holds the DEFAULTS;
-- this holds what the user changed in Settings (a UI must not rewrite a
-- commented YAML file). See preferences.py.
CREATE TABLE IF NOT EXISTS preferences (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL           -- JSON
);

-- User-chosen display names for instances (Phase 39). Lambda fixes an
-- instance's name at launch; this is Manifold's own overlay, applied
-- wherever instances are shown. Deleting the row restores Lambda's name.
CREATE TABLE IF NOT EXISTS instance_names (
    instance_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL
);

-- The Autopilot project brief: ONE persistent description of what the user
-- is working on overall, included in every run's system prompt so a goal
-- reads as a step in the project instead of an isolated command.
CREATE TABLE IF NOT EXISTS project_brief (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    content     TEXT NOT NULL DEFAULT '',
    updated_at  TEXT
);

-- Phase 95: detached commands - long work started through Manifold that
-- outlives the request (and the backend: all live state is ON the box,
-- this row is the registry and the history). exit_code NULL with
-- exited_at set means VANISHED: it ended and how is not knowable - never
-- rendered as a normal exit.
CREATE TABLE IF NOT EXISTS detached_commands (
    handle       TEXT PRIMARY KEY,
    instance_id  TEXT NOT NULL,
    command      TEXT NOT NULL,
    note         TEXT,
    created_by   TEXT,
    started_at   TEXT NOT NULL,
    pid          INTEGER NOT NULL,
    exit_code    INTEGER,
    exited_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_detached_instance
    ON detached_commands(instance_id);

CREATE TABLE IF NOT EXISTS watches (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    instance_type   TEXT NOT NULL,
    region          TEXT NOT NULL,
    filesystem      TEXT,               -- needed only for auto-launch
    auto_launch     INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,      -- watching|available|launched|cancelled
    last_checked    TEXT,
    triggered_at    TEXT                -- when capacity was first seen
);

-- Periodic GPU telemetry, sampled by the dispatcher while an instance is
-- connected. Backs the post-run utilization verdict, the right-size hint,
-- and idle-spend accounting. Purely advisory; nothing on the launch path
-- reads or writes this, and nothing destructive may be gated on it.
--
-- One row is one sample of the WHOLE BOX, not of one card:
--   vram_used_mib / util_pct  the MAX across the box's GPUs
--   util_pct_mean             the MEAN across the box's GPUs
--   gpu_count                 how many GPUs that sample covered
-- Every metric column is nullable on purpose. A sidecar frozen into an
-- already-running instance can omit a field, and NULL ("this sample did not
-- say") must never be stored as 0 ("the GPUs were doing nothing").
CREATE TABLE IF NOT EXISTS telemetry_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id     TEXT NOT NULL,
    at              TEXT NOT NULL,
    gpu_name        TEXT,
    vram_used_mib   INTEGER,
    vram_total_mib  INTEGER,
    util_pct        INTEGER,
    util_pct_mean   REAL,
    gpu_count       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_telemetry_instance
    ON telemetry_samples(instance_id);
-- Additional composite for the time-windowed reads (spend/idle history):
-- the single-column index above still serves "everything for one instance",
-- this one keeps "one instance, one window" from scanning the whole table
-- as samples accumulate.
CREATE INDEX IF NOT EXISTS idx_telemetry_instance_at
    ON telemetry_samples(instance_id, at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _interval(start_iso: str | None, end_iso: str | None) -> float | None:
    """Seconds between two ISO timestamps, or None if either is missing."""
    if not start_iso or not end_iso:
        return None
    try:
        return (datetime.fromisoformat(end_iso)
                - datetime.fromisoformat(start_iso)).total_seconds()
    except (TypeError, ValueError):
        return None


class Database:
    def __init__(self, path: str):
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        # Additive migrations for databases created before a column existed
        # (CREATE TABLE IF NOT EXISTS does not alter existing tables).
        self._ensure_column("launches", "keep_alive",
                            "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("launches", "idle_timeout_seconds", "REAL")
        self._ensure_column("launches", "provider", "TEXT NOT NULL DEFAULT 'lambda'")
        # Phase 76: the two bounds on a launch whose stop was never observed.
        # Historical rows have neither, which spend.py reads as "unknown" and
        # caps at the boot timeout rather than letting a cost grow forever.
        self._ensure_column("launches", "last_seen_at", "TEXT")
        self._ensure_column("launches", "resolved_at", "TEXT")
        # Phase 76b: the opt-in maximum total lifetime, anchored on
        # launched_at. NULL (every historical row, and every launch that does
        # not ask for one) means no ceiling — nothing about the idle loop
        # changes for them.
        self._ensure_column("launches", "max_lifetime_seconds", "REAL")
        # Phase 76b: a telemetry sample describes the whole box, not GPU 0.
        # Rows written before this exist and have both columns NULL, which
        # spend.idle_spend reads as "that span was never measured" rather
        # than as an idle span - the whole point of the columns being
        # nullable (see the schema comment above).
        self._ensure_column("telemetry_samples", "util_pct_mean", "REAL")
        self._ensure_column("telemetry_samples", "gpu_count", "INTEGER")
        # Auto-manage columns (Phase 24) for databases created earlier.
        self._ensure_column("tasks", "auto_manage",
                            "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("tasks", "gpu_type", "TEXT")
        self._ensure_column("tasks", "region", "TEXT")
        self._ensure_column("tasks", "filesystem", "TEXT")
        self._ensure_column("tasks", "launch_id", "TEXT")
        self._ensure_column("tasks", "lifecycle", "TEXT")
        self._ensure_column("tasks", "lifecycle_detail", "TEXT")
        self._ensure_column("tasks", "lifecycle_events", "TEXT")
        # Phase 35: pin a manual job to a specific instance (multi-GPU).
        self._ensure_column("tasks", "target_instance_id", "TEXT")
        # Phase 77: JSON list of task ids this job runs after. Set only at
        # enqueue, only referencing tasks that already exist, immutable after
        # - which makes cycles impossible by construction (an older task's
        # deps were frozen before this one existed). NULL = no dependencies,
        # so every historical row keeps its exact old behavior.
        self._ensure_column("tasks", "depends_on", "TEXT")
        # Phase 79: who caused this row. A principal name; NULL on every
        # historical row (rendered as unattributed, never guessed). Launches
        # inherit through the chain: an auto-managed job's launch carries the
        # job creator's name, a watch's auto-launch the watch creator's.
        self._ensure_column("launches", "created_by", "TEXT")
        self._ensure_column("tasks", "created_by", "TEXT")
        self._ensure_column("watches", "created_by", "TEXT")
        self._ensure_column("agent_runs", "created_by", "TEXT")
        # Phase 94: what this box is FOR, in the launcher's own words. Free
        # text, carried into the instances payload so a reader who did not
        # launch it can tell work from waste. NULL on historical rows and
        # never inferred: an empty purpose reads as "nobody said", which is
        # the honest answer and the one that makes a reader ask rather than
        # assume. See DECISIONS.md - an agent read an unattributed box as a
        # stray and terminated a model that was still loading.
        self._ensure_column("launches", "purpose", "TEXT")
        # Phase 80: a principal's role. Pre-80 rows default to operator -
        # exactly what a minted token could do before roles existed (act,
        # but not manage credentials or policy).
        self._ensure_column("api_principals", "role",
                            "TEXT NOT NULL DEFAULT 'operator'")
        # Phase 81: an ENFORCED per-principal hourly ceiling (NULL = none).
        # Unlike the advisory monthly wallet, this is a rate guard in the
        # orchestrator: a launch that would push this principal's
        # attributed hourly burn past it is refused. Chain attribution
        # makes it bind - auto-manage and watch launches count against
        # whoever caused them.
        self._ensure_column("api_principals", "max_hourly_spend_usd", "REAL")
        # Phase 36: runs whose spend actions pause for human approval.
        self._ensure_column("agent_runs", "require_approval",
                            "INTEGER NOT NULL DEFAULT 0")
        # Phase 37: WHICH actions this run gates (JSON list). require_approval
        # above stays as the derived "is anything gated" flag, so old rows and
        # old clients keep working.
        self._ensure_column("agent_runs", "approval_policy", "TEXT")
        self._lock = threading.Lock()

    def open_path(self) -> str:
        """The file this connection is ACTUALLY writing to.

        Asked of SQLite rather than trusted from a caller's argument, because
        the mock seeder gates on it before fabricating money and a gate that
        checks a passed-in string can be handed one that names a different
        file than the connection is open on. 'main' is the schema a plain
        connect() opens; '' means there is no file behind it (:memory:).
        """
        for _, schema, file in self._execute("PRAGMA database_list").fetchall():
            if schema == "main":
                return file or ""
        return self._path

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        cols = [r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")]
        if column not in cols:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    # -- launches ------------------------------------------------------------

    def create_launch(
        self,
        *,
        requested_type: str,
        region: str,
        filesystem: str | None,
        connection_mode: str,
        hourly_rate_cents: int,
        idle_timeout_seconds: float | None = None,
        max_lifetime_seconds: float | None = None,
        provider: str = "lambda",
        launch_id: str | None = None,
        created_at: str | None = None,
        created_by: str | None = None,
        purpose: str | None = None,
    ) -> str:
        """Insert a launch row and return its id.

        `launch_id` and `created_at` default to a fresh id and the current
        time, which is what the live launch path wants and what every caller
        but one passes. The exception is the mock-history seeder, whose rows
        must carry greppable `seed-` ids and sit at fabricated times inside
        the demo window. Two optional parameters here are the boring way to
        give it that; the alternative (the seeder reaching in from outside
        to rebind this module's `uuid` and `utcnow`) made a fixture's needs
        into a live writer's hazard.
        """
        launch_id = launch_id or uuid.uuid4().hex[:12]
        self._execute(
            """INSERT INTO launches
               (id, provider, created_at, requested_type, region, filesystem,
                connection_mode, hourly_rate_cents, status,
                idle_timeout_seconds, max_lifetime_seconds, created_by,
                purpose)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'launching', ?, ?, ?, ?)""",
            (launch_id, provider, created_at or utcnow(), requested_type,
             region, filesystem, connection_mode, hourly_rate_cents,
             idle_timeout_seconds, max_lifetime_seconds, created_by,
             (purpose or "").strip() or None),
        )
        return launch_id

    def update_launch(self, launch_id: str, **fields: Any) -> None:
        allowed = {
            "status", "attempts", "error", "lambda_instance_id",
            "launched_type", "hourly_rate_cents",
            "launched_at", "active_at", "terminated_at", "keep_alive",
            "idle_timeout_seconds", "last_seen_at", "resolved_at",
            "max_lifetime_seconds",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown launch fields: {unknown}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._execute(
            f"UPDATE launches SET {cols} WHERE id = ?",
            (*fields.values(), launch_id),
        )

    def get_launch(self, launch_id: str) -> dict | None:
        row = self._execute(
            "SELECT * FROM launches WHERE id = ?", (launch_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_launches(self) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM launches ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def pending_launch_count(self) -> int:
        """How MANY launches are admitted but not yet visible on the cloud.

        The concurrency guard adds these to the cloud's running count:
        cluster nodes launch in detached tasks, so without this N sibling
        launches admitted in the same window would each see the same
        baseline and all pass a 1-instance limit. Rows that already have a
        lambda_instance_id are cloud-visible and counted there instead —
        the two sets never overlap."""
        row = self._execute(
            """SELECT COUNT(*) AS n FROM launches
                WHERE status IN ('launching', 'retrying')
                  AND lambda_instance_id IS NULL""",
        ).fetchone()
        return row["n"] or 0

    def pending_launch_spend_cents(self) -> int:
        """The hourly SPEND of those same admitted-but-not-yet-visible
        launches. The budget guard adds this to the cloud's running spend,
        exactly parallel to pending_launch_count on the concurrency axis:
        without it, two quick launches both see the same spend baseline and
        both admit, blowing past the budget. create_launch stamps the
        requested rate on the 'launching' row, so pending rows carry a
        price; and since they have no lambda_instance_id they are not
        cloud-visible, so this never double-counts with `running`."""
        row = self._execute(
            """SELECT COALESCE(SUM(hourly_rate_cents), 0) AS c FROM launches
                WHERE status IN ('launching', 'retrying')
                  AND lambda_instance_id IS NULL""",
        ).fetchone()
        return row["c"] or 0

    def principal_pending_spend_cents(self, created_by: str) -> int:
        """pending_launch_spend_cents, filtered to one principal's
        admitted-but-not-yet-visible launches (Phase 81: the per-principal
        ceiling needs the same double-admit protection as the global
        budget guard)."""
        row = self._execute(
            """SELECT COALESCE(SUM(hourly_rate_cents), 0) AS c FROM launches
                WHERE status IN ('launching', 'retrying')
                  AND lambda_instance_id IS NULL
                  AND created_by = ?""",
            (created_by,),
        ).fetchone()
        return row["c"] or 0

    def mark_launches_seen(self, instance_ids: list[str]) -> None:
        """Stamp last_seen_at on the launches behind these live instances.

        Called from the reconcile sweep, which already has the live id list in
        hand, so this is near-free. Its whole job is to leave evidence: when a
        launch later turns out to have stopped unobserved, last_seen_at is the
        only honest LOWER bound on how long it billed (see spend.py). Without
        it the cheapest defensible answer is "we have no idea"."""
        if not instance_ids:
            return
        placeholders = ", ".join("?" for _ in instance_ids)
        self._execute(
            f"""UPDATE launches SET last_seen_at = ?
                 WHERE lambda_instance_id IN ({placeholders})""",
            (utcnow(), *instance_ids),
        )

    # -- cost/utilization intelligence (read-only; off the launch path) --------

    def task_durations(self, template: str, gpu_type: str) -> list[float]:
        """Runtimes (seconds) of PAST successful runs of `template` on
        `gpu_type`, joining each task to the launch it ran on to recover the
        GPU. Feeds the pre-launch estimate; grows more accurate as history
        accumulates. Excludes rows without both timestamps."""
        rows = self._execute(
            """SELECT t.started_at AS s, t.finished_at AS f
                 FROM tasks t
                 JOIN launches l ON t.instance_id = l.lambda_instance_id
                WHERE t.template = ?
                  AND l.launched_type = ?
                  AND t.status = 'succeeded'
                  AND t.started_at IS NOT NULL
                  AND t.finished_at IS NOT NULL""",
            (template, gpu_type),
        ).fetchall()
        out = []
        for r in rows:
            try:
                start = datetime.fromisoformat(r["s"])
                finish = datetime.fromisoformat(r["f"])
            except (TypeError, ValueError):
                continue
            secs = (finish - start).total_seconds()
            if secs >= 0:
                out.append(secs)
        return out

    def task_costs(self) -> dict[str, dict]:
        """Actual runtime and cost per FINISHED task, by task id.

        Cost is the task's wall time at the hourly rate of the launch its
        instance came from - the honest attribution on a shared instance
        (the box costs the same whether one job or three ran on it, so each
        job is charged for the time it held the GPU). Tasks on adopted
        instances have no launch row and therefore no cost: unknown stays
        unknown rather than guessed. Feeds the per-job cost readout that
        lets the user sanity-check the pre-launch estimates over time."""
        rows = self._execute(
            """SELECT t.id AS id, t.started_at AS s, t.finished_at AS f,
                      l.hourly_rate_cents AS rate
                 FROM tasks t
            LEFT JOIN launches l ON t.instance_id = l.lambda_instance_id
                WHERE t.started_at IS NOT NULL
                  AND t.finished_at IS NOT NULL""",
        ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            try:
                secs = (datetime.fromisoformat(r["f"])
                        - datetime.fromisoformat(r["s"])).total_seconds()
            except (TypeError, ValueError):
                continue
            if secs < 0:
                continue
            rate = r["rate"]
            out[r["id"]] = {
                "runtime_seconds": secs,
                "actual_cost_cents": (
                    round(secs / 3600.0 * rate) if rate is not None else None
                ),
            }
        return out

    def record_telemetry_sample(self, instance_id: str, *, gpu_name: str,
                                vram_used_mib: int | None,
                                vram_total_mib: int | None,
                                util_pct: int | None,
                                util_pct_mean: float | None = None,
                                gpu_count: int | None = None,
                                at: str | None = None) -> None:
        """Store one whole-box sample. Every metric is nullable: pass None for
        anything the instance did not report, never 0 (see the schema).

        `at` defaults to now and exists so a test can lay samples out along a
        timeline; same shape, and same reason, as create_launch's optional
        `created_at`. No production caller passes it.
        """
        self._execute(
            """INSERT INTO telemetry_samples
                   (instance_id, at, gpu_name, vram_used_mib,
                    vram_total_mib, util_pct, util_pct_mean, gpu_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (instance_id, at or utcnow(), gpu_name, vram_used_mib,
             vram_total_mib, util_pct, util_pct_mean, gpu_count),
        )

    def telemetry_summary(self, instance_id: str) -> dict:
        """Aggregate an instance's samples: sample count, PEAK vram used, the
        card's total vram, and average utilization. Peak (not average) vram is
        the OOM-relevant figure the right-size hint keys on.

        NOTE what the two per-sample columns mean on a multi-GPU box, because
        it decides what these aggregates mean:
          MAX(vram_used_mib)  the peak of the per-sample MAXIMA, i.e. the
                              busiest card at its busiest moment. That is the
                              OOM-relevant figure; a mean would hide the card
                              that actually filled up.
          AVG(util_pct)       the mean of the per-sample MAXIMA, so it reads
                              "how busy was the busiest card, on average".
                              Deliberately NOT the mean across cards: the
                              right-size hint keys on it, and a hint that
                              downsizes a box because seven of its eight GPUs
                              were quiet is exactly the OOM this refuses to
                              risk. Idle-spend accounting wants the opposite
                              and reads util_pct_mean instead - see
                              spend.idle_spend. The two never cross.
        """
        row = self._execute(
            """SELECT COUNT(*) AS n,
                      MAX(vram_used_mib) AS peak_used,
                      MAX(vram_total_mib) AS total,
                      AVG(util_pct) AS avg_util,
                      MAX(gpu_name) AS gpu_name,
                      MAX(gpu_count) AS gpus
                 FROM telemetry_samples WHERE instance_id = ?""",
            (instance_id,),
        ).fetchone()
        return {
            "sample_count": row["n"] or 0,
            "peak_vram_used_mib": row["peak_used"] or 0,
            "vram_total_mib": row["total"] or 0,
            "avg_util_pct": float(row["avg_util"] or 0.0),
            "gpu_name": row["gpu_name"] or "",
            # None (not 0) when no sample ever said - every row predates the
            # column, or every sample came from a source that omits it.
            "gpu_count": row["gpus"],
        }

    def telemetry_samples_between(self, instance_id: str, start_iso: str,
                                  end_iso: str) -> list[dict]:
        """Every sample for one instance inside one window, oldest first.

        Windowed rather than "all of it" because idle-spend accounting only
        ever asks about a launch's own lifetime, and telemetry_samples grows
        without bound. The (instance_id, at) composite index serves exactly
        this shape.
        """
        rows = self._execute(
            """SELECT at, util_pct, util_pct_mean, gpu_count
                 FROM telemetry_samples
                WHERE instance_id = ? AND at >= ? AND at <= ?
                ORDER BY at""",
            (instance_id, start_iso, end_iso),
        ).fetchall()
        return [dict(r) for r in rows]

    def peak_util_since(self, instance_id: str, since_iso: str) -> dict:
        """Busiest GPU sample in a window, and HOW MANY samples there were.

        The count is not decoration. "No samples" and "samples, all zero"
        are opposite findings — one is no evidence, the other is evidence of
        no work — and the idle sweep must act on them differently. Collapsing
        them into a bare peak of 0 would recreate, inside the fix, the exact
        inference the fix exists to remove.

        MAX and not the mean: one busy card out of eight is a box doing work.
        (util_pct is already the per-sample max across cards; util_pct_mean
        is the idle-SPEND figure, where under-reporting busyness would be the
        unsafe direction. Here it is the reverse.)
        """
        row = self._execute(
            """SELECT COUNT(util_pct) AS samples, MAX(util_pct) AS peak
                 FROM telemetry_samples
                WHERE instance_id = ? AND at >= ? AND util_pct IS NOT NULL""",
            (instance_id, since_iso),
        ).fetchone()
        return {"samples": row["samples"] or 0, "peak": row["peak"]}

    def latest_telemetry(self, instance_ids: list[str]) -> dict[str, dict]:
        """The most recent GPU sample for each of these instances, keyed by id.

        ONE query for the whole fleet, not one per box: this feeds a view that
        polls, and the N+1 version would put a query per instance behind every
        tick. The `(instance_id, at)` index serves it as a seek per id.

        Uses SQLite's documented bare-column-with-MAX form: when MAX() is the
        aggregate, the other selected columns come from the row that produced
        it. That is a real guarantee in SQLite, not an accident of ordering.

        An instance with no samples is simply ABSENT from the result, never a
        row of zeroes. A caller must be able to tell "never measured" from
        "measured, and idle" - the whole point of the sampling in the first
        place - and `at` rides along so a caller can also tell a fresh reading
        from a stale one rather than presenting old numbers as current.
        """
        if not instance_ids:
            return {}
        marks = ",".join("?" for _ in instance_ids)
        rows = self._execute(
            f"""SELECT instance_id, MAX(at) AS at, gpu_name, vram_used_mib,
                       vram_total_mib, util_pct, util_pct_mean, gpu_count
                  FROM telemetry_samples
                 WHERE instance_id IN ({marks})
                 GROUP BY instance_id""",
            tuple(instance_ids),
        ).fetchall()
        return {r["instance_id"]: dict(r) for r in rows}

    # -- detached commands (Phase 95) ---------------------------------------

    def create_detached(self, *, handle: str, instance_id: str, command: str,
                        note: str, created_by: str | None, pid: int) -> None:
        self._execute(
            """INSERT INTO detached_commands
                   (handle, instance_id, command, note, created_by,
                    started_at, pid)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (handle, instance_id, command, note, created_by, utcnow(), pid),
        )

    def finish_detached(self, handle: str, exit_code: int | None) -> None:
        """Settle a handle. exit_code None means VANISHED - it ended and how
        is not knowable. Idempotent-by-guard: the first settle wins, so a
        late probe cannot overwrite a recorded exit with a vanish."""
        self._execute(
            """UPDATE detached_commands
                  SET exit_code = ?, exited_at = ?
                WHERE handle = ? AND exited_at IS NULL""",
            (exit_code, utcnow(), handle),
        )

    def open_detached(self, instance_id: str) -> list[dict]:
        """Handles on this instance that have not settled - what the
        telemetry loop probes, and what counts as evidence of work."""
        rows = self._execute(
            """SELECT * FROM detached_commands
                WHERE instance_id = ? AND exited_at IS NULL
                ORDER BY started_at""",
            (instance_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_detached(self, handle: str) -> dict | None:
        row = self._execute(
            "SELECT * FROM detached_commands WHERE handle = ?", (handle,)
        ).fetchone()
        return dict(row) if row else None

    def list_detached(self, instance_id: str, limit: int = 20) -> list[dict]:
        rows = self._execute(
            """SELECT * FROM detached_commands WHERE instance_id = ?
                ORDER BY started_at DESC LIMIT ?""",
            (instance_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def find_launch_by_instance(self, lambda_instance_id: str) -> dict | None:
        row = self._execute(
            """SELECT * FROM launches WHERE lambda_instance_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (lambda_instance_id,),
        ).fetchone()
        return dict(row) if row else None

    # -- audit log -----------------------------------------------------------

    # -- api principals (Phase 79) ---------------------------------------------

    def create_principal(self, *, name: str, token_hash: str,
                         created_by: str, role: str = "operator",
                         max_hourly_spend_usd: float | None = None) -> str:
        pid = uuid.uuid4().hex[:12]
        self._execute(
            """INSERT INTO api_principals
               (id, name, token_hash, created_at, created_by, role,
                max_hourly_spend_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (pid, name, token_hash, utcnow(), created_by, role,
             max_hourly_spend_usd),
        )
        return pid

    def principal_by_hash(self, token_hash: str) -> dict | None:
        """The principal a presented token resolves to, or None. The caller
        hashes; this table never sees a token value."""
        row = self._execute(
            "SELECT * FROM api_principals WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        return dict(row) if row else None

    def principal_by_name(self, name: str) -> dict | None:
        row = self._execute(
            "SELECT * FROM api_principals WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None

    def list_principals(self) -> list[dict]:
        """All principals WITHOUT their hashes: this feeds the API, and a
        hash is still an offline-crackable fingerprint of a secret."""
        rows = self._execute(
            """SELECT id, name, role, created_at, created_by, last_used_at,
                      revoked_at, max_hourly_spend_usd
                 FROM api_principals ORDER BY created_at, id"""
        ).fetchall()
        return [dict(r) for r in rows]

    def touch_principal(self, name: str) -> None:
        self._execute(
            "UPDATE api_principals SET last_used_at = ? WHERE name = ?",
            (utcnow(), name),
        )

    def revoke_principal(self, name: str) -> None:
        """Revoke, never delete: created_by columns across the database
        point at this name, and history outlives credentials."""
        self._execute(
            """UPDATE api_principals SET revoked_at = ?
                WHERE name = ? AND revoked_at IS NULL""",
            (utcnow(), name),
        )

    def record_audit(self, actor: str, action: str, detail: str = "") -> None:
        self._execute(
            "INSERT INTO audit_log (at, actor, action, detail) VALUES (?, ?, ?, ?)",
            (utcnow(), actor, action, detail),
        )

    def list_audit(self, actor: str | None = None, limit: int = 200) -> list[dict]:
        if actor:
            rows = self._execute(
                "SELECT * FROM audit_log WHERE actor = ? ORDER BY id DESC LIMIT ?",
                (actor, limit),
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- tasks -----------------------------------------------------------------

    def create_task(self, *, template: str, parameters: dict,
                    auto_manage: bool = False, gpu_type: str | None = None,
                    region: str | None = None,
                    filesystem: str | None = None,
                    target_instance_id: str | None = None,
                    depends_on: list[str] | None = None,
                    created_by: str | None = None) -> str:
        task_id = uuid.uuid4().hex[:12]
        # Auto-managed jobs start in lifecycle 'queued' with a first event
        # stamp; manual jobs leave lifecycle NULL (the field is unused for
        # them, so the dispatcher and UI treat them exactly as before).
        lifecycle = "queued" if auto_manage else None
        events = json.dumps({"queued": utcnow()}) if auto_manage else None
        self._execute(
            """INSERT INTO tasks
               (id, created_at, template, parameters, status,
                auto_manage, gpu_type, region, filesystem,
                lifecycle, lifecycle_events, target_instance_id, depends_on,
                created_by)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, utcnow(), template, json.dumps(parameters),
             1 if auto_manage else 0, gpu_type, region, filesystem,
             lifecycle, events, target_instance_id,
             json.dumps(depends_on) if depends_on else None, created_by),
        )
        return task_id

    def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {"status", "instance_id", "started_at", "finished_at",
                   "exit_code", "error", "output_paths"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown task fields: {unknown}")
        if "output_paths" in fields and not isinstance(fields["output_paths"], str):
            fields["output_paths"] = json.dumps(fields["output_paths"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._execute(
            f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id)
        )

    # Auto-manage lifecycle states that still hold the single-instance slot
    # (the job is mid-flight). 'queued' is not-yet-started; the terminal
    # states (done/failed/cancelled) release the slot.
    ACTIVE_LIFECYCLE = ("waiting", "launching", "ready", "running",
                        "syncing", "terminating")
    # In-flight states that actually have an instance attached (excludes
    # 'waiting', which is pre-launch). Used to find instances an auto-managed
    # job owns.
    _OWNING_LIFECYCLE = ("launching", "ready", "running",
                         "syncing", "terminating")

    def set_task_lifecycle(self, task_id: str, lifecycle: str, *,
                           detail: str | None = None,
                           launch_id: str | None = None,
                           stamp: bool = True) -> None:
        """Move an auto-managed task to a new lifecycle state.

        Records a single timestamp per state in lifecycle_events (stamp=False
        updates only the detail, e.g. re-describing a blocked termination
        without re-stamping). Optionally attaches the launch this job created.
        """
        row = self._execute(
            "SELECT lifecycle_events FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        events = json.loads(row["lifecycle_events"]) if row and row["lifecycle_events"] else {}
        if stamp and lifecycle not in events:
            events[lifecycle] = utcnow()
        fields: dict[str, Any] = {
            "lifecycle": lifecycle,
            "lifecycle_events": json.dumps(events),
        }
        if detail is not None:
            fields["lifecycle_detail"] = detail
        if launch_id is not None:
            fields["launch_id"] = launch_id
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._execute(
            f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id)
        )

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict:
        task = dict(row)
        task["parameters"] = json.loads(task["parameters"])
        task["output_paths"] = json.loads(task["output_paths"] or "[]")
        task["auto_manage"] = bool(task.get("auto_manage"))
        task["depends_on"] = json.loads(task["depends_on"]) if task.get("depends_on") else []
        events = json.loads(task["lifecycle_events"]) if task.get("lifecycle_events") else {}
        task["lifecycle_events"] = events
        # Launch-to-ready instrumentation: how long from kicking off the
        # launch to a connected, ready-to-run GPU. The zero-waste headline
        # number, surfaced on the job card.
        task["launch_to_ready_seconds"] = _interval(
            events.get("launching"), events.get("ready"))
        return task

    def get_task(self, task_id: str) -> dict | None:
        row = self._execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._task_row(row) if row else None

    def list_tasks(self) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM tasks ORDER BY created_at DESC, id"
        ).fetchall()
        return [self._task_row(r) for r in rows]

    def next_queued_task(self) -> dict | None:
        row = self._execute(
            "SELECT * FROM tasks WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
        ).fetchone()
        return self._task_row(row) if row else None

    def queued_tasks(self) -> list[dict]:
        """All queued tasks, oldest first. The dispatcher scans these to find
        the first one with an eligible instance (auto-managed jobs bind to
        their own launched instance; manual jobs take any free one)."""
        rows = self._execute(
            "SELECT * FROM tasks WHERE status = 'queued' ORDER BY created_at, id"
        ).fetchall()
        return [self._task_row(r) for r in rows]

    # -- auto-manage lifecycle queries -----------------------------------------

    def pending_auto_managed_tasks(self) -> list[dict]:
        """ALL not-yet-started auto-managed jobs, oldest first. The lifecycle
        loop scans these for the first one whose dependencies are met, so a
        job waiting on a parent does not block a younger independent job
        from taking the launch slot (the waiter waits on a TASK, not the
        slot - nothing starves)."""
        rows = self._execute(
            """SELECT * FROM tasks
                WHERE auto_manage = 1 AND lifecycle = 'queued'
                ORDER BY created_at, id"""
        ).fetchall()
        return [self._task_row(r) for r in rows]

    def active_auto_managed_task(self) -> dict | None:
        """The auto-managed job currently holding the instance slot, if any.

        v1 is sequential (one in flight at a time); if more than one ever
        exists, the oldest is returned so it drains first."""
        placeholders = ", ".join("?" for _ in self.ACTIVE_LIFECYCLE)
        row = self._execute(
            f"""SELECT * FROM tasks
                 WHERE auto_manage = 1 AND lifecycle IN ({placeholders})
                 ORDER BY created_at, id LIMIT 1""",
            self.ACTIVE_LIFECYCLE,
        ).fetchone()
        return self._task_row(row) if row else None

    def auto_managed_instance_ids(self) -> set[str]:
        """Instance ids owned by an in-flight auto-managed job. The idle loop
        skips these (their lifecycle owns teardown) and manual jobs never
        dispatch onto them."""
        placeholders = ", ".join("?" for _ in self._OWNING_LIFECYCLE)
        rows = self._execute(
            f"""SELECT DISTINCT l.lambda_instance_id AS iid
                  FROM tasks t JOIN launches l ON t.launch_id = l.id
                 WHERE t.auto_manage = 1
                   AND t.lifecycle IN ({placeholders})
                   AND l.lambda_instance_id IS NOT NULL""",
            self._OWNING_LIFECYCLE,
        ).fetchall()
        return {r["iid"] for r in rows}

    def running_tasks(self) -> list[dict]:
        """All currently-running tasks. The dispatcher derives per-instance
        busy state from these (which box is running what)."""
        rows = self._execute(
            "SELECT * FROM tasks WHERE status = 'running'"
        ).fetchall()
        return [self._task_row(r) for r in rows]

    def running_task_count(self) -> int:
        row = self._execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = 'running'"
        ).fetchone()
        return row["n"]

    def queued_dependents(self, task_id: str) -> list[dict]:
        """Queued tasks whose depends_on names task_id. These still need the
        parent row to gate on (the dependency check reads parent rows live),
        so deleting the parent out from under them would leave a dangling
        edge. A Python scan, not JSON SQL: the queued list is small."""
        return [t for t in self.queued_tasks()
                if task_id in (t.get("depends_on") or [])]

    def delete_task(self, task_id: str) -> None:
        """Remove one task and its logs (used by the Job History 'remove')."""
        with self._lock:
            self._conn.execute("DELETE FROM task_logs WHERE task_id = ?",
                               (task_id,))
            self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._conn.commit()

    # Terminal statuses history-clearing may remove. 'skipped' (a dependent
    # that never ran because its parent did not succeed) is finished too.
    FINISHED_STATUSES = ("succeeded", "failed", "skipped")

    def delete_finished_tasks(self) -> int:
        """Clear finished (succeeded/failed/skipped) tasks and their logs.
        Active jobs (queued/running) are left untouched, and so is any
        finished task a QUEUED task still depends on - clearing a succeeded
        parent while its child waits to dispatch would sever the edge the
        child's gate reads. Returns the count removed."""
        keep = {dep for t in self.queued_tasks()
                for dep in (t.get("depends_on") or [])}
        placeholders = ", ".join("?" for _ in self.FINISHED_STATUSES)
        with self._lock:
            ids = [r["id"] for r in self._conn.execute(
                f"SELECT id FROM tasks WHERE status IN ({placeholders})",
                self.FINISHED_STATUSES,
            ).fetchall() if r["id"] not in keep]
            for tid in ids:
                self._conn.execute("DELETE FROM task_logs WHERE task_id = ?",
                                   (tid,))
                self._conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
            self._conn.commit()
            return len(ids)

    # -- task logs ---------------------------------------------------------------

    def append_task_log(self, task_id: str, line: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS seq FROM task_logs WHERE task_id = ?",
                (task_id,),
            )
            seq = cur.fetchone()["seq"]
            self._conn.execute(
                "INSERT INTO task_logs (task_id, seq, at, line) VALUES (?, ?, ?, ?)",
                (task_id, seq, utcnow(), line),
            )
            self._conn.commit()

    def get_task_logs(self, task_id: str, tail: int | None = None) -> list[dict]:
        if tail is not None:
            rows = self._execute(
                """SELECT * FROM (
                       SELECT * FROM task_logs WHERE task_id = ?
                       ORDER BY seq DESC LIMIT ?
                   ) ORDER BY seq""",
                (task_id, tail),
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT * FROM task_logs WHERE task_id = ? ORDER BY seq",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_task_logs_after(self, task_id: str, after_seq: int = -1) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM task_logs WHERE task_id = ? AND seq > ? ORDER BY seq",
            (task_id, after_seq),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- task events (Phase 71) --------------------------------------------------

    def record_task_event(self, task_id: str, kind: str, detail: dict | str | None = None,
                          instance_id: str | None = None, cost_cents_at_event: int = 0) -> None:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail)
        self._execute(
            """INSERT INTO task_events (task_id, at, kind, detail, instance_id, cost_cents_at_event)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, utcnow(), kind, detail, instance_id, cost_cents_at_event),
        )

    def get_task_events(self, task_id: str) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_task_events_after(self, task_id: str, after_id: int = 0) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM task_events WHERE task_id = ? AND id > ? ORDER BY id",
            (task_id, after_id),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- autopilot runs ---------------------------------------------------------------

    def create_agent_run(self, *, goal: str, brain_instance_id: str,
                         brain_model: str, max_steps: int,
                         gated_actions: tuple[str, ...] = (),
                         created_by: str | None = None) -> str:
        run_id = uuid.uuid4().hex[:12]
        self._execute(
            """INSERT INTO agent_runs
               (id, created_at, goal, brain_instance_id, brain_model,
                status, max_steps, require_approval, approval_policy,
                created_by)
               VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)""",
            (run_id, utcnow(), goal, brain_instance_id, brain_model,
             max_steps, 1 if gated_actions else 0,
             json.dumps(sorted(gated_actions)), created_by),
        )
        return run_id

    # -- approvals (Phase 36) -----------------------------------------------------

    def create_approval(self, run_id: str, seq: int, action: str,
                        args: dict) -> str:
        approval_id = uuid.uuid4().hex[:12]
        self._execute(
            """INSERT INTO approvals (id, run_id, seq, action, args,
               status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (approval_id, run_id, seq, action, json.dumps(args), utcnow()),
        )
        return approval_id

    def get_approval(self, approval_id: str) -> dict | None:
        row = self._execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            return None
        approval = dict(row)
        approval["args"] = json.loads(approval["args"])
        return approval

    def decide_approval(self, approval_id: str, status: str) -> bool:
        """pending -> approved/denied/expired. False if already decided
        (the WHERE guard makes concurrent decisions race-safe)."""
        cur = self._execute(
            """UPDATE approvals SET status = ?, decided_at = ?
               WHERE id = ? AND status = 'pending'""",
            (status, utcnow(), approval_id),
        )
        return cur.rowcount > 0

    def pending_approvals(self) -> list[dict]:
        rows = self._execute(
            """SELECT a.*, r.goal AS run_goal FROM approvals a
               LEFT JOIN agent_runs r ON a.run_id = r.id
               WHERE a.status = 'pending' ORDER BY a.created_at""",
        ).fetchall()
        out = []
        for r in rows:
            approval = dict(r)
            approval["args"] = json.loads(approval["args"])
            out.append(approval)
        return out

    def update_agent_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"status", "steps_taken", "summary", "error", "finished_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown agent_run fields: {unknown}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._execute(
            f"UPDATE agent_runs SET {cols} WHERE id = ?",
            (*fields.values(), run_id),
        )

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        run = dict(row)
        raw = run.get("approval_policy")
        run["approval_policy"] = json.loads(raw) if raw else []
        run["require_approval"] = bool(run.get("require_approval"))
        return run

    def get_agent_run(self, run_id: str) -> dict | None:
        row = self._execute(
            "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return self._run_row(row) if row else None

    def list_agent_runs(self, limit: int = 50) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM agent_runs ORDER BY created_at DESC, id LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._run_row(r) for r in rows]

    def add_agent_step(self, run_id: str, seq: int, *, thought: str,
                       action: str, args: dict, result: dict) -> None:
        self._execute(
            """INSERT INTO agent_steps (run_id, seq, at, thought, action,
               args, result) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, seq, utcnow(), thought, action,
             json.dumps(args), json.dumps(result)),
        )

    def get_agent_steps(self, run_id: str) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        steps = []
        for r in rows:
            step = dict(r)
            step["args"] = json.loads(step["args"])
            step["result"] = json.loads(step["result"])
            steps.append(step)
        return steps

    def fail_orphaned_agent_runs(self) -> int:
        """Mark runs left 'running' by a dead process as failed. Called at
        startup: an in-memory agent loop cannot survive a restart, and a
        row that claims to be running forever would be a lie."""
        with self._lock:
            cur = self._conn.execute(
                """UPDATE agent_runs
                   SET status = 'failed', finished_at = ?,
                       error = 'backend restarted mid-run'
                   WHERE status = 'running'""",
                (utcnow(),),
            )
            self._conn.commit()
            return cur.rowcount

    # -- notifications (Phase 37) ---------------------------------------------------

    def create_notification(self, *, kind: str, title: str, body: str = "",
                            ref: str | None = None) -> str:
        notification_id = uuid.uuid4().hex[:12]
        self._execute(
            """INSERT INTO notifications (id, at, kind, title, body, ref, read)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (notification_id, utcnow(), kind, title, body, ref),
        )
        return notification_id

    def list_notifications(self, *, unread_only: bool = False,
                           limit: int = 50) -> list[dict]:
        where = "WHERE read = 0 " if unread_only else ""
        rows = self._execute(
            f"SELECT * FROM notifications {where}ORDER BY at DESC, id LIMIT ?",
            (limit,),
        ).fetchall()
        return [{**dict(r), "read": bool(r["read"])} for r in rows]

    def notification_exists(self, kind: str, ref: str) -> bool:
        """Has this exact (kind, ref) already been recorded?

        The dedupe key for notifications that describe an ongoing CONDITION
        rather than an event - "this instance is idle" is true on every
        telemetry tick, and a ping per tick is how a bell becomes noise a
        user learns to ignore. Stored in the table rather than in memory so
        a backend restart does not re-ping what you already dismissed.
        """
        row = self._execute(
            "SELECT 1 FROM notifications WHERE kind = ? AND ref = ? LIMIT 1",
            (kind, ref),
        ).fetchone()
        return row is not None

    def unread_notification_count(self) -> int:
        row = self._execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE read = 0"
        ).fetchone()
        return row["n"]

    def mark_notifications_read(self, ids: list[str] | None = None) -> int:
        """Mark the given notifications read; ids=None marks everything."""
        if ids is None:
            cur = self._execute("UPDATE notifications SET read = 1 WHERE read = 0")
            return cur.rowcount
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        cur = self._execute(
            f"UPDATE notifications SET read = 1 WHERE id IN ({placeholders})",
            tuple(ids),
        )
        return cur.rowcount

    def clear_notifications(self) -> int:
        cur = self._execute("DELETE FROM notifications")
        return cur.rowcount

    # -- preferences (Phase 37) -----------------------------------------------------

    def get_preferences(self, key: str) -> dict | None:
        row = self._execute(
            "SELECT value FROM preferences WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def set_preferences(self, key: str, value: dict) -> None:
        self._execute(
            """INSERT INTO preferences (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, json.dumps(value)),
        )

    # -- instance display names (Phase 39) --------------------------------------------

    def set_instance_name(self, instance_id: str, name: str) -> None:
        """Set (or clear, with an empty name) the user's display name."""
        if name:
            self._execute(
                """INSERT INTO instance_names (instance_id, name)
                   VALUES (?, ?)
                   ON CONFLICT(instance_id) DO UPDATE SET name = excluded.name""",
                (instance_id, name),
            )
        else:
            self._execute(
                "DELETE FROM instance_names WHERE instance_id = ?",
                (instance_id,),
            )

    def instance_names(self) -> dict[str, str]:
        rows = self._execute("SELECT * FROM instance_names").fetchall()
        return {r["instance_id"]: r["name"] for r in rows}

    # -- project brief --------------------------------------------------------------

    def get_project_brief(self) -> dict:
        row = self._execute(
            "SELECT content, updated_at FROM project_brief WHERE id = 1"
        ).fetchone()
        if row is None:
            return {"content": "", "updated_at": None}
        return {"content": row["content"], "updated_at": row["updated_at"]}

    def set_project_brief(self, content: str) -> None:
        self._execute(
            """INSERT INTO project_brief (id, content, updated_at)
               VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET content = excluded.content,
                                             updated_at = excluded.updated_at""",
            (content, utcnow()),
        )

    # -- capacity watches -----------------------------------------------------------

    def create_watch(self, *, instance_type: str, region: str,
                     filesystem: str | None, auto_launch: bool,
                     created_by: str | None = None) -> str:
        watch_id = uuid.uuid4().hex[:12]
        self._execute(
            """INSERT INTO watches
               (id, created_at, instance_type, region, filesystem,
                auto_launch, status, created_by)
               VALUES (?, ?, ?, ?, ?, ?, 'watching', ?)""",
            (watch_id, utcnow(), instance_type, region, filesystem,
             int(auto_launch), created_by),
        )
        return watch_id

    def update_watch(self, watch_id: str, **fields: Any) -> None:
        allowed = {"status", "last_checked", "triggered_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown watch fields: {unknown}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._execute(
            f"UPDATE watches SET {cols} WHERE id = ?", (*fields.values(), watch_id)
        )

    def get_watch(self, watch_id: str) -> dict | None:
        row = self._execute(
            "SELECT * FROM watches WHERE id = ?", (watch_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_watches(self) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM watches ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def active_watches(self) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM watches WHERE status = 'watching'"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- cluster orchestration -----------------------------------------------------

    def create_cluster(self, *, cluster_id: str, name: str, gpu_type: str,
                       region: str, filesystem: str, node_count: int,
                       head_instance_id: str | None = None,
                       head_ip: str | None = None) -> None:
        self._execute(
            """INSERT INTO clusters (id, name, created_at, status, gpu_type, region,
               filesystem, node_count, head_instance_id, head_ip)
               VALUES (?, ?, ?, 'provisioning', ?, ?, ?, ?, ?, ?)""",
            (cluster_id, name, utcnow(), gpu_type, region, filesystem, node_count,
             head_instance_id, head_ip),
        )

    def add_cluster_node(self, *, cluster_id: str, instance_id: str, role: str,
                         node_index: int, ip: str | None = None,
                         status: str = "provisioning") -> None:
        self._execute(
            """INSERT INTO cluster_nodes (cluster_id, instance_id, role, node_index, ip, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cluster_id, instance_id, role, node_index, ip, status, utcnow()),
        )

    def update_cluster_status(self, cluster_id: str, status: str,
                              head_instance_id: str | None = None,
                              head_ip: str | None = None,
                              cost_cents: int | None = None) -> None:
        updates = ["status = ?"]
        params: list[Any] = [status]
        if head_instance_id is not None:
            updates.append("head_instance_id = ?")
            params.append(head_instance_id)
        if head_ip is not None:
            updates.append("head_ip = ?")
            params.append(head_ip)
        if cost_cents is not None:
            updates.append("cost_cents = ?")
            params.append(cost_cents)
        if status == "terminated":
            updates.append("terminated_at = ?")
            params.append(utcnow())
        params.append(cluster_id)
        sql = f"UPDATE clusters SET {', '.join(updates)} WHERE id = ?"
        self._execute(sql, tuple(params))

    def update_cluster_node_status(self, cluster_id: str, instance_id: str,
                                   status: str, ip: str | None = None) -> None:
        # Superseded: get_cluster_nodes now resolves each node's status and
        # real instance id LIVE from its launch row, so nothing needs to
        # write cluster_nodes.status. Kept for callers that want to freeze a
        # status onto the node row directly; currently unused.
        if ip is not None:
            self._execute(
                "UPDATE cluster_nodes SET status = ?, ip = ? WHERE cluster_id = ? AND instance_id = ?",
                (status, ip, cluster_id, instance_id),
            )
        else:
            self._execute(
                "UPDATE cluster_nodes SET status = ? WHERE cluster_id = ? AND instance_id = ?",
                (status, cluster_id, instance_id),
            )

    def get_cluster(self, cluster_id: str) -> dict | None:
        row = self._execute("SELECT * FROM clusters WHERE id = ?", (cluster_id,)).fetchone()
        if not row:
            return None
        res = dict(row)
        res["nodes"] = self.get_cluster_nodes(cluster_id)
        return res

    def list_clusters(self) -> list[dict]:
        rows = self._execute("SELECT * FROM clusters ORDER BY created_at DESC").fetchall()
        clusters = []
        for r in rows:
            c = dict(r)
            c["nodes"] = self.get_cluster_nodes(c["id"])
            clusters.append(c)
        return clusters

    def get_cluster_nodes(self, cluster_id: str) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM cluster_nodes WHERE cluster_id = ? ORDER BY node_index",
            (cluster_id,),
        ).fetchall()
        nodes = []
        for r in rows:
            node = dict(r)
            # A node's stored `instance_id` is the LAUNCH id — the stable node
            # key the dashboard uses. But telemetry, SSH, and node lookups need
            # the REAL cloud instance id, which the launch pipeline records as
            # `lambda_instance_id` when the node reaches 'booting' (null before
            # then). We also want the node's LIVE status, not the value frozen
            # at add_cluster_node time. Resolve both from the launch row here,
            # so no separate write path has to keep cluster_nodes in sync (this
            # is why update_cluster_node_status has no callers).
            launch = self.get_launch(node["instance_id"])
            if launch:
                node["lambda_instance_id"] = launch.get("lambda_instance_id")
                node["status"] = launch["status"]
            else:
                node["lambda_instance_id"] = None
            nodes.append(node)
        return nodes
