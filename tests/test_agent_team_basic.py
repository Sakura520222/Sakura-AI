"""Agent 专家团队模式基础测试"""

import pickle
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.services.agent_team.ai_client import (
    AgentTeamAIConfig,
    load_agent_team_ai_config,
)
from backend.services.agent_team.candidate_service import (
    AgentCandidate,
    _parse_ai_filter_response,
    _select_ai_filter_model,
    candidates_to_dicts,
)
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

    monkeypatch.setattr(
        "backend.services.agent_team.ai_client.get_dynamic_config",
        fake_get_dynamic_config,
    )
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

    monkeypatch.setattr(
        "backend.services.agent_team.ai_client.get_dynamic_config",
        fake_get_dynamic_config,
    )
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


def test_agent_team_max_tokens_range_is_provider_safe():
    from backend.core.config import DYNAMIC_CONFIG_RANGES

    assert DYNAMIC_CONFIG_RANGES["agent_team_max_tokens"] == (1024, 32768)


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


def test_agent_team_ai_config_safe_dict_and_getstate_mask_key():
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

    assert "secret-key" not in str(config.as_safe_dict())
    assert "secret-key" not in str(config.__getstate__())
    assert config.as_safe_dict()["api_key_set"] is True


def test_agent_team_ai_config_pickle_roundtrip_masks_key():
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

    restored = pickle.loads(pickle.dumps(config))

    assert restored.api_key == ""
    assert restored.provider == "openai"
    assert restored.model == "fullstack-model"
    assert restored.review_model == "review-model"
    assert restored.summary_model == "summary-model"
    assert "secret-key" not in str(restored.safe_snapshot())


def test_agent_team_ai_config_setstate_ignores_api_key():
    config = AgentTeamAIConfig.__new__(AgentTeamAIConfig)

    config.__setstate__({"provider": "openai", "api_key": "leaked-key"})

    assert config.provider == "openai"
    assert config.api_key == ""


@pytest.mark.asyncio
async def test_load_agent_team_ai_config_preserves_explicit_zero_values(monkeypatch):
    async def fake_get_dynamic_config(key: str):
        values = {
            "agent_team_model_provider": "qwen",
            "agent_team_api_base": "https://agent.example/v1",
            "agent_team_api_key": "agent-key",
            "agent_team_model": "agent-model",
            "agent_team_review_model": "",
            "agent_team_summary_model": "",
            "agent_team_temperature": 0,
            "agent_team_max_tokens": 0,
            "agent_team_timeout_seconds": 0,
        }
        return values.get(key)

    monkeypatch.setattr(
        "backend.services.agent_team.ai_client.get_dynamic_config",
        fake_get_dynamic_config,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.ai_client.get_settings",
        lambda: SimpleNamespace(
            ai_provider="openai",
            openai_api_base="https://settings.example/v1",
            openai_api_key="settings-key",
            openai_model="settings-model",
            summary_model="settings-summary-model",
            agent_team_test_command_allowlist="pytest -q",
        ),
    )

    config = await load_agent_team_ai_config()

    assert config.temperature == 0
    assert config.max_tokens == 0
    assert config.timeout_seconds == 0
    assert config.review_model == "agent-model"
    assert config.summary_model == "agent-model"


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
    assert [group["key"] for group in groups] == ["basic", "ai", "guardrails", "skills"]


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
    items = _parse_ai_filter_response(
        '{"results": [{"source_id": 7, "score": 90, "priority": "high"}]}'
    )

    assert items == [
        {
            "source_id": 7,
            "selected": True,
            "score": 90,
            "priority": "high",
            "reason": "",
        }
    ]


def test_ai_filter_model_prefers_main_agent_model():
    model = _select_ai_filter_model(
        model="deepseek-chat",
        review_model="review-model",
        summary_model="invalid-summary-alias",
    )

    assert model == "deepseek-chat"


def test_ai_filter_model_falls_back_when_main_empty():
    assert (
        _select_ai_filter_model("", "review-model", "summary-model") == "review-model"
    )


# ── 候选池去重与 GitHub 状态过滤 ────────────────────────


