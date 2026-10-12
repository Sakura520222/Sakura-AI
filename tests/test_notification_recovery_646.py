"""Real database regression tests for Issue 646's recovery failure paths."""

import asyncio

import pytest

from backend.models import database as db_module
from backend.models.announcement_models import DeliveryStatus
from backend.models.telegram_models import TelegramUser
from backend.services import notification_service
from tests.test_notification_eligibility_fixes import (
    _CountingProvider,
    _seed_delivery,
    _settings,
    sqlite_database,  # noqa: F401
)


@pytest.mark.asyncio
async def test_transient_eligibility_failure_is_durable_and_recovers(
    sqlite_database, monkeypatch
):
    session, _ = sqlite_database
    announcement, delivery = await _seed_delivery(session)
    provider = _CountingProvider()
    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry({"web": provider})
    )
    _settings(monkeypatch)
    original = service._fresh_row
    failed = False

    async def fail_user_query_once(db, model, row_id):
        nonlocal failed
        if model is TelegramUser and not failed:
            failed = True
            raise ConnectionError("temporary outage")
        return await original(db, model, row_id)

    monkeypatch.setattr(service, "_fresh_row", fail_user_query_once)
    assert (
        await service._deliver_one(
            session, delivery, announcement, None, asyncio.Lock(), 1
        )
        is None
    )
    assert provider.calls == 0
    assert delivery.status == DeliveryStatus.PENDING.value
    assert delivery.next_retry_at is not None
    assert delivery.error_category == "infrastructure"
    assert (
        await service._deliver_one(
            session, delivery, announcement, None, asyncio.Lock(), 1
        )
        is True
    )
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_session_factory_failure_keeps_tuple_contract(
    sqlite_database, monkeypatch
):
    session, _ = sqlite_database
    announcement, delivery = await _seed_delivery(session)

    def broken_factory():
        raise ConnectionError("session unavailable")

    monkeypatch.setattr(db_module, "async_session", broken_factory)
    service = notification_service.NotificationService()
    assert await service._fresh_publication_rows(session, 1, 1) == (None, None, True)
    assert (
        await service._publication_is_current(session, delivery, announcement, 1)
        is False
    )
    assert await service._eligibility_snapshot(session, delivery, None) is None
