"""Idempotent Billing 2.0 indexes and database-enforced immutable ledger rows."""

import re

from sqlalchemy import VARCHAR, Column, Index, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateColumn

IMMUTABLE_TABLES = (
    "billing_transactions",
    "billing_price_profiles",
    "billing_usage_charges",
    "billing_reservation_events",
    "billing_legacy_entitlement_events",
    "payment_refund_attempt_events",
    "billing_reconciliation_events",
    "billing_rate_limit_admissions",
    "payment_code_snapshot_audits",
    "payment_receipts",
    "payment_refund_inbox_audits",
    "payment_refund_references",
)


class BillingSchemaPermissionError(RuntimeError):
    """A DBA must install required guards before this deployment can start."""

    code = "billing_schema_privilege_required"

    def __init__(self, table_name, error_code):
        self.database_error_code = error_code
        super().__init__(
            f"{self.code}: MySQL rejected the immutable guard for {table_name} "
            f"(error {error_code}). Billing startup remains blocked. "
            "Ask a database administrator to install docs/billing-mysql-guards.sql "
            "in this application database, then restart. The application needs "
            "table TRIGGER privileges to verify the installed guards. "
            "See docs/billing-2.md for binlog permissions and the persistent DEFINER."
        )


def mysql_guard_ddl(table_name: str, action: str, *, if_not_exists=False) -> str:
    """Fixed, auditable DDL; never accepts a caller-selected arbitrary table."""
    if table_name not in IMMUTABLE_TABLES or action not in {"UPDATE", "DELETE"}:
        raise ValueError("Unknown billing immutable guard")
    name = f"{table_name}_no_{action.lower()}"
    clause = "IF NOT EXISTS " if if_not_exists else ""
    return (
        f"CREATE TRIGGER {clause}{name} BEFORE {action} ON {table_name} "
        "FOR EACH ROW SIGNAL SQLSTATE '45000' "
        "SET MESSAGE_TEXT = 'Billing ledger is append-only'"
    )


def _validate_mysql_guard(metadata, table_name, action):
    fields = {
        "EVENT_OBJECT_TABLE": table_name,
        "EVENT_MANIPULATION": action,
        "ACTION_TIMING": "BEFORE",
        "ACTION_ORIENTATION": "ROW",
    }
    statement = str(metadata.get("ACTION_STATEMENT") or "").strip()
    rejection = re.fullmatch(
        r"(?:BEGIN\s+)?SIGNAL\s+SQLSTATE\s+(?:VALUE\s+)?'45000'\s+"
        r"SET\s+MESSAGE_TEXT\s*=\s*'Billing ledger is append-only'"
        r"(?:\s*;\s*END)?\s*;?",
        statement,
        flags=re.IGNORECASE,
    )
    if (
        any(metadata.get(key) != value for key, value in fields.items())
        or not rejection
    ):
        raise RuntimeError(
            f"Conflicting billing immutable trigger: {table_name}_no_{action.lower()}"
        )


def _ensure_mysql_guard(connection, table_name, action):
    name = f"{table_name}_no_{action.lower()}"
    installed = (
        connection.execute(
            text(
                "SELECT EVENT_OBJECT_TABLE, EVENT_MANIPULATION, ACTION_TIMING, "
                "ACTION_ORIENTATION, ACTION_STATEMENT FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA = DATABASE() AND TRIGGER_NAME = :name"
            ),
            {"name": name},
        )
        .mappings()
        .one_or_none()
    )
    if installed is not None:
        _validate_mysql_guard(installed, table_name, action)
        return False
    try:
        connection.execute(text(mysql_guard_ddl(table_name, action)))
    except DBAPIError as exc:
        args = getattr(exc.orig, "args", ())
        code = args[0] if args else None
        if code in {1419, 1142, 1227}:
            # Never retry with elevated credentials or relax server-wide policy.
            # Keep startup closed and omit potentially sensitive driver details.
            raise BillingSchemaPermissionError(table_name, code) from None
        raise
    return True


def _create_index(connection, table, name, columns, unique):
    existing = next((index for index in table.indexes if index.name == name), None)
    if existing is None:
        existing = Index(name, *(table.c[column] for column in columns), unique=unique)
    elif (
        existing.unique != unique
        or tuple(column.name for column in existing.columns) != columns
    ):
        raise RuntimeError(f"Conflicting billing metadata index: {name}")
    existing.create(connection)


def _ensure_usage_charge_currency_width(connection, inspector):
    """Widen old native VARCHAR columns without changing any financial rows.

    SQLite already accepts supported currency codes regardless of VARCHAR's
    declared width. Never rebuild its immutable table or drop any ledger guard.
    """
    dialect = connection.dialect
    table_name = "billing_usage_charges"
    if dialect.name not in {
        "mysql",
        "mariadb",
        "postgresql",
    } or not inspector.has_table(table_name):
        return False
    columns = {column["name"]: column for column in inspector.get_columns(table_name)}
    statements = []
    identifiers = dialect.identifier_preparer
    quoted_table = identifiers.quote(table_name)
    for name in ("provider_currency", "settlement_currency"):
        column = columns.get(name)
        if column is None:
            # Startup's general additive migration supplies missing columns.
            continue
        current_type = column["type"]
        if not isinstance(current_type, VARCHAR) or any(
            column.get(key) for key in ("computed", "identity")
        ):
            raise RuntimeError(
                f"Conflicting billing currency column: {table_name}.{name}"
            )
        length = current_type.length
        if length is None or length >= 10:
            continue
        expanded_type = current_type.copy()
        expanded_type.length = 10
        if dialect.name == "postgresql":
            # TYPE alone preserves the default, nullability, comment and guards.
            statements.append(
                f"ALTER TABLE {quoted_table} ALTER COLUMN {identifiers.quote(name)} "
                f"TYPE {expanded_type.compile(dialect=dialect)}"
            )
        else:
            # MySQL MODIFY replaces a full definition. Compile the reflected
            # definition so charset/collation, default, NULL and comment survive.
            reflected = Column(
                name,
                expanded_type,
                nullable=column["nullable"],
                server_default=(
                    text(column["default"])
                    if column.get("default") is not None
                    else None
                ),
                comment=column.get("comment"),
            )
            definition = str(CreateColumn(reflected).compile(dialect=dialect))
            statements.append(f"ALTER TABLE {quoted_table} MODIFY COLUMN {definition}")
    # Inspect every targeted definition before applying any of the width changes.
    for statement in statements:
        connection.execute(text(statement))
    return bool(statements)


