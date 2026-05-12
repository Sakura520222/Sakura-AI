"""AI API client dynamic configuration coverage."""

from types import SimpleNamespace

import pytest

from backend.core.config import get_settings
from backend.services.ai_reviewer.api_client import AIApiClient


class _FakeCompletions:
    def __init__(self):
        self.kwargs = None
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        raise RuntimeError("boom")


class _FakeChat:
    def __init__(self):
        self.completions = _FakeCompletions()


class _FakeOpenAIClient:
    def __init__(self):
        self.chat = _FakeChat()


@pytest.mark.asyncio
async def test_call_with_retry_uses_dynamic_timeout_and_retry(monkeypatch):
    settings = get_settings()
    old_values = {
        "ai_api_timeout_seconds": settings.ai_api_timeout_seconds,
        "ai_api_max_retries": settings.ai_api_max_retries,
    }
    try:
        settings.ai_api_timeout_seconds = 3.5
        settings.ai_api_max_retries = 1

        sleep_calls = []

        async def fake_sleep(delay):
            sleep_calls.append(delay)

        monkeypatch.setattr(
            "backend.services.ai_reviewer.api_client.asyncio.sleep", fake_sleep
        )

        api_client = AIApiClient("https://example.invalid/v1", "test-key")
        fake_client = _FakeOpenAIClient()
        api_client.client = fake_client

        with pytest.raises(RuntimeError, match="boom"):
            await api_client.call_with_retry(
                messages=[{"role": "user", "content": "hi"}],
                model="test-model",
            )

        assert fake_client.chat.completions.calls == 1
        assert fake_client.chat.completions.kwargs["timeout"] == 3.5
        assert sleep_calls == []
    finally:
        for key, value in old_values.items():
            setattr(settings, key, value)


def test_calculate_delay_uses_dynamic_initial_delay(monkeypatch):
    settings = get_settings()
    old_value = settings.ai_api_initial_retry_delay_seconds
    try:
        settings.ai_api_initial_retry_delay_seconds = 2.0
        monkeypatch.setattr(
            "backend.services.ai_reviewer.api_client.random.uniform", lambda _a, _b: 1.0
        )

        api_client = AIApiClient("https://example.invalid/v1", "test-key")

        assert api_client._calculate_delay(0) == 2.0
        assert api_client._calculate_delay(1) == 4.0
        assert api_client._calculate_delay(3) == 16.0
    finally:
        settings.ai_api_initial_retry_delay_seconds = old_value


def test_estimate_prompt_tokens_supports_sdk_tool_call_objects():
    tool_call = SimpleNamespace(
        function=SimpleNamespace(
            name="fetch_url",
            arguments='{"url":"https://example.com"}',
        )
    )

    tokens = AIApiClient._estimate_prompt_tokens(
        [
            {"role": "user", "content": "读取网页"},
            {"role": "assistant", "content": None, "tool_calls": [tool_call]},
        ]
    )

    assert tokens > 0


def test_estimate_prompt_tokens_supports_dict_tool_calls():
    tokens = AIApiClient._estimate_prompt_tokens(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "search_web",
                            "arguments": '{"query":"动态配置"}',
                        }
                    }
                ],
            }
        ]
    )

    assert tokens > 0
