"""MySQL binlog permissions must fail closed without privilege changes."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy
from sqlalchemy.exc import OperationalError

from backend.models import billing_schema


class Result:
    def __init__(self, row):
        self.row = row

    def scalar_one(self):
        return int(self.row is not None)

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class MySQLConnection:
    dialect = SimpleNamespace(name="mysql")

    def __init__(self, installed=None, error_code=1419):
        self.installed = installed or {}
        self.error_code = error_code
        self.statements = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "information_schema.TRIGGERS" in sql:
            return Result(self.installed.get(params["name"]))
        if sql.startswith("CREATE TRIGGER"):
            raise OperationalError(
                sql,
                {},
                Exception(self.error_code, "private driver detail must not leak"),
            )
        raise AssertionError(f"Unexpected schema statement: {sql}")


def guards():
    return {
        f"billing_transactions_no_{action.lower()}": {
            "EVENT_OBJECT_TABLE": "billing_transactions",
            "EVENT_MANIPULATION": action,
            "ACTION_TIMING": "BEFORE",
            "ACTION_ORIENTATION": "ROW",
            "ACTION_STATEMENT": "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Billing ledger is append-only'",
        }
        for action in ("UPDATE", "DELETE")
    }


@pytest.fixture(autouse=True)
def only_ledger_table(monkeypatch):
    monkeypatch.setattr(
        sqlalchemy,
        "inspect",
        lambda connection: SimpleNamespace(
            has_table=lambda name: name == "billing_transactions"
        ),
    )


@pytest.mark.parametrize("error_code", [1419, 1142, 1227])
def test_missing_mysql_guard_permission_has_actionable_fail_closed_error(error_code):
    connection = MySQLConnection(error_code=error_code)
    with pytest.raises(
        RuntimeError, match="billing_schema_privilege_required"
    ) as failed:
        billing_schema.ensure_billing_schema(connection)
    message = str(failed.value)
    assert "docs/billing-mysql-guards.sql" in message
    assert "private driver detail" not in message
    assert not any(
        "SET GLOBAL" in sql or "GRANT " in sql for sql in connection.statements
    )


def test_valid_preinstalled_guards_do_not_need_super_or_create():
    connection = MySQLConnection(guards())
    assert billing_schema.ensure_billing_schema(connection) is False
    assert all("information_schema.TRIGGERS" in sql for sql in connection.statements)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("EVENT_OBJECT_TABLE", "another_table"),
        ("EVENT_MANIPULATION", "INSERT"),
        ("ACTION_TIMING", "AFTER"),
        ("ACTION_ORIENTATION", "STATEMENT"),
        ("ACTION_STATEMENT", "SET @ledger_mutation_allowed = 1"),
    ],
)
def test_same_name_cannot_hide_missing_immutable_guard(field, value):
    installed = guards()
    installed["billing_transactions_no_update"][field] = value
    connection = MySQLConnection(installed)
    with pytest.raises(RuntimeError, match="Conflicting billing immutable trigger"):
        billing_schema.ensure_billing_schema(connection)
    assert not any("CREATE TRIGGER" in sql for sql in connection.statements)


def test_connection_errors_are_not_misreported_as_trigger_permissions():
    connection = MySQLConnection(error_code=2013)
    with pytest.raises(OperationalError) as failed:
        billing_schema.ensure_billing_schema(connection)
    assert failed.value.orig.args[0] == 2013


def test_mysql_guard_export_is_offline_fixed_and_does_not_elevate(tmp_path):
    script = (
        Path(__file__).resolve().parents[1] / "scripts/export_billing_mysql_guards.py"
    )
    target = tmp_path / "guards.sql"
    run = subprocess.run(
        [sys.executable, str(script), "--output", str(target)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    sql = target.read_text()
    assert sql.count("CREATE TRIGGER IF NOT EXISTS") == 24
    assert sql.count("SIGNAL SQLSTATE '45000'") == 24
    assert "DROP " not in sql
    assert "GRANT " not in sql
    assert "SET GLOBAL" not in sql
    assert "DEFINER = 'root'" not in sql
    assert "connect" not in run.stdout.lower()
