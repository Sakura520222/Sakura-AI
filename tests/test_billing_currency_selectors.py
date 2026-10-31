"""Currency choices are shared by billing forms, JSON and configuration."""

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from backend.core.config import (
    DYNAMIC_CONFIG_RANGES,
    DYNAMIC_CONFIG_SELECT_OPTIONS,
    get_dynamic_config_input_type,
)
from backend.models.billing_models import BillingPriceProfile
from backend.models.database import AppConfig
from backend.models.legacy_entitlement_models import PaymentRefundInboxEvent
from backend.models.payment_models import Plan
from backend.services.billing_pricing import (
    PricingPending,
    calculate_price,
    validate_price_config,
)
from backend.services.payment.currency_units import CURRENCY_MINOR_EXPONENTS
from backend.webui import deps
from backend.webui.routes.billing import _pricing_editor_values
from backend.webui.routes.config import (
    _DynamicConfigValidationError,
    _validate_dynamic_config_value,
)
from tests.billing_webui_fixtures import (
    pricing_app as pricing_app_fixture,
)
from tests.billing_webui_fixtures import (
    pricing_fields,
    seed_language,
)
from tests.billing_webui_fixtures import (
    sql_runtime as runtime_fixture,
)
from tests.test_billing_credits_api import TEST_PRICE
from tests.test_billing_credits_api import billing_client as billing_client_fixture

pricing_app = pricing_app_fixture
sql_runtime = runtime_fixture
billing_client = billing_client_fixture


@pytest.mark.parametrize("field", ["currency", "settlement_currency"])
def test_price_json_rejects_arbitrary_currency(field):
    with pytest.raises(ValueError, match=field):
        validate_price_config({**TEST_PRICE, field: "ZZZ"})


def test_historical_unsupported_currency_is_pending_review():
    with pytest.raises(PricingPending, match="requires review"):
        calculate_price(SimpleNamespace(), {**TEST_PRICE, "currency": "ZZZ"})


def test_supported_crypto_currency_and_all_catalog_codes():
    for code in CURRENCY_MINOR_EXPONENTS:
        result = validate_price_config(
            {**TEST_PRICE, "currency": code, "settlement_currency": code}
        )
        assert result["currency"] == code


def test_dynamic_currency_fields_use_shared_catalog():
    for field in (
        "payment_default_currency",
        "stripe_currency",
    ):
        assert get_dynamic_config_input_type(field) == "select"
        codes = [option["value"] for option in DYNAMIC_CONFIG_SELECT_OPTIONS[field]]
        assert codes[:2] == ["USD", "CNY"]
        assert set(codes) == set(CURRENCY_MINOR_EXPONENTS)
    paddle_codes = [
        option["value"] for option in DYNAMIC_CONFIG_SELECT_OPTIONS["paddle_currency"]
    ]
    assert paddle_codes[:2] == ["USD", "CNY"]
    assert {"USD", "CNY", "EUR", "JPY"} <= set(paddle_codes)
    assert "USDT" not in paddle_codes and "AED" not in paddle_codes
    assert DYNAMIC_CONFIG_SELECT_OPTIONS["alipay_currency"] == [
        {"value": "CNY", "label": "CNY"}
    ]


def test_old_stream_price_loads_as_merged_chat_editor():
    profile = SimpleNamespace(
        provider_id="openai",
        model_id="historical",
        call_kind="chat_stream",
        config=TEST_PRICE,
    )
    assert _pricing_editor_values(profile)["call_kind"] == "chat"
    assert profile.call_kind == "chat_stream"


def test_dynamic_currency_validation_rejects_forged_code_and_normalizes_legacy_case():
    kwargs = {
        "expected_type": str,
        "ranges": DYNAMIC_CONFIG_RANGES,
        "select_options": DYNAMIC_CONFIG_SELECT_OPTIONS,
    }
    with pytest.raises(_DynamicConfigValidationError):
        _validate_dynamic_config_value("stripe_currency", "ZZZ", **kwargs)
    assert _validate_dynamic_config_value("stripe_currency", "usd", **kwargs) == "USD"


