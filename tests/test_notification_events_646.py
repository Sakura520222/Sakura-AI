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

    @property
    def info(self):
        return self.session.info

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

    async def refresh(self, row, **kwargs):
        self.session.refresh(row, **kwargs)

    def begin_nested(self):
        from tests.test_billing_entitlements import _AsyncSQLiteSession

        return _AsyncSQLiteSession(self.session).begin_nested()

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
async def test_provider_message_preserves_announcement_type_and_business_details(monkeypatch):
    from backend.services.notification_events import NotificationMessage

    received = []
    class Capture(notification_service.EmailNotificationProvider):
        async def send(self, **kwargs):
            received.append(kwargs)
    monkeypatch.setattr(notification_service, "get_settings", lambda: SimpleNamespace(sanitized_app_domain="app.example.org"))
    provider = Capture()
    await provider.send_message(endpoint=None, message=NotificationMessage("id", "announcement.published", "Release", "Body", "info", payload={"announcement_type": "release"}))
    assert received[0]["announcement_type"] == "release"
    await provider.send_message(endpoint=None, message=NotificationMessage("id2", "review.completed", "Review", "Done", "info", detail_url="/pr/100"))
    assert "https://app.example.org/pr/100" in received[1]["content"]
    assert notification_service._notification_type_label("review.completed") == "Review Completed"


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
async def test_announcement_adapter_keeps_per_attempt_counts_and_no_resend(event_db):
    from backend.models.announcement_models import Announcement
    from backend.services.announcement_service import _ensure_delivery_rows

    engine, _ = event_db
    provider = Provider()
    dispatcher = service(provider)
    async with AsyncDB(engine) as db:
        announcement = Announcement(id=1, title="Release", content="Body", status="published", publication_version=1, announcement_type="release")
        db.add(announcement)
        await db.flush()
        await _ensure_delivery_rows(db, 1, 1)
        await db.commit()
        assert await dispatcher.broadcast_announcement(db, announcement, expected_version=1) == {"sent": 4, "failed": 0, "skipped": 0}
        assert await dispatcher.broadcast_announcement(db, announcement, expected_version=1) == {"sent": 0, "failed": 0, "skipped": 0}
    assert provider.sent == [1, 2]


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
async def test_disabled_rules_suppress_inbox_and_announcement_prompts(event_db):
    from backend.models.announcement_models import Announcement
    from backend.services.announcement_service import unread_count

    engine, _ = event_db
    with Session(engine) as db:
        db.query(NotificationSubscription).delete()
        db.add_all([NotificationSubscription(user_id=1, event_type="review.*", enabled=False),
                    NotificationSubscription(user_id=1, event_type="announcement.*", enabled=False)])
        db.add(Announcement(id=1, title="Published", content="Body", status="published", publication_version=1))
        db.commit()
    async with AsyncDB(engine) as db:
        item = await make_event(db)
        await db.commit()
        await service(Provider()).materialize_event(db, item.id)
        assert await unread_count(db, 1) == 0
        assert await unread_count(db, 2) == 1  # default compatibility recipient
    with Session(engine) as db:
        assert db.query(UserNotification).count() == 0
        assert db.query(NotificationDelivery).count() == 0


@pytest.mark.asyncio
async def test_quota_new_reset_round_emits_new_event(event_db):
    from backend.services.telegram_service import TelegramService

    engine, _ = event_db
    async with AsyncDB(engine, outbox=True) as db:
        user = await db.get(TelegramUser, 1)
        user.daily_used = user.daily_quota
        user.last_reset_daily = now_utc()
        await db.commit()
        source = TelegramService(db)
        await source._notify_quota_exhausted(user, "review")
        await source._notify_quota_exhausted(user, "review")
        rows = (await db.execute(select(NotificationEvent))).scalars().all()
        assert len(rows) == 1
        user.last_reset_daily += timedelta(hours=24)
        await db.commit()
        await source._notify_quota_exhausted(user, "review")
        rows = (await db.execute(select(NotificationEvent))).scalars().all()
        assert len(rows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["review", "issue", "scan", "agent"])
