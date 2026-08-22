"""SetupService.test_github_app 的回归测试。

锁定 GitHub App JWT 测试连接的两个关键行为：
- JWT 的过期时间为 GitHub 10 分钟上限留出时钟偏差裕量；
- GitHub 返回 401 时保留上游错误消息，避免把时间错误误报为凭证无效。
"""

from types import SimpleNamespace

import jwt
import pytest

import backend.core.setup_service as setup_service_module
from backend.core.setup_service import SetupService


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, str]):
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, str]:
        return self._body


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse, captured: dict):
        self._response = response
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    async def get(self, url: str, **kwargs):
        self._captured["url"] = url
        self._captured["kwargs"] = kwargs
        return self._response


def _patch_github_test_dependencies(monkeypatch, response: _FakeResponse):
    captured: dict = {}
    base_timestamp = 1_700_000_000

    def fake_encode(payload, key, algorithm):
        captured["payload"] = payload
        captured["key"] = key
        captured["algorithm"] = algorithm
        return "signed-token"

    monkeypatch.setattr(jwt, "encode", fake_encode)
    monkeypatch.setattr(
        setup_service_module,
        "now_utc",
        lambda: SimpleNamespace(timestamp=lambda: base_timestamp),
    )
    monkeypatch.setattr(
        setup_service_module.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(response, captured),
    )
    return captured, base_timestamp


@pytest.mark.asyncio
async def test_github_app_jwt_uses_clock_skew_margin_and_normalizes_inputs(monkeypatch):
    captured, base_timestamp = _patch_github_test_dependencies(
        monkeypatch,
        _FakeResponse(200, {"name": "Test App", "slug": "test-app"}),
    )

    result = await SetupService().test_github_app(
        " 4592967 ",
        "-----BEGIN RSA PRIVATE KEY-----\\nbody\\n-----END RSA PRIVATE KEY-----",
    )

    assert result["success"] is True
    assert captured["payload"] == {
        "iat": base_timestamp - 60,
        "exp": base_timestamp + (9 * 60),
        "iss": "4592967",
    }
    assert captured["key"] == (
        "-----BEGIN RSA PRIVATE KEY-----\nbody\n-----END RSA PRIVATE KEY-----"
    )
    assert captured["algorithm"] == "RS256"
    assert captured["url"] == "https://api.github.com/app"
    assert captured["kwargs"]["headers"] == {
        "Authorization": "Bearer signed-token",
        "Accept": "application/vnd.github+json",
    }
    assert captured["kwargs"]["timeout"] == 10


@pytest.mark.asyncio
async def test_github_app_401_preserves_github_error_message(monkeypatch):
    github_error = "'Expiration time' claim ('exp') is too far in the future"
    captured, _ = _patch_github_test_dependencies(
        monkeypatch,
        _FakeResponse(401, {"message": github_error}),
    )

    result = await SetupService().test_github_app("4592967", "PEM")

    assert result["success"] is False
    assert github_error in result["message"]
    assert "凭证无效，请检查 App ID 和 Private Key" not in result["message"]
    assert captured["payload"]["exp"] - captured["payload"]["iat"] == 600
