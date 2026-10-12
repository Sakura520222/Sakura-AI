"""Idempotent delivery schema upgrade preserving rows, archives and read markers."""
from sqlalchemy import MetaData, text
from sqlalchemy.schema import AddConstraint, CreateTable

from backend.models.announcement_models import NotificationDelivery


def upgrade_notification_schema(conn):
    from sqlalchemy import inspect

    inspector = inspect(conn)
    table_name = "notification_deliveries"
    if not inspector.has_table(table_name):
        return False
    columns = {col["name"]: col for col in inspector.get_columns(table_name)}
    constraints = {item["name"]: item for item in inspector.get_unique_constraints(table_name)}
    dialect = conn.dialect.name
    changed = False
    if dialect == "sqlite":
        if columns["announcement_id"]["nullable"] and "uq_notification_delivery" not in constraints:
            return False
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
    for constraint in NotificationDelivery.__table__.constraints:
        if constraint.name and constraint.name.startswith("uq_notification_") and constraint.name not in constraints:
            conn.execute(AddConstraint(constraint))
            changed = True
    return changed
