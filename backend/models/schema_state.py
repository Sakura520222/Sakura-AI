"""Durable Setup checkpoints, bound to both migration code and physical schema."""

import hashlib
import json
from pathlib import Path

import sqlalchemy
from sqlalchemy import bindparam, inspect, select, text


def _migration_revision():
    root = Path(__file__).resolve().parents[2]
    paths = sorted((root / "backend/models").glob("*.py"))
    paths.extend(
        root / relative
        for relative in (
            "backend/services/identity_service.py",
            "backend/services/notification_migration.py",
        )
    )
    digest = hashlib.sha256()
    digest.update(sqlalchemy.__version__.encode())
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_text(encoding="utf-8-sig").encode())
    return digest.hexdigest()


def _checkpoint_key(connection):
    from backend.models.database import Base, _ensure_model_modules_imported

    _ensure_model_modules_imported()
    expected = set(Base.metadata.tables)
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    if not expected <= existing:
        return None
    dialect = connection.dialect.name
    if dialect in {"mysql", "mariadb"}:
        queries = (
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
            "EXTRA, CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN :tables",
            "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, "
            "SUB_PART, COLLATION, INDEX_TYPE FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN :tables",
        )
    elif dialect == "postgresql":
        queries = (
            "SELECT table_name, column_name, udt_name, is_nullable, column_default, "
            "character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name IN :tables",
            "SELECT tablename, indexname, indexdef FROM pg_catalog.pg_indexes "
            "WHERE schemaname = current_schema() AND tablename IN :tables",
        )
    elif dialect == "sqlite":
        if any(
            not set(table.columns.keys())
            <= {column["name"] for column in inspector.get_columns(name)}
            for name, table in Base.metadata.tables.items()
        ):
            return None
        queries = (
            "SELECT tbl_name, type, name, sql FROM sqlite_master "
            "WHERE tbl_name IN :tables AND type IN ('table', 'index')",
        )
    else:
        return None
    schema = []
    for query in queries:
        rows = connection.execute(
            text(query).bindparams(bindparam("tables", expanding=True)),
            {"tables": sorted(expected)},
        )
        schema.append(sorted([tuple(row) for row in rows], key=repr))
    if dialect in {"mysql", "mariadb", "postgresql"}:
        present = {}
        for row in schema[0]:
            present.setdefault(row[0], set()).add(row[1])
        if any(
            not set(table.columns.keys()) <= present.get(name, set())
            for name, table in Base.metadata.tables.items()
        ):
            return None
    payload = json.dumps(
        [_migration_revision(), dialect, schema], default=str, ensure_ascii=True
    )
    # Fits the existing unique schema_migrations.version VARCHAR(50).
    return "setup:" + hashlib.sha256(payload.encode()).hexdigest()[:40]


def _has_checkpoint(connection):
    from backend.models.database import SchemaMigration

    key = _checkpoint_key(connection)
    if key is None:
        return False
    return (
        connection.execute(
            select(SchemaMigration.id).where(SchemaMigration.version == key).limit(1)
        ).first()
        is not None
    )


async def schema_is_current(engine):
    async with engine.connect() as connection:
        return await connection.run_sync(_has_checkpoint)


def _record_checkpoint(connection):
    from backend.models.database import SchemaMigration

    key = _checkpoint_key(connection)
    if key is None:
        raise RuntimeError("Cannot checkpoint an incomplete database schema")
    table = SchemaMigration.__table__
    dialect = connection.dialect.name
    if dialect in {"mysql", "mariadb"}:
        from sqlalchemy.dialects.mysql import insert

        statement = insert(table).values(version=key)
        statement = statement.on_duplicate_key_update(
            version=statement.inserted.version
        )
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        statement = (
            insert(table)
            .values(version=key)
            .on_conflict_do_nothing(index_elements=["version"])
        )
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        statement = (
            insert(table)
            .values(version=key)
            .on_conflict_do_nothing(index_elements=["version"])
        )
    else:
        raise RuntimeError("Unsupported database checkpoint dialect")
    connection.execute(statement)


async def record_schema_checkpoint(engine):
    async with engine.begin() as connection:
        await connection.run_sync(_record_checkpoint)
