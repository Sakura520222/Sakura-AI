"""SQLAlchemy 时间类型：数据库存 UTC，领域层始终得到 aware UTC。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """带严格 aware 校验的跨数据库 UTC 时间列类型。"""

    impl = DateTime
    cache_ok = True
    python_type = datetime

    def load_dialect_impl(self, dialect):
        # SQLite ignores timezone=True but values are normalized on both sides;
        # native server dialects retain an explicit timezone-aware declaration.
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTCDateTime 只接受 aware datetime")
        normalized = value.astimezone(UTC)
        # SQLite and MySQL/MariaDB have no native timezone-aware timestamp
        # storage.  Their connection session is fixed to UTC, so pass a naive
        # UTC value only at this driver boundary and restore awareness on read.
        if dialect.name in {"sqlite", "mysql", "mariadb"}:
            return normalized.replace(tzinfo=None)
        return normalized

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            # SQLite and MySQL TIMESTAMP drivers may strip tzinfo; their session
            # is fixed to UTC by database initialization, so attach UTC only at
            # the storage boundary and never leak a naive value to the domain.
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
