"""Manifold sidecar — runs ON the GPU instance, binds to 127.0.0.1 ONLY.

The backend reaches it exclusively through an SSH local port forward over
the managed connection; it is never exposed on a public interface. sshd
stays the only public listener on the box.

Endpoints:
    GET /health                liveness
    GET /metrics               VRAM / utilization / temperature via pynvml
    WS  /metrics/stream        the same payload pushed every interval
    GET /storage/unpersisted   files under /workspace/ephemeral matching
                               "valuable" patterns — the termination safety
                               hook's evidence list
    GET /storage/recent        recently modified files on both volumes
    GET /fs/list               one directory level (the file navigator)
    GET /fs/usage              recursive sizes of a directory's children
    POST /fs/delete            delete a file or directory (jailed to roots)

Single file, minimal dependencies (fastapi, uvicorn, pynvml), installed by
cloud-init. Run: python3 manifold_sidecar.py
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel


# NFS turns "delete a file another process still has open" into a hidden
# .nfsXXXX placeholder, so the parent then refuses to go with a bare
# "Directory not empty" — which reads like a bug rather than "a job is still
# using this". Recognize that shape and say what is actually wrong.
_BUSY_HINT = (
    "a running job still has these files open; NFS keeps hidden .nfs* "
    "placeholders until that process exits. Stop the job using this path, "
    "then delete again."
)


def _busy_hint(detail: str) -> str | None:
    low = detail.lower()
    if "not empty" in low or "resource busy" in low or ".nfs" in low:
        return _BUSY_HINT
    return None


def _privileged_remove(target: Path) -> None:
    """Remove `target` with elevated privileges — the fallback when the
    ubuntu-run sidecar cannot unlink a path a job container wrote as root.

    The caller has already jail-resolved `target` to an absolute path inside
    a sanctioned root, and it is passed as a single argv (no shell, with `--`
    to stop option parsing), so the escalation stays confined to that path.
    Relies on the instance's passwordless sudo (Lambda's default); if sudo
    is unavailable or refuses, we surface a clear error rather than hang."""
    try:
        result = subprocess.run(
            ["sudo", "-n", "rm", "-rf", "--", str(target)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(500, f"privileged delete failed: {exc}")
    if result.returncode != 0:
        detail = result.stderr.strip() or "sudo rm failed"
        hint = _busy_hint(detail)
        if hint:
            raise HTTPException(409, f"could not delete: {hint} ({detail})")
        raise HTTPException(
            500, "could not delete (even with elevated privileges): " + detail)

try:
    import pynvml
except ImportError:  # pragma: no cover - tests inject a fake
    pynvml = None

EPHEMERAL_ROOT = Path(os.environ.get("MANIFOLD_EPHEMERAL_ROOT", "/workspace/ephemeral"))

# Files worth warning about before termination. Overridable via env:
# MANIFOLD_VALUABLE_PATTERNS="*.safetensors,*.pt,*.bin"
DEFAULT_VALUABLE_PATTERNS = [
    "*.safetensors", "*.pt", "*.ckpt", "*.bin", "*.gguf",
    "*.png", "*.jpg", "*.jsonl", "*.csv", "*.srt", "*.wav", "*.mp4",
]
STREAM_INTERVAL_SECONDS = float(os.environ.get("MANIFOLD_STREAM_INTERVAL", "2.0"))


def valuable_patterns() -> list[str]:
    raw = os.environ.get("MANIFOLD_VALUABLE_PATTERNS", "")
    if raw.strip():
        return [p.strip() for p in raw.split(",") if p.strip()]
    return DEFAULT_VALUABLE_PATTERNS


PERSISTENT_ROOT = Path(os.environ.get("MANIFOLD_PERSISTENT_ROOT", "/lambda/nfs"))
# Bound the recent-files walk so a huge model cache cannot stall the sidecar.
MAX_SCAN_ENTRIES = 20_000


class DeleteRequest(BaseModel):
    # Module level (not inside create_app): with `from __future__ import
    # annotations`, FastAPI resolves annotation strings via module globals,
    # so a factory-local model would silently become a query parameter.
    root_name: str
    path: str
    recursive: bool = False


def create_app(nvml=None, ephemeral_root: Path | None = None,
               persistent_root: Path | None = None) -> FastAPI:
    """App factory; tests pass a fake nvml module and temp roots."""
    nvml = nvml if nvml is not None else pynvml
    root = ephemeral_root if ephemeral_root is not None else EPHEMERAL_ROOT
    p_root = persistent_root if persistent_root is not None else PERSISTENT_ROOT
    app = FastAPI(title="manifold-sidecar")
    state = {"nvml_ready": False}

    def ensure_nvml() -> bool:
        if nvml is None:
            return False
        if not state["nvml_ready"]:
            try:
                nvml.nvmlInit()
                state["nvml_ready"] = True
            except Exception:
                return False
        return True

    def read_metrics() -> dict:
        ide_processes = []
        try:
            out = subprocess.check_output(["ps", "-eo", "comm,args"], text=True)
            for line in out.splitlines():
                if "vscode-server" in line and "vscode" not in ide_processes:
                    ide_processes.append("vscode")
                elif "cursor-server" in line and "cursor" not in ide_processes:
                    ide_processes.append("cursor")
                elif "sshd:" in line and "@pts" in line and "ssh" not in ide_processes:
                    ide_processes.append("ssh")
        except Exception:
            pass

        base = {"active_ide_processes": ide_processes}

        if not ensure_nvml():
            base.update({"available": False, "gpus": [],
                    "error": "pynvml unavailable or no NVIDIA driver"})
            return base

        gpus = []
        for i in range(nvml.nvmlDeviceGetCount()):
            handle = nvml.nvmlDeviceGetHandleByIndex(i)
            mem = nvml.nvmlDeviceGetMemoryInfo(handle)
            util = nvml.nvmlDeviceGetUtilizationRates(handle)
            temp = nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU)
            name = nvml.nvmlDeviceGetName(handle)
            gpus.append({
                "index": i,
                "name": name.decode() if isinstance(name, bytes) else str(name),
                "vram_used_mib": mem.used // (1024 * 1024),
                "vram_total_mib": mem.total // (1024 * 1024),
                "utilization_pct": util.gpu,
                "temperature_c": temp,
            })
        
        base.update({"available": True, "gpus": gpus,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return base


    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics():
        return read_metrics()

    @app.websocket("/metrics/stream")
    async def metrics_stream(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_json(read_metrics())
                await asyncio.sleep(STREAM_INTERVAL_SECONDS)
        except WebSocketDisconnect:
            pass

    @app.get("/storage/unpersisted")
    async def unpersisted():
        """Valuable files in ephemeral scratch that would die with the box."""
        patterns = valuable_patterns()
        files = []
        if root.exists():
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if any(fnmatch.fnmatch(path.name, p) for p in patterns):
                    stat = path.stat()
                    files.append({
                        "path": str(path.relative_to(root)),
                        "size_bytes": stat.st_size,
                        "modified": datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(timespec="seconds"),
                    })
        files.sort(key=lambda f: f["size_bytes"], reverse=True)
        return {"root": str(root), "patterns": patterns, "files": files}

    @app.get("/storage/recent")
    async def recent(hours: float = 24, limit: int = 50):
        """Files changed in the last N hours across ephemeral scratch and
        the persistent mounts — a live view of what jobs are producing."""
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        results = []
        scanned = 0
        truncated = False
        for base, kind in ((root, "ephemeral"), (p_root, "persistent")):
            if not base.exists():
                continue
            for path in base.rglob("*"):
                scanned += 1
                if scanned > MAX_SCAN_ENTRIES:
                    truncated = True
                    break
                try:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < cutoff:
                    continue
                results.append({
                    "root": kind,
                    "path": str(path.relative_to(base)),
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(timespec="seconds"),
                })
        results.sort(key=lambda f: f["modified"], reverse=True)
        return {"files": results[:limit], "truncated": truncated,
                "hours": hours}

    # -- file navigator ------------------------------------------------------------

    roots = {"ephemeral": root, "persistent": p_root}

    def resolve_jailed(root_name: str, rel_path: str) -> Path:
        """Resolve root+relative path; refuse anything escaping the root."""
        base = roots.get(root_name)
        if base is None:
            raise HTTPException(400, f"unknown root '{root_name}'; "
                                     f"use ephemeral or persistent")
        target = (base / rel_path.lstrip("/")).resolve()
        if target != base.resolve() and base.resolve() not in target.parents:
            raise HTTPException(400, f"path escapes the {root_name} root")
        return target

    @app.get("/fs/list")
    async def fs_list(root_name: str = "persistent", path: str = ""):
        """One directory level: entries with type, size, mtime. Directories
        first, then files, both alphabetical. Fast — a single scandir, no
        recursion (sizes of directories come from /fs/usage)."""
        target = resolve_jailed(root_name, path)
        if not target.exists():
            raise HTTPException(404, f"{root_name}:{path or '/'} not found")
        if not target.is_dir():
            raise HTTPException(400, f"{root_name}:{path} is a file, not a "
                                     f"directory")
        entries = []
        with os.scandir(target) as it:
            for entry in it:
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(follow_symlinks=False),
                    "size_bytes": 0 if entry.is_dir(follow_symlinks=False)
                    else stat.st_size,
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(timespec="seconds"),
                })
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return {"root": root_name, "path": path, "entries": entries}

    @app.get("/fs/usage")
    async def fs_usage(root_name: str = "persistent", path: str = ""):
        """Recursive total size of each child of a directory — the
        'what is eating my filesystem' view, heaviest first. Bounded walk
        (MAX_SCAN_ENTRIES) with an honest truncated flag."""
        target = resolve_jailed(root_name, path)
        if not target.is_dir():
            raise HTTPException(404, f"{root_name}:{path or '/'} is not a "
                                     f"directory")
        scanned = 0
        truncated = False

        def tree_size(p: Path) -> tuple[int, int]:
            nonlocal scanned, truncated
            total, files = 0, 0
            stack = [p]
            while stack:
                current = stack.pop()
                scanned += 1
                if scanned > MAX_SCAN_ENTRIES:
                    truncated = True
                    break
                try:
                    if current.is_symlink():
                        continue
                    if current.is_file():
                        total += current.stat().st_size
                        files += 1
                    elif current.is_dir():
                        stack.extend(current.iterdir())
                except OSError:
                    continue
            return total, files

        children = []
        with os.scandir(target) as it:
            for entry in it:
                is_dir = entry.is_dir(follow_symlinks=False)
                if is_dir:
                    size, count = tree_size(Path(entry.path))
                else:
                    try:
                        size, count = entry.stat().st_size, 1
                    except OSError:
                        continue
                children.append({
                    "name": entry.name, "is_dir": is_dir,
                    "total_bytes": size, "file_count": count,
                })
        children.sort(key=lambda c: c["total_bytes"], reverse=True)
        return {"root": root_name, "path": path, "children": children,
                "truncated": truncated}

    @app.post("/fs/delete")
    async def fs_delete(req: DeleteRequest):
        """Delete a file or (recursive=true) a directory. The roots
        themselves are never deletable."""
        target = resolve_jailed(req.root_name, req.path)
        if target == roots[req.root_name].resolve() or not req.path.strip("/ "):
            raise HTTPException(400, "refusing to delete a filesystem root")
        if not target.exists():
            raise HTTPException(404, f"{req.root_name}:{req.path} not found")
        is_dir = target.is_dir()
        if is_dir and not req.recursive:
            raise HTTPException(
                409, f"{req.path} is a directory; pass recursive=true "
                     f"to delete it and everything inside")
        try:
            if is_dir:
                shutil.rmtree(target)
            else:
                target.unlink()
        except PermissionError:
            # Job containers write outputs as root (uid 0) into root-owned
            # dirs, so the ubuntu sidecar can't unlink them. Retry with a
            # privileged remove, still confined to the jailed path above.
            _privileged_remove(target)
        except OSError as exc:
            # Most often the NFS "still open by a running job" shape.
            hint = _busy_hint(str(exc))
            if hint:
                raise HTTPException(409, f"could not delete: {hint} ({exc})")
            raise HTTPException(400, f"could not delete {req.path}: {exc}")
        return {"deleted": f"{req.root_name}:{req.path}"}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    # Loopback only — this is a hard security rule, not a default.
    uvicorn.run(app, host="127.0.0.1", port=9411)
