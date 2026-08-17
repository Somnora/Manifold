"""Manifold MCP server — stdio bridge from any MCP client to the backend.

HARD RULE (enforced by a test that scans this file's imports): this module
talks to the backend over HTTP only. It imports nothing from the rest of
the application — no orchestrator, no database, no Lambda client — so there
is structurally no path around the backend's guards. An agent calling
launch_gpu hits the exact same budget/concurrency/region walls as the
dashboard's Launch button.

Every tool accepts an optional `note` (why the agent is doing this); each
call is recorded in the backend audit log (tool, args, note, result) and
shown on the dashboard's Agent Activity page.

Run: `uv run manifold-mcp` from backend/ (stdio transport).
Config: MANIFOLD_API_URL (default http://localhost:8000), and
MANIFOLD_API_TOKEN when the backend enforces its API token (real mode
does; see docs/mcp-setup.md).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

API_URL = os.environ.get("MANIFOLD_API_URL", "http://localhost:8000")
# The backend's API token (Phase 78). Empty is fine against a mock or
# open backend; against a real one every call would come back 401 with
# the .env path to copy it from.
API_TOKEN = os.environ.get("MANIFOLD_API_TOKEN", "")

mcp = FastMCP(
    "manifold",
    instructions=(
        "Manifold orchestrates Lambda Cloud GPU instances through a guarded "
        "local backend. FIRST call get_skill once per session: it returns "
        "the full playbook (recipes for launching, serving models, batch "
        "jobs, fine-tuning, teardown, and the rules that keep work safe). "
        "Core rules: go through Manifold, never around it (no raw Lambda "
        "API calls, no hand-rolled SSH for long work). Launches are "
        "asynchronous: launch_gpu returns a launch id immediately; then "
        "call wait_for_launch to block until it is 'active' or 'failed' "
        "(one call, not a poll loop - large GPU instances can take 15-40 "
        "min to boot). Termination may be blocked by a safety hook if "
        "unsaved files exist on the instance; sync_outputs saves them. "
        "Pass a short `note` with each call saying why - it lands in the "
        "audit log the user reviews. If list_instances, list_filesystems, "
        "or list_launch_options responses carry \"mock\": true, the backend "
        "is in demo mode: everything is fixture data, no real GPUs or spend "
        "- say so instead of acting on it as production state."
    ),
)

# Injectable for tests: tests replace this with an ASGI-transport client
# aimed at an in-process app instance.
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=API_URL, timeout=60.0,
            headers=({"Authorization": f"Bearer {API_TOKEN}"}
                     if API_TOKEN else {}),
        )
    return _client


async def _audit(tool: str, args: dict, note: str, result: str) -> None:
    """Best-effort audit post; an unreachable backend already failed the
    real call, so audit failures must not mask the original error. Its own
    timeout is short: a wedged backend must not stack a second 60s wait on
    top of the failed call's (the MCP client kills the request at ~60s)."""
    try:
        await _http().post("/audit/agent", json={
            "tool": tool, "args": args, "note": note, "result": result[:500],
        }, timeout=5.0)
    except httpx.HTTPError:
        pass


async def _call(
    tool: str,
    method: str,
    path: str,
    *,
    note: str,
    args: dict[str, Any] | None = None,
    body: dict | None = None,
    params: dict | None = None,
    request_timeout: float | None = None,
) -> dict:
    """One guarded backend call + its audit entry. Backend rejections come
    back as {"error": <the backend's own message>} so the agent sees the
    same truth a human sees in the dashboard. Transport failures (backend
    down or restarting) additionally carry `unreachable: true`, so a caller
    can tell "the backend said no" from "the backend didn't answer".

    `request_timeout` overrides the client's default 60s ceiling for calls
    that legitimately hold the socket longer (the wait_for_launch long-poll
    parks server-side for up to 300s)."""
    args = args or {}
    try:
        resp = await _http().request(
            method, path, json=body, params=params,
            **({"timeout": request_timeout} if request_timeout else {}),
        )
        # A healthy backend answers JSON. A 500 that escaped the route,
        # though, is a Starlette plain-text "Internal Server Error" page —
        # calling .json() on that raises a cryptic "Expecting value" decode
        # error that would surface to the agent instead of the real status.
        # Fall back to the body text so the status-code branch below can
        # report something actionable.
        try:
            payload = resp.json() if resp.content else {}
        except ValueError:
            payload = {"detail": (resp.text or "").strip()[:300]
                       or f"HTTP {resp.status_code} (non-JSON response)"}
    except httpx.HTTPError as exc:
        result = {
            "error": f"Manifold backend unreachable at {API_URL}: {exc}",
            "unreachable": True,
        }
        await _audit(tool, args, note, result["error"])
        return result
    if resp.status_code >= 400:
        result = {"error": payload.get("detail", f"HTTP {resp.status_code}")}
        # Termination safety hook: return the evidence, not just the error.
        if payload.get("blocked"):
            result["blocked"] = True
            result["unpersisted_files"] = payload.get("unpersisted_files", [])
        await _audit(tool, args, note, f"rejected: {result['error']}")
        return result
    await _audit(tool, args, note, "ok")
    return payload