def ensure_billing_schema(connection):
    from sqlalchemy import inspect

    inspector = inspect(connection)
    changed = _ensure_usage_charge_currency_width(connection, inspector)
    if inspector.has_table("billing_price_profiles"):
        from backend.models.billing_models import BillingPriceProfile

        table = BillingPriceProfile.__table__
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        # Normal startup first adds the new identity columns with the provider
        # default. Do not infer or backfill accounts into immutable old prices.
        if {"scope_key", "account_id"} <= columns:
            name = "uq_billing_price_scope_version"
            identity = ("scope_key", "provider_id", "model_id", "call_kind", "version")
            indexes = {
                index["name"]: index for index in inspector.get_indexes(table.name)
            }
            if name in indexes:
                index = indexes[name]
                if not index.get("unique") or tuple(index["column_names"]) != identity:
                    raise RuntimeError(f"Conflicting billing database index: {name}")
            else:
                _create_index(connection, table, name, identity, True)
                changed = True
    if inspector.has_table("agent_team_tasks"):
        from backend.models.agent_team_models import AgentTeamTask

        unique_columns = {
            tuple(item["column_names"])
            for item in (
                inspector.get_unique_constraints("agent_team_tasks")
                + inspector.get_indexes("agent_team_tasks")
            )
            if item.get("unique", True)
        }
        if (
            "webhook_delivery_id" in AgentTeamTask.__table__.c
            and ("webhook_delivery_id",) not in unique_columns
        ):
            _create_index(
                connection,
                AgentTeamTask.__table__,
                "uq_agent_verified_delivery",
                ("webhook_delivery_id",),
                True,
            )
            changed = True
    if inspector.has_table("payment_orders"):
        from backend.models.payment_models import Order

        table = Order.__table__
        unique_columns = {
            tuple(item["column_names"])
            for item in (
                inspector.get_unique_constraints(table.name)
                + inspector.get_indexes(table.name)
            )
            if item.get("unique", True)
        }
        for column, name in (
            ("grant_idempotency_key", "uq_payment_grant_event"),
            ("invoice_identity", "uq_payment_invoice_identity"),
        ):
            if column not in table.c or (column,) in unique_columns:
                continue
            _create_index(connection, table, name, (column,), True)
            changed = True
    if inspector.has_table("ai_usage_records"):
        from backend.models.ai_usage_models import AIUsageRecord

        table = AIUsageRecord.__table__
        indexes = {index["name"]: index for index in inspector.get_indexes(table.name)}
        unique_columns = {
            tuple(item["column_names"])
            for item in (
                inspector.get_unique_constraints(table.name)
                + inspector.get_indexes(table.name)
            )
            if item.get("unique", True)
        }
        for name, columns, unique in (
            ("uq_ai_usage_actual_request", ("actual_call_id",), True),
            (
                "ix_ai_usage_owner_operation",
                ("user_id", "operation_id", "feature"),
                False,
            ),
        ):
            if name in indexes:
                index = indexes[name]
                if (
                    bool(index.get("unique")) != unique
                    or tuple(index["column_names"]) != columns
                ):
                    raise RuntimeError(f"Conflicting billing database index: {name}")
                continue
            if unique and columns in unique_columns:
                continue
            _create_index(connection, table, name, columns, unique)
            changed = True
    dialect = connection.dialect.name
    for table_name in IMMUTABLE_TABLES:
        if not inspector.has_table(table_name):
            continue
        for action in ("UPDATE", "DELETE"):
            name = f"{table_name}_no_{action.lower()}"
            if dialect == "sqlite":
                connection.execute(
                    text(
                        f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} ON {table_name} "
                        "BEGIN SELECT RAISE(ABORT, 'Billing ledger is append-only'); END"
                    )
                )
            elif dialect in {"mysql", "mariadb"}:
                changed = _ensure_mysql_guard(connection, table_name, action) or changed
            elif dialect == "postgresql":
                connection.execute(
                    text(
                        "CREATE OR REPLACE FUNCTION sakura_billing_immutable() RETURNS trigger "
                        "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Billing ledger is append-only'; END $$"
                    )
                )
                exists = connection.execute(
                    text(
                        "SELECT COUNT(*) FROM pg_trigger WHERE tgname = :name "
                        "AND tgrelid = CAST(:table_name AS regclass) AND NOT tgisinternal"
                    ),
                    {"name": name, "table_name": table_name},
                ).scalar_one()
                if not exists:
                    connection.execute(
                        text(
                            f"CREATE TRIGGER {name} BEFORE {action} ON {table_name} "
                            "FOR EACH ROW EXECUTE FUNCTION sakura_billing_immutable()"
                        )
                    )
                    changed = True
            else:
                raise RuntimeError(
                    f"Billing immutable ledger does not support dialect: {dialect}"
                )
    return changed
