"""Agent 专家团队模式基础测试"""

import pytest

from backend.services.agent_team.ai_client import AgentTeamAIConfig
from backend.webui.routes.agent_team import (
    AGENT_TEAM_ACTIVE_STATUSES,
    AGENT_TEAM_CONFIG_KEYS,
    _group_config_items,
)


def test_agent_team_ai_config_requires_dedicated_values():
    config = AgentTeamAIConfig(
        provider="openai",
        api_base="",
        api_key="",
        model="",
        review_model="",
        summary_model="",
        temperature=0.2,
        max_tokens=8192,
        timeout_seconds=600,
    )

    with pytest.raises(ValueError) as exc:
        config.validate()

    message = str(exc.value)
    assert "agent_team_api_base" in message
    assert "agent_team_api_key" in message
    assert "agent_team_model" in message
    assert "agent_team_review_model" in message


def test_agent_team_ai_config_safe_snapshot_masks_key():
    config = AgentTeamAIConfig(
        provider="openai",
        api_base="https://example.test/v1",
        api_key="secret-key",
        model="fullstack-model",
        review_model="review-model",
        summary_model="summary-model",
        temperature=0.2,
        max_tokens=8192,
        timeout_seconds=600,
    )

    snapshot = config.safe_snapshot()

    assert snapshot["api_key_set"] is True
    assert "secret-key" not in str(snapshot)
    assert snapshot["model"] == "fullstack-model"
    assert snapshot["review_model"] == "review-model"


def test_agent_team_config_includes_required_dedicated_ai_keys():
    required = {
        "agent_team_api_base",
        "agent_team_api_key",
        "agent_team_model",
        "agent_team_review_model",
        "agent_team_enabled",
        "agent_team_workspace_root",
    }

    assert required.issubset(set(AGENT_TEAM_CONFIG_KEYS))


def test_agent_team_active_statuses_include_waiting_human():
    assert "queued" in AGENT_TEAM_ACTIVE_STATUSES
    assert "waiting_human" in AGENT_TEAM_ACTIVE_STATUSES
    assert "completed" not in AGENT_TEAM_ACTIVE_STATUSES


def test_agent_team_config_grouping_preserves_all_items():
    config_items = [{"key": key} for key in AGENT_TEAM_CONFIG_KEYS]

    groups = _group_config_items(config_items, lang="zh-CN")
    grouped_keys = [item["key"] for group in groups for item in group["items"]]

    assert grouped_keys == AGENT_TEAM_CONFIG_KEYS
    assert [group["key"] for group in groups] == ["basic", "ai", "guardrails"]
