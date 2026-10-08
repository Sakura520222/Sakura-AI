"""Real Git regressions for read-only delegated change inspection.

Only temporary local repositories and harmless marker-writing helpers are used.
There are no commits, remote repositories, network requests or host Git changes.
"""

import asyncio
import os
import shlex
import shutil
import subprocess
import sys

import pytest

from backend.services.agent_team.execution import (
    ExecutionProfile,
    ExecutionRequest,
    ExecutionResult,
    LocalExecutionRunner,
    UnsupportedExecutionProfile,
    execute_request,
)
from backend.services.agent_team.network_policy import AgentTeamNetworkPolicy
from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.tools.registry import create_executor
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


@pytest.mark.asyncio
async def test_old_git_reports_tool_prerequisite_without_unsafe_retry(tmp_path):
    requests = []

    class OldGitRunner:
        def supports_profile(self, profile):
            return profile is ExecutionProfile.READ_ONLY

        async def execute(self, request):
            requests.append(request)
            return ExecutionResult(
                exit_code=129, stderr="unknown option: --no-lazy-fetch\nusage: git [...]"
            )

    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("o", "r")
    ctx = ToolContext(str(workspace), service, execution_runner=OldGitRunner())
    executor = create_executor(read_only=True)
    result = await executor.execute_raw("check_changes", {"mode": "full"}, ctx)
    assert not result.success and result.error_code == "GIT_READ_ONLY_UNSUPPORTED"
    assert "2.45" in result.error
    assert len(requests) == 1
    assert "--no-lazy-fetch" in requests[0].argv
    assert "config" in requests[0].argv
    (workspace / "normal.txt").write_text("still readable")
    read = await executor.execute_raw("read_file", {"file_path": "normal.txt"}, ctx)
    assert read.success and "still readable" in read.output["content"]


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    home = tmp_path / "git-home"
    home.mkdir()
    env = {"HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.defpath}
    git_binary = shutil.which("git")

    def git(*args, check=True):
        return subprocess.run(
            [git_binary, *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init", "--quiet")
    target = root / "evidence.txt"
    target.write_text("before\n")
    git("add", "evidence.txt")
    target.write_text("after\n")
    marker = tmp_path / "helper-ran"
    helper = tmp_path / "helper.py"
    helper.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('executed')\n"
        "mode = sys.argv[1]\n"
        "if mode == 'textconv':\n"
        "    sys.stdout.write(pathlib.Path(sys.argv[-1]).read_text())\n"
        "elif mode == 'clean':\n"
        "    sys.stdout.write(sys.stdin.read())\n"
        "elif mode == 'fsmonitor':\n"
        "    sys.stdout.buffer.write(b'token\\0')\n"
        "elif mode in {'process', 'promisor'}:\n"
        "    sys.exit(1)\n"
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(helper))}"
    service = AgentTeamWorkspaceService(tmp_path)
    runner = LocalExecutionRunner(root, service)
    ctx = ToolContext(str(root), service, execution_runner=runner)

    async def policy():
        return AgentTeamNetworkPolicy.FULL_ACCESS

    monkeypatch.setattr(
        "backend.services.agent_team.execution.get_agent_team_network_policy", policy
    )
    return root, git, command, marker, ctx


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "helper_type", ["external", "textconv", "fsmonitor", "clean", "process"]
)
@pytest.mark.parametrize("mode", ["full", "summary"])
async def test_change_inspection_never_executes_repository_helpers(
    repository, helper_type, mode
):
    root, git, command, marker, ctx = repository
    if helper_type == "external":
        git("config", "diff.external", command + " external")
    elif helper_type == "textconv":
        (root / ".gitattributes").write_text("evidence.txt diff=probe\n")
        git("config", "diff.probe.textconv", command + " textconv")
    elif helper_type == "fsmonitor":
        git("config", "core.fsmonitor", command + " fsmonitor")
        git("config", "core.fsmonitorHookVersion", "2")
    else:
        (root / ".gitattributes").write_text("evidence.txt filter=probe\n")
        git("config", f"filter.probe.{helper_type}", command + " " + helper_type)

    result = await create_executor(read_only=True).execute_raw(
        "check_changes", {"mode": mode}, ctx
    )
    assert not marker.exists(), f"{helper_type} executed during {mode} inspection"
    assert result.success and result.output["has_changes"]
    if mode == "full":
        assert "-before" in result.output["diff"] and "+after" in result.output["diff"]
    else:
        assert "evidence.txt" in result.output["stat"]


