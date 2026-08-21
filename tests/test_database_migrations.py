"""Regression tests for idempotent cross-dialect schema upgrades."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import Boolean, Column, Index, String, create_engine, inspect, text
from sqlalchemy.dialects import mysql, postgresql

from backend.models.database import (
    _OBSERVABILITY_TRIGGER_UNIQUE_INDEX_NAME,
    PRReviewIncrementalQueue,
    _build_add_column_sql,
    _ensure_observability_trigger_unique_index,
)


class _SyncConnectionAdapter:
    """Small async-connection facade for testing a ``run_sync`` helper."""

    def __init__(self, connection):
        self.connection = connection
        self.dialect = connection.dialect

    async def run_sync(self, callback):
        return callback(self.connection)


def _drop_queue_indexes(engine) -> None:
    table = PRReviewIncrementalQueue.__table__
    for index in list(table.indexes):
        index.drop(engine, checkfirst=True)


def _make_queue_engine():
    engine = create_engine("sqlite:///:memory:")
    PRReviewIncrementalQueue.__table__.create(engine)
    _drop_queue_indexes(engine)
    return engine


def test_add_column_sql_uses_active_dialect_identifier_quotes():
    column = Column("select", String(20), nullable=False, default="ready")

    mysql_sql = _build_add_column_sql(mysql.dialect(), "order", column)
    postgres_sql = _build_add_column_sql(postgresql.dialect(), "order", column)

    assert mysql_sql == (
        "ALTER TABLE `order` ADD COLUMN `select` VARCHAR(20) NOT NULL DEFAULT 'ready'"
    )
    assert postgres_sql == (
        'ALTER TABLE "order" ADD COLUMN "select" VARCHAR(20) NOT NULL DEFAULT \'ready\''
    )


def test_add_column_sql_uses_postgresql_boolean_literals():
    column = Column("enabled", Boolean, nullable=False, default=True)

    assert _build_add_column_sql(postgresql.dialect(), "flags", column).endswith(
        "BOOLEAN NOT NULL DEFAULT TRUE"
    )
    assert _build_add_column_sql(mysql.dialect(), "flags", column).endswith(
        "BOOL NOT NULL DEFAULT 1"
    )


@pytest.mark.asyncio
async def test_observability_trigger_unique_index_is_created_idempotently():
    engine = _make_queue_engine()
    try:
        with engine.begin() as connection:
            adapted = _SyncConnectionAdapter(connection)
            assert (
                await _ensure_observability_trigger_unique_index(
                    adapted, logging.getLogger(__name__)
                )
                is True
            )
            assert (
                await _ensure_observability_trigger_unique_index(
                    adapted, logging.getLogger(__name__)
                )
                is False
            )

        indexes = inspect(engine).get_indexes(PRReviewIncrementalQueue.__tablename__)
        matching = [
            index
            for index in indexes
            if index["name"] == _OBSERVABILITY_TRIGGER_UNIQUE_INDEX_NAME
        ]
        assert matching and matching[0]["unique"]
        assert matching[0]["column_names"] == ["observability_trigger_id"]
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_observability_trigger_duplicate_rows_fail_closed():
    engine = _make_queue_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO pr_review_incremental_queue "
                    "(repo_owner, repo_name, repo_full_name, pr_number, head_sha, "
                    "observability_trigger_id, status, created_at) "
                    "VALUES ('owner', 'repo', 'owner/repo', 1, 'sha-a', 42, "
                    "'pending', CURRENT_TIMESTAMP), "
                    "('owner', 'repo', 'owner/repo', 1, 'sha-b', 42, "
                    "'pending', CURRENT_TIMESTAMP)"
                )
            )
            adapted = _SyncConnectionAdapter(connection)
            with pytest.raises(RuntimeError, match="duplicate non-NULL"):
                await _ensure_observability_trigger_unique_index(
                    adapted, logging.getLogger(__name__)
                )

        indexes = inspect(engine).get_indexes(PRReviewIncrementalQueue.__tablename__)
        assert not any(
            index["name"] == _OBSERVABILITY_TRIGGER_UNIQUE_INDEX_NAME
            for index in indexes
        )
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_existing_unique_trigger_index_is_left_untouched():
    engine = _make_queue_engine()
    try:
        with engine.begin() as connection:
            Index(
                "existing_unique_trigger_index",
                PRReviewIncrementalQueue.__table__.c.observability_trigger_id,
                unique=True,
            ).create(connection)
            adapted = _SyncConnectionAdapter(connection)
            assert (
                await _ensure_observability_trigger_unique_index(
                    adapted, logging.getLogger(__name__)
                )
                is False
            )

        indexes = inspect(engine).get_indexes(PRReviewIncrementalQueue.__tablename__)
        assert any(
            index["name"] == "existing_unique_trigger_index" and index["unique"]
            for index in indexes
        )
        assert not any(
            index["name"] == _OBSERVABILITY_TRIGGER_UNIQUE_INDEX_NAME
            for index in indexes
        )
    finally:
        engine.dispose()
