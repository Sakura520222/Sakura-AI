"""PR 660 compatibility regressions exercise real HTTP and persisted rows."""

import re
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.api.v1.billing import RefundRequest
from backend.core.config import DYNAMIC_CONFIG_SELECT_OPTIONS, Settings
from backend.models.billing_models import BillingTransaction
from backend.models.payment_models import Order, Plan
from backend.services.payment_service import PaymentService
from backend.webui.auth import WEBUI_TOKEN_COOKIE_NAME
from backend.webui.routes.config import (
    _DynamicConfigValidationError,
    _validate_dynamic_config_value,
)
from tests.test_billing_credits_api import billing_client as client_fixture
from tests.test_billing_pricing_editor import pricing_app as app_fixture
from tests.test_billing_usage_attribution import sql_runtime as sql_fixture

billing_client = client_fixture
pricing_app = app_fixture
sql_runtime = sql_fixture


async def seed_plan(factory, *, concurrency_limit=3):
    async with factory() as db:
        plan = await PaymentService(db).create_plan(
            "Compat",
            "one_time",
            500,
            credit_grant="10",
            concurrency_limit=concurrency_limit,
        )
        await db.commit()
        return plan.id


@pytest.mark.asyncio
async def test_legacy_grant_body_generates_distinct_returned_keys(billing_client):
    client, factory = billing_client
    plan_id = await seed_plan(factory)
    payload = {"user_id": 1, "plan_id": plan_id}
    responses = [
        await client.post(
            "/api/v1/billing/admin/grant",
            json=payload,
            headers={"x-test-identity": "operator"},
        )
        for _ in range(2)
    ]
    assert [r.status_code for r in responses] == [200, 200]
    keys = [r.json()["idempotency_key"] for r in responses]
    assert len(set(keys)) == 2
    async with factory() as db:
        orders = (await db.execute(select(Order))).scalars().all()
        assert {o.grant_idempotency_key for o in orders} == set(keys)
        grants = (
            (
                await db.execute(
                    select(BillingTransaction).where(BillingTransaction.kind == "grant")
                )
            )
            .scalars()
            .all()
        )
        assert len(grants) == 2
        assert sum(t.delta_units for t in grants) == 20_000_000


