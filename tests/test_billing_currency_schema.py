"""Currency widths upgrade additively without altering immutable quotes."""

from types import SimpleNamespace

import pytest
import sqlalchemy
from sqlalchemy import (
    VARCHAR,
    Column,
    Integer,
    MetaData,
    Table,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.exc import DatabaseError

from backend.models import billing_schema
from backend.models.billing_models import BillingUsageCharge


def test_fresh_usage_charge_currency_columns_hold_supported_assets():
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table("billing_price_profiles", metadata, Column("id", Integer, primary_key=True))
    table = BillingUsageCharge.__table__.to_metadata(metadata)
    metadata.create_all(engine)
    with engine.begin() as connection:
        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns(table.name)
        }
        assert columns["provider_currency"]["type"].length == 10
        assert columns["settlement_currency"]["type"].length == 10
        # SQLite does not need a table rebuild, which would remove its guards.
        assert not billing_schema._ensure_usage_charge_currency_width(
            connection, inspect(connection)
        )
    engine.dispose()


class NativeConnection:
    def __init__(self, dialect):
        self.dialect = dialect
        self.statements = []
        self.columns = [
            {
                "name": "provider_currency",
                "type": VARCHAR(3),
                "nullable": False,
                "default": "'USD'",
                "comment": "original provider currency",
            },
            {
                "name": "settlement_currency",
                "type": VARCHAR(3),
                "nullable": True,
                "default": None,
                "comment": None,
            },
        ]
        if dialect.name in {"mysql", "mariadb"}:
            for column in self.columns:
                column["type"] = mysql.VARCHAR(
                    3, charset="ascii", collation="ascii_bin"
                )
        self.inspector = SimpleNamespace(
            has_table=lambda name: name == "billing_usage_charges",
            get_columns=lambda name: self.columns,
        )

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "information_schema.TRIGGERS" in sql:
            action = params["name"].rsplit("_", 1)[-1].upper()
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(
                    one_or_none=lambda: {
                        "EVENT_OBJECT_TABLE": "billing_usage_charges",
                        "EVENT_MANIPULATION": action,
                        "ACTION_TIMING": "BEFORE",
                        "ACTION_ORIENTATION": "ROW",
                        "ACTION_STATEMENT": "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only'",
                    }
                )
            )
        assert sql.startswith("ALTER TABLE")
        column_name = (
            "provider_currency" if "provider_currency" in sql else "settlement_currency"
        )
        next(column for column in self.columns if column["name"] == column_name)[
            "type"
        ].length = 10


@pytest.mark.parametrize("dialect", [mysql.dialect(), postgresql.dialect()])
def test_native_currency_width_upgrade_compiles_preserves_metadata_and_is_repeatable(
    dialect,
):
    connection = NativeConnection(dialect)
    assert billing_schema._ensure_usage_charge_currency_width(
        connection, connection.inspector
    )
    assert len(connection.statements) == 2
    assert not billing_schema._ensure_usage_charge_currency_width(
        connection, connection.inspector
    )
    assert len(connection.statements) == 2
    assert all(
        sql.startswith("ALTER TABLE") and "VARCHAR(10)" in sql
        for sql in connection.statements
    )
    assert all(
        "DROP " not in sql and "UPDATE " not in sql and "DELETE " not in sql
        for sql in connection.statements
    )
    if dialect.name == "mysql":
        sql = connection.statements[0]
        assert (
            "MODIFY COLUMN provider_currency VARCHAR(10) CHARACTER SET ascii COLLATE ascii_bin NOT NULL"
            in sql
        )
        assert "DEFAULT 'USD'" in sql and "COMMENT 'original provider currency'" in sql
        assert (
            "settlement_currency VARCHAR(10) CHARACTER SET ascii COLLATE ascii_bin"
            in connection.statements[1]
        )
        assert "NOT NULL" not in connection.statements[1]
    else:
        assert (
            "ALTER COLUMN provider_currency TYPE VARCHAR(10)"
            in connection.statements[0]
        )
        assert (
            "ALTER COLUMN settlement_currency TYPE VARCHAR(10)"
            in connection.statements[1]
        )
        assert (
            "DEFAULT" not in connection.statements[0]
            and "NOT NULL" not in connection.statements[0]
        )


@pytest.mark.parametrize("dialect", [mysql.dialect(), postgresql.dialect()])
@pytest.mark.parametrize("conflict_column", [0, 1])
def test_native_currency_width_rejects_unexpected_column_definition_before_ddl(
    dialect, conflict_column
):
    connection = NativeConnection(dialect)
    connection.columns[conflict_column]["type"] = Integer()
    with pytest.raises(RuntimeError, match="Conflicting billing currency column"):
        billing_schema._ensure_usage_charge_currency_width(
            connection, connection.inspector
        )
    assert connection.statements == []


def test_startup_applies_currency_width_upgrade_without_replacing_native_guards(
    monkeypatch,
):
    connection = NativeConnection(mysql.dialect())
    monkeypatch.setattr(sqlalchemy, "inspect", lambda value: connection.inspector)
    assert billing_schema.ensure_billing_schema(connection)
    assert not billing_schema.ensure_billing_schema(connection)
    assert (
        len([sql for sql in connection.statements if sql.startswith("ALTER TABLE")])
        == 2
    )
    assert (
        len(
            [
                sql
                for sql in connection.statements
                if "information_schema.TRIGGERS" in sql
            ]
        )
        == 4
    )
    assert not any(
        "CREATE TRIGGER" in sql or "DROP " in sql for sql in connection.statements
    )


def test_legacy_sqlite_currency_columns_keep_existing_data_and_immutable_guards():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE billing_usage_charges (id INTEGER PRIMARY KEY, provider_currency VARCHAR(3) NOT NULL, settlement_currency VARCHAR(3) NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO billing_usage_charges VALUES (1, 'USD', 'CNY')")
        )
        billing_schema.ensure_billing_schema(connection)
        before = connection.execute(
            text(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
            )
        ).all()
        assert not billing_schema.ensure_billing_schema(connection)
        after = connection.execute(
            text(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
            )
        ).all()
        assert before == after and len(before) == 2
        assert connection.execute(
            text(
                "SELECT provider_currency, settlement_currency FROM billing_usage_charges"
            )
        ).one() == ("USD", "CNY")
        # SQLite never enforces the VARCHAR display length; four characters fit.
        connection.execute(
            text("INSERT INTO billing_usage_charges VALUES (2, 'USDT', 'USDT')")
        )
        with pytest.raises(DatabaseError, match="append-only"):
            connection.execute(
                text(
                    "UPDATE billing_usage_charges SET provider_currency = 'USD' WHERE id = 2"
                )
            )
    engine.dispose()
