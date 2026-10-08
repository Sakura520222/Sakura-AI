"""Supported plan currencies are enforced beyond the WebUI dropdown."""

import pytest
from sqlalchemy import select

from backend.models.payment_models import Plan
from backend.services.payment_service import PaymentError, PaymentService
from tests.test_billing_credits_api import billing_client as billing_client_fixture

billing_client = billing_client_fixture


@pytest.mark.asyncio
async def test_unknown_plan_currency_is_rejected_before_persistence(billing_client):
    _, factory = billing_client
    async with factory() as db:
        with pytest.raises(PaymentError) as error:
            await PaymentService(db).create_plan(
                name="Unsupported test plan",
                plan_type="one_time",
                price_cents=100,
                currency="ZZZ",
            )
        assert error.value.code == "invalid_currency"
        assert (await db.execute(select(Plan))).scalars().all() == []


@pytest.mark.asyncio
async def test_invalid_update_keeps_plan_identity_and_amount(billing_client):
    _, factory = billing_client
    async with factory() as db:
        service = PaymentService(db)
        plan = await service.create_plan(
            name="Original", plan_type="one_time", price_cents=123, currency="USD"
        )
        await db.commit()
        with pytest.raises(PaymentError) as error:
            await service.update_plan(
                plan.id, currency="ZZZ", name="Invalid changed name", price_cents=999
            )
        assert error.value.code == "invalid_currency"
        assert (plan.name, plan.price_cents, plan.currency) == ("Original", 123, "USD")
        await db.commit()
    async with factory() as db:
        saved = (await db.execute(select(Plan))).scalar_one()
        assert (saved.name, saved.price_cents, saved.currency) == ("Original", 123, "USD")


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", ["USD", "CNY", "JPY", "USDT", "usd"])
async def test_api_accepts_supported_currency_and_preserves_minor_units(
    billing_client, currency
):
    client, factory = billing_client
    response = await client.post(
        "/api/v1/billing/admin/plans",
        headers={"x-test-identity": "operator"},
        json={
            "name": "Test currency",
            "plan_type": "one_time",
            "price_cents": 123,
            "currency": currency,
            "credit_grant": "5",
        },
    )
    assert response.status_code == 200, response.text
    async with factory() as db:
        plan = (await db.execute(select(Plan))).scalar_one()
        assert plan.currency == currency.upper()
        assert plan.price_cents == 123


@pytest.mark.asyncio
async def test_api_cannot_bypass_plan_currency_selector(billing_client):
    client, factory = billing_client
    body = {
        "name": "Invalid test currency",
        "plan_type": "one_time",
        "price_cents": 123,
        "currency": "ZZZ",
    }
    response = await client.post(
        "/api/v1/billing/admin/plans",
        headers={"x-test-identity": "operator"},
        json=body,
    )
    assert response.status_code == 400, response.text
    async with factory() as db:
        assert (await db.execute(select(Plan))).scalars().all() == []
