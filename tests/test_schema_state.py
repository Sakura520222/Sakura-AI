"""Successful Setup is reused; interrupted, changed, or damaged schemas are not."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects import mysql
from sqlalchemy.pool import StaticPool

from backend.models import database, schema_state


@pytest.fixture
def schema_connection(monkeypatch):
    database._ensure_model_modules_imported()
    engine = create_engine("sqlite://", poolclass=StaticPool)
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(schema_state, "_migration_revision", lambda: "migration-code-1")
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()


def test_completed_schema_has_one_durable_checkpoint(schema_connection):
    connection = schema_connection
    assert schema_state._has_checkpoint(connection) is False
    schema_state._record_checkpoint(connection)
    assert schema_state._has_checkpoint(connection) is True
    schema_state._record_checkpoint(connection)
    assert len(connection.execute(select(database.SchemaMigration.id)).all()) == 1


def test_migration_code_change_invalidates_checkpoint(schema_connection, monkeypatch):
    connection = schema_connection
    schema_state._record_checkpoint(connection)
    monkeypatch.setattr(schema_state, "_migration_revision", lambda: "migration-code-2")
    assert schema_state._has_checkpoint(connection) is False


def test_physical_column_change_invalidates_checkpoint(schema_connection):
    connection = schema_connection
    schema_state._record_checkpoint(connection)
    connection.execute(text("ALTER TABLE app_config ADD COLUMN checkpoint_probe TEXT"))
    assert schema_state._has_checkpoint(connection) is False


def test_physical_index_change_invalidates_checkpoint(schema_connection):
    connection = schema_connection
    schema_state._record_checkpoint(connection)
    connection.execute(text("CREATE INDEX checkpoint_probe_idx ON app_config (key_name)"))
    assert schema_state._has_checkpoint(connection) is False


def test_missing_table_cannot_reuse_or_record_checkpoint(schema_connection):
    connection = schema_connection
    schema_state._record_checkpoint(connection)
    connection.execute(text("DROP TABLE app_config"))
    assert schema_state._has_checkpoint(connection) is False
    with pytest.raises(RuntimeError, match="incomplete"):
        schema_state._record_checkpoint(connection)


def test_missing_required_column_cannot_be_checkpointed(schema_connection):
    connection = schema_connection
    schema_state._record_checkpoint(connection)
    connection.execute(text("ALTER TABLE app_config DROP COLUMN description"))
    assert schema_state._has_checkpoint(connection) is False
    with pytest.raises(RuntimeError, match="incomplete"):
        schema_state._record_checkpoint(connection)


@pytest.mark.asyncio
async def test_current_schema_skips_setup_ddl_and_defaults(monkeypatch):
    from backend.core import setup_service as setup

    monkeypatch.setattr(
        setup,
        "check_database",
        AsyncMock(return_value={"success": True, "dialect": "mysql"}),
    )
    monkeypatch.setattr(database, "async_engine", object())
    monkeypatch.setattr(schema_state, "schema_is_current", AsyncMock(return_value=True))
    migrate = AsyncMock()
    defaults = AsyncMock()
    record = AsyncMock()
    monkeypatch.setattr(database, "migrate_schema_async", migrate)
    monkeypatch.setattr(database, "insert_default_configs_async", defaults)
    monkeypatch.setattr(schema_state, "record_schema_checkpoint", record)
    await setup.SetupService().init_database("mysql://u:p@db/test")
    migrate.assert_not_awaited()
    defaults.assert_not_awaited()
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_failure_does_not_record_completed_setup(monkeypatch):
    from backend.core import setup_service as setup

    monkeypatch.setattr(
        setup,
        "check_database",
        AsyncMock(return_value={"success": True, "dialect": "mysql"}),
    )
    monkeypatch.setattr(database, "async_engine", object())
    monkeypatch.setattr(
        schema_state, "schema_is_current", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(database, "migrate_schema_async", AsyncMock())
    monkeypatch.setattr(
        database,
        "insert_default_configs_async",
        AsyncMock(side_effect=RuntimeError("defaults unavailable")),
    )
    record = AsyncMock()
    monkeypatch.setattr(schema_state, "record_schema_checkpoint", record)
    with pytest.raises(setup.DatabaseSetupError):
        await setup.SetupService().init_database("mysql://u:p@db/test")
    record.assert_not_awaited()


class AgentTextConnection:
    dialect = mysql.dialect()

    def __init__(self, *, fail_column=None, failure=None):
        self.columns = {
            "agent_team_messages": {
                "content": {
                    "name": "content",
                    "type": mysql.TEXT(charset="utf8mb4", collation="utf8mb4_bin"),
                    "nullable": True,
                    "default": "'kept'",
                    "comment": "keep this",
                },
                "message_json": {
                    "name": "message_json",
                    "type": mysql.LONGTEXT(),
                    "nullable": False,
                },
            },
            "agent_team_tool_calls": {
                "arguments_json": {
                    "name": "arguments_json",
                    "type": mysql.MEDIUMTEXT(),
                    "nullable": True,
                },
            },
        }
        self.statements = []
        self.fail_column = fail_column
        self.failure = failure or RuntimeError("DDL interrupted")

    async def run_sync(self, callback):
        return callback(self)

    async def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        for table, columns in self.columns.items():
            for name, column in columns.items():
                if table in sql and name in sql:
                    if name == self.fail_column:
                        raise self.failure
                    column["type"] = mysql.LONGTEXT()
                    return
        raise AssertionError(sql)


def agent_inspector(connection):
    return SimpleNamespace(
        get_table_names=lambda: list(connection.columns),
        get_columns=lambda table: list(connection.columns[table].values()),
    )


@pytest.mark.asyncio
async def test_agent_longtext_only_expands_legacy_columns_and_preserves_definition(
    monkeypatch,
):
    monkeypatch.setattr("sqlalchemy.inspect", agent_inspector)
    connection = AgentTextConnection()
    migrate = database._ensure_agent_message_longtext_columns
    assert await migrate(connection, logging.getLogger(__name__)) is True
    assert len(connection.statements) == 2
    assert "CHARACTER SET utf8mb4" in connection.statements[0]
    assert "COLLATE utf8mb4_bin" in connection.statements[0]
    assert "DEFAULT 'kept'" in connection.statements[0]
    assert "COMMENT 'keep this'" in connection.statements[0]
    assert await migrate(connection, logging.getLogger(__name__)) is False
    assert len(connection.statements) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("interrupted"), asyncio.CancelledError()]
)
async def test_partial_agent_text_upgrade_resumes_remaining_column(
    monkeypatch, failure
):
    monkeypatch.setattr("sqlalchemy.inspect", agent_inspector)
    connection = AgentTextConnection(fail_column="arguments_json", failure=failure)
    with pytest.raises(type(failure)):
        await database._ensure_agent_message_longtext_columns(
            connection, logging.getLogger(__name__)
        )
    connection.fail_column = None
    connection.statements.clear()
    assert (
        await database._ensure_agent_message_longtext_columns(
            connection, logging.getLogger(__name__)
        )
        is True
    )
    assert len(connection.statements) == 1
    assert "arguments_json" in connection.statements[0]
