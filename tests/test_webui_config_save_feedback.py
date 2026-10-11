"""Execute configuration-save feedback against a DOM event harness."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from backend.webui.deps import get_templates
from backend.webui.i18n import i18n

TEMPLATES = Path(__file__).parents[1] / "backend/webui/templates"


def _run_save_feedback(assertions: str, *, lang: str = "zh-CN") -> None:
    source = (TEMPLATES / "config_unified.html").read_text(encoding="utf-8")
    save_source = (
        source.split("// ===== 统一保存", 1)[1]
        .split("\n", 1)[1]
        .split("if (saveBtn) saveBtn.addEventListener", 1)[0]
    )
    feedback_path = TEMPLATES / "components/config_save_feedback.html"
    feedback_source = (
        feedback_path.read_text(encoding="utf-8") if feedback_path.exists() else ""
    )
    script_sources = [
        feedback_source.replace("<script>", "").replace("</script>", ""),
        save_source,
    ]
    rendered = [
        get_templates()
        .env.from_string(script)
        .render(_=lambda key, **params: i18n.t(key, lang=lang, **params))
        for script in script_sources
    ]
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const nodes = new Map();
class Element {
    constructor(tag, id = '') {
        this.tagName = tag.toUpperCase(); this.id = id; this.attributes = {};
        this.children = []; this.parentElement = null; this.textContent = '';
        this.hidden = false; this.disabled = false; this.name = ''; this.type = '';
        this.validity = {valid: true}; this.min = ''; this.max = ''; this.step = '';
        this.listeners = {}; this.classes = new Set();
        this.classList = {
            add: (...values) => values.forEach(value => this.classes.add(value)),
            remove: (...values) => values.forEach(value => this.classes.delete(value)),
            contains: value => this.classes.has(value),
        };
        if (id) nodes.set(id, this);
    }
    appendChild(child) { child.parentElement = this; this.children.push(child); if (child.id) nodes.set(child.id, child); return child; }
    append(...children) { children.forEach(child => this.appendChild(child)); }
    replaceChildren(...children) { this.children = []; this.append(...children); }
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    getAttribute(key) { return this.attributes[key] ?? null; }
    removeAttribute(key) { delete this.attributes[key]; }
    addEventListener(key, value) { this.listeners[key] = value; }
    contains(node) { return this === node || this.children.some(child => child.contains(node)); }
    focus() { document.activeElement = this; }
    getBoundingClientRect() { return {top: 0}; }
    scrollIntoView() { this.scrolled = true; }
    get innerHTML() { throw new Error('feedback must use textContent'); }
    set innerHTML(value) { throw new Error('feedback must not inject HTML'); }
    closest(selector) {
        for (let node = this; node; node = node.parentElement) {
            if (selector === 'section' && node.tagName === 'SECTION') return node;
            if (selector === 'form' && node.tagName === 'FORM') return node;
            if (selector === '[data-config-field]' && node.attributes['data-config-field']) return node;
            if (selector === '[x-data]' && node.attributes['x-data']) return node;
        }
        return null;
    }
    querySelectorAll(selector) {
        const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
        if (selector === '*') return descendants;
        if (selector.includes('input') || selector.includes('select') || selector.includes('textarea')) {
            return descendants.filter(child => ['INPUT', 'SELECT', 'TEXTAREA'].includes(child.tagName));
        }
        if (selector.includes(':invalid')) return descendants.filter(child => child.validity.valid === false);
        if (selector.includes('config-field-error')) return descendants.filter(child => child.classes.has('config-field-error'));
        if (selector.includes('aria-invalid')) return descendants.filter(child => child.attributes['aria-invalid'] === 'true');
        if (selector.includes('data-config-error')) return descendants.filter(child => child.attributes['data-config-error']);
        if (selector === 'h2, h3') return descendants.filter(child => ['H2', 'H3'].includes(child.tagName));
        return [];
    }
    querySelector(selector) {
        if (selector === 'input[name="csrf_token"]') return csrf;
        return this.querySelectorAll(selector)[0] || null;
    }
}
const button = new Element('button', 'save-all-btn'); button.textContent = '保存全部配置';
const summary = new Element('section', 'config-save-errors'); summary.hidden = true;
const list = new Element('ul', 'config-save-error-list'); summary.appendChild(list);
const root = new Element('div', 'config-scroll');
const form = new Element('form', 'configForm'); form.attributes.action = '/config/general/save';
form.checkValidity = () => control.validity.valid; form.reportValidity = () => { form.nativeReported = true; };
root.appendChild(form);
const section = new Element('section', 'section-group-quota'); form.appendChild(section);
const heading = new Element('h3'); heading.textContent = payload.lang === 'zh-CN' ? '付费配额配置' : 'Billing configuration'; section.appendChild(heading);
const fieldRow = new Element('div'); fieldRow.attributes['data-config-field'] = 'billing_enabled'; section.appendChild(fieldRow);
const control = new Element('input'); control.name = 'billing_enabled'; control.attributes['data-config-label'] = payload.lang === 'zh-CN' ? '启用 Credits 用量收费' : 'Enable Credits usage billing'; fieldRow.appendChild(control);
const csrf = new Element('input'); csrf.name = 'csrf_token'; csrf.value = 'signed-token';
const toasts = []; const scrolls = [];
global.window = globalThis;
window.location = {href: '', origin: 'http://localhost:8767', pathname: '/config', search: ''};
window.dispatchEvent = event => toasts.push(event.detail);
global.CustomEvent = class {constructor(type, options) {this.type = type; this.detail = options.detail;}};
global.history = {replaceState() {}};
global.document = {
    activeElement: null,
    getElementById: id => nodes.get(id) || null,
    getElementsByName: name => root.querySelectorAll('input, select, textarea').filter(node => node.name === name),
    querySelectorAll: selector => selector === '#config-scroll form' ? [form] : root.querySelectorAll(selector),
    createElement: tag => new Element(tag),
};
global.requestAnimationFrame = callback => callback();
global.Alpine = {nextTick: callback => callback(), $data: () => ({})};
global.FormData = class {forEach(callback) {callback('true', 'billing_enabled'); callback('signed-token', 'csrf_token');}};
global.scrollToSection = id => scrolls.push(id);
let response = {ok: true, status: 200, json: async () => ({ok: true, toast: 'saved'})};
let fetchError = null;
global.fetch = async () => {if (fetchError) throw fetchError; return response;};
for (const script of payload.scripts) vm.runInThisContext(script);
function allText(element) {return [element.textContent, ...element.children.map(allText)].join(' ');}
async function saved() {await saveAllConfig(); await new Promise(resolve => setImmediate(resolve));}
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
        input=json.dumps({"scripts": rendered, "lang": lang}),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("status", [200, 422])
def test_structured_save_failure_locates_field_and_retains_unsaved_values(status):
    _run_save_feedback(
        f"""
