"""Notification failures preserve critical state and optional-alert boundaries."""
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.core.time_service import now_utc
from backend.models import database as db_module
from backend.models.agent_team_models import AgentTeamTask
from backend.models.notification_models import NotificationEvent
from backend.models.scan_models import RepoScan
from backend.models.telegram_models import QuotaUsageLog, TelegramUser
from backend.services import (
    notification_business,
    notification_events,
    telegram_service,
)
from backend.services.telegram_service import TelegramService
from backend.workers import scan_worker
from tests.test_notification_events_646 import AsyncDB
from tests.test_notification_events_646 import event_db as _event_db


@pytest.fixture
def event_db(tmp_path, monkeypatch):
    yield from _event_db.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_scan_terminal_outbox_failure_propagates_after_fresh_transaction_retries(event_db, monkeypatch, status):
    engine, _ = event_db
    with Session(engine) as db:
        db.add(RepoScan(id=646, repo_owner="alice", repo_name="alice/repo", trigger_type="manual", status="reporting"))
        db.commit()
    original_insert = notification_business._insert_event
    attempts = []

    def fail_after_insert(conn, **kwargs):
        original_insert(conn, **kwargs)
        attempts.append(kwargs["event_type"])
        raise OperationalError("outbox", None, ConnectionError("lost connection"))

    original_retry = scan_worker._db_retry

    async def immediate_retry(func):
        return await original_retry(func, delay=0)

    monkeypatch.setattr(db_module, "async_session", lambda: AsyncDB(engine, outbox=True))
    monkeypatch.setattr(notification_business, "_insert_event", fail_after_insert)
    monkeypatch.setattr(scan_worker, "_db_retry", immediate_retry)
    worker = scan_worker.ScanWorker.__new__(scan_worker.ScanWorker)
    with pytest.raises(OperationalError, match="lost connection"):
        await worker._update_scan(646, status=status)
    assert attempts == [f"scan.{status}"] * 3
    with Session(engine) as db:
        assert db.get(RepoScan, 646).status == "reporting"
        assert db.query(NotificationEvent).count() == 0


@pytest.mark.asyncio
async def test_scan_transient_outbox_error_recovers_without_duplicate_events(event_db, monkeypatch):
    engine, _ = event_db
    with Session(engine) as db:
        db.add(RepoScan(id=646, repo_owner="alice", repo_name="alice/repo", trigger_type="manual", status="reporting"))
        db.commit()
    original_insert, original_retry = notification_business._insert_event, scan_worker._db_retry
    attempts = []

    def flaky_insert(conn, **kwargs):
        original_insert(conn, **kwargs)
        attempts.append(1)
        if len(attempts) < 3:
            raise OperationalError("outbox", None, ConnectionError("lost connection"))

    async def immediate_retry(func):
        return await original_retry(func, delay=0)

    monkeypatch.setattr(db_module, "async_session", lambda: AsyncDB(engine, outbox=True))
    monkeypatch.setattr(notification_business, "_insert_event", flaky_insert)
    monkeypatch.setattr(scan_worker, "_db_retry", immediate_retry)
    await scan_worker.ScanWorker.__new__(scan_worker.ScanWorker)._update_scan(646, status="completed")
    with Session(engine) as db:
        assert db.get(RepoScan, 646).status == "completed"
        assert db.query(NotificationEvent).one().event_type == "scan.completed"
    assert len(attempts) == 3


def exhaust_quotas(engine):
    with Session(engine) as db:
        user = db.get(TelegramUser, 1)
        for prefix in ("", "issue_", "agent_"):
            for period in ("daily", "weekly", "monthly"):
                setattr(user, f"{prefix}{period}_quota", 10 if period == "daily" else 20)
                setattr(user, f"{prefix}{period}_used", 10 if period == "daily" else 0)
                setattr(user, f"last_reset_{prefix}{period}", now_utc())
        db.commit()


