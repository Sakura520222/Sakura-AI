"""Currency choices are shared by billing forms, JSON and configuration."""

import json
from html.parser import HTMLParser
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
    _build_dynamic_groups,
    _DynamicConfigValidationError,
    _validate_dynamic_config_value,
)
from tests.test_billing_credits_api import TEST_PRICE
from tests.test_billing_credits_api import billing_client as billing_client_fixture
from tests.test_billing_pricing_editor import (
    pricing_app as pricing_app_fixture,
)
from tests.test_billing_pricing_editor import (
    pricing_fields,
    seed_language,
)
from tests.test_billing_pricing_editor import (
    sql_runtime as runtime_fixture,
)

pricing_app = pricing_app_fixture
sql_runtime = runtime_fixture
billing_client = billing_client_fixture


class Selects(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.options = {}
        self.currency_inputs = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        name = values.get("name", "")
        if tag == "input" and (name == "currency" or name.endswith("_currency")):
            self.currency_inputs.append(name)
        if tag == "select":
            self.current = name
            self.options.setdefault(name, [])
        elif tag == "option" and self.current is not None:
            self.options[self.current].append(values.get("value"))

    def handle_endtag(self, tag):
        if tag == "select":
            self.current = None


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
        "paddle_currency",
    ):
        assert get_dynamic_config_input_type(field) == "select"
        codes = [option["value"] for option in DYNAMIC_CONFIG_SELECT_OPTIONS[field]]
        assert codes[:2] == ["USD", "CNY"]
        assert set(codes) == set(CURRENCY_MINOR_EXPONENTS)
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
        groups = await _build_dynamic_groups(db, "en")
        fields = {field["key"]: field for group in groups for field in group["fields"]}
        assert fields["stripe_currency"]["value"] == "ZZZ"
        assert fields["stripe_currency"]["select_options"][-1]["disabled"] is True
        assert fields["paddle_currency"]["value"] == "USD"
        assert (
            await db.execute(
                select(AppConfig.key_value).where(
                    AppConfig.key_name == "paddle_currency"
                )
            )
        ).scalar_one() == "usd"


@pytest.mark.asyncio
async def test_pricing_and_plan_currency_controls_are_selects(pricing_app):
    app, factory, _token = pricing_app
    await seed_language(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        for path, names in (
            ("/billing/admin/pricing", ("currency", "settlement_currency")),
            ("/billing/admin/plans", ("currency",)),
        ):
            response = await client.get(path)
            assert response.status_code == 200, response.text
            controls = Selects()
            controls.feed(response.text)
            assert controls.currency_inputs == []
            for name in names:
                codes = controls.options[name]
                assert codes[:3] == ["", "USD", "CNY"]
                assert set(codes) - {""} == set(CURRENCY_MINOR_EXPONENTS)
            if path.endswith("pricing"):
                assert "chat" in controls.options["call_kind"]
                assert "chat_stream" not in controls.options["call_kind"]


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
async def test_pending_payment_evidence_uses_optional_currency_selects(pricing_app):
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
        response = await client.get("/billing/admin/pricing")
        assert response.status_code == 200
        controls = Selects()
        controls.feed(response.text)
        assert controls.currency_inputs == []
        for name in ("checkout_currency", "refund_currency"):
            assert controls.options[name][:3] == ["", "USD", "CNY"]
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
        response = await client.get("/billing/admin/plans")
        assert response.status_code == 200
        assert "ZZZ 321" in response.text
        assert "Integer currency minor units" in response.text
        controls = Selects()
        controls.feed(response.text)
        assert set(controls.options["currency"]) - {""} == set(CURRENCY_MINOR_EXPONENTS)
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