# -- onboarding --------------------------------------------------------------------


@mcp.tool()
async def get_skill(note: str = "") -> str:
    """The Manifold playbook for AI agents: task recipes (launch a GPU,
    serve a model, batch work, fine-tune, browse files, clean up) and the
    rules that keep work safe and cheap. Call this ONCE at the start of a
    session, before other Manifold tools."""
    try:
        resp = await _http().get("/skill")
    except httpx.HTTPError as exc:
        return f"Manifold backend unreachable at {API_URL}: {exc}"
    if resp.status_code >= 400:
        return f"skill document unavailable (HTTP {resp.status_code})"
    await _audit("get_skill", {}, note, "ok")
    return resp.text


@mcp.tool()
async def get_work_log(limit: int = 20, note: str = "") -> dict:
    """What Manifold accomplished recently: one markdown entry per settled
    job and autopilot run (template, GPU, runtime, cost, outputs, errors).
    Call this at the start of a session to know what previous sessions -
    including other agents and local models - already did, instead of
    re-deriving or redoing their work."""
    return await _call(
        "get_work_log", "GET", "/worklog",
        note=note, args={"limit": limit}, params={"limit": limit},
    )


# -- instances -------------------------------------------------------------------


@mcp.tool()
async def list_launch_options(note: str = "") -> dict:
    """Launchable {instance_type, region, filesystem} targets Lambda can
    satisfy RIGHT NOW, ranked best-first. CALL THIS BEFORE launch_gpu: it is
    the only way to see which instance types have capacity in which regions,
    and it keeps you from guessing a region that has no capacity or no
    filesystem.

    A launch needs the three to line up — the type must have capacity in the
    region, and a persistent filesystem is region-locked, so it can only be
    used from its own region. Each returned target is a combination that lines
    up, so you can copy one straight into launch_gpu.

    `targets` is ranked: co-located with your EXISTING data first (a filesystem
    that already holds files, so a job runs next to what it reads/writes), then
    co-located with an empty filesystem, then scratch-only (capacity but no
    filesystem there — everything on it is ephemeral), and cheaper first within
    each band. A target's `filesystem` is null for a scratch-only launch; pass
    "" as launch_gpu's filesystem for those. `unavailable` lists types with no
    capacity anywhere right now (retry later or pick another from `targets`)."""
    return await _call(
        "list_launch_options", "GET", "/launch-options", note=note,
    )


@mcp.tool()
async def launch_gpu(
    instance_type: str,
    region: str,
    filesystem: str,
    connection_mode: str | None = None,
    idle_timeout_seconds: float | None = None,
    max_lifetime_seconds: float | None = None,
    purpose: str = "",
    note: str = "",
) -> dict:
    """Launch a GPU instance. Flows through ALL backend guards (budget,
    concurrency, region-filesystem match); a rejection returns the guard's
    message in `error`. Returns a launch record — poll get_launch_status
    with its `id` until status is 'active' (SSH up) or 'failed'.

    Call list_launch_options FIRST and pass one of its targets: it returns
    only {type, region, filesystem} combinations that have capacity right now
    and are co-located with your data, which avoids a blind region guess that
    fails on capacity or a region-filesystem mismatch.

    `max_lifetime_seconds` is an optional hard ceiling on the instance's TOTAL
    lifetime, timed from the moment the provider accepts the launch, so it
    includes boot (15-40 minutes on a big box) — the backend rejects a value
    that does not cover the boot budget rather than quietly raising it. Unlike
    the idle timeout, nothing on the instance can push it out, and it applies
    even to a box serving a model. Manifold terminates at the ceiling if it
    can reach the instance and save its files first.

    ALWAYS pass `purpose`: a short phrase saying what this box is for, e.g.
    "Tally extraction+evaluation run" or "Red Hope mesh cleanup batch". You
    are probably not the only agent on this account, and purpose is what
    everyone else sees in list_instances. A box with no purpose reads as
    unexplained, and an unexplained box gets terminated by someone trying to
    be helpful — that has already happened here and cost a multi-hour run."""
    body = {
        "instance_type": instance_type,
        "region": region,
        "filesystem": filesystem,
    }
    if purpose:
        body["purpose"] = purpose
    if connection_mode:
        body["connection_mode"] = connection_mode
    if idle_timeout_seconds is not None:
        body["idle_timeout_seconds"] = idle_timeout_seconds
    if max_lifetime_seconds is not None:
        body["max_lifetime_seconds"] = max_lifetime_seconds
    return await _call(
        "launch_gpu", "POST", "/instances",
        note=note, args=body, body=body,
    )