@pytest.mark.asyncio
async def test_summary_does_not_refresh_or_lock_the_index(repository):
    root, _, _, _, ctx = repository
    target = root / "evidence.txt"
    target.write_text("before\n")
    stamp = target.stat()
    os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 10_000_000_000))
    before = (root / ".git" / "index").read_bytes()
    result = await create_executor(read_only=True).execute_raw(
        "check_changes", {"mode": "summary"}, ctx
    )
    assert result.success
    assert (root / ".git" / "index").read_bytes() == before
    assert not (root / ".git" / "index.lock").exists()


@pytest.mark.asyncio
async def test_real_diff_preserves_linked_worktree_metadata(repository):
    root, _, _, _, ctx = repository
    # Construct the normal Git linked-worktree layout in this temporary
    # fixture without creating a task worktree/branch or making any commit.
    common = root.parent / "common.git"
    (root / ".git").rename(common)
    task_git = common / "worktrees" / "task"
    task_git.mkdir(parents=True)
    (task_git / "HEAD").write_bytes((common / "HEAD").read_bytes())
    index = (common / "index").read_bytes()
    (task_git / "index").write_bytes(index)
    (task_git / "commondir").write_text("../..\n")
    (task_git / "gitdir").write_text(str(root / ".git") + "\n")
    (root / ".git").write_text("gitdir: " + str(task_git) + "\n")
    result = await create_executor(read_only=True).execute_raw(
        "check_changes", {"mode": "full"}, ctx
    )
    assert result.success, result.error
    assert "-before" in result.output["diff"] and "+after" in result.output["diff"]
    assert (task_git / "index").read_bytes() == index
    assert not (task_git / "index.lock").exists()


@pytest.mark.asyncio
async def test_missing_partial_clone_blob_never_starts_lazy_fetch(repository):
    root, git, command, marker, ctx = repository
    oid = git("rev-parse", ":evidence.txt").stdout.strip()
    git("config", "core.repositoryformatversion", "1")
    git("config", "extensions.partialClone", "origin")
    git("config", "remote.origin.promisor", "true")
    git("config", "remote.origin.url", "ext::" + command + " promisor")
    git("config", "protocol.ext.allow", "always")
    (root / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
    result = await create_executor(read_only=True).execute_raw(
        "check_changes", {"mode": "full"}, ctx
    )
    assert not marker.exists(), "read-only diff started a promisor fetch helper"
    assert not result.success


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "binary,tool,args",
    [
        ("git", "check_changes", {"mode": "full"}),
        ("grep", "search_in_files", {"keyword": "after"}),
    ],
)
async def test_read_tools_ignore_workspace_binary_shadow(
    repository, binary, tool, args
):
    root, _, _, marker, ctx = repository
    bin_dir = root / ".venv" / "local" / "bin"
    bin_dir.mkdir(parents=True)
    shadow = bin_dir / binary
    shadow.write_text(
        f"#!/bin/sh\nprintf executed > {shlex.quote(str(marker))}\nexit 1\n"
    )
    shadow.chmod(0o755)
    result = await create_executor(read_only=True).execute_raw(tool, args, ctx)
    assert not marker.exists(), f"workspace {binary} shadow executed"
    assert result.success, result.error


@pytest.mark.asyncio
async def test_filter_added_after_preflight_cannot_write(repository, monkeypatch):
    root, git, command, marker, ctx = repository
    (root / ".gitattributes").write_text("evidence.txt filter=late\n")
    from backend.services.agent_team.tools.git_diff_tool import GitDiffTool

    original = GitDiffTool._read_only_prefix

    async def race(self, context):
        prefix = await original(self, context)
        git("config", "filter.late.clean", command + " clean")
        git("config", "filter.late.required", "true")
        return prefix

    monkeypatch.setattr(GitDiffTool, "_read_only_prefix", race)
    result = await create_executor(read_only=True).execute_raw(
        "check_changes", {"mode": "full"}, ctx
    )
    assert not marker.exists(), "post-preflight filter acquired host write authority"
    assert not result.success, "blocked required filter must remain an observable error"


@pytest.mark.parametrize(
    "argv",
    [
        ("sh", "-c", "true"),
        ("rg", "needle"),
        ("git", "config", "x.y", "unsafe"),
        ("grep", "--include=*.py", "x", "/etc"),
    ],
)
def test_readonly_profile_rejects_non_inspection_arguments(argv):
    with pytest.raises(ValueError, match="read-only"):
        ExecutionRequest(
            workspace_key="test", profile=ExecutionProfile.READ_ONLY, argv=argv
        )


