from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from sakura_ai_sandboxer.config import SandboxdConfig
from sakura_ai_sandboxer.docker_runtime import (
    DOCKER_CLI_ENV,
    DockerRuntimeAdapter,
    WorkspaceResolver,
    _CommandResult,
    _ContainerState,
    _workspace_key_for_relative_identity,
)
from sakura_ai_sandboxer.errors import ImageUnavailableError, InvalidRequestError
from sakura_ai_sandboxer.models import ExecutionProfile, ExecutionRequest

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
            return _CommandResult(0, b"deadbeef\nforeign\n")
        if argv[1] == "inspect" and "Config.Labels" in argv[2]:
            labels = owned if argv[-1] == "deadbeef" else {**owned, "ai.sakura.instance-id": "other"}
            return _CommandResult(0, json.dumps(labels).encode())
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
