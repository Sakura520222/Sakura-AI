"""Agent 执行信任域和秘密边界回归测试。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

import backend.services.agent_team.execution as execution_module
from backend.services.agent_team.execution import (
    ExecutionError,
    ExecutionProfile,
    ExecutionRequest,
    ExecutionResult,
    LocalExecutionRunner,
    TrustedGitRunner,
    UnsupportedExecutionProfile,
    execute_request,
    execution_workspace_key,
)
from backend.services.agent_team.network_policy import AgentTeamNetworkPolicy
from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.tools.grep_tool import GrepTool
from backend.services.agent_team.tools.shell_tool import ShellTool
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


def _workspace(tmp_path: Path) -> tuple[AgentTeamWorkspaceService, Path]:
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    return service, service.ensure_workspace("owner", "repo")


def test_execution_request_requires_exactly_one_command_form():
    with pytest.raises(ValueError, match="command 或 argv"):
        ExecutionRequest(
            workspace_key="task-1",
            command="echo ok",
            argv=("echo", "ok"),
        )

    with pytest.raises(ValueError, match="command 或 argv"):
        ExecutionRequest(workspace_key="task-1")


def test_execution_request_rejects_agent_environment_overrides():
    with pytest.raises(ValueError, match="环境变量"):
        ExecutionRequest(
            workspace_key="task-1",
            command="echo ok",
            env={"SECRET": "should-not-pass"},
        )


def test_local_runner_advertises_dependency_profile_for_explicit_local_installs(
    tmp_path,
):
    service, workspace = _workspace(tmp_path)
    runner = LocalExecutionRunner(workspace, service)

    assert runner.supports_profile(ExecutionProfile.AGENT)
    assert not runner.supports_profile(ExecutionProfile.TRUSTED_CONTROL)
    assert runner.supports_profile(ExecutionProfile.DEPENDENCY)


@pytest.mark.parametrize(
    "workspace_key",
    ["../task", "task/1", "task\\1", "task with space", ""],
)
def test_untrusted_request_requires_fixed_workspace_key(workspace_key):
    with pytest.raises(ValueError, match="workspace_key"):
        ExecutionRequest(workspace_key=workspace_key, command="echo ok")


@pytest.mark.parametrize("cwd", [PurePosixPath("/tmp"), PurePosixPath("a/../b")])
def test_untrusted_request_requires_relative_cwd(cwd):
    with pytest.raises(ValueError, match="cwd"):
        ExecutionRequest(workspace_key="task-1", command="echo ok", cwd=cwd)


def test_trusted_request_may_use_local_absolute_cwd(tmp_path):
    request = ExecutionRequest(
        workspace_key=str(tmp_path),
        argv=("git", "--version"),
        cwd=PurePosixPath("/tmp/sandbox"),
        profile=ExecutionProfile.TRUSTED_CONTROL,
    )
    assert request.cwd.is_absolute()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"command": "x" * 32769},
        {"command": "echo\x00ok"},
        {"argv": ("x" * 8193,)},
        {"argv": ("echo\x00",)},
        {"argv": tuple("echo" for _ in range(257))},
        {"workspace_key": "task\x00-1"},
        {"cwd": PurePosixPath("safe\x00path")},
    ],
)
def test_execution_request_rejects_nul_and_field_limits(kwargs):
    values = {
        "workspace_key": "task-1",
        "command": "echo ok",
    }
    values.update(kwargs)
    if "argv" in kwargs:
        values.pop("command", None)
    with pytest.raises(ValueError):
        ExecutionRequest(**values)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 3600.1])
def test_execution_request_rejects_invalid_or_excessive_timeout(timeout):
    with pytest.raises(ValueError, match="timeout"):
        ExecutionRequest(workspace_key="task-1", command="echo ok", timeout_seconds=timeout)


def test_execution_request_accepts_limits_at_boundary():
    request = ExecutionRequest(
        workspace_key="task-1",
        command="x" * 32768,
        timeout_seconds=3600,
    )
    assert len(request.command or "") == 32768


@pytest.mark.asyncio
async def test_execute_request_fails_closed_without_execute():
    class LegacyRunner:
        async def run(self, command, **kwargs):
            raise AssertionError("legacy run must not be called")

        async def run_args(self, args, **kwargs):
            raise AssertionError("legacy run_args must not be called")

    request = ExecutionRequest(workspace_key="task-1", command="echo ok")
    with pytest.raises(ExecutionError, match="execute"):
        await execute_request(LegacyRunner(), request)


def test_execution_workspace_key_distinguishes_same_named_repositories(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    first = service.ensure_workspace("owner-a", "same-repo")
    second = service.ensure_workspace("owner-b", "same-repo")

    first_key = execution_workspace_key(first, service)
    second_key = execution_workspace_key(second, service)

    assert first_key != second_key
    assert "/" not in first_key
    assert len(first_key) <= 128


@pytest.mark.asyncio
async def test_trusted_git_runner_public_execute_is_strict(tmp_path):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)

    assert runner.supports_profile(ExecutionProfile.TRUSTED_CONTROL)
    assert not runner.supports_profile(ExecutionProfile.AGENT)
    assert not runner.supports_profile(ExecutionProfile.DEPENDENCY)

    result = await runner.execute(
        ExecutionRequest(
            workspace_key="task-1",
            argv=("git", "--version"),
            profile=ExecutionProfile.TRUSTED_CONTROL,
        )
    )
    assert result.returncode == 0

    with pytest.raises(UnsupportedExecutionProfile):
        await runner.execute(
            ExecutionRequest(
                workspace_key="task-1",
                argv=("git", "--version"),
                profile=ExecutionProfile.AGENT,
            )
        )
    with pytest.raises(ExecutionError, match="shell command"):
        await runner.execute(
            ExecutionRequest(
                workspace_key="task-1",
                command="git --version",
                profile=ExecutionProfile.TRUSTED_CONTROL,
            )
        )
    with pytest.raises(ExecutionError, match="固定系统 Git"):
        await runner.execute(
            ExecutionRequest(
                workspace_key="task-1",
                argv=("subdir/git", "--version"),
                profile=ExecutionProfile.TRUSTED_CONTROL,
            )
        )
    with pytest.raises(ExecutionError, match="userinfo"):
        await runner.execute(
            ExecutionRequest(
                workspace_key="task-1",
                argv=("git", "clone", "https://user:secret@example.com/repo.git"),
                profile=ExecutionProfile.TRUSTED_CONTROL,
            )
        )


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/owner/repo.git?token=secret",
        "https://github.com/owner/repo.git#fragment",
        "user:secret@github.com:owner/repo.git",
        "user@github.com:owner/repo.git",
        "file:///etc/passwd",
        "ext::ssh://evil.example/owner/repo.git",
    ],
)
def test_trusted_git_rejects_unsafe_remote_url_shapes(remote):
    with pytest.raises(ExecutionError):
        TrustedGitRunner._reject_url_userinfo(("git", "clone", remote))


def test_trusted_git_allows_explicit_safe_ssh_url():
    TrustedGitRunner._reject_url_userinfo(
        ("git", "clone", "ssh://git@github.com/owner/repo.git")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attacker_remote",
    [
        "https://evil.example/owner/repo.git",
        "https://github.com/other/repo.git",
    ],
)
async def test_trusted_git_token_binds_clone_remote_before_askpass(
    tmp_path, monkeypatch, attacker_remote
):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)

    def token_must_not_be_created(_token):
        raise AssertionError("remote mismatch must fail before askpass")

    monkeypatch.setattr(runner, "_create_askpass", token_must_not_be_created)
    with pytest.raises(ExecutionError, match="expected remote"):
        await runner.run_args(
            ["git", "clone", attacker_remote, "."],
            credential_token="secret-token",
            trusted_expected_remote="https://github.com/owner/repo.git",
        )


@pytest.mark.asyncio
async def test_trusted_git_token_requires_expected_remote(tmp_path):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    with pytest.raises(ExecutionError, match="trusted_expected_remote"):
        await runner.run_args(
            ["git", "clone", "https://github.com/owner/repo.git", "."],
            credential_token="secret-token",
        )


@pytest.mark.asyncio
async def test_trusted_git_token_binds_fetch_origin_before_askpass(tmp_path, monkeypatch):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    await runner.run_args(["git", "init"])
    await runner.run_args(
        ["git", "remote", "add", "origin", "https://evil.example/owner/repo.git"]
    )

    def token_must_not_be_created(_token):
        raise AssertionError("remote mismatch must fail before askpass")

    monkeypatch.setattr(runner, "_create_askpass", token_must_not_be_created)
    with pytest.raises(ExecutionError, match="expected remote"):
        await runner.run_args(
            ["git", "fetch", "origin", "main"],
            credential_token="secret-token",
            trusted_expected_remote="https://github.com/owner/repo.git",
        )


@pytest.mark.asyncio
async def test_trusted_git_github_fetch_with_bound_remote_reaches_runner(
    tmp_path, monkeypatch
):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    await runner.run_args(["git", "init"])
    await runner.run_args(
        [
            "git",
            "remote",
            "add",
            "origin",
            "https://github.com/owner/repo.git",
        ]
    )
    captured: dict[str, ExecutionRequest] = {}

    async def fake_execute(request):
        captured["request"] = request
        return ExecutionResult(
            command="git fetch origin main",
            cwd=str(workspace),
            exit_code=0,
        )

    monkeypatch.setattr(runner, "execute", fake_execute)
    result = await runner.run_args(
        ["git", "fetch", "origin", "main"],
        credential_token="secret-token",
        trusted_expected_remote="https://github.com/owner/repo.git",
    )
    assert result.returncode == 0
    assert "request" in captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config_text",
    [
        "[credential]\n\thelper = !echo token\n",
        "[core]\n\thooksPath = hooks\n",
        "[core]\n\tfsmonitor = !touch marker\n",
        "[core]\n\tsshCommand = sh -c evil\n",
        "[diff \"evil\"]\n\texternal = evil-diff\n",
        "[filter \"evil\"]\n\tclean = evil-filter\n",
        "[include]\n\tpath = ../outside.config\n",
        "[url \"https://evil.example/\"]\n\tinsteadOf = https://github.com/\n",
        "[http \"https://github.com\"]\n\textraHeader = Authorization: Bearer secret\n",
        "[http]\n\tproxy = http://evil.example\n",
        "[core]\n\taskpass = evil-askpass\n",
        "[remote \"origin\"]\n\tproxy = evil-proxy\n",
        "[submodule \"evil\"]\n\tupdate = !evil-command\n",
        "[remote \"origin\"]\n\turl = https://user:secret@github.com/owner/repo.git\n",
    ],
)
async def test_trusted_git_rejects_executable_local_config_before_token(
    tmp_path, monkeypatch, config_text
):
    service, workspace = _workspace(tmp_path)
    git_dir = workspace / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(config_text, encoding="utf-8")
    runner = TrustedGitRunner(workspace, service)

    def token_must_not_be_created(_token):
        raise AssertionError("unsafe local config must fail before askpass")

    monkeypatch.setattr(runner, "_create_askpass", token_must_not_be_created)
    with pytest.raises(ExecutionError, match="Git 配置"):
        await runner.run_args(
            ["git", "fetch"],
            credential_token="secret-token",
            trusted_expected_remote="https://github.com/owner/repo.git",
        )


@pytest.mark.asyncio
async def test_trusted_git_rejects_metadata_symlink_before_askpass(tmp_path, monkeypatch):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    await runner.run_args(["git", "init"])
    await runner.run_args(
        ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"]
    )
    outside = tmp_path / "outside-refs"
    outside.mkdir()
    symlink = workspace / ".git" / "refs" / "heads" / "attacker"
    try:
        symlink.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable on this platform: {exc}")

    monkeypatch.setattr(
        runner,
        "_create_askpass",
        lambda _token: (_ for _ in ()).throw(
            AssertionError("metadata symlink must fail before askpass")
        ),
    )
    with pytest.raises(ExecutionError, match="symlink/reparse"):
        await runner.run_args(
            ["git", "fetch", "origin", "main"],
            credential_token="secret-token",
            trusted_expected_remote="https://github.com/owner/repo.git",
        )


@pytest.mark.asyncio
async def test_trusted_git_rejects_object_alternates_before_askpass(
    tmp_path, monkeypatch
):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    await runner.run_args(["git", "init"])
    await runner.run_args(
        ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"]
    )
    alternates = workspace / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(str(tmp_path / "outside-objects") + "\n", encoding="utf-8")

    monkeypatch.setattr(
        runner,
        "_create_askpass",
        lambda _token: (_ for _ in ()).throw(
            AssertionError("alternates must fail before askpass")
        ),
    )
    with pytest.raises(ExecutionError, match="alternates"):
        await runner.run_args(
            ["git", "fetch", "origin", "main"],
            credential_token="secret-token",
            trusted_expected_remote="https://github.com/owner/repo.git",
        )


@pytest.mark.asyncio
async def test_trusted_git_rejects_metadata_symlink_without_token(tmp_path):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    await runner.run_args(["git", "init"])
    outside = tmp_path / "outside-refs"
    outside.mkdir()
    symlink = workspace / ".git" / "refs" / "heads" / "attacker"
    try:
        symlink.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable on this platform: {exc}")

    with pytest.raises(ExecutionError, match="symlink/reparse"):
        await runner.run_args(["git", "status", "--short"])

    with pytest.raises(ExecutionError, match="symlink/reparse"):
        await runner.execute(
            ExecutionRequest(
                workspace_key=str(workspace),
                argv=("git", "rev-parse", "HEAD"),
                profile=ExecutionProfile.TRUSTED_CONTROL,
            )
        )


@pytest.mark.asyncio
async def test_trusted_git_rejects_object_alternates_without_token(tmp_path):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    await runner.run_args(["git", "init"])
    alternates = workspace / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(str(tmp_path / "outside-objects") + "\n", encoding="utf-8")

    with pytest.raises(ExecutionError, match="alternates"):
        await runner.run_args(["git", "status", "--short"])

    with pytest.raises(ExecutionError, match="alternates"):
        await runner.execute(
            ExecutionRequest(
                workspace_key=str(workspace),
                argv=("git", "diff", "--stat"),
                profile=ExecutionProfile.TRUSTED_CONTROL,
            )
        )


def test_trusted_git_lock_isolated_by_event_loop(tmp_path):
    """A repository lock must not be reused across asyncio event loops."""

    service, workspace = _workspace(tmp_path)
    del service
    locks = []

    async def capture_lock():
        lock = execution_module._workspace_git_lock(workspace)
        locks.append(lock)
        async with lock:
            pass

    asyncio.run(capture_lock())
    asyncio.run(capture_lock())

    assert len(locks) == 2
    assert locks[0] is not locks[1]


@pytest.mark.asyncio
async def test_trusted_git_run_request_rejects_outer_overrides(tmp_path, monkeypatch):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    request = ExecutionRequest(
        workspace_key="task-1",
        argv=("git", "--version"),
        profile=ExecutionProfile.TRUSTED_CONTROL,
    )
    calls: list[ExecutionRequest] = []

    async def fake_execute(received: ExecutionRequest) -> ExecutionResult:
        calls.append(received)
        return ExecutionResult(command="git --version", cwd=".", exit_code=0)

    monkeypatch.setattr(runner, "execute", fake_execute)

    result = await runner.run(request)
    assert result.returncode == 0
    assert calls == [request]

    with pytest.raises(ValueError, match="非默认"):
        await runner.run(request, cwd=workspace)
    with pytest.raises(ValueError, match="非默认"):
        await runner.run(request, timeout_seconds=30)
    with pytest.raises(ValueError, match="非默认"):
        await runner.run(request, profile=ExecutionProfile.AGENT)
    assert calls == [request]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relative_path", "target_is_directory"),
    [
        (Path("objects") / "aa", True),
        (Path("objects") / "pack" / "attacker.pack", False),
        (Path("objects") / "info" / "attacker", False),
    ],
)
async def test_trusted_git_rejects_object_structure_reparse_points(
    tmp_path,
    relative_path,
    target_is_directory,
):
    """Object roots and pack/info children cannot redirect Git metadata."""

    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    await runner.run_args(["git", "init"])

    outside = tmp_path / "outside-object-target"
    if target_is_directory:
        outside.mkdir()
    else:
        outside.write_text("outside\n", encoding="utf-8")
    link = workspace / ".git" / relative_path
    try:
        link.symlink_to(outside, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable on this platform: {exc}")

    with pytest.raises(ExecutionError, match="objects|symlink/reparse"):
        await runner.run_args(["git", "status", "--short"])


@pytest.mark.asyncio
async def test_trusted_git_object_direct_scan_has_node_cap(tmp_path, monkeypatch):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    await runner.run_args(["git", "init"])

    monkeypatch.setattr(execution_module, "MAX_GIT_OBJECTS_DIRECT_NODES", 1)
    with pytest.raises(ExecutionError, match="objects.*节点数量超过上限"):
        await runner.run_args(["git", "status", "--short"])


@pytest.mark.asyncio
async def test_trusted_git_object_pack_info_scan_has_node_cap(tmp_path, monkeypatch):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    await runner.run_args(["git", "init"])
    marker = workspace / ".git" / "objects" / "pack" / "pack-marker"
    marker.write_text("marker\n", encoding="utf-8")

    monkeypatch.setattr(execution_module, "MAX_GIT_OBJECTS_AUX_NODES", 0)
    with pytest.raises(ExecutionError, match="objects/pack.*节点数量超过上限"):
        await runner.run_args(["git", "status", "--short"])


@pytest.mark.asyncio
async def test_trusted_git_refs_logs_scan_has_node_cap(tmp_path, monkeypatch):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    await runner.run_args(["git", "init"])

    # Remove the empty refs tree so the shared cap can specifically exercise
    # logs rather than failing first on the normal refs/heads directories.
    refs = workspace / ".git" / "refs"
    for child in (refs / "heads", refs / "tags"):
        if child.exists():
            child.rmdir()
    if refs.exists():
        refs.rmdir()
    logs = workspace / ".git" / "logs"
    logs.mkdir()
    (logs / "HEAD").write_text("log\n", encoding="utf-8")

    monkeypatch.setattr(execution_module, "MAX_GIT_REFS_LOGS_NODES", 1)
    with pytest.raises(ExecutionError, match="Git metadata logs.*节点数量超过上限"):
        await runner.run_args(["git", "status", "--short"])


@pytest.mark.asyncio
async def test_trusted_git_real_local_config_rejects_hook_before_token(tmp_path):
    """真实 Git 配置检查必须在凭据创建和 hook 执行之前失败。"""

    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    await runner.run_args(["git", "init"])

    marker = workspace / "malicious-hook-ran"
    hook_dir = workspace / "hooks"
    hook_dir.mkdir()
    hook = hook_dir / "pre-commit"
    hook.write_text(
        "#!/bin/sh\nprintf hooked > malicious-hook-ran\n",
        encoding="utf-8",
    )
    try:
        hook.chmod(0o700)
    except OSError:
        pass
    config_path = workspace / ".git" / "config"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "[core]\n\thooksPath = hooks\n",
        encoding="utf-8",
    )

    with pytest.raises(ExecutionError, match="Git 配置"):
        await runner.run_args(
            ["git", "commit", "--allow-empty", "-m", "blocked"],
            credential_token="secret-token",
            trusted_expected_remote="https://github.com/owner/repo.git",
        )
    assert not marker.exists()


@pytest.mark.asyncio
async def test_trusted_git_real_pre_commit_hook_is_isolated(tmp_path):
    """即使配置没有恶意 key，受控 Git 也不能执行仓库自带 hook。"""

    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    await runner.run_args(["git", "init"])
    await runner.run_args(["git", "config", "user.name", "Agent Test"])
    await runner.run_args(
        ["git", "config", "user.email", "agent@example.test"]
    )

    marker = workspace / "physical-hook-ran"
    hook = workspace / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\nprintf hooked > physical-hook-ran\n",
        encoding="utf-8",
    )
    try:
        hook.chmod(0o700)
    except OSError:
        pass
    (workspace / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    await runner.run_args(["git", "add", "--", "tracked.txt"])

    result = await runner.run_args(
        ["git", "commit", "--no-gpg-sign", "-m", "controlled"]
    )

    assert result.returncode == 0
    assert not marker.exists()


@pytest.mark.asyncio
async def test_trusted_git_validates_worktree_pointer_chain(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    base = service.ensure_base_workspace("owner", "repo")
    base_runner = TrustedGitRunner(base, service)
    await base_runner.run_args(["git", "init"])
    await base_runner.run_args(["git", "config", "user.name", "Agent Test"])
    await base_runner.run_args(
        ["git", "config", "user.email", "agent@example.test"]
    )
    (base / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    await base_runner.run_args(["git", "add", "--", "tracked.txt"])
    await base_runner.run_args(["git", "commit", "--no-gpg-sign", "-m", "base"])

    worktree = service.get_task_worktree_path("owner", "repo", 1, "task-1")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    repo_runner = TrustedGitRunner(
        service.get_repo_root_path("owner", "repo"), service
    )
    await repo_runner.run_args(
        ["git", "worktree", "add", "-b", "task-1", str(worktree), "HEAD"],
        cwd=base,
    )

    worktree_runner = TrustedGitRunner(worktree, service)
    result = await worktree_runner.run_args(["git", "status", "--short"])
    assert result.returncode == 0

    pointer_dir = next((base / ".git" / "worktrees").iterdir())
    outside = tmp_path / "outside-common-gitdir"
    outside.mkdir()
    (pointer_dir / "commondir").write_text(str(outside), encoding="utf-8")
    with pytest.raises(ExecutionError, match="commondir"):
        await worktree_runner.run_args(["git", "status", "--short"])


@pytest.mark.asyncio
async def test_trusted_git_revalidates_metadata_at_spawn_boundary(tmp_path, monkeypatch):
    """A metadata replacement between preflight and spawn is fail-closed."""

    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    await runner.run_args(["git", "init"])
    link = workspace / ".git" / "refs" / "heads" / "race"
    link.write_text("0" * 40 + "\n", encoding="utf-8")

    original_validate = runner._validate_git_metadata_before_execution
    validation_count = 0

    async def replace_before_final_validation():
        nonlocal validation_count
        validation_count += 1
        if validation_count == 2:
            outside = tmp_path / "outside-race-target"
            outside.mkdir()
            try:
                link.unlink()
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                pytest.skip(f"symlink unavailable on this platform: {exc}")
        await original_validate()

    monkeypatch.setattr(
        runner,
        "_validate_git_metadata_before_execution",
        replace_before_final_validation,
    )

    with pytest.raises(ExecutionError, match="symlink/reparse"):
        await runner.execute(
            ExecutionRequest(
                workspace_key=execution_workspace_key(workspace, service),
                argv=("git", "status", "--short"),
                profile=ExecutionProfile.TRUSTED_CONTROL,
            )
        )
    assert validation_count == 2


@pytest.mark.asyncio
async def test_trusted_git_rejects_gitdir_pointer_outside_controlled_repo_before_token(
    tmp_path, monkeypatch
):
    service, workspace = _workspace(tmp_path)
    outside = tmp_path / "outside-gitdir"
    outside.mkdir()
    (workspace / ".git").write_text(
        f"gitdir: {outside}\n",
        encoding="utf-8",
    )
    runner = TrustedGitRunner(workspace, service)

    monkeypatch.setattr(
        runner,
        "_create_askpass",
        lambda _token: (_ for _ in ()).throw(
            AssertionError("unsafe gitdir must fail before askpass")
        ),
    )
    with pytest.raises(ExecutionError, match="gitdir"):
        await runner.run_args(
            ["git", "fetch"],
            credential_token="secret-token",
            trusted_expected_remote="https://github.com/owner/repo.git",
        )


def test_trusted_git_runner_uses_system_path_not_workspace_venv(tmp_path):
    service, workspace = _workspace(tmp_path)
    venv_bin = workspace / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    venv_bin.mkdir(parents=True)
    fake_git = venv_bin / ("git.exe" if os.name == "nt" else "git")
    fake_git.write_text("not a git executable", encoding="utf-8")
    runner = TrustedGitRunner(workspace, service)

    env = runner._build_env()
    assert not env["PATH"].split(os.pathsep)[0].startswith(str(venv_bin))
    assert runner.git_path != fake_git.resolve()


def test_trusted_git_runner_does_not_consult_ambient_git_path(tmp_path, monkeypatch):
    service, workspace = _workspace(tmp_path)

    def fail_which(_name):
        raise AssertionError("Trusted Git must use fixed system paths")

    monkeypatch.setattr(
        "backend.services.agent_team.execution.shutil.which", fail_which
    )
    runner = TrustedGitRunner(workspace, service)

    assert runner.git_path.is_absolute()


@pytest.mark.asyncio
async def test_local_runner_executes_dependency_profile_only_with_full_access(
    tmp_path,
    monkeypatch,
):
    service, workspace = _workspace(tmp_path)
    runner = LocalExecutionRunner(workspace, service)

    async def full_access_policy():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        full_access_policy,
    )

    result = await runner.execute(
        ExecutionRequest(
            workspace_key=runner.workspace_key,
            argv=(
                str(Path(sys.executable).resolve()),
                "-m",
                "venv",
                ".venv/local",
            ),
            profile=ExecutionProfile.DEPENDENCY,
        )
    )

    assert result.returncode == 0
    assert (workspace / ".venv" / "local").is_dir()


@pytest.mark.asyncio
async def test_local_runner_rejects_dependency_profile_without_full_access(
    tmp_path,
    monkeypatch,
):
    service, workspace = _workspace(tmp_path)
    runner = LocalExecutionRunner(workspace, service)

    async def web_tools_policy():
        return AgentTeamNetworkPolicy.WEB_TOOLS

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        web_tools_policy,
    )

    request = ExecutionRequest(
        workspace_key=execution_workspace_key(workspace, service),
        argv=(
            str(Path(sys.executable).resolve()),
            "-m",
            "venv",
            ".venv/local",
        ),
        profile=ExecutionProfile.DEPENDENCY,
    )
    with pytest.raises(ExecutionError, match="full_access"):
        await runner.execute(request)
    assert not (workspace / ".venv" / "local").exists()


@pytest.mark.asyncio
async def test_local_runner_rejects_workspace_key_mismatch_before_spawn(
    tmp_path,
    monkeypatch,
):
    service, workspace = _workspace(tmp_path)
    runner = LocalExecutionRunner(workspace, service)

    async def fail_policy_read():
        raise AssertionError("workspace mismatch must be rejected first")

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        fail_policy_read,
    )
    request = ExecutionRequest(
        workspace_key="task-1",
        command="echo should-not-run",
        profile=ExecutionProfile.AGENT,
    )

    with pytest.raises(ExecutionError, match="workspace_key"):
        await runner.execute(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_factory",
    [
        lambda runner, key: ExecutionRequest(
            workspace_key=key,
            command="python -m venv .venv",
            profile=ExecutionProfile.DEPENDENCY,
        ),
        lambda runner, key: ExecutionRequest(
            workspace_key=key,
            argv=(str(Path(sys.executable).resolve()), "-c", "print('escape')"),
            profile=ExecutionProfile.DEPENDENCY,
        ),
        lambda runner, key: ExecutionRequest(
            workspace_key=key,
            argv=(
                str(Path(sys.executable).resolve()),
                "-m",
                "pip",
                "install",
                "--index-url",
                "https://evil.example/simple",
                "package",
            ),
            profile=ExecutionProfile.DEPENDENCY,
        ),
    ],
)
async def test_local_runner_rejects_uncontrolled_dependency_requests(
    request_factory,
    tmp_path,
    monkeypatch,
):
    service, workspace = _workspace(tmp_path)
    runner = LocalExecutionRunner(workspace, service)

    async def full_access_policy():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        full_access_policy,
    )
    request = request_factory(runner, runner.workspace_key)

    with pytest.raises(UnsupportedExecutionProfile, match="受控|不受支持"):
        await runner.execute(request)


@pytest.mark.asyncio
async def test_local_runner_rechecks_full_access_between_dependency_requests(
    tmp_path,
    monkeypatch,
):
    service, workspace = _workspace(tmp_path)
    runner = LocalExecutionRunner(workspace, service)
    policies = iter(
        (
            AgentTeamNetworkPolicy.FULL_ACCESS,
            AgentTeamNetworkPolicy.WEB_TOOLS,
        )
    )

    async def changing_policy():
        return next(policies)

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        changing_policy,
    )
    real_create_subprocess_exec = execution_module.asyncio.create_subprocess_exec
    spawned: list[tuple[object, ...]] = []

    async def observed_create(*args, **kwargs):
        spawned.append(args)
        return await real_create_subprocess_exec(*args, **kwargs)

    monkeypatch.setattr(
        execution_module.asyncio,
        "create_subprocess_exec",
        observed_create,
    )
    key = runner.workspace_key
    bootstrap = ExecutionRequest(
        workspace_key=key,
        argv=(
            str(Path(sys.executable).resolve()),
            "-m",
            "venv",
            ".venv/local",
        ),
        profile=ExecutionProfile.DEPENDENCY,
    )
    dependency = ExecutionRequest(
        workspace_key=key,
        argv=(
            str(runner.dependency_venv_python()),
            "-m",
            "pip",
            "install",
            "-r",
            "requirements.txt",
            "--quiet",
        ),
        profile=ExecutionProfile.DEPENDENCY,
    )

    first = await runner.execute(bootstrap)
    assert first.returncode == 0
    assert len(spawned) == 1

    with pytest.raises(ExecutionError, match="full_access"):
        await runner.execute(dependency)
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_local_runner_uses_fixed_environment_without_parent_secret(
    tmp_path, monkeypatch
):
    service, workspace = _workspace(tmp_path)
    monkeypatch.setenv("SAKURA_AGENT_CANARY_SECRET", "parent-secret-value")
    async def full_access_policy():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        full_access_policy,
    )
    runner = LocalExecutionRunner(workspace, service)

    result = await runner.run_args(
        [
            "python",
            "-c",
            "import os; print(os.environ.get('SAKURA_AGENT_CANARY_SECRET'))",
        ]
    )

    assert result.returncode == 0
    assert "parent-secret-value" not in result.stdout
    assert "SAKURA_AGENT_CANARY_SECRET" not in runner._build_env()


@pytest.mark.asyncio
async def test_shell_tool_uses_context_runner(tmp_path, monkeypatch):
    service, workspace = _workspace(tmp_path)

    class FakeRunner:
        def __init__(self):
            self.calls: list[tuple[str, int]] = []

        async def execute(self, request):
            assert request.command is not None
            self.calls.append((request.command, int(request.timeout_seconds)))
            return ExecutionResult(
                command=request.command,
                cwd=str(workspace),
                exit_code=0,
                stdout="runner-output",
                stderr="",
            )

    fake = FakeRunner()
    ctx = ToolContext(
        workspace=str(workspace),
        workspace_service=service,
        execution_runner=fake,
    )
    result = await ShellTool().execute({"command": "pytest -q", "timeout": 17}, ctx)

    assert result.success
    assert result.output["stdout"] == "runner-output"
    assert fake.calls == [("pytest -q", 17)]


@pytest.mark.asyncio
async def test_grep_tool_uses_context_runner_argv(tmp_path):
    service, workspace = _workspace(tmp_path)
    (workspace / "README.md").write_text("needle\n", encoding="utf-8")

    class FakeRunner:
        def __init__(self):
            self.request: ExecutionRequest | None = None

        async def execute(self, request):
            self.request = request
            return ExecutionResult(
                command=" ".join(request.argv or ()),
                cwd=str(workspace),
                exit_code=0,
                stdout="./README.md:1:needle\n",
                stderr="",
            )

    fake = FakeRunner()
    ctx = ToolContext(
        workspace=str(workspace),
        workspace_service=service,
        execution_runner=fake,
    )

    result = await GrepTool().execute({"keyword": "needle"}, ctx)

    assert result.success
    assert result.output["files"] == ["./README.md"]
    assert fake.request is not None
    assert fake.request.argv is not None
    assert fake.request.argv[-2:] == ("needle", ".")


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword", ["..", "$HOME", "/etc/passwd"])
async def test_grep_allows_path_like_literals_after_separator(tmp_path, keyword):
    service, workspace = _workspace(tmp_path)

    class FakeRunner:
        async def execute(self, request):
            assert request.argv is not None
            assert request.argv[-2] == keyword
            return ExecutionResult(
                command=" ".join(request.argv),
                cwd=str(workspace),
                exit_code=0,
                stdout="./README.md:1:literal\n",
                stderr="",
            )

    ctx = ToolContext(
        workspace=str(workspace),
        workspace_service=service,
        execution_runner=FakeRunner(),
    )
    result = await GrepTool().execute({"keyword": keyword}, ctx)

    assert result.success
    assert result.output["files"] == ["./README.md"]


def test_agent_team_subprocesses_are_centralized():
    source_root = Path(__file__).parents[1] / "backend" / "services" / "agent_team"
    forbidden_patterns = (
        "create_subprocess_",
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "os.system(",
        "os.popen(",
        "anyio.run_process(",
        "anyio.open_process(",
    )
    offenders: list[str] = []
    for path in source_root.rglob("*.py"):
        if path.name == "execution.py":
            continue
        text = path.read_text(encoding="utf-8")
        if any(pattern in text for pattern in forbidden_patterns):
            offenders.append(str(path.relative_to(source_root)))
    assert offenders == []


def test_execution_result_keeps_legacy_returncode_view():
    result = ExecutionResult(
        command="true",
        cwd=".",
        exit_code=0,
        stdout="",
        stderr="",
    )
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_timeout_result_is_explicit(tmp_path, monkeypatch):
    service, workspace = _workspace(tmp_path)
    async def full_access_policy():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        full_access_policy,
    )
    runner = LocalExecutionRunner(workspace, service)

    result = await runner.run_args(
        [
            "python",
            "-c",
            "import time; time.sleep(2)",
        ],
        timeout_seconds=0.01,
    )

    assert result.timed_out
    assert result.returncode == -1


@pytest.mark.asyncio
async def test_cancel_cleanup_failure_is_not_reported_as_cancelled(
    tmp_path, monkeypatch
):
    """A failed tree cleanup must surface as infrastructure failure."""

    service, workspace = _workspace(tmp_path)

    async def full_access_policy():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy",
        full_access_policy,
    )

    async def failed_terminate(process_tree):
        # Keep this test self-cleaning while simulating a tree-specific
        # failure: the process has no descendants, so killing the parent is
        # sufficient to avoid leaking it from the test process.
        if process_tree._process.returncode is None:
            process_tree._process.kill()
        return "simulated process-tree cleanup failure"

    monkeypatch.setattr(
        execution_module._ProcessTreeController,
        "terminate",
        failed_terminate,
    )
    cancel_event = asyncio.Event()
    cancel_event.set()
    result = await asyncio.wait_for(
        LocalExecutionRunner(workspace, service).execute(
            ExecutionRequest(
                workspace_key=execution_workspace_key(workspace, service),
                argv=("python", "-c", "import time; time.sleep(10)"),
                cancel_event=cancel_event,
            )
        ),
        timeout=4,
    )

    assert not result.cancelled
    assert result.infrastructure_error is not None
    assert "simulated process-tree cleanup failure" in result.infrastructure_error


@pytest.mark.asyncio
async def test_trusted_git_runner_uses_ephemeral_askpass_without_token_in_argv(
    tmp_path, monkeypatch
):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    captured: dict[str, object] = {}

    async def fake_execute(request):
        captured["request"] = request
        askpass = Path(request.env["GIT_ASKPASS"])
        assert askpass.read_text(encoding="utf-8")
        assert "secret-token" in (askpass.parent / "token").read_text(
            encoding="utf-8"
        )
        return ExecutionResult(
            command="git clone secret-token https://github.com/owner/repo.git .",
            cwd=str(workspace),
            exit_code=0,
            stdout="printed secret-token",
            stderr="error secret-token",
        )

    monkeypatch.setattr(runner, "execute", fake_execute)

    result = await runner.run_args(
        ["git", "clone", "https://github.com/owner/repo.git", "."],
        credential_token="secret-token",
        trusted_expected_remote="https://github.com/owner/repo.git",
    )

    request = captured["request"]
    assert isinstance(request, ExecutionRequest)
    assert request.profile is ExecutionProfile.TRUSTED_CONTROL
    assert "secret-token" not in (request.argv or ())
    assert not Path(request.env["GIT_ASKPASS"]).exists()
    assert "secret-token" not in result.command
    assert "secret-token" not in result.stdout
    assert "secret-token" not in result.stderr


def test_trusted_git_askpass_distinguishes_username_and_password(tmp_path):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    askpass_dir = runner._create_askpass("secret-token")
    askpass_path = askpass_dir / ("askpass.cmd" if os.name == "nt" else "askpass")
    try:
        script = askpass_path.read_text(encoding="utf-8")
        assert "secret-token" not in script
        assert (askpass_dir / "token").read_text(encoding="utf-8").strip() == (
            "secret-token"
        )

        def run_askpass(prompt: str) -> str:
            if os.name == "nt":
                command = ["cmd.exe", "/d", "/c", str(askpass_path), prompt]
            else:
                command = [str(askpass_path), prompt]
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
            return completed.stdout.strip()

        # Exercise the generated script instead of asserting a particular
        # source spelling (the POSIX implementation intentionally accepts
        # both ``Username`` and ``username`` prompts).
        assert run_askpass("Username for https://github.com:") == "x-access-token"
        assert run_askpass("Password for https://github.com:") == "secret-token"
        assert run_askpass("credential:") == "secret-token"
    finally:
        runner._cleanup_askpass(askpass_dir)
    assert not askpass_dir.exists()


@pytest.mark.asyncio
async def test_trusted_git_askpass_is_cleaned_on_exception_and_cancellation(
    tmp_path, monkeypatch
):
    service, workspace = _workspace(tmp_path)
    runner = TrustedGitRunner(workspace, service)
    captured: dict[str, Path] = {}

    async def fail_execute(request):
        captured["path"] = Path(request.env["GIT_ASKPASS"])
        raise RuntimeError("failed secret-token")

    monkeypatch.setattr(runner, "execute", fail_execute)
    with pytest.raises(ExecutionError) as raised:
        await runner.run_args(
            ["git", "clone", "https://github.com/owner/repo.git", "."],
            credential_token="secret-token",
            trusted_expected_remote="https://github.com/owner/repo.git",
        )
    assert "secret-token" not in str(raised.value)
    assert not captured["path"].parent.exists()

    async def leak_execute(request):
        captured["path"] = Path(request.env["GIT_ASKPASS"])
        raise ExecutionError("internal secret-token failure")

    monkeypatch.setattr(runner, "execute", leak_execute)
    with pytest.raises(ExecutionError) as raised:
        await runner.run_args(
            ["git", "clone", "https://github.com/owner/repo.git", "."],
            credential_token="secret-token",
            trusted_expected_remote="https://github.com/owner/repo.git",
        )
    assert "secret-token" not in str(raised.value)
    assert not captured["path"].parent.exists()

    async def cancel_execute(request):
        captured["path"] = Path(request.env["GIT_ASKPASS"])
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "execute", cancel_execute)
    with pytest.raises(asyncio.CancelledError):
        await runner.run_args(
            ["git", "clone", "https://github.com/owner/repo.git", "."],
            credential_token="secret-token",
            trusted_expected_remote="https://github.com/owner/repo.git",
        )
    assert not captured["path"].parent.exists()


def test_trusted_git_cleanup_reports_delete_failure(monkeypatch, tmp_path):
    attempts: list[Path] = []

    def fail_remove(path):
        attempts.append(path)
        raise OSError("busy")

    monkeypatch.setattr(
        "backend.services.agent_team.execution.shutil.rmtree", fail_remove
    )
    with pytest.raises(ExecutionError, match="清理失败"):
        TrustedGitRunner._cleanup_askpass(tmp_path / "askpass")
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_trusted_git_async_cleanup_runs_off_event_loop(tmp_path, monkeypatch):
    directory = tmp_path / "runtime"
    directory.mkdir()
    observed: list[str] = []

    async def fake_to_thread(func, *args):
        observed.append(func.__name__)
        return func(*args)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    await TrustedGitRunner._cleanup_temporary_directory_async(
        directory,
        "Git 运行时目录",
    )

    assert observed == ["_cleanup_temporary_directory"]
    assert not directory.exists()
