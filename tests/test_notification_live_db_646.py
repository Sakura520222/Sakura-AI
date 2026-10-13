"""Real MySQL/PostgreSQL transaction tests; skipped without an isolated test DB."""
import asyncio
import os
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.config import get_settings
from backend.models import Base
from backend.models import database as db_module
from backend.models.agent_team_models import AgentTeamTask
from backend.models.announcement_models import NotificationDelivery
from backend.models.database import IssueAnalysis
from backend.models.identity_models import NotificationEndpoint
from backend.models.notification_models import (
    NotificationEvent,
    NotificationSubscription,
)
from backend.models.telegram_models import TelegramUser
from backend.services import notification_events, telegram_service
from backend.services.issue_service import IssueService
from backend.services.notification_business import install_notification_outbox
from backend.services.notification_events import emit_event
from backend.services.notification_migration import upgrade_notification_schema
from backend.services.notification_service import (
    NotificationProviderRegistry,
    NotificationService,
)
from backend.services.telegram_service import TelegramService


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
        "agent_team_tasks",
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
@pytest.mark.parametrize("kind", ["review", "issue", "agent"])
@pytest.mark.parametrize("failure", ["insert", "commit"])
async def test_live_quota_denial_survives_alert_failure_without_expired_attribute_reads(live_db, monkeypatch, kind, failure):
    async with live_db() as db:
        user = await db.get(TelegramUser, 1)
        for prefix in ("", "issue_", "agent_"):
            for period in ("daily", "weekly", "monthly"):
                setattr(user, f"{prefix}{period}_quota", 10 if period == "daily" else 20)
                setattr(user, f"{prefix}{period}_used", 10 if period == "daily" else 0)
                from backend.core.time_service import now_utc

                setattr(user, f"last_reset_{prefix}{period}", now_utc())
        await db.commit()
    monkeypatch.setattr(telegram_service, "is_payment_enabled", AsyncMock(return_value=False))
    original_emit = notification_events.emit_event

    async def fail_after_insert(*args, **kwargs):
        await original_emit(*args, **kwargs)
        raise RuntimeError("alert persistence unavailable")

    if failure == "insert":
        monkeypatch.setattr(notification_events, "emit_event", fail_after_insert)
    async with live_db() as db:
        user = await db.get(TelegramUser, 1)
        if failure == "commit":
            monkeypatch.setattr(db, "commit", AsyncMock(side_effect=RuntimeError("alert commit unavailable")))
        service = TelegramService(db)
        if kind == "review":
            result = await service.check_and_consume_quota("alice", "Acme/repo", 646)
        elif kind == "issue":
            result = await service.check_and_consume_issue_quota("alice", "Acme/repo", 646)
        else:
            result = await service.check_and_consume_agent_quota("alice", "Acme/repo", 646)
        assert result[0] is False
        assert "每日配额已用完 (10/10)" in result[1]
        assert inspect(user).expired
    async with live_db() as db:
        assert (await db.execute(select(NotificationEvent))).scalars().all() == []
        user = await db.get(TelegramUser, 1)
        assert (user.daily_used, user.issue_daily_used, user.agent_daily_used) == (10, 10, 10)


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
async def test_live_agent_duplicate_admissions_and_early_retry_failures(live_db):
    async with live_db(info={"notification_outbox": False}) as db:
        db.add(AgentTeamTask(id=646, repo_full_name="Acme/repo", repo_owner="Acme", repo_name="repo",
            source_type="manual_issue", title="Task", status="failed", started_by="alice"))
        await db.commit()

    async def queue():
        async with live_db() as db:
            task = await db.get(AgentTeamTask, 646)
            task.status = "queued"
            await db.commit()

    for expected_round in (1, 2):
        await asyncio.gather(queue(), queue())
        async with live_db() as db:
            task = await db.get(AgentTeamTask, 646)
            assert task.execution_round == expected_round
            task.status = "failed"  # before skills/cloning/iteration begins
            await db.commit()
    async with live_db() as db:
        events = (await db.execute(select(NotificationEvent).where(NotificationEvent.resource_type == "agent"))).scalars().all()
        assert {event.run_key for event in events} == {"1:0:0", "2:0:0"}
        assert len(events) == 2


@pytest.mark.asyncio
async def test_live_agent_round_migration_preserves_existing_tasks(live_db):
    async with live_db(info={"notification_outbox": False}) as db:
        db.add(AgentTeamTask(id=646, repo_full_name="Acme/repo", repo_owner="Acme", repo_name="repo",
            source_type="manual_issue", title="Task", status="completed", started_by="alice"))
        await db.commit()
        await db.execute(text("ALTER TABLE agent_team_tasks DROP COLUMN execution_round"))
        conn = await db.connection()
        assert await conn.run_sync(upgrade_notification_schema) is True
        assert await conn.run_sync(upgrade_notification_schema) is False
        await db.commit()
    async with live_db() as db:
        task = await db.get(AgentTeamTask, 646)
        assert (task.status, task.execution_round) == ("completed", 0)
        assert (await db.execute(select(NotificationEvent))).scalars().all() == []


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
