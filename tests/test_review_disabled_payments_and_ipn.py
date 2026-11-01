"""Disabling new purchases must not disable refund recovery or lose callbacks."""

import hashlib
import hmac
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from backend.api import webhook
from backend.models.legacy_entitlement_models import PaymentRefundInboxEvent
from backend.models.payment_models import Order, RefundRequest
from backend.services.payment.nowpayments_gateway import NowPaymentsGateway
from backend.services.payment_service import PaymentService
from backend.webui import deps
from tests.test_billing_credits_api import billing_client as client_fixture

billing_client = client_fixture


async def disable_new_payments(client, monkeypatch):
    async def disabled():
        return False

    async def no_providers():
        return []

    client._transport.app.dependency_overrides.pop(deps.require_payment_enabled)
    monkeypatch.setattr(deps, "is_payment_enabled", disabled)
    monkeypatch.setattr(
        "backend.services.payment.gateway_factory.get_configured_providers",
        no_providers,
    )


async def seed_paid_orders(factory):
    async with factory() as db:
        service = PaymentService(db)
        plan = await service.create_plan("TEST paid legacy", "one_time", 500)
        ids = []
        for index in range(4):
            order = Order(
                order_no=f"TEST-DISABLED-{index}",
                user_id=1,
                plan_id=plan.id,
                plan_snapshot=service._snapshot_plan(plan),
                amount_cents=500,
                currency="CNY",
                status="fulfilled",
                payment_provider="manual",
            )
            db.add(order)
            await db.flush()
            ids.append(order.id)
        await db.commit()
        return plan.id, ids


@pytest.mark.asyncio
async def test_disabled_purchase_switch_keeps_own_history_and_blocks_new_orders(
    billing_client, monkeypatch
):
    client, factory = billing_client
    plan_id, order_ids = await seed_paid_orders(factory)
    await disable_new_payments(client, monkeypatch)
    assert (await client.get("/api/v1/billing/orders")).status_code == 200
    own = await client.get(f"/api/v1/billing/orders/{order_ids[0]}")
    assert own.status_code == 200 and own.json()["order_no"] == "TEST-DISABLED-0"
    assert (
        await client.get(
            f"/api/v1/billing/orders/{order_ids[0]}", headers={"x-test-identity": "bob"}
        )
    ).status_code == 404
    assert (
        await client.get("/api/v1/billing/orders", headers={"x-test-identity": "bob"})
    ).json()["total"] == 0
    assert (
        await client.post("/api/v1/billing/orders", json={"plan_id": plan_id})
    ).status_code == 404
    async with factory() as db:
        assert len((await db.execute(select(Order))).scalars().all()) == 4


