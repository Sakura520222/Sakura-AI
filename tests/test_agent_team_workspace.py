"""Agent 专家团队工作区与 Shell 执行安全测试"""

import pytest

from backend.services.agent_team.git_workspace_service import AgentTeamGitWorkspaceService
from backend.services.agent_team.shell_executor import AgentTeamShellExecutor
from backend.services.agent_team.workspace_service import (
    AgentTeamWorkspaceService,
    WorkspaceSecurityError,
)
from backend.workers.agent_team_worker import _merge_modified_files


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


def test_parse_changed_file_stats_counts_lines_and_statuses():
    stats = AgentTeamGitWorkspaceService.parse_changed_file_stats(
        "12\t3\tbackend/main.py\n-\t-\tres/logo.png\n",
        " M backend/main.py\nA  docs/new.md\nR  old.py -> new.py\n?? notes.txt\n",
    )

    assert stats["backend/main.py"] == {
        "additions": 12,
        "deletions": 3,
        "change_type": "modify",
    }
    assert stats["res/logo.png"]["additions"] == 0
    assert stats["res/logo.png"]["deletions"] == 0
    assert stats["docs/new.md"]["change_type"] == "add"
    assert stats["new.py"]["change_type"] == "rename"
    assert stats["notes.txt"]["change_type"] == "add"


@pytest.mark.asyncio
async def test_changed_file_stats_include_staged_changes(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentTeamShellExecutor(workspace, service)
    git_service = AgentTeamGitWorkspaceService(workspace_service=service)

    await executor.run_args(["git", "init"])
    await executor.run_args(["git", "config", "user.name", "Tester"])
    await executor.run_args(["git", "config", "user.email", "tester@example.com"])
    (workspace / "main.py").write_text("print('old')\n", encoding="utf-8")
    await executor.run_args(["git", "add", "main.py"])
    await executor.run_args(["git", "commit", "-m", "init"])

    (workspace / "main.py").write_text("print('new')\nprint('more')\n", encoding="utf-8")
    await executor.run_args(["git", "add", "main.py"])

    stats = await git_service.get_changed_file_stats(workspace)

    assert stats["main.py"]["additions"] == 2
    assert stats["main.py"]["deletions"] == 1
    assert stats["main.py"]["change_type"] == "modify"


def test_merge_modified_files_normalizes_and_includes_git_stats():
    merged = _merge_modified_files(["./main.py", r"backend\\app.py"], {"docs/new.md": {}})

    assert merged == ["backend/app.py", "docs/new.md", "main.py"]


@pytest.mark.asyncio
async def test_shell_executor_blocks_parent_escape(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentTeamShellExecutor(workspace, service)

    with pytest.raises(WorkspaceSecurityError):
        await executor.run("python -c \"open('../evil.txt','w').write('x')\"")
