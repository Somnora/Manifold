"""The brains registry: every model Manifold can put in the driver's seat.

Three kinds, one interface (OpenAI-compatible chat completions):

- instance:  a model served on a Manifold-launched GPU (vllm-serve /
             sglang-serve), reached over the managed SSH connection —
             the original Autopilot brain.
- local:     a model server on THIS machine (Ollama, LM Studio). The
             backend already runs on the user's machine, so these are
             plain loopback HTTP — auto-detected by probing /v1/models.
- api:       a frontier API (Anthropic, OpenAI, Gemini) via its
             OpenAI-compatible endpoint. Appears only once its key env
             var is set; keys live in .env (Settings page), never here.
- cli:       a frontier CLI the user is ALREADY logged into (claude /
             codex / gemini). Each authenticates through the provider's
             own official OAuth; Manifold shells out to the CLI and never
             sees a token - the ToS-clean way to use a subscription.

Brain refs are strings so they fit the existing agent_runs schema:
    instance:<id>   local:<endpoint>/<model>   api:<name>   cli:<name>
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from .config import DATA_ROOT, Settings
from .model_client import ModelClientError

logger = logging.getLogger("manifold.brains")

PROBE_TIMEOUT = 1.5          # local servers answer instantly or not at all
DETECT_CACHE_SECONDS = 10.0

# A macOS app launched from Finder inherits launchd's bare PATH
# (/usr/bin:/bin:...), which hides every user-installed CLI - the packaged
# app reported "No brains found" to a user logged into all three frontier
# CLIs. So CLI detection searches these well-known install dirs on top of
# PATH. Order matters only for duplicates: PATH wins, then this list.
WELL_KNOWN_BIN_DIRS = (
    "/opt/homebrew/bin",      # Homebrew (Apple Silicon)
    "/usr/local/bin",         # Homebrew (Intel), classic installs
    "~/.local/bin",           # pipx / uv / npm prefix installs
    "~/.npm-global/bin",      # npm with a user prefix
    "~/.bun/bin",             # bun global installs
    "~/bin",
)


def which_with_fallback(name: str) -> str | None:
    """shutil.which, then the well-known bin dirs a GUI app's PATH misses."""
    path = shutil.which(name)
    if path:
        return path
    for d in WELL_KNOWN_BIN_DIRS:
        candidate = os.path.join(os.path.expanduser(d), name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


@dataclass
class BrainInfo:
    ref: str                 # "instance:i-...", "local:ollama/llama3", "api:claude"
    kind: str                # instance | local | api
    label: str               # human name for the picker
    model: str               # model id sent in requests
    detail: str = ""         # where it lives / status note
    ready: bool = True


class ExternalBrainClient:
    """OpenAI-compatible chat against a plain HTTP endpoint (local or API).

    Presents the same chat_stream/chat_completion surface as ModelClient,
    so the agent loop and chat-tools executor are brain-agnostic. `port` is
    accepted and ignored (instance clients need it; HTTP brains do not).
    """

    def __init__(self, base_url: str, *, api_key: str = "",
                 timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._timeout = timeout

    async def chat_completion(self, port: int, payload: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload, headers=self._headers,
                )
        except httpx.HTTPError as exc:
            raise ModelClientError(f"brain unreachable at {self.base_url}: {exc}")
        if resp.status_code != 200:
            raise ModelClientError(
                f"brain at {self.base_url} answered {resp.status_code}: "
                f"{resp.text[:200]}")
        return resp.json()

    async def chat_stream(self, port: int,
                          payload: dict) -> AsyncIterator[str]:
        payload = {**payload, "stream": True}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/chat/completions",
                    json=payload, headers=self._headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode(errors="replace")
                        raise ModelClientError(
                            f"brain at {self.base_url} answered "
                            f"{resp.status_code}: {body[:200]}")
                    async for line in resp.aiter_lines():
                        if line:
                            yield line + "\n\n"
        except httpx.HTTPError as exc:
            raise ModelClientError(f"brain unreachable at {self.base_url}: {exc}")


# Frontier CLIs usable as brains. Each entry: how to invoke non-interactive
# mode (verified against the installed CLIs' --help on 2026-07-13) and which
# provider login it rides on. No tokens pass through Manifold - the CLI's
# own official OAuth session does the auth.
CLI_ADAPTERS = {
    "claude": {"provider": "Claude (Anthropic)"},
    "codex": {"provider": "ChatGPT (OpenAI)"},
    "gemini": {"provider": "Google"},
}


def _flatten_messages(messages: list[dict]) -> str:
    """One prompt string from a chat history - frontier CLIs take a single
    prompt, not a message array. Roles are labeled so the model keeps the
    system/user/assistant structure."""
    parts = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):   # OpenAI content-parts -> text only
            content = " ".join(p.get("text", "") for p in content
                               if p.get("type") == "text")
        parts.append(f"[{m.get('role', 'user')}]\n{content}")
    return "\n\n".join(parts)


class CliBrainClient:
    """A frontier CLI (claude / codex / gemini) as a chat brain.

    Runs the CLI as a subprocess per turn - argv list, no shell, cwd set to
    an empty scratch dir so an agentic CLI has nothing to poke at. Same
    chat surface as ModelClient, so the agent loop cannot tell the
    difference. Turn-at-once (no token streaming), like the tools chat.
    """

    def __init__(self, name: str, executable: str, *, timeout: float = 280.0):
        self.name = name
        self.executable = executable
        self._timeout = timeout
        self._workdir = DATA_ROOT / "hub-scratch"

    async def _run(self, argv: list[str]) -> str:
        self._workdir.mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(self._workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout)
        except FileNotFoundError:
            raise ModelClientError(f"{self.name} CLI vanished from PATH")
        except asyncio.TimeoutError:
            proc.kill()
            raise ModelClientError(
                f"{self.name} CLI took over {self._timeout:.0f}s - aborted")
        if proc.returncode != 0:
            detail = (stderr or stdout).decode(errors="replace").strip()[-400:]
            raise ModelClientError(
                f"{self.name} CLI exited {proc.returncode}: {detail} "
                f"(is it logged in? run `{self.name}` once in a terminal)")
        return stdout.decode(errors="replace")

    async def _answer(self, prompt: str) -> str:
        if self.name == "claude":
            # Print mode; JSON output carries the reply in .result.
            out = await self._run([self.executable, "-p", prompt,
                                   "--output-format", "json"])
            try:
                return str(json.loads(out).get("result", "")) or out.strip()
            except ValueError:
                return out.strip()
        if self.name == "codex":
            # Non-interactive exec; the final message lands in a temp file
            # (--output-last-message), read-only sandbox, no session litter.
            with tempfile.NamedTemporaryFile(suffix=".txt",
                                             delete=False) as tmp:
                last = tmp.name
            try:
                await self._run([self.executable, "exec", prompt,
                                 "--skip-git-repo-check", "-s", "read-only",
                                 "--output-last-message", last])
                from pathlib import Path as _P
                return _P(last).read_text(errors="replace").strip()
            finally:
                try:
                    os.unlink(last)
                except OSError:
                    pass
        if self.name == "gemini":
            out = await self._run([self.executable, "-p", prompt,
                                   "-o", "text"])
            return out.strip()
        raise ModelClientError(f"no adapter for CLI brain '{self.name}'")

    async def chat_completion(self, port: int, payload: dict) -> dict:
        text = await self._answer(_flatten_messages(payload.get("messages", [])))
        return {"choices": [{"message": {"role": "assistant",
                                         "content": text}}]}

    async def chat_stream(self, port: int,
                          payload: dict) -> AsyncIterator[str]:
        text = await self._answer(_flatten_messages(payload.get("messages", [])))
        chunk = {"choices": [{"delta": {"content": text}}]}
        yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"


class BrainRegistry:
    """Discovers brains and resolves a ref to a chat client."""

    def __init__(self, settings: Settings, orchestrator, queue, templates,
                 *, http_get=None, which=None):
        self.settings = settings
        self.orchestrator = orchestrator
        self.queue = queue
        self.templates = templates
        # Injectable prober for tests: async (url) -> list[model_id] | None.
        self._http_get = http_get or self._probe_models
        # Injectable executable lookup for tests.
        self._which = which or which_with_fallback
        self._local_cache: tuple[float, list[BrainInfo]] | None = None

    # -- discovery ------------------------------------------------------------------

    @staticmethod
    async def _probe_models(url: str) -> list[str] | None:
        """GET <base>/models; None = server not there, [] = there, no models.

        Defensive about the BODY, not just the status code, because this
        parses a third party's JSON and a crash here 500s /brains - which
        is the brain picker, the chat, Autopilot and the distill panel all
        at once.

        A freshly installed Ollama with no models pulled answers
        `{"object": "list", "data": null}`. `.get("data", [])` does NOT
        default there: the key exists and holds null, so the old code
        iterated None and took the whole route down. Found at the
        2026-08-14 real gate, on an Ollama installed minutes earlier by
        following this project's own docs.
        """
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                return None
            body = resp.json()
            if not isinstance(body, dict):
                return []
            data = body.get("data") or []
            if not isinstance(data, list):
                return []
            return [m["id"] for m in data
                    if isinstance(m, dict) and m.get("id")]
        except (httpx.HTTPError, ValueError):
            return None

    async def _instance_brains(self) -> list[BrainInfo]:
        from .agent import find_serving_task
        out = []
        for instance_id in list(self.orchestrator.connections):
            task = find_serving_task(self.queue, self.templates, instance_id)
            if task is None:
                continue
            out.append(BrainInfo(
                ref=f"instance:{instance_id}", kind="instance",
                label=f"{task['model_id']} (GPU instance)",
                model=task["model_id"],
                detail=f"served on {instance_id}",
            ))
        return out

    async def _local_brains(self) -> list[BrainInfo]:
        now = asyncio.get_event_loop().time()
        if self._local_cache and now - self._local_cache[0] < DETECT_CACHE_SECONDS:
            return self._local_cache[1]
        out: list[BrainInfo] = []
        for ep in self.settings.hub.local_endpoints:
            models = await self._http_get(f"{ep.base_url}/models")
            if models is None:
                continue
            for model in models[:8]:     # keep the picker sane
                out.append(BrainInfo(
                    ref=f"local:{ep.name}/{model}", kind="local",
                    label=f"{model} ({ep.name}, this machine)",
                    model=model, detail=ep.base_url,
                ))
        self._local_cache = (now, out)
        return out

    def _cli_brains(self) -> list[BrainInfo]:
        out = []
        for name in self.settings.hub.cli_brains:
            adapter = CLI_ADAPTERS.get(name)
            if adapter is None:
                continue
            path = self._which(name)
            if not path:
                continue      # not installed -> not offered
            out.append(BrainInfo(
                ref=f"cli:{name}", kind="cli",
                label=f"{name} CLI (your {adapter['provider']} login)",
                model=name, detail=path,
            ))
        return out

    def _api_brains(self) -> list[BrainInfo]:
        out = []
        for brain in self.settings.hub.api_brains:
            if not os.environ.get(brain.api_key_env, ""):
                continue     # no key -> not offered; never a broken option
            out.append(BrainInfo(
                ref=f"api:{brain.name}", kind="api",
                label=f"{brain.model} ({brain.name} API)",
                model=brain.model,
                detail=f"key from {brain.api_key_env}",
            ))
        return out

    async def list_brains(self) -> list[BrainInfo]:
        return (await self._instance_brains()
                + await self._local_brains()
                + self._cli_brains()
                + self._api_brains())

    # -- resolution -----------------------------------------------------------------

    def resolve(self, ref: str) -> tuple[object, str, int]:
        """ref -> (chat client, model id, port). Raises ValueError with a
        human-readable reason when the ref cannot be served right now."""
        kind, _, rest = ref.partition(":")
        if kind == "instance":
            from .agent import find_serving_task
            task = find_serving_task(self.queue, self.templates, rest)
            if task is None:
                raise ValueError(f"no model is being served on {rest}")
            client = self.orchestrator.model_client_for(rest)
            if client is None:
                raise ValueError(f"no managed connection to {rest}")
            return client, task["model_id"], task["port"]
        if kind == "local":
            name, _, model = rest.partition("/")
            for ep in self.settings.hub.local_endpoints:
                if ep.name == name:
                    if not model:
                        raise ValueError(f"local brain ref missing model: {ref}")
                    return (ExternalBrainClient(ep.base_url), model, 0)
            raise ValueError(f"unknown local endpoint '{name}'")
        if kind == "cli":
            if rest not in CLI_ADAPTERS or rest not in self.settings.hub.cli_brains:
                raise ValueError(f"unknown cli brain '{rest}'")
            path = self._which(rest)
            if not path:
                raise ValueError(
                    f"the {rest} CLI is not installed (or not on PATH)")
            return (CliBrainClient(rest, path), rest, 0)
        if kind == "api":
            for brain in self.settings.hub.api_brains:
                if brain.name == rest:
                    key = os.environ.get(brain.api_key_env, "")
                    if not key:
                        raise ValueError(
                            f"{brain.api_key_env} is not set - add it in "
                            f"Settings to use the {brain.name} brain")
                    return (ExternalBrainClient(brain.base_url, api_key=key),
                            brain.model, 0)
            raise ValueError(f"unknown api brain '{rest}'")
        raise ValueError(
            f"bad brain ref '{ref}' "
            f"(expected instance:/local:/api:/cli: prefix)")
