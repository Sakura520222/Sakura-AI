"""System configuration HTTP feedback follows the authenticated user's locale."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from backend.core import config as core_config
from backend.models.admin_action_log import AdminActionLog
from backend.models.database import AppConfig, WebUIConfig
from backend.webui import deps
from backend.webui.deps import get_db, get_templates, require_csrf, require_super_admin
from backend.webui.i18n import i18n
from backend.webui.routes import system_config as routes
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture
from tests.test_webui_config_save_feedback import _run_save_feedback
from tests.test_webui_modal_system import _extract_inline_scripts

sql_runtime = runtime_fixture


@pytest.fixture
def system_http_app(sql_runtime, monkeypatch):
    factory, _, _ = sql_runtime

    async def db_dependency():
        async with factory() as db:
            yield db

    async def administrator():
        return {"sub": "owner1", "user_id": 1, "role": "super_admin"}

    async def csrf():
        return "test-csrf"

    async def isolated_runtime_update(changes):
        # Persist to isolated SQL; never reconfigure the test process runtime.
        pass

    monkeypatch.setattr(
        deps,
        "decode_access_token",
        lambda token: {"user_id": 1, "token_type": "access"},
    )
    monkeypatch.setattr(deps, "is_access_token_payload", lambda payload: True)
    monkeypatch.setattr(
        routes.system_config_service, "apply_live_settings", isolated_runtime_update
    )
    deps._USER_PREFS_CACHE.clear()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[require_super_admin] = administrator
    app.dependency_overrides[require_csrf] = csrf
    yield app, factory
    deps._USER_PREFS_CACHE.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("language,global_language", [("zh-CN", "en"), ("en", "zh-CN")])
@pytest.mark.parametrize(
    "fields,toast_key,should_persist",
    [
        ({"log_level": "INFO"}, "system_config.saved", True),
        ({"app_timezone": "UTC"}, "system_config.saved_restart_required", True),
        ({"smtp_port": "not-a-number"}, "toast.numeric_required", False),
        ({"app_port": "70000"}, "system_config.invalid_port", False),
        ({"smtp_security": "invalid"}, "system_config.invalid_smtp_security", False),
    ],
)
async def test_system_save_localized_http_and_real_persistence(
    system_http_app,
    monkeypatch,
    language,
    global_language,
    fields,
    toast_key,
    should_persist,
):
    app, factory = system_http_app
    monkeypatch.setattr(core_config, "get_cached_config", lambda key: global_language)
    async with factory() as db:
        db.add(WebUIConfig(user_id=1, language=language))
        await db.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.post("/system-config/save", data=fields)

    assert response.status_code == 302
    feedback = parse_qs(urlsplit(response.headers["location"]).query)
    context = (
        {"field_key": i18n.t("system_config.key_smtp_port", lang=language)}
        if toast_key == "toast.numeric_required"
        else {}
    )
    message = i18n.t(toast_key, lang=language, **context)
    if not should_persist:
        message = i18n.t("toast.config_validation_failed", lang=language, error=message)
    assert feedback["_toast"] == [message]
    assert feedback["_toast_type"] == ["success" if should_persist else "error"]
    if not should_persist:
        issues = json.loads(feedback["_errors"][0])
        assert issues[0]["field"] == next(iter(fields))
    async with factory() as db:
        values = (await db.execute(select(AppConfig))).scalars().all()
        assert {row.key_name: row.key_value for row in values} == (
            fields if should_persist else {}
        )
        audits = (await db.execute(select(AdminActionLog))).scalars().all()
        assert len(audits) == (1 if should_persist else 0)


def test_system_save_keeps_authorization_and_csrf_dependencies():
    route = next(
        route for route in routes.router.routes if route.path == "/system-config/save"
    )
    calls = {dependency.call for dependency in route.dependant.dependencies}
    assert require_super_admin in calls
    assert require_csrf in calls
    assert deps.get_user_preferences in calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields,field",
    [
        ({"smtp_port": "not-a-number"}, "smtp_port"),
        ({"app_port": "70000"}, "app_port"),
        (
            {
                "database_url": "unsafe://operator:secret-password@host/db",
                "database_url_changed": "true",
            },
            "database_url",
        ),
    ],
)
async def test_system_save_ajax_identifies_field_without_disclosing_values(
    system_http_app, monkeypatch, fields, field
):
    app, factory = system_http_app
    monkeypatch.setattr(core_config, "get_cached_config", lambda key: "en")
    async with factory() as db:
        db.add(WebUIConfig(user_id=1, language="zh-CN"))
        await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        response = await client.post(
            "/system-config/save", data=fields, headers={"Accept": "application/json"}
        )
    assert response.headers.get("content-type", "").startswith("application/json")
    result = response.json()
    assert result["ok"] is False
    assert result["errors"][0]["field"] == field
    assert result["errors"][0]["anchor"].startswith("section-system-")
    if field == "smtp_port":
        assert (
            i18n.t("system_config.key_smtp_port", lang="zh-CN")
            in result["errors"][0]["message"]
        )
    assert "secret-password" not in response.text
    async with factory() as db:
        assert (await db.execute(select(AppConfig))).scalars().all() == []


@pytest.mark.asyncio
async def test_system_ajax_success_and_no_change_are_json(system_http_app, monkeypatch):
    app, factory = system_http_app
    monkeypatch.setattr(core_config, "get_cached_config", lambda key: "zh-CN")
    async with factory() as db:
        db.add(WebUIConfig(user_id=1, language="en"))
        await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={deps.WEBUI_TOKEN_COOKIE_NAME: "isolated-token"},
    ) as client:
        for toast_key in ("system_config.saved", "toast.config_no_change"):
            response = await client.post(
                "/system-config/save",
                data={"log_level": "INFO"},
                headers={"Accept": "application/json"},
            )
            assert response.status_code == 200
            assert response.json() == {
                "ok": True,
                "toast": i18n.t(toast_key, lang="en"),
                "errors": [],
            }
    async with factory() as db:
        assert (await db.execute(select(AppConfig.key_value))).scalar_one() == "INFO"
        assert len((await db.execute(select(AdminActionLog))).scalars().all()) == 1


def _system_script(lang):
    source = (
        Path(__file__).parents[1] / "backend/webui/templates/system_config.html"
    ).read_text(encoding="utf-8")
    script = next(
        script
        for script in _extract_inline_scripts(source)
        if "function systemConfig()" in script
    )
    return (
        get_templates()
        .env.from_string(script)
        .render(
            _=lambda key, **params: i18n.t(key, lang=lang, **params),
            csrf_token="isolated-csrf",
            database_reset_confirmation="RESET TEST DATABASE",
        )
    )


def test_system_save_javascript_preserves_failure_input_and_retry():
    _run_save_feedback(
        "vm.runInThisContext("
        + json.dumps(_system_script("zh-CN"))
        + ");\n"
        + """
