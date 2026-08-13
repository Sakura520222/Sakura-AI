"""Jinja helpers for application-zone display of aware instants."""

from __future__ import annotations

from datetime import datetime

from backend.core.time_service import (
    datetime_local_fold as get_datetime_local_fold,
)
from backend.core.time_service import (
    datetime_local_value,
    get_time_service,
    parse_rfc3339,
)


def _coerce_datetime(value: datetime | str) -> datetime | None:
    """Convert a domain datetime or strict RFC3339 string for display.

    A small number of protocol-backed WebUI contexts (updater release/cache
    data and the dashboard JSON fragment) intentionally carry RFC3339 strings
    instead of ORM datetimes.  Invalid/non-time strings are returned by the
    caller unchanged; they are never guessed as local time.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return parse_rfc3339(value)
        except ValueError:
            return None
    return None


def _display(value: datetime | str | None, *, seconds: bool = True) -> str:
    if value is None:
        return ""
    parsed = _coerce_datetime(value)
    if parsed is None:
        return value if isinstance(value, str) else ""
    return get_time_service().format_display(parsed, seconds=seconds)


def format_datetime(value: datetime | str | None) -> str:
    return _display(value)


def format_datetime_short(value: datetime | str | None) -> str:
    return _display(value, seconds=False)


def format_date(value: datetime | None) -> str:
    if value is None:
        return ""
    return get_time_service().to_app_timezone(value).strftime("%Y-%m-%d")


def datetime_local(value: datetime | None) -> str:
    if value is None:
        return ""
    return datetime_local_value(value, get_time_service().zone)


def datetime_local_fold(value: datetime | None) -> int | str:
    if value is None:
        return ""
    return get_datetime_local_fold(value, get_time_service().zone)


def register_time_filters(environment) -> None:
    environment.filters.update(
        {
            "format_datetime": format_datetime,
            "format_datetime_short": format_datetime_short,
            "format_date": format_date,
            "datetime_local": datetime_local,
            "datetime_local_fold": datetime_local_fold,
        }
    )