def _make_candidate(
    issue_number: int = 10,
    repo: str = "owner/repo",
    score: int = 50,
    source_id: int = 1,
    source_type: str = "issue_analysis",
) -> AgentCandidate:
    owner, name = repo.split("/", 1) if "/" in repo else ("", repo)
    return AgentCandidate(
        source_type=source_type,
        source_id=source_id,
        source_issue_number=issue_number,
        repo_full_name=repo,
        repo_owner=owner,
        repo_name=name,
        title=f"Issue #{issue_number}",
        summary="",
        priority="medium",
        candidate_score=score,
    )


def test_deduplicate_candidates_keeps_highest_score():
    from backend.services.agent_team.candidate_service import AgentTeamCandidateService

    service = AgentTeamCandidateService()
    candidates = [
        _make_candidate(issue_number=10, score=50, source_id=1),
        _make_candidate(issue_number=10, score=80, source_id=2),
        _make_candidate(issue_number=10, score=60, source_id=3),
    ]

    result = service._deduplicate_candidates(candidates)

    assert len(result) == 1
    assert result[0].candidate_score == 80
    assert result[0].source_id == 2


def test_deduplicate_candidates_preserves_different_issues():
    from backend.services.agent_team.candidate_service import AgentTeamCandidateService

    service = AgentTeamCandidateService()
    candidates = [
        _make_candidate(issue_number=10, repo="owner/repo"),
        _make_candidate(issue_number=11, repo="owner/repo"),
        _make_candidate(issue_number=10, repo="other/repo"),
    ]

    result = service._deduplicate_candidates(candidates)

    assert len(result) == 3


def test_deduplicate_candidates_handles_none_issue_number():
    from backend.services.agent_team.candidate_service import AgentTeamCandidateService

    service = AgentTeamCandidateService()
    candidates = [
        _make_candidate(issue_number=10),
        AgentCandidate(
            source_type="scan_finding",
            source_id=100,
            source_issue_number=None,
            repo_full_name="owner/repo",
            repo_owner="owner",
            repo_name="repo",
            title="Finding",
            summary="",
            priority="high",
            candidate_score=90,
        ),
    ]

    result = service._deduplicate_candidates(candidates)

    # None issue_number 不应与有值冲突
    assert len(result) == 2


@pytest.mark.asyncio
async def test_filter_closed_issues_removes_closed(monkeypatch):
    from backend.services.agent_team.candidate_service import AgentTeamCandidateService

    service = AgentTeamCandidateService()

    closed_issue = MagicMock()
    closed_issue.state = "closed"
    open_issue = MagicMock()
    open_issue.state = "open"

    mock_app = MagicMock()
    mock_app.get_issue = MagicMock(side_effect=[closed_issue, open_issue])

    # monkeypatch 修改模块级属性 GitHubAppClient；延迟导入 `from ... import GitHubAppClient`
    # 在函数体内执行时，Python 会从已加载的 sys.modules 中查找模块，因此 patch
    # 模块属性即可影响后续延迟导入的行为。
    monkeypatch.setattr(
        "backend.core.github_app.GitHubAppClient",
        lambda: mock_app,
    )

    candidates = [
        _make_candidate(issue_number=10, source_type="scan_finding"),
        _make_candidate(issue_number=11, source_type="scan_finding"),
    ]

    result = await service._filter_closed_issues(candidates)

    assert len(result) == 1
    assert result[0].source_issue_number == 11


@pytest.mark.asyncio
async def test_filter_closed_issues_fail_open(monkeypatch):
    from backend.services.agent_team.candidate_service import AgentTeamCandidateService

    service = AgentTeamCandidateService()

    mock_app = MagicMock()
    mock_app.get_issue = MagicMock(side_effect=Exception("API error"))

    monkeypatch.setattr(
        "backend.core.github_app.GitHubAppClient",
        lambda: mock_app,
    )

    candidates = [_make_candidate(issue_number=10, source_type="scan_finding")]

    result = await service._filter_closed_issues(candidates)

    # API 异常不应阻塞，fail-open
    assert len(result) == 1
