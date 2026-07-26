"""TaskQueue interface, backed by SQLite.

The spec requires task dispatch to live behind a single interface so
alternative backends (Redis, cloud queues) could be swapped in later
without touching callers. SQLiteTaskQueue is the only implementation today:
tasks, their state transitions, and their logs all live in the same
database as everything else, so history survives restarts for free.
"""

from __future__ import annotations

import abc
import re

from .db import Database, utcnow


# One line per layer per state during a `docker pull` in captured (non-TTY)
# output: dozens of "<hash>: Waiting / Downloading / Pull complete" lines that
# bury a job's real output and burn agent tokens on every log read. They carry
# nothing the surviving lines don't - "Pulling from ...", "Digest: ...", and
# "Status: Downloaded ..." are NOT matched here, so image identity and the
# pull's start/finish still show. The full docker output is also archived to
# the per-task log file on the instance.
_DOCKER_LAYER_NOISE = re.compile(
    r"^[0-9a-f]{12}: "
    r"(Waiting|Downloading|Verifying Checksum|Download complete|Extracting|"
    r"Pull complete|Already exists|Pulling fs layer|Preparing|"
    r"Retrying in \d+ seconds?)\b"
)


def is_docker_pull_noise(line: str) -> bool:
    """True when a captured stdout line is per-layer `docker pull` churn that
    is safe to drop from the stored job log (see the pattern above)."""
    return bool(_DOCKER_LAYER_NOISE.match(line))


def collapse_progress(line: str) -> str:
    """Render a captured stdout line the way a terminal would.

    Progress bars (pip, training loops, huggingface downloads, rsync) redraw
    one line by emitting `\\r` and overwriting from the start. When we capture
    stdout by splitting on `\\n` only, all of a bar's intermediate frames
    arrive glued into ONE line, separated by `\\r` - thousands of characters
    of "10%\\r11%\\r12%..." that a terminal never actually shows and that
    bloats the log and burns agent tokens on read. A terminal only ever
    displays the segment after the last `\\r`, so that is what we store.
    """
    line = line.rstrip("\r")
    if "\r" in line:
        line = line.rsplit("\r", 1)[-1]
    return line


class TaskQueue(abc.ABC):
    @abc.abstractmethod
    def enqueue(self, *, template: str, parameters: dict,
                auto_manage: bool = False, gpu_type: str | None = None,
                region: str | None = None,
                filesystem: str | None = None,
                target_instance_id: str | None = None) -> str:
        """Add a task; returns its id.

        When auto_manage is set, the dispatcher owns the instance lifecycle
        for this job (launch -> run -> sync -> terminate) using the supplied
        gpu_type/region/filesystem."""

    @abc.abstractmethod
    def next_queued(self) -> dict | None:
        """The oldest queued task, or None."""

    @abc.abstractmethod
    def mark_running(self, task_id: str, instance_id: str) -> None: ...

    @abc.abstractmethod
    def mark_finished(self, task_id: str, *, exit_code: int,
                      output_paths: list[str], error: str = "") -> None: ...

    @abc.abstractmethod
    def append_log(self, task_id: str, line: str) -> None: ...

    @abc.abstractmethod
    def get_logs(self, task_id: str, tail: int | None = None) -> list[dict]: ...

    @abc.abstractmethod
    def get(self, task_id: str) -> dict | None: ...

    @abc.abstractmethod
    def list(self) -> list[dict]: ...

    @abc.abstractmethod
    def running_count(self) -> int: ...

    @abc.abstractmethod
    def delete(self, task_id: str) -> None: ...

    @abc.abstractmethod
    def clear_finished(self) -> int: ...


class SQLiteTaskQueue(TaskQueue):
    def __init__(self, db: Database):
        self._db = db

    def enqueue(self, *, template: str, parameters: dict,
                auto_manage: bool = False, gpu_type: str | None = None,
                region: str | None = None,
                filesystem: str | None = None,
                target_instance_id: str | None = None) -> str:
        task_id = self._db.create_task(
            template=template, parameters=parameters, auto_manage=auto_manage,
            gpu_type=gpu_type, region=region, filesystem=filesystem,
            target_instance_id=target_instance_id)
        self._db.record_task_event(task_id, "queued")
        return task_id

    def next_queued(self) -> dict | None:
        return self._db.next_queued_task()

    def mark_running(self, task_id: str, instance_id: str) -> None:
        self._db.update_task(
            task_id, status="running", instance_id=instance_id,
            started_at=utcnow(),
        )

    def mark_finished(self, task_id: str, *, exit_code: int,
                      output_paths: list[str], error: str = "") -> None:
        self._db.update_task(
            task_id,
            status="succeeded" if exit_code == 0 and not error else "failed",
            finished_at=utcnow(),
            exit_code=exit_code,
            output_paths=output_paths,
            error=error or None,
        )

    def append_log(self, task_id: str, line: str) -> None:
        line = collapse_progress(line)
        if is_docker_pull_noise(line):
            return   # per-layer pull churn: dropped, never stored
        self._db.append_task_log(task_id, line)

    def get_logs(self, task_id: str, tail: int | None = None) -> list[dict]:
        return self._db.get_task_logs(task_id, tail)

    def get(self, task_id: str) -> dict | None:
        return self._db.get_task(task_id)

    def list(self) -> list[dict]:
        return self._db.list_tasks()

    def running_count(self) -> int:
        return self._db.running_task_count()

    def delete(self, task_id: str) -> None:
        self._db.delete_task(task_id)

    def clear_finished(self) -> int:
        return self._db.delete_finished_tasks()
