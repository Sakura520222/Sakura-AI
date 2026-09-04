"""SQLite regressions for announcement delivery claims and leases.

The application uses async SQLAlchemy, while this environment intentionally
does not install ``aiosqlite``.  These tests keep the database real and only
adapt the synchronous SQLAlchemy session at the await boundary.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.core.time_service import now_utc
from backend.models import Base
from backend.models import database as database_module
from backend.models.announcement_models import (
    Announcement,
    DeliveryStatus,
    NotificationDelivery,
)
from backend.services import notification_service


class _AsyncSQLiteSession:
    """Small awaitable facade around a real synchronous SQLite session."""

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

    async def flush(self):
        self._session.flush()

    async def close(self):
        self._session.close()


@pytest.fixture
def delivery_database(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def factory():
        return _AsyncSQLiteSession(Session(engine))

    monkeypatch.setattr(database_module, "async_session", factory)
    session = _AsyncSQLiteSession(Session(engine))
    try:
        yield session, engine
    finally:
        session._session.close()
        engine.dispose()


async def _seed_delivery(session: _AsyncSQLiteSession, *, claim_until=None):
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
        channel="web",
        status=DeliveryStatus.PENDING.value,
        publication_version=1,
        claim_token="expired-worker" if claim_until is not None else None,
        claim_until=claim_until,
    )
    session._session.add_all([announcement, delivery])
    await session.commit()
    return announcement, delivery


def _settings(monkeypatch, **overrides):
    values = {
        "smtp_password": None,
        "telegram_bot_token": None,
        "notification_retry_max_attempts": 1,
        "notification_retry_initial_delay_seconds": 0,
        "notification_retry_backoff_factor": 1,
        "notification_rate_limit_seconds": 0,
        "notification_max_concurrency": 4,
    }
    values.update(overrides)
    monkeypatch.setattr(notification_service, "get_settings", lambda: SimpleNamespace(**values))


class _CountingProvider:
    channel = "web"

    def __init__(self, wait: float = 0):
        self.calls = 0
        self.wait = wait

    async def send(self, **_kwargs):
        self.calls += 1
        if self.wait:
            await asyncio.sleep(self.wait)
        else:
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_two_concurrent_broadcasts_claim_one_row(delivery_database, monkeypatch):
    session, engine = delivery_database
    announcement, _delivery = await _seed_delivery(session)
    provider = _CountingProvider(wait=0.03)
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"web": provider})
    )
    _settings(monkeypatch)
    first = _AsyncSQLiteSession(Session(engine))
    second = _AsyncSQLiteSession(Session(engine))

    try:
        results = await asyncio.gather(
            service.broadcast_announcement(first, announcement, expected_version=1),
            service.broadcast_announcement(second, announcement, expected_version=1),
        )
        assert provider.calls == 1
        assert sorted(result["sent"] for result in results) == [0, 1]
        assert sorted(result["skipped"] for result in results) == [0, 1]
        current = await session.get(
            NotificationDelivery, 1, populate_existing=True
        )
        assert current.status == DeliveryStatus.SENT.value
        assert current.claim_token is None
        assert current.claim_until is None
    finally:
        first._session.close()
        second._session.close()


@pytest.mark.asyncio
async def test_sent_row_cannot_be_claimed_again(delivery_database, monkeypatch):
    session, engine = delivery_database
    announcement, delivery = await _seed_delivery(session)
    provider = _CountingProvider()
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"web": provider})
    )
    _settings(monkeypatch)
    worker = _AsyncSQLiteSession(Session(engine))
    try:
        token = await service._claim_delivery(
            worker, delivery, announcement, expected_version=1
        )
        assert token
        await worker.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id == delivery.id)
            .values(status=DeliveryStatus.SENT.value, claim_token=None, claim_until=None)
        )
        await worker.commit()
        assert (
            await service._claim_delivery(
                worker, delivery, announcement, expected_version=1
            )
            is None
        )
        assert provider.calls == 0
    finally:
        worker._session.close()


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_cleared(delivery_database, monkeypatch):
    session, engine = delivery_database
    announcement, delivery = await _seed_delivery(
        session, claim_until=now_utc() - timedelta(seconds=1)
    )
    provider = _CountingProvider()
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"web": provider})
    )
    _settings(monkeypatch)
    worker = _AsyncSQLiteSession(Session(engine))
    try:
        result = await service._deliver_one(
            worker,
            delivery,
            announcement,
            None,
            asyncio.Lock(),
            expected_version=1,
        )
        assert result is True
        assert provider.calls == 1
        current = await session.get(NotificationDelivery, 1)
        assert current.status == DeliveryStatus.SENT.value
        assert current.claim_token is None
        assert current.claim_until is None
    finally:
        worker._session.close()


@pytest.mark.asyncio
async def test_stale_worker_token_cannot_write_terminal_state(
    delivery_database, monkeypatch
):
    session, engine = delivery_database
    announcement, delivery = await _seed_delivery(session)
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"web": _CountingProvider()})
    )
    _settings(monkeypatch)
    worker = _AsyncSQLiteSession(Session(engine))
    takeover = _AsyncSQLiteSession(Session(engine))
    try:
        old_token = await service._claim_delivery(
            worker, delivery, announcement, expected_version=1
        )
        assert old_token
        new_token = "new-worker-token"
        await takeover.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id == delivery.id)
            .values(claim_token=new_token, claim_until=now_utc() + timedelta(minutes=5))
        )
        await takeover.commit()
        marked = await service._mark_terminal_state(
            worker,
            delivery,
            announcement,
            lock=asyncio.Lock(),
            expected_version=1,
            worker_token=old_token,
            values={"status": DeliveryStatus.SENT.value},
        )
        assert marked is None
        current = await session.get(
            NotificationDelivery, 1, populate_existing=True
        )
        assert current.status == DeliveryStatus.PENDING.value
        assert current.claim_token == new_token
    finally:
        worker._session.close()
        takeover._session.close()


@pytest.mark.asyncio
async def test_provider_failure_releases_claim(delivery_database, monkeypatch):
    session, engine = delivery_database
    announcement, _delivery = await _seed_delivery(session)

    class _FailingProvider:
        channel = "web"

        async def send(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"web": _FailingProvider()})
    )
    _settings(monkeypatch)
    worker = _AsyncSQLiteSession(Session(engine))
    try:
        result = await service.broadcast_announcement(
            worker, announcement, expected_version=1
        )
        assert result == {"sent": 0, "failed": 1, "skipped": 0}
        current = await session.get(NotificationDelivery, 1)
        assert current.status == DeliveryStatus.FAILED.value
        assert current.claim_token is None
        assert current.claim_until is None
    finally:
        worker._session.close()


@pytest.mark.asyncio
async def test_long_provider_call_heartbeats_lease(delivery_database, monkeypatch):
    session, engine = delivery_database
    announcement, _delivery = await _seed_delivery(session)
    provider = _CountingProvider(wait=0.18)
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"web": provider})
    )
    _settings(monkeypatch)
    monkeypatch.setattr(notification_service, "_DELIVERY_LEASE_SECONDS", 0.06)
    first = _AsyncSQLiteSession(Session(engine))
    second = _AsyncSQLiteSession(Session(engine))
    try:
        first_task = asyncio.create_task(
            service.broadcast_announcement(first, announcement, expected_version=1)
        )
        await asyncio.sleep(0.1)
        second_result = await service.broadcast_announcement(
            second, announcement, expected_version=1
        )
        first_result = await first_task
        assert provider.calls == 1
        assert second_result == {"sent": 0, "failed": 0, "skipped": 1}
        assert first_result == {"sent": 1, "failed": 0, "skipped": 0}
    finally:
        first._session.close()
        second._session.close()
