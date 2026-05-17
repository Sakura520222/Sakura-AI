"""Agent Team 工具轮次配置、新工具与失败状态传播测试。"""

from dataclasses import dataclass

import pytest

from backend.core.config import DYNAMIC_CONFIG_LABELS, DYNAMIC_CONFIG_RANGES, get_settings
from backend.services.agent_team.fullstack_expert import FullStackResult
from backend.services.agent_team.iteration_loop import IterationLoopService
from backend.services.agent_team.professional_reviewer import ReviewResult
from backend.utils.config_utils import resolve_clamped_int_config
from backend.services.agent_team.tools.registry import (
    FULLSTACK_TOOL_INSTANCES,
    REVIEWER_TOOL_INSTANCES,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.webui.routes.agent_team import AGENT_TEAM_CONFIG_KEYS
from backend.workers.agent_team_worker import _format_failure_reason


def test_agent_team_max_tool_rounds_is_registered_for_webui():
    assert "agent_team_max_tool_rounds" in AGENT_TEAM_CONFIG_KEYS
    assert DYNAMIC_CONFIG_LABELS["agent_team_max_tool_rounds"] == "工具调用最大轮次"
    assert DYNAMIC_CONFIG_RANGES["agent_team_max_tool_rounds"] == (1, 1000)
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
        "backend.utils.config_utils.get_dynamic_config",
        fake_get_dynamic_config,
    )
    monkeypatch.setattr(
        "backend.utils.config_utils.get_settings",
        lambda: type("Settings", (), {"agent_team_max_tool_rounds": 30})(),
    )

    assert await resolve_clamped_int_config(
        "agent_team_max_tool_rounds"
    ) == 75


@pytest.mark.asyncio
async def test_resolve_agent_team_max_tool_rounds_falls_back_on_invalid_dynamic_config(
    monkeypatch,
):
    async def fake_get_dynamic_config(key: str):
        assert key == "agent_team_max_tool_rounds"
        return "invalid"

    monkeypatch.setattr(
        "backend.utils.config_utils.get_dynamic_config",
        fake_get_dynamic_config,
    )
    monkeypatch.setattr(
        "backend.utils.config_utils.get_settings",
        lambda: type("Settings", (), {"agent_team_max_tool_rounds": 42})(),
    )

    assert await resolve_clamped_int_config(
        "agent_team_max_tool_rounds"
    ) == 42


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


# ── Reviewer 轮次可配置测试 ──


def test_agent_team_reviewer_max_tool_rounds_is_registered_for_webui():
    assert "agent_team_reviewer_max_tool_rounds" in AGENT_TEAM_CONFIG_KEYS
    assert DYNAMIC_CONFIG_LABELS["agent_team_reviewer_max_tool_rounds"] == "审查工具调用最大轮次"
    assert DYNAMIC_CONFIG_RANGES["agent_team_reviewer_max_tool_rounds"] == (5, 500)
    assert get_settings().agent_team_reviewer_max_tool_rounds == 20


@pytest.mark.asyncio
async def test_resolve_reviewer_max_tool_rounds_uses_dynamic_config(monkeypatch):
    async def fake_get_dynamic_config(key: str):
        assert key == "agent_team_reviewer_max_tool_rounds"
        return "50"

    monkeypatch.setattr(
        "backend.utils.config_utils.get_dynamic_config",
        fake_get_dynamic_config,
    )
    monkeypatch.setattr(
        "backend.utils.config_utils.get_settings",
        lambda: type("Settings", (), {"agent_team_reviewer_max_tool_rounds": 20})(),
    )

    assert await resolve_clamped_int_config(
        "agent_team_reviewer_max_tool_rounds"
    ) == 50


@pytest.mark.asyncio
async def test_resolve_reviewer_max_tool_rounds_falls_back_on_invalid(monkeypatch):
    async def fake_get_dynamic_config(key: str):
        return "not_a_number"

    monkeypatch.setattr(
        "backend.utils.config_utils.get_dynamic_config",
        fake_get_dynamic_config,
    )
    monkeypatch.setattr(
        "backend.utils.config_utils.get_settings",
        lambda: type("Settings", (), {"agent_team_reviewer_max_tool_rounds": 20})(),
    )

    assert await resolve_clamped_int_config(
        "agent_team_reviewer_max_tool_rounds"
    ) == 20


