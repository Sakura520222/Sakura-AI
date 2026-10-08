"""Human-readable model pricing editor against real HTTP, SQL and price engine."""

import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import select

from backend.core import config as core_config
from backend.models.admin_action_log import AdminActionLog
from backend.models.billing_models import BillingPriceProfile
from backend.models.database import WebUIConfig
from backend.webui import deps
from backend.webui.deps import get_db, get_templates
from backend.webui.i18n import i18n
from backend.webui.routes import billing as routes
from tests.test_billing_credits_api import TEST_PRICE
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture
from tests.test_webui_modal_system import _extract_inline_scripts

sql_runtime = runtime_fixture


@pytest.fixture
def pricing_app(sql_runtime, monkeypatch):
    factory, _, _ = sql_runtime

    async def db_dependency():
        async with factory() as db:
            yield db

    async def identity(request: Request):
        return {
            "user_id": 1,
            "sub": "owner1",
            "role": request.headers.get("x-test-role", "super_admin"),
        }

    monkeypatch.setattr(deps, "get_current_user", identity)
    monkeypatch.setattr(
        deps,
        "decode_access_token",
        lambda token: {"user_id": 1, "token_type": "access"},
    )
    monkeypatch.setattr(deps, "is_access_token_payload", lambda payload: True)
    monkeypatch.setattr(
        deps,
        "_csrf_serializer",
        URLSafeTimedSerializer("isolated-pricing-csrf", salt="webui-csrf"),
    )
    monkeypatch.setattr(core_config, "get_cached_config", lambda key: "zh-CN")
    deps._USER_PREFS_CACHE.clear()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = db_dependency
    yield app, factory, deps.generate_csrf_token()
    deps._USER_PREFS_CACHE.clear()


def pricing_fields(csrf_token, **overrides):
    return {
        "csrf_token": csrf_token,
        "config_mode": "fields",
        "provider_id": "openai",
        "model_id": "test-priced-model",
        "call_kind": "chat",
        "unit": "tokens",
        "token_scale": "1000",
        "currency": "USD",
        "settlement_currency": "CNY",
        "fx_rate": "7",
        "markup": "1.25",
        "credits_per_currency_unit": "10",
        "input_price": "0.003",
        "output_price": "0.009",
        "cache_read_supported": "false",
        "cache_creation_supported": "false",
        "reasoning_supported": "false",
        **overrides,
    }


async def seed_language(factory, language="en"):
    async with factory() as db:
        db.add(WebUIConfig(user_id=1, language=language))
        await db.commit()