@mcp.tool()
async def get_launch_status(launch_id: str, note: str = "") -> dict:
    """Progress of an asynchronous launch: launching -> retrying (capacity)
    -> booting -> active | failed. Returns a stable `phase`
    (requesting_capacity | retrying_capacity | waiting_for_active | ready |
    failed | terminated), a human `phase_detail`, and `settled` (true once
    nothing more will change). While booting it also returns
    boot_elapsed_seconds / boot_timeout_seconds / boot_remaining_seconds.
    For a slow boot, prefer wait_for_launch: one blocking call instead of a
    poll loop."""
    return await _call(
        "get_launch_status", "GET", f"/launches/{launch_id}",
        note=note, args={"launch_id": launch_id},
    )


@mcp.tool()
async def wait_for_launch(launch_id: str, timeout: float = 45,
                          note: str = "") -> dict:
    """Wait (up to `timeout` seconds, max 50) for a launch to settle
    (active | failed | terminated), then return the same enriched record
    as get_launch_status. Each call is deliberately bounded to fit inside
    MCP client request timeouts (~60s): a still-booting result
    (settled=false, with boot_elapsed/remaining seconds) is NORMAL for big
    GPUs - just call wait_for_launch again to keep waiting; the wait parks
    server-side, not in a poll loop. A backend restart mid-wait (dev
    --reload) is absorbed: the wait reconnects and keeps parking; the
    launch itself is resumed by the backend and keeps booting either
    way."""
    timeout = max(1.0, min(float(timeout), 50.0))
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = max(1.0, deadline - loop.time())
        result = await _call(
            "wait_for_launch", "GET", f"/launches/{launch_id}/wait",
            note=note, args={"launch_id": launch_id, "timeout": timeout},
            params={"timeout": remaining},
            # The server parks up to `remaining`; give the socket headroom
            # past that so a legitimate long park is not misread as death.
            request_timeout=remaining + 15.0,
        )
        # A restart mid-park drops the socket. The launch is unharmed (the
        # backend resumes it on startup), so reconnect and keep waiting
        # instead of alarming the caller with a transport error.
        if not result.get("unreachable"):
            return result
        if loop.time() >= deadline:
            return {
                "status": "unknown",
                "settled": False,
                "phase": "backend_restarting",
                "phase_detail": (
                    "The Manifold backend was unreachable for the whole wait "
                    "window (it may be restarting). The launch itself is not "
                    "affected - the backend resumes in-flight boots on "
                    "startup. Call wait_for_launch again."
                ),
            }
        await asyncio.sleep(2.0)


@mcp.tool()
async def list_instances(note: str = "") -> dict:
    """Running instances with live status, SSH connection state, GPU type,
    region, and hourly rate.

    Also `created_by` (which principal launched it), `purpose` (what they
    said it is for), and `activity` — the idle sweep's own verdict, with a
    `state` (loading | serving | batch_running | auto_managed | keep_alive |
    idle_countdown | unreachable | unknown), a `busy` flag, and the reason
    in words. Read those three before acting on anyone's instance but your
    own. busy=null means the sweep could not tell, which is not the same as
    false and must not be treated as one."""
    return await _call("list_instances", "GET", "/instances", note=note)


@mcp.tool()
async def terminate_instance(
    instance_id: str, force: bool = False, confirm_owner: str = "",
    note: str = ""
) -> dict:
    """Terminate an instance. With force=false the safety hook checks for
    unsaved files in ephemeral scratch first: if any exist, this returns
    blocked=true with the file list INSTEAD of terminating. Either call
    sync_outputs then retry, or pass force=true to accept the loss.

    ONLY TERMINATE BOXES YOU LAUNCHED. Check `created_by` and `purpose` in
    list_instances first. An instance launched by another principal is
    refused with refused=true, the owner's name, and what they said it is
    for; confirm_owner=<that name> overrides, and is for when you are sure,
    not for when you are unsure.

    A box that looks idle is the case to be most careful about, not least.
    Read `activity` in list_instances before concluding anything: a model
    server loading its weights has no user processes, writes nothing, and
    holds the GPU for 10+ minutes, which is indistinguishable from an
    abandoned box by every signal except that one. If you believe someone
    else's instance should die, ask the human rather than deciding."""
    params: dict = {"force": str(force).lower()}
    if confirm_owner:
        params["confirm_owner"] = confirm_owner
    return await _call(
        "terminate_instance", "DELETE", f"/instances/{instance_id}",
        note=note,
        args={"instance_id": instance_id, "force": force,
              "confirm_owner": confirm_owner},
        params=params,
    )


@mcp.tool()
async def sync_outputs(instance_id: str, note: str = "") -> dict:
    """rsync the instance's ephemeral scratch to the persistent filesystem
    (ephemeral-backup/), over the managed SSH connection."""
    return await _call(
        "sync_outputs", "POST", f"/instances/{instance_id}/sync",
        note=note, args={"instance_id": instance_id},
    )


