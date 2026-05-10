"""Agent 专家团队模式基础测试"""

from types import SimpleNamespace

import pytest

from backend.services.agent_team.ai_client import AgentTeamAIConfig, load_agent_team_ai_config
from backend.services.agent_team.candidate_service import AgentCandidate, _parse_ai_filter_response, candidates_to_dicts
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
    assert "agent_team_api_base 或 openai_api_base" in message
    assert "agent_team_api_key 或 openai_api_key" in message
    assert "agent_team_model 或 openai_model" in message
    assert "agent_team_review_model 或 agent_team_model/openai_model" in message


@pytest.mark.asyncio
async def test_load_agent_team_ai_config_uses_main_ai_when_selected(monkeypatch):
    async def fake_get_dynamic_config(key: str):
        values = {
            "agent_team_model_provider": "main",
            "agent_team_api_base": "",
            "agent_team_api_key": "",
            "agent_team_model": "",
            "agent_team_review_model": "",
            "agent_team_summary_model": "",
            "ai_provider": "deepseek",
            "openai_api_base": "https://main.example/v1",
            "openai_api_key": "main-key",
            "openai_model": "main-model",
            "summary_model": "summary-model",
            "agent_team_temperature": 0.25,
            "agent_team_max_tokens": 4096,
            "agent_team_timeout_seconds": 300,
        }
        return values.get(key)

    monkeypatch.setattr("backend.services.agent_team.ai_client.get_dynamic_config", fake_get_dynamic_config)
    monkeypatch.setattr(
        "backend.services.agent_team.ai_client.get_settings",
        lambda: SimpleNamespace(
            ai_provider="openai",
            openai_api_base="https://settings.example/v1",
            openai_api_key="settings-key",
            openai_model="settings-model",
            summary_model="settings-summary-model",
        ),
    )

    config = await load_agent_team_ai_config()

    assert config.provider == "deepseek"
    assert config.api_base == "https://main.example/v1"
    assert config.api_key == "main-key"
    assert config.model == "main-model"
    assert config.review_model == "main-model"
    assert config.summary_model == "summary-model"
    assert config.temperature == 0.25
    assert config.max_tokens == 4096
    assert config.timeout_seconds == 300


@pytest.mark.asyncio
async def test_load_agent_team_ai_config_uses_independent_agent_values(monkeypatch):
    async def fake_get_dynamic_config(key: str):
        values = {
            "agent_team_model_provider": "qwen",
            "agent_team_api_base": "https://agent.example/v1",
            "agent_team_api_key": "agent-key",
            "agent_team_model": "agent-model",
            "agent_team_review_model": "agent-review-model",
            "agent_team_summary_model": "agent-summary-model",
            "ai_provider": "deepseek",
            "openai_api_base": "https://main.example/v1",
            "openai_api_key": "main-key",
            "openai_model": "main-model",
            "summary_model": "summary-model",
            "agent_team_temperature": 0.2,
            "agent_team_max_tokens": 8192,
            "agent_team_timeout_seconds": 600,
        }
        return values.get(key)

    monkeypatch.setattr("backend.services.agent_team.ai_client.get_dynamic_config", fake_get_dynamic_config)
    monkeypatch.setattr(
        "backend.services.agent_team.ai_client.get_settings",
        lambda: SimpleNamespace(
            ai_provider="openai",
            openai_api_base="https://settings.example/v1",
            openai_api_key="settings-key",
            openai_model="settings-model",
            summary_model="settings-summary-model",
        ),
    )

    config = await load_agent_team_ai_config()

    assert config.provider == "qwen"
    assert config.api_base == "https://agent.example/v1"
    assert config.api_key == "agent-key"
    assert config.model == "agent-model"
    assert config.review_model == "agent-review-model"
    assert config.summary_model == "agent-summary-model"


def test_agent_team_provider_options_include_main_ai_choice():
    from backend.core.config import DYNAMIC_CONFIG_SELECT_OPTIONS

    options = DYNAMIC_CONFIG_SELECT_OPTIONS["agent_team_model_provider"]

    assert options[0]["value"] == "main"


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


def test_agent_candidate_dict_includes_ai_filter_reason():
    candidate = AgentCandidate(
        source_type="issue_analysis",
        source_id=1,
        source_issue_number=10,
        repo_full_name="owner/repo",
        repo_owner="owner",
        repo_name="repo",
        title="Fix bug",
        summary="Small bug",
        priority="high",
        candidate_score=88,
        filter_reason="符合低风险快速修复要求",
    )

    data = candidates_to_dicts([candidate])[0]

    assert data["filter_reason"] == "符合低风险快速修复要求"


def test_parse_ai_filter_response_normalizes_values():
    response = """
    ```json
    [
      {"source_id": "1", "selected": true, "score": 120, "priority": "urgent", "reason": "Good fit"},
      {"source_id": 2, "selected": "false", "score": -5, "priority": "low", "reason": "Skip"}
    ]
    ```
    """

    items = _parse_ai_filter_response(response)

    assert items[0] == {
        "source_id": 1,
        "selected": True,
        "score": 100,
        "priority": "medium",
        "reason": "Good fit",
    }
    assert items[1]["selected"] is False
    assert items[1]["score"] == 0
    assert items[1]["priority"] == "low"


def test_parse_ai_filter_response_accepts_wrapped_results():
    items = _parse_ai_filter_response('{"results": [{"source_id": 7, "score": 90, "priority": "high"}]}')

    assert items == [
        {
            "source_id": 7,
            "selected": True,
            "score": 90,
            "priority": "high",
            "reason": "",
        }
    ]
