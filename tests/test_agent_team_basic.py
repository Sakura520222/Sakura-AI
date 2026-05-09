"""Agent 专家团队模式基础测试"""

import pytest

from backend.services.agent_team.ai_client import AgentTeamAIConfig
from backend.webui.routes.agent_team import AGENT_TEAM_CONFIG_KEYS


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
    }

    assert required.issubset(set(AGENT_TEAM_CONFIG_KEYS))
