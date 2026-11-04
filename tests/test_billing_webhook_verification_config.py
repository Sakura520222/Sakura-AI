"""Local native signatures, real HTTP and SQL: unverified callbacks are not ACKed."""

import hashlib
import hmac
import json

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from backend.api import webhook
from backend.core.time_service import now_utc
from backend.models.billing_models import BillingTransaction
from backend.models.legacy_entitlement_models import PaymentRefundInboxEvent
from backend.models.payment_models import Order
from backend.services.billing_service import BillingService
from backend.services.payment.paddle_gateway import PaddleGateway
from backend.services.payment.stripe_gateway import StripeGateway
from backend.services.payment_service import PaymentService
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture

sql_runtime = runtime_fixture
SECRET = "TEST-local-verification-secret"


def signed_payload(provider, *, event_type=None, payload=None):
    if payload is None:
        if provider == "paddle":
            data = {
                "event_id": "TEST-paddle-event",
                "event_type": event_type or "transaction.completed",
                "data": {
                    "id": "TEST-paddle-transaction",
                    "currency_code": "USD",
                    "custom_data": {"order_no": "TEST-verification-order"},
                    "details": {"totals": {"total": "100"}},
                },
            }
        else:
            data = {
                "id": "TEST-stripe-event",
                "type": event_type or "checkout.session.completed",
                "data": {
                    "object": {
                        "id": "TEST-stripe-session",
                        "amount_total": 100,
                        "currency": "usd",
                        "payment_status": "paid",
                        "metadata": {"order_no": "TEST-verification-order"},
                    }
                },
            }
        data["internal_test_marker"] = "TEST-PRIVATE-PAYLOAD"
        payload = json.dumps(data).encode()
    timestamp = str(int(now_utc().timestamp()))
    delimiter = b":" if provider == "paddle" else b"."
    signature = hmac.new(
        SECRET.encode(), timestamp.encode() + delimiter + payload, hashlib.sha256
    ).hexdigest()
    header = (
        {"paddle-signature": f"ts={timestamp};h1={signature}"}
        if provider == "paddle"
        else {"stripe-signature": f"t={timestamp},v1={signature}"}
    )
    return payload, header


def webhook_app(factory, monkeypatch, provider, secret):
    gateway = (
        PaddleGateway("TEST-local-key", secret)
        if provider == "paddle"
        else StripeGateway("TEST-local-key", secret)
    )

    async def get_gateway(name):
        assert name == provider
        return gateway

    monkeypatch.setattr("backend.services.payment.get_gateway", get_gateway)
    monkeypatch.setattr(webhook, "get_async_session", factory)
    app = FastAPI()
    app.include_router(webhook.router)
    return app, gateway


async def seed_order(factory, provider):
    async with factory() as db:
        service = PaymentService(db)
        plan = await service.create_plan(
            "TEST verification credits",
            "one_time",
            100,
            currency="USD",
            credit_grant="10",
        )
        # Manual construction avoids the external checkout-creation boundary.
        order = await service.create_order(1, plan.id)
        order.payment_provider = provider
        order.order_no = "TEST-verification-order"
        order.metadata_json = json.dumps(
            {"gateway_amount_cents": 100, "gateway_currency": "USD"}
        )
        await db.commit()
        return order.id


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["paddle", "stripe"])
@pytest.mark.parametrize("secret", [None, "", " \t\n"])
async def test_missing_secret_is_retryable_then_same_signed_event_fulfills_once(
    sql_runtime, monkeypatch, provider, secret
):
    factory, _, _ = sql_runtime
    order_id = await seed_order(factory, provider)
    app, gateway = webhook_app(factory, monkeypatch, provider, secret)
    payload, headers = signed_payload(provider)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        failed = await client.post(f"/{provider}", content=payload, headers=headers)
        assert failed.status_code == 503 and failed.json() == {
            "status": "retry_required"
        }
        assert SECRET not in failed.text and "TEST-PRIVATE-PAYLOAD" not in failed.text
        async with factory() as db:
            assert (await db.get(Order, order_id)).status == "pending"
            assert (
                await db.execute(select(PaymentRefundInboxEvent))
            ).scalars().all() == []
            assert (await db.execute(select(BillingTransaction))).scalars().all() == []
        gateway._webhook_secret = SECRET
        accepted = await client.post(f"/{provider}", content=payload, headers=headers)
        replay = await client.post(f"/{provider}", content=payload, headers=headers)
        assert accepted.status_code == replay.status_code == 200
        assert accepted.json()["status"] == replay.json()["status"] == "processed"
    async with factory() as db:
        assert (await db.get(Order, order_id)).status == "fulfilled"
        assert (
            len((await db.execute(select(PaymentRefundInboxEvent))).scalars().all())
            == 1
        )
        assert len((await db.execute(select(BillingTransaction))).scalars().all()) == 1
        assert (await BillingService(db).get_wallet(1)).balance_units == 10_000_000
        assert (await BillingService(db).reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["paddle", "stripe"])
@pytest.mark.parametrize(
    "case", ["missing_signature", "bad_signature", "invalid_json", "unsupported"]
)
async def test_verification_failure_is_distinct_from_verified_unsupported_event(
    sql_runtime, monkeypatch, provider, case
):
    factory, _, _ = sql_runtime
    app, _ = webhook_app(factory, monkeypatch, provider, SECRET)
    payload, headers = signed_payload(
        provider,
        event_type="unsupported.test",
        payload=b"not-json" if case == "invalid_json" else None,
    )
    if case == "missing_signature":
        headers = {}
    elif case == "bad_signature":
        key = "paddle-signature" if provider == "paddle" else "stripe-signature"
        headers[key] = "INVALID"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/{provider}", content=payload, headers=headers)
    assert response.status_code == (200 if case == "unsupported" else 400)
    assert response.json() == {
        "status": "ignored" if case == "unsupported" else "verification_failed"
    }
    async with factory() as db:
        assert (await db.execute(select(PaymentRefundInboxEvent))).scalars().all() == []
        assert (await db.execute(select(BillingTransaction))).scalars().all() == []
