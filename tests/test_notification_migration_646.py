"""Upgrade old delivery schemas without replaying historical announcements."""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.schema import CreateTable

from backend.models import Base
from backend.models.announcement_models import NotificationDelivery
from backend.models.database import _build_add_column_sql
from backend.services.notification_migration import upgrade_notification_schema


def test_upgrade_preserves_ids_outcomes_and_is_idempotent():
    engine = create_engine("sqlite://")
    with engine.begin() as db:
        db.execute(text("""CREATE TABLE notification_deliveries (
            id INTEGER PRIMARY KEY, announcement_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, channel VARCHAR(50) NOT NULL,
            status VARCHAR(50) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, publication_version INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_notification_delivery UNIQUE(announcement_id, user_id, channel))"""))
        db.execute(text("INSERT INTO notification_deliveries (id, announcement_id, user_id, channel, status, attempts, publication_version) VALUES (7, 2, 1, 'email', 'sent', 3, 4)"))
        Base.metadata.create_all(db)
        existing = {column["name"] for column in inspect(db).get_columns("notification_deliveries")}
        for column in NotificationDelivery.__table__.columns:
            if column.name not in existing:
                # Exercise the same additive upgrader as the application.
                db.execute(text(_build_add_column_sql(db.dialect, "notification_deliveries", column)))
        assert upgrade_notification_schema(db) is True
        assert upgrade_notification_schema(db) is False
        row = db.execute(text("SELECT id, announcement_id, status, attempts, publication_version, event_id, endpoint_key FROM notification_deliveries")).one()
        assert tuple(row) == (7, 2, "sent", 3, 4, None, "legacy")
        assert db.execute(text("SELECT count(*) FROM notification_events")).scalar() == 0
        columns = {item["name"]: item for item in inspect(db).get_columns("notification_deliveries")}
        assert columns["announcement_id"]["nullable"]
    engine.dispose()


def test_delivery_schema_compiles_for_production_dialects():
    for dialect in (mysql.dialect(), postgresql.dialect()):
        sql = str(CreateTable(NotificationDelivery.__table__).compile(dialect=dialect))
        assert "uq_notification_event_target" in sql
        assert "endpoint_key" in sql
        assert "notification_events" in sql
        assert "announcement_id INTEGER NOT NULL" not in sql
    from backend.models.notification_models import NotificationProviderBudget

    assert "DATETIME(6)" in str(CreateTable(NotificationProviderBudget.__table__).compile(dialect=mysql.dialect()))


def test_agent_round_upgrade_preserves_legacy_tasks_without_replay():
    engine = create_engine("sqlite://")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE agent_team_tasks (id INTEGER PRIMARY KEY, status VARCHAR(50))"))
        db.execute(text("INSERT INTO agent_team_tasks VALUES (7, 'completed')"))
        assert upgrade_notification_schema(db) is True
        assert upgrade_notification_schema(db) is False
        assert tuple(db.execute(text("SELECT id, status, execution_round FROM agent_team_tasks")).one()) == (7, "completed", 0)
    engine.dispose()
