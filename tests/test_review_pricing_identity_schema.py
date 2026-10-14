"""Exact identity DDL and additive native upgrades preserve financial definitions."""

from types import SimpleNamespace

import pytest
from sqlalchemy import Integer
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from backend.models.billing_models import BillingPriceProfile
from backend.models.billing_schema import _ensure_price_identity_collation
from backend.models.billing_types import price_identity_collation

NAMES = ("provider_id", "account_id", "scope_key", "model_id", "call_kind")
LENGTHS = (128, 128, 136, 255, 32)


@pytest.mark.parametrize("maria", [False, True])
def test_mysql_identity_types_preserve_exact_case_and_trailing_spaces(maria):
    dialect = mysql.dialect()
    dialect.is_mariadb = maria
    ddl = str(CreateTable(BillingPriceProfile.__table__).compile(dialect=dialect))
    expected = "utf8mb4_nopad_bin" if maria else "utf8mb4_0900_bin"
    assert ddl.count("COLLATE " + expected) == len(NAMES)
    assert "scope_key VARCHAR(136) CHARACTER SET utf8mb4 COLLATE " + expected in ddl


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_other_dialects_keep_existing_string_identity_contracts(dialect):
    ddl = str(CreateTable(BillingPriceProfile.__table__).compile(dialect=dialect))
    assert "utf8mb4" not in ddl and "provider_id VARCHAR(128)" in ddl


class NativePriceSchema:
    def __init__(self, *, current="utf8mb4_unicode_ci"):
        self.dialect = mysql.dialect()
        self.statements = []
        self.collations = {name: current for name in NAMES}
        self.columns = [
            {
                "name": name,
                "type": mysql.VARCHAR(length, charset="utf8mb4"),
                "nullable": name == "account_id",
                "default": "'provider'" if name == "scope_key" else None,
                "comment": "original scope" if name == "scope_key" else None,
            }
            for name, length in zip(NAMES, LENGTHS, strict=True)
        ]
        self.inspector = SimpleNamespace(
            has_table=lambda name: name == "billing_price_profiles",
            get_columns=lambda name: self.columns,
        )

    def execute(self, sql, params=None):
        self.statements.append(str(sql))
        if "information_schema.COLUMNS" in str(sql):
            return SimpleNamespace(all=lambda: list(self.collations.items()))


def test_upgrade_changes_only_collation_and_preserves_defaults_nullability_and_comments():
    schema = NativePriceSchema()
    assert _ensure_price_identity_collation(schema, schema.inspector)
    alters = schema.statements[1:]
    assert len(alters) == 5
    assert all(
        "ALTER TABLE billing_price_profiles MODIFY COLUMN" in sql for sql in alters
    )
    assert all("COLLATE utf8mb4_0900_bin" in sql for sql in alters)
    scope = next(sql for sql in alters if "scope_key" in sql)
    assert (
        "DEFAULT 'provider'" in scope
        and "NOT NULL" in scope
        and "COMMENT 'original scope'" in scope
    )
    account = next(sql for sql in alters if "account_id" in sql)
    assert "NOT NULL" not in account
    assert not any("DROP" in sql or "UPDATE" in sql for sql in alters)


def test_upgrade_reads_effective_inherited_collation_and_is_idempotent():
    schema = NativePriceSchema(current="utf8mb4_0900_bin")
    assert not _ensure_price_identity_collation(schema, schema.inspector)
    assert len(schema.statements) == 1


def test_upgrade_validates_every_column_before_mutating_any_definition():
    schema = NativePriceSchema()
    schema.columns[-1]["type"] = Integer()
    with pytest.raises(RuntimeError, match="Conflicting pricing identity"):
        _ensure_price_identity_collation(schema, schema.inspector)
    assert len(schema.statements) == 1


def test_mariadb_compatibility_driver_uses_its_supported_no_pad_collation():
    assert (
        price_identity_collation(SimpleNamespace(name="mysql", is_mariadb=True))
        == "utf8mb4_nopad_bin"
    )
