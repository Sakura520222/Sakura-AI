from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import pytest
from sakura_ai_sandboxer.config import SandboxdConfig
from sakura_ai_sandboxer.docker_runtime import (
    CONTAINER_GIT_COMMON,
    CONTAINER_GIT_WORKTREE_ROOT,
    DOCKER_CLI_ENV,
    FIXED_ENVIRONMENT,
    RUNNER_GID,
    RUNNER_UID,
    DockerRuntimeAdapter,
    WorkspaceOwnershipError,
    WorkspaceResolver,
    _CommandResult,
    _ContainerState,
    _decode_container_id,
    _GitMountPlan,
    _handoff_tree,
    _workspace_key_for_relative_identity,
)
from sakura_ai_sandboxer.errors import (
    CleanupFailedError,
    ImageUnavailableError,
    InvalidRequestError,
    RuntimeUnavailableError,
)
from sakura_ai_sandboxer.models import ExecutionProfile, ExecutionRequest, NetworkMode

IMAGE = "registry.example/sakura-agent@sha256:" + "a" * 64


def _workspace(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "workplace"
    task = root / "owner" / "repo" / "worktrees" / "42-feature"
    task.mkdir(parents=True)
    return root, _workspace_key_for_relative_identity("owner/repo/worktrees/42-feature")


def _request(workspace_key: str, **overrides: object) -> ExecutionRequest:
    values: dict[str, object] = {
        "request_id": "request-42",
        "workspace_key": workspace_key,
        "command": "printf 'ok'; echo --network host",
        "profile": ExecutionProfile.AGENT,
        "timeout_seconds": 5,
        "network_mode": NetworkMode.NONE,
    }
    values.update(overrides)
    return ExecutionRequest(**values)


def _config(root: Path, **overrides: object) -> SandboxdConfig:
    values: dict[str, object] = {
        "workspace_root": str(root),
        "runner_image_digest": IMAGE,
        "instance_id": "sandbox-test123",
        "cleanup_margin_seconds": 0.2,
    }
    values.update(overrides)
    return SandboxdConfig(**values)


@pytest.mark.parametrize("value", [b"container-42\v", b"container-42\f"])
def test_decode_container_id_rejects_non_crlf_control_framing(value: bytes):
    with pytest.raises(RuntimeUnavailableError):
        _decode_container_id(value)


def test_decode_container_id_accepts_crlf_framing():
    assert _decode_container_id(b"container-42\r\n") == "container-42"


@pytest.mark.asyncio
async def test_docker_lifecycle_uses_only_fixed_server_owned_argv(tmp_path: Path):
    root, key = _workspace(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        calls.append(argv)
        if argv[1] == "create":
            return _CommandResult(0, b"container-42\n")
        if argv[1] == "wait":
            return _CommandResult(0, b"0\n")
        return _CommandResult(0)

    async def log_runner(
        container_id: str,
        max_output_bytes: int,
        deadline: float,
    ) -> tuple[str, str, bool]:
        del container_id, max_output_bytes, deadline
        return "ok", "", False

    adapter = DockerRuntimeAdapter(
        _config(root),
        command_runner=command_runner,
        log_runner=log_runner,
    )
    result = await adapter.execute(
        _request(key),
        cancel_event=asyncio.Event(),
        max_output_bytes=1024,
        deadline=asyncio.get_running_loop().time() + 2,
    )

    assert result.exit_code == 0
    assert result.stdout == "ok"
    assert [argv[1] for argv in calls] == [
        "create",
        "start",
        "wait",
        "inspect",
        "kill",
        "rm",
    ]
    create = calls[0]
    assert "--network" in create and create[create.index("--network") + 1] == "none"
    assert "--read-only" in create
    assert create[create.index("--user") + 1] == "65532:65532"
    assert create[create.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges:true" in create
    assert "--pids-limit" in create
    assert "--memory" in create
    assert "--memory-swap" in create
    assert "--cpus" in create
    assert "--ulimit" in create
    assert "--tmpfs" in create
    tmpfs = [create[index + 1] for index, item in enumerate(create) if item == "--tmpfs"]
    assert tmpfs == [
        f"/tmp:rw,noexec,nosuid,nodev,size={adapter.config.tmpfs_bytes}",
        (
            f"/home/agent:rw,nosuid,nodev,uid=65532,gid=65532,mode=0700,"
            f"size={adapter.config.home_tmpfs_bytes}"
        ),
    ]
    assert any(
        item.startswith("type=bind,src=") and ",dst=/workspace" in item
        for item in create
    )
    image_index = create.index(IMAGE)
    assert create[image_index + 1 :] == (
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-lc",
        "printf 'ok'; echo --network host",
    )
    assert "--network host" not in create[:image_index]
    assert "docker.sock" not in create
    assert any(item == "ai.sakura.managed-by=sandboxd" for item in create)
    assert any(item == "ai.sakura.instance-id=sandbox-test123" for item in create)
    assert any(item == "ai.sakura.request-id=request-42" for item in create)
    assert any(item == f"ai.sakura.workspace-key={key}" for item in create)


@pytest.mark.asyncio
async def test_docker_logs_uses_supported_flags_and_fails_on_log_process_error(
    tmp_path: Path,
):
    root, _ = _workspace(tmp_path)
    observed: list[tuple[str, ...]] = []

    class _Stream:
        def __init__(self, *chunks: bytes) -> None:
            self._chunks = list(chunks)

        async def read(self, _size: int) -> bytes:
            return self._chunks.pop(0) if self._chunks else b""

    class _Process:
        def __init__(self, returncode: int, *stdout_chunks: bytes) -> None:
            self.stdout = _Stream(*stdout_chunks)
            self.stderr = _Stream(b"")
            self.returncode = returncode

        def kill(self) -> None:
            return None

        async def wait(self) -> int:
            return self.returncode

    adapter = DockerRuntimeAdapter(_config(root))

    async def spawn(argv: tuple[str, ...]) -> _Process:
        observed.append(argv)
        return _Process(0, b"ok\n", b"")

    adapter._spawn_process = spawn  # type: ignore[method-assign]
    output = await adapter._collect_logs(
        "container-42",
        1024,
        asyncio.get_running_loop().time() + 2,
    )
    assert output == ("ok\n", "", False)
    assert observed == [("docker", "logs", "--follow", "container-42")]

    async def failing_spawn(argv: tuple[str, ...]) -> _Process:
        observed.append(argv)
        return _Process(1, b"unknown flag\n", b"")

    adapter._spawn_process = failing_spawn  # type: ignore[method-assign]
    with pytest.raises(RuntimeUnavailableError, match="log collection"):
        await adapter._collect_logs(
            "container-42",
            1024,
            asyncio.get_running_loop().time() + 2,
        )


def test_linked_worktree_gets_task_metadata_and_readonly_common_mounts(
    tmp_path: Path,
):
    root, key = _workspace(tmp_path)
    workspace = root / "owner" / "repo" / "worktrees" / "42-feature"
    common = root / "owner" / "repo" / "base" / ".git"
    task_gitdir = common / "worktrees" / workspace.name
    task_gitdir.mkdir(parents=True)
    (workspace / ".git").write_text(
        "gitdir: /app/workplace/owner/repo/base/.git/worktrees/42-feature\n",
        encoding="utf-8",
    )
    (task_gitdir / "gitdir").write_text(
        "/app/workplace/owner/repo/worktrees/42-feature/.git\n",
        encoding="utf-8",
    )
    (task_gitdir / "commondir").write_text("../..\n", encoding="utf-8")

    adapter = DockerRuntimeAdapter(_config(root))
    argv = adapter.build_create_argv(
        _request(key),
        workspace=workspace,
        container_name="sakura-sandbox-sandbox-test123-request-42",
    )
    mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "--mount"]
    assert mounts[0].endswith(",dst=/workspace,bind-propagation=rprivate")
    assert ",dst=/workspace,rw," not in mounts[0]
    assert ",dst=/workspace,readonly" not in mounts[0]
    common_mount = next(
        mount
        for mount in mounts
        if f"dst={CONTAINER_GIT_COMMON}" in mount
    )
    task_mount = next(
        mount
        for mount in mounts
        if f"dst={CONTAINER_GIT_WORKTREE_ROOT}/42-feature" in mount
    )
    assert any(
        f"src={common},dst={CONTAINER_GIT_COMMON},readonly,bind-propagation=rprivate"
        in mount
        for mount in mounts
    )
    assert ",readonly" in common_mount
    assert any(
        f"src={task_gitdir},dst={CONTAINER_GIT_WORKTREE_ROOT}/42-feature,"
        "bind-propagation=rprivate"
        in mount
        for mount in mounts
    )
    assert ",readonly" not in task_mount
    assert f"GIT_DIR={CONTAINER_GIT_WORKTREE_ROOT}/42-feature" in argv
    assert f"GIT_COMMON_DIR={CONTAINER_GIT_COMMON}" in argv
    assert "GIT_WORK_TREE=/workspace" in argv


def test_linked_worktree_rejects_metadata_for_another_task(tmp_path: Path):
    root, key = _workspace(tmp_path)
    workspace = root / "owner" / "repo" / "worktrees" / "42-feature"
    common = root / "owner" / "repo" / "base" / ".git"
    task_gitdir = common / "worktrees" / workspace.name
    task_gitdir.mkdir(parents=True)
    (workspace / ".git").write_text(
        "gitdir: /app/workplace/owner/repo/base/.git/worktrees/43-other\n",
        encoding="utf-8",
    )
    (task_gitdir / "gitdir").write_text(
        "/app/workplace/owner/repo/worktrees/42-feature/.git\n",
        encoding="utf-8",
    )
    (task_gitdir / "commondir").write_text("../..\n", encoding="utf-8")

    adapter = DockerRuntimeAdapter(_config(root))
    with pytest.raises(InvalidRequestError, match="Git metadata"):
        adapter.build_create_argv(
            _request(key),
            workspace=workspace,
            container_name="sakura-sandbox-sandbox-test123-request-42",
        )


def test_linked_worktree_rejects_unsafe_task_name_for_nested_mount_destination(
    tmp_path: Path,
):
    """A metadata name must not become an unescaped nested Docker path."""

    root = tmp_path / "workplace"
    workspace = root / "owner" / "repo" / "worktrees" / "42-feature"
    common = root / "owner" / "repo" / "base" / ".git"
    task_gitdir = common / "worktrees" / "42,evil"
    task_gitdir.mkdir(parents=True)
    workspace.mkdir(parents=True)
    key = _workspace_key_for_relative_identity("owner/repo/worktrees/42-feature")

    adapter = DockerRuntimeAdapter(_config(root))
    with pytest.raises(InvalidRequestError, match="Git metadata"):
        adapter.build_create_argv(
            _request(key),
            workspace=workspace,
            container_name="sakura-sandbox-sandbox-test123-request-42",
            git_mount_plan=_GitMountPlan(task_gitdir, common),
        )


def test_linked_worktree_requires_runner_read_access_to_common_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, key = _workspace(tmp_path)
    workspace = root / "owner" / "repo" / "worktrees" / "42-feature"
    common = root / "owner" / "repo" / "base" / ".git"
    task_gitdir = common / "worktrees" / workspace.name
    task_gitdir.mkdir(parents=True)
    (workspace / ".git").write_text(
        "gitdir: /app/workplace/owner/repo/base/.git/worktrees/42-feature\n",
        encoding="utf-8",
    )
    (task_gitdir / "gitdir").write_text(
        "/app/workplace/owner/repo/worktrees/42-feature/.git\n",
        encoding="utf-8",
    )
    (task_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    monkeypatch.setattr(
        "sakura_ai_sandboxer.docker_runtime.os.access",
        lambda *args, **kwargs: False,
    )

    adapter = DockerRuntimeAdapter(_config(root))
    with pytest.raises(InvalidRequestError, match="Git metadata"):
        adapter.build_create_argv(
            _request(key),
            workspace=workspace,
            container_name="sakura-sandbox-sandbox-test123-request-42",
        )


def test_linked_worktree_handoff_protects_all_git_pointer_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, key = _workspace(tmp_path)
    workspace = root / "owner" / "repo" / "worktrees" / "42-feature"
    common = root / "owner" / "repo" / "base" / ".git"
    task_gitdir = common / "worktrees" / workspace.name
    task_gitdir.mkdir(parents=True)
    (workspace / ".git").write_text(
        "gitdir: /app/workplace/owner/repo/base/.git/worktrees/42-feature\n",
        encoding="utf-8",
    )
    (task_gitdir / "gitdir").write_text(
        "/app/workplace/owner/repo/worktrees/42-feature/.git\n",
        encoding="utf-8",
    )
    (task_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    adapter = DockerRuntimeAdapter(_config(root))
    snapshot = adapter.workspace_resolver.resolve_snapshot(key)
    plan = adapter._resolve_git_mount_plan(workspace)
    assert plan is not None
    handed_off: list[Path] = []
    readonly: list[Path] = []
    protected: list[Path] = []
    monkeypatch.setattr("sakura_ai_sandboxer.docker_runtime.os.name", "posix")
    monkeypatch.setattr(
        "sakura_ai_sandboxer.docker_runtime.os.geteuid",
        lambda: 0,
        raising=False,
    )
    monkeypatch.setattr(
        "sakura_ai_sandboxer.docker_runtime._handoff_tree",
        lambda path, *, runner_uid, runner_gid: handed_off.append(path),
    )
    monkeypatch.setattr(
        "sakura_ai_sandboxer.docker_runtime._set_owner_readonly",
        lambda path: readonly.append(path),
    )
    monkeypatch.setattr(
        "sakura_ai_sandboxer.docker_runtime._protect_pointer_parent",
        lambda path: protected.append(path),
    )

    adapter._handoff_workspace_to_runner(snapshot.path, plan)

    assert handed_off == [workspace, task_gitdir]
    assert readonly == [workspace / ".git", task_gitdir / "gitdir", task_gitdir / "commondir"]
    assert protected == [workspace, task_gitdir]


def test_runner_environment_prefers_workspace_venv_and_sets_virtualenv(tmp_path: Path):
    root, key = _workspace(tmp_path)
    adapter = DockerRuntimeAdapter(_config(root))
    argv = adapter.build_create_argv(
        _request(key),
        workspace=root / "owner" / "repo" / "worktrees" / "42-feature",
        container_name="sakura-sandbox-sandbox-test123-request-42",
    )
    assert any(
        item.startswith("PATH=/workspace/.venv/sandbox/bin:") for item in argv
    )
    assert "VIRTUAL_ENV=/workspace/.venv/sandbox" in argv
    assert "VIRTUAL_ENV=/workspace/.venv/sandbox" in FIXED_ENVIRONMENT


def test_egress_network_is_server_owned_and_agent_stays_offline(tmp_path: Path):
    root, key = _workspace(tmp_path)
    adapter = DockerRuntimeAdapter(_config(root, egress_network="sakura-egress"))
    dependency_argv = adapter.build_create_argv(
        _request(
            key,
            profile=ExecutionProfile.DEPENDENCY,
            network_mode=NetworkMode.EGRESS,
        ),
        workspace=root / "owner" / "repo" / "worktrees" / "42-feature",
        container_name="sakura-sandbox-sandbox-test123-request-42",
    )
    assert dependency_argv[dependency_argv.index("--network") + 1] == "sakura-egress"
    agent_argv = adapter.build_create_argv(
        _request(key),
        workspace=root / "owner" / "repo" / "worktrees" / "42-feature",
        container_name="sakura-sandbox-sandbox-test123-request-42",
    )
    assert agent_argv[agent_argv.index("--network") + 1] == "none"


@pytest.mark.parametrize("profile", [ExecutionProfile.AGENT, ExecutionProfile.DEPENDENCY])
def test_explicit_egress_capability_uses_server_fixed_network(
    tmp_path: Path,
    profile: ExecutionProfile,
):
    root, key = _workspace(tmp_path)
    adapter = DockerRuntimeAdapter(_config(root, egress_network="bridge"))
    argv = adapter.build_create_argv(
        _request(key, profile=profile, network_mode=NetworkMode.EGRESS),
        workspace=root / "owner" / "repo" / "worktrees" / "42-feature",
        container_name="sakura-sandbox-sandbox-test123-request-42",
    )
    assert argv[argv.index("--network") + 1] == "bridge"


def test_explicit_none_capability_keeps_both_profiles_offline(tmp_path: Path):
    root, key = _workspace(tmp_path)
    adapter = DockerRuntimeAdapter(_config(root, egress_network="sakura-egress"))
    for profile in (ExecutionProfile.AGENT, ExecutionProfile.DEPENDENCY):
        argv = adapter.build_create_argv(
            _request(key, profile=profile, network_mode=NetworkMode.NONE),
            workspace=root / "owner" / "repo" / "worktrees" / "42-feature",
            container_name="sakura-sandbox-sandbox-test123-request-42",
        )
        assert argv[argv.index("--network") + 1] == "none"


@pytest.mark.parametrize(
    "network",
    ["host", "container:other", "ns:/run/netns/x", "--network=host", "bad network", ""],
)
def test_egress_network_rejects_uncontrolled_names(tmp_path: Path, network: str):
    root, _ = _workspace(tmp_path)
    with pytest.raises(ValueError, match="egress_network"):
        _config(root, egress_network=network)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result, output",
    [
        (_CommandResult(0, b"sakura-egress\n"), True),
        (_CommandResult(1, stderr=b"network not found"), False),
        (_CommandResult(0, b"other-network\n"), False),
    ],
)
async def test_named_egress_network_is_validated_without_leaking_name(
    tmp_path: Path,
    result: _CommandResult,
    output: bool,
):
    root, _ = _workspace(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        calls.append(argv)
        return result

    adapter = DockerRuntimeAdapter(
        _config(root, egress_network="sakura-egress"),
        command_runner=command_runner,
    )
    if output:
        await adapter.validate_egress_network(
            deadline=asyncio.get_running_loop().time() + 2
        )
    else:
        with pytest.raises(RuntimeUnavailableError, match="egress network") as error:
            await adapter.validate_egress_network(
                deadline=asyncio.get_running_loop().time() + 2
            )
        assert "sakura-egress" not in str(error.value)
    assert calls == [
        (
            "docker",
            "network",
            "inspect",
            "--format={{.Name}}",
            "sakura-egress",
        )
    ]


def test_workspace_handoff_never_widens_to_world_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file_path = workspace / "main.py"
    file_path.write_text("print('ok')\n", encoding="utf-8")
    (workspace / "nested").mkdir()
    chown_calls: list[tuple[str, int, int, bool]] = []
    chmod_calls: list[tuple[str, int]] = []
    fd_chown_calls: list[tuple[int, int, int]] = []
    fd_chmod_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "sakura_ai_sandboxer.docker_runtime.os.chown",
        lambda path, uid, gid, *, follow_symlinks: chown_calls.append(
            (str(path), uid, gid, follow_symlinks)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "sakura_ai_sandboxer.docker_runtime.os.chmod",
        lambda path, mode: chmod_calls.append((str(path), mode)),
    )
    # POSIX handoff is descriptor-based.  Mock both APIs so this test does
    # not require the test process to be root (the CI runner is intentionally
    # unprivileged), while ``raising=False`` keeps the test runnable on
    # platforms whose ``os`` module has no descriptor ownership functions.
    monkeypatch.setattr(
        "sakura_ai_sandboxer.docker_runtime.os.fchown",
        lambda file_descriptor, uid, gid: fd_chown_calls.append(
            (file_descriptor, uid, gid)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "sakura_ai_sandboxer.docker_runtime.os.fchmod",
        lambda file_descriptor, mode: fd_chmod_calls.append((file_descriptor, mode)),
        raising=False,
    )

    _handoff_tree(workspace, runner_uid=RUNNER_UID, runner_gid=RUNNER_GID)

    if os.name == "posix":
        assert fd_chown_calls
        assert all(
            isinstance(file_descriptor, int)
            and file_descriptor >= 0
            and uid == RUNNER_UID
            and gid == RUNNER_GID
            for file_descriptor, uid, gid in fd_chown_calls
        )
        assert fd_chmod_calls
        assert all(
            mode & 0o002 == 0 and mode & 0o020 == 0
            for _file_descriptor, mode in fd_chmod_calls
        )
        assert all(mode != 0o777 for _file_descriptor, mode in fd_chmod_calls)
        assert not chown_calls
        assert not chmod_calls
    else:
        assert chown_calls
        assert all(
            uid == RUNNER_UID and gid == RUNNER_GID and follow_symlinks is False
            for _path, uid, gid, follow_symlinks in chown_calls
        )
        assert chmod_calls
        assert all(
            mode & 0o002 == 0 and mode & 0o020 == 0 for _path, mode in chmod_calls
        )
        assert all(mode != 0o777 for _path, mode in chmod_calls)


def test_workspace_handoff_rejects_descendant_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("data", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    monkeypatch.setattr(
        "sakura_ai_sandboxer.docker_runtime.os.chown",
        lambda *args, **kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        "sakura_ai_sandboxer.docker_runtime.os.chmod",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(WorkspaceOwnershipError, match="symlink|reparse"):
        _handoff_tree(workspace, runner_uid=RUNNER_UID, runner_gid=RUNNER_GID)


@pytest.mark.asyncio
async def test_workspace_handoff_is_reused_after_venv_creation(tmp_path: Path, monkeypatch):
    root, key = _workspace(tmp_path)
    workspace = root / "owner" / "repo" / "worktrees" / "42-feature"
    adapter = DockerRuntimeAdapter(_config(root))
    snapshot = adapter.workspace_resolver.resolve_snapshot(key)
    calls: list[Path] = []

    def fake_handoff(path: Path, _git_mount_plan):
        calls.append(path)

    monkeypatch.setattr(adapter, "_handoff_workspace_to_runner", fake_handoff)
    await adapter._ensure_workspace_handoff(snapshot, None)
    (workspace / ".venv" / "sandbox" / "bin").mkdir(parents=True)
    (workspace / ".venv" / "sandbox" / "bin" / "python").symlink_to(
        "/usr/bin/python3"
    )
    await adapter._ensure_workspace_handoff(snapshot, None)

    assert calls == [workspace]


def test_create_argv_cannot_be_changed_by_request_owned_fields(tmp_path: Path):
    root, key = _workspace(tmp_path)
    adapter = DockerRuntimeAdapter(_config(root))
    request = _request(
        key,
        request_id="req-network-runtime",
        cwd=".",
        command="--network host --privileged --volume /:/escape",
    )
    argv = adapter.build_create_argv(
        request,
        workspace=root / "owner" / "repo" / "worktrees" / "42-feature",
        container_name="sakura-sandbox-sandbox-test123-req-network-runtime",
    )
    image_index = argv.index(IMAGE)
    assert argv[image_index + 1 : image_index + 6] == (
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-lc",
        request.command,
    )
    assert argv[argv.index("--network") + 1] == "none"
    assert "--privileged" not in argv[:image_index]
    assert "--runtime" not in argv[:image_index]
    assert all("escape" not in item for item in argv[:image_index])


def test_workspace_key_resolution_rejects_unknown_alias_and_symlink(tmp_path: Path):
    root, key = _workspace(tmp_path)
    resolver = WorkspaceResolver(root)
    assert resolver.resolve(key).name == "42-feature"
    with pytest.raises(ValueError):
        resolver.resolve("owner-repo-worktrees-42-feature-deadbeefdeadbeef")

    link = root / "owner" / "repo" / "worktrees" / "alias"
    try:
        link.symlink_to(root / "owner" / "repo" / "worktrees" / "42-feature", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows runner")
    alias_key = _workspace_key_for_relative_identity("owner/repo/worktrees/alias")
    with pytest.raises(ValueError):
        resolver.resolve(alias_key)


def test_production_docker_requires_immutable_digest(tmp_path: Path):
    root, _ = _workspace(tmp_path)
    with pytest.raises(ValueError, match="digest"):
        DockerRuntimeAdapter(
            _config(root, runner_image_digest=None),
        )
    with pytest.raises(ValueError, match="explicit stable instance_id"):
        DockerRuntimeAdapter(_config(root, instance_id=None))


def test_production_docker_uses_only_the_immutable_digest(tmp_path: Path):
    root, _ = _workspace(tmp_path)
    adapter = DockerRuntimeAdapter(
        _config(root, runner_image="registry.example/runner:mutable-tag")
    )
    assert adapter.image_reference == IMAGE
    assert "mutable-tag" not in adapter.image_reference


@pytest.mark.asyncio
async def test_timeout_cancel_path_kills_and_removes_container(tmp_path: Path):
    root, key = _workspace(tmp_path)
    calls: list[tuple[str, ...]] = []
    wait_started = asyncio.Event()

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        calls.append(argv)
        if argv[1] == "create":
            return _CommandResult(0, b"container-42")
        if argv[1] == "wait":
            wait_started.set()
            await asyncio.Future()
        return _CommandResult(0)

    adapter = DockerRuntimeAdapter(
        _config(root),
        command_runner=command_runner,
        log_runner=lambda *_args: asyncio.sleep(0, result=("", "", False)),
    )
    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        adapter.execute(
            _request(key),
            cancel_event=cancel_event,
            max_output_bytes=1024,
            deadline=asyncio.get_running_loop().time() + 5,
        )
    )
    await wait_started.wait()
    cancel_event.set()
    result = await task
    assert result.cancelled is True
    assert [argv[1] for argv in calls][-2:] == ["kill", "rm"]
    assert not adapter._active


@pytest.mark.asyncio
async def test_output_budget_is_enforced_even_for_injected_log_adapter(tmp_path: Path):
    root, key = _workspace(tmp_path)

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        if argv[1] == "create":
            return _CommandResult(0, b"container-42")
        if argv[1] == "wait":
            return _CommandResult(0, b"0")
        return _CommandResult(0)

    async def log_runner(*_args: object) -> tuple[str, str, bool]:
        return "é" * 30, "stderr", False

    adapter = DockerRuntimeAdapter(
        _config(root),
        command_runner=command_runner,
        log_runner=log_runner,
    )
    result = await adapter.execute(
        _request(key),
        cancel_event=asyncio.Event(),
        max_output_bytes=10,
        deadline=asyncio.get_running_loop().time() + 2,
    )
    assert result.output_truncated is True
    assert len((result.stdout + result.stderr).encode("utf-8")) <= 10


@pytest.mark.asyncio
async def test_orphan_recovery_requires_exact_service_and_instance_labels(tmp_path: Path):
    root, _ = _workspace(tmp_path)
    calls: list[tuple[str, ...]] = []
    owned = {
        "ai.sakura.managed-by": "sandboxd",
        "ai.sakura.instance-id": "sandbox-test123",
        "ai.sakura.request-id": "request-42",
        "ai.sakura.workspace-key": "task-42",
    }

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        calls.append(argv)
        if argv[1] == "ps":
            return _CommandResult(0, b"deadbeef\n")
        if argv[1] == "inspect" and "Config.Labels" in argv[2]:
            return _CommandResult(0, json.dumps(owned).encode())
        return _CommandResult(0)

    await DockerRuntimeAdapter(_config(root), command_runner=command_runner).recover_orphans(
        deadline=asyncio.get_running_loop().time() + 2
    )
    removed = [argv[-1] for argv in calls if argv[1] == "rm"]
    assert removed == ["deadbeef"]
    ps = next(argv for argv in calls if argv[1] == "ps")
    assert "label=ai.sakura.managed-by=sandboxd" in ps
    assert "label=ai.sakura.instance-id=sandbox-test123" in ps


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inspect_result",
    [
        _CommandResult(1, stderr=b"inspect failed"),
        _CommandResult(0, b"not-json"),
        _CommandResult(0, b"{}"),
    ],
)
async def test_orphan_recovery_fails_startup_when_owned_candidate_cannot_be_verified(
    tmp_path: Path,
    inspect_result: _CommandResult,
):
    root, _ = _workspace(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        calls.append(argv)
        if argv[1] == "ps":
            return _CommandResult(0, b"deadbeef\n")
        if argv[1] == "inspect":
            return inspect_result
        return _CommandResult(0)

    with pytest.raises(RuntimeUnavailableError, match="orphan|ownership"):
        await DockerRuntimeAdapter(
            _config(root), command_runner=command_runner
        ).recover_orphans(deadline=asyncio.get_running_loop().time() + 2)
    assert not any(argv[1] == "rm" for argv in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listing",
    [
        b"not a container id\n",
        b"deadbeef\n!\n",
        b"deadbeef\v123456\n",
        b"deadbeef\f123456\n",
        b"\xff\n",
    ],
)
async def test_orphan_recovery_rejects_every_nonempty_malformed_ps_row(
    tmp_path: Path,
    listing: bytes,
):
    root, _ = _workspace(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        calls.append(argv)
        if argv[1] == "ps":
            return _CommandResult(0, listing)
        raise AssertionError("malformed ps output must fail before inspect/rm")

    with pytest.raises(RuntimeUnavailableError, match="listing|identifier|ASCII"):
        await DockerRuntimeAdapter(
            _config(root), command_runner=command_runner
        ).recover_orphans(deadline=asyncio.get_running_loop().time() + 2)
    assert [argv[1] for argv in calls] == ["ps"]


@pytest.mark.asyncio
async def test_pre_id_cleanup_workspace_label_mismatch_fences_lease(
    tmp_path: Path,
):
    root, key = _workspace(tmp_path)
    calls: list[tuple[str, ...]] = []
    labels = {
        "ai.sakura.managed-by": "sandboxd",
        "ai.sakura.instance-id": "sandbox-test123",
        "ai.sakura.request-id": "request-42",
        "ai.sakura.workspace-key": "another-workspace",
    }

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        calls.append(argv)
        if argv[1] == "create":
            return _CommandResult(1, stderr=b"create failed")
        if argv[1] == "ps":
            return _CommandResult(0, b"container-42\n")
        if argv[1] == "inspect":
            return _CommandResult(0, json.dumps(labels).encode())
        raise AssertionError(f"unexpected command: {argv}")

    adapter = DockerRuntimeAdapter(_config(root), command_runner=command_runner)
    with pytest.raises(CleanupFailedError):
        await adapter.execute(
            _request(key),
            cancel_event=asyncio.Event(),
            max_output_bytes=1024,
            deadline=asyncio.get_running_loop().time() + 2,
        )

    lease = adapter._workspace_leases["owner/repo/worktrees/42-feature"]
    assert lease.cleanup_pending is True
    with pytest.raises(InvalidRequestError, match="cleanup is still in progress"):
        await adapter._acquire_workspace_lease("request-43", lease.snapshot)
    assert [argv[1] for argv in calls] == ["create", "ps", "inspect"]


@pytest.mark.asyncio
async def test_pre_id_cleanup_inspect_exception_fences_lease(tmp_path: Path):
    root, key = _workspace(tmp_path)

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        if argv[1] == "create":
            return _CommandResult(1, stderr=b"create failed")
        if argv[1] == "ps":
            return _CommandResult(0, b"container-42\n")
        if argv[1] == "inspect":
            raise RuntimeError("inspect transport failed")
        raise AssertionError(f"unexpected command: {argv}")

    adapter = DockerRuntimeAdapter(_config(root), command_runner=command_runner)
    with pytest.raises(CleanupFailedError):
        await adapter.execute(
            _request(key),
            cancel_event=asyncio.Event(),
            max_output_bytes=1024,
            deadline=asyncio.get_running_loop().time() + 2,
        )

    lease = adapter._workspace_leases["owner/repo/worktrees/42-feature"]
    assert lease.cleanup_pending is True


@pytest.mark.asyncio
async def test_create_failure_classifies_missing_runner_image_without_echoing_cli(tmp_path: Path):
    root, key = _workspace(tmp_path)

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        if argv[1] == "ps":
            return _CommandResult(0)
        return _CommandResult(1, stderr=b"No such image: secret/path")

    adapter = DockerRuntimeAdapter(_config(root), command_runner=command_runner)
    with pytest.raises(ImageUnavailableError) as error:
        await adapter.execute(
            _request(key),
            cancel_event=asyncio.Event(),
            max_output_bytes=1024,
            deadline=asyncio.get_running_loop().time() + 2,
        )
    assert "secret/path" not in str(error.value)
    assert "/" not in str(error.value)


@pytest.mark.asyncio
async def test_create_cancelled_after_host_create_scans_and_cleans_owned_container(
    tmp_path: Path,
):
    root, key = _workspace(tmp_path)
    calls: list[tuple[str, ...]] = []
    create_started = asyncio.Event()
    labels = {
        "ai.sakura.managed-by": "sandboxd",
        "ai.sakura.instance-id": "sandbox-test123",
        "ai.sakura.request-id": "request-42",
        "ai.sakura.workspace-key": key,
    }

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        calls.append(argv)
        if argv[1] == "create":
            # Model a Docker create that happened, but whose CLI task is
            # cancelled before an ID can be returned to the adapter.
            create_started.set()
            await asyncio.Future()
        if argv[1] == "ps":
            return _CommandResult(0, b"container-42\n")
        if argv[1] == "inspect":
            return _CommandResult(0, json.dumps(labels).encode())
        return _CommandResult(0)

    adapter = DockerRuntimeAdapter(_config(root), command_runner=command_runner)
    task = asyncio.create_task(
        adapter.execute(
            _request(key),
            cancel_event=asyncio.Event(),
            max_output_bytes=1024,
            deadline=asyncio.get_running_loop().time() + 5,
        )
    )
    await create_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [argv[1] for argv in calls] == [
        "create",
        "ps",
        "inspect",
        "kill",
        "rm",
    ]
    assert not adapter._active
    assert not adapter._workspace_leases


@pytest.mark.asyncio
async def test_create_id_cancelled_while_registering_cleans_known_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, key = _workspace(tmp_path)
    calls: list[tuple[str, ...]] = []
    create_returned = asyncio.Event()

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        calls.append(argv)
        if argv[1] == "create":
            create_returned.set()
            return _CommandResult(0, b"container-42")
        if argv[1] == "start":
            raise AssertionError("registration cancellation must prevent start")
        return _CommandResult(0)

    adapter = DockerRuntimeAdapter(_config(root), command_runner=command_runner)
    snapshot = adapter.workspace_resolver.resolve_snapshot(key)
    await adapter._acquire_workspace_lease("request-42", snapshot)

    async def lease_already_acquired(
        request_id: str,
        lease_snapshot: object,
    ) -> None:
        del request_id, lease_snapshot

    # Skip the first lease lock acquisition so the held lock below targets
    # exactly the known-ID active registration boundary.
    monkeypatch.setattr(adapter, "_acquire_workspace_lease", lease_already_acquired)
    await adapter._lock.acquire()
    task = asyncio.create_task(
        adapter.execute(
            _request(key),
            cancel_event=asyncio.Event(),
            max_output_bytes=1024,
            deadline=asyncio.get_running_loop().time() + 5,
        )
    )
    try:
        await create_returned.wait()
        # Let execute consume the known ID and block on the adapter lock.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
    finally:
        adapter._lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [argv[1] for argv in calls] == ["create", "kill", "rm"]
    assert not adapter._active
    assert not adapter._workspace_leases


@pytest.mark.asyncio
async def test_pre_id_cleanup_keeps_workspace_lease_until_delayed_scan_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, key = _workspace(tmp_path)
    cleanup_release = asyncio.Event()
    create_calls: list[tuple[str, ...]] = []

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        create_calls.append(argv)
        if argv[1] == "create":
            return _CommandResult(1, stderr=b"create failed")
        raise AssertionError("delayed cleanup is replaced below")

    adapter = DockerRuntimeAdapter(
        _config(root, cleanup_margin_seconds=0.01),
        command_runner=command_runner,
    )

    async def delayed_cleanup(request_id: str, workspace_key: str, deadline: float) -> None:
        del request_id, workspace_key, deadline
        await cleanup_release.wait()

    monkeypatch.setattr(adapter, "_cleanup_owned_request", delayed_cleanup)
    with pytest.raises(CleanupFailedError, match="still in progress"):
        await adapter.execute(
            _request(key),
            cancel_event=asyncio.Event(),
            max_output_bytes=1024,
            deadline=asyncio.get_running_loop().time() + 5,
        )

    lease = adapter._workspace_leases["owner/repo/worktrees/42-feature"]
    assert lease.cleanup_pending is True
    with pytest.raises(InvalidRequestError, match="cleanup is still in progress"):
        await adapter._acquire_workspace_lease("request-43", lease.snapshot)

    cleanup_release.set()
    for _ in range(20):
        if not adapter._workspace_leases:
            break
        await asyncio.sleep(0)
    assert not adapter._workspace_leases
    assert [argv[1] for argv in create_calls] == ["create"]


@pytest.mark.asyncio
async def test_detached_pre_id_cleanup_failure_keeps_fenced_lease_and_is_consumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    root, key = _workspace(tmp_path)
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        if argv[1] == "create":
            return _CommandResult(1, stderr=b"create failed")
        raise AssertionError("ownership scan is replaced below")

    adapter = DockerRuntimeAdapter(
        _config(root, cleanup_margin_seconds=0.01),
        command_runner=command_runner,
    )

    async def failed_cleanup(request_id: str, workspace_key: str, deadline: float) -> None:
        del request_id, workspace_key, deadline
        cleanup_started.set()
        await cleanup_release.wait()
        raise CleanupFailedError()

    monkeypatch.setattr(adapter, "_cleanup_owned_request", failed_cleanup)
    with pytest.raises(CleanupFailedError, match="still in progress"):
        await adapter.execute(
            _request(key),
            cancel_event=asyncio.Event(),
            max_output_bytes=1024,
            deadline=asyncio.get_running_loop().time() + 2,
        )
    await cleanup_started.wait()
    cleanup_release.set()
    for _ in range(20):
        await asyncio.sleep(0)
    lease = adapter._workspace_leases["owner/repo/worktrees/42-feature"]
    assert lease.cleanup_pending is True
    with pytest.raises(InvalidRequestError, match="cleanup is still in progress"):
        await adapter._acquire_workspace_lease("request-43", lease.snapshot)
    assert "sandbox pre-ID cleanup failed" in caplog.text


@pytest.mark.asyncio
async def test_cleanup_worker_resolves_state_when_caller_is_cancelled(tmp_path: Path):
    root, _ = _workspace(tmp_path)
    kill_started = asyncio.Event()
    release_kill = asyncio.Event()

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        if argv[1] == "kill":
            kill_started.set()
            await release_kill.wait()
        return _CommandResult(0)

    adapter = DockerRuntimeAdapter(_config(root), command_runner=command_runner)
    state = _ContainerState("request-42", "container-name", "container-42")
    cleanup = asyncio.create_task(
        adapter._cleanup_state(
            state,
            asyncio.get_running_loop().time() + 5,
        )
    )
    await kill_started.wait()
    worker = state.cleanup_task
    assert worker is not None
    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert state.cleaning is True
    release_kill.set()
    assert await worker is True
    assert state.cleaned is True
    assert state.cleaning is False
    assert state.cleanup_future is not None and state.cleanup_future.done()


@pytest.mark.asyncio
async def test_cleanup_worker_cancelled_before_first_schedule_is_retryable(
    tmp_path: Path,
):
    root, key = _workspace(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        calls.append(argv)
        return _CommandResult(0)

    adapter = DockerRuntimeAdapter(_config(root), command_runner=command_runner)
    snapshot = adapter.workspace_resolver.resolve_snapshot(key)
    await adapter._acquire_workspace_lease("request-42", snapshot)
    state = _ContainerState(
        "request-42",
        "container-name",
        "container-42",
        workspace_key=key,
        workspace_snapshot=snapshot,
    )
    waiter = asyncio.get_running_loop().create_future()
    state.cleaning = True
    state.cleanup_future = waiter
    worker = asyncio.create_task(
        adapter._cleanup_state_worker(
            state,
            asyncio.get_running_loop().time() + 5,
        )
    )
    state.cleanup_task = worker
    worker.add_done_callback(
        lambda done: adapter._schedule_cleanup_reconciliation(state, done)
    )

    # Cancel before the coroutine receives its first scheduling turn: its
    # internal finally block cannot run, so the done-callback must reconcile.
    worker.cancel()
    for _ in range(10):
        if waiter.done():
            break
        await asyncio.sleep(0)
    assert waiter.done() and waiter.result() is False
    assert state.cleaning is False
    assert state.cleanup_task is None
    assert state.cleanup_future is None
    assert adapter._workspace_leases

    await adapter._cleanup_state(
        state,
        asyncio.get_running_loop().time() + 5,
    )
    assert [argv[1] for argv in calls] == ["kill", "rm"]
    assert state.cleaned is True
    assert not adapter._workspace_leases


@pytest.mark.asyncio
async def test_workspace_replacement_before_create_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, key = _workspace(tmp_path)
    workspace = root / "owner" / "repo" / "worktrees" / "42-feature"
    moved = root / "owner" / "repo" / "worktrees" / "42-feature-old"
    calls: list[tuple[str, ...]] = []

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        calls.append(argv)
        raise AssertionError("workspace replacement must prevent Docker create")

    adapter = DockerRuntimeAdapter(_config(root), command_runner=command_runner)
    original_build = adapter.build_create_argv

    def build_then_replace(*args: object, **kwargs: object) -> tuple[str, ...]:
        result = original_build(*args, **kwargs)
        workspace.rename(moved)
        workspace.mkdir()
        return result

    monkeypatch.setattr(adapter, "build_create_argv", build_then_replace)
    with pytest.raises(InvalidRequestError, match="workspace identity"):
        await adapter.execute(
            _request(key),
            cancel_event=asyncio.Event(),
            max_output_bytes=1024,
            deadline=asyncio.get_running_loop().time() + 5,
        )
    assert calls == []
    assert not adapter._workspace_leases


@pytest.mark.asyncio
async def test_workspace_rename_recreate_during_create_never_starts_container(
    tmp_path: Path,
):
    root, key = _workspace(tmp_path)
    workspace = root / "owner" / "repo" / "worktrees" / "42-feature"
    moved = root / "owner" / "repo" / "worktrees" / "42-feature-old"
    calls: list[tuple[str, ...]] = []

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        calls.append(argv)
        if argv[1] == "create":
            workspace.rename(moved)
            workspace.mkdir()
            return _CommandResult(0, b"container-42")
        return _CommandResult(0)

    adapter = DockerRuntimeAdapter(_config(root), command_runner=command_runner)
    with pytest.raises(InvalidRequestError, match="workspace identity"):
        await adapter.execute(
            _request(key),
            cancel_event=asyncio.Event(),
            max_output_bytes=1024,
            deadline=asyncio.get_running_loop().time() + 5,
        )
    assert "start" not in [argv[1] for argv in calls]
    assert [argv[1] for argv in calls][-2:] == ["kill", "rm"]
    assert not adapter._workspace_leases


@pytest.mark.asyncio
async def test_workspace_symlink_replacement_during_create_never_starts_container(
    tmp_path: Path,
):
    root, key = _workspace(tmp_path)
    workspace = root / "owner" / "repo" / "worktrees" / "42-feature"
    moved = root / "owner" / "repo" / "worktrees" / "42-feature-old"
    calls: list[tuple[str, ...]] = []
    try:
        workspace.rename(moved)
        workspace.symlink_to(moved, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows runner")
    finally:
        if workspace.is_symlink():
            workspace.unlink()
        if moved.exists():
            moved.rename(workspace)

    async def command_runner(argv: tuple[str, ...], deadline: float) -> _CommandResult:
        del deadline
        calls.append(argv)
        if argv[1] == "create":
            workspace.rename(moved)
            workspace.symlink_to(moved, target_is_directory=True)
            return _CommandResult(0, b"container-42")
        return _CommandResult(0)

    adapter = DockerRuntimeAdapter(_config(root), command_runner=command_runner)
    try:
        with pytest.raises(InvalidRequestError, match="workspace identity"):
            await adapter.execute(
                _request(key),
                cancel_event=asyncio.Event(),
                max_output_bytes=1024,
                deadline=asyncio.get_running_loop().time() + 5,
            )
    finally:
        if workspace.is_symlink():
            workspace.unlink()
        if moved.exists():
            moved.rename(workspace)
    assert "start" not in [argv[1] for argv in calls]
    assert [argv[1] for argv in calls][-2:] == ["kill", "rm"]


@pytest.mark.asyncio
async def test_docker_cli_uses_a_minimal_server_owned_environment(monkeypatch, tmp_path: Path):
    root, _ = _workspace(tmp_path)
    observed: dict[str, object] = {}

    class _FakeProcess:
        stdout = None
        stderr = None
        returncode = 0

        async def communicate(self):
            return b"", b""

        def kill(self):
            return None

        async def wait(self):
            return 0

    async def fake_exec(*argv: str, **kwargs: object) -> _FakeProcess:
        observed["argv"] = argv
        observed["env"] = kwargs.get("env")
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = DockerRuntimeAdapter(_config(root))
    await adapter._spawn_process(("docker", "version"))
    assert observed["env"] == DOCKER_CLI_ENV
    assert "SAKURA_AI_SECRET" not in DOCKER_CLI_ENV
    assert "DOCKER_HOST" not in DOCKER_CLI_ENV
    assert "DOCKER_CONTEXT" not in DOCKER_CLI_ENV


def test_runner_image_and_context_are_immutable_and_secret_free():
    repo = Path(__file__).resolve().parents[2]
    dockerfile = (repo / "docker" / "Dockerfile.agent-sandbox").read_text(
        encoding="utf-8"
    )
    dockerignore = (repo / "docker" / "Dockerfile.agent-sandbox.dockerignore").read_text(
        encoding="utf-8"
    )
    assert re.search(
        r"^FROM python:3\.14-slim-bookworm@sha256:[0-9a-f]{64}$",
        dockerfile,
        flags=re.MULTILINE,
    )
    assert "python:3.13" not in dockerfile
    assert "\nCOPY " not in dockerfile.upper()
    assert "\nADD " not in dockerfile.upper()
    pyproject = (repo / "sandboxer" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.14"' in pyproject
    assert "*" in dockerignore
    for denied in (".deploy/", "config/", "workplace/", ".env", "secret", "key", "token", "logs"):
        assert denied in dockerignore


def test_request_env_is_rejected_at_protocol_boundary(tmp_path: Path):
    _root, key = _workspace(tmp_path)
    with pytest.raises(ValueError):
        _request(key, env={"SECRET": "value"})
