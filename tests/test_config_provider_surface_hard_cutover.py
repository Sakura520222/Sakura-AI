"""供应商协议硬切换的配置 surface 回归测试。"""

import pytest

from backend.core import config as config_module
from backend.core.config import (
    AI_STRATEGY_CONFIG_KEYS,
    BASIC_CONFIG_KEYS,
    CORE_CONFIG_KEYS,
    DYNAMIC_CONFIG_GROUPS,
    DYNAMIC_CONFIG_LABELS,
    DYNAMIC_CONFIG_RANGES,
    DYNAMIC_CONFIG_SELECT_OPTIONS,
    DYNAMIC_CONFIG_SENSITIVE_KEYS,
    Settings,
    get_all_db_config_keys,
    get_all_dynamic_config_keys,
    get_dynamic_config,
)
from backend.webui.routes.agent_team import (
    AGENT_TEAM_CONFIG_GROUPS,
    AGENT_TEAM_CONFIG_KEYS,
)

LEGACY_SUPPLIER_KEYS = {
    "ai_provider",
    "openai_api_base",
    "openai_api_key",
    "openai_model",
    "openai_temperature",
    "openai_max_tokens",
    "summary_provider",
    "summary_api_base",
    "summary_api_key",
    "summary_model",
    "agent_team_model_provider",
    "agent_team_api_base",
    "agent_team_api_key",
    "agent_team_model",
    "agent_team_review_model",
    "agent_team_summary_model",
    "scan_model",
}


def test_settings_and_config_registries_drop_legacy_supplier_keys():
    settings = Settings()

    assert LEGACY_SUPPLIER_KEYS.isdisjoint(Settings.model_fields)
    assert all(not hasattr(settings, key) for key in LEGACY_SUPPLIER_KEYS)

    registries = (
        set(get_all_dynamic_config_keys()),
        set(get_all_db_config_keys()),
        set(DYNAMIC_CONFIG_LABELS),
        set(DYNAMIC_CONFIG_RANGES),
        set(DYNAMIC_CONFIG_SELECT_OPTIONS),
        set(DYNAMIC_CONFIG_SENSITIVE_KEYS),
        set(CORE_CONFIG_KEYS),
        set(BASIC_CONFIG_KEYS),
    )
    for registry in registries:
        assert LEGACY_SUPPLIER_KEYS.isdisjoint(registry)

    assert {"ai_temperature", "ai_max_tokens"}.issubset(Settings.model_fields)
    assert {
        "agent_team_temperature",
        "agent_team_max_tokens",
        "agent_team_timeout_seconds",
    }.issubset(Settings.model_fields)
    assert {
        "agent_team_temperature",
        "agent_team_max_tokens",
        "agent_team_timeout_seconds",
    }.issubset(DYNAMIC_CONFIG_GROUPS["agent_team"]["keys"])


def test_agent_team_webui_surface_excludes_legacy_and_retired_ai_keys():
    assert LEGACY_SUPPLIER_KEYS.isdisjoint(AGENT_TEAM_CONFIG_KEYS)
    grouped_keys = {key for group in AGENT_TEAM_CONFIG_GROUPS for key in group["keys"]}
    assert grouped_keys == set(AGENT_TEAM_CONFIG_KEYS)
    assert LEGACY_SUPPLIER_KEYS.isdisjoint(grouped_keys)

    # 温度/max_tokens/超时/压缩等模型相关配置已迁至新版 /config/ai 角色绑定，
    # /agent-team 配置页不再暴露「专用 AI 模型」选项卡（原 ai group）。
    retired_ai_keys = {
        "agent_team_temperature",
        "agent_team_max_tokens",
        "agent_team_enable_context_compression",
        "agent_team_context_compression_threshold",
        "agent_team_context_summary_max_tokens",
        "agent_team_timeout_seconds",
    }
    assert retired_ai_keys.isdisjoint(AGENT_TEAM_CONFIG_KEYS)
    assert retired_ai_keys.isdisjoint(grouped_keys)
    assert "ai" not in {group["key"] for group in AGENT_TEAM_CONFIG_GROUPS}


def test_ai_strategy_registry_keeps_request_policy_fields():
    assert {"ai_api_timeout_seconds", "ai_api_max_retries"}.issubset(
        AI_STRATEGY_CONFIG_KEYS
    )
    assert {"ai_temperature", "ai_max_tokens"}.isdisjoint(AI_STRATEGY_CONFIG_KEYS)


@pytest.mark.asyncio
async def test_get_dynamic_config_does_not_read_historical_supplier_keys(monkeypatch):
    async def fail_if_read(key: str):
        raise AssertionError(f"historical key must not reach DB: {key}")

    monkeypatch.setattr(config_module, "_read_config_from_db", fail_if_read)
    config_module.invalidate_dynamic_config_cache()

    for key in LEGACY_SUPPLIER_KEYS:
        assert await get_dynamic_config(key) is None
