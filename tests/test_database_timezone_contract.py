from __future__ import annotations

from pathlib import Path

from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

from backend.models import Base
from backend.models.database import _set_connection_timezone
from backend.models.time_types import UTCDateTime


def test_time_type_is_available_to_models():
    assert UTCDateTime().python_type.__name__ == "datetime"


def test_models_do_not_declare_naive_utcnow_defaults():
    source = "\n".join(
        p.read_text(encoding="utf-8")
        for p in Path("backend/models").glob("*.py")
        if p.name != "time_types.py"
    )
    assert "datetime.utcnow" not in source
    assert "default=datetime.utcnow" not in source
    assert "onupdate=datetime.utcnow" not in source


class _Cursor:
    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)

    def close(self):
        pass


class _Connection:
    def __init__(self):
        self.cursor_instance = _Cursor()

    def cursor(self):
        return self.cursor_instance


def test_database_sessions_are_pinned_to_utc():
    mysql_connection = _Connection()
    _set_connection_timezone(mysql_connection, None, "mysql")
    assert mysql_connection.cursor_instance.statements == ["SET time_zone = '+00:00'"]

    postgres_connection = _Connection()
    _set_connection_timezone(postgres_connection, None, "postgresql")
    assert postgres_connection.cursor_instance.statements == ["SET TIME ZONE 'UTC'"]

    sqlite_connection = _Connection()
    _set_connection_timezone(sqlite_connection, None, "sqlite")
    assert sqlite_connection.cursor_instance.statements == []


def test_all_model_metadata_compiles_for_empty_sqlite_schema():
    assert len(Base.metadata.tables) >= 60
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
        assert "CURRENT_UTCDateTime" not in ddl
        assert "None" not in ddl
        for column in table.columns:
            assert column.type is not None
