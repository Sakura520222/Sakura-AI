"""Pricing persistence and authorization contracts. Browser controls are covered in Billing.test.tsx."""

import httpx
import pytest
from sqlalchemy import select

from backend.models.admin_action_log import AdminActionLog
from backend.models.billing_models import BillingPriceProfile
from backend.services.billing_service import BillingService
from tests.billing_webui_fixtures import pricing_app as pricing_app_fixture
from tests.billing_webui_fixtures import sql_runtime as runtime_fixture
from tests.test_billing_credits_api import TEST_PRICE
from tests.test_billing_credits_api import billing_client as billing_client_fixture

pricing_app = pricing_app_fixture
sql_runtime = runtime_fixture
billing_client = billing_client_fixture


@pytest.mark.asyncio
async def test_structured_prices_publish_immutable_canonical_versions(pricing_app):
    app, factory, _csrf_token = pricing_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/billing/admin/pricing",
            json={
                "provider_id": "openai",
                "model_id": "test-priced-model",
                "call_kind": "chat",
                "config": {
                    **TEST_PRICE,
                    "fx_rate": "7",
                    "markup": "1.25",
                    "input_price": "3",
                    "output_price": "9",
                    "cache_read_supported": False,
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["version"] == 1
        response = await client.post(
            "/api/v1/billing/admin/pricing",
            json={
                "provider_id": "openai",
                "model_id": "test-priced-model",
                "call_kind": "chat",
                "config": {
                    **TEST_PRICE,
                    "fx_rate": "7",
                    "markup": "1.25",
                    "input_price": "4",
                    "output_price": "9",
                    "cache_read_supported": False,
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["version"] == 2
    async with factory() as db:
        prices = (
            (
                await db.execute(
                    select(BillingPriceProfile).order_by(BillingPriceProfile.version)
                )
            )
            .scalars()
            .all()
        )
        # A new publish appends a version; earlier versions stay byte-identical.
        assert [price.version for price in prices] == [1, 2]
        assert prices[0].config["input_price"] == "3"
        assert prices[1].config["input_price"] == "4"
        assert prices[0].config["output_price"] == "9"
        assert prices[0].config["fx_rate"] == "7"
        assert prices[0].config["markup"] == "1.25"
        assert prices[0].config["credits_per_currency_unit"] == "10"
        assert prices[0].config["cache_read_supported"] is False
        assert len((await db.execute(select(AdminActionLog))).scalars().all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("unit", ["requests", "documents", "search_units"])
async def test_non_token_editor_prices_native_meter(pricing_app, unit):
    app, factory, _csrf_token = pricing_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/billing/admin/pricing",
            json={
                "provider_id": "openai",
                "model_id": "native-meter-model",
                "call_kind": "rerank",
                "config": {
                    "currency": "USD",
                    "settlement_currency": "CNY",
                    "fx_rate": "7",
                    "markup": "1.25",
                    "credits_per_currency_unit": "10",
                    "unit": unit,
                    "meter": unit,
                    "unit_price": "0.00000001",
                },
            },
        )
    assert response.status_code == 200, response.text
    async with factory() as db:
        price = (await db.execute(select(BillingPriceProfile))).scalar_one()
        assert price.config["unit"] == unit
        assert price.config["meter"] == unit
        assert price.config["unit_price"] == "1E-8"
        assert "input_price" not in price.config


@pytest.mark.asyncio
async def test_advanced_json_stays_compatible_and_rejects_floats(pricing_app):
    app, factory, _csrf_token = pricing_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        legacy = {
            "provider_id": "openai",
            "model_id": "legacy-api-editor-model",
            "call_kind": "chat",
            "config": TEST_PRICE,
        }
        response = await client.post("/api/v1/billing/admin/pricing", json=legacy)
        assert response.status_code == 200, response.text
        response = await client.post(
            "/api/v1/billing/admin/pricing",
            json={**legacy, "config": {**TEST_PRICE, "fx_rate": 1.1}},
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "invalid_billing_config"
    async with factory() as db:
        assert len((await db.execute(select(BillingPriceProfile))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_loading_a_price_for_editing_preserves_its_published_version(
    billing_client,
):
    client, sessions = billing_client
    async with sessions() as db:
        original = await BillingService(db).publish_price(
            "openai", "existing-test-model", "chat", TEST_PRICE, actor_id=3
        )
        await db.commit()
        profile_id, snapshot = original.id, dict(original.config)

    response = await client.get(
        "/api/v1/billing/admin/pricing", headers={"x-test-identity": "operator"}
    )
    assert response.status_code == 200, response.text
    row = next(item for item in response.json()["items"] if item["id"] == profile_id)
    assert row["model_id"] == "existing-test-model"
    assert row["config"] == snapshot

    async with sessions() as db:
        rows = (
            (
                await db.execute(
                    select(BillingPriceProfile).where(
                        BillingPriceProfile.model_id == "existing-test-model"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [(row.id, row.config) for row in rows] == [(profile_id, snapshot)]
