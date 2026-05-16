"""Agent Team 工具轮次配置与失败状态传播测试。"""

from dataclasses import dataclass

import pytest

from backend.core.config import DYNAMIC_CONFIG_LABELS, DYNAMIC_CONFIG_RANGES, get_settings
from backend.services.agent_team.fullstack_expert import resolve_agent_team_max_tool_rounds
from backend.services.agent_team.fullstack_expert import FullStackResult
from backend.services.agent_team.iteration_loop import IterationLoopService
from backend.services.agent_team.professional_reviewer import ReviewResult
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.webui.routes.agent_team import AGENT_TEAM_CONFIG_KEYS
from backend.workers.agent_team_worker import _format_failure_reason


def test_agent_team_max_tool_rounds_is_registered_for_webui():
    assert "agent_team_max_tool_rounds" in AGENT_TEAM_CONFIG_KEYS
    assert DYNAMIC_CONFIG_LABELS["agent_team_max_tool_rounds"] == "工具调用最大轮次"
    assert DYNAMIC_CONFIG_RANGES["agent_team_max_tool_rounds"] == (1, 500)
    assert get_settings().agent_team_max_tool_rounds == 30


def test_agent_team_context_compression_config_is_registered_for_webui():
    expected_keys = {
        "agent_team_enable_context_compression",
        "agent_team_context_compression_threshold",
        "agent_team_context_compression_keep_rounds",
        "agent_team_context_summary_max_tokens",
    }

    assert expected_keys.issubset(AGENT_TEAM_CONFIG_KEYS)
    assert DYNAMIC_CONFIG_LABELS["agent_team_enable_context_compression"] == "启用上下文压缩"
    assert DYNAMIC_CONFIG_RANGES["agent_team_context_compression_threshold"] == (0.1, 1.0)
    assert DYNAMIC_CONFIG_RANGES["agent_team_context_compression_keep_rounds"] == (1, 20)
    assert DYNAMIC_CONFIG_RANGES["agent_team_context_summary_max_tokens"] == (500, 8192)
    settings = get_settings()
    assert settings.agent_team_enable_context_compression is True
    assert settings.agent_team_context_compression_threshold == 0.85


@pytest.mark.asyncio
async def test_resolve_agent_team_max_tool_rounds_uses_dynamic_config(monkeypatch):
    async def fake_get_dynamic_config(key: str):
        assert key == "agent_team_max_tool_rounds"
        return "75"

    monkeypatch.setattr(
        "backend.services.agent_team.fullstack_expert.get_dynamic_config",
        fake_get_dynamic_config,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.fullstack_expert.get_settings",
        lambda: type("Settings", (), {"agent_team_max_tool_rounds": 30})(),
    )

    assert await resolve_agent_team_max_tool_rounds() == 75


@pytest.mark.asyncio
async def test_resolve_agent_team_max_tool_rounds_falls_back_on_invalid_dynamic_config(
    monkeypatch,
):
    async def fake_get_dynamic_config(key: str):
        assert key == "agent_team_max_tool_rounds"
        return "invalid"

    monkeypatch.setattr(
        "backend.services.agent_team.fullstack_expert.get_dynamic_config",
        fake_get_dynamic_config,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.fullstack_expert.get_settings",
        lambda: type("Settings", (), {"agent_team_max_tool_rounds": 42})(),
    )

    assert await resolve_agent_team_max_tool_rounds() == 42


@dataclass
class _FakeFullstackAgent:
    workspace: str
    workspace_service: object

    async def execute(self, **kwargs):
        return FullStackResult(
            success=False,
            summary="达到最大工具调用轮次 (2)，已修改 1 个文件但未调用 finish_task",
            modified_files=["main.py"],
            tool_calls_count=2,
            error="max_rounds_reached_with_changes",
        )


@dataclass
class _FakeReviewer:
    workspace: str
    workspace_service: object

    async def review(self, **kwargs):
        return ReviewResult(
            verdict="pass",
            score=9,
            summary=f"reviewed {','.join(kwargs['modified_files'])}",
            passed=True,
            tool_calls_count=1,
        )


@pytest.mark.asyncio
async def test_iteration_loop_reviews_changes_when_fullstack_hits_tool_round_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "backend.services.agent_team.iteration_loop.FullStackExpertAgent",
        _FakeFullstackAgent,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.iteration_loop.ProfessionalReviewAgent",
        _FakeReviewer,
    )

    workspace_service = AgentTeamWorkspaceService(tmp_path)
    workspace = workspace_service.ensure_workspace("owner", "repo")

    outcome = await IterationLoopService(workspace, workspace_service).run(
        task_title="测试任务",
        task_summary="测试摘要",
        max_iterations=1,
    )

    assert outcome.success is True
    assert outcome.modified_files == ["main.py"]
    assert outcome.reason == "审查通过 (第 1 轮, 分数 9)"
    assert outcome.review_result is not None
    assert outcome.review_result.summary == "reviewed main.py"


@dataclass
class _FakeBrokenFullstackAgent:
    workspace: str
    workspace_service: object

    async def execute(self, **kwargs):
        return FullStackResult(
            success=False,
            summary="AI 返回空响应",
            modified_files=["main.py"],
            tool_calls_count=1,
            error="empty_response",
        )


class _ReviewerMustNotRun:
    def __init__(self, workspace: str, workspace_service: object):
        pass

    async def review(self, **kwargs):
        raise AssertionError("reviewer should not run for non-recoverable failures")


@pytest.mark.asyncio
async def test_iteration_loop_stops_non_recoverable_failures_even_with_modified_files(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "backend.services.agent_team.iteration_loop.FullStackExpertAgent",
        _FakeBrokenFullstackAgent,
    )
    monkeypatch.setattr(
        "backend.services.agent_team.iteration_loop.ProfessionalReviewAgent",
        _ReviewerMustNotRun,
    )

    workspace_service = AgentTeamWorkspaceService(tmp_path)
    workspace = workspace_service.ensure_workspace("owner", "repo")

    outcome = await IterationLoopService(workspace, workspace_service).run(
        task_title="测试任务",
        task_summary="测试摘要",
        max_iterations=1,
    )

    assert outcome.success is False
    assert outcome.modified_files == ["main.py"]
    assert outcome.review_result is None
    assert outcome.reason == "全栈专家执行失败: empty_response"


def test_failure_reason_keeps_real_reason_when_modified_files_exist():
    assert (
        _format_failure_reason(
            reason="全栈专家执行失败: max_rounds_reached_with_changes",
            modified_files=["main.py"],
        )
        == "全栈专家执行失败: max_rounds_reached_with_changes"
    )


def test_failure_reason_reports_no_valid_change_only_when_no_files_exist():
    assert (
        _format_failure_reason(
            reason="全栈专家未修改任何文件",
            modified_files=[],
        )
        == "全栈专家未能生成有效的代码修改"
    )
