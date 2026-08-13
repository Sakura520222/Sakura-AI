"""In-memory bounded structured logs for update jobs."""

from __future__ import annotations

from collections import deque
from typing import Any

from sakura_ai_updater.time import now_rfc3339


def _timestamp() -> str:
    return now_rfc3339()


class JobLogBuffer:
    """A per-job ring buffer that never truncates an individual log entry."""

    def __init__(self, max_entries: int = 200) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._entries: deque[dict[str, Any]] = deque(maxlen=max_entries)
        self.truncated = False

    def append(
        self,
        message: str,
        *,
        level: str = "info",
        step: str | None = None,
        error_code: str | None = None,
        stderr_lines: list[str] | None = None,
        ts: str | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": ts or _timestamp(),
            "level": level,
            "step": step,
            "msg": message,
        }
        if error_code is not None:
            entry["error_code"] = error_code
        if stderr_lines is not None:
            entry["stderr_lines"] = list(stderr_lines)
        if len(self._entries) == self.max_entries:
            self.truncated = True
        self._entries.append(entry)
        return dict(entry)

    # ``add`` is a convenient alias used by a few integrations.
    add = append

    def extend(self, entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            self.append(
                str(entry.get("msg", "")),
                level=str(entry.get("level", "info")),
                step=entry.get("step"),
                error_code=entry.get("error_code"),
                stderr_lines=entry.get("stderr_lines"),
                ts=entry.get("ts"),
            )

    def clear(self) -> None:
        self._entries.clear()
        self.truncated = False

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._entries]

    def to_dict(self, job_id: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "logs": self.snapshot(),
            "truncated": self.truncated,
        }
        if job_id is not None:
            result["job_id"] = job_id
        return result

    def __len__(self) -> int:
        return len(self._entries)


class JobLogStore:
    """Manage one ring buffer per job while the daemon process is alive."""

    def __init__(self, max_entries: int = 200) -> None:
        self.max_entries = max_entries
        self._buffers: dict[str, JobLogBuffer] = {}

    def for_job(self, job_id: str) -> JobLogBuffer:
        buffer = self._buffers.get(job_id)
        if buffer is None:
            buffer = self._buffers[job_id] = JobLogBuffer(self.max_entries)
        return buffer

    def append(self, job_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        return self.for_job(job_id).append(message, **kwargs)

    def get(self, job_id: str) -> JobLogBuffer | None:
        return self._buffers.get(job_id)

    def snapshot(self, job_id: str) -> dict[str, Any]:
        buffer = self._buffers.get(job_id)
        return buffer.to_dict(job_id) if buffer else {"job_id": job_id, "logs": [], "truncated": False}

    def clear(self, job_id: str) -> None:
        self._buffers.pop(job_id, None)


# Names kept intentionally simple for external tests/integrations.
RingBuffer = JobLogBuffer

