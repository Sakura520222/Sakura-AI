"""Integration tests use actual SQL transactions, uniqueness and lease updates."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from backend.core.time_service import now_utc
from backend.models import Base
from backend.models import database as db_module
from backend.models.announcement_models import NotificationDelivery
from backend.models.identity_models import NotificationEndpoint
from backend.models.notification_models import (
    NotificationEvent,
    NotificationSubscription,
    UserNotification,
)
from backend.models.telegram_models import TelegramUser
from backend.services import notification_dispatch, notification_service
from backend.services.notification_business import install_notification_outbox
from backend.services.notification_events import emit_event


class AsyncDB:
    def __init__(self, engine, *, outbox=False):
        self.session = Session(engine, expire_on_commit=False, info={"notification_outbox": outbox})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.session.close()

    def get_bind(self):
        return self.session.get_bind()

    def add(self, row):
        self.session.add(row)

    def expunge(self, row):
        self.session.expunge(row)

    async def get(self, model, row_id, **kwargs):
        return self.session.get(model, row_id, **kwargs)

    async def execute(self, statement, *args, **kwargs):
        return self.session.execute(statement, *args, **kwargs)

    async def scalar(self, statement, *args, **kwargs):
        return self.session.scalar(statement, *args, **kwargs)

    async def refresh(self, row):
        self.session.refresh(row)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def delete(self, row):
        self.session.delete(row)


@pytest.fixture
def event_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'events.db'}")
    Base.metadata.create_all(engine)
    install_notification_outbox()
    settings = SimpleNamespace(
        notification_rate_limit_seconds=0, notification_send_timeout_seconds=5,
        notification_retry_max_attempts=3, notification_retry_initial_delay_seconds=1,
        notification_retry_backoff_factor=2, notification_batch_size=100,
        notification_max_concurrency=4, notification_poll_seconds=0.1,
    )
    monkeypatch.setattr(notification_service, "get_settings", lambda: settings)
    monkeypatch.setattr(notification_dispatch, "get_settings", lambda: settings)
    monkeypatch.setattr(db_module, "async_session", lambda: AsyncDB(engine))
    with Session(engine) as db:
        db.add_all([TelegramUser(id=1, github_username="alice", is_active=True),
                    TelegramUser(id=2, github_username="bob", is_active=True)])
        db.add_all([NotificationEndpoint(id=1, user_id=1, provider="email", address="a@example.org", verified=True, enabled=True),
                    NotificationEndpoint(id=2, user_id=1, provider="email", address="b@example.org", verified=True, enabled=True)])
        db.add_all([NotificationSubscription(user_id=1, event_type="review.*", endpoint_id=1),
                    NotificationSubscription(user_id=1, event_type="review.*", endpoint_id=2),
                    NotificationSubscription(user_id=1, event_type="review.*", endpoint_id=None)])
        db.commit()
    yield engine, settings
    engine.dispose()


async def make_event(db, *, run="1", event_type="review.completed", **kwargs):
    return await emit_event(db, event_type=event_type, resource_type="review", resource_id=100,
        run_key=run, title="Review", content="Complete", repo_name="alice/repo", recipient_ids=[1], **kwargs)


class Provider:
    channel = "email"

    def __init__(self):
        self.sent = []
        self.fail = set()

    async def send(self, *, endpoint, **_):
        if endpoint.id in self.fail:
            raise ConnectionError("network down")
        self.sent.append(endpoint.id)


def service(provider):
    return notification_service.NotificationService(notification_service.NotificationProviderRegistry({
        "email": provider, "web": notification_service.WebUINotificationProvider(),
    }))


@pytest.mark.asyncio
async def test_event_dedup_new_round_and_transaction_rollback(event_db):
    engine, _ = event_db
    async with AsyncDB(engine) as db:
        first = await make_event(db)
        duplicate = await make_event(db)
        assert first.id == duplicate.id
        new_round = await make_event(db, run="2")
        assert new_round.id != first.id
        await db.rollback()
    with Session(engine) as db:
        assert db.query(NotificationEvent).count() == 0


@pytest.mark.asyncio
async def test_endpoint_failures_recover_without_resending_success(event_db):
    engine, _ = event_db
    provider = Provider()
    provider.fail = {2}
    dispatcher = service(provider)
    async with AsyncDB(engine) as db:
        item = await make_event(db)
        await db.commit()
        await dispatcher.materialize_event(db, item.id)
        rows = (await db.execute(select(NotificationDelivery))).scalars().all()
        assert len(rows) == 3
        ids = [row.id for row in rows]
    for row_id in ids:
        async with AsyncDB(engine) as db:
            await dispatcher.deliver_event_row(db, row_id)
    with Session(engine) as db:
        rows = db.query(NotificationDelivery).all()
        assert [row.status for row in rows].count("sent") == 2
        pending = next(row for row in rows if row.status == "pending")
        assert pending.error_category == "transient"
        assert pending.next_retry_at is not None
        pending.next_retry_at = now_utc() - timedelta(seconds=1)
        db.commit()
    provider.fail.clear()
    await dispatcher.recover_events_once()
    assert provider.sent == [1, 2]
    with Session(engine) as db:
        assert all(row.status == "sent" for row in db.query(NotificationDelivery))
        assert db.query(UserNotification).one().read_at is None


@pytest.mark.asyncio
async def test_permission_revocation_and_endpoint_disable_cancel(event_db):
    engine, _ = event_db
    dispatcher = service(Provider())
    async with AsyncDB(engine) as db:
        item = await make_event(db)
        await db.commit()
        await dispatcher.materialize_event(db, item.id)
    with Session(engine) as db:
        db.get(TelegramUser, 1).github_username = "former-owner"
        db.commit()
    await dispatcher.recover_events_once()
    with Session(engine) as db:
        assert all(row.status == "cancelled" for row in db.query(NotificationDelivery))


@pytest.mark.asyncio
async def test_concurrent_claim_and_expired_lease_recovery(event_db):
    engine, _ = event_db
    provider = Provider()
    dispatcher = service(provider)
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.send

    async def held_send(**kwargs):
        entered.set()
        await release.wait()
        await original(**kwargs)

    provider.send = held_send
    async with AsyncDB(engine) as db:
        item = await make_event(db)
        await db.commit()
        await dispatcher.materialize_event(db, item.id)
        row_id = (await db.execute(select(NotificationDelivery.id).where(NotificationDelivery.endpoint_id == 1))).scalar_one()

    async def run():
        async with AsyncDB(engine) as db:
            return await dispatcher.deliver_event_row(db, row_id)

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 2)
    assert await run() is False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with Session(engine) as db:
        row = db.get(NotificationDelivery, row_id)
        assert row.claim_token
        row.claim_until = now_utc() - timedelta(seconds=1)
        db.commit()
    release.set()
    assert await run() is True
    assert provider.sent == [1]


@pytest.mark.asyncio
async def test_subscriber_cannot_gain_repository_permission(event_db):
    engine, _ = event_db
    with Session(engine) as db:
        db.add(NotificationSubscription(user_id=2, repo_name="alice/repo", event_type="review.*"))
        db.commit()
    async with AsyncDB(engine) as db:
        item = await make_event(db)
        await db.commit()
        await service(Provider()).materialize_event(db, item.id)
    with Session(engine) as db:
        assert {row.user_id for row in db.query(NotificationDelivery)} == {1}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["review", "issue", "scan", "agent"])
async def test_business_outbox_shares_commit_and_rollback(event_db, kind):
    from backend.models.agent_team_models import AgentTeamTask
    from backend.models.database import IssueAnalysis, PRReview
    from backend.models.scan_models import RepoScan

    engine, _ = event_db
    classes = {"review": PRReview, "issue": IssueAnalysis, "scan": RepoScan, "agent": AgentTeamTask}
    values = {"repo_owner": "alice", "repo_name": "repo", "status": "completed"}
    values.update({"review": {"pr_id": 500, "strategy": "comprehensive", "author": "alice"},
        "issue": {"issue_number": 42, "analysis_version": 1, "author": "alice", "title": "Issue"},
        "scan": {"trigger_type": "manual"}, "agent": {"repo_full_name": "alice/repo", "source_type": "manual_issue", "title": "Task"}}[kind])
    with Session(engine, info={"notification_outbox": True}) as db:
        db.add(classes[kind](**values))
        db.flush()
        assert db.query(NotificationEvent).one().event_type == f"{kind}.completed"
        db.rollback()
    with Session(engine) as db:
        assert db.query(NotificationEvent).count() == 0
    with Session(engine, info={"notification_outbox": True}) as db:
        record = classes[kind](**values)
        db.add(record)
        db.commit()
        db.refresh(record)
        record.status = "completed"  # duplicate business callback
        db.commit()
        assert db.query(NotificationEvent).count() == 1


@pytest.mark.asyncio
async def test_sensitive_account_events_exclude_webhooks_and_other_users(event_db):
    engine, _ = event_db
    with Session(engine) as db:
        db.add(NotificationEndpoint(id=3, user_id=1, provider="webhook", address="https://public.example.org/notify", enabled=True, verified=True))
        db.add_all([NotificationSubscription(user_id=1, event_type="security.*", endpoint_id=3),
                    NotificationSubscription(user_id=1, event_type="security.*", endpoint_id=1),
                    NotificationSubscription(user_id=1, event_type="security.*", endpoint_id=None),
                    NotificationSubscription(user_id=2, event_type="security.*", endpoint_id=None)])
        db.commit()
    class Webhook(Provider):
        channel = "webhook"
    dispatcher = service(Provider())
    dispatcher.registry.register(Webhook())
    async with AsyncDB(engine) as db:
        item = await emit_event(db, event_type="security.changed", resource_type="user", resource_id=1,
            run_key="mfa-enabled", title="Security", content="MFA changed", recipient_ids=[1], sensitive=True)
        await db.commit()
        await dispatcher.materialize_event(db, item.id)
    with Session(engine) as db:
        rows = db.query(NotificationDelivery).all()
        assert {row.channel for row in rows} == {"web", "email"}
        assert {row.user_id for row in rows} == {1}


@pytest.mark.asyncio
async def test_infrastructure_failure_does_not_spend_provider_attempt(event_db, monkeypatch):
    engine, _ = event_db
    provider = Provider()
    dispatcher = service(provider)
    async with AsyncDB(engine) as db:
        item = await make_event(db)
        await db.commit()
        await dispatcher.materialize_event(db, item.id)
        row_id = (await db.execute(select(NotificationDelivery.id).where(NotificationDelivery.endpoint_id == 1))).scalar_one()
    original = dispatcher._event_eligible

    async def unavailable(*_):
        raise ConnectionError("unavailable")
    monkeypatch.setattr(dispatcher, "_event_eligible", unavailable)
    async with AsyncDB(engine) as db:
        await dispatcher.deliver_event_row(db, row_id)
    with Session(engine) as db:
        row = db.get(NotificationDelivery, row_id)
        assert row.status == "pending"
        assert row.attempts == 0
        assert row.error_category == "infrastructure"
        row.next_retry_at = now_utc() - timedelta(seconds=1)
        db.commit()
    monkeypatch.setattr(dispatcher, "_event_eligible", original)
    async with AsyncDB(engine) as db:
        await dispatcher.deliver_event_row(db, row_id)
    assert provider.sent == [1]


@pytest.mark.asyncio
async def test_permanent_credentials_fail_once_and_rate_limit_is_durable(event_db):
    engine, settings = event_db
    settings.notification_rate_limit_seconds = 60
    dispatcher = service(Provider())
    async with AsyncDB(engine) as db:
        assert await dispatcher._reserve_provider_start(db, "email") is None
    # A separate service/session does not share memory with the first.
    async with AsyncDB(engine) as db:
        assert await service(Provider())._reserve_provider_start(db, "email") > now_utc()
    settings.notification_rate_limit_seconds = 0
    class Rejected(Provider):
        async def send(self, **_):
            raise notification_service.NotificationProviderPermanentError("token not accepted", "credential")
    dispatcher = service(Rejected())
    async with AsyncDB(engine) as db:
        item = await make_event(db)
        await db.commit()
        await dispatcher.materialize_event(db, item.id)
        row_id = (await db.execute(select(NotificationDelivery.id).where(NotificationDelivery.endpoint_id == 1))).scalar_one()
        from backend.models.notification_models import NotificationProviderBudget
        await db.execute(update(NotificationProviderBudget).values(next_available_at=now_utc()-timedelta(seconds=1)))
        await db.commit()
        await dispatcher.deliver_event_row(db, row_id)
    with Session(engine) as db:
        row = db.get(NotificationDelivery, row_id)
        assert row.status == "failed"
        assert row.error_category == "credential"
        assert row.attempts == 1
        assert row.next_retry_at is None
