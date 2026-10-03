"""Large Issue evidence storage and legacy MySQL schema upgrade regressions.

MySQL coverage uses dialect compilation and a stateful reflected-schema facade;
ORM persistence uses real SQLite, not a live MySQL server.
"""

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from backend.models import database
from backend.models.database import IssueAnalysis, _build_add_column_sql
from backend.services.issue_service import IssueService
from backend.services.issues.relation_analyzer import (
    IssueRelationResult,
    parse_issue_relations,
)

_COLUMNS = ("issue_relations", "analysis_detail")


@pytest.mark.parametrize(
    ("dialect", "expected_type"),
    [
        (mysql.dialect(), "LONGTEXT"),
        (postgresql.dialect(), "TEXT"),
        (sqlite.dialect(), "TEXT"),
    ],
)
def test_fresh_schema_and_additive_columns_use_large_dialect_storage(
    dialect, expected_type
):
    ddl = str(CreateTable(IssueAnalysis.__table__).compile(dialect=dialect))
    for name in _COLUMNS:
        assert f"{name} {expected_type}" in ddl
        column = IssueAnalysis.__table__.c[name]
        assert column.nullable is True
        assert _build_add_column_sql(dialect, "issue_analyses", column).endswith(
            f"{name} {expected_type} NULL"
        )


class _SchemaConnection:
    """Reflect and execute the migration's DDL against an evolving MySQL schema.

    This models MySQL's independently committed ALTERs, including a failed
    second column, without representing it as live database coverage.
    """

    def __init__(self, columns, *, fail_column=None, failure=None):
        self.dialect = mysql.dialect()
        self.columns = columns
        self.fail_column = fail_column
        self.failure = failure or RuntimeError("DDL unavailable")
        self.statements = []
        self.migration_versions = []

    async def run_sync(self, callback):
        return callback(self)

    async def execute(self, statement, parameters=None):
        sql = str(statement)
        self.statements.append(sql)
        match = re.fullmatch(
            r"ALTER TABLE `?issue_analyses`? (ADD|MODIFY) COLUMN `?(\w+)`? (.+)", sql
        )
        if match:
            operation, name, declaration = match.groups()
            if name == self.fail_column:
                raise self.failure
            if operation == "MODIFY":
                assert name in self.columns, "Cannot modify an absent column"
            else:
                assert name not in self.columns, "Cannot add an existing column"
            assert declaration.startswith("LONGTEXT")
            self.columns[name] = {
                "name": name,
                "type": mysql.LONGTEXT(),
                "nullable": "NOT NULL" not in declaration,
            }
        elif sql.startswith("INSERT INTO schema_migrations"):
            self.migration_versions.append(parameters["v"])
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")


class _Inspector:
    def __init__(self, connection):
        self.connection = connection

    def get_table_names(self):
        return ["issue_analyses"]

    def has_table(self, name):
        return name == "issue_analyses"

    def get_columns(self, name):
        assert name == "issue_analyses"
        return list(self.connection.columns.values())


def _legacy_columns():
    return {
        name: {"name": name, "type": mysql.TEXT(), "nullable": True}
        for name in _COLUMNS
    }


def _use_schema_inspector(monkeypatch):
    monkeypatch.setattr("sqlalchemy.inspect", _Inspector)


@pytest.mark.asyncio
async def test_legacy_text_columns_expand_once_without_rewriting_nullability(
    monkeypatch,
):
    _use_schema_inspector(monkeypatch)
    columns = _legacy_columns()
    columns["analysis_detail"]["nullable"] = False
    conn = _SchemaConnection(columns)
    migrate = database._ensure_issue_analysis_longtext_columns
    assert await migrate(conn, logging.getLogger(__name__)) is True
    assert conn.statements == [
        "ALTER TABLE `issue_analyses` MODIFY COLUMN `issue_relations` LONGTEXT NULL",
        "ALTER TABLE `issue_analyses` MODIFY COLUMN `analysis_detail` LONGTEXT NOT NULL",
    ]
    assert await migrate(conn, logging.getLogger(__name__)) is False
    assert len(conn.statements) == 2
    assert columns["analysis_detail"]["nullable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("column_type", [mysql.TINYTEXT, mysql.MEDIUMTEXT])
async def test_smaller_text_variants_expand_without_truncating(
    monkeypatch, column_type
):
    _use_schema_inspector(monkeypatch)
    columns = _legacy_columns()
    columns["issue_relations"]["type"] = column_type()
    conn = _SchemaConnection(columns)
    assert (
        await database._ensure_issue_analysis_longtext_columns(
            conn, logging.getLogger(__name__)
        )
        is True
    )
    assert all(
        isinstance(column["type"], mysql.LONGTEXT) for column in columns.values()
    )


@pytest.mark.asyncio
async def test_upgrade_preserves_explicit_character_set_and_collation(monkeypatch):
    _use_schema_inspector(monkeypatch)
    columns = _legacy_columns()
    columns["issue_relations"]["type"] = mysql.TEXT(
        charset="utf8mb4", collation="utf8mb4_bin"
    )
    conn = _SchemaConnection(columns)
    await database._ensure_issue_analysis_longtext_columns(
        conn, logging.getLogger(__name__)
    )
    assert conn.statements[0] == (
        "ALTER TABLE `issue_analyses` MODIFY COLUMN `issue_relations` "
        "LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("DDL unavailable"), asyncio.CancelledError()]
)
async def test_partial_upgrade_failure_propagates_and_retry_only_expands_remaining_column(
    monkeypatch, failure
):
    _use_schema_inspector(monkeypatch)
    conn = _SchemaConnection(
        _legacy_columns(), fail_column="analysis_detail", failure=failure
    )
    with pytest.raises(type(failure)):
        await database._ensure_issue_analysis_longtext_columns(
            conn, logging.getLogger(__name__)
        )
    assert isinstance(conn.columns["issue_relations"]["type"], mysql.LONGTEXT)
    assert isinstance(conn.columns["analysis_detail"]["type"], mysql.TEXT)
    conn.fail_column = None
    conn.statements.clear()
    assert (
        await database._ensure_issue_analysis_longtext_columns(
            conn, logging.getLogger(__name__)
        )
        is True
    )
    assert conn.statements == [
        "ALTER TABLE `issue_analyses` MODIFY COLUMN `analysis_detail` LONGTEXT NULL"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()])
