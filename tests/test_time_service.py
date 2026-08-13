from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.core.time_service import (
    DateTimeLocalError,
    InvalidTimezoneError,
    TimeService,
    filename_timestamp,
    format_rfc3339,
    parse_datetime_local,
    parse_rfc3339,
    resolve_timezone,
)


class FakeClock:
    def __init__(self) -> None:
        self.instant = datetime(2026, 8, 12, 13, 45, 30, 123456, tzinfo=UTC)
        self.ticks = 42.5

    def now_utc(self) -> datetime:
        return self.instant

    def monotonic(self) -> float:
        return self.ticks


def test_resolve_timezone_accepts_system_utc_and_iana(monkeypatch):
    monkeypatch.setattr(
        "backend.core.time_service.get_localzone_name", lambda: "Asia/Shanghai"
    )
    assert resolve_timezone("system").key == "Asia/Shanghai"
    assert resolve_timezone("UTC").key == "UTC"
    assert resolve_timezone("America/New_York").key == "America/New_York"


@pytest.mark.parametrize("value", ["", "CST", "UTC+08:00", "not/a-zone"])
def test_resolve_timezone_rejects_ambiguous_or_invalid_values(value):
    with pytest.raises(InvalidTimezoneError):
        resolve_timezone(value)


def test_system_timezone_failure_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        "backend.core.time_service.get_localzone_name",
        lambda: (_ for _ in ()).throw(RuntimeError("no zone")),
    )
    with pytest.raises(InvalidTimezoneError):
        resolve_timezone("system")


def test_rfc3339_is_strict_utc_and_rejects_naive():
    instant = datetime(2026, 8, 12, 21, 45, 30, 123456, tzinfo=UTC)
    encoded = format_rfc3339(instant)
    assert encoded == "2026-08-12T21:45:30.123456Z"
    assert parse_rfc3339(encoded) == instant
    assert parse_rfc3339("2026-08-12T21:45:30+08:00") == datetime(
        2026, 8, 12, 13, 45, 30, tzinfo=UTC
    )
    with pytest.raises(ValueError):
        parse_rfc3339("2026-08-12T21:45:30")
    with pytest.raises(ValueError):
        parse_rfc3339("2026-08-12 21:45:30+00:00")
    with pytest.raises(ValueError):
        parse_rfc3339("2026-08-12T21:45+00:00")


def test_time_service_freezes_resolved_zone_and_injects_clock(monkeypatch):
    clock = FakeClock()
    service = TimeService("Asia/Shanghai", clock=clock)
    monkeypatch.setattr(
        "backend.core.time_service.get_localzone_name", lambda: "UTC"
    )
    assert service.configured_timezone == "Asia/Shanghai"
    assert service.resolved_timezone == "Asia/Shanghai"
    assert service.now_utc() == clock.instant
    assert service.monotonic() == 42.5
    assert service.to_app_timezone(clock.instant).hour == 21


def test_datetime_local_gap_and_fold_require_explicit_fold():
    with pytest.raises(DateTimeLocalError, match="gap"):
        parse_datetime_local("2026-03-08T02:30", "America/New_York")
    with pytest.raises(DateTimeLocalError, match="fold"):
        parse_datetime_local("2026-11-01T01:30", "America/New_York")
    early = parse_datetime_local("2026-11-01T01:30", "America/New_York", fold=0)
    late = parse_datetime_local("2026-11-01T01:30", "America/New_York", fold=1)
    assert early < late


def test_filename_timestamp_uses_local_calendar_offset_and_zone():
    value = datetime(2026, 8, 12, 13, 45, tzinfo=UTC)
    result = filename_timestamp(value, "Asia/Shanghai")
    assert result.startswith("20260812-214500-+0800-Asia-Shanghai")
