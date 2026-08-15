"""Setup Wizard test_ai_api 的 API Key 规范化回归测试（Issue #502）。"""
from types import SimpleNamespace

import pytest

from backend.core.ai_protocol.errors import AIError
from backend.core.ai_protocol.models import AIErrorCategory
from backend.core.setup_service import setup_service


class _RecordingAdapter:
    def __init__(self) -> None:
        self.api_key = None

    async def list_models(self, client, endpoint, api_key):
        self.api_key = api_key
        return [SimpleNamespace(model_id="test-model", context_window_tokens=None)]


class _AuthFailingAdapter:
    async def list_models(self, client, endpoint, api_key):
        raise AIError(AIErrorCategory.AUTH_INVALID, "upstream secret diagnostic")


@pytest.mark.asyncio
async def test_setup_test_ai_api_strips_api_key_before_request(monkeypatch):
    adapter = _RecordingAdapter()
    monkeypatch.setattr(
        "backend.core.ai_protocol.registry.get_adapter", lambda family: adapter
    )
    monkeypatch.setattr(
        "backend.core.ai_protocol.registry.resolve_endpoint",
        lambda decl, api_base="": SimpleNamespace(),
    )
    result = await setup_service.test_ai_api(
        "  sk-setup-token\n", "https://example.com/v1", model="test-model"
    )
    assert result["success"] is True
    assert adapter.api_key == "sk-setup-token"


@pytest.mark.asyncio
async def test_setup_test_ai_api_auth_error_is_actionable_without_upstream_leak(
    monkeypatch,
):
    monkeypatch.setattr(
        "backend.core.ai_protocol.registry.get_adapter",
        lambda family: _AuthFailingAdapter(),
    )
    monkeypatch.setattr(
        "backend.core.ai_protocol.registry.resolve_endpoint",
        lambda decl, api_base="": SimpleNamespace(),
    )
    result = await setup_service.test_ai_api(
        "sk-setup-token", "https://example.com/v1"
    )
    assert result["success"] is False
    assert "API 鉴权失败" in result["message"]
    assert "upstream secret diagnostic" not in result["message"]