@mcp.tool()
async def get_spend(note: str = "") -> dict:
    """What Manifold's launches have cost: today, this week, month to date,
    all time, and the rate money is burning at right now ($/hour).

    Call this BEFORE launching anything expensive. An agent that can start
    GPUs but cannot see the bill cannot limit itself; this is how you check
    what the session has already spent, and how you notice a box that is
    still running.

    Two honest limits on every number here:

    - It is MANIFOLD-OBSERVED. Only launches Manifold itself started are
      counted. An instance created in the Lambda console, and filesystem
      storage (billed per GiB for as long as it exists), are not in these
      figures. `lower_bound` says so in the response.
    - It is an UPPER BOUND on what it does count: the clock starts when the
      cloud accepted the launch, while billing really starts a little later,
      when the instance passes health checks. Over-reporting is the safe
      direction for a spend guard; `disclaimer` carries the same sentence
      the dashboard shows.

    Costs that cannot be known are reported as `unresolved` (a low/high
    range, with launch ids) and `rate_unknown` (a count) — never folded into
    a total as $0. Report them as unknown too; do not treat them as free.
    Times are bucketed in UTC. `mock: true` means fixture data, not spend.
    """
    return await _call("get_spend", "GET", "/spend/summary", note=note)


# -- jobs -----------------------------------------------------------------------


@mcp.tool()
async def list_templates(note: str = "") -> dict:
    """Job templates with parameter schemas (name, type, default, required).
    Templates run as Docker containers on the instance with the GPU attached."""
    return await _call("list_templates", "GET", "/templates", note=note)


@mcp.tool()
async def run_job(template: str, parameters: dict, note: str = "",
                  depends_on: list[str] | None = None) -> dict:
    """Enqueue a job from a template. Parameters are validated against the
    template schema immediately. The job runs on the connected instance;
    poll get_job_status. Logs stream to get_job_logs.

    depends_on chains jobs into a pipeline: pass task ids from earlier
    run_job calls and this job waits until ALL of them succeed, settling as
    'skipped' if any of them fails. Deps must already exist and may not be
    servers (a server never exits; to run a batch job against a live server,
    just run it - server and batch coexist on an instance by design)."""
    body: dict = {"template": template, "parameters": parameters}
    if depends_on:
        body["depends_on"] = depends_on
    return await _call(
        "run_job", "POST", "/tasks",
        note=note, args={"template": template, "parameters": parameters,
                         **({"depends_on": depends_on} if depends_on else {})},
        body=body,
    )


@mcp.tool()
async def save_template(yaml_text: str, note: str = "") -> dict:
    """Create or update a CUSTOM job template from raw YAML, so a workflow
    you have proven by hand becomes a one-click recipe the user can rerun
    from the Jobs page without any agent involved. Validated exactly like
    bundled templates (image, command with {{param}} placeholders, parameter
    schema; volume mounts only under /workspace/ephemeral or {persistent};
    ports always loopback-bound). Returns the parsed template or the
    validation error. Prefer parameterizing over hardcoding: a template with
    good parameters serves the user forever."""
    return await _call(
        "save_template", "POST", "/templates/custom",
        note=note, args={"yaml": f"({len(yaml_text)} chars)"},
        body={"yaml": yaml_text},
    )


@mcp.tool()
async def generate_training_config(spec: str, dataset: str, brain: str,
                                   student_model: str = "",
                                   note: str = "") -> dict:
    """Turn a plain-words distillation goal into an axolotl LoRA training
    config, written by a model and checked by the backend.

    `spec` is what the student should learn, in the user's words, e.g.
    "distill film-shot tagging into a 3B LoRA that fits an A10". `dataset`
    is the bare filename of the curated training set under
    <filesystem>/synthesized (llm-judge writes kept-<name>.jsonl there).
    `brain` is a ref from the backend's brain registry: "cli:claude",
    "cli:codex", "cli:gemini", "api:<name>", "local:<endpoint>/<model>", or
    "instance:<instance_id>" for a model already served on a GPU.
    `student_model` optionally pins the base model; leave it empty and the
    brain picks from Manifold's curated shelf of small open bases.

    REVIEW ONLY: nothing is written to the filesystem and no training
    starts. Show the returned YAML to the user first. When they approve,
    upload it with upload_file to configs/<name>.yaml (the response's
    suggested_path) and then run_job axolotl-finetune. `advisories` lists
    things worth saying out loud before they spend a GPU hour.

    The backend rejects a config that names an unvetted base model, points
    at a path the job does not mount, or asks to run remote code; the error
    says which. That is a real rejection, not a hiccup: fix the spec or the
    brain's answer, do not retry blindly."""
    return await _call(
        "generate_training_config", "POST", "/distill/config",
        note=note,
        args={"dataset": dataset, "brain": brain,
              "student_model": student_model,
              "spec": f"({len(spec)} chars)"},
        body={"spec": spec, "dataset": dataset, "brain": brain,
              "student_model": student_model},
        # A CLI brain is allowed to think for up to 280s server-side; the
        # client's default 60s would abandon the call mid-answer.
        request_timeout=300.0,
    )


@mcp.tool()
async def delete_template(name: str, note: str = "") -> dict:
    """Delete a CUSTOM template by name. Bundled templates cannot be
    deleted; if the custom one was overriding a bundled name, the bundled
    version is restored."""
    return await _call(
        "delete_template", "DELETE", f"/templates/custom/{name}",
        note=note, args={"name": name},
    )


