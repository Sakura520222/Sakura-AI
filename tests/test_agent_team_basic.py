"""Agent 专家团队模式基础测试"""

import pickle
from unittest.mock import MagicMock

import pytest

from backend.core.config import (
    DYNAMIC_CONFIG_GROUPS,
    DYNAMIC_CONFIG_LABELS,
    DYNAMIC_CONFIG_RANGES,
    DYNAMIC_CONFIG_SELECT_OPTIONS,
    get_settings,
)
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
from backend.webui.routes.agent_team import AGENT_TEAM_ACTIVE_STATUSES

AGENT_TEAM_DYNAMIC_KEYS = set(DYNAMIC_CONFIG_GROUPS["agent_team"]["keys"])


@pytest.mark.asyncio
async def test_load_agent_team_ai_config_uses_only_role_and_policy(monkeypatch):
    requested_keys: list[str] = []

    async def fake_get_dynamic_config(key: str):
        requested_keys.append(key)
        values = {
            "agent_team_temperature": 0.25,
            "agent_team_max_tokens": 4096,
            "agent_team_timeout_seconds": 300,
            # Legacy values must not be read or reflected in the snapshot.
            "ai_provider": "legacy-provider",
            "openai_api_base": "https://legacy.example/v1",
            "openai_api_key": "legacy-key",
            "openai_model": "legacy-model",
            "summary_api_base": "https://legacy-summary.example/v1",
            "summary_api_key": "legacy-summary-key",
            "summary_model": "legacy-summary-model",
            "agent_team_api_base": "https://legacy-agent.example/v1",
            "agent_team_api_key": "legacy-agent-key",
            "agent_team_model": "legacy-agent-model",
        }
        return values.get(key)

    monkeypatch.setattr(
        "backend.services.agent_team.ai_client.get_dynamic_config",
        fake_get_dynamic_config,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.ai_client.get_settings",
        lambda: (_ for _ in ()).throw(
            AssertionError("legacy settings must not be read")
        ),
    )

    config = await load_agent_team_ai_config()

    assert config.agent_role == "agent_team"
    assert config.summary_role == "summary"
    assert config.timeout_seconds == 300
    # temperature/max_tokens 已迁至新版 /config/ai 角色绑定的 reasoning_params，
    # load 不再读取这两个 key（即使数据库存在也不读）。
    assert set(requested_keys) == {"agent_team_timeout_seconds"}
    snapshot = config.safe_snapshot()
    assert snapshot == {
        "agent_role": "agent_team",
        "summary_role": "summary",
        "timeout_seconds": 300,
    }
    assert "legacy" not in str(snapshot)


@pytest.mark.asyncio
async def test_create_agent_team_client_is_role_only(monkeypatch):
    from backend.services.agent_team import ai_client as module

    class FakeClient:
        def __init__(self, *args, **kwargs):
            assert not args
            assert not kwargs

    monkeypatch.setattr(module, "AIApiClient", FakeClient)
    monkeypatch.setattr(
        module,
        "load_agent_team_ai_config",
        lambda: None,
    )
    config = AgentTeamAIConfig(
        agent_role="agent_team",
        summary_role="summary",
        timeout_seconds=600,
    )
    monkeypatch.setattr(
        module, "load_agent_team_ai_config", lambda: _async_value(config)
    )

    client, loaded = await module.create_agent_team_client()

    assert isinstance(client, FakeClient)
    assert loaded is config
    assert loaded.agent_role == "agent_team"
    assert loaded.summary_role == "summary"


@pytest.mark.asyncio
async def test_summary_factory_returns_summary_role_without_legacy_config(monkeypatch):
    from backend.services.agent_team import ai_client as module

    class FakeClient:
        def __init__(self, *args, **kwargs):
            assert not args
            assert not kwargs

    monkeypatch.setattr(module, "AIApiClient", FakeClient)
    config = AgentTeamAIConfig(
        agent_role="agent_team",
        summary_role="summary",
        timeout_seconds=600,
    )
    client, role, loaded = await module.create_agent_team_summary_client(config)

    assert isinstance(client, FakeClient)
    assert role == "summary"
    assert loaded is config


