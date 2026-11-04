"""Bounded, read-only lookup of sanitized local structured application logs."""

from __future__ import annotations

import json

from backend.core import logging_bridge
from backend.core.log_sanitizer import sanitize_text

# A support lookup must not scan an entire deployment's retention window or
# block the event loop. At most 4 MiB and 200 matching records per request.
LOG_QUERY_MAX_BYTES = 4 * 1024 * 1024
LOG_QUERY_MAX_RECORDS = 200


def session_logs(session_id: int) -> str:
    remaining = LOG_QUERY_MAX_BYTES
    result = []
    directory = logging_bridge.APP_LOG_DIRECTORY
    paths = []
    for path in directory.glob("app_*.log"):
        try:
            if path.is_file() and not path.is_symlink():
                paths.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(paths, reverse=True):
        if remaining <= 0 or len(result) >= LOG_QUERY_MAX_RECORDS:
            break
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, 2)
                start = max(0, size - remaining)
                stream.seek(start)
                data = stream.read(remaining)
                remaining -= len(data)
        except OSError:
            continue
        lines = data.splitlines()
        if start:
            lines = lines[1:]  # never interpret a partial JSON record
        for line in reversed(lines):
            try:
                entry = json.loads(line)
                if entry["record"]["extra"].get("session_id") != session_id:
                    continue
                result.append(sanitize_text(str(entry["text"])))
                if len(result) >= LOG_QUERY_MAX_RECORDS:
                    break
            except ValueError, KeyError, TypeError, AttributeError:
                # Legacy text logs and interrupted final writes are not indexed.
                continue
    return "".join(reversed(result))
