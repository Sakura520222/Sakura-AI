from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import mysql, postgresql, sqlite

from backend.models.time_types import UTCDateTime


def test_bind_rejects_naive_and_normalizes_to_utc():
    type_ = UTCDateTime()
    value = datetime(2026, 8, 12, 21, 45, tzinfo=timezone(timedelta(hours=8)))
    assert type_.process_bind_param(value, sqlite.dialect()) == datetime(
        2026, 8, 12, 13, 45
    )
    assert type_.process_bind_param(value, postgresql.dialect()) == datetime(
        2026, 8, 12, 13, 45, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="aware"):
        type_.process_bind_param(datetime(2026, 8, 12, 13, 45), sqlite.dialect())


def test_result_always_returns_aware_utc():
    type_ = UTCDateTime()
    assert (
        type_.process_result_value(
            datetime(2026, 8, 12, 13, 45), sqlite.dialect()
        ).tzinfo
        == UTC
    )
    assert type_.process_result_value(
        datetime(2026, 8, 12, 21, 45, tzinfo=timezone(timedelta(hours=8))),
        mysql.dialect(),
    ) == datetime(2026, 8, 12, 13, 45, tzinfo=UTC)


@pytest.mark.parametrize(
    "dialect", [sqlite.dialect(), mysql.dialect(), postgresql.dialect()]
)
def test_impl_uses_timezone_aware_datetime_for_server_dialects(dialect):
    impl = UTCDateTime().load_dialect_impl(dialect)
    assert impl.timezone is True or dialect.name == "sqlite"