async def _async_value(value):
    return value


def test_agent_team_provider_options_are_removed_from_dynamic_surface():
    assert "agent_team_model_provider" not in DYNAMIC_CONFIG_SELECT_OPTIONS


def test_agent_team_max_tokens_range_is_provider_safe():
    assert DYNAMIC_CONFIG_RANGES["agent_team_max_tokens"] == (1024, 32768)


def test_agent_pr_closed_loop_config_registered_for_webui():
    settings = get_settings()
    assert settings.agent_team_pr_closed_loop_enabled is True
    assert settings.agent_team_pr_review_pass_score == 8
    assert settings.agent_team_pr_review_blocking_severities == "critical,major"
    assert "agent_team_pr_closed_loop_enabled" in AGENT_TEAM_DYNAMIC_KEYS
    assert "agent_team_pr_review_pass_score" in AGENT_TEAM_DYNAMIC_KEYS
    assert "agent_team_pr_review_blocking_severities" in AGENT_TEAM_DYNAMIC_KEYS
    assert (
        DYNAMIC_CONFIG_LABELS["agent_team_pr_closed_loop_enabled"]
        == "启用 Agent PR 闭环"
    )
    assert (
        DYNAMIC_CONFIG_LABELS["agent_team_pr_review_pass_score"]
        == "Agent PR 审查通过分数"
    )
    assert DYNAMIC_CONFIG_RANGES["agent_team_pr_review_pass_score"] == (1, 10)


def test_agent_team_ai_config_safe_snapshot_contains_only_roles_and_policy():
    config = AgentTeamAIConfig(
        agent_role="agent_team",
        summary_role="summary",
        timeout_seconds=600,
    )

    assert config.safe_snapshot() == {
        "agent_role": "agent_team",
        "summary_role": "summary",
        "timeout_seconds": 600,
    }


def test_agent_team_ai_config_pickle_roundtrip_preserves_roles_and_policy():
    config = AgentTeamAIConfig(
        agent_role="agent_team",
        summary_role="summary",
        timeout_seconds=600,
    )

    restored = pickle.loads(pickle.dumps(config))

    assert restored == config


@pytest.mark.asyncio
async def test_load_agent_team_ai_config_preserves_explicit_zero_values(monkeypatch):
    async def fake_get_dynamic_config(key: str):
        values = {
            "agent_team_timeout_seconds": 0,
        }
        return values.get(key)

    monkeypatch.setattr(
        "backend.services.agent_team.ai_client.get_dynamic_config",
        fake_get_dynamic_config,
    )

    config = await load_agent_team_ai_config()

    assert config.timeout_seconds == 0
    assert config.agent_role == "agent_team"
    assert config.summary_role == "summary"


def test_agent_team_config_includes_policy_keys():
    required = {
        "agent_team_enabled",
        "agent_team_workspace_root",
        "agent_team_max_concurrent",
        "agent_team_max_iterations_per_task",
        "agent_team_test_command_blocklist",
    }

    assert required.issubset(AGENT_TEAM_DYNAMIC_KEYS)

    # 模型 provider/凭据类配置已迁至新版 /config/ai 角色绑定，不在动态组暴露
    # （温度/max_tokens 等推理参数仍保留在动态组，Agent 运行时可读）。
    retired_ai_keys = {"agent_team_model_provider", "agent_team_model"}
    assert retired_ai_keys.isdisjoint(AGENT_TEAM_DYNAMIC_KEYS)


def test_agent_team_active_statuses_include_waiting_human():
    assert "queued" in AGENT_TEAM_ACTIVE_STATUSES
    assert "waiting_human" in AGENT_TEAM_ACTIVE_STATUSES
    assert "completed" not in AGENT_TEAM_ACTIVE_STATUSES


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
