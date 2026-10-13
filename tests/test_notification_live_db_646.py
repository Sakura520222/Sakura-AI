"""Real MySQL/PostgreSQL transaction tests; skipped without an isolated test DB."""
import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.config import get_settings
from backend.models import Base
from backend.models import database as db_module
from backend.models.announcement_models import NotificationDelivery
from backend.models.database import IssueAnalysis
from backend.models.identity_models import NotificationEndpoint
from backend.models.notification_models import (
    NotificationEvent,
    NotificationSubscription,
)
from backend.models.telegram_models import TelegramUser
from backend.services.issue_service import IssueService
from backend.services.notification_business import install_notification_outbox
from backend.services.notification_events import emit_event
from backend.services.notification_migration import upgrade_notification_schema
from backend.services.notification_service import (
    NotificationProviderRegistry,
    NotificationService,
)


@pytest_asyncio.fixture
async def live_db(monkeypatch):
    url = os.environ.get("NOTIFICATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("No isolated MySQL/PostgreSQL test database configured")
    parsed = make_url(url)
    if parsed.database != "sakura_notification_test" or parsed.host not in {"127.0.0.1", "localhost"}:
        pytest.fail("Notification integration tests require an isolated localhost test database")
    engine = create_async_engine(url)
    table_names = {
        "telegram_users", "notification_endpoints", "announcements", "notification_events",
        "notification_deliveries", "user_notifications", "notification_subscriptions",
        "notification_provider_budgets", "user_repo_subscriptions",
        "issue_analyses",
    }
    tables = [table for table in Base.metadata.sorted_tables if table.name in table_names]
    async with engine.begin() as conn:
        await conn.run_sync(lambda db: Base.metadata.drop_all(db, tables=tables))
        await conn.run_sync(lambda db: Base.metadata.create_all(db, tables=tables))
    install_notification_outbox()
    factory = async_sessionmaker(engine, expire_on_commit=False, info={"notification_outbox": True})
    monkeypatch.setattr(db_module, "async_session", factory)
    monkeypatch.setattr(get_settings(), "notification_rate_limit_seconds", 0)
    async with factory() as db:
        db.add(TelegramUser(id=1, github_username="alice", is_active=True))
        await db.flush()
        db.add(NotificationEndpoint(id=1, user_id=1, provider="email", address="a@example.org", verified=True, enabled=True))
        await db.flush()
        db.add(NotificationSubscription(user_id=1, event_type="review.*", endpoint_id=1))
        await db.commit()
    try:
        yield factory
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda db: Base.metadata.drop_all(db, tables=tables))
        await engine.dispose()


@pytest.mark.asyncio
async def test_live_database_concurrent_producers_share_event(live_db):
    async def produce():
        async with live_db() as db:
            item = await emit_event(db, event_type="review.completed", resource_type="review", resource_id=1,
                run_key="1", title="Completed", content="Ready", repo_name="alice/repo", recipient_ids=[1])
            await db.commit()
            return item.id
    ids = await asyncio.gather(*(produce() for _ in range(4)))
    assert len(set(ids)) == 1


@pytest.mark.asyncio
async def test_live_issue_completion_and_close_race_emit_only_committed_transition(live_db):
    async with live_db() as db:
        db.add(IssueAnalysis(id=646, issue_number=646, repo_name="repo", repo_owner="alice",
            author="alice", title="Issue", status="analyzing", analysis_version=1))
        await db.commit()

    async def complete():
        async with live_db() as db:
            await IssueService().save_analysis_result({"summary": "Done", "priority": "high"},
                {"repo_owner": "alice", "repo_name": "repo", "issue_number": 646}, db, analysis_id=646)

    async def close():
        async with live_db() as db:
            await IssueService().mark_issue_closed("alice", "repo", 646, db)

    await asyncio.gather(complete(), close(), close())
    async with live_db() as db:
        record = await db.get(IssueAnalysis, 646)
        events = (await db.execute(select(NotificationEvent).where(NotificationEvent.resource_type == "issue"))).scalars().all()
        assert record.issue_state == "closed"
        assert record.status in {"completed", "cancelled"}
        # Seeding an active task through an application session legitimately
        # emits started. The concurrent writers must add exactly one terminal.
        assert len(events) == 2
        by_type = {event.event_type: event for event in events}
        assert set(by_type) == {"issue.started", f"issue.{record.status}"}
        if record.status == "completed":
            assert by_type["issue.completed"].severity == "warning"


@pytest.mark.asyncio
async def test_live_database_competing_claims_send_once(live_db):
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    class Provider:
        channel = "email"
        async def send(self, **_):
            entered.set()
            await release.wait()
            sent.append(1)
    dispatcher = NotificationService(NotificationProviderRegistry({"email": Provider()}))
    async with live_db() as db:
        item = await emit_event(db, event_type="review.completed", resource_type="review", resource_id=1,
            run_key="1", title="Completed", content="Ready", repo_name="alice/repo", recipient_ids=[1])
        await db.commit()
        await dispatcher.materialize_event(db, item.id)
        row_id = (await db.execute(select(NotificationDelivery.id))).scalar_one()
    async def deliver():
        async with live_db() as db:
            return await dispatcher.deliver_event_row(db, row_id)
    first = asyncio.create_task(deliver())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert await deliver() is False
    finally:
        release.set()
        await first
    assert sent == [1]


@pytest.mark.asyncio
async def test_live_legacy_unique_key_and_nullability_upgrade(live_db):
    async with live_db() as db:
        conn = await db.connection()
        dialect = conn.dialect.name
        keyword = "INDEX" if dialect in {"mysql", "mariadb"} else "CONSTRAINT"
        for name in ("uq_notification_event_target", "uq_notification_delivery_target"):
            await db.execute(text(f"ALTER TABLE notification_deliveries DROP {keyword} {name}"))
        statement = ("ALTER TABLE notification_deliveries MODIFY COLUMN announcement_id INTEGER NOT NULL"
                     if dialect in {"mysql", "mariadb"} else
                     "ALTER TABLE notification_deliveries ALTER COLUMN announcement_id SET NOT NULL")
        await db.execute(text(statement))
        await db.execute(text("ALTER TABLE notification_deliveries ADD CONSTRAINT uq_notification_delivery UNIQUE (announcement_id, user_id, channel)"))
        assert await conn.run_sync(upgrade_notification_schema) is True
        assert await conn.run_sync(upgrade_notification_schema) is False
        await db.commit()