async def test_non_mysql_databases_do_not_receive_issue_longtext_alters(dialect):
    conn = _SchemaConnection(_legacy_columns())
    conn.dialect = dialect
    assert (
        await database._ensure_issue_analysis_longtext_columns(
            conn, logging.getLogger(__name__)
        )
        is False
    )
    assert conn.statements == []


def _configure_auto_migration(monkeypatch, conn):
    _use_schema_inspector(monkeypatch)

    class Engine:
        @asynccontextmanager
        async def begin(self):
            yield conn

    async def unchanged(*_args):
        return False

    monkeypatch.setattr(database, "async_engine", Engine())
    monkeypatch.setattr(database, "_ensure_model_modules_imported", lambda: None)
    monkeypatch.setattr(
        database.Base.metadata, "create_all", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        database.SchemaMigration.__table__, "create", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(database, "_drop_legacy_activity_tables", lambda _conn: ())
    for name in (
        "_ensure_agent_message_longtext_columns",
        "_ensure_activity_publication_marker_column",
        "_ensure_legacy_telegram_id_nullable",
        "_ensure_observability_trigger_unique_index",
        "_ensure_pr_issue_link_unique_index",
    ):
        monkeypatch.setattr(database, name, unchanged)


def _full_legacy_columns(missing=()):
    columns = {
        column.name: {
            "name": column.name,
            "type": column.type,
            "nullable": column.nullable,
        }
        for column in IssueAnalysis.__table__.columns
        if column.name not in missing
    }
    for name in _COLUMNS:
        if name in columns:
            columns[name]["type"] = mysql.TEXT()
    return columns


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing", [(), ("issue_relations",), ("analysis_detail",), _COLUMNS]
)
async def test_auto_migration_handles_missing_columns_and_tracks_only_actual_upgrades(
    monkeypatch, missing
):
    columns = _full_legacy_columns(missing)
    conn = _SchemaConnection(columns)
    _configure_auto_migration(monkeypatch, conn)
    await database._auto_migrate()
    assert len(conn.migration_versions) == 1
    assert all(isinstance(columns[name]["type"], mysql.LONGTEXT) for name in _COLUMNS)
    for name in missing:
        assert any(f"ADD COLUMN {name} LONGTEXT" in sql for sql in conn.statements)
        assert not any(f"MODIFY COLUMN `{name}`" in sql for sql in conn.statements)
    statements_after_upgrade = list(conn.statements)
    await database._auto_migrate()
    assert conn.statements == statements_after_upgrade
    assert len(conn.migration_versions) == 1


@pytest.mark.asyncio
async def test_failed_auto_migration_is_not_recorded_and_retry_completes_partial_upgrade(
    monkeypatch,
):
    conn = _SchemaConnection(_full_legacy_columns(), fail_column="analysis_detail")
    _configure_auto_migration(monkeypatch, conn)
    with pytest.raises(RuntimeError, match="DDL unavailable"):
        await database._auto_migrate()
    assert conn.migration_versions == []
    conn.fail_column = None
    conn.statements.clear()
    await database._auto_migrate()
    assert len(conn.migration_versions) == 1
    assert conn.statements[0] == (
        "ALTER TABLE `issue_analyses` MODIFY COLUMN `analysis_detail` LONGTEXT NULL"
    )
    assert len(conn.statements) == 2  # remaining ALTER, then the migration record


