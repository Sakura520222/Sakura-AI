"""Supported historical monetary codes load without rewriting saved data."""

import asyncio
import json
from html.parser import HTMLParser
from types import SimpleNamespace

import httpx
import pytest

from backend.models.payment_models import Plan
from backend.services.payment.currency_units import supported_currencies
from backend.webui import deps
from backend.webui.deps import get_templates
from backend.webui.routes.billing import _pricing_editor_values
from tests.test_billing_credits_api import TEST_PRICE
from tests.test_billing_pricing_editor import pricing_app as pricing_app_fixture
from tests.test_billing_pricing_editor import seed_language
from tests.test_billing_pricing_editor import sql_runtime as runtime_fixture

pricing_app = pricing_app_fixture
sql_runtime = runtime_fixture


class SelectedCurrency(HTMLParser):
    def __init__(self):
        super().__init__()
        self.selected = []
        self.option_index = -1

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "option":
            self.option_index += 1
            if "selected" in values:
                values["index"] = self.option_index
                self.selected.append(values)


@pytest.mark.parametrize(
    "raw, expected", [("usd", "USD"), (" usd ", "USD"), ("usdt", "USDT"), (" BTC ", "")]
)
def test_currency_macro_selects_supported_history_only(raw, expected):
    html = (
        get_templates()
        .env.from_string(
            "{% from 'components/currency_select.html' import currency_select with context %}"
            "{{ currency_select('currency', old) }}"
        )
        .render(old=raw, currency_codes=supported_currencies(), _=lambda key: key)
    )
    parser = SelectedCurrency()
    parser.feed(html)
    assert parser.selected[0]["value"] == expected
    assert ("disabled" in parser.selected[0]) == (
        expected not in supported_currencies()
    )
    if not expected:
        assert raw in html
        assert parser.selected[0]["index"] == 0


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
async def test_rendered_plan_editor_normalizes_only_supported_saved_currency(
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
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.get("/billing/admin/plans")
    assert response.status_code == 200, response.text
    function = response.text.split("function openEditPlanModal(planId) {", 1)[1].split(
        "function openDeletePlanModal", 1
    )[0]
    script = (
        """
const assert = require('node:assert/strict');
const currency = {options: [{value: '', textContent: 'Choose currency'}, {value: 'USD'}, {value: 'CNY'}], querySelectorAll: () => [], add(option) {this.options.push(option);}, set selectedIndex(index) {this.value = this.options[index].value; this.selected = this.options[index]; this.currentIndex = index;}};
const document = {getElementById: id => id === 'edit-currency' ? currency : {}};
const Sakura = {openModal: () => {}};
const Option = function(label,value) {this.value = value; this.label = label; this.dataset = {};};
const plansData = {1: {id: 1, rate_limits: {}, currency: RAW}};
function openEditPlanModal(planId) {FUNCTION
openEditPlanModal(1);
assert.equal(currency.value, EXPECTED);
assert.equal(currency.options.filter(option => option.disabled).length, UNSUPPORTED ? 1 : 0);
assert.equal(currency.options.length, 3);
if (UNSUPPORTED) {
    assert.equal(currency.currentIndex, 0);
    assert.ok(currency.selected.textContent.includes(RAW));
}
assert.equal(plansData[1].currency, RAW);
plansData[2] = {id: 2, rate_limits: {}, currency: 'usd'};
openEditPlanModal(2);
assert.equal(currency.value, 'USD');
assert.equal(currency.options.length, 3);
assert.equal(currency.options[0].disabled, false);
assert.equal(currency.options[0].textContent, 'Choose currency');
""".replace("FUNCTION", function)
        .replace("RAW", json.dumps(raw))
        .replace("EXPECTED", json.dumps(expected))
        .replace("UNSUPPORTED", str(expected not in supported_currencies()).lower())
    )
    process = await asyncio.create_subprocess_exec(
        "node",
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
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
