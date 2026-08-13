"""Updater protocol time helpers (UTC RFC3339 ``Z`` only)."""

from datetime import UTC, datetime


def format_rfc3339(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("updater timestamps require an aware datetime")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def now_rfc3339() -> str:
    return format_rfc3339(datetime.now(UTC))