@pytest.mark.asyncio
async def test_disabled_sales_keep_existing_tron_invoice_and_settle_once(
    billing_client, monkeypatch
):
    from decimal import Decimal
    from unittest.mock import AsyncMock

    from backend.models.billing_models import BillingTransaction, BillingWallet
    from backend.services.payment.gateway_base import PaymentStatusResult
    from backend.services.payment.tron_gateway import TronGateway

    client, factory = billing_client
    async with factory() as db:
        service = PaymentService(db)
        plan = await service.create_plan(
            "TEST TRON invoice", "one_time", 100, credit_grant=Decimal(5)
        )
        order = await service.create_order(1, plan.id)
        order.payment_provider = "tron"
        metadata = {
            "pay_amount": "1.000123",
            "pay_address": "TEST-tron-address",
            "pay_currency": "usdttrc20",
            "price_amount": "1",
            "price_currency": "usd",
            "gateway_amount_cents": 100,
            "gateway_currency": "USD",
        }
        order.metadata_json = json.dumps(metadata)
        fingerprint = PaymentService._tron_invoice_identity(metadata)
        order.invoice_identity = fingerprint
        order_no, order_id = order.order_no, order.id
        before = (await db.get(BillingWallet, 1)).balance_units
        await db.commit()

    gateway = TronGateway("TEST-tron-address")
    gateway.check_payment_by_amount = AsyncMock(
        return_value=PaymentStatusResult(
            success=True, status="completed", provider_tx_id="TEST-chain-transfer"
        )
    )
    gateway.create_payment = AsyncMock(side_effect=AssertionError("No new invoice"))
    gateway_factory = AsyncMock(return_value=gateway)
    monkeypatch.setattr("backend.services.payment.get_gateway", gateway_factory)
    await disable_new_payments(client, monkeypatch)

    invoice = await client.get(f"/api/v1/billing/invoice/{order_no}")
    assert invoice.status_code == 200
    assert invoice.json()["payment"]["pay_amount"] == "1.000123"
    assert invoice.json()["payment"]["pay_address"] == "TEST-tron-address"
    wrong_owner = {"x-test-identity": "bob"}
    assert (
        await client.get(f"/api/v1/billing/invoice/{order_no}", headers=wrong_owner)
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/billing/invoice-status",
            params={"order_no": order_no},
            headers=wrong_owner,
        )
    ).status_code == 404
    gateway_factory.assert_not_awaited()
    for _ in range(2):
        status = await client.post(
            "/api/v1/billing/invoice-status", params={"order_no": order_no}
        )
        assert status.status_code == 200 and status.json()["status"] == "completed"
    gateway.check_payment_by_amount.assert_awaited_once()
    args = gateway.check_payment_by_amount.call_args
    assert args.args == (order_no, Decimal("1.000123"))
    assert args.kwargs["min_block_timestamp"] > 0
    gateway.create_payment.assert_not_awaited()
    assert (
        await client.post("/api/v1/billing/orders", json={"plan_id": plan.id})
    ).status_code == 404
    async with factory() as db:
        order = await db.get(Order, order_id)
        assert order.status == "fulfilled" and order.invoice_identity == fingerprint
        assert json.loads(order.metadata_json) == metadata
        assert (await db.get(BillingWallet, 1)).balance_units == before + 5_000_000
        purchases = (
            (
                await db.execute(
                    select(BillingTransaction).where(
                        BillingTransaction.order_id == order_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(purchases) == 1 and purchases[0].kind == "purchase"


@pytest.mark.parametrize(
    ("path", "method", "allowed"),
    [
        ("/billing/crypto-status", "GET", True),
        ("/billing/crypto-payment/ORDexisting", "GET", True),
        ("/billing/crypto-status", "POST", False),
        ("/billing/crypto-payment/ORDexisting", "POST", False),
        ("/billing/crypto-payment/ORDexisting/anything", "GET", False),
        ("/billing/purchase/1", "POST", False),
    ],
)
def test_existing_checkout_allowlist_is_method_and_route_specific(
    path, method, allowed
):
    assert deps._is_existing_order_request(path, method) is allowed


@pytest.mark.asyncio
async def test_disabled_payments_preserve_refund_review_auth_and_processing(
    billing_client, monkeypatch
):
    client, factory = billing_client
    _, order_ids = await seed_paid_orders(factory)
    await disable_new_payments(client, monkeypatch)
    admin_headers = {"x-test-identity": "operator"}
    foreign = await client.post(
        f"/api/v1/billing/orders/{order_ids[0]}/refund-request",
        headers={"x-test-identity": "bob"},
        json={"reason": "Unauthorized"},
    )
    assert foreign.status_code == 400
    async with factory() as db:
        assert (await db.execute(select(RefundRequest))).scalars().all() == []
    for order_id in order_ids[:2]:
        submitted = await client.post(
            f"/api/v1/billing/orders/{order_id}/refund-request",
            json={"reason": "TEST request"},
        )
        assert submitted.status_code == 200
    async with factory() as db:
        requests = (
            (await db.execute(select(RefundRequest).order_by(RefundRequest.id)))
            .scalars()
            .all()
        )
        assert len(requests) == 2 and all(r.status == "pending" for r in requests)
        request_ids = [r.id for r in requests]
    listing = await client.get(
        "/api/v1/billing/admin/refund-requests", headers=admin_headers
    )
    assert listing.status_code == 200 and listing.json()["total"] == 2
    assert (
        await client.get("/api/v1/billing/admin/refund-requests")
    ).status_code == 403
    assert (
        await client.post(
            f"/api/v1/billing/admin/refund-requests/{request_ids[0]}/approve",
            json={"review_note": ""},
        )
    ).status_code == 403
    approved = await client.post(
        f"/api/v1/billing/admin/refund-requests/{request_ids[0]}/approve",
        headers=admin_headers,
        json={"review_note": "TEST"},
    )
    rejected = await client.post(
        f"/api/v1/billing/admin/refund-requests/{request_ids[1]}/reject",
        headers=admin_headers,
        json={"review_note": "TEST"},
    )
    assert approved.status_code == rejected.status_code == 200
    assert (
        await client.post(f"/api/v1/billing/orders/{order_ids[2]}/refund", json={})
    ).status_code == 403
    direct = await client.post(
        f"/api/v1/billing/orders/{order_ids[2]}/refund",
        headers=admin_headers,
        json={"idempotency_key": "TEST-disabled-direct"},
    )
    assert direct.status_code == 200
    web_direct = await client.post(
        f"/api/v1/billing/orders/{order_ids[3]}/refund",
        headers=admin_headers,
        json={"idempotency_key": "TEST-disabled-web"},
    )
    assert web_direct.status_code == 200
    async with factory() as db:
        assert (await db.get(RefundRequest, request_ids[0])).status == "approved"
        assert (await db.get(RefundRequest, request_ids[1])).status == "rejected"
        assert (await db.get(Order, order_ids[0])).refunded_amount_cents == 500
        assert (await db.get(Order, order_ids[1])).refunded_amount_cents == 0
        assert (await db.get(Order, order_ids[2])).refunded_amount_cents == 500
        assert (await db.get(Order, order_ids[3])).refunded_amount_cents == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected,status",
    [
        ("missing_secret", 503, "retry_required"),
        ("bad_signature", 400, "verification_failed"),
        ("missing_signature", 400, "verification_failed"),
        ("unsupported", 200, "ignored"),
    ],
)
async def test_nowpayments_http_distinguishes_unverified_from_verified_ignored(
    billing_client, monkeypatch, case, expected, status
):
    _, factory = billing_client
    gateway = NowPaymentsGateway(
        "TEST-api-key", "" if case == "missing_secret" else "TEST-ipn-secret"
    )

    async def get_gateway(provider):
        assert provider == "nowpayments"
        return gateway

    monkeypatch.setattr("backend.services.payment.get_gateway", get_gateway)

    @asynccontextmanager
    async def sessions():
        async with factory() as db:
            yield db

    monkeypatch.setattr(webhook, "get_async_session", sessions)
    data = {
        "payment_id": "TEST-payment",
        "order_id": "TEST-order",
        "payment_status": "waiting" if case == "unsupported" else "finished",
    }
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(b"TEST-ipn-secret", payload, hashlib.sha512).hexdigest()
    headers = (
        {}
        if case == "missing_signature"
        else {"x-nowpayments-sig": "BAD" if case == "bad_signature" else signature}
    )
    app = FastAPI()
    app.include_router(webhook.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/nowpayments", content=payload, headers=headers)
    assert response.status_code == expected and response.json()["status"] == status
    assert "TEST-ipn-secret" not in response.text and "TEST-order" not in response.text
    async with factory() as db:
        assert (await db.execute(select(PaymentRefundInboxEvent))).scalars().all() == []