response = {{ok: {json.dumps(status == 200)}, status: {status}, json: async () => ({{
    ok: false, toast: '有 1 项配置保存失败', results: [{{ok: false, anchor: null,
    errors: [{{field: 'billing_enabled', code: 'billing_missing_prices',
        message: '请先配置模型价格', help_url: '/billing/admin/pricing', help_label: '配置价格',
        details: ['模型 A：chat']}}]}}]
}})}};
control.value = 'unsaved';
await saved();
assert.equal(control.getAttribute('aria-invalid'), 'true');
assert.equal(document.activeElement, control);
assert.equal(summary.hidden, false);
assert.match(allText(summary), /请先配置模型价格/);
assert.match(allText(summary), /付费配额配置/);
assert.match(allText(summary), /启用 Credits 用量收费/);
assert.equal(control.value, 'unsaved');
assert.equal(window.location.href, '');
assert.equal(button.disabled, false);
assert.equal(toasts.at(-1).type, 'error');
"""
    )


@pytest.mark.parametrize("lang, expected", [("zh-CN", "至少"), ("en", "at least")])
def test_native_validation_uses_account_language_and_bounds(lang, expected):
    _run_save_feedback(
        f"""
control.validity = {{valid: false, rangeUnderflow: true}}; control.min = '60';
await saved();
assert.equal(control.getAttribute('aria-invalid'), 'true');
assert.equal(form.nativeReported, undefined);
assert.match(allText(summary), /60/);
assert.match(allText(summary), /{expected}/);
assert.equal(document.activeElement, control);
""",
        lang=lang,
    )


def test_error_focus_opens_hidden_config_tab_and_scrolls_document():
    _run_save_feedback(
        """
