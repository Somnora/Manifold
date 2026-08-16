"""Autopilot: an agent loop driven by a model served ON an instance.

The brain is any OpenAI-compatible model server Manifold itself launched
(vllm-serve). Each turn, the loop sends the conversation to the brain over
the managed SSH connection, expects EXACTLY ONE JSON action back, executes
it against Manifold's own guarded operations, and feeds the observation to
the next turn. GPU A literally manages GPU B.

Safety model — same philosophy as the MCP bridge, one level deeper:
- The action surface is a fixed allowlist below. There is no shell action,
  no arbitrary HTTP, no self-modification.
- Every launch goes through orchestrator.request_launch: budget,
  concurrency, and region guards bind the autopilot exactly as they bind
  the dashboard. A rejection comes back as an observation the model can
  read and adapt to.
- Hard step cap per run (config: autopilot.max_steps_cap), a wait cap so
  the loop cannot sleep forever, and a per-turn chat timeout.
- Every step is persisted (agent_steps) and audited (actor "autopilot"),
  so the dashboard shows the loop as it happens. Runs are cancellable.
- A protocol that small open-weight models can actually follow: one JSON
  object per turn, errors returned as data. Malformed output is bounced
  back with a correction hint; three consecutive failures end the run.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any

from .auth import bind_principal, current_principal
from .config import Settings
from .db import Database, utcnow
from .dispatcher import ParameterError, coerce_parameters
from .lambda_api import LambdaAPIError
from .model_client import ModelClientError
from .orchestrator import LaunchRejected, Orchestrator, TerminationBlocked
from .preferences import GATEABLE_ACTIONS
from .task_queue import TaskQueue
from .templates import JobTemplate

logger = logging.getLogger("manifold.autopilot")

MAX_CONSECUTIVE_FAILURES = 3
MAX_HISTORY_MESSAGES = 40      # system prompt + trailing window

# Actions that CHANGE something: they spend money, queue work, write a
# template, move files, or stop billing. Everything else the loop can call
# (list_*, get_*, wait) only reads. The split is what lets a finished run
# be judged by what it DID rather than by whether it managed to say "done".
EFFECTFUL_ACTIONS = frozenset({
    "launch_gpu", "run_job", "save_template", "sync_outputs",
    "terminate_instance",
})


def _succeeded(step: dict) -> bool:
    """An action that came back with an "error" key changed nothing - the
    guards refused it, or the arguments were wrong. Only results without
    one count as having happened."""
    result = step.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            return False
    return isinstance(result, dict) and "error" not in result


def run_effect(steps: list[dict]) -> dict:
    """What a run actually DID, derived from its own recorded steps.

    Pure, and computed at READ time on purpose: it costs nothing, it needs
    no migration, and it reclassifies runs that finished before this
    existed - which is the point, because the runs that motivated it are
    already in the database.

    Why it exists: `done` was the only thing separating "succeeded" from
    everything else, so a brain that opened with a fabricated summary and
    called `done` scored the same green badge as a run that launched a
    GPU, ran a job, and cleaned up. Two real runs on the owner's machine
    ended `succeeded` having done nothing at all, while the one that did
    the whole job was labelled `exhausted` for running out of turns. The
    labels were exactly backwards, and the fix is to stop inferring
    accomplishment from the last word the model said.

    Returns:
      effect     "acted" if any effectful action actually went through,
                 else "no_effect"
      launched   this run started an instance
      terminated this run stopped one
    """
    launched = terminated = acted = False
    for step in steps or []:
        action = step.get("action")
        if action not in EFFECTFUL_ACTIONS or not _succeeded(step):
            continue
        acted = True
        if action == "launch_gpu":
            launched = True
        elif action == "terminate_instance":
            terminated = True
    return {
        "effect": "acted" if acted else "no_effect",
        "launched": launched,
        "terminated": terminated,
    }


def find_serving_task(queue: TaskQueue, templates: dict[str, JobTemplate],
                      instance_id: str) -> dict | None:
    """The running task on this instance whose template publishes a port —
    i.e. a live model server. Single source of truth for 'is a model
    being served here', shared by the chat endpoints and the autopilot."""
    for task in queue.list():
        if task["status"] != "running" or task["instance_id"] != instance_id:
            continue
        template = templates.get(task["template"])
        if template is None or not template.ports:
            continue
        return {
            **task,
            "port": template.ports[0].host,
            "model_id": task["parameters"].get("model_id") or task["template"],
        }
    return None


