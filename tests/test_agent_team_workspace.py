"""Agent 专家团队工作区与 Shell 执行安全测试"""

import pytest

from backend.services.agent_team.shell_executor import AgentTeamShellExecutor
from backend.services.agent_team.workspace_service import (
    AgentTeamWorkspaceService,
    WorkspaceSecurityError,
)


def test_workspace_path_shape_and_creation(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")

    workspace = service.ensure_workspace("Sakura520222", "Sakura-AI-Reviewer")

    assert workspace == (tmp_path / "workplace" / "Sakura520222" / "Sakura-AI-Reviewer").resolve()
    assert workspace.exists()


@pytest.mark.parametrize(
    ("owner", "repo"),
    [
        ("..", "repo"),
        ("owner/name", "repo"),
        ("owner", "../repo"),
        ("owner", "repo/name"),
        ("owner", "repo:name"),
    ],
)
def test_workspace_rejects_unsafe_segments(tmp_path, owner, repo):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")

    with pytest.raises(WorkspaceSecurityError):
        service.ensure_workspace(owner, repo)


def test_resolve_inside_workspace_blocks_escape(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")

    with pytest.raises(WorkspaceSecurityError):
        service.resolve_inside_workspace(workspace, "../other")


@pytest.mark.asyncio
async def test_shell_executor_uses_workspace_and_python_env(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentTeamShellExecutor(workspace, service)

    result = await executor.run("python --version", timeout_seconds=30)

    assert result.returncode == 0
    assert "Python" in (result.stdout + result.stderr)
    assert result.cwd == str(workspace)


@pytest.mark.asyncio
async def test_shell_executor_run_args_allows_https_url(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentTeamShellExecutor(workspace, service)

    result = await executor.run_args(["git", "--version"])

    assert result.returncode == 0
    assert result.command == "git --version"


def test_shell_executor_masks_access_token(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentTeamShellExecutor(workspace, service)

    masked = executor._mask_sensitive_arg("https://x-access-token:secret@github.com/owner/repo.git")

    assert masked == "https://x-access-token:***@github.com/owner/repo.git"


@pytest.mark.asyncio
async def test_shell_executor_blocks_parent_escape(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentTeamShellExecutor(workspace, service)

    with pytest.raises(WorkspaceSecurityError):
        await executor.run("python -c \"open('../evil.txt','w').write('x')\"")
