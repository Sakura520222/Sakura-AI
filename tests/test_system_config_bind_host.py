"""监听地址 app_host 的校验、分组归属与 Setup 映射测试。

app_host 与 app_port 共享「系统核心配置」页 application 分组与
RESTART_REQUIRED_KEYS 语义；格式校验规则由
SystemConfigService.validate_bind_host 单一定义，配置备份导入复用。
"""

from __future__ import annotations

import pytest

from backend.core.setup_service import SetupService
from backend.services.system_config_service import (
    RESTART_REQUIRED_KEYS,
    SYSTEM_CONFIG_GROUPS,
    SYSTEM_CONFIG_KEYS,
    SystemConfigService,
    SystemConfigValidationError,
    validate_bind_host,
)


def test_app_host_belongs_to_application_group_and_requires_restart():
    app_group = next(g for g in SYSTEM_CONFIG_GROUPS if g["id"] == "application")
    keys = app_group["keys"]

    assert "app_host" in keys
    assert "app_host" in SYSTEM_CONFIG_KEYS
    assert "app_host" in RESTART_REQUIRED_KEYS
    # 与 app_domain/app_port 相邻展示
    assert keys.index("app_domain") < keys.index("app_host") < keys.index("app_port")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("0.0.0.0", "0.0.0.0"),
        ("192.168.1.10", "192.168.1.10"),
        ("::1", "::1"),
        ("::", "::"),
        ("localhost", "localhost"),
        ("  localhost  ", "localhost"),
        ("LOCALHOST", "localhost"),
        ("srv.example.com", "srv.example.com"),
        ("  10.0.0.8 ", "10.0.0.8"),
    ],
)
def test_validate_bind_host_accepts_and_normalizes(raw, expected):
    assert validate_bind_host(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not a host!",
        "999.999.999.999",
        "-leading-hyphen.example.com",
        "bad_host_name!",
        "a" * 300,
        "under_score.example.com",
    ],
)
def test_validate_bind_host_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        validate_bind_host(raw)


def test_system_config_validate_updates_normalizes_app_host():
    validated = SystemConfigService.validate_updates({"app_host": " LOCALHOST "})

    assert validated["app_host"] == "localhost"


def test_system_config_validate_updates_rejects_invalid_app_host():
    with pytest.raises(SystemConfigValidationError) as exc_info:
        SystemConfigService.validate_updates({"app_host": "not a host!"})

    assert exc_info.value.key == "app_host"
    assert exc_info.value.toast_key == "system_config.invalid_bind_host"


def test_system_config_validate_updates_rejects_empty_app_host():
    with pytest.raises(SystemConfigValidationError):
        SystemConfigService.validate_updates({"app_host": ""})


def test_setup_collects_app_host_env_spelling():
    items = SetupService._collect_config_items({"APP_HOST": " 0.0.0.0 "})

    assert items == {"app_host": "0.0.0.0"}


def test_setup_validate_rejects_invalid_app_host():
    with pytest.raises(SystemConfigValidationError):
        SetupService._validate_config_items({"APP_HOST": "not a host!"})


def test_setup_validate_normalizes_app_host():
    items = SetupService._validate_config_items({"APP_HOST": "LOCALHOST"})

    assert items["app_host"] == "localhost"
