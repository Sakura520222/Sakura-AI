"""Idempotent delivery schema upgrade preserving rows, archives and read markers."""
from sqlalchemy import MetaData, text
from sqlalchemy.schema import AddConstraint, CreateTable

from backend.models.announcement_models import NotificationDelivery


def upgrade_notification_schema(conn):
    from sqlalchemy import inspect

    inspector = inspect(conn)
    table_name = "notification_deliveries"
    changed = False
    for name in ("agent_team_tasks", "repo_scans"):
        if inspector.has_table(name):
            task_columns = {item["name"] for item in inspector.get_columns(name)}
            if "execution_round" not in task_columns:
                conn.execute(text(f"ALTER TABLE {name} ADD COLUMN execution_round INTEGER NOT NULL DEFAULT 0"))
                changed = True
    if conn.dialect.name in {"mysql", "mariadb"} and inspector.has_table("notification_provider_budgets"):
        deadline = next(item for item in inspector.get_columns("notification_provider_budgets") if item["name"] == "next_available_at")
        if getattr(deadline["type"], "fsp", None) != 6:
            conn.execute(text("ALTER TABLE notification_provider_budgets MODIFY COLUMN next_available_at DATETIME(6) NOT NULL"))
            changed = True
    if conn.dialect.name in {"mysql", "mariadb"}:
        for name in ("notification_deliveries", "notification_events"):
            if inspector.has_table(name):
                retry = next((col for col in inspector.get_columns(name) if col["name"] == "next_retry_at"), None)
                if retry and getattr(retry["type"], "fsp", None) != 6:
                    conn.execute(text(f"ALTER TABLE {name} MODIFY COLUMN next_retry_at DATETIME(6) NULL"))
                    changed = True
    if not inspector.has_table(table_name):
        return changed
    columns = {col["name"]: col for col in inspector.get_columns(table_name)}
    constraints = {item["name"]: item for item in inspector.get_unique_constraints(table_name)}
    dialect = conn.dialect.name
    if dialect == "sqlite":
        if columns["announcement_id"]["nullable"] and "uq_notification_delivery" not in constraints:
            return changed
        # SQLite is a test dialect; production uses PostgreSQL or MySQL.
        # Create/copy/swap instead of dropping data or recreating identities.
        if conn.execute(text("PRAGMA foreign_keys")).scalar():
            raise RuntimeError("SQLite notification upgrade requires foreign_keys OFF during migration")
        metadata = MetaData()
        for table in NotificationDelivery.metadata.sorted_tables:
            table.to_metadata(metadata)
        temporary = NotificationDelivery.__table__.to_metadata(metadata, name="notification_deliveries_upgrade")
        conn.execute(CreateTable(temporary))
        names = [name for name in columns if name in temporary.c]
        quoted = ", ".join(conn.dialect.identifier_preparer.quote(name) for name in names)
        conn.execute(text(f"INSERT INTO notification_deliveries_upgrade ({quoted}) SELECT {quoted} FROM notification_deliveries"))
        conn.execute(text("DROP TABLE notification_deliveries"))
        conn.execute(text("ALTER TABLE notification_deliveries_upgrade RENAME TO notification_deliveries"))
        for index in NotificationDelivery.__table__.indexes:
            index.create(conn, checkfirst=True)
        return True
    if not columns["announcement_id"]["nullable"]:
        statement = (
            "ALTER TABLE notification_deliveries MODIFY COLUMN announcement_id INTEGER NULL"
            if dialect in {"mysql", "mariadb"} else
            "ALTER TABLE notification_deliveries ALTER COLUMN announcement_id DROP NOT NULL"
        )
        conn.execute(text(statement))
        changed = True
    if "uq_notification_delivery" in constraints:
        keyword = "INDEX" if dialect in {"mysql", "mariadb"} else "CONSTRAINT"
        conn.execute(text(f"ALTER TABLE notification_deliveries DROP {keyword} uq_notification_delivery"))
        changed = True
    # AddConstraint marks its constraint as excluded from inline CREATE TABLE.
    # Never attach that DDL state to the application's shared mapped metadata.
    target = NotificationDelivery.__table__.to_metadata(MetaData())
    for constraint in target.constraints:
        if constraint.name and constraint.name.startswith("uq_notification_") and constraint.name not in constraints:
            conn.execute(AddConstraint(constraint))
            changed = True
    return changed
