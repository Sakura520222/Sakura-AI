"""PR 660 compatibility regressions exercise real HTTP and persisted rows."""

from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.api.v1.billing import RefundRequest
from backend.core.config import DYNAMIC_CONFIG_SELECT_OPTIONS, Settings
from backend.models.billing_models import BillingTransaction
from backend.models.payment_models import Order, Plan
from backend.services.config_center_service import ConfigError, validate_field
from backend.services.config_metadata import field_catalog
from backend.services.payment_service import PaymentService
from tests.test_billing_credits_api import billing_client as client_fixture

billing_client = client_fixture


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
    with pytest.raises(ConfigError):
        validate_field(field_catalog()["global/alipay_currency"], "USD")


@pytest.mark.asyncio
async def test_unknown_history_currency_preserves_rows_in_public_api(billing_client):
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

    async with factory() as db:
        assert (await db.get(Plan, plan_id)).currency == "ZZZ"
        assert (await db.get(Order, order_id)).amount_cents == 654