@pytest.mark.asyncio
async def test_grant_header_is_replayable_and_conflicts_are_rejected(billing_client):
    client, factory = billing_client
    plan_id = await seed_plan(factory)
    payload = {"user_id": 1, "plan_id": plan_id}
    headers = {"x-test-identity": "operator", "Idempotency-Key": "compat-header"}
    first = await client.post(
        "/api/v1/billing/admin/grant", json=payload, headers=headers
    )
    second = await client.post(
        "/api/v1/billing/admin/grant", json=payload, headers=headers
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["idempotency_key"] == "compat-header"
    conflict = await client.post(
        "/api/v1/billing/admin/grant",
        json={**payload, "idempotency_key": "different-body-key"},
        headers=headers,
    )
    assert conflict.status_code == 400
    forbidden = await client.post("/api/v1/billing/admin/grant", json=payload)
    assert forbidden.status_code == 403
    async with factory() as db:
        assert len((await db.execute(select(Order))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_web_legacy_grant_returns_key_for_header_replay(billing_client):
    client, factory = billing_client
    plan_id = await seed_plan(factory)
    headers = {"x-test-identity": "operator"}
    page = await client.get("/billing/admin/pricing", headers=headers)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    payload = {"user_id": 1, "plan_id": plan_id, "csrf_token": token}
    first = await client.post("/billing/admin/grant", data=payload, headers=headers)
    assert first.status_code == 302
    key = first.headers["Idempotency-Key"]
    replay = await client.post(
        "/billing/admin/grant",
        data=payload,
        headers={
            **headers,
            "Idempotency-Key": key,
        },
    )
    assert replay.status_code == 302 and replay.headers["Idempotency-Key"] == key
    async with factory() as db:
        orders = (await db.execute(select(Order))).scalars().all()
        assert len(orders) == 1 and orders[0].grant_idempotency_key == key
        assert (
            len(
                (
                    await db.execute(
                        select(BillingTransaction).where(
                            BillingTransaction.kind == "grant"
                        )
                    )
                )
                .scalars()
                .all()
            )
            == 1
        )


@pytest.mark.asyncio
async def test_api_explicit_null_clears_limit_omission_preserves_it(billing_client):
    client, factory = billing_client
    plan_id = await seed_plan(factory)
    url = f"/api/v1/billing/admin/plans/{plan_id}"
    headers = {"x-test-identity": "operator"}
    assert (
        await client.put(url, json={"name": "Rename"}, headers=headers)
    ).status_code == 200
    async with factory() as db:
        assert (await db.get(Plan, plan_id)).concurrency_limit == 3
    assert (
        await client.put(url, json={"concurrency_limit": None}, headers=headers)
    ).status_code == 200
    async with factory() as db:
        assert (await db.get(Plan, plan_id)).concurrency_limit is None


@pytest.mark.asyncio
async def test_web_blank_limit_clears_omission_preserves(pricing_app):
    app, factory, token = pricing_app
    plan_id = await seed_plan(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set(WEBUI_TOKEN_COOKIE_NAME, "isolated")
        client.cookies.set("csrf_token", token)
        url = f"/billing/admin/plans/{plan_id}/edit"
        assert (
            await client.post(url, data={"csrf_token": token, "name": "Rename"})
        ).status_code == 302
        async with factory() as db:
            assert (await db.get(Plan, plan_id)).concurrency_limit == 3
        assert (
            await client.post(url, data={"csrf_token": token, "concurrency_limit": ""})
        ).status_code == 302
        async with factory() as db:
            assert (await db.get(Plan, plan_id)).concurrency_limit is None


def test_refund_key_schema_matches_service_boundary():
    assert RefundRequest(idempotency_key="x" * 160).idempotency_key == "x" * 160
    with pytest.raises(ValidationError):
        RefundRequest(idempotency_key="x" * 161)
    assert RefundRequest().idempotency_key is None


def test_alipay_currency_is_cny_only_in_settings_and_forms():
    assert DYNAMIC_CONFIG_SELECT_OPTIONS["alipay_currency"] == [
        {"value": "CNY", "label": "CNY"}
    ]
    assert Settings(alipay_currency="cny").alipay_currency == "CNY"
    with pytest.raises(ValidationError):
        Settings(alipay_currency="USD")
    with pytest.raises(_DynamicConfigValidationError):
        _validate_dynamic_config_value(
            "alipay_currency",
            "USD",
            expected_type=str,
            ranges={},
            select_options=DYNAMIC_CONFIG_SELECT_OPTIONS,
        )


@pytest.mark.asyncio
async def test_unknown_history_currency_preserves_rows_in_public_api_and_ui(
    billing_client, monkeypatch
):
    client, factory = billing_client
    async with factory() as db:
        plan = Plan(
            name="Unknown legacy",
            plan_type="one_time",
            price_cents=321,
            currency="ZZZ",
            credit_grant=Decimal(0),
        )
        db.add(plan)
        await db.flush()
        order = Order(
            order_no="LEGACY-CURRENCY",
            plan_id=plan.id,
            user_id=1,
            amount_cents=654,
            refunded_amount_cents=7,
            currency="ZZZ",
            status="paid",
            payment_provider="manual",
        )
        db.add(order)
        await db.commit()
        plan_id, order_id = plan.id, order.id
    plans = await client.get("/api/v1/billing/plans")
    assert plans.status_code == 200
    assert plans.json()[0]["formatted_price"] is None
    assert plans.json()[0]["currency_supported"] is False
    orders = await client.get("/api/v1/billing/orders")
    assert orders.status_code == 200
    item = orders.json()["orders"][0]
    assert (
        item["formatted_amount"] is None and item["formatted_refunded_amount"] is None
    )
    assert item["currency"] == "ZZZ" and item["amount_cents"] == 654
    detail = await client.get(f"/api/v1/billing/orders/{order_id}")
    assert detail.status_code == 200 and detail.json()["currency_supported"] is False

    async def no_providers():
        return []

    monkeypatch.setattr(
        "backend.services.payment.gateway_factory.get_configured_providers",
        no_providers,
    )
    public = await client.get("/billing/")
    assert public.status_code == 200
    assert (
        "Unknown legacy" in public.text
        and "321" in public.text
        and "654" in public.text
    )
    assert "unknown currency unit" in public.text.lower()
    async with factory() as db:
        assert (await db.get(Plan, plan_id)).currency == "ZZZ"
        assert (await db.get(Order, order_id)).amount_cents == 654
