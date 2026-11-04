"""Low-balance alerts share the financial transaction and existing private routing."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from backend.models.announcement_models import NotificationDelivery
from backend.models.billing_models import (
    BillingNotice,
    BillingTransaction,
    BillingWallet,
)
from backend.models.identity_models import NotificationEndpoint
from backend.models.notification_models import (
    NotificationEvent,
    NotificationSubscription,
    UserNotification,
)
from backend.services.billing_service import BillingService
from tests.test_notification_events_646 import AsyncDB, Provider, service
from tests.test_notification_events_646 import event_db as real_event_fixture

event_db = real_event_fixture


async def fund(db):
    billing = BillingService(db)
    await billing.set_low_balance_threshold(1, "5")
    await billing.grant(1, "10", "balance:topup")
    await db.commit()
    return billing


@pytest.mark.asyncio
async def test_low_balance_alert_is_private_routable_and_not_a_second_inbox(event_db):
    engine, _ = event_db
    with Session(engine) as db:
        db.add_all(
            [
                NotificationEndpoint(
                    id=3,
                    user_id=1,
                    provider="webhook",
                    address="https://example.org/notify",
                    enabled=True,
                    verified=True,
                ),
                NotificationEndpoint(
                    id=4,
                    user_id=1,
                    provider="email",
                    address="unverified@example.org",
                    enabled=True,
                    verified=False,
                ),
                NotificationEndpoint(
                    id=5,
                    user_id=1,
                    provider="telegram",
                    address="123",
                    enabled=True,
                    verified=True,
                ),
                NotificationEndpoint(
                    id=6,
                    user_id=1,
                    provider="email",
                    address="disabled@example.org",
                    enabled=False,
                    verified=True,
                ),
            ]
        )
        db.add_all(
            [
                NotificationSubscription(
                    user_id=1, event_type="billing.*", endpoint_id=target
                )
                for target in (1, 3, 4, 5, 6, None)
            ]
        )
        db.add(
            NotificationSubscription(
                user_id=2, event_type="billing.*", endpoint_id=None
            )
        )
        db.commit()
    email, telegram = Provider(), Provider()
    telegram.channel = "telegram"
    dispatcher = service(email)
    dispatcher.registry.register(telegram)
    webhook = Provider()
    webhook.channel = "webhook"
    dispatcher.registry.register(webhook)
    async with AsyncDB(engine) as db:
        billing = await fund(db)
        debit = await billing.adjust(
            1, Decimal(-7), idempotency_key="balance:debit", actor_id=1, reason="TEST"
        )
        await db.commit()
        again = await billing.adjust(
            1, Decimal(-7), idempotency_key="balance:debit", actor_id=1, reason="TEST"
        )
        assert again.id == debit.id
        await db.commit()
        event = (await db.execute(select(NotificationEvent))).scalar_one()
        assert event.event_type == "billing.low_balance" and event.sensitive
        assert event.resource_type == "user" and event.resource_id == "1"
        assert event.detail_url == "/billing/credits"
        assert event.run_key == f"transaction:{debit.id}"
        assert (await db.execute(select(BillingNotice))).scalars().all() == []
        # A historical producer forgetting the flag cannot make financial
        # alerts eligible for a verified but shared Webhook destination.
        await db.execute(
            update(NotificationEvent)
            .where(NotificationEvent.id == event.id)
            .values(sensitive=False)
        )
        await db.commit()
        await dispatcher.materialize_event(db, event.id)
        inbox = (await db.execute(select(UserNotification))).scalars().all()
        deliveries = (await db.execute(select(NotificationDelivery))).scalars().all()
        assert {item.user_id for item in inbox} == {1}
        assert {item.channel for item in deliveries} == {"web", "email", "telegram"}
        assert {item.endpoint_id for item in deliveries if item.channel != "web"} == {
            1,
            5,
        }
        await billing.adjust(
            1,
            Decimal(-1),
            idempotency_key="balance:still-low",
            actor_id=1,
            reason="TEST",
        )
        await db.commit()
        assert len((await db.execute(select(NotificationEvent))).scalars().all()) == 1
        await billing.grant(1, "10", "balance:rearm")
        await billing.adjust(
            1, Decimal(-8), idempotency_key="balance:second", actor_id=1, reason="TEST"
        )
        await db.commit()
        assert len((await db.execute(select(NotificationEvent))).scalars().all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["insert", "rollback"])
async def test_low_balance_outbox_rolls_back_with_the_debit(
    event_db, monkeypatch, failure
):
    engine, _ = event_db
    async with AsyncDB(engine) as db:
        billing = await fund(db)
        if failure == "insert":
            monkeypatch.setattr(
                "backend.services.notification_events.emit_event",
                AsyncMock(side_effect=RuntimeError("TEST outbox failure")),
            )
            with pytest.raises(RuntimeError, match="outbox failure"):
                await billing.adjust(
                    1,
                    Decimal(-7),
                    idempotency_key="balance:failure",
                    actor_id=1,
                    reason="TEST",
                )
            await (
                db.commit()
            )  # Catching the error cannot retain a debit-only transition.
        else:
            # SQLite's legacy driver mode otherwise releases a root SAVEPOINT
            # as a commit; exercise a real caller-owned outer SQL transaction.
            await db.execute(text("BEGIN"))
            await billing.adjust(
                1,
                Decimal(-7),
                idempotency_key="balance:failure",
                actor_id=1,
                reason="TEST",
            )
            await db.rollback()
        wallet = await db.get(BillingWallet, 1, populate_existing=True)
        assert wallet.balance_units == 10_000_000 and not wallet.low_balance_notified
        assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1
        assert (await db.execute(select(NotificationEvent))).scalars().all() == []
