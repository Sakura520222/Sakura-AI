"""Configured account selection and its automatic model discovery in the WebUI."""

import json
import subprocess
from pathlib import Path

import pytest
from jinja2 import ChoiceLoader, DictLoader

from backend.webui.deps import get_templates
from backend.webui.i18n import i18n

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "backend/webui/templates/components/billing_pricing_accounts.js"


def run_picker(assertions):
    assert SCRIPT.exists(), "The pricing page needs a configured-account model picker"
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
global.window = globalThis;
function control(value = '') {
    return {value, disabled: false, listeners: {}, children: [], dataset: {},
        addEventListener(event, fn) {this.listeners[event] = fn;},
        replaceChildren() {this.children = []; this.value = '';},
        appendChild(option) {this.children.push(option); if (option.selected) this.value = option.value;},
        setAttribute() {}};
}
const account = control('acc_a'), model = control('a-selected'), scope = control('account'), kind = control('chat');
const status = control(), refresh = control();
const controls = {account_id: account, model_id: model, source_scope: scope, call_kind: kind};
const form = {elements: {namedItem(name) {return controls[name];}}};
global.document = {
    getElementById(id) {return {'pricing-model-status': status, 'pricing-refresh-models': refresh}[id];},
    createElement() {return {};},
};
const requests = [];
global.fetch = (url, options) => new Promise((resolve, reject) => {requests.push({url, options, resolve, reject});});
const labels = {chooseAccount: 'Choose account', chooseModel: 'Choose model',
    noModels: 'No models', loading: 'Loading', ready: '{count} models',
    failed: 'Discovery failed; saved models remain available', unavailable: 'Unavailable',
    auxiliary: 'Models come from the independent configured service'};
vm.runInThisContext(payload.script);
const accounts = [
    {id: 'acc_a', name: 'Account A', provider_id: 'custom', models: ['a-selected','shared'], default_model: 'a-default'},
    {id: 'acc_b', name: 'Account B', provider_id: 'custom', models: ['b-only'], default_model: 'b-only'},
    {id: 'acc_empty', name: 'Empty', provider_id: 'openai', models: [], default_model: ''},
];
const auxiliary = [{feature: 'embedding', provider_id: 'custom', model_id: 'standalone-embed'},
    {feature: 'rerank', provider_id: 'custom', model_id: 'standalone-rerank'}];
const picker = billingPricingAccountPicker(form, accounts, labels, auxiliary);
picker.init();
const ids = () => model.children.map(option => option.value);
const flush = async () => {await new Promise(resolve => setImmediate(resolve));};
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
        input=json.dumps({"script": SCRIPT.read_text(encoding="utf-8")}),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_saved_models_are_immediate_and_discovery_is_automatic():
    run_picker("""
assert.deepEqual(ids(), ['', 'a-selected','shared','a-default']);
assert.equal(model.value, 'a-selected');
assert.equal(requests.length, 1);
assert.equal(requests[0].url, '/billing/admin/pricing/accounts/acc_a/models');
assert.equal(requests[0].options.credentials, 'same-origin');
assert.equal(status.dataset.state, 'loading');
requests[0].resolve({ok: true, json: async () => ({success: true, data: {account_id: 'acc_a', models: [{id: 'a-new', label: 'New A'}]}})});
await flush();
assert.deepEqual(ids(), ['', 'a-selected','shared','a-default','a-new']);
assert.equal(model.value, 'a-selected');
assert.equal(status.textContent, '4 models');
""")


def test_account_switch_and_stale_discovery_do_not_reuse_another_accounts_models():
    run_picker("""
account.value = 'acc_b'; account.listeners.change();
assert.equal(model.value, 'b-only');
assert.deepEqual(ids(), ['', 'b-only']);
assert.equal(requests.length, 2);
requests[1].resolve({ok: true, json: async () => ({success: true, data: {account_id: 'acc_b', models: ['b-discovered']}})});
await flush();
requests[0].resolve({ok: true, json: async () => ({success: true, data: {account_id: 'acc_a', models: ['a-stale']}})});
await flush();
assert.deepEqual(ids(), ['', 'b-only','b-discovered']);
assert.equal(model.value, 'b-only');
assert.equal(status.textContent, '2 models');
""")


def test_discovery_failure_preserves_selection_and_uses_localized_message():
    run_picker("""
requests[0].resolve({ok: false, json: async () => ({success: false, error: 'secret key upstream failure'})});
await flush();
assert.equal(model.value, 'a-selected');
assert.equal(status.textContent, labels.failed);
assert.equal(status.dataset.state, 'error');
assert.equal(refresh.disabled, false);
refresh.listeners.click();
assert.equal(requests.length, 2);
requests[1].resolve({ok: true, json: async () => ({success: true, data: {account_id: 'wrong-account', models: ['do-not-use']}})});
await flush();
assert.equal(model.value, 'a-selected');
assert.equal(ids().includes('do-not-use'), false);
""")


def test_saved_model_fallback_keeps_an_observable_discovery_failure():
    run_picker("""
requests[0].resolve({ok: true, json: async () => ({success: true, data: {
    account_id: 'acc_a', models: ['a-selected'], source: 'saved', discovery_failed: true}})});
await flush();
assert.equal(model.value, 'a-selected');
assert.equal(status.dataset.state, 'error');
assert.equal(status.textContent, labels.failed);
assert.equal(refresh.disabled, false);
""")


