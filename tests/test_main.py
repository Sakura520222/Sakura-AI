"""Main app helper tests"""

from datetime import datetime
import time
from unittest.mock import Mock, call, patch

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from backend import main
from backend.core.config import Settings
from backend.main import (
    _format_duration,
    _get_allowed_origins,
    _get_webui_error_user,
    _should_start_background_tasks,
    get_startup_info,
    get_system_info_dict,
)


def test_create_startup_log_file_is_unique_for_each_start(tmp_path):
    startup_time = datetime(2026, 8, 1, 12, 34, 56, 123456)

    first_log = main._create_startup_log_file(
        tmp_path,
        started_at=startup_time,
        process_id=4321,
    )
    second_log = main._create_startup_log_file(
        tmp_path,
        started_at=startup_time,
        process_id=4321,
    )

    assert first_log.name == "app_20260801_123456_123456_pid4321.log"
    assert second_log.name == "app_20260801_123456_123456_pid4321_1.log"
    assert first_log.is_file()
    assert second_log.is_file()


def test_configure_logging_uses_a_new_file_for_each_start(monkeypatch, tmp_path):
    remove = Mock()
    add = Mock()
    install_filter = Mock()
    app_log_path = tmp_path / "app_20260801_123456_123456_pid4321.log"
    cleanup = Mock()
    monkeypatch.setattr(main.logger, "remove", remove)
    monkeypatch.setattr(main.logger, "add", add)
    monkeypatch.setattr(main, "install_quiet_successful_access_filter", install_filter)
    monkeypatch.setattr(main, "_cleanup_expired_app_logs", cleanup)
    monkeypatch.setattr(main, "_create_startup_log_file", Mock(return_value=app_log_path))

    main.configure_logging()

    assert add.call_args_list[1] == call(
        str(app_log_path),
        rotation="500 MB",
        retention=cleanup,
        level="DEBUG",
    )
    install_filter.assert_called_once()
    remove.assert_called_once()
    cleanup.assert_called_once()


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
async def test_rate_limit_handler_returns_sync_slowapi_response_without_await(
    monkeypatch,
):
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
    with (
        patch.object(main, "_startup_finished_at", 0.0),
        patch.object(main, "_startup_duration", 0.0),
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

    with (
        patch.object(main, "_startup_finished_at", fake_finished),
        patch.object(main, "_startup_duration", fake_duration),
    ):
        info = get_startup_info()

    assert info["startup_time"] is not None
    assert info["startup_time"].endswith("Z")
    assert info["startup_duration_seconds"] == 3.45
    # uptime 应大约为 120 秒（允许 ±1 秒误差）
    assert 119 <= info["uptime_seconds"] <= 121


def test_get_system_info_dict_contains_formatted_fields():
    """get_system_info_dict 应包含所有格式化字段和版本号"""
    now = time.time()
    fake_finished = now - 3600  # 1 小时前启动完成
    fake_duration = 2.5  # 启动耗时 2.5 秒

    with (
        patch.object(main, "_startup_finished_at", fake_finished),
        patch.object(main, "_startup_duration", fake_duration),
    ):
        info = get_system_info_dict()

    assert info["startup_time"] is not None
    assert info["startup_duration_seconds"] == 2.5
    assert info["startup_duration_formatted"] == "2.5s"
    assert 3599 <= info["uptime_seconds"] <= 3601
    assert info["uptime_formatted"] is not None and "h" in info["uptime_formatted"]
    assert info["version"] is not None


def test_get_system_info_dict_before_startup():
    """lifespan 未执行时，get_system_info_dict 应返回合理的默认值"""
    with (
        patch.object(main, "_startup_finished_at", 0.0),
        patch.object(main, "_startup_duration", 0.0),
    ):
        info = get_system_info_dict()

    assert info["startup_time"] is None
    assert info["startup_duration_seconds"] == 0.0
    assert info["startup_duration_formatted"] == "0ms"
    assert info["uptime_seconds"] == 0
    assert info["uptime_formatted"] == "0ms"
    assert info["version"] is not None