async def consume(source, kind):
    if kind == "review":
        return await source.check_and_consume_quota("alice", "Acme/repo", 646)
    if kind == "issue":
        return await source.check_and_consume_issue_quota("alice", "Acme/repo", 646)
    return await source.check_and_consume_agent_quota("alice", "Acme/repo", 646)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["review", "issue", "agent"])
@pytest.mark.parametrize("failure", ["insert", "commit"])
async def test_quota_denial_survives_alert_failure_and_expired_user(event_db, monkeypatch, kind, failure):
    engine, _ = event_db
    exhaust_quotas(engine)
    monkeypatch.setattr(telegram_service, "is_payment_enabled", AsyncMock(return_value=False))
    original_emit = notification_events.emit_event
    secret = "must-not-appear-in-alert-failure-log"

    async def fail_after_insert(*args, **kwargs):
        await original_emit(*args, **kwargs)
        raise RuntimeError(secret)

    class CommitFailureDB(AsyncDB):
        async def commit(self):
            raise RuntimeError(secret)

    if failure == "insert":
        monkeypatch.setattr(notification_events, "emit_event", fail_after_insert)
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        adapter = CommitFailureDB if failure == "commit" else AsyncDB
        async with adapter(engine, outbox=True) as db:
            source = TelegramService(db)
            original_lookup = source.get_user_by_github_username
            captured = []

            async def lookup(username):
                user = await original_lookup(username)
                captured.append(user)
                return user

            source.get_user_by_github_username = lookup
            ok, reason = await consume(source, kind)
            assert (ok, reason) == (False, f"{ {'review': '', 'issue': 'Issue ', 'agent': 'Agent '}[kind]}每日配额已用完 (10/10)")
            assert inspect(captured[0]).expired  # no user access after rollback
    finally:
        logger.remove(sink)
    assert any("Quota alert persistence failed" in message for message in messages)
    assert secret not in "".join(messages)
    with Session(engine) as db:
        assert db.query(NotificationEvent).count() == 0
        assert db.query(QuotaUsageLog).count() == 0
        user = db.get(TelegramUser, 1)
        assert (user.daily_used, user.issue_daily_used, user.agent_daily_used) == (10, 10, 10)


@pytest.mark.asyncio
async def test_agent_quota_alert_failure_still_cleans_up_precreated_task(event_db, monkeypatch):
    from backend.api import webhook

    engine, _ = event_db
    exhaust_quotas(engine)
    with Session(engine) as db:
        db.add(AgentTeamTask(id=646, repo_full_name="Acme/repo", repo_owner="Acme", repo_name="repo",
            source_type="manual_issue", title="Task", status="queued", started_by="alice"))
        db.commit()
    monkeypatch.setattr(telegram_service, "is_payment_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(notification_events, "emit_event", AsyncMock(side_effect=RuntimeError("alert unavailable")))
    monkeypatch.setattr(webhook, "get_async_session", lambda: AsyncDB(engine, outbox=True))
    monkeypatch.setattr(webhook, "_post_issue_comment", AsyncMock())
    response = await webhook._consume_agent_quota_or_cleanup(None, "alice", "repo", "Acme/repo", 646, 646)
    assert response.status_code == 200
    assert b'"reason":"quota exceeded"' in response.body
    with Session(engine) as db:
        assert db.get(AgentTeamTask, 646) is None
        assert db.query(NotificationEvent).count() == 0


@pytest.mark.asyncio
async def test_successful_quota_denial_alert_is_persisted_and_deduplicated(event_db, monkeypatch):
    engine, _ = event_db
    exhaust_quotas(engine)
    monkeypatch.setattr(telegram_service, "is_payment_enabled", AsyncMock(return_value=False))
    async with AsyncDB(engine, outbox=True) as db:
        source = TelegramService(db)
        assert (await consume(source, "review"))[0] is False
        assert (await consume(source, "review"))[0] is False
    with Session(engine) as db:
        assert db.query(NotificationEvent).one().event_type == "quota.exceeded"
        assert db.query(QuotaUsageLog).count() == 0
