"""Tools for the in-dashboard chat: let the served model touch the system.

A vLLM-served model is pure text — it has no access to anything. This module
gives the chat endpoint a small, guarded action surface (same philosophy as
the Autopilot allowlist, scoped to what a chat needs): browse and read files
on the instance's mounted filesystems, and queue/inspect jobs. The backend
runs the loop: the model replies with a JSON action, we execute it through
Manifold's existing guarded paths, feed the observation back, and repeat
until the model answers in plain text.

No shell action, no arbitrary HTTP, no launch/terminate from chat (that is
Autopilot's job, with its own step caps and run ledger). File reads are
capped and confined to the same roots the file navigator allows.
"""

from __future__ import annotations

import json
import logging
import posixpath
import shlex

from .db import Database
from .dispatcher import ParameterError, coerce_parameters
from .orchestrator import Orchestrator
from .sidecar_client import SidecarError
from .task_queue import TaskQueue
from .templates import JobTemplate

logger = logging.getLogger("manifold.chat_tools")

MAX_TOOL_TURNS = 8              # tool calls per user message, then answer
READ_CAP_BYTES = 16_384         # head-read cap per file

ALLOWED_FILE_ROOTS = ("/lambda/nfs/", "/workspace/ephemeral/")

TOOLS_PROMPT = """You are the model served on a Manifold GPU instance, with tools.

To use a tool, reply with EXACTLY ONE JSON object and nothing else:
{"action": "<name>", "args": {...}}

Tools:
- list_files {"root": "persistent"|"ephemeral", "path": "<dir>"} -> one directory level of the instance's filesystem
- read_file {"path": "<file>"} -> first 16 KB of a file (relative paths = the persistent filesystem root)
- list_templates {} -> runnable job templates and their parameters
- run_job {"template": str, "parameters": {...}} -> queue a job on this instance (scrapes, transcodes, synthesis...)
- get_job_status {"task_id": str} -> queued|running|succeeded|failed|skipped + output paths
- get_job_logs {"task_id": str, "tail": int} -> recent log lines

The tool result arrives as JSON in the next user message; an "error" key
means the call was refused — read it and adapt. Chain tools as needed.
When you have what you need, reply in PLAIN TEXT (no JSON) to answer the
user. Plain text is always treated as your final answer."""


class ChatToolExecutor:
    """Executes one chat tool call through the guarded paths. Every failure
    returns {"error": ...} data for the model; nothing raises out."""

    def __init__(self, orchestrator: Orchestrator, queue: TaskQueue,
                 templates: dict[str, JobTemplate], db: Database,
                 instance_id: str):
        self.orchestrator = orchestrator
        self.queue = queue
        self.templates = templates
        self.db = db
        self.instance_id = instance_id

    async def execute(self, action: str, args: dict) -> dict:
        handler = getattr(self, f"_act_{action}", None)
        if handler is None:
            return {"error": f"unknown tool '{action}'. Valid: list_files, "
                             "read_file, list_templates, run_job, "
                             "get_job_status, get_job_logs"}
        try:
            result = await handler(args)
        except SidecarError as exc:
            result = {"error": str(exc)}
        except (KeyError, TypeError, ValueError) as exc:
            result = {"error": f"bad arguments for {action}: {exc}"}
        except Exception as exc:   # noqa: BLE001 - surface, never crash chat
            logger.exception("chat tool %s failed", action)
            result = {"error": f"{action} failed internally: {exc}"}
        outcome = "error" if result.get("error") else "ok"
        self.db.record_audit(
            "chat", f"tool_{action}",
            f"{self.instance_id}: {json.dumps(args)[:150]} -> {outcome}")
        return result

    # -- filesystem -----------------------------------------------------------------

    async def _act_list_files(self, args: dict) -> dict:
        sidecar = self.orchestrator.sidecar_for(self.instance_id)
        if sidecar is None:
            return {"error": f"no managed connection to {self.instance_id}"}
        root = str(args.get("root", "persistent"))
        path = str(args.get("path", ""))
        return await sidecar.list_dir(root, path)

    def _resolve_path(self, path: str) -> str | None:
        """Same containment rule as the file navigator: relative paths land
        on the instance's persistent filesystem; everything must stay under
        the sanctioned roots."""
        if not path.startswith("/"):
            launch = self.db.find_launch_by_instance(self.instance_id)
            filesystem = (launch or {}).get("filesystem")
            if not filesystem:
                return None
            path = f"/lambda/nfs/{filesystem}/{path}"
        resolved = posixpath.normpath(path)
        if not any(resolved.startswith(root) for root in ALLOWED_FILE_ROOTS):
            return None
        return resolved

    async def _act_read_file(self, args: dict) -> dict:
        conn = self.orchestrator.connections.get(self.instance_id)
        if conn is None:
            return {"error": f"no managed connection to {self.instance_id}"}
        resolved = self._resolve_path(str(args["path"]))
        if resolved is None:
            return {"error": "path must stay under "
                             + " or ".join(ALLOWED_FILE_ROOTS)
                             + " (relative paths = the persistent filesystem)"}
        exit_status, stdout, stderr = await conn.run(
            f"head -c {READ_CAP_BYTES} -- {shlex.quote(resolved)}")
        if exit_status != 0:
            return {"error": f"cannot read {resolved}: {stderr.strip()[:200]}"}
        return {"path": resolved, "content": stdout,
                "truncated_at_bytes": READ_CAP_BYTES}

    # -- jobs ------------------------------------------------------------------------

    async def _act_list_templates(self, args: dict) -> dict:
        return {"templates": [
            {"name": t.name, "description": t.description,
             "parameters": [
                 {"name": p.name, "type": p.type,
                  "required": p.required, "default": p.default}
                 for p in t.parameters
             ]}
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
        task_id = self.queue.enqueue(template=name, parameters=parameters)
        return {"task": {"id": task_id, "status": "queued"},
                "note": "poll get_job_status; logs via get_job_logs"}

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
