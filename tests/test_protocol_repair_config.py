"""protocol_repair_max_attempts 配置项测试。"""

from backend.core.config import BASIC_CONFIG_KEYS, Settings


def test_default_value_is_three():
    """Settings 默认 protocol_repair_max_attempts == 3。"""
    settings = Settings()
    assert settings.protocol_repair_max_attempts == 3


def test_key_in_basic_config_keys():
    """键已注册到 BASIC_CONFIG_KEYS，WebUI 基础配置页可加载。"""
    assert "protocol_repair_max_attempts" in BASIC_CONFIG_KEYS