def test_linux_kernel_boundary_denies_writes_metadata_and_sockets(tmp_path):
    from backend.services.agent_team import readonly_process

    target = tmp_path / "existing"
    target.write_text("original")
    script = f"""
import importlib.util, os, resource, socket
spec = importlib.util.spec_from_file_location('boundary', {readonly_process.__file__!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.restrict_process('/usr/bin/git')
target = {str(target)!r}
limits = resource.getrlimit(resource.RLIMIT_NOFILE)
operations = [
    lambda: open(target, 'w'),
    lambda: os.open(target, os.O_RDONLY | os.O_TRUNC),
    lambda: os.chmod(target, 0o777),
    lambda: os.utime(target, None),
    lambda: os.unlink(target),
    lambda: os.mkdir(target + '-dir'),
    lambda: socket.socket(),
    lambda: resource.prlimit(0, resource.RLIMIT_NOFILE, limits),
    lambda: os.execv('/bin/sh', ['sh', '-c', 'true']),
]
for operation in operations:
    try:
        operation()
    except PermissionError:
        continue
    raise AssertionError('kernel permitted forbidden operation')
print('denied', len(operations))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "denied 9"
    assert target.read_text() == "original"


@pytest.mark.asyncio
async def test_cancel_readonly_search_preserves_runner_cancellation(repository):
    _, _, _, marker, ctx = repository
    ctx.cancel_event = asyncio.Event()
    ctx.cancel_event.set()
    with pytest.raises(asyncio.CancelledError):
        await create_executor(read_only=True).execute_raw(
            "search_in_files", {"keyword": "after"}, ctx
        )
    assert not marker.exists()


@pytest.mark.asyncio
async def test_runner_cancels_real_readonly_child_after_spawn(repository, monkeypatch):
    _, _, _, _, ctx = repository
    event = asyncio.Event()
    original = asyncio.create_subprocess_exec
    child = None

    async def spawn(*args, **kwargs):
        nonlocal child
        child = await original(*args, **kwargs)
        event.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    runner = ctx.execution_runner
    result = await runner.execute(
        ExecutionRequest(
            workspace_key=runner.workspace_key,
            profile=ExecutionProfile.READ_ONLY,
            argv=("grep", "-rl", "-Z", "-I", "--", "^", "."),
            cancel_event=event,
        )
    )
    assert result.cancelled and child.returncode is not None


@pytest.mark.asyncio
async def test_unsupported_local_platform_fails_closed(repository, monkeypatch):
    _, _, _, marker, ctx = repository
    monkeypatch.setattr("backend.services.agent_team.execution.sys.platform", "darwin")
    result = await create_executor(read_only=True).execute_raw(
        "search_in_files", {"keyword": "after"}, ctx
    )
    assert not result.success and "UnsupportedExecutionProfile" in result.error
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("advertised", [None, False])
async def test_unknown_or_disabled_readonly_backend_never_executes(advertised):
    class UnsupportedRunner:
        called = False

        async def execute(self, request):
            self.called = True

    runner = UnsupportedRunner()
    if advertised is not None:
        runner.supports_profile = lambda profile: advertised
    request = ExecutionRequest(
        workspace_key="test",
        profile=ExecutionProfile.READ_ONLY,
        argv=("grep", "-rl", "-Z", "-I", "--", "^", "."),
    )
    with pytest.raises(UnsupportedExecutionProfile, match="read_only"):
        await execute_request(runner, request)
    assert not runner.called


@pytest.mark.parametrize("missing", ["kernel", "seccomp"])
def test_unavailable_kernel_or_library_never_executes_target(tmp_path, missing):
    from backend.services.agent_team import readonly_process

    script = f"""
import importlib.util, sys
spec = importlib.util.spec_from_file_location('boundary', {readonly_process.__file__!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original = module.ctypes.CDLL
class NoLandlock:
    def syscall(self, *args):
        return -1
def library(name, **kwargs):
    if {missing!r} == 'kernel' and name is None:
        return NoLandlock()
    if {missing!r} == 'seccomp' and name == 'libseccomp.so.2':
        raise OSError('libseccomp.so.2 unavailable')
    return original(name, **kwargs)
module.ctypes.CDLL = library
sys.argv = ['launcher', '/usr/bin/git', '--version']
sys.exit(module.main())
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 126
    assert not result.stdout
    assert "READ_ONLY_UNAVAILABLE" in result.stderr
    assert (
        "Landlock ABI" if missing == "kernel" else "libseccomp.so.2"
    ) in result.stderr