async def test_business_outbox_shares_commit_and_rollback(event_db, kind):
    from backend.models.agent_team_models import AgentTeamTask
    from backend.models.database import IssueAnalysis, PRReview
    from backend.models.scan_models import RepoScan

    engine, _ = event_db
    classes = {"review": PRReview, "issue": IssueAnalysis, "scan": RepoScan, "agent": AgentTeamTask}
    values = {"repo_owner": "alice", "repo_name": "repo", "status": "completed"}
    values.update({"review": {"pr_id": 500, "strategy": "comprehensive", "author": "ALIce"},
        "issue": {"issue_number": 42, "analysis_version": 1, "author": "ALIce", "title": "Issue"},
        "scan": {"trigger_type": "manual", "triggered_by": "ALIce"}, "agent": {"repo_full_name": "alice/repo", "source_type": "manual_issue", "title": "Task"}}[kind])
    with Session(engine, info={"notification_outbox": True}) as db:
        db.add(classes[kind](**values))
        db.flush()
        assert db.query(NotificationEvent).one().event_type == f"{kind}.completed"
        if kind in {"review", "issue", "scan"}:
            assert db.query(NotificationEvent).one().recipients_json == "[1]"
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
        if kind in {"review", "issue"}:
            from backend.webui.deps import build_user_scope_filter

            record.repo_owner = "Acme"
            db.commit()
            scope = build_user_scope_filter({"sub": "alice", "role": "user"}, classes[kind])
            assert db.scalars(select(classes[kind]).where(scope)).one().id == record.id


@pytest.mark.asyncio
async def test_business_result_severity_routes_critical_subscriptions(event_db):
    from backend.models.database import IssueAnalysis

    engine, _ = event_db
    with Session(engine, info={"notification_outbox": True}) as db:
        db.add(NotificationSubscription(user_id=1, event_type="issue.*", min_severity="critical", endpoint_id=1))
        db.add(IssueAnalysis(repo_owner="alice", repo_name="repo", issue_number=646,
            analysis_version=1, author="alice", title="Critical issue", status="completed", priority="critical"))
        db.commit()
        item = db.query(NotificationEvent).one()
        assert item.severity == "critical"
        assert '"issue_number": 646' in item.payload_json
        event_id = item.id
    async with AsyncDB(engine) as db:
        await service(Provider()).materialize_event(db, event_id)
    with Session(engine) as db:
        assert db.query(NotificationDelivery).filter_by(endpoint_id=1).one().status == "pending"


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

    # Short budget contention must drain this cycle, not sleep for a whole
    # poll; a final transient failure is exhausted, not permanently transient.
    settings.notification_rate_limit_seconds = 0.01
    settings.notification_poll_seconds = 0.3
    settings.notification_retry_max_attempts = 1
    provider = Provider()
    provider.fail = {2}
    dispatcher = service(provider)
    async with AsyncDB(engine) as db:
        item = await make_event(db, run="drain")
        await db.commit()
        await dispatcher.materialize_event(db, item.id)
        event_id = item.id
    await dispatcher.recover_events_once()
    with Session(engine) as db:
        rows = db.query(NotificationDelivery).filter_by(event_id=event_id).all()
        assert all(row.status != "pending" for row in rows)
        failed = next(row for row in rows if row.endpoint_id == 2)
        assert failed.error_category == "exhausted" and failed.attempts == 1
        assert "transient" in failed.error_message
    assert provider.sent == [1]


@pytest.mark.asyncio
async def test_endpoint_rate_limit_does_not_pause_other_destinations(event_db):
    engine, _ = event_db
    class Limited(Provider):
        async def send(self, *, endpoint, **kwargs):
            if endpoint.id == 1:
                raise notification_service.NotificationProviderRetryAfter("Limited target", 60, scope="endpoint")
            await super().send(endpoint=endpoint, **kwargs)
    provider = Limited()
    dispatcher = service(provider)
    async with AsyncDB(engine) as db:
        item = await make_event(db)
        await db.commit()
        await dispatcher.materialize_event(db, item.id)
        ids = (await db.execute(select(NotificationDelivery.id).where(NotificationDelivery.endpoint_id.is_not(None)))).scalars().all()
    for row_id in ids:
        async with AsyncDB(engine) as db:
            await dispatcher.deliver_event_row(db, row_id)
    assert provider.sent == [2]
    with Session(engine) as db:
        limited = db.query(NotificationDelivery).filter_by(endpoint_id=1).one()
        assert limited.status == "pending"
        assert limited.error_category == "rate_limit"
        assert db.query(NotificationDelivery).filter_by(endpoint_id=2).one().status == "sent"
