"""供应商协议硬切换的配置 surface 回归测试。"""

from pathlib import Path

import pytest

from backend.api.v1.config import _AI_STRATEGY_RANGES, AIStrategyRequest
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
from backend.webui.routes.agent_team import router as agent_team_router

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
    # Agent 专属任务时限和轮数上限不再属于 Settings 或动态配置 surface。
    removed_agent_limits = {
        "agent_team_timeout_seconds",
        "agent_team_max_iterations_per_task",
    }
    assert removed_agent_limits.isdisjoint(Settings.model_fields)
    assert removed_agent_limits.isdisjoint(DYNAMIC_CONFIG_GROUPS["agent_team"]["keys"])
    assert removed_agent_limits.isdisjoint(DYNAMIC_CONFIG_LABELS)
    assert removed_agent_limits.isdisjoint(DYNAMIC_CONFIG_RANGES)


def test_agent_team_webui_surface_excludes_legacy_supplier_keys():
    agent_team_keys = set(DYNAMIC_CONFIG_GROUPS["agent_team"]["keys"])
    assert LEGACY_SUPPLIER_KEYS.isdisjoint(agent_team_keys)

    from fastapi.routing import APIRoute

    assert all(
        not (isinstance(route, APIRoute) and route.path == "/agent-team/config/save")
        for route in agent_team_router.routes
    )


def test_ai_strategy_registry_keeps_request_policy_fields():
    assert {"ai_api_timeout_seconds", "ai_api_max_retries"}.issubset(
        AI_STRATEGY_CONFIG_KEYS
    )
    assert {"ai_temperature", "ai_max_tokens"}.isdisjoint(AI_STRATEGY_CONFIG_KEYS)


def test_ai_retry_zero_is_valid_in_api_and_webui():
    """The retry field is a post-initial-failure count, so zero is valid."""
    request = AIStrategyRequest(ai_api_max_retries=0)
    assert request.ai_api_max_retries == 0
    assert _AI_STRATEGY_RANGES["ai_api_max_retries"] == (0, 20)

    template = (
        Path(__file__).parents[1] / "backend" / "webui" / "templates" / "config_ai.html"
    ).read_text(encoding="utf-8")
    assert 'min="0" max="20" x-model.number="strategy.ai_api_max_retries"' in template


@pytest.mark.asyncio
async def test_get_dynamic_config_does_not_read_historical_supplier_keys(monkeypatch):
    async def fail_if_read(key: str):
        raise AssertionError(f"historical key must not reach DB: {key}")

    monkeypatch.setattr(config_module, "_read_config_from_db", fail_if_read)
    config_module.invalidate_dynamic_config_cache()

    for key in LEGACY_SUPPLIER_KEYS:
        assert await get_dynamic_config(key) is None