def parse_action(text: str) -> tuple[dict | None, str | None]:
    """Extract the first JSON object with an "action" key from model text.

    Tolerates code fences and prose around the JSON — small models add
    both. Returns (parsed, None) or (None, error_for_the_model)."""
    cleaned = text.replace("```json", "```").replace("```", " ")
    decoder = json.JSONDecoder()
    idx = cleaned.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(cleaned, idx)
        except json.JSONDecodeError:
            idx = cleaned.find("{", idx + 1)
            continue
        if isinstance(obj, dict) and isinstance(obj.get("action"), str):
            args = obj.get("args")
            if args is None:
                obj["args"] = {}
            elif not isinstance(args, dict):
                return None, '"args" must be a JSON object'
            return obj, None
        idx = cleaned.find("{", idx + 1)
    return None, (
        "Your reply contained no JSON action. Respond with exactly one "
        'JSON object: {"thought": "...", "action": "<name>", "args": {...}}'
    )


SYSTEM_PROMPT = """You are Manifold Autopilot, an agent operating a Lambda Cloud GPU \
orchestrator to accomplish the user's goal.

Respond with EXACTLY ONE JSON object per turn, nothing else:
{"thought": "<brief reasoning>", "action": "<name>", "args": {...}}

Actions:
- list_instance_types {} -> GPU types with $/hr and regions that have capacity
- list_instances {} -> running instances with connection state
- launch_gpu {"instance_type": str, "region": str, "filesystem": str} -> start a GPU (async; poll get_launch_status)
- get_launch_status {"launch_id": str} -> launching|retrying|booting|active|failed
- list_templates {} -> runnable job templates and their parameters
- run_job {"template": str, "parameters": {...}} -> queue a job on the connected instance
- save_template {"yaml": str} -> create a custom job template (image, command with {{param}} placeholders, parameter schema; mounts only under {persistent} or /workspace/ephemeral). Use when the goal needs a job no existing template covers - then run it with run_job. The template stays for the user afterwards, so parameterize it well.
- get_job_status {"task_id": str} -> queued|running|succeeded|failed|skipped + outputs
- get_job_logs {"task_id": str, "tail": int} -> recent log lines
- sync_outputs {"instance_id": str} -> save ephemeral scratch to persistent storage
- terminate_instance {"instance_id": str, "force": bool} -> stop billing; force=false first RESCUES ephemeral files (saving them to persistent storage), and is blocked only if some file still could not be saved
- wait {"seconds": number} -> pause before polling again
- done {"summary": str} -> finish the run; ALWAYS end with this

Rules:
- Results arrive as JSON in the next user message. An "error" key means the
  action was refused (budget, concurrency, region guards) - read it and adapt;
  do not repeat a refused action unchanged.
- GPUs cost real money. Prefer the cheapest type that fits. Terminate
  instances you started once the work is finished (sync first if needed).
- One action per turn. Be decisive; you have a limited number of steps.
- Some runs gate certain actions (launch_gpu / run_job / terminate_instance)
  behind HUMAN approval: such an action may take a while to answer, and a
  denial comes back as an error - respect it, adapt or finish instead of
  retrying it.

Goal: {goal}"""