@mcp.tool()
async def run_command(instance_id: str, command: str, timeout: float = 45,
                      note: str = "") -> dict:
    """Run ONE shell command on the instance over the managed SSH connection
    and return {exit_code, stdout, stderr}. Full shell parity, but audited:
    every command lands in the user's activity log with its exit code, which
    raw SSH would not. `timeout` is capped at 50s so the response always
    arrives before an MCP client request timeout (~60s). Use this for the
    quick real commands in between: inspecting files, checking nvidia-smi,
    preparing directories. Anything longer belongs in a job (run_job
    streams logs and survives restarts) - or start it detached
    (`nohup ... > log 2>&1 &`) and check the log with a later
    run_command."""
    timeout = max(1.0, min(float(timeout), 50.0))
    return await _call(
        "run_command", "POST", f"/instances/{instance_id}/run",
        note=note, args={"instance_id": instance_id, "command": command[:200]},
        body={"command": command, "timeout": timeout},
        request_timeout=timeout + 10.0,
    )


@mcp.tool()
async def get_job_status(task_id: str, note: str = "") -> dict:
    """Job state (queued|running|succeeded|failed|skipped), exit code, and the
    persistent output paths it writes to."""
    return await _call(
        "get_job_status", "GET", f"/tasks/{task_id}",
        note=note, args={"task_id": task_id},
    )


@mcp.tool()
async def get_job_logs(task_id: str, tail: int = 100, note: str = "") -> dict:
    """The last `tail` log lines of a job (live while it runs)."""
    return await _call(
        "get_job_logs", "GET", f"/tasks/{task_id}/logs",
        note=note, args={"task_id": task_id, "tail": tail},
        params={"tail": tail},
    )


# -- storage ---------------------------------------------------------------------


@mcp.tool()
async def list_filesystems(note: str = "") -> dict:
    """Persistent filesystems with their regions. Filesystems are
    region-locked: an instance can only mount one in its own region."""
    return await _call("list_filesystems", "GET", "/filesystems", note=note)


@mcp.tool()
async def create_filesystem(name: str, region: str, note: str = "") -> dict:
    """Create a persistent filesystem in a region (e.g. before launching in
    a region that has capacity but no filebase yet). Creation is free;
    storage bills by the GB-month actually used. Region must be one of the
    codes from list_launch_options / the regions the account can see."""
    return await _call(
        "create_filesystem", "POST", "/filesystems", note=note,
        args={"name": name, "region": region},
        body={"name": name, "region": region},
    )


async def _connected_instance_for_fs(filesystem: str | None) -> tuple | None:
    """A connected instance that mounts `filesystem` (or any, if None), as
    (instance_id, filesystem_name). None when nothing suitable is connected.
    Lets file browsing ride the SSH connection with no S3 keys."""
    listing = await _call("list_persistent_files", "GET", "/instances", note="")
    for inst in listing.get("instances", []):
        if inst.get("connection_state") != "connected":
            continue
        mounts = inst.get("filesystems") or []
        if filesystem is None:
            if mounts:
                return inst["id"], mounts[0]
        elif filesystem in mounts:
            return inst["id"], filesystem
    return None


@mcp.tool()
async def list_persistent_files(
    prefix: str = "", filesystem: str | None = None, note: str = ""
) -> dict:
    """Browse one directory level of a persistent filesystem.

    Prefers a RUNNING instance: if one is connected and mounts the target
    filesystem, this browses over its SSH connection (via the sidecar, at
    local-disk speed and needing NO S3 keys) — the same path the dashboard's
    per-instance Files panel uses. It returns {source, filesystem, root, path,
    entries:[{name, is_dir, size_bytes, modified}]}. `prefix` is relative to
    the filesystem, e.g. "outputs/images".

    Only when no connected instance mounts the filesystem does it fall back to
    Lambda's S3 "Files" API — which CAN browse with nothing running, but needs
    S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY in .env and returns {filesystem,
    files:[...]}. If `filesystem` is omitted and exactly one exists (or one
    instance is connected), it is used."""
    # 1) Keyless path: a connected instance that mounts this filesystem.
    hit = await _connected_instance_for_fs(filesystem)
    if hit is not None:
        instance_id, fs_name = hit
        # The sidecar's persistent root is /lambda/nfs (the filesystem's
        # PARENT), so its first path segment is the filesystem name; prepend
        # it to keep `prefix` filesystem-relative like the S3 path.
        sub = "/".join(p for p in [fs_name, prefix.strip("/")] if p)
        result = await _call(
            "list_persistent_files", "GET",
            f"/instances/{instance_id}/files/list",
            note=note, args={"filesystem": fs_name, "prefix": prefix},
            params={"root_name": "persistent", "path": sub},
        )
        if "error" not in result:
            result["filesystem"] = fs_name
            result["source"] = f"instance:{instance_id} (ssh, no S3 keys)"
        return result

    # 2) S3 fallback (needs keys; works with no instance running).
    if filesystem is None:
        listing = await _call(
            "list_persistent_files", "GET", "/filesystems", note="",
        )
        names = [f["name"] for f in listing.get("filesystems", [])]
        if len(names) != 1:
            result = {
                "error": f"Specify `filesystem`; available: {', '.join(names) or '(none)'}"
            }
            await _audit("list_persistent_files",
                         {"prefix": prefix}, note, result["error"])
            return result
        filesystem = names[0]
    result = await _call(
        "list_persistent_files", "GET", "/storage/files",
        note=note, args={"filesystem": filesystem, "prefix": prefix},
        params={"filesystem": filesystem, "prefix": prefix},
    )
    # No S3 keys AND no connected instance: point at the keyless route.
    if result.get("error") and "credential" in result["error"].lower():
        result["hint"] = (
            "No S3 Files keys configured. Launch or connect an instance that "
            "mounts this filesystem, then this browses it over SSH with no keys."
        )
    return result