nodes.set('systemConfigForm', form);
root.setAttribute('x-data', 'systemConfig()');
root.setAttribute('data-config-document-scroll', '');
section.setAttribute('data-config-tab', 'email');
control.name = 'smtp_port'; control.value = 'not-a-number';
form.action = '/system-config/save';
const settings = systemConfig();
Alpine.$data = () => settings;
settings.init();
assert.equal(form.noValidate, true);
let fetches = 0;
global.fetch = async (url, options) => {
    fetches += 1;
    assert.equal(url, '/system-config/save');
    assert.equal(options.headers.Accept, 'application/json');
    return response;
};
response = {ok: false, status: 400, json: async () => ({ok: false,
    toast: 'SMTP端口必须是有效数值', errors: [
        {field: 'smtp_port', message: 'SMTP端口必须是有效数值'}]})};
await settings.saveConfig({target: form});
assert.equal(control.value, 'not-a-number');
assert.equal(control.getAttribute('aria-invalid'), 'true');
assert.equal(document.activeElement, control);
assert.equal(settings.activeTab, 'email');
assert.equal(control.scrolled, true);
assert.equal(window.location.href, '');
assert.equal(settings.saving, false);
response = {ok: true, status: 200, json: async () => ({ok: true,
    toast: '系统配置已保存', errors: []})};
control.value = '587';
await settings.saveConfig({target: form});
assert.equal(control.getAttribute('aria-invalid'), null);
assert.match(window.location.href, /_toast_type=success/);
assert.equal(fetches, 2);
"""
    )


@pytest.mark.parametrize(
    "lang,expected", [("zh-CN", "不能超过"), ("en", "must not exceed")]
)
def test_system_native_validation_uses_page_language(lang, expected):
    _run_save_feedback(
        "vm.runInThisContext("
        + json.dumps(_system_script(lang))
        + ");\n"
        + f"""
nodes.set('systemConfigForm', form);
const settings = systemConfig(); settings.init();
control.validity = {{valid: false, rangeOverflow: true}};
control.max = '65535';
global.fetch = () => {{throw new Error('invalid input must not be sent');}};
await settings.saveConfig({{target: form}});
assert.equal(form.noValidate, true);
assert.match(allText(summary), /{expected}/);
assert.match(allText(summary), /65535/);
assert.equal(document.activeElement, control);
assert.equal(form.nativeReported, undefined);
assert.equal(window.location.href, '');
""",
        lang=lang,
    )