# ── 新工具注册测试 ──


def test_check_changes_tool_registered_for_both_roles():
    fullstack_names = {t.name for t in FULLSTACK_TOOL_INSTANCES}
    reviewer_names = {t.name for t in REVIEWER_TOOL_INSTANCES}
    assert "check_changes" in fullstack_names
    assert "check_changes" in reviewer_names


def test_revert_file_tool_registered_only_for_fullstack():
    fullstack_names = {t.name for t in FULLSTACK_TOOL_INSTANCES}
    reviewer_names = {t.name for t in REVIEWER_TOOL_INSTANCES}
    assert "revert_file" in fullstack_names
    assert "revert_file" not in reviewer_names


def test_detect_project_tool_registered_for_both_roles():
    fullstack_names = {t.name for t in FULLSTACK_TOOL_INSTANCES}
    reviewer_names = {t.name for t in REVIEWER_TOOL_INSTANCES}
    assert "detect_project" in fullstack_names
    assert "detect_project" in reviewer_names


def test_check_changes_schema_has_modes():
    tool = next(t for t in FULLSTACK_TOOL_INSTANCES if t.name == "check_changes")
    schema = tool.get_schema()
    mode_enum = schema["function"]["parameters"]["properties"]["mode"]["enum"]
    assert "summary" in mode_enum
    assert "full" in mode_enum


def test_revert_file_schema_requires_file_path():
    tool = next(t for t in FULLSTACK_TOOL_INSTANCES if t.name == "revert_file")
    schema = tool.get_schema()
    required = schema["function"]["parameters"].get("required", [])
    assert "file_path" in required


def test_detect_project_schema_has_no_required_params():
    tool = next(t for t in FULLSTACK_TOOL_INSTANCES if t.name == "detect_project")
    schema = tool.get_schema()
    required = schema["function"]["parameters"].get("required", [])
    assert required == []


# ── FullStack 进度感知参数传递测试 ──


@dataclass
class _FakeFullstackAgentWithProgress:
    workspace: str
    workspace_service: object

    async def execute(self, **kwargs):
        assert "iteration" in kwargs
        assert "max_iterations" in kwargs
        assert kwargs["iteration"] == 1
        assert kwargs["max_iterations"] == 3
        return FullStackResult(
            success=True,
            summary="done",
            modified_files=["main.py"],
            tool_calls_count=1,
        )


@pytest.mark.asyncio
async def test_iteration_loop_passes_progress_params_to_fullstack(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "backend.services.agent_team.iteration_loop.FullStackExpertAgent",
        _FakeFullstackAgentWithProgress,
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
        max_iterations=3,
    )

    assert outcome.success is True


# ── 结构化反馈测试 ──


def test_build_feedback_groups_blocking_and_optional():
    from backend.services.agent_team.iteration_loop import IterationLoopService
    from backend.services.agent_team.professional_reviewer import ReviewFinding

    review = ReviewResult(
        verdict="needs_improvement",
        score=5,
        summary="需要修复",
        findings=[
            ReviewFinding(severity="critical", file="a.py", message="bug", suggestion="fix it"),
            ReviewFinding(severity="major", file="a.py", message="error", suggestion="handle"),
            ReviewFinding(severity="minor", file="b.py", message="style", suggestion="rename"),
            ReviewFinding(severity="suggestion", file="c.py", message="refactor"),
        ],
        passed=False,
    )

    service = IterationLoopService.__new__(IterationLoopService)
    feedback = service._build_feedback(review, iteration=1)

    assert "必须修复" in feedback
    assert "可选改进" in feedback
    assert "重要提示" in feedback
    assert "迭代 1" in feedback
    assert "a.py" in feedback
    assert "critical" in feedback
