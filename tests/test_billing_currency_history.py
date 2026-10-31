"""Supported historical monetary codes load without rewriting saved data."""

from types import SimpleNamespace

import httpx
import pytest

from backend.models.payment_models import Plan
from backend.webui import deps
from backend.webui.routes.billing import _pricing_editor_values
from tests.billing_webui_fixtures import pricing_app as pricing_app_fixture
from tests.billing_webui_fixtures import seed_language
from tests.billing_webui_fixtures import sql_runtime as runtime_fixture
from tests.test_billing_credits_api import TEST_PRICE

pricing_app = pricing_app_fixture
sql_runtime = runtime_fixture


def test_price_editor_normalizes_currency_display_without_mutating_snapshot():
    old = {**TEST_PRICE, "currency": " usd ", "settlement_currency": "cny"}
    profile = SimpleNamespace(
        provider_id="p", model_id="m", call_kind="chat", config=old
    )
    values = _pricing_editor_values(profile)
    assert values["currency"] == "USD"
    assert values["settlement_currency"] == "CNY"
    assert old["currency"] == " usd " and old["settlement_currency"] == "cny"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,expected", [("usd", "USD"), (" usd ", "USD"), (" BTC ", "")]
)
async def test_plan_projection_preserves_historical_currency(
    pricing_app, raw, expected
):
    app, factory, _token = pricing_app
    await seed_language(factory)
    async with factory() as db:
        plan = Plan(
            name="Historical plan", plan_type="one_time", price_cents=99, currency=raw
        )
        db.add(plan)
        await db.commit()
        plan_id = plan.id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/billing/admin/plans")
    assert response.status_code == 200, response.text
    item = next(row for row in response.json() if row["id"] == plan_id)
    assert item["currency"] == raw
    assert item["price_cents"] == 99
    if not expected:
        assert item["formatted_price"] is None
    async with factory() as db:
        assert (await db.get(Plan, plan_id)).currency == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["USD", "ZZZ"])
async def test_partial_plan_edit_omitting_currency_preserves_saved_financial_fields(
    pricing_app, raw
):
    app, factory, token = pricing_app
    await seed_language(factory)
    async with factory() as db:
        plan = Plan(
            name="Historical plan", plan_type="one_time", price_cents=1234, currency=raw
        )
        db.add(plan)
        await db.commit()
        plan_id = plan.id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.post(
            f"/billing/admin/plans/{plan_id}/edit",
            data={"csrf_token": token, "name": "Updated description only"},
        )
    assert response.status_code == 302
    assert "error" not in response.headers["location"]
    async with factory() as db:
        unchanged = await db.get(Plan, plan_id)
        assert unchanged.currency == raw
        assert unchanged.price_cents == 1234
        assert unchanged.name == "Updated description only"
