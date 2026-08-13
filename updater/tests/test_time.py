from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sakura_ai_updater.time import format_rfc3339, now_rfc3339


def test_format_rfc3339_normalizes_non_utc_aware_datetime_to_z():
    value = datetime(2026, 8, 12, 20, 34, 56, 123456, tzinfo=timezone(timedelta(hours=8)))

    assert format_rfc3339(value) == "2026-08-12T12:34:56.123456Z"


def test_format_rfc3339_rejects_naive_datetime():
    with pytest.raises(ValueError, match="aware"):
        format_rfc3339(datetime(2026, 8, 12, 12, 34, 56))


def test_now_rfc3339_is_strict_utc_z(monkeypatch):
    class FrozenDateTime:
        @classmethod
        def now(cls, tz):
            assert tz is UTC
            return datetime(2026, 8, 12, 12, 34, 56, 123456, tzinfo=UTC)

    monkeypatch.setattr("sakura_ai_updater.time.datetime", FrozenDateTime)

    assert now_rfc3339() == "2026-08-12T12:34:56.123456Z"
