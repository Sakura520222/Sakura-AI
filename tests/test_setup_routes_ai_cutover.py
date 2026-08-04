"""Setup 路由旧 LLM supplier 流程硬切换测试。"""

import json
from types import SimpleNamespace

import pytest

from backend.webui.routes import setup as setup_route


class _Request:
    def __init__(self, body: dict | None = None):
        self.body = body or {}

    async def json(self):
        return self.body


def _response_json(response) -> dict:
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_setup_page_loads_without_legacy_settings_attributes(monkeypatch):
    """Setup 页面不能依赖已删除的旧 Settings AI 字段。"""
    monkeypatch.setattr(setup_route, "is_bootstrap_mode", lambda: True)
    monkeypatch.setattr(setup_route, "get_current_step", lambda: _async_value(0))
    monkeypatch.setattr(setup_route, "get_missing_fields", lambda: _async_value([]))
    monkeypatch.setattr(
        setup_route,
        "render_template",
        lambda template, request, **context: {
            "template": template,
            "context": context,
        },
    )

    result = await setup_route.setup_page(_Request())

    assert result["template"] == "setup_wizard.html"
    assert not hasattr(SimpleNamespace(), "openai_api_key")
    assert not hasattr(SimpleNamespace(), "openai_api_base")
    assert not hasattr(SimpleNamespace(), "openai_model")


@pytest.mark.asyncio
async def test_openai_test_connection_returns_migration_response_without_supplier_call(
    monkeypatch,
):
    """旧 openai 测试请求只能得到迁移提示，不能调用 supplier 流程。"""
    monkeypatch.setattr(setup_route, "is_bootstrap_mode", lambda: True)
    test_ai_api = _FailIfCalled()
    monkeypatch.setattr(setup_route.setup_service, "test_ai_api", test_ai_api)

    response = await setup_route.test_connection(
        _Request(
            {
                "type": "openai",
                "api_key": "legacy-key",
                "api_base": "https://legacy.example/v1",
                "provider": "legacy-provider",
                "model": "legacy-model",
            }
        )
    )

    assert response.status_code == 410
    payload = _response_json(response)
    assert payload["success"] is False
    assert payload["migration"]["accounts"] == "ai_account.*"
    assert payload["migration"]["role_bindings"] == "ai_role_bindings"
    assert test_ai_api.called is False


@pytest.mark.asyncio
async def test_ai_provider_catalog_returns_migration_response_without_supplier_call(
    monkeypatch,
):
    """旧供应商目录 API 不得再读取 SetupService 的供应商目录。"""
    monkeypatch.setattr(setup_route, "is_bootstrap_mode", lambda: True)
    list_ai_providers = _FailIfCalled()
    monkeypatch.setattr(
        setup_route.setup_service, "list_ai_providers", list_ai_providers
    )

    response = await setup_route.get_ai_providers(_Request())

    assert response.status_code == 410
    payload = _response_json(response)
    assert payload["success"] is False
    assert payload["migration"]["accounts"] == "ai_account.*"
    assert list_ai_providers.called is False


@pytest.mark.asyncio
async def test_ai_models_returns_migration_response_without_supplier_call(monkeypatch):
    """旧按供应商拉取模型 API 不得再调用 SetupService。"""
    monkeypatch.setattr(setup_route, "is_bootstrap_mode", lambda: True)
    fetch_provider_models = _FailIfCalled()
    monkeypatch.setattr(
        setup_route.setup_service,
        "fetch_provider_models",
        fetch_provider_models,
    )

    response = await setup_route.get_ai_models(
        _Request(
            {
                "provider": "legacy-provider",
                "api_key": "legacy-key",
                "api_base": "https://legacy.example/v1",
            }
        )
    )

    assert response.status_code == 410
    payload = _response_json(response)
    assert payload["success"] is False
    assert payload["migration"]["role_bindings"] == "ai_role_bindings"
    assert fetch_provider_models.called is False


class _FailIfCalled:
    def __init__(self):
        self.called = False

    def __call__(self, *args, **kwargs):
        self.called = True
        pytest.fail("旧 SetupService supplier 流程不应被调用")


async def _async_value(value):
    return value
