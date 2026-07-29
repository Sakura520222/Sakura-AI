import logging

from backend.core.access_log import (
    QuietSuccessfulAccessFilter,
    install_quiet_successful_access_filter,
)


def _access_record(method: str, path: str, status_code: int) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1", method, path, "1.1", status_code),
        exc_info=None,
    )


def test_quiet_access_filter_only_hides_successful_high_frequency_gets():
    access_filter = QuietSuccessfulAccessFilter()

    quiet_paths = (
        "/sse/events",
        "/activity/observability/api/sessions?limit=50",
        "/activity/observability/api/sessions/5/snapshot",
        "/activity/observability/api/sessions/5/stream",
        "/activity/observability/api/sessions/5/conversation/events?cursor=signed",
        "/agent-team/api/active-tasks",
        "/agent-team/api/tasks/5/status",
    )
    for path in quiet_paths:
        assert access_filter.filter(_access_record("GET", path, 200)) is False

    assert (
        access_filter.filter(
            _access_record(
                "GET",
                "/activity/observability/api/sessions/5/snapshot",
                500,
            )
        )
        is True
    )
    assert (
        access_filter.filter(
            _access_record(
                "POST",
                "/activity/observability/api/sessions/5/snapshot",
                200,
            )
        )
        is True
    )
    assert (
        access_filter.filter(
            _access_record(
                "GET",
                "/activity/observability/api/sessions/5/conversation",
                200,
            )
        )
        is True
    )


def test_quiet_access_filter_installation_is_idempotent():
    access_logger = logging.getLogger("uvicorn.access")
    original_filters = list(access_logger.filters)
    try:
        access_logger.filters = [
            item
            for item in access_logger.filters
            if not isinstance(item, QuietSuccessfulAccessFilter)
        ]
        install_quiet_successful_access_filter()
        install_quiet_successful_access_filter()

        installed = [
            item
            for item in access_logger.filters
            if isinstance(item, QuietSuccessfulAccessFilter)
        ]
        assert len(installed) == 1
    finally:
        access_logger.filters = original_filters