class Autopilot:
    """Owns agent runs: starts the loop task, executes actions, records
    every step, and enforces the caps."""

    def __init__(self, settings: Settings, orchestrator: Orchestrator,
                 queue: TaskQueue, templates: dict[str, JobTemplate],
                 db: Database, *, sleep=asyncio.sleep, notifier=None,
                 template_saver=None, worklog=None):
        self.settings = settings
        self.orchestrator = orchestrator
        self.queue = queue
        self.templates = templates
        self.db = db
        self._sleep = sleep
        # NotificationCenter (optional): pings when an action pauses for
        # approval and when a run ends. An approval nobody hears about is a
        # stall, not a safety feature.
        self.notifier = notifier
        # Worklog (optional): every finished run becomes one markdown entry
        # other agents can read (see worklog.py).
        self.worklog = worklog
        # template_saver(yaml_text) -> JobTemplate: the same validated save
        # path the Jobs page and MCP use. Lets a run author the job it needs
        # (save_template action) instead of dead-ending when no bundled
        # template fits; what it saves persists for the user.
        self._template_saver = template_saver
        self.tasks: dict[str, asyncio.Task] = {}

    # -- lifecycle ------------------------------------------------------------------

    def start_run(self, *, goal: str, brain_ref: str,
                  brain_model: str, brain_port: int, max_steps: int,
                  client_fn=None,
                  gated_actions: frozenset[str] = frozenset(),
                  created_by: str | None = None) -> str:
        """client_fn() -> chat client for the brain, resolved per turn (an
        instance brain's connection can be replaced mid-run). None keeps
        the legacy instance resolution via the orchestrator.

        gated_actions names the actions that pause for a human Approve/Deny
        (a subset of GATEABLE_ACTIONS). Empty = the run is fully autonomous
        within the guards. The policy is frozen at start: changing the
        default in Settings mid-run does not move the goalposts under a run
        that is already going."""
        gated = frozenset(gated_actions) & set(GATEABLE_ACTIONS)
        run_id = self.db.create_agent_run(
            goal=goal, brain_instance_id=brain_ref,
            brain_model=brain_model, max_steps=max_steps,
            gated_actions=tuple(gated),
            created_by=created_by,
        )
        self.db.record_audit(
            "autopilot", "run_start",
            f"{run_id}: goal={goal[:120]!r} brain={brain_model} "
            f"via {brain_ref} "
            + ("(UNLIMITED steps" if max_steps == 0
               else f"(max {max_steps} steps")
            + (f", approval required for: {', '.join(sorted(gated))}"
               if gated else ", no approval gates") + ")",
        )
        if client_fn is None:
            instance_id = brain_ref.partition(":")[2] or brain_ref
            client_fn = lambda: self.orchestrator.model_client_for(instance_id)  # noqa: E731
        task = asyncio.create_task(
            self._run_loop(run_id, goal, client_fn, brain_model,
                           brain_port, max_steps, gated,
                           created_by=created_by)
        )
        self.tasks[run_id] = task
        # Settled runs drop out of the registry; without this, a long-lived
        # backend accumulates every finished task object forever and stop()
        # re-cancels hundreds of already-done runs.
        task.add_done_callback(lambda _t: self.tasks.pop(run_id, None))
        return run_id

    def cancel_run(self, run_id: str) -> bool:
        task = self.tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def stop(self) -> None:
        # Snapshot: done-callbacks pop entries from the dict as tasks settle.
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.tasks.clear()

    # -- the loop ---------------------------------------------------------------------

    async def _run_loop(self, run_id: str, goal: str, client_fn,
                        brain_model: str, brain_port: int,
                        max_steps: int, gated: frozenset[str],
                        created_by: str | None = None) -> None:
        # Phase 79: everything this run creates (launches, jobs) is
        # attributed to whoever STARTED it. Rebound explicitly rather than
        # inherited from the request context, so attribution does not
        # depend on where the task object happened to be created. The
        # audit actor for run events stays "autopilot" - what acted vs.
        # who it acted for.
        bind_principal(created_by)
        # The project brief (Autopilot page) frames every run: the goal
        # reads as one step in the user's project, not an isolated command.
        # Read at run start, so editing the brief never shifts a live run.
        brief = ""
        try:
            content = self.db.get_project_brief()["content"].strip()
            if content:
                brief = (
                    "\nProject context (set by the user; the goal below is "
                    "one step in this project):\n" + content + "\n"
                )
        except Exception:
            logger.exception("project brief read failed; run continues")
        template = SYSTEM_PROMPT
        if brief:
            template = template.replace("Goal: {goal}", brief + "\nGoal: {goal}")
        messages: list[dict] = [
            # .replace, not .format: the prompt is full of literal JSON braces.
            {"role": "system", "content": template.replace("{goal}", goal)},
            {"role": "user", "content": "Begin. What is your first action?"},
        ]
        failures = 0
        # max_steps == 0 means UNLIMITED (an explicit user choice): the run
        # ends only via done/cancel/failure. Still bounded in every way that
        # matters - the money is behind approval gates and the launch guards,
        # each wait is capped, and consecutive failures kill the loop.
        steps = (itertools.count(1) if max_steps == 0
                 else range(1, max_steps + 1))
        try:
            for seq in steps:
                try:
                    reply = await asyncio.wait_for(
                        self._chat(client_fn, brain_model,
                                   brain_port, messages),
                        timeout=self.settings.autopilot.chat_timeout_seconds,
                    )
                except (ModelClientError, asyncio.TimeoutError) as exc:
                    failures += 1
                    self._record(run_id, seq, thought="",
                                 action="__brain_error__", args={},
                                 result={"error": str(exc)})
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        self._finish(run_id, seq, "failed",
                                     error=f"brain unreachable: {exc}")
                        return
                    await self._sleep(2)
                    continue

                messages.append({"role": "assistant", "content": reply})
                parsed, parse_err = parse_action(reply)
                if parsed is None:
                    failures += 1
                    observation = {"error": parse_err}
                    self._record(run_id, seq, thought=reply[:300],
                                 action="__invalid__", args={},
                                 result=observation)
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        self._finish(run_id, seq, "failed",
                                     error="brain kept producing unparseable "
                                           "output")
                        return
                    messages.append({"role": "user",
                                     "content": json.dumps(observation)})
                    continue

                failures = 0
                thought = str(parsed.get("thought", ""))[:500]
                action, args = parsed["action"], parsed["args"]

                if action == "done":
                    summary = str(args.get("summary", ""))[:2000]
                    self._record(run_id, seq, thought=thought, action="done",
                                 args=args, result={"ok": True})
                    self._finish(run_id, seq, "succeeded", summary=summary)
                    return

                observation = await self._execute(
                    run_id, seq, action, args, gated)
                self._record(run_id, seq, thought=thought, action=action,
                             args=args, result=observation)
                messages.append({"role": "user",
                                 "content": json.dumps(observation)})
                messages = self._trim(messages)

            # Only a finite range falls through to here; an unlimited run
            # exits solely via done/cancel/failure returns above.
            self._finish(run_id, max_steps, "exhausted",
                         error=f"step limit ({max_steps}) reached before done")
        except asyncio.CancelledError:
            self.db.update_agent_run(
                run_id, status="cancelled", finished_at=utcnow(),
                error="cancelled by user",
            )
            self.db.record_audit("autopilot", "run_cancelled", run_id)
            self._worklog_run(run_id, "cancelled", error="cancelled by user")
            raise
        except Exception as exc:   # never leave a run stuck 'running'
            logger.exception("autopilot run %s crashed", run_id)
            self.db.update_agent_run(
                run_id, status="failed", finished_at=utcnow(),
                error=f"internal error: {exc}",
            )
            self._worklog_run(run_id, "failed",
                              error=f"internal error: {exc}")

    @staticmethod
    def _trim(messages: list[dict]) -> list[dict]:
        if len(messages) <= MAX_HISTORY_MESSAGES:
            return messages
        # Keep the system prompt and the most recent window.
        return [messages[0]] + messages[-(MAX_HISTORY_MESSAGES - 1):]

    def _record(self, run_id: str, seq: int, *, thought: str, action: str,
                args: dict, result: dict) -> None:
        self.db.add_agent_step(run_id, seq, thought=thought, action=action,
                               args=args, result=result)
        self.db.update_agent_run(run_id, steps_taken=seq)
        outcome = "error" if result.get("error") else "ok"
        self.db.record_audit(
            "autopilot", action,
            f"run {run_id} step {seq}: {json.dumps(args)[:150]} -> {outcome}",
        )

    def _finish(self, run_id: str, steps: int, status: str, *,
                summary: str = "", error: str = "") -> None:
        self.db.update_agent_run(
            run_id, status=status, steps_taken=steps, finished_at=utcnow(),
            summary=summary or None, error=error or None,
        )
        self.db.record_audit(
            "autopilot", f"run_{status}",
            f"{run_id} after {steps} step(s)"
            + (f": {summary[:150]}" if summary else "")
            + (f": {error[:150]}" if error else ""),
        )
        self._notify(
            "run_finished",
            f"Autopilot run {status}",
            (summary or error or f"{steps} step(s)")[:200],
            ref=run_id,
        )
        self._worklog_run(run_id, status, steps=steps, summary=summary,
                          error=error)

    def _worklog_run(self, run_id: str, status: str, *,
                     steps: int | None = None, summary: str = "",
                     error: str = "") -> None:
        """One worklog entry per settled run - including cancelled and
        crashed ones, which are exactly the outcomes the next agent session
        most needs to know about."""
        if self.worklog is None:
            return
        try:
            run = self.db.get_agent_run(run_id) or {}
            if steps is None:
                steps = run.get("steps_taken") or 0
            lines = [f"run {run_id}, {steps} step(s)"]
            if run.get("goal"):
                lines.append(f"goal: {run['goal'][:300]}")
            if summary:
                lines.append(f"summary: {summary[:500]}")
            if error:
                lines.append(f"error: {error[:300]}")
            self.worklog.record(f"autopilot run {status}", lines)
        except Exception:
            logger.exception("worklog entry for run %s failed", run_id)

    def _notify(self, kind: str, title: str, body: str,
                ref: str | None = None) -> None:
        if self.notifier is not None:
            self.notifier.notify(kind, title, body, ref=ref)

    # -- talking to the brain ------------------------------------------------------------

    async def _chat(self, client_fn, model: str, port: int,
                    messages: list[dict]) -> str:
        client = client_fn()
        if client is None:
            raise ModelClientError("brain unreachable: no connection/client")
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 2048,
            "temperature": 0.0,   # agent turns want determinism, not flair
        }
        parts: list[str] = []
        async for line in client.chat_stream(port, payload):
            line = line.strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                chunk = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise ModelClientError(str(chunk["error"]))
            delta = (chunk.get("choices") or [{}])[0].get("delta", {})
            parts.append(delta.get("content") or "")
        return "".join(parts)

    # -- the action surface ---------------------------------------------------------------

    async def _await_approval(self, approval_id: str) -> str:
        """Poll until the human decides or the timeout auto-denies.

        Cancellation (run cancelled while waiting) expires the approval so
        no stale pending card lingers in the UI."""
        timeout = self.settings.autopilot.approval_timeout_seconds
        deadline = asyncio.get_event_loop().time() + timeout
        try:
            while asyncio.get_event_loop().time() < deadline:
                approval = self.db.get_approval(approval_id)
                if approval and approval["status"] != "pending":
                    return approval["status"]
                await self._sleep(0.5)
        except asyncio.CancelledError:
            self.db.decide_approval(approval_id, "expired")
            raise
        self.db.decide_approval(approval_id, "expired")
        return "expired"

    async def _execute(self, run_id: str, seq: int, action: str, args: dict,
                       gated: frozenset[str]) -> dict:
        """Run one action; ALL failures come back as {"error": ...} data so
        the model can read them. Nothing raises across this boundary except
        cancellation."""
        if action in gated:
            approval_id = self.db.create_approval(run_id, seq, action, args)
            self.db.record_audit(
                "autopilot", "approval_requested",
                f"run {run_id} step {seq}: {action} "
                f"{json.dumps(args)[:150]} (id {approval_id})")
            self._notify(
                "approval_requested",
                f"Autopilot needs your approval: {action}",
                f"{json.dumps(args)[:160]}\nThe run is paused until you "
                f"decide, and auto-denies after "
                f"{self.settings.autopilot.approval_timeout_seconds / 60:.0f} "
                f"minutes.",
                ref=approval_id,
            )
            decision = await self._await_approval(approval_id)
            if decision == "denied":
                return {"error": f"{action} was DENIED by the user. Do not "
                                 f"retry it unchanged; adapt or finish."}
            if decision == "expired":
                return {"error": f"{action} approval timed out with no "
                                 f"human decision - treat as denied."}
            # approved: fall through and execute for real.
        try:
            handler = getattr(self, f"_act_{action}", None)
            if handler is None:
                return {"error": f"unknown action '{action}'. Valid: "
                                 "list_instance_types, list_instances, "
                                 "launch_gpu, get_launch_status, "
                                 "list_templates, run_job, save_template, "
                                 "get_job_status, get_job_logs, "
                                 "sync_outputs, terminate_instance, wait, "
                                 "done"}
            return await handler(args)
        except asyncio.CancelledError:
            raise
        except LaunchRejected as exc:
            return {"error": exc.detail}
        except TerminationBlocked as exc:
            return {"blocked": True, "error": str(exc),
                    "unpersisted_files": exc.files}
        except LambdaAPIError as exc:
            return {"error": exc.message}
        except (KeyError, TypeError, ValueError) as exc:
            return {"error": f"bad arguments for {action}: {exc}"}
        except Exception as exc:
            logger.exception("autopilot action %s failed", action)
            return {"error": f"{action} failed internally: {exc}"}

    async def _act_list_instance_types(self, args: dict) -> dict:
        types = await self.orchestrator.client.list_instance_types()
        return {"instance_types": {
            name: {
                "usd_per_hour": t.price_cents_per_hour / 100,
                "regions_with_capacity": t.regions_with_capacity,
            }
            for name, t in sorted(types.items())
        }}

    async def _act_list_instances(self, args: dict) -> dict:
        instances = await self.orchestrator.instances_with_state()
        return {"instances": [
            {k: i[k] for k in ("id", "name", "status", "region",
                               "instance_type", "hourly_rate_usd",
                               "connection_state")}
            for i in instances
        ]}

    async def _act_launch_gpu(self, args: dict) -> dict:
        launch = await self.orchestrator.request_launch(
            instance_type=str(args["instance_type"]),
            region=str(args["region"]),
            filesystem=str(args["filesystem"]),
            created_by=current_principal(),   # the run starter (see _run_loop)
        )
        return {"launch": {k: launch[k] for k in ("id", "status")}}

    async def _act_get_launch_status(self, args: dict) -> dict:
        launch = self.db.get_launch(str(args["launch_id"]))
        if launch is None:
            return {"error": f"launch {args['launch_id']} not found"}
        return {k: launch[k] for k in ("id", "status", "lambda_instance_id",
                                       "launched_type", "attempts", "error")}

    async def _act_list_templates(self, args: dict) -> dict:
        return {"templates": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": [
                    {"name": p.name, "type": p.type,
                     "required": p.required, "default": p.default}
                    for p in t.parameters
                ],
            }
            for t in self.templates.values()
        ]}

    async def _act_run_job(self, args: dict) -> dict:
        name = str(args["template"])
        parameters = args.get("parameters") or {}
        template = self.templates.get(name)
        if template is None:
            return {"error": f"unknown template '{name}'. Available: "
                             f"{', '.join(sorted(self.templates))}"}
        try:
            coerce_parameters(template, parameters)
        except ParameterError as exc:
            return {"error": str(exc)}
        task_id = self.queue.enqueue(template=name, parameters=parameters,
                                     created_by=current_principal())
        return {"task": {"id": task_id, "status": "queued"}}

    async def _act_save_template(self, args: dict) -> dict:
        """Author a custom template mid-run. Same validated path as the Jobs
        page and MCP - the mount jail and parameter rules bind identically,
        and the template persists for the user after the run ends."""
        if self._template_saver is None:
            return {"error": "template saving is not available in this run"}
        yaml_text = str(args.get("yaml") or "")
        if not yaml_text.strip():
            return {"error": 'save_template needs {"yaml": "<template>"}'}
        try:
            template = self._template_saver(yaml_text)
        except Exception as exc:   # loader errors come back as data
            return {"error": f"template rejected: {exc}"}
        return {
            "saved": template.name,
            "parameters": [p.name for p in template.parameters],
            "hint": "run it with run_job now if the goal needs it",
        }

    async def _act_get_job_status(self, args: dict) -> dict:
        task = self.queue.get(str(args["task_id"]))
        if task is None:
            return {"error": f"task {args['task_id']} not found"}
        return {k: task[k] for k in ("id", "status", "exit_code", "error",
                                     "output_paths", "instance_id")}

    async def _act_get_job_logs(self, args: dict) -> dict:
        task_id = str(args["task_id"])
        if self.queue.get(task_id) is None:
            return {"error": f"task {task_id} not found"}
        tail = min(int(args.get("tail", 30)), 200)
        lines = self.queue.get_logs(task_id, tail=tail)
        return {"lines": [l["line"] for l in lines]}

    async def _act_sync_outputs(self, args: dict) -> dict:
        return await self.orchestrator.sync_ephemeral(str(args["instance_id"]))

    async def _act_terminate_instance(self, args: dict) -> dict:
        return await self.orchestrator.terminate(
            str(args["instance_id"]), force=bool(args.get("force", False))
        )

    async def _act_wait(self, args: dict) -> dict:
        seconds = min(float(args.get("seconds", 5)),
                      self.settings.autopilot.wait_cap_seconds)
        seconds = max(seconds, 0.0)
        await self._sleep(seconds)
        return {"waited_seconds": seconds}