@pytest.mark.asyncio
async def test_structured_prices_normalize_unit_and_publish_immutable_versions(
    pricing_app,
):
    app, factory, csrf_token = pricing_app
    await seed_language(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.post(
            "/billing/admin/pricing",
            data=pricing_fields(csrf_token),
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        response = await client.post(
            "/billing/admin/pricing",
            data=pricing_fields(csrf_token, input_price="0.004"),
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200, response.text
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
    app, factory, csrf_token = pricing_app
    await seed_language(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.post(
            "/billing/admin/pricing",
            data=pricing_fields(
                csrf_token,
                unit=unit,
                meter=unit,
                unit_price="0.00000001",
                call_kind="rerank",
            ),
            headers={"Accept": "application/json"},
        )
    assert response.status_code == 200, response.text
    async with factory() as db:
        price = (await db.execute(select(BillingPriceProfile))).scalar_one()
        assert price.config["unit"] == unit
        assert price.config["meter"] == unit
        assert price.config["unit_price"] == "1E-8"
        assert "input_price" not in price.config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,field",
    [
        ({"fx_rate": ""}, "fx_rate"),
        ({"markup": "0"}, "markup"),
        ({"input_price": "NaN"}, "input_price"),
        ({"currency": "US"}, "currency"),
        ({"token_scale": "100"}, "token_scale"),
    ],
)
async def test_editor_invalid_fields_are_localized_without_persistence(
    pricing_app, changes, field
):
    app, factory, csrf_token = pricing_app
    await seed_language(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.post(
            "/billing/admin/pricing",
            data=pricing_fields(csrf_token, **changes),
            headers={"Accept": "application/json"},
        )
    assert response.status_code == 400, response.text
    assert response.json()["errors"][0]["field"] == field
    assert "无效" not in response.json()["errors"][0]["message"]
    async with factory() as db:
        assert (await db.execute(select(BillingPriceProfile))).scalars().all() == []
        assert (await db.execute(select(AdminActionLog))).scalars().all() == []


@pytest.mark.asyncio
async def test_advanced_json_stays_compatible_and_rejects_floats(pricing_app):
    app, factory, csrf_token = pricing_app
    await seed_language(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        legacy = {
            "csrf_token": csrf_token,
            "provider_id": "openai",
            "model_id": "legacy-api-editor-model",
            "call_kind": "chat",
            "config": json.dumps(TEST_PRICE),
        }
        response = await client.post("/billing/admin/pricing", data=legacy)
        assert response.status_code == 302
        response = await client.post(
            "/billing/admin/pricing",
            data={**legacy, "config": json.dumps({**TEST_PRICE, "fx_rate": 1.1})},
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 400, response.text
        assert response.json()["errors"][0]["field"] == "config"
    async with factory() as db:
        assert len((await db.execute(select(BillingPriceProfile))).scalars().all()) == 1


class Controls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}
        self.select_name = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in {"input", "select", "textarea"} and values.get("name"):
            self.fields[values["name"]] = values
        if tag == "select":
            self.select_name = values.get("name")
        elif tag == "option" and self.select_name and "selected" in values:
            self.fields[self.select_name]["value"] = values.get("value", "")

    def handle_endtag(self, tag):
        if tag == "select":
            self.select_name = None


@pytest.mark.asyncio
async def test_editor_page_has_blank_commercial_fields_and_loads_existing_version(
    pricing_app,
):
    app, factory, _csrf_token = pricing_app
    await seed_language(factory)
    async with factory() as db:
        from backend.services.billing_service import BillingService

        original = await BillingService(db).publish_price(
            "openai", "existing-test-model", "chat", TEST_PRICE, actor_id=1
        )
        await db.commit()
        profile_id = original.id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.get("/billing/admin/pricing")
        assert response.status_code == 200, response.text
        controls = Controls()
        controls.feed(response.text)
        for field in (
            "currency",
            "settlement_currency",
            "fx_rate",
            "markup",
            "credits_per_currency_unit",
        ):
            assert controls.fields[field].get("value", "") == ""
        assert "Provider base prices" in response.text
        response = await client.get(f"/billing/admin/pricing?edit_price={profile_id}")
        assert response.status_code == 200, response.text
        controls = Controls()
        controls.feed(response.text)
        assert controls.fields["model_id"]["value"] == "existing-test-model"
        assert controls.fields["fx_rate"]["value"] == TEST_PRICE["fx_rate"]
    async with factory() as db:
        assert len((await db.execute(select(BillingPriceProfile))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_pricing_editor_keeps_superadmin_and_csrf_authority(pricing_app):
    app, factory, csrf_token = pricing_app
    await seed_language(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.post(
            "/billing/admin/pricing", data=pricing_fields("invalid-csrf")
        )
        assert response.status_code == 403
        response = await client.post(
            "/billing/admin/pricing",
            data=pricing_fields(csrf_token),
            headers={"x-test-role": "user"},
        )
        assert response.status_code == 403
    async with factory() as db:
        assert (await db.execute(select(BillingPriceProfile))).scalars().all() == []


def _run_pricing_javascript(assertions):
    template = (
        Path(__file__).parents[1] / "backend/webui/templates/billing/admin_pricing.html"
    ).read_text(encoding="utf-8")
    script = next(
        (
            script
            for script in _extract_inline_scripts(template)
            if "function billingPricingEditor" in script
        ),
        "",
    )
    script = (
        get_templates()
        .env.from_string(script)
        .render(
            _=lambda key, **params: i18n.t(key, lang="en", **params), pricing_catalog=[]
        )
    )
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const controls = {};
function control(name, value = '') {
    return controls[name] = {name, value, disabled: false, required: false,
        addEventListener() {}, setAttribute() {}, validity: {valid: true}};
}
const tokenFields = ['token_scale','input_price','output_price','cached_input_price',
    'cache_creation_price','reasoning_price','cache_read_supported','cache_creation_supported','reasoning_supported'];
const nativeFields = ['unit_price','meter'];
const fields = ['currency','settlement_currency','fx_rate','markup','credits_per_currency_unit',
    'unit', ...tokenFields, ...nativeFields];
fields.forEach(name => control(name));
['provider_id','model_id','call_kind','config_mode','config','csrf_token'].forEach(name => control(name));
controls.provider_id.value = 'openai'; controls.model_id.value = 'test-model'; controls.call_kind.value = 'chat';
controls.config_mode.value = 'fields'; controls.unit.value = 'tokens'; controls.token_scale.value = '1000';
controls.currency.value = 'USD'; controls.settlement_currency.value = 'CNY'; controls.fx_rate.value = '7';
controls.markup.value = '1.25'; controls.credits_per_currency_unit.value = '10';
controls.input_price.value = '0.003'; controls.output_price.value = '0.009';
const group = names => ({hidden: false, querySelectorAll() {return names.map(name => controls[name]);}});
const tokenGroup = group(tokenFields), nativeGroup = group(nativeFields);
const nodes = {
    'pricing-fields': group(fields), 'pricing-json-section': group(['config']),
    'pricing-model-options': {replaceChildren() {}, appendChild() {}},
    'publish-price-button': {disabled: false, textContent: ''},
};
const form = {action: '/billing/admin/pricing',
    elements: {namedItem(name) {return controls[name];}},
    addEventListener() {},
    querySelectorAll(selector) {
        if (selector === '[data-price-token-only]') return [tokenGroup];
        if (selector === '[data-price-native-only]') return [nativeGroup];
        return Object.values(controls);
    },
};
const feedback = [];
global.SakuraConfigFeedback = {clear() {}, nativeErrors() {return [];}, render(value) {feedback.push(value);}};
global.window = globalThis;
window.location = {href: '', pathname: '/billing/admin/pricing', search: '', hash: ''};
window.dispatchEvent = () => {};
global.CustomEvent = class {constructor(name, value) {this.detail = value.detail;}};
global.document = {getElementById(id) {return nodes[id];}, addEventListener() {}, createElement() {return {};}};
global.FormData = class {constructor() {this.values = Object.fromEntries(Object.values(controls).filter(item => !item.disabled).map(item => [item.name, item.value]));}};
let response = {ok: false, status: 400, json: async () => ({ok: false, toast: 'Invalid input', errors: [{field: 'input_price', message: 'Invalid price'}]})};
global.fetch = async () => response;
vm.runInThisContext(payload.script);
assert.equal(typeof billingPricingEditor, 'function');
const editor = billingPricingEditor(form, []);
editor.init();
(async () => {
"""
    result = subprocess.run(
        [
            "node",
            "-e",
            harness
            + assertions
            + "\n})().catch(error => {console.error(error); process.exit(1);});",
        ],
        input=json.dumps({"script": script}),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_advanced_json_mode_keeps_exact_token_strings_and_optional_unknowns():
    _run_pricing_javascript("""
controls.input_price.value = '0.000000000000000000000000000000001';
controls.config_mode.value = 'json';
editor.changeMode();
const value = JSON.parse(controls.config.value);
assert.equal(typeof value.input_price, 'string');
assert.equal(value.input_price, '0.000000000000000000000000000000001e3');
assert.equal(value.output_price, '0.009e3');
assert.equal(value.fx_rate, '7');
assert.equal(value.cached_input_price, undefined);
assert.equal(value.cache_read_supported, undefined);
assert.equal(controls.currency.disabled, true);
assert.equal(controls.config.required, true);
controls.config_mode.value = 'fields';
editor.changeMode();
assert.equal(controls.token_scale.value, '1000000');
assert.equal(controls.input_price.value, value.input_price);
""")


def test_native_unit_ui_disables_token_fields_and_requires_native_price():
    _run_pricing_javascript("""
assert.equal(controls.unit_price.disabled, true);
assert.equal(controls.input_price.required, true);
controls.unit.value = 'documents'; editor.syncUnit(true);
assert.equal(controls.input_price.disabled, true);
assert.equal(controls.token_scale.disabled, true);
assert.equal(controls.unit_price.disabled, false);
assert.equal(controls.unit_price.required, true);
assert.equal(controls.meter.value, 'documents');
assert.equal(controls.unit_price.value, '');
""")


def test_json_float_is_not_silently_coerced_into_form_price():
    _run_pricing_javascript("""
controls.config_mode.value = 'json'; editor.changeMode();
const value = JSON.parse(controls.config.value); value.fx_rate = 1.1;
controls.config.value = JSON.stringify(value);
controls.config_mode.value = 'fields'; editor.changeMode();
assert.equal(controls.config_mode.value, 'json');
assert.equal(JSON.parse(controls.config.value).fx_rate, 1.1);
assert.equal(feedback.at(-1).errors[0].field, 'config');
""")


def test_pricing_ajax_failure_retains_input_and_retry_publishes():
    _run_pricing_javascript("""
await editor.save();
assert.equal(controls.input_price.value, '0.003');
assert.equal(window.location.href, '');
assert.equal(nodes['publish-price-button'].disabled, false);
assert.equal(feedback.at(-1).errors[0].field, 'input_price');
response = {ok: true, status: 200, json: async () => ({ok: true, toast: 'Price version published', errors: []})};
await editor.save();
assert.match(window.location.href, /_toast_type=success/);
""")