@pytest.mark.asyncio
async def test_global_currency_render_keeps_unknown_history_and_supported_lowercase(
    pricing_app,
):
    _app, factory, _token = pricing_app
    async with factory() as db:
        db.add(AppConfig(key_name="stripe_currency", key_value="ZZZ"))
        db.add(AppConfig(key_name="paddle_currency", key_value="usd"))
        await db.commit()
        from backend.services.config_center_service import ConfigCenterService

        projection = await ConfigCenterService().describe(
            db,
            {"user_id": 1, "role": "super_admin", "sub": "admin"},
            category="system",
            lang="en",
        )
        fields = {field["id"]: field for field in projection}
        assert fields["global/stripe_currency"]["value"] == "ZZZ"
        assert fields["global/paddle_currency"]["value"] == "USD"
        assert "ZZZ" not in fields["global/stripe_currency"]["constraints"]["enum"]
        assert (
            await db.execute(
                select(AppConfig.key_value).where(
                    AppConfig.key_name == "paddle_currency"
                )
            )
        ).scalar_one() == "usd"


@pytest.mark.asyncio
async def test_editor_apis_share_the_currency_catalog(pricing_app):
    app, _, _ = pricing_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in (
            "/api/v1/billing/context",
            "/api/v1/billing/admin/pricing-context",
        ):
            response = await client.get(path)
            assert response.status_code == 200, response.text
            codes = response.json()["currencies"]
            assert codes[:2] == ["USD", "CNY"]
            assert set(codes) == set(CURRENCY_MINOR_EXPONENTS)
        assert "chat" in response.json()["call_kinds"]
        assert "chat_stream" not in response.json()["call_kinds"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fields", "json"])
async def test_unknown_currency_cannot_be_posted_around_selector(pricing_app, mode):
    app, factory, token = pricing_app
    await seed_language(factory)
    payload = pricing_fields(token, currency="ZZZ")
    if mode == "json":
        payload.update(
            config_mode="json", config=json.dumps({**TEST_PRICE, "currency": "ZZZ"})
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.post(
            "/billing/admin/pricing",
            data=payload,
            headers={"Accept": "application/json"},
        )
    assert response.status_code == 400, response.text
    assert response.json()["ok"] is False
    assert response.json()["errors"][0]["field"] == (
        "currency" if mode == "fields" else "config"
    )
    async with factory() as db:
        assert (await db.execute(select(BillingPriceProfile))).scalars().all() == []


@pytest.mark.asyncio
async def test_pending_payment_evidence_rejects_unknown_currency(pricing_app):
    app, factory, token = pricing_app
    await seed_language(factory)
    async with factory() as db:
        event = PaymentRefundInboxEvent(
            provider="stripe",
            event_key="currency-choice",
            evidence={},
            pending_reason="unknown_order",
        )
        db.add(event)
        await db.commit()
        event_id = event.id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.get("/api/v1/billing/admin/payment-events")
        assert response.status_code == 200, response.text
        assert event_id in [row["id"] for row in response.json()["items"]]
        response = await client.post(
            f"/billing/admin/payment-events/{event_id}/resolve",
            data={
                "csrf_token": token,
                "evidence": "verified",
                "checkout_currency": "ZZZ",
            },
        )
        assert response.status_code == 302
        assert "error" in response.headers["location"]
    async with factory() as db:
        unchanged = await db.get(PaymentRefundInboxEvent, event_id)
        assert unchanged.status == "pending_reconciliation"
        assert unchanged.evidence == {}


@pytest.mark.asyncio
async def test_plan_page_can_show_unsupported_historical_currency_without_rewriting(
    pricing_app,
):
    app, factory, _token = pricing_app
    await seed_language(factory)
    async with factory() as db:
        legacy = Plan(
            name="Legacy monetary code",
            plan_type="one_time",
            price_cents=321,
            currency="ZZZ",
        )
        db.add(legacy)
        await db.commit()
        plan_id = legacy.id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.get("/api/v1/billing/admin/plans")
        assert response.status_code == 200, response.text
        row = next(row for row in response.json() if row["id"] == plan_id)
        assert (row["currency"], row["price_cents"], row["formatted_price"]) == (
            "ZZZ",
            321,
            None,
        )
    async with factory() as db:
        unchanged = await db.get(Plan, plan_id)
        assert unchanged.currency == "ZZZ" and unchanged.price_cents == 321


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", ["ZZZ", "USDT"])
async def test_api_price_currency_uses_same_supported_catalog(billing_client, currency):
    client, factory = billing_client
    response = await client.post(
        "/api/v1/billing/admin/pricing",
        headers={"x-test-identity": "operator"},
        json={
            "provider_id": "openai",
            "model_id": "currency-example",
            "call_kind": "chat",
            "config": {**TEST_PRICE, "currency": currency},
        },
    )
    assert response.status_code == (400 if currency == "ZZZ" else 200), response.text
    async with factory() as db:
        records = (await db.execute(select(BillingPriceProfile))).scalars().all()
        assert len(records) == (0 if currency == "ZZZ" else 1)
        if records:
            assert records[0].config["currency"] == currency
