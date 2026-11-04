"""Idempotent Billing 2.0 schema upgrades: indexes, constraints, widths, collations.

Historical note: Billing 2.0 originally enforced append-only ledger rows with
database triggers (and a Setup preflight requiring TRIGGER privileges). That
Billing Guard mechanism was removed entirely (issue #715); this module now
performs only ordinary, additive schema upgrades.
"""

from sqlalchemy import VARCHAR, Column, Index, text
from sqlalchemy.schema import CreateColumn


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
    declared width. Never rebuild the table or rewrite existing rows.
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
            # TYPE alone preserves the default, nullability and comment.
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


def _ensure_price_identity_collation(connection, inspector):
    """Change comparison semantics without rewriting historical financial facts."""
    from backend.models.billing_types import price_identity_collation

    desired = price_identity_collation(connection.dialect)
    table_name = "billing_price_profiles"
    if desired is None or not inspector.has_table(table_name):
        return False
    columns = {item["name"]: item for item in inspector.get_columns(table_name)}
    collations = dict(
        connection.execute(
            text(
                "SELECT COLUMN_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name"
            ),
            {"table_name": table_name},
        ).all()
    )
    identifiers = connection.dialect.identifier_preparer
    statements = []
    for name in ("provider_id", "account_id", "scope_key", "model_id", "call_kind"):
        column = columns.get(name)
        if column is None:
            continue  # The startup additive migrator supplies missing columns.
        if not isinstance(column["type"], VARCHAR) or any(
            column.get(key) for key in ("computed", "identity")
        ):
            raise RuntimeError(
                f"Conflicting pricing identity column: {table_name}.{name}"
            )
        if collations.get(name) == desired:
            continue
        exact_type = column["type"].copy()
        exact_type.charset = "utf8mb4"
        exact_type.collation = desired
        reflected = Column(
            name,
            exact_type,
            nullable=column["nullable"],
            server_default=text(column["default"])
            if column.get("default") is not None
            else None,
            comment=column.get("comment"),
        )
        definition = str(CreateColumn(reflected).compile(dialect=connection.dialect))
        statements.append(
            f"ALTER TABLE {identifiers.quote(table_name)} MODIFY COLUMN {definition}"
        )
    # Validate every definition before applying DDL; keep index identities.
    for statement in statements:
        connection.execute(text(statement))
    return bool(statements)


def ensure_billing_schema(connection):
    from sqlalchemy import inspect

    inspector = inspect(connection)
    changed = _ensure_usage_charge_currency_width(connection, inspector)
    changed = _ensure_price_identity_collation(connection, inspector) or changed
    if inspector.has_table("billing_price_profiles"):
        from backend.models.billing_models import BillingPriceProfile

        table = BillingPriceProfile.__table__
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        # Normal startup first adds the new identity columns with the provider
        # default. Do not infer or backfill accounts into historical prices.
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
    return changed