async def _pick_instance(instance_id: str | None, tool: str, note: str,
                         args: dict) -> str | dict:
    """Use the given instance, or auto-select when exactly one is connected."""
    if instance_id:
        return instance_id
    listing = await _call(tool, "GET", "/instances", note="", args={})
    if listing.get("error"):
        # An unreachable backend must surface as exactly that - reporting
        # "connected instances: (none)" here would present a dead backend
        # as a healthy account with nothing running.
        return listing
    connected = [i["id"] for i in listing.get("instances", [])
                 if i.get("connection_state") == "connected"]
    if len(connected) != 1:
        result = {"error": f"Specify `instance_id`; connected instances: "
                           f"{', '.join(connected) or '(none)'}"}
        await _audit(tool, args, note, result["error"])
        return result
    return connected[0]


@mcp.tool()
async def upload_file(local_path: str, remote_path: str = "inbox/",
                      instance_id: str | None = None, note: str = "") -> dict:
    """Upload a file from THIS machine to an instance over the managed SSH
    connection. remote_path ending in '/' keeps the filename; relative
    paths land on the persistent filesystem (surviving termination).
    If instance_id is omitted and exactly one instance is connected, it is
    used."""
    args = {"local_path": local_path, "remote_path": remote_path,
            "instance_id": instance_id}
    if not os.path.isfile(local_path):
        result = {"error": f"local file not found: {local_path}"}
        await _audit("upload_file", args, note, result["error"])
        return result
    target = await _pick_instance(instance_id, "upload_file", note, args)
    if isinstance(target, dict):
        return target
    try:
        with open(local_path, "rb") as fh:
            resp = await _http().post(
                f"/instances/{target}/files/upload",
                files={"file": (os.path.basename(local_path), fh)},
                data={"dest": remote_path},
            )
        # Same guard as _call: a 500 that escaped the route is a plain-text
        # page, and .json() on it would raise instead of reporting the status.
        try:
            payload = resp.json() if resp.content else {}
        except ValueError:
            payload = {"detail": (resp.text or "").strip()[:300]
                       or f"HTTP {resp.status_code} (non-JSON response)"}
    except httpx.HTTPError as exc:
        result = {"error": f"upload failed: {exc}"}
        await _audit("upload_file", args, note, result["error"])
        return result
    if resp.status_code >= 400:
        result = {"error": payload.get("detail", f"HTTP {resp.status_code}")}
        await _audit("upload_file", args, note, f"rejected: {result['error']}")
        return result
    await _audit("upload_file", args, note,
                 f"ok: {payload.get('bytes', 0)} bytes -> {payload.get('path')}")
    return payload


# Resumable download tuning. Each ranged request stays small enough to
# finish quickly; the whole tool call returns before a typical MCP client
# request timeout (~60s) and reports progress instead of erroring, so a
# large file is fetched by calling download_file repeatedly (it resumes
# from the .part file automatically). A 127MB batch output used to require
# manual split/reassemble on the instance; this bakes that pattern in.
DOWNLOAD_CHUNK_BYTES = 16 * 1024 * 1024
DOWNLOAD_TIME_BUDGET_SECONDS = 40.0


