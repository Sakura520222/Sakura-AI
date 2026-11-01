"""Unrepresentable gateway money must never leave an intent or Credits hold."""

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.models.billing_models import (
    BillingReservationEvent,
    BillingTransaction,
    BillingWallet,
)
from backend.models.legacy_entitlement_models import (
    PaymentRefundAttempt,
    PaymentRefundAttemptEvent,
)
from backend.services.billing_service import BillingService
from backend.services.payment.gateway_base import RefundResult
from backend.services.payment_service import PaymentError, PaymentService
from tests.test_billing_entitlements import db as database_fixture
from tests.test_billing_payment_events import paid_order

db = database_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [0, 1, 49, 50])
async def test_zero_gateway_delta_rejected_before_intent_and_hold(
    db, monkeypatch, requested
):
    user, order = await paid_order(db, f"TEST-rounding-{requested}")
    order.metadata_json = json.dumps(
        {"gateway_amount_cents": 1, "gateway_currency": "USD"}
    )
    await db.commit()
    monkeypatch.setattr(
        "backend.services.payment_service.get_dynamic_config",
        AsyncMock(return_value="proportional_unused_credits"),
    )
    gateway = AsyncMock()
    gateway.refund.return_value = RefundResult(success=False, status="failed")
    factory = AsyncMock(return_value=gateway)
    monkeypatch.setattr("backend.services.payment.get_gateway", factory)
    service = PaymentService(db)
    for _ in range(2):
        with pytest.raises(PaymentError) as rejected:
            await service.process_refund(
                order.id, requested, user.id, idempotency_key="TEST-small-refund"
            )
        assert rejected.value.code == "refund_amount_unrepresentable"
        # Even an approval handler committing its refusal must not freeze funds.
        await db.commit()
        for model in (
            PaymentRefundAttempt,
            PaymentRefundAttemptEvent,
            BillingReservationEvent,
        ):
            assert (await db.execute(select(model))).scalars().all() == []
        wallet = await db.get(BillingWallet, user.id)
        assert (wallet.balance_units, wallet.reserved_units) == (5_000_000, 0)
        assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1
        assert (await BillingService(db).reconcile_wallet(user.id))["consistent"]
    gateway.refund.assert_not_awaited()
    factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_representable_cumulative_refunds_round_once_and_replay_without_io(
    db, monkeypatch
):
    user, order = await paid_order(db, "TEST-representable-rounding")
    order.metadata_json = json.dumps(
        {"gateway_amount_cents": 3, "gateway_currency": "USD"}
    )
    await db.commit()
    monkeypatch.setattr(
        "backend.services.payment_service.get_dynamic_config",
        AsyncMock(return_value="proportional_unused_credits"),
    )
    gateway = AsyncMock()

    async def refund(**kwargs):
        assert not db._session.in_transaction()
        assert kwargs["amount_cents"] == 1
        return RefundResult(
            success=True,
            status="succeeded",
            refund_id=kwargs["idempotency_key"],
            amount_cents=1,
        )

    gateway.refund.side_effect = refund
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    service = PaymentService(db)
    for index, amount in enumerate((25, 25, 50)):
        key = f"TEST-representable-{index}"
        await service.process_refund(order.id, amount, user.id, idempotency_key=key)
        await db.commit()
        await service.process_refund(order.id, amount, user.id, idempotency_key=key)
        await db.commit()
        assert gateway.refund.await_count == index + 1
        assert (await BillingService(db).reconcile_wallet(user.id))["consistent"]
    attempts = (await db.execute(select(PaymentRefundAttempt))).scalars().all()
    assert sum(row.gateway_amount_cents for row in attempts) == 3
    assert all(
        row.status == "succeeded" and row.gateway_currency == "USD" for row in attempts
    )
    wallet = await db.get(BillingWallet, user.id)
    assert (wallet.balance_units, wallet.reserved_units) == (0, 0)
    assert order.status == "refunded" and order.refunded_amount_cents == 100


@pytest.mark.asyncio
async def test_full_refund_of_one_gateway_unit_still_succeeds(db, monkeypatch):
    user, order = await paid_order(db, "TEST-full-minimum-unit")
    order.metadata_json = json.dumps(
        {"gateway_amount_cents": 1, "gateway_currency": "USD"}
    )
    await db.commit()
    gateway = AsyncMock()
    gateway.refund.return_value = RefundResult(
        success=True, status="succeeded", refund_id="TEST-one-unit", amount_cents=1
    )
    monkeypatch.setattr(
        "backend.services.payment.get_gateway", AsyncMock(return_value=gateway)
    )
    await PaymentService(db).process_refund(order.id, operator_id=user.id)
    await db.commit()
    assert gateway.refund.call_args.kwargs["amount_cents"] == 1
    assert order.status == "refunded"
    assert (await db.get(BillingWallet, user.id)).balance_units == 0
    assert (await BillingService(db).reconcile_wallet(user.id))["consistent"]


@pytest.mark.asyncio
async def test_zero_cash_manual_grant_reversal_needs_no_gateway(db, monkeypatch):
    from backend.models.telegram_models import TelegramUser

    user, purchased = await paid_order(db, "TEST-zero-manual-grant")
    operator = await db.get(TelegramUser, user.id)
    order = await PaymentService(db).grant_plan_to_user(
        user.id, purchased.plan_id, operator.id, idempotency_key="TEST-manual-grant"
    )
    await db.commit()
    assert order.amount_cents == 0 and order.payment_provider == "manual"
    factory = AsyncMock(side_effect=AssertionError("Manual grant must not send money"))
    monkeypatch.setattr("backend.services.payment.get_gateway", factory)
    await PaymentService(db).process_refund(order.id, operator_id=operator.id)
    await db.commit()
    assert order.status == "refunded"
    assert (await db.get(BillingWallet, user.id)).balance_units == 5_000_000
    assert (await BillingService(db).reconcile_wallet(user.id))["consistent"]
    factory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "zh-CN"])
async def test_api_refusal_preserves_string_detail_and_saved_language(
    db, monkeypatch, language
):
    import httpx
    from fastapi import FastAPI

    from backend.api.v1.billing import router
    from backend.api.v1.deps import require_api_super_admin
    from backend.models.database import WebUIConfig
    from backend.webui.deps import get_db, require_payment_enabled
    from backend.webui.i18n import i18n

    user, order = await paid_order(db, f"TEST-refund-locale-{language}")
    order.metadata_json = json.dumps(
        {"gateway_amount_cents": 1, "gateway_currency": "USD"}
    )
    db.add(WebUIConfig(user_id=user.id, language=language))
    await db.commit()
    monkeypatch.setattr(
        "backend.services.payment_service.get_dynamic_config",
        AsyncMock(return_value="proportional_unused_credits"),
    )
    factory = AsyncMock(
        side_effect=AssertionError("Preflight must reject before gateway lookup")
    )
    monkeypatch.setattr("backend.services.payment.get_gateway", factory)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_api_super_admin] = lambda: {
        "user_id": user.id,
        "role": "super_admin",
    }
    app.dependency_overrides[require_payment_enabled] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/billing/orders/{order.id}/refund",
            json={"amount_cents": 1, "idempotency_key": "TEST-zero-locale"},
        )
    assert response.status_code == 400
    assert response.headers["X-Error-Code"] == "refund_amount_unrepresentable"
    assert response.json()["detail"] == i18n.t(
        "billing.refund_amount_unrepresentable", lang=language
    )
    assert (await db.get(BillingWallet, user.id)).reserved_units == 0
    assert (await db.execute(select(PaymentRefundAttempt))).scalars().all() == []
    factory.assert_not_awaited()