const alpineState = {activeTab: 'email'};
form.setAttribute('x-data', '{activeTab: "email"}');
section.setAttribute('data-config-tab', 'billing');
root.setAttribute('data-config-document-scroll', '');
Alpine.$data = () => alpineState;
response.json = async () => ({ok: false, errors: [{field: 'billing_enabled', message: '先配置价格'}]});
await saved();
assert.equal(alpineState.activeTab, 'billing');
assert.equal(control.scrolled, true);
assert.equal(document.activeElement, control);
"""
    )


def test_section_error_opens_tab_even_without_matching_field():
    _run_save_feedback(
        """
const alpineState = {activeTab: 'email'};
form.setAttribute('x-data', '{activeTab: "email"}');
section.setAttribute('data-config-tab', 'billing');
root.setAttribute('data-config-document-scroll', '');
Alpine.$data = () => alpineState;
response.json = async () => ({ok: false, errors: [{anchor: section.id, message: '此组保存失败'}]});
await saved();
assert.equal(alpineState.activeTab, 'billing');
assert.equal(section.scrolled, true);
"""
    )


def test_retry_clears_field_errors_and_success_has_no_failed_results():
    _run_save_feedback(
        """
response.json = async () => ({ok: false, errors: [{field: 'billing_enabled', message: '先配置价格'}]});
control.setAttribute('aria-describedby', 'billing-help');
await saved();
assert.equal(control.getAttribute('aria-invalid'), 'true');
response.json = async () => ({ok: true, toast: '全部配置已保存', results: [{ok: false, errors: [
    {field: 'billing_enabled', message: '保存仍失败'}]}]});
await saved();
assert.equal(window.location.href, '', 'a failed section must never redirect as successful');
assert.match(allText(summary), /保存仍失败/);
response.json = async () => ({ok: true, toast: '全部配置已保存', results: [{ok: true}]});
await saved();
assert.equal(control.getAttribute('aria-invalid'), null);
assert.equal(control.getAttribute('aria-describedby'), 'billing-help');
assert.equal(summary.hidden, true);
assert.match(window.location.href, /_toast_type=success/);
assert.equal(decodeURIComponent(window.location.href).includes('全部配置已保存'), true);
"""
    )


def test_feedback_does_not_interpret_html_or_external_repair_links():
    _run_save_feedback(
        r"""
response.json = async () => ({ok: false, errors: [{field: 'billing_enabled',
    message: '<img src=x onerror=alert(1)>', help_url: 'javascript:alert(1)', help_label: 'unsafe'}]});
await saved();
assert.match(allText(summary), /<img src=x onerror=alert\(1\)>/);
assert.equal(list.querySelectorAll('*').filter(node => node.tagName === 'A').length, 0);
"""
    )


@pytest.mark.parametrize(
    "lang,expected", [("zh-CN", "请求失败"), ("en", "request failed")]
)
def test_network_failure_uses_page_language(lang, expected):
    _run_save_feedback(
        f"""
fetchError = new Error('secret backend exception');
await saved();
assert.match(toasts.at(-1).message, /{expected}/i);
assert.doesNotMatch(toasts.at(-1).message, /secret/);
assert.equal(button.disabled, false);
assert.equal(window.location.href, '');
""",
        lang=lang,
    )