class _AsyncSessionFacade:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    async def commit(self):
        self.session.commit()

    async def refresh(self, record):
        self.session.refresh(record)


@pytest.mark.asyncio
@pytest.mark.parametrize("long_evidence", [True, False])
async def test_real_orm_save_retains_large_multibyte_evidence_and_complete_analysis(
    long_evidence,
):
    engine = create_engine("sqlite:///:memory:")
    IssueAnalysis.__table__.create(engine)
    try:
        with Session(engine) as session:
            record = IssueAnalysis(
                issue_number=7, repo_owner="owner", repo_name="repo", status="analyzing"
            )
            session.add(record)
            session.commit()
            quote = "证据🙂" * (8000 if long_evidence else 3000)
            facts = {
                "number": 42,
                "title": "Crash",
                "body": quote,
                "state_reason": None,
                "labels": ["bug"],
                "comments": [],
            }
            decision = {
                "number": 42,
                "relation": "duplicate",
                "confidence": 0.99,
                "reason": "Same grounded failure",
                "similarities": ["Same crash"],
                "differences": [],
                "evidence": [{"current_quote": quote, "candidate_quote": quote}],
            }
            accepted = parse_issue_relations(
                json.dumps({"relations": [decision]}, ensure_ascii=False),
                facts,
                [facts],
                "open",
                0.8,
                0.9,
            )
            assert len(accepted) == 1 and accepted[0]["number"] == 42
            relations = IssueRelationResult(
                primary=accepted[0], duplicate_of=42
            ).to_dict()
            analysis = {
                "summary": "完整主分析🙂" * 2000,
                "duplicate_of": 42,
                "issue_relations": relations,
            }
            relation_bytes = len(
                json.dumps(relations, ensure_ascii=False).encode("utf-8")
            )
            assert (relation_bytes > 65535) is long_evidence
            assert len(analysis["summary"].encode("utf-8")) < 65535
            assert len(json.dumps(analysis, ensure_ascii=False).encode("utf-8")) > 65535
            result = await IssueService().save_analysis_result(
                analysis,
                {"issue_number": 7, "repo_owner": "owner", "repo_name": "repo"},
                _AsyncSessionFacade(session),
                analysis_id=record.id,
            )
            session.expire_all()
            persisted = session.get(IssueAnalysis, record.id)
            assert result is not None and persisted.status == "completed"
            assert persisted.duplicate_of == 42
            assert persisted.summary == analysis["summary"]
            assert json.loads(persisted.issue_relations) == relations
            assert json.loads(persisted.analysis_detail) == analysis
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "issue_state", "owner"),
    [
        ("cancelled", "open", "owner"),
        ("analyzing", "closed", "owner"),
        ("analyzing", "open", "other"),
    ],
)
async def test_large_result_keeps_closed_cancelled_and_owner_guards(
    status, issue_state, owner
):
    engine = create_engine("sqlite:///:memory:")
    IssueAnalysis.__table__.create(engine)
    try:
        with Session(engine) as session:
            record = IssueAnalysis(
                issue_number=7,
                repo_owner="owner",
                repo_name="repo",
                status=status,
                issue_state=issue_state,
                duplicate_of=41,
                issue_relations='{"status":"old"}',
                analysis_detail='{"summary":"old"}',
            )
            session.add(record)
            session.commit()
            result = await IssueService().save_analysis_result(
                {"summary": "长证据🙂" * 10000, "duplicate_of": 42},
                {"issue_number": 7, "repo_owner": owner, "repo_name": "repo"},
                _AsyncSessionFacade(session),
                analysis_id=record.id,
            )
            session.expire_all()
            assert result is None
            assert record.status == status and record.issue_state == issue_state
            assert record.duplicate_of == 41
            assert record.issue_relations == '{"status":"old"}'
            assert record.analysis_detail == '{"summary":"old"}'
    finally:
        engine.dispose()


def test_additive_sqlite_upgrade_preserves_existing_json_and_duplicate():
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE issue_analyses (id INTEGER PRIMARY KEY, duplicate_of BIGINT, analysis_detail TEXT)"
                )
            )
            conn.execute(
                text('INSERT INTO issue_analyses VALUES (1, 42, \'{"summary":"old"}\')')
            )
            conn.execute(
                text(
                    _build_add_column_sql(
                        conn.dialect,
                        "issue_analyses",
                        IssueAnalysis.__table__.c.issue_relations,
                    )
                )
            )
            assert conn.execute(
                text(
                    "SELECT duplicate_of, analysis_detail, issue_relations FROM issue_analyses"
                )
            ).one() == (42, '{"summary":"old"}', None)
            assert inspect(conn).get_columns("issue_analyses")[-1]["nullable"] is True
    finally:
        engine.dispose()