@mcp.tool()
async def download_file(remote_path: str, local_path: str,
                        instance_id: str | None = None, note: str = "") -> dict:
    """Download a file from an instance to THIS machine over the managed
    SSH connection. Relative remote paths read from the persistent
    filesystem. If instance_id is omitted and exactly one instance is
    connected, it is used.

    Large files are safe here: the transfer runs in bounded chunks and
    this returns complete=false with progress before any client timeout
    would hit. When you see complete=false, just call download_file again
    with the SAME arguments - it resumes from where it stopped (progress
    lives in <local_path>.part until the file is whole and verified by
    size, then it is moved into place)."""
    args = {"remote_path": remote_path, "local_path": local_path,
            "instance_id": instance_id}
    target = await _pick_instance(instance_id, "download_file", note, args)
    if isinstance(target, dict):
        return target

    part_path = local_path + ".part"
    parent = os.path.dirname(os.path.abspath(local_path))
    os.makedirs(parent, exist_ok=True)
    offset = os.path.getsize(part_path) if os.path.exists(part_path) else 0

    loop = asyncio.get_event_loop()
    started = loop.time()
    total = None
    try:
        while True:
            async with _http().stream(
                "GET", f"/instances/{target}/files/download",
                params={"path": remote_path, "offset": offset,
                        "max_bytes": DOWNLOAD_CHUNK_BYTES},
            ) as resp:
                if resp.status_code == 416:
                    # The remote file changed under our .part: start over.
                    await _audit("download_file", args, note,
                                 "remote file changed; restarting from 0")
                    os.remove(part_path)
                    offset = 0
                    continue
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    result = {"error": body[:300] or f"HTTP {resp.status_code}"}
                    await _audit("download_file", args, note,
                                 f"rejected: {result['error'][:150]}")
                    return result
                total = int(resp.headers["X-File-Size"])
                with open(part_path, "ab") as fh:
                    async for chunk in resp.aiter_bytes():
                        fh.write(chunk)
                offset = os.path.getsize(part_path)

            if offset >= total:
                os.replace(part_path, local_path)
                await _audit("download_file", args, note,
                             f"ok: {offset} bytes -> {local_path}")
                return {"local_path": local_path, "bytes": offset,
                        "complete": True}
            if loop.time() - started > DOWNLOAD_TIME_BUDGET_SECONDS:
                await _audit(
                    "download_file", args, note,
                    f"in progress: {offset}/{total} bytes")
                return {
                    "complete": False,
                    "bytes_done": offset,
                    "total_bytes": total,
                    "detail": (
                        f"{offset}/{total} bytes so far. Call download_file "
                        f"again with the same arguments to resume - progress "
                        f"is kept in {part_path}."
                    ),
                }
    except httpx.HTTPError as exc:
        result = {
            "error": f"download interrupted: {exc}",
            "detail": (f"progress is kept in {part_path}; call "
                       f"download_file again to resume"),
        }
        await _audit("download_file", args, note, result["error"])
        return result


async def _task_settled(task_id: str) -> bool:
    """True once the task reached a terminal state. Best-effort: any error
    (unreachable backend, bad status) reads as 'not settled', leaving the
    follow loop's own deadline and error handling to decide what to do."""
    try:
        resp = await _http().get(f"/tasks/{task_id}", timeout=15.0)
        if resp.status_code >= 400:
            return False
        status = resp.json().get("status")
    except (httpx.HTTPError, ValueError):
        return False
    return status in ("succeeded", "failed", "skipped")


@mcp.tool()
async def stream_job_logs(task_id: str, max_seconds: float = 1800.0,
                          note: str = "") -> dict:
    """Follow a job's logs until it finishes (or max_seconds elapses), then
    return every collected line in order. Useful for agents tracking long
    training or fine-tuning runs.

    Poll-based on the log sequence cursor rather than a long-held SSE socket:
    each HTTP call is short, so a run that goes quiet for minutes between log
    lines never trips a request timeout (the old SSE version errored after
    30s of quiet)."""
    args = {"task_id": task_id, "max_seconds": max_seconds}
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_seconds
    last_seq = -1
    lines: list[dict] = []
    try:
        while True:
            resp = await _http().get(f"/tasks/{task_id}/logs", timeout=15.0)
            if resp.status_code >= 400:
                body = (resp.text or "").strip()
                result = {"error": body[:300] or f"HTTP {resp.status_code}"}
                await _audit("stream_job_logs", args, note,
                             f"rejected: {result['error']}")
                return result
            for line in resp.json().get("lines", []):
                if line.get("seq", 0) > last_seq:
                    last_seq = line["seq"]
                    lines.append(line)
            if await _task_settled(task_id) or loop.time() >= deadline:
                break
            await asyncio.sleep(2.0)
    except httpx.HTTPError as exc:
        result = {"error": f"streaming failed: {exc}"}
        await _audit("stream_job_logs", args, note, result["error"])
        return result
    await _audit("stream_job_logs", args, note,
                 f"ok: {len(lines)} lines streamed")
    return {"task_id": task_id, "lines": lines, "count": len(lines)}


