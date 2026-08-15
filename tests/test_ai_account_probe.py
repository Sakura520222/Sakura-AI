"""AI 账号连接探测回归测试。"""

from types import SimpleNamespace

import pytest

from backend.core.ai_protocol import account_probe
from backend.core.ai_protocol.errors import AIError
from backend.core.ai_protocol.models import AIErrorCategory


class _RecordingAdapter:
    def __init__(self) -> None:
        self.api_key = None

    async def list_models(self, client, endpoint, api_key):
        self.api_key = api_key
        return [SimpleNamespace(model_id="test-model", context_window_tokens=None)]


@pytest.mark.asyncio
async def test_probe_account_strips_api_key_before_request(monkeypatch):
    adapter = _RecordingAdapter()

    monkeypatch.setattr(
        account_probe,
        "validate_provider_base_url",
        lambda *args, **kwargs: (True, ""),
    )
    monkeypatch.setattr(account_probe, "get_adapter", lambda family: adapter)
    monkeypatch.setattr(
        account_probe,
        "resolve_account_endpoint",
        lambda *args, **kwargs: SimpleNamespace(),
    )

    result = await account_probe.probe_account(
        provider_id="custom",
        protocol="openai-compatible",
        api_base="https://example.com/v1",
        api_key="  sk-test-token\n",
    )

    assert result["success"] is True
    assert adapter.api_key == "sk-test-token"


class _AuthFailingAdapter:
    async def list_models(self, client, endpoint, api_key):
        raise AIError(AIErrorCategory.AUTH_INVALID, "upstream secret diagnostic")


@pytest.mark.asyncio
async def test_probe_account_auth_error_is_actionable_without_upstream_leak(monkeypatch):
    monkeypatch.setattr(
        account_probe,
        "validate_provider_base_url",
        lambda *args, **kwargs: (True, ""),
    )
    monkeypatch.setattr(
        account_probe,
        "get_adapter",
        lambda family: _AuthFailingAdapter(),
    )
    monkeypatch.setattr(
        account_probe,
        "resolve_account_endpoint",
        lambda *args, **kwargs: SimpleNamespace(),
    )

    result = await account_probe.probe_account(
        provider_id="custom",
        protocol="openai-compatible",
        api_base="https://example.com/v1",
        api_key="sk-test-token",
    )

    assert result["success"] is False
    assert "API 鉴权失败" in result["message"]
    assert "令牌权限/渠道" in result["message"]
    assert "upstream secret diagnostic" not in result["message"]