def test_manual_refresh_bypasses_the_discovery_cache_but_automatic_fetch_does_not():
    run_picker("""
assert.equal(requests[0].url, '/billing/admin/pricing/accounts/acc_a/models');
requests[0].resolve({ok: true, json: async () => ({success: true, data: {account_id: 'acc_a', models: ['initial']}})});
await flush();
refresh.listeners.click();
assert.equal(requests[1].url, '/billing/admin/pricing/accounts/acc_a/models?refresh=true');
requests[1].resolve({ok: true, json: async () => ({success: true, data: {account_id: 'acc_a', models: ['newly-available']}})});
await flush();
assert.equal(ids().includes('newly-available'), true);
account.value = 'acc_b'; account.listeners.change();
assert.equal(requests[2].url, '/billing/admin/pricing/accounts/acc_b/models');
""")


def test_unavailable_account_and_empty_discovery_have_explicit_states():
    run_picker("""
account.value = 'removed-account'; account.listeners.change();
assert.equal(model.value, '');
assert.equal(model.disabled, true);
assert.equal(status.textContent, labels.chooseAccount);
assert.equal(requests.length, 1);
account.value = 'acc_empty'; account.listeners.change();
requests[1].resolve({ok: true, json: async () => ({success: true, data: {account_id: 'acc_empty', models: []}})});
await flush();
assert.equal(status.textContent, labels.noModels);
assert.equal(model.value, '');
assert.equal(model.disabled, false);
""")


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_rendered_picker_uses_configured_accounts_and_preserves_removed_history(
    language,
):
    env = get_templates().env.overlay()
    env.loader = ChoiceLoader(
        [
            DictLoader(
                {
                    "base.html": "{% block content %}{% endblock %}{% block extra_scripts %}{% endblock %}"
                }
            ),
            env.loader,
        ]
    )
    html = env.get_template("billing/admin_pricing.html").render(
        _=lambda key, **params: i18n.t(key, lang=language, **params),
        csrf_token="isolated-token",
        editing_profile=None,
        price_form={
            "account_id": "deleted_account",
            "model_id": "historical-model",
            "call_kind": "chat",
            "unit": "tokens",
        },
        pricing_accounts=[
            {
                "id": "configured_account",
                "name": "Configured <account>",
                "provider_id": "custom",
                "models": ["private-model"],
                "default_model": "private-model",
                "enabled": True,
            }
        ],
        pricing_auxiliary=[
            {
                "feature": "embedding",
                "provider_id": "custom",
                "model_id": "configured-embed",
            }
        ],
        pricing_call_kinds=["chat"],
        pricing_units=["tokens"],
        price_json="{}",
        prices={"items": [], "offset": 0, "limit": 20, "total": 0},
        payment_events={"items": [], "has_more": False},
        grant_plans=[],
        page=1,
        grant_idempotency_key="isolated-grant",
        initial_errors=[],
    )
    assert '<select id="price-account" name="account_id"' in html
    assert '<select id="price-model" name="model_id"' in html
    assert '<select id="pricing-source" name="source_scope"' in html
    assert 'value="embedding"' in html
    assert 'value="configured_account"' in html
    assert "Configured &lt;account&gt;" in html
    assert 'value="deleted_account" selected disabled' in html
    assert "pricing-provider-options" not in html
    assert "pricing_catalog" not in html
    assert i18n.t("billing.form.account_id", lang=language) in html
    assert i18n.t("billing.form.refresh_models", lang=language) in html
    assert "/static/js/billing_pricing_accounts.js" not in html
    assert html.index("function billingPricingAccountPicker") < html.index(
        "function billingPricingEditor"
    )


def test_auxiliary_source_uses_only_configured_model_and_cancels_account_discovery():
    run_picker("""
scope.value = 'embedding'; scope.listeners.change();
assert.equal(account.disabled, true);
assert.equal(account.required, false);
assert.equal(model.disabled, true);
assert.deepEqual(ids(), ['standalone-embed']);
assert.equal(model.value, 'standalone-embed');
assert.equal(kind.value, 'embedding');
assert.equal(kind.disabled, true);
assert.equal(refresh.hidden, true);
assert.equal(status.textContent, labels.auxiliary);
requests[0].resolve({ok: true, json: async () => ({success: true, data: {account_id: 'acc_a', models: ['late-account-model']}})});
await flush();
assert.deepEqual(ids(), ['standalone-embed']);
scope.value = 'rerank'; scope.listeners.change();
assert.equal(model.value, 'standalone-rerank');
assert.equal(kind.value, 'rerank');
assert.equal(requests.length, 1);
scope.value = 'account'; scope.listeners.change();
assert.equal(account.value, '');
assert.equal(account.disabled, false);
assert.equal(account.required, true);
assert.equal(model.value, '');
assert.equal(kind.value, 'chat');
assert.equal(kind.disabled, false);
assert.equal(refresh.hidden, false);
""")


def test_loading_an_auxiliary_version_does_not_restore_an_auxiliary_call_type_for_accounts():
    run_picker("""
scope.value = 'embedding'; kind.value = 'embedding';
const auxiliaryEditor = billingPricingAccountPicker(form, accounts, labels, auxiliary);
auxiliaryEditor.init();
assert.equal(model.value, 'standalone-embed');
scope.value = 'account'; scope.listeners.change();
assert.equal(kind.value, 'chat');
assert.equal(account.value, '');
""")
