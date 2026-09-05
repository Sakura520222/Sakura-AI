"""Regression tests for notification eligibility and retry compatibility."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from telegram.error import RetryAfter

from backend.models import Base
from backend.models import database as database_module
from backend.models.announcement_models import (
    Announcement,
    DeliveryStatus,
    NotificationDelivery,
)
from backend.models.identity_models import NotificationEndpoint
from backend.models.payment_models import RefundRequest
from backend.models.telegram_models import TelegramUser
from backend.services import notification_service, refund_notification_service


class _AsyncSQLiteSession:
    """Async-shaped facade over a real SQLite session."""

    def __init__(self, session: Session):
        self._session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self._session.close()

    async def execute(self, statement, *args, **kwargs):
        return self._session.execute(statement, *args, **kwargs)

    async def commit(self):
        self._session.commit()

    async def rollback(self):
        self._session.rollback()

    async def get(self, model, row_id, **kwargs):
        return self._session.get(model, row_id, **kwargs)

    async def close(self):
        self._session.close()


@pytest.fixture
def sqlite_database(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def factory():
        return _AsyncSQLiteSession(Session(engine, expire_on_commit=False))

    monkeypatch.setattr(database_module, "async_session", factory)
    session = _AsyncSQLiteSession(Session(engine, expire_on_commit=False))
    try:
        yield session, engine
    finally:
        session._session.close()
        engine.dispose()


def _settings(monkeypatch, **overrides):
    values = {
        "smtp_password": None,
        "telegram_bot_token": None,
        "notification_retry_max_attempts": 1,
        "notification_retry_initial_delay_seconds": 0,
        "notification_retry_backoff_factor": 1,
        "notification_rate_limit_seconds": 0,
        "notification_max_concurrency": 2,
    }
    values.update(overrides)
    monkeypatch.setattr(
        notification_service, "get_settings", lambda: SimpleNamespace(**values)
    )


async def _seed_delivery(
    session: _AsyncSQLiteSession,
    *,
    active: bool = True,
    channel: str = "web",
    endpoint: NotificationEndpoint | None = None,
):
    user = TelegramUser(id=1, is_active=active)
    announcement = Announcement(
        id=1,
        title="Title",
        content="Body",
        status="published",
        publication_version=1,
    )
    delivery = NotificationDelivery(
        id=1,
        announcement_id=1,
        user_id=1,
        channel=channel,
        status=DeliveryStatus.PENDING.value,
        publication_version=1,
    )
    values = [user, announcement, delivery]
    if endpoint is not None:
        values.append(endpoint)
    session._session.add_all(values)
    await session.commit()
    return announcement, delivery


class _CountingProvider:
    def __init__(self, channel: str = "web"):
        self.channel = channel
        self.calls = 0

    async def send(self, **_kwargs):
        self.calls += 1


@pytest.mark.asyncio
async def test_user_disabled_after_claim_is_not_contacted(sqlite_database, monkeypatch):
    session, engine = sqlite_database
    announcement, delivery = await _seed_delivery(session)
    provider = _CountingProvider()
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"web": provider})
    )
    _settings(monkeypatch)
    real_claim = service._claim_delivery

    async def claim_then_disable(db, row, item, *, expected_version):
        token = await real_claim(db, row, item, expected_version=expected_version)
        with Session(engine) as other:
            user = other.get(TelegramUser, 1)
            user.is_active = False
            other.commit()
        return token

    monkeypatch.setattr(service, "_claim_delivery", claim_then_disable)
    result = await service._deliver_one(
        session,
        delivery,
        announcement,
        None,
        asyncio.Lock(),
        expected_version=1,
    )

    assert result is False
    assert provider.calls == 0
    current = await session.get(NotificationDelivery, 1)
    assert current.status == DeliveryStatus.FAILED.value
    assert current.claim_token is None
    assert current.claim_until is None


@pytest.mark.asyncio
async def test_active_user_is_delivered_normally(sqlite_database, monkeypatch):
    session, _engine = sqlite_database
    announcement, delivery = await _seed_delivery(session)
    provider = _CountingProvider()
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"web": provider})
    )
    _settings(monkeypatch)

    result = await service._deliver_one(
        session,
        delivery,
        announcement,
        None,
        asyncio.Lock(),
        expected_version=1,
    )

    assert result is True
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_unverified_legacy_email_endpoint_is_not_contacted(
    sqlite_database, monkeypatch
):
    session, _engine = sqlite_database
    endpoint = NotificationEndpoint(
        id=1,
        user_id=1,
        provider="email",
        address="legacy@example.invalid",
        enabled=True,
        verified=False,
    )
    announcement, delivery = await _seed_delivery(
        session, channel="email", endpoint=endpoint
    )
    provider = _CountingProvider("email")
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"email": provider})
    )
    _settings(monkeypatch)

    result = await service._deliver_one(
        session,
        delivery,
        announcement,
        endpoint,
        asyncio.Lock(),
        expected_version=1,
    )

    assert result is False
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_endpoint_bound_to_another_user_is_not_contacted(
    sqlite_database, monkeypatch
):
    session, _engine = sqlite_database
    session._session.add(TelegramUser(id=2, is_active=True))
    endpoint = NotificationEndpoint(
        id=1,
        user_id=2,
        provider="email",
        address="other-user@example.invalid",
        enabled=True,
        verified=True,
    )
    announcement, delivery = await _seed_delivery(
        session, channel="email", endpoint=endpoint
    )
    provider = _CountingProvider("email")
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"email": provider})
    )
    _settings(monkeypatch)

    result = await service._deliver_one(
        session,
        delivery,
        announcement,
        endpoint,
        asyncio.Lock(),
        expected_version=1,
    )

    assert result is False
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_endpoint_with_wrong_provider_is_not_contacted(
    sqlite_database, monkeypatch
):
    session, _engine = sqlite_database
    endpoint = NotificationEndpoint(
        id=1,
        user_id=1,
        provider="telegram",
        address="12345",
        enabled=True,
        verified=True,
    )
    announcement, delivery = await _seed_delivery(
        session, channel="email", endpoint=endpoint
    )
    provider = _CountingProvider("email")
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"email": provider})
    )
    _settings(monkeypatch)

    result = await service._deliver_one(
        session,
        delivery,
        announcement,
        endpoint,
        asyncio.Lock(),
        expected_version=1,
    )

    assert result is False
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_verified_email_endpoint_is_delivered_normally(
    sqlite_database, monkeypatch
):
    session, _engine = sqlite_database
    endpoint = NotificationEndpoint(
        id=1,
        user_id=1,
        provider="email",
        address="verified@example.invalid",
        enabled=True,
        verified=True,
    )
    announcement, delivery = await _seed_delivery(
        session, channel="email", endpoint=endpoint
    )
    provider = _CountingProvider("email")
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"email": provider})
    )
    _settings(monkeypatch)

    result = await service._deliver_one(
        session,
        delivery,
        announcement,
        endpoint,
        asyncio.Lock(),
        expected_version=1,
    )

    assert result is True
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_refund_notifications_send_all_enabled_valid_endpoints(
    sqlite_database, monkeypatch
):
    session, _engine = sqlite_database
    session._session.add(TelegramUser(id=1, is_active=True))
    session._session.add_all(
        [
            NotificationEndpoint(
                user_id=1,
                provider="telegram",
                address="101",
                enabled=True,
            ),
            NotificationEndpoint(
                user_id=1,
                provider="telegram",
                address="0101",
                enabled=True,
            ),
            NotificationEndpoint(
                user_id=1,
                provider="telegram",
                address="102",
                enabled=True,
            ),
            NotificationEndpoint(
                user_id=1,
                provider="telegram",
                address="103",
                enabled=False,
            ),
            NotificationEndpoint(
                user_id=1,
                provider="telegram",
                address="not-a-chat-id",
                enabled=True,
            ),
            NotificationEndpoint(
                user_id=1,
                provider="email",
                address="person@example.invalid",
                enabled=True,
            ),
        ]
    )
    await session.commit()
    sender = SimpleNamespace(targets=[])

    async def send_to_targets(text, chat_ids):
        sender.targets.append((text, chat_ids))

    sender.send_to_targets = send_to_targets
    monkeypatch.setattr(
        refund_notification_service, "get_notification_sender", lambda: sender
    )
    request = RefundRequest(
        id=1,
        order_id=9,
        user_id=1,
        amount_cents=1000,
        currency="CNY",
        reason="duplicate",
    )

    await refund_notification_service.notify_refund_request_approved(session, request)
    await refund_notification_service.notify_refund_request_rejected(session, request)

    assert [chat_ids for _text, chat_ids in sender.targets] == [[101, 102], [101, 102]]


def test_retry_after_normalizes_numbers_and_timedelta():
    assert notification_service.normalize_retry_after(7) == 7
    assert notification_service.normalize_retry_after(7.5) == 7.5
    assert notification_service.normalize_retry_after(timedelta(seconds=7)) == 7
    assert notification_service.normalize_retry_after(timedelta(seconds=-1)) == 0
    assert (
        notification_service.NotificationProviderRetryAfter(
            "limited", timedelta(seconds=7)
        ).retry_after
        == 7
    )
    assert notification_service.normalize_retry_after(float("inf")) == 0


@pytest.mark.asyncio
async def test_worker_honors_timedelta_retry_after(sqlite_database, monkeypatch):
    session, _engine = sqlite_database
    announcement, delivery = await _seed_delivery(session)

    class _RetryProvider:
        channel = "web"

        def __init__(self):
            self.calls = 0

        async def send(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise notification_service.NotificationProviderRetryAfter(
                    "limited", timedelta(seconds=7)
                )

    provider = _RetryProvider()
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"web": provider})
    )
    _settings(
        monkeypatch,
        notification_retry_max_attempts=2,
        notification_retry_initial_delay_seconds=1,
        notification_retry_backoff_factor=2,
    )
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(notification_service.asyncio, "sleep", fake_sleep)
    result = await service._deliver_one(
        session,
        delivery,
        announcement,
        None,
        asyncio.Lock(),
        expected_version=1,
    )

    assert result is True
    assert provider.calls == 2
    assert sleeps == [7]


@pytest.mark.asyncio
async def test_ptb_retry_after_timedelta_is_normalized(monkeypatch):
    from backend.telegram import bot as telegram_bot

    class _Bot:
        base_url = "https://api.telegram.org/bot-token"

        async def send_message(self, **_kwargs):
            raise RetryAfter(timedelta(seconds=7))

    monkeypatch.setattr(
        notification_service,
        "get_settings",
        lambda: SimpleNamespace(telegram_enabled=True, telegram_bot_token="token"),
    )

    async def rich_fallback(*_args, **_kwargs):
        return False

    monkeypatch.setattr(notification_service, "_send_telegram_rich", rich_fallback)
    monkeypatch.setattr(telegram_bot, "get_telegram_bot", lambda: _Bot())
    endpoint = NotificationEndpoint(
        id=1, user_id=1, provider="telegram", address="42", enabled=True
    )

    with pytest.raises(notification_service.NotificationProviderRetryAfter) as exc_info:
        await notification_service.TelegramNotificationProvider().send(
            endpoint=endpoint,
            title="Title",
            content="Body",
            content_html="<p>Body</p>",
            announcement_type="general",
        )

    assert exc_info.value.retry_after == 7
    assert "retry_after=7s" in str(exc_info.value)
