"""Main app helper tests"""

import time
from unittest.mock import patch

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

import backend.main as main
from backend.core.config import Settings
from backend.main import _get_webui_error_user
from backend.main import _get_allowed_origins, _should_start_background_tasks
from backend.main import get_startup_info, _format_duration


class RequestStub:
    def __init__(self, token: str | None):
        self.cookies = {"webui_token": token} if token else {}


def test_get_webui_error_user_decodes_cookie_payload():
    payload = {
        "sub": "octocat",
        "role": "super_admin",
        "user_id": 123,
        "github_id": 456,
        "avatar_url": "https://example.com/avatar.png",
    }

    with patch("backend.main.decode_access_token", return_value=payload):
        user = _get_webui_error_user(RequestStub("token"))

    assert user == payload


def test_get_webui_error_user_returns_none_without_valid_payload():
    with patch("backend.main.decode_access_token", return_value=None):
        assert _get_webui_error_user(RequestStub("token")) is None

    assert _get_webui_error_user(RequestStub(None)) is None


def test_get_webui_error_user_returns_none_when_decode_fails():
    with patch("backend.main.decode_access_token", side_effect=RuntimeError("boom")):
        assert _get_webui_error_user(RequestStub("token")) is None


def test_get_allowed_origins_includes_local_development_origins():
    settings = Settings(
        sakura_env="development",
        app_domain="example.com",
        app_port=9000,
    )

    origins = _get_allowed_origins(settings)

    assert "https://example.com" in origins
    assert "http://localhost:9000" in origins
    assert "http://127.0.0.1:9000" in origins


def test_get_allowed_origins_production_excludes_localhost():
    settings = Settings(app_domain="example.com", app_port=9000)

    origins = _get_allowed_origins(settings)

    assert "https://example.com" in origins
    assert "http://localhost:9000" not in origins
    assert "http://127.0.0.1:9000" not in origins


def test_background_tasks_can_be_skipped_for_local_development():
    settings = Settings(sakura_skip_background_tasks=True)

    assert _should_start_background_tasks(settings) is False
    assert _should_start_background_tasks(Settings(sakura_dev_bootstrap=True)) is False
    assert _should_start_background_tasks(Settings()) is True


@pytest.mark.anyio
async def test_rate_limit_handler_returns_sync_slowapi_response_without_await(monkeypatch):
    expected = JSONResponse({"detail": "rate limit"}, status_code=429)

    def fake_rate_limit_exceeded_handler(_request, _exc):
        return expected

    monkeypatch.setattr(
        main,
        "_rate_limit_exceeded_handler",
        fake_rate_limit_exceeded_handler,
    )

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/example",
            "headers": [(b"accept", b"application/json")],
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 123),
        }
    )

    response = await main.rate_limit_exception_handler(request, object())

    assert response is expected


def test_format_duration_milliseconds():
    assert _format_duration(0.5) == "500ms"
    assert _format_duration(0.123) == "123ms"
    assert _format_duration(0) == "0ms"


def test_format_duration_seconds():
    assert _format_duration(1) == "1.0s"
    assert _format_duration(5.5) == "5.5s"
    assert _format_duration(59.9) == "59.9s"


def test_format_duration_minutes():
    assert _format_duration(60) == "1m 0s"
    assert _format_duration(90) == "1m 30s"
    assert _format_duration(3599) == "59m 59s"


def test_format_duration_hours():
    assert _format_duration(3600) == "1h 0m 0s"
    assert _format_duration(3661) == "1h 1m 1s"
    assert _format_duration(7200) == "2h 0m 0s"


def test_get_startup_info_returns_none_when_not_started():
    """lifespan 未执行时，startup_info 中 startup_time 应为 None"""
    with patch.object(main, "_startup_finished_at", 0.0), patch.object(
        main, "_startup_duration", 0.0
    ):
        info = get_startup_info()
    assert info["startup_time"] is None
    assert info["startup_duration_seconds"] == 0.0
    assert info["uptime_seconds"] == 0


def test_get_startup_info_returns_valid_data_after_startup():
    """模拟 lifespan 已执行后，startup_info 应包含有效数据"""
    now = time.time()
    fake_finished = now - 120  # 2 分钟前启动完成
    fake_duration = 3.45  # 启动耗时 3.45 秒

    with patch.object(main, "_startup_finished_at", fake_finished), patch.object(
        main, "_startup_duration", fake_duration
    ):
        info = get_startup_info()

    assert info["startup_time"] is not None
    assert info["startup_time"].endswith("Z")
    assert info["startup_duration_seconds"] == 3.45
    # uptime 应大约为 120 秒（允许 ±1 秒误差）
    assert 119 <= info["uptime_seconds"] <= 121