@mcp.tool()
async def stream_task_events(task_id: str, max_seconds: float = 1800.0,
                             note: str = "") -> dict:
    """Follow a task's lifecycle events (queued -> launched -> started ->
    finished/failed) until it settles (or max_seconds elapses), then return
    every event once, in order.

    Poll-based on the event-id cursor rather than a long-held SSE socket:
    each HTTP call is short, so a training run's long quiet period never
    trips a request timeout (the old SSE version errored after 30s of
    quiet)."""
    args = {"task_id": task_id, "max_seconds": max_seconds}
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_seconds
    seen_ids: set = set()
    events: list[dict] = []
    try:
        while True:
            resp = await _http().get(f"/tasks/{task_id}/events", timeout=15.0)
            if resp.status_code >= 400:
                body = (resp.text or "").strip()
                result = {"error": body[:300] or f"HTTP {resp.status_code}"}
                await _audit("stream_task_events", args, note,
                             f"rejected: {result['error']}")
                return result
            for ev in resp.json().get("events", []):
                if ev.get("id") not in seen_ids:
                    seen_ids.add(ev.get("id"))
                    events.append(ev)
            if await _task_settled(task_id) or loop.time() >= deadline:
                break
            await asyncio.sleep(2.0)
    except httpx.HTTPError as exc:
        result = {"error": f"streaming failed: {exc}"}
        await _audit("stream_task_events", args, note, result["error"])
        return result
    await _audit("stream_task_events", args, note,
                 f"ok: {len(events)} events streamed")
    return {"task_id": task_id, "events": events, "count": len(events)}


@mcp.tool()
async def get_pending_approvals(note: str = "") -> dict:
    """List actions waiting on a human Approve/Deny (gated launches or spend-heavy actions)."""
    return await _call("get_pending_approvals", "GET", "/approvals/pending", note=note)


@mcp.tool()
async def decide_approval(approval_id: str, approve: bool, note: str = "") -> dict:
    """Approve or reject a pending approval request."""
    body = {"approve": approve}
    return await _call(
        "decide_approval", "POST", f"/approvals/{approval_id}",
        note=note, args={"approval_id": approval_id, "approve": approve}, body=body,
    )


@mcp.tool()
async def launch_cluster(
    instance_type: str,
    region: str,
    filesystem: str,
    node_count: int,
    name: str = "",
    note: str = "",
) -> dict:
    """Launch an elastic multi-node GPU cluster (head + worker nodes)."""
    body = {
        "instance_type": instance_type,
        "region": region,
        "filesystem": filesystem,
        "node_count": node_count,
        "name": name,
    }
    return await _call("launch_cluster", "POST", "/clusters/launch", note=note, body=body)


@mcp.tool()
async def list_clusters(note: str = "") -> dict:
    """List all active and historical multi-node GPU clusters."""
    return await _call("list_clusters", "GET", "/clusters", note=note)


@mcp.tool()
async def get_cluster_details(cluster_id: str, note: str = "") -> dict:
    """Get detailed state and node list for a multi-node GPU cluster."""
    return await _call("get_cluster_details", "GET", f"/clusters/{cluster_id}", note=note)


@mcp.tool()
async def terminate_cluster(cluster_id: str, force: bool = False, note: str = "") -> dict:
    """Safely terminate a multi-node GPU cluster and all associated instances."""
    args = {"cluster_id": cluster_id, "force": force}
    return await _call("terminate_cluster", "POST", f"/clusters/{cluster_id}/terminate?force={str(force).lower()}", note=note, args=args)


@mcp.tool()
async def dispatch_local_subagent(model: str, prompt: str, tools: list[dict] | None = None, note: str = "") -> dict:
    """Dispatch a task to a local model subagent."""
    body = {"model": model, "prompt": prompt}
    if tools:
        body["tools"] = tools
    return await _call("dispatch_local_subagent", "POST", "/subagents/dispatch", note=note, body=body)

@mcp.tool()
async def list_local_subagent_models(note: str = "") -> dict:
    """List available local GPU models & active subagent endpoints."""
    return await _call("list_local_subagent_models", "GET", "/subagents/models", note=note)

@mcp.tool()
async def get_swarm_status(note: str = "") -> dict:
    """Returns swarm health, queue depth, and active subagent stats."""
    return await _call("get_swarm_status", "GET", "/subagents/swarm/status", note=note)


@mcp.tool()
async def agent_handshake(session_id: str, protocol: str = "manifold-v1", note: str = "") -> dict:
    """Negotiates agent handshake and returns AgentContext."""
    body = {"session_id": session_id, "protocol": protocol}
    return await _call("agent_handshake", "POST", "/agent/handshake", note=note, body=body)

@mcp.tool()
async def get_agent_context(session_id: str, note: str = "") -> dict:
    """Retrieves active agent context."""
    return await _call("get_agent_context", "GET", f"/agent/context/{session_id}", note=note)

@mcp.tool()
async def update_agent_context(
    session_id: str,
    workspace_environment: dict | None = None,
    active_gpu_connections: dict | None = None,
    task_graphs: dict | None = None,
    note: str = ""
) -> dict:
    """Updates context variables. There is no session_tokens field: secrets
    stay in .env, never in the agent context."""
    body = {}
    if workspace_environment is not None:
        body["workspace_environment"] = workspace_environment
    if active_gpu_connections is not None:
        body["active_gpu_connections"] = active_gpu_connections
    if task_graphs is not None:
        body["task_graphs"] = task_graphs
    return await _call("update_agent_context", "POST", f"/agent/context/{session_id}/update", note=note, body=body)


def main() -> None:
    mcp.run()   # stdio transport


if __name__ == "__main__":
    main()
