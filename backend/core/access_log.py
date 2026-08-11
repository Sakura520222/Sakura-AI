"""Uvicorn access-log filtering for high-frequency browser subscriptions."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

_QUIET_ACTIVITY_SESSION_PATH = re.compile(
    r"^/activity/observability/api/sessions/\d+/"
    r"(?:snapshot|stream|conversation/events)/?$"
)


class QuietSuccessfulAccessFilter(logging.Filter):
    """Hide successful monitoring traffic while preserving failures."""

    def filter(self, record: logging.LogRecord) -> bool:
        request = self._request_from_record(record)
        if request is None:
            return True

        method, path, status_code = request
        if method != "GET" or not 200 <= status_code < 400:
            return True

        normalized_path = path.partition("?")[0]
        return not (
            normalized_path == "/sse/events"
            or normalized_path == "/health"
            or normalized_path == "/activity/observability/api/sessions"
            or _QUIET_ACTIVITY_SESSION_PATH.fullmatch(normalized_path)
            or normalized_path == "/agent-team/api/active-tasks"
            or normalized_path.startswith("/agent-team/api/tasks/")
            or normalized_path == "/agent-team/list-fragment"
        )

    @staticmethod
    def _request_from_record(
        record: logging.LogRecord,
    ) -> tuple[str, str, int] | None:
        args: Any = record.args
        if not isinstance(args, Sequence) or isinstance(args, (str, bytes)):
            return None
        if len(args) < 5:
            return None

        try:
            method = str(args[1]).upper()
            path = str(args[2])
            status_code = int(args[4])
        except (TypeError, ValueError):
            return None
        return method, path, status_code


def install_quiet_successful_access_filter() -> None:
    """Install the filter once for both CLI and ``python backend/main.py`` starts."""

    access_logger = logging.getLogger("uvicorn.access")
    if any(
        isinstance(existing, QuietSuccessfulAccessFilter)
        for existing in access_logger.filters
    ):
        return
    access_logger.addFilter(QuietSuccessfulAccessFilter())
