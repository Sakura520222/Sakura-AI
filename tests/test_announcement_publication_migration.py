"""SQLite regression coverage for announcement publication schema upgrades.

The production auto-migrator uses an async engine, while this environment has
no ``aiosqlite`` driver.  These tests therefore exercise the same relevant
DDL path with a real in-memory SQLite engine: ``checkfirst`` creation of the
two history tables, Inspector-based missing-column detection, and the actual
``_build_add_column_sql`` helper.  They intentionally do not claim coverage of
the auto-migrator's unrelated model initialization steps.
"""

from __future__ import annotations

import pytest
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from backend.models.announcement_models import (
    Announcement,
    AnnouncementDeliveryHistory,
    AnnouncementPublicationHistory,
    AnnouncementRead,
    NotificationDelivery,
)
from backend.models.database import _build_add_column_sql
from backend.models.telegram_models import TelegramUser


class _AsyncSQLiteConnection:
    """Small async-shaped facade backed by a real synchronous SQLite engine."""

    def __init__(self, engine: Engine):
        self.engine = engine

    @property
    def dialect(self):
        return self.engine.dialect

    async def run_sync(self, callback):
        with self.engine.begin() as connection:
            return callback(connection)

    async def execute(self, statement, parameters=None):
        with self.engine.begin() as connection:
            return connection.execute(statement, parameters or {})


def _legacy_metadata() -> MetaData:
    metadata = MetaData()
    TelegramUser.__table__.to_metadata(metadata)
    announcements = Announcement.__table__.to_metadata(metadata)
    AnnouncementRead.__table__.to_metadata(metadata)
    deliveries = NotificationDelivery.__table__.to_metadata(metadata)

    # Reproduce an old schema before publication_version and delivery leases
    # were introduced.  The model indexes for removed columns must go with
    # those columns.
    for table, column_name in (
        (announcements, "publication_version"),
        (deliveries, "publication_version"),
        (deliveries, "claim_token"),
        (deliveries, "claim_until"),
    ):
        table._columns.remove(table.c[column_name])
        for index in tuple(table.indexes):
            if any(column.name == column_name for column in index.columns):
                table.indexes.remove(index)
    return metadata


@pytest.fixture
def legacy_sqlite_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _legacy_metadata().create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_legacy_rows(engine: Engine) -> None:
    metadata = _legacy_metadata()
    users = metadata.tables[TelegramUser.__tablename__]
    announcements = metadata.tables[Announcement.__tablename__]
    reads = metadata.tables[AnnouncementRead.__tablename__]
    deliveries = metadata.tables[NotificationDelivery.__tablename__]
    with engine.begin() as connection:
        connection.execute(
            users.insert().values(
                id=7,
                telegram_id=7007,
                github_username="legacy-admin",
                role="admin",
                is_active=True,
            )
        )
        connection.execute(
            announcements.insert().values(
                id=11,
                title="Old announcement",
                content="Published before migration",
                status="published",
                created_by=7,
            )
        )
        connection.execute(reads.insert().values(id=13, announcement_id=11, user_id=7))
        connection.execute(
            deliveries.insert().values(
                id=17,
                announcement_id=11,
                user_id=7,
                channel="email",
                status="sent",
            )
        )


def _legacy_snapshot(engine: Engine) -> dict[str, list[tuple]]:
    queries = {
        "users": """
            SELECT id, telegram_id, github_username, role, is_active
            FROM telegram_users ORDER BY id
        """,
        "announcements": """
            SELECT id, title, content, status, created_by
            FROM announcements ORDER BY id
        """,
        "reads": """
            SELECT id, announcement_id, user_id FROM announcement_reads ORDER BY id
        """,
        "deliveries": """
            SELECT id, announcement_id, user_id, channel, status
            FROM notification_deliveries ORDER BY id
        """,
    }
    with engine.connect() as connection:
        return {
            name: [tuple(row) for row in connection.execute(text(query)).all()]
            for name, query in queries.items()
        }


async def _migrate_publication_schema(connection: _AsyncSQLiteConnection) -> None:
    """Mirror the publication-specific branch of the production migrator."""
    await connection.run_sync(
        lambda sync_connection: AnnouncementPublicationHistory.__table__.create(
            sync_connection, checkfirst=True
        )
    )
    await connection.run_sync(
        lambda sync_connection: AnnouncementDeliveryHistory.__table__.create(
            sync_connection, checkfirst=True
        )
    )

    for table_name, column in (
        (Announcement.__tablename__, Announcement.__table__.c.publication_version),
        (
            NotificationDelivery.__tablename__,
            NotificationDelivery.__table__.c.publication_version,
        ),
        (
            NotificationDelivery.__tablename__,
            NotificationDelivery.__table__.c.claim_token,
        ),
        (
            NotificationDelivery.__tablename__,
            NotificationDelivery.__table__.c.claim_until,
        ),
    ):
        existing_columns = await connection.run_sync(
            lambda sync_connection, table_name=table_name: {
                item["name"]
                for item in inspect(sync_connection).get_columns(table_name)
            }
        )
        if column.name not in existing_columns:
            await connection.execute(
                text(_build_add_column_sql(connection.dialect, table_name, column))
            )


@pytest.mark.asyncio
async def test_publication_schema_upgrade_is_idempotent_and_preserves_legacy_rows(
    legacy_sqlite_engine,
):
    _seed_legacy_rows(legacy_sqlite_engine)
    before = _legacy_snapshot(legacy_sqlite_engine)
    connection = _AsyncSQLiteConnection(legacy_sqlite_engine)

    await _migrate_publication_schema(connection)
    first = _legacy_snapshot(legacy_sqlite_engine)
    first_tables = set(inspect(legacy_sqlite_engine).get_table_names())
    await _migrate_publication_schema(connection)
    second = _legacy_snapshot(legacy_sqlite_engine)
    second_tables = set(inspect(legacy_sqlite_engine).get_table_names())

    assert before == first == second
    assert {
        "announcement_publication_history",
        "announcement_delivery_history",
    } <= first_tables
    assert first_tables == second_tables

    for table_name in ("announcements", "notification_deliveries"):
        columns = {
            column["name"]: column
            for column in inspect(legacy_sqlite_engine).get_columns(table_name)
        }
        publication_version = columns["publication_version"]
        assert publication_version["nullable"] is False
        assert str(publication_version["default"]) == "1"

    delivery_columns = {
        column["name"]: column
        for column in inspect(legacy_sqlite_engine).get_columns(
            "notification_deliveries"
        )
    }
    assert delivery_columns["claim_token"]["nullable"] is True
    assert delivery_columns["claim_until"]["nullable"] is True

    with legacy_sqlite_engine.connect() as connection:
        assert connection.execute(
            text("SELECT id, publication_version FROM announcements ORDER BY id")
        ).all() == [(11, 1)]
        assert connection.execute(
            text(
                "SELECT id, publication_version "
                "FROM notification_deliveries ORDER BY id"
            )
        ).all() == [(17, 1)]


def test_publication_version_add_column_sql_has_default_one_for_supported_dialects():
    columns = {
        "announcements": Announcement.__table__.c.publication_version,
        "notification_deliveries": NotificationDelivery.__table__.c.publication_version,
    }
    expected = {
        table_name: (
            f"ALTER TABLE {table_name} ADD COLUMN publication_version INTEGER "
            "NOT NULL DEFAULT 1"
        )
        for table_name in columns
    }

    for dialect in (mysql.dialect(), postgresql.dialect()):
        for table_name, column in columns.items():
            assert (
                _build_add_column_sql(dialect, table_name, column)
                == expected[table_name]
            )
