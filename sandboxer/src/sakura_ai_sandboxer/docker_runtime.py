"""Server-owned Docker/OCI runtime for one-shot Agent executions.

The module deliberately keeps the Docker CLI behind a small adapter.  No
request field is ever appended to the host-side Docker option list: request
values are used only as validated container arguments, a validated workdir,
or bounded ownership labels.  The daemon must be the only process with access
to the Docker socket/CLI; the runner image never receives that socket.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .config import SandboxdConfig
from .errors import (
    CleanupFailedError,
    ImageUnavailableError,
    InvalidRequestError,
    RuntimeUnavailableError,
)
from .models import (
    REQUEST_ID_PATTERN,
    WORKSPACE_KEY_PATTERN,
    ExecutionRequest,
    NetworkMode,
)
from .runtime import RuntimeAdapter, RuntimeResult

logger = logging.getLogger(__name__)

_REQUEST_ID_RE = re.compile(REQUEST_ID_PATTERN)
_WORKSPACE_KEY_RE = re.compile(WORKSPACE_KEY_PATTERN)
_CONTAINER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_DIR_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_IMAGE_MISSING_MARKERS = (
    "no such image",
    "pull access denied",
    "image not found",
    "manifest unknown",
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

SERVICE_LABEL = "ai.sakura.managed-by=sandboxd"
INSTANCE_LABEL_KEY = "ai.sakura.instance-id"
REQUEST_LABEL_KEY = "ai.sakura.request-id"
WORKSPACE_LABEL_KEY = "ai.sakura.workspace-key"
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_HOME = "/home/agent"
CONTAINER_TMP = "/tmp"
CONTAINER_GIT_ROOT = "/sakura-git"
CONTAINER_GIT_COMMON = f"{CONTAINER_GIT_ROOT}/common"
# Keep the task metadata below the common Git directory.  Git's linked
# worktree implementation resolves ``commondir`` relative to GIT_DIR; a
# sibling mount (``/sakura-git/worktree``) breaks that resolution even when
# GIT_COMMON_DIR happens to point at the right directory.  Docker permits a
# second bind mount below a read-only bind mount, so the task directory can
# retain its real relative layout while remaining the only writable subtree.
CONTAINER_GIT_WORKTREE_ROOT = f"{CONTAINER_GIT_COMMON}/worktrees"
RUNNER_UID = 65532
RUNNER_GID = 65532
# The workspace virtualenv is created by the trusted dependency admission
# step.  Keeping it first in the server-owned PATH makes every later Agent
# command use the same environment without accepting request-side env values.
FIXED_PATH = (
    f"{CONTAINER_WORKSPACE}/.venv/bin:/usr/local/sbin:/usr/local/bin:"
    "/usr/sbin:/usr/bin:/sbin:/bin"
)
FIXED_ENVIRONMENT = (
    f"HOME={CONTAINER_HOME}",
    f"PATH={FIXED_PATH}",
    f"VIRTUAL_ENV={CONTAINER_WORKSPACE}/.venv",
    "LANG=C.UTF-8",
    "LC_ALL=C.UTF-8",
    "CI=true",
    "TERM=dumb",
)
# Host-side Docker CLI receives no sandboxd/application environment.  In
# particular, Docker host/context/TLS/config variables are deliberately not
# inherited; the default local engine socket is the only supported control
# endpoint in this slice.
DOCKER_CLI_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
    "LANG": "C",
    "LC_ALL": "C",
    "DOCKER_CONFIG": "/nonexistent/docker-config",
}


class WorkspaceResolutionError(ValueError):
    """The opaque key does not map to one safe daemon-owned directory."""


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _assert_no_link_components(path: Path, *, require_exists: bool = True) -> None:
    """Reject symlink/junction aliases in a path before passing it to Docker."""

    path = Path(path)
    if require_exists and not path.exists():
        raise WorkspaceResolutionError("workspace path does not exist")
    anchor = Path(path.anchor or path.root or ".")
    parts = path.parts[1:] if path.is_absolute() else path.parts
    current = anchor
    for part in parts:
        current /= part
        if _is_link_or_reparse(current):
            raise WorkspaceResolutionError("workspace path contains a link or reparse point")


def _workspace_key_for_relative_identity(identity: str) -> str:
    """Mirror Backend's opaque key derivation without accepting host paths."""

    if not identity or identity.startswith("/") or "\x00" in identity:
        raise WorkspaceResolutionError("workspace identity is invalid")
    normalized = PurePosixPath(identity).as_posix()
    if normalized in {"", "."} or normalized.startswith("../"):
        raise WorkspaceResolutionError("workspace identity is invalid")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", normalized).strip("-.") or "workspace"
    slug = slug[: 128 - len(digest) - 1].rstrip("-.") or "workspace"
    key = f"{slug}-{digest}"
    if not _WORKSPACE_KEY_RE.fullmatch(key):
        raise WorkspaceResolutionError("workspace identity is invalid")
    return key


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """Stable host directory identity held for one container lifetime.

    ``st_dev``/``st_ino`` are the POSIX identity.  On Windows Python exposes
    the volume/file-index identity through the same fields; file attributes
    are included as an additional replacement/junction signal.  The external
    workspace manager in Phase 4 must respect the active lease below and must
    not delete or replace this directory until the container is removed.
    """

    path: Path
    relative_identity: str
    file_identity: tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class _GitMountPlan:
    """The minimum metadata view needed by a linked worktree runner.

    Git's linked-worktree ``.git`` entry is a pointer into the base checkout's
    ``.git/worktrees/<name>`` directory.  That pointer contains the Web
    container path (``/app/workplace``), which is not present in sandboxd's
    namespace.  The plan therefore resolves the task-specific metadata from
    the daemon-owned workspace root and exposes it through stable container
    paths.  The task metadata is writable for Git's index/HEAD updates; the
    common repository metadata is mounted read-only and never handed to the
    request as a writable source.  The runner destination deliberately keeps
    the task directory nested under the common destination so Git's native
    ``commondir=../..`` contract remains valid.
    """

    worktree_gitdir: Path
    common_gitdir: Path


class WorkspaceOwnershipError(RuntimeError):
    """The daemon could not establish the fixed runner ownership contract."""


class _CleanupPendingError(CleanupFailedError):
    """Cleanup is still running and the workspace lease must remain held."""

    def __init__(self, *, lease_retained: bool) -> None:
        super().__init__("sandbox execution cleanup is still in progress")
        self.lease_retained = lease_retained


def _file_identity(path: Path) -> tuple[int, int, int]:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceResolutionError("workspace identity is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or _is_link_or_reparse(path):
        raise WorkspaceResolutionError("workspace is not a real directory")
    return (
        int(getattr(metadata, "st_dev", 0)),
        int(getattr(metadata, "st_ino", 0)),
        int(getattr(metadata, "st_file_attributes", 0)),
    )


class WorkspaceResolver:
    """Resolve opaque keys under one server-owned workspace root.

    The resolver does not decode a path from the request.  It computes the
    same stable key for actual directories below the configured root and
    requires exactly one match.  Symlinks and reparse points are pruned before
    their children can be considered, so a link cannot alias another task.
    """

    def __init__(self, workspace_root: str | Path) -> None:
        root = Path(workspace_root)
        if not root.is_absolute():
            raise ValueError("workspace_root must be absolute")
        self.root = root

    def _resolve_path(self, workspace_key: str) -> Path:
        if not _WORKSPACE_KEY_RE.fullmatch(workspace_key or ""):
            raise WorkspaceResolutionError("workspace key is invalid")
        root = self.root
        _assert_no_link_components(root)
        if not root.is_dir():
            raise WorkspaceResolutionError("workspace root is unavailable")

        matches: list[Path] = []
        for directory, dir_names, _file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            directory_path = Path(directory)
            safe_names: list[str] = []
            for name in dir_names:
                child = directory_path / name
                if _is_link_or_reparse(child):
                    continue
                safe_names.append(name)
            dir_names[:] = safe_names
            try:
                _assert_no_link_components(directory_path)
                relative = directory_path.relative_to(root).as_posix()
            except (ValueError, WorkspaceResolutionError):
                continue
            if relative != "." and _workspace_key_for_relative_identity(relative) == workspace_key:
                matches.append(directory_path)

        if len(matches) != 1:
            raise WorkspaceResolutionError("workspace key is unknown or ambiguous")
        resolved = matches[0]
        _assert_no_link_components(resolved)
        if not resolved.is_dir():
            raise WorkspaceResolutionError("workspace is not a directory")
        return resolved

    def resolve(self, workspace_key: str) -> Path:
        """Resolve a key to a safe path (compatibility helper for callers)."""

        return self._resolve_path(workspace_key)

    def resolve_snapshot(self, workspace_key: str) -> WorkspaceSnapshot:
        path = self._resolve_path(workspace_key)
        try:
            relative_identity = path.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise WorkspaceResolutionError("workspace is outside its root") from exc
        return WorkspaceSnapshot(
            path=path,
            relative_identity=relative_identity,
            file_identity=_file_identity(path),
        )

    def verify_snapshot(self, snapshot: WorkspaceSnapshot) -> None:
        """Verify the directory identity has not been replaced since resolve."""

        path = snapshot.path
        _assert_no_link_components(path)
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceResolutionError("workspace moved outside its root") from exc
        if _file_identity(path) != snapshot.file_identity:
            raise WorkspaceResolutionError("workspace identity changed")

    def resolve_cwd(self, workspace: Path, cwd: str) -> str:
        if not isinstance(cwd, str) or "\x00" in cwd or "\\" in cwd:
            raise WorkspaceResolutionError("cwd is invalid")
        relative = PurePosixPath(cwd)
        if relative.is_absolute() or any(part in {"", ".."} for part in relative.parts):
            raise WorkspaceResolutionError("cwd must stay inside the workspace")
        candidate = workspace if cwd == "." else workspace.joinpath(*relative.parts)
        _assert_no_link_components(candidate)
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(workspace)
        except ValueError as exc:
            raise WorkspaceResolutionError("cwd leaves the workspace") from exc
        if not resolved.is_dir():
            raise WorkspaceResolutionError("cwd is not a directory")
        # Reject even an in-workspace symlink alias; the command receives a
        # canonical workdir only after every component has been checked.
        if resolved != candidate:
            raise WorkspaceResolutionError("cwd contains a link or alias")
        return "." if cwd == "." else PurePosixPath(*relative.parts).as_posix()


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


CommandRunner = Callable[[tuple[str, ...], float], Awaitable[_CommandResult]]
LogRunner = Callable[[str, int, float], Awaitable[tuple[str, str, bool]]]


class _ProcessLike(Protocol):
    stdout: Any
    stderr: Any
    returncode: int | None

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


@dataclass(slots=True)
class _ContainerState:
    request_id: str
    container_name: str
    container_id: str
    workspace_key: str | None = None
    workspace_snapshot: WorkspaceSnapshot | None = None
    logs_process: _ProcessLike | None = None
    logs_task: asyncio.Task[tuple[str, str, bool]] | None = None
    wait_task: asyncio.Task[_CommandResult] | None = None
    cleaned: bool = False
    cleaning: bool = False
    cleanup_future: asyncio.Future[bool] | None = None
    cleanup_task: asyncio.Task[bool] | None = None


@dataclass(slots=True)
class _WorkspaceLease:
    snapshot: WorkspaceSnapshot
    request_ids: set[str]
    cleanup_pending: bool = False


class DockerRuntimeAdapter(RuntimeAdapter):
    """A fixed-policy, one-shot Docker CLI runtime adapter."""

    name = "docker"

    def __init__(
        self,
        config: SandboxdConfig,
        *,
        command_runner: CommandRunner | None = None,
        log_runner: LogRunner | None = None,
    ) -> None:
        if not config.workspace_root:
            raise ValueError("Docker runtime requires a server-owned workspace_root")
        if config.runner_image_digest is None:
            raise ValueError("Docker production runtime requires a runner image digest")
        if config.instance_id is None:
            raise ValueError("Docker runtime requires an explicit stable instance_id")
        self.config = config
        self.workspace_resolver = WorkspaceResolver(config.workspace_root)
        self._command_runner = command_runner
        self._log_runner = log_runner
        self._active: dict[str, _ContainerState] = {}
        self._workspace_leases: dict[str, _WorkspaceLease] = {}
        self._lock = asyncio.Lock()
        # Handoff runs once for a stable workspace inode.  It must happen
        # before the dependency runner creates ``.venv``; later one-shot
        # commands reuse the handoff so legitimate venv symlinks are never
        # mistaken for a pre-existing untrusted workspace symlink.  A daemon
        # restart clears this state and revalidates the complete tree.
        self._handoff_lock = asyncio.Lock()
        self._handoff_workspaces: dict[str, tuple[int, ...]] = {}

    @property
    def image_reference(self) -> str:
        assert self.config.runner_image_digest is not None
        return self.config.runner_image_digest

    @property
    def instance_id(self) -> str:
        # Config validates and fills this value.  The assertion keeps type
        # checkers from treating the optional constructor field as untrusted.
        assert self.config.instance_id is not None
        return self.config.instance_id

    def build_create_argv(
        self,
        request: ExecutionRequest,
        *,
        workspace: Path,
        container_name: str,
        git_mount_plan: _GitMountPlan | None = None,
    ) -> tuple[str, ...]:
        """Build the complete server-owned OCI create argv for snapshot tests."""

        if request.env:
            raise InvalidRequestError("environment injection is not supported")
        workdir = self.workspace_resolver.resolve_cwd(workspace, request.cwd)
        source = str(workspace)
        if any(char in source for char in ("\n", "\r", ",")):
            raise InvalidRequestError("workspace path is not mountable")
        if not _REQUEST_ID_RE.fullmatch(request.request_id):
            raise InvalidRequestError("request id is invalid")
        if not _WORKSPACE_KEY_RE.fullmatch(request.workspace_key):
            raise InvalidRequestError("workspace key is invalid")
        if request.command is not None:
            command = (
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-lc",
                request.command,
            )
        else:
            assert request.argv is not None
            command = tuple(request.argv)

        if git_mount_plan is None:
            try:
                git_mount_plan = self._resolve_git_mount_plan(workspace)
            except WorkspaceResolutionError as exc:
                raise InvalidRequestError("workspace Git metadata is invalid") from exc
        network = "none"
        if request.network_mode is NetworkMode.EGRESS:
            # The request carries only a capability.  Resolve the concrete
            # Docker network from daemon configuration so Backend callers can
            # never submit a network name or runtime namespace.
            network = self.config.egress_network

        labels = (
            SERVICE_LABEL,
            f"{INSTANCE_LABEL_KEY}={self.instance_id}",
            f"{REQUEST_LABEL_KEY}={request.request_id}",
            f"{WORKSPACE_LABEL_KEY}={request.workspace_key}",
        )
        argv: list[str] = [
            self.config.docker_binary,
            "create",
            "--pull",
            "never",
            "--name",
            container_name,
        ]
        for label in labels:
            argv.extend(("--label", label))
        argv.extend(
            (
                "--network",
                network,
                "--read-only",
                "--user",
                "65532:65532",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                str(self.config.pids_limit),
                "--memory",
                str(self.config.memory_bytes),
                "--memory-swap",
                str(self.config.memory_bytes),
                "--cpus",
                str(self.config.cpus),
                "--ulimit",
                f"nofile={self.config.nofile_soft}:{self.config.nofile_hard}",
                "--tmpfs",
                f"{CONTAINER_TMP}:rw,noexec,nosuid,nodev,size={self.config.tmpfs_bytes}",
                "--tmpfs",
                f"{CONTAINER_HOME}:rw,nosuid,nodev,uid=65532,gid=65532,mode=0700,size={self.config.home_tmpfs_bytes}",
                "--mount",
                # A bind mount is writable by default.  ``rw`` is not a
                # standalone long-syntax field on all supported Docker
                # versions; omitting it preserves the writable default while
                # keeping the propagation policy explicit.
                f"type=bind,src={source},dst={CONTAINER_WORKSPACE},bind-propagation=rprivate",
                "--workdir",
                f"{CONTAINER_WORKSPACE}/{workdir}" if workdir != "." else CONTAINER_WORKSPACE,
                "--entrypoint",
                "",
            )
        )
        for item in FIXED_ENVIRONMENT:
            argv.extend(("--env", item))
        if git_mount_plan is not None:
            common_source = str(git_mount_plan.common_gitdir)
            worktree_source = str(git_mount_plan.worktree_gitdir)
            worktree_destination = self._container_git_worktree_path(git_mount_plan)
            for path in (common_source, worktree_source):
                if any(char in path for char in ("\n", "\r", ",")):
                    raise InvalidRequestError("workspace Git metadata path is not mountable")
            # Keep the common repository view read-only.  Only this task's
            # linked-worktree metadata receives write access for its index and
            # HEAD/log files; no other worktree metadata is writable in the
            # runner namespace.
            argv.extend(
                (
                    "--mount",
                    (
                        f"type=bind,src={common_source},dst={CONTAINER_GIT_COMMON},"
                        "readonly,bind-propagation=rprivate"
                    ),
                    "--mount",
                    (
                        f"type=bind,src={worktree_source},dst={worktree_destination},"
                        "bind-propagation=rprivate"
                    ),
                    "--env",
                    f"GIT_DIR={worktree_destination}",
                    "--env",
                    f"GIT_COMMON_DIR={CONTAINER_GIT_COMMON}",
                    "--env",
                    f"GIT_WORK_TREE={CONTAINER_WORKSPACE}",
                )
            )
        if self.config.oci_runtime:
            argv.extend(("--runtime", self.config.oci_runtime))
        argv.append(self.image_reference)
        argv.extend(command)
        return tuple(argv)

    @staticmethod
    def _container_git_worktree_path(git_mount_plan: _GitMountPlan) -> str:
        """Return the canonical nested destination for one task's Git dir.

        ``worktree_gitdir`` has already been derived below the daemon-owned
        common Git directory and its final component has passed the strict
        task-name validation.  Re-check the component here because this
        helper is also used from the argv construction boundary: a future
        caller must never be able to turn a metadata path into a Docker
        destination or option fragment.
        """

        name = git_mount_plan.worktree_gitdir.name
        if not _SAFE_DIR_RE.fullmatch(name) or name in {".", ".."}:
            raise InvalidRequestError("workspace Git metadata path is invalid")
        return f"{CONTAINER_GIT_WORKTREE_ROOT}/{name}"

    async def execute(
        self,
        request: ExecutionRequest,
        *,
        cancel_event: asyncio.Event,
        max_output_bytes: int,
        deadline: float,
    ) -> RuntimeResult:
        snapshot = self._resolve_workspace(request)
        workspace = snapshot.path
        await self._acquire_workspace_lease(request.request_id, snapshot)
        try:
            try:
                git_mount_plan = self._resolve_git_mount_plan(workspace)
            except WorkspaceResolutionError as exc:
                raise InvalidRequestError("workspace Git metadata is invalid") from exc
            # The production sandboxd is deliberately a root-owned host
            # control process.  It is the only component allowed to hand the
            # current task tree to the fixed non-root runner UID.  Test command
            # runners do not represent a Docker host and skip this filesystem
            # operation; a real adapter never does.
            if self._command_runner is None:
                try:
                    await self._ensure_workspace_handoff(snapshot, git_mount_plan)
                except WorkspaceOwnershipError as exc:
                    raise RuntimeUnavailableError(
                        "sandboxd could not establish runner workspace ownership"
                    ) from exc
            container_name = self._container_name(request.request_id)
            create_argv = self.build_create_argv(
                request,
                workspace=workspace,
                container_name=container_name,
                git_mount_plan=git_mount_plan,
            )
        except BaseException:
            await self._release_workspace_lease(request.request_id, snapshot)
            raise
        try:
            self.workspace_resolver.verify_snapshot(snapshot)
        except WorkspaceResolutionError as exc:
            await self._release_workspace_lease(request.request_id, snapshot)
            raise InvalidRequestError("workspace identity changed") from exc
        try:
            create_result = await self._run_command(create_argv, deadline)
        except BaseException as exc:
            try:
                await self._cleanup_owned_request_bounded(
                    request.request_id,
                    request.workspace_key,
                    self._cleanup_deadline_from_now(),
                    workspace_snapshot=snapshot,
                )
            except CleanupFailedError as cleanup_error:
                await self._fence_workspace_lease(request.request_id, snapshot)
                raise cleanup_error from exc
            except BaseException:
                if not await self._workspace_lease_cleanup_pending(snapshot):
                    await self._fence_workspace_lease(request.request_id, snapshot)
                raise
            await self._release_workspace_lease(request.request_id, snapshot)
            raise
        if create_result.returncode != 0:
            try:
                await self._cleanup_owned_request_bounded(
                    request.request_id,
                    request.workspace_key,
                    self._cleanup_deadline_from_now(),
                    workspace_snapshot=snapshot,
                )
            except CleanupFailedError:
                await self._fence_workspace_lease(request.request_id, snapshot)
                raise
            else:
                await self._release_workspace_lease(request.request_id, snapshot)
            if _looks_like_missing_image(create_result.stderr):
                raise ImageUnavailableError()
            raise RuntimeUnavailableError()
        try:
            container_id = _decode_container_id(create_result.stdout)
        except RuntimeUnavailableError:
            try:
                await self._cleanup_owned_request_bounded(
                    request.request_id,
                    request.workspace_key,
                    self._cleanup_deadline_from_now(),
                    workspace_snapshot=snapshot,
                )
            except CleanupFailedError:
                await self._fence_workspace_lease(request.request_id, snapshot)
                raise
            else:
                await self._release_workspace_lease(request.request_id, snapshot)
            raise
        state = _ContainerState(
            request.request_id,
            container_name,
            container_id,
            workspace_key=request.workspace_key,
            workspace_snapshot=snapshot,
        )
        try:
            async with self._lock:
                self._active[request.request_id] = state
        except BaseException as exc:
            # The container already exists even if registration was cancelled
            # while waiting for the adapter lock.  It must use the known-ID
            # kill/rm path, not the weaker create-without-ID ownership scan.
            try:
                await self._cleanup_state(
                    state,
                    self._cleanup_deadline_from_now(),
                )
            except CleanupFailedError as cleanup_exc:
                await self._fence_workspace_lease(request.request_id, snapshot)
                raise cleanup_exc from exc
            raise
        try:
            # The create call can race a workspace manager rename/recreate.  A
            # changed identity is never started; cleanup removes the newly
            # created container first.  Docker resolves a bind source while
            # ``create`` establishes the container mount configuration; a
            # later host rename does not make that mount an atomic filesystem
            # transaction.  This check is therefore a lease/admission guard,
            # not a claim that an uncooperative host root is in the threat
            # model.  Phase 4 workspace management must honor this lease and
            # avoid deleting active workspace paths.
            try:
                self.workspace_resolver.verify_snapshot(snapshot)
            except WorkspaceResolutionError as exc:
                raise InvalidRequestError("workspace identity changed") from exc
            start_result = await self._run_command(
                (self.config.docker_binary, "start", container_id),
                deadline,
            )
            if start_result.returncode != 0:
                raise RuntimeUnavailableError()
            state.logs_task = asyncio.create_task(
                self._collect_logs(container_id, max_output_bytes, deadline),
                name=f"sandbox-docker-logs-{request.request_id}",
            )
            state.wait_task = asyncio.create_task(
                self._run_command(
                    (self.config.docker_binary, "wait", container_id),
                    deadline,
                ),
                name=f"sandbox-docker-wait-{request.request_id}",
            )
            cancel_task = asyncio.create_task(cancel_event.wait())
            try:
                done, _ = await self._wait_tasks(
                    (state.wait_task, cancel_task),
                    deadline,
                )
            finally:
                cancel_task.cancel()
                _detach_task(cancel_task)
            if cancel_task in done or cancel_event.is_set():
                await self._cleanup_state(state, self._cleanup_deadline(deadline))
                return RuntimeResult(exit_code=None, cancelled=True)
            wait_result = state.wait_task.result()
            if wait_result.returncode != 0:
                raise RuntimeUnavailableError()
            exit_code = _parse_exit_code(wait_result.stdout)
            stdout, stderr, truncated = await self._finish_logs(state, deadline)
            inspect_result = await self._run_command(
                (
                    self.config.docker_binary,
                    "inspect",
                    "--format={{json .State}}",
                    container_id,
                ),
                deadline,
            )
            if inspect_result.returncode != 0:
                raise RuntimeUnavailableError()
            await self._cleanup_state(state, self._cleanup_deadline(deadline))
            return RuntimeResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                output_truncated=truncated,
            )
        except TimeoutError:
            await self._cleanup_state(state, self._cleanup_deadline(deadline))
            return RuntimeResult(exit_code=None, timed_out=True)
        except asyncio.CancelledError:
            # Service-level hard deadlines cancel this task.  Cleanup remains
            # bounded and happens before propagating cancellation.
            await self._cleanup_state(state, self._cleanup_deadline(deadline))
            raise
        except Exception as exc:
            try:
                await self._cleanup_state(state, self._cleanup_deadline(deadline))
            except CleanupFailedError as cleanup_exc:
                raise cleanup_exc from exc
            raise

    async def cancel(self, request_id: str, *, deadline: float) -> None:
        async with self._lock:
            state = self._active.get(request_id)
        if state is not None:
            await self._cleanup_state(state, deadline)

    async def validate_egress_network(self, *, deadline: float) -> None:
        """Verify a configured named network before advertising readiness.

        Docker's built-in ``bridge`` network is guaranteed by the daemon and
        does not need a host-side setup step.  A named network is an explicit
        deployment dependency, so check it through a fixed ``network
        inspect`` invocation during startup.  The name remains server-side;
        neither the health payload nor the exception detail echoes it.
        """

        network = self.config.egress_network
        if network in {"none", "bridge"}:
            return
        result = await self._run_command(
            (
                self.config.docker_binary,
                "network",
                "inspect",
                "--format={{.Name}}",
                network,
            ),
            deadline,
        )
        if result.returncode != 0:
            raise RuntimeUnavailableError(
                "configured sandbox egress network is unavailable"
            )
        try:
            discovered = result.stdout.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeUnavailableError(
                "configured sandbox egress network identity is invalid"
            ) from exc
        if discovered != network:
            raise RuntimeUnavailableError(
                "configured sandbox egress network identity is invalid"
            )

    async def shutdown(self, *, deadline: float) -> None:
        async with self._lock:
            states = tuple(self._active.values())
        failure: CleanupFailedError | None = None
        for state in states:
            try:
                await self._cleanup_state(state, deadline)
            except CleanupFailedError as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    async def recover_orphans(self, *, deadline: float) -> None:
        """Remove only containers carrying this daemon instance's labels."""

        result = await self._run_command(
            (
                self.config.docker_binary,
                "ps",
                "-aq",
                "--filter",
                f"label={SERVICE_LABEL}",
                "--filter",
                f"label={INSTANCE_LABEL_KEY}={self.instance_id}",
            ),
            deadline,
        )
        if result.returncode != 0:
            raise RuntimeUnavailableError()
        identifiers = _parse_container_ids(result.stdout)
        for container_id in sorted(identifiers):
            inspect = await self._run_command(
                (
                    self.config.docker_binary,
                    "inspect",
                    "--format={{json .Config.Labels}}",
                    container_id,
                ),
                deadline,
            )
            if inspect.returncode != 0:
                # ``ps`` already proved that this container carries the
                # current daemon's service/instance filters.  An inability to
                # inspect it is therefore an ownership-verification failure,
                # not a foreign container to ignore.  Keep startup fail-closed
                # so an uninspectable runner cannot survive beside a healthy
                # daemon.
                raise RuntimeUnavailableError(
                    "sandboxd could not verify an owned orphan container"
                )
            try:
                labels = json.loads(inspect.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RuntimeUnavailableError(
                    "sandboxd orphan ownership metadata is invalid"
                )
            if not isinstance(labels, dict) or not self._owns_labels(labels):
                raise RuntimeUnavailableError(
                    "sandboxd orphan ownership labels are invalid"
                )
            state = _ContainerState("recovery", "", container_id)
            await self._cleanup_state(state, deadline)

    async def _cleanup_owned_request(
        self,
        request_id: str,
        workspace_key: str,
        deadline: float,
    ) -> None:
        """Best-effort recovery for a create/ID failure using exact labels."""

        if not _REQUEST_ID_RE.fullmatch(request_id) or not _WORKSPACE_KEY_RE.fullmatch(
            workspace_key
        ):
            return
        try:
            result = await self._run_command(
                (
                    self.config.docker_binary,
                    "ps",
                    "-aq",
                    "--filter",
                    f"label={SERVICE_LABEL}",
                    "--filter",
                    f"label={INSTANCE_LABEL_KEY}={self.instance_id}",
                    "--filter",
                    f"label={REQUEST_LABEL_KEY}={request_id}",
                ),
                deadline,
            )
        except Exception as exc:
            raise CleanupFailedError() from exc
        if result.returncode != 0:
            raise CleanupFailedError()
        try:
            identifiers = _parse_container_ids(result.stdout)
        except (RuntimeUnavailableError, UnicodeDecodeError) as exc:
            raise CleanupFailedError() from exc
        for container_id in sorted(identifiers):
            try:
                inspect = await self._run_command(
                    (
                        self.config.docker_binary,
                        "inspect",
                        "--format={{json .Config.Labels}}",
                        container_id,
                    ),
                    deadline,
                )
                if inspect.returncode != 0:
                    raise CleanupFailedError()
                labels = json.loads(inspect.stdout.decode("utf-8"))
            except Exception as exc:
                raise CleanupFailedError() from exc
            # The ps filters prove only service/instance/request ownership;
            # the workspace label is part of the exact pre-ID ownership proof.
            # A mismatch or malformed label is not a foreign container to
            # ignore: deleting it would be unsafe, while releasing the lease
            # would allow the workspace to be reused beside an unknown runner.
            if (
                not isinstance(labels, dict)
                or not self._owns_labels(labels)
                or labels.get(REQUEST_LABEL_KEY) != request_id
                or labels.get(WORKSPACE_LABEL_KEY) != workspace_key
            ):
                raise CleanupFailedError()
            await self._cleanup_state(
                _ContainerState("recovery", "", container_id),
                deadline,
            )

    async def _cleanup_owned_request_bounded(
        self,
        request_id: str,
        workspace_key: str,
        deadline: float,
        *,
        workspace_snapshot: WorkspaceSnapshot | None = None,
    ) -> None:
        """Run ownership-scan cleanup in a shielded worker.

        This path is used before a container ID is available.  Cancellation
        must not cancel the scan between ``ps`` and ``rm``; the worker is
        detached and continues under its own short absolute deadline while
        the caller preserves the original cancellation/error.
        """

        task = asyncio.create_task(
            self._cleanup_owned_request(request_id, workspace_key, deadline),
            name=f"sandbox-owned-cleanup-{request_id}",
        )
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            lease_retained = await self._retain_workspace_lease_until_cleanup(
                request_id,
                workspace_snapshot,
                task,
            )
            _detach_task(task)
            raise _CleanupPendingError(lease_retained=lease_retained)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except asyncio.CancelledError:
            lease_retained = await self._retain_workspace_lease_until_cleanup(
                request_id,
                workspace_snapshot,
                task,
            )
            _detach_task(task)
            if lease_retained:
                # Preserve service cancellation while the ownership scan
                # remains detached and the workspace lease remains held.
                raise
            raise
        except TimeoutError as exc:
            lease_retained = await self._retain_workspace_lease_until_cleanup(
                request_id,
                workspace_snapshot,
                task,
            )
            _detach_task(task)
            raise _CleanupPendingError(lease_retained=lease_retained) from exc
        except Exception:
            # A pre-ID scan that completed but could not prove ownership (or
            # could not inspect/remove a candidate) is a cleanup failure, not
            # an empty result.  Fence the workspace before propagating it so
            # no subsequent request can reuse a possibly mounted directory.
            if workspace_snapshot is not None:
                await self._fence_workspace_lease(request_id, workspace_snapshot)
            raise

    async def _retain_workspace_lease_until_cleanup(
        self,
        request_id: str,
        snapshot: WorkspaceSnapshot | None,
        cleanup_task: asyncio.Task[Any],
    ) -> bool:
        """Keep a pre-ID workspace lease until detached cleanup completes."""

        if snapshot is None:
            return False
        async with self._lock:
            lease = self._workspace_leases.get(snapshot.relative_identity)
            if (
                lease is None
                or lease.snapshot.file_identity != snapshot.file_identity
                or request_id not in lease.request_ids
            ):
                return False
            lease.cleanup_pending = True

        async def release_after_cleanup() -> None:
            cleanup_succeeded = False
            try:
                await cleanup_task
                cleanup_succeeded = True
            except BaseException as exc:
                # A detached failure is deliberately retained as a fence.
                # Consume/log it here so the task never emits an unhandled
                # late-task warning while preserving an observable failure.
                logger.error(
                    "sandbox pre-ID cleanup failed request_id=%s workspace=%s error=%s",
                    request_id,
                    snapshot.relative_identity,
                    type(exc).__name__,
                )
            finally:
                async with self._lock:
                    lease = self._workspace_leases.get(snapshot.relative_identity)
                    if (
                        lease is not None
                        and lease.snapshot.file_identity == snapshot.file_identity
                    ):
                        if cleanup_succeeded:
                            lease.cleanup_pending = False
                        else:
                            lease.cleanup_pending = True
                            lease.request_ids.add(request_id)
                if cleanup_succeeded:
                    await self._release_workspace_lease(request_id, snapshot)

        _detach_task(
            asyncio.create_task(
                release_after_cleanup(),
                name=f"sandbox-workspace-lease-release-{request_id}",
            )
        )
        return True

    async def _workspace_lease_cleanup_pending(
        self,
        snapshot: WorkspaceSnapshot,
    ) -> bool:
        async with self._lock:
            lease = self._workspace_leases.get(snapshot.relative_identity)
            return bool(
                lease is not None
                and lease.snapshot.file_identity == snapshot.file_identity
                and lease.cleanup_pending
            )

    def _resolve_workspace(self, request: ExecutionRequest) -> WorkspaceSnapshot:
        try:
            return self.workspace_resolver.resolve_snapshot(request.workspace_key)
        except WorkspaceResolutionError as exc:
            raise InvalidRequestError("workspace key cannot be resolved") from exc

    def _resolve_git_mount_plan(self, workspace: Path) -> _GitMountPlan | None:
        """Resolve linked-worktree metadata without trusting its host pointer.

        The Web container creates linked worktrees using its own mount prefix,
        commonly ``/app/workplace``.  sandboxd sees the same host directory at
        ``workspace_root`` instead, so the literal ``gitdir:`` path cannot be
        used as a Docker mount source.  We derive the only supported layout
        from the daemon-owned root and then validate both Git's metadata
        pointers before exposing it to the runner.
        """

        git_entry = workspace / ".git"
        if not os.path.lexists(str(git_entry)):
            return None
        if _is_link_or_reparse(git_entry):
            raise WorkspaceResolutionError("workspace .git entry is a link")
        if git_entry.is_dir():
            # A normal clone owns its complete metadata under the task mount;
            # no extra view is required.
            return None
        if not git_entry.is_file():
            raise WorkspaceResolutionError("workspace .git entry is invalid")
        try:
            pointer = git_entry.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise WorkspaceResolutionError("workspace .git pointer is unreadable") from exc
        lines = [line.strip() for line in pointer.splitlines() if line.strip()]
        if len(lines) != 1 or not lines[0].lower().startswith("gitdir:"):
            raise WorkspaceResolutionError("workspace .git pointer is invalid")
        raw_target = lines[0][len("gitdir:") :].strip()
        if not raw_target or len(raw_target) > 4_096 or "\x00" in raw_target:
            raise WorkspaceResolutionError("workspace .git pointer is invalid")

        root = self.workspace_resolver.root
        try:
            relative = workspace.relative_to(root)
        except ValueError as exc:
            raise WorkspaceResolutionError("workspace is outside daemon root") from exc
        relative_parts = relative.parts
        # AgentTeamWorkspaceService's contract is
        # <root>/<owner>/<repo>/worktrees/<task>.  Refuse an ambiguous layout
        # instead of guessing a metadata directory elsewhere in the root.
        if len(relative_parts) != 4 or relative_parts[-2] != "worktrees":
            raise WorkspaceResolutionError("linked worktree layout is not controlled")
        repo_relative = relative_parts[:-2]
        task_name = relative_parts[-1]
        if not _SAFE_DIR_RE.fullmatch(task_name) or task_name in {".", ".."}:
            raise WorkspaceResolutionError("linked worktree name is invalid")
        common_gitdir = root.joinpath(*repo_relative, "base", ".git")
        _assert_no_link_components(common_gitdir)
        if not common_gitdir.is_dir():
            raise WorkspaceResolutionError("linked worktree common gitdir is unavailable")

        pointer_parts = PurePosixPath(raw_target.replace("\\", "/")).parts
        if (
            len(pointer_parts) < 2
            or pointer_parts[-1] != task_name
            or "worktrees" not in pointer_parts
        ):
            raise WorkspaceResolutionError("linked worktree metadata name is invalid")
        metadata_name = task_name
        worktree_gitdir = common_gitdir / "worktrees" / metadata_name
        _assert_no_link_components(worktree_gitdir)
        if not worktree_gitdir.is_dir():
            raise WorkspaceResolutionError("linked worktree metadata is unavailable")
        try:
            worktree_gitdir.relative_to(common_gitdir / "worktrees")
        except ValueError as exc:
            raise WorkspaceResolutionError("linked worktree metadata escaped common gitdir") from exc
        if not self._path_reference_matches_path(raw_target, worktree_gitdir):
            raise WorkspaceResolutionError("linked worktree pointer is not this task metadata")

        self._validate_linked_worktree_pointer(
            worktree_gitdir,
            workspace,
            common_gitdir,
        )
        self._validate_common_gitdir_access(common_gitdir)
        return _GitMountPlan(worktree_gitdir, common_gitdir)

    def _validate_common_gitdir_access(self, common_gitdir: Path) -> None:
        """Ensure the fixed runner can read the read-only common Git view.

        The common repository metadata is deliberately never chowned or
        chmodded by sandboxd.  A linked worktree therefore needs an
        administrator-created ACL/mode that grants UID 65532 traversal and
        read access.  Check the root and the standard Git metadata roots
        before creating a container so a bind mount cannot fail later after
        the task has already started.  Existing optional paths are checked;
        this keeps empty repositories valid while still validating real
        object/ref stores.
        """

        _assert_no_link_components(common_gitdir)
        paths_and_modes: list[tuple[Path, int]] = [(common_gitdir, os.R_OK | os.X_OK)]
        for name in ("objects", "refs"):
            candidate = common_gitdir / name
            if os.path.lexists(candidate):
                paths_and_modes.append((candidate, os.R_OK | os.X_OK))
        for name in ("HEAD", "config", "packed-refs"):
            candidate = common_gitdir / name
            if os.path.lexists(candidate):
                paths_and_modes.append((candidate, os.R_OK))
        for candidate, mode in paths_and_modes:
            _assert_no_link_components(candidate)
            if _is_link_or_reparse(candidate):
                raise WorkspaceResolutionError(
                    "linked worktree common gitdir contains a link"
                )
            try:
                if os.name == "posix":
                    accessible = os.access(
                        candidate,
                        mode,
                        effective_ids=(RUNNER_UID, RUNNER_GID),
                    )
                else:
                    accessible = os.access(candidate, mode)
            except (OSError, NotImplementedError, TypeError) as exc:
                raise WorkspaceResolutionError(
                    "linked worktree common gitdir access cannot be verified"
                ) from exc
            if not accessible:
                raise WorkspaceResolutionError(
                    "linked worktree common gitdir is not readable by runner"
                )

    def _validate_linked_worktree_pointer(
        self,
        worktree_gitdir: Path,
        workspace: Path,
        common_gitdir: Path,
    ) -> None:
        """Validate task metadata before adding the two controlled mounts."""

        gitdir_pointer = worktree_gitdir / "gitdir"
        commondir_pointer = worktree_gitdir / "commondir"
        for pointer in (gitdir_pointer, commondir_pointer):
            if _is_link_or_reparse(pointer) or not pointer.is_file():
                raise WorkspaceResolutionError("linked worktree metadata pointer is invalid")
        try:
            gitdir_text = gitdir_pointer.read_text(encoding="utf-8").strip()
            commondir_text = commondir_pointer.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise WorkspaceResolutionError("linked worktree metadata pointer is unreadable") from exc
        if (
            not gitdir_text
            or "\x00" in gitdir_text
            or "\n" in gitdir_text
            or "\r" in gitdir_text
            or not commondir_text
            or "\x00" in commondir_text
            or "\n" in commondir_text
            or "\r" in commondir_text
        ):
            raise WorkspaceResolutionError("linked worktree metadata pointer is invalid")
        if not self._path_reference_matches_path(gitdir_text, workspace / ".git"):
            raise WorkspaceResolutionError(
                "linked worktree metadata points to another workspace"
            )

        common_target = Path(commondir_text)
        if not common_target.is_absolute():
            common_target = worktree_gitdir / common_target
        try:
            common_resolved = common_target.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceResolutionError("linked worktree common gitdir is invalid") from exc
        try:
            if common_resolved != common_gitdir.resolve(strict=True):
                raise WorkspaceResolutionError("linked worktree common gitdir is not the base checkout")
        except (OSError, RuntimeError) as exc:
            raise WorkspaceResolutionError("linked worktree common gitdir is unavailable") from exc

    def _path_reference_matches_path(self, raw_target: str, target: Path) -> bool:
        """Match an absolute pointer by the exact controlled relative suffix.

        A pointer written in the Web container has a different absolute mount
        prefix, but its suffix must still be exactly the daemon-resolved target
        path.  This is only used after the metadata directory itself has been
        derived from the server-owned layout, so a suffix alone cannot select
        an arbitrary mount source.  Relative pointers are rejected: the Git
        layout contract uses absolute Web-container paths for both pointers.
        """

        normalized = raw_target.replace("\\", "/").rstrip("/")
        if not normalized:
            return False
        if not PurePosixPath(normalized).is_absolute():
            return False
        try:
            relative = target.relative_to(self.workspace_resolver.root).as_posix()
        except ValueError:
            return False
        return normalized == relative or normalized.endswith("/" + relative)

    def _handoff_workspace_to_runner(
        self,
        workspace: Path,
        git_mount_plan: _GitMountPlan | None,
    ) -> None:
        """Transfer only the current task tree to UID/GID 65532.

        The sandbox daemon is a root-owned host control process.  Refusing to
        continue when that contract is absent is important: chmod 0777 or an
        unverified ACL would make the bind mount writable by unrelated host
        users.  The common linked-worktree metadata stays untouched/read-only;
        only this task's metadata directory is handed off.
        """

        if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise WorkspaceOwnershipError("sandboxd must be a root-owned POSIX control process")
        _handoff_tree(workspace, runner_uid=RUNNER_UID, runner_gid=RUNNER_GID)
        git_entry = workspace / ".git"
        if git_mount_plan is not None:
            _handoff_tree(
                git_mount_plan.worktree_gitdir,
                runner_uid=RUNNER_UID,
                runner_gid=RUNNER_GID,
            )
            # The pointer is used by the trusted Web Git control plane, not by
            # the sandbox runner (which receives GIT_DIR/GIT_COMMON_DIR).  A
            # read-only pointer prevents an Agent from redirecting later host
            # Git operations while retaining compatibility with Git discovery.
            _set_owner_readonly(git_entry)
            _protect_pointer_parent(workspace)
            _set_owner_readonly(git_mount_plan.worktree_gitdir / "gitdir")
            _set_owner_readonly(git_mount_plan.worktree_gitdir / "commondir")
            _protect_pointer_parent(git_mount_plan.worktree_gitdir)

    async def _ensure_workspace_handoff(
        self,
        snapshot: WorkspaceSnapshot,
        git_mount_plan: _GitMountPlan | None,
    ) -> None:
        """Perform the strict handoff once for this stable workspace inode."""

        handoff_identity = snapshot.file_identity
        if git_mount_plan is not None:
            try:
                handoff_identity += _file_identity(git_mount_plan.worktree_gitdir)
            except WorkspaceResolutionError as exc:
                raise WorkspaceOwnershipError(
                    "linked worktree metadata identity is unavailable"
                ) from exc
        async with self._handoff_lock:
            if (
                self._handoff_workspaces.get(snapshot.relative_identity)
                == handoff_identity
            ):
                return
            await asyncio.to_thread(
                self._handoff_workspace_to_runner,
                snapshot.path,
                git_mount_plan,
            )
            self._handoff_workspaces[snapshot.relative_identity] = (
                handoff_identity
            )

    async def _acquire_workspace_lease(
        self,
        request_id: str,
        snapshot: WorkspaceSnapshot,
    ) -> None:
        async with self._lock:
            lease = self._workspace_leases.get(snapshot.relative_identity)
            if lease is None:
                self._workspace_leases[snapshot.relative_identity] = _WorkspaceLease(
                    snapshot=snapshot,
                    request_ids={request_id},
                )
                return
            if lease.snapshot.file_identity != snapshot.file_identity:
                raise InvalidRequestError("workspace identity changed")
            if lease.cleanup_pending:
                raise InvalidRequestError("workspace cleanup is still in progress")
            lease.request_ids.add(request_id)

    async def _release_workspace_lease(
        self,
        request_id: str,
        snapshot: WorkspaceSnapshot,
    ) -> None:
        released = False
        async with self._lock:
            lease = self._workspace_leases.get(snapshot.relative_identity)
            if lease is None:
                return
            if lease.snapshot.file_identity != snapshot.file_identity:
                return
            lease.request_ids.discard(request_id)
            if not lease.request_ids:
                self._workspace_leases.pop(snapshot.relative_identity, None)
                released = True
        if released:
            # A path/inode can be removed and recreated with the same stat
            # identity (notably on filesystems that recycle inode numbers).
            # Never let a handoff cache survive the lease that established it.
            async with self._handoff_lock:
                self._handoff_workspaces.pop(snapshot.relative_identity, None)

    async def _fence_workspace_lease(
        self,
        request_id: str,
        snapshot: WorkspaceSnapshot,
    ) -> None:
        """Keep a workspace unavailable after unproven cleanup.

        A cleanup failure means the daemon cannot prove that a container no
        longer has the workspace mounted.  Retaining the request ID and a
        ``cleanup_pending`` fence is safer than releasing the lease and
        allowing a replacement task to race the still-unknown container.
        """

        async with self._lock:
            lease = self._workspace_leases.get(snapshot.relative_identity)
            if lease is None or lease.snapshot.file_identity != snapshot.file_identity:
                return
            lease.request_ids.add(request_id)
            lease.cleanup_pending = True

    def _cleanup_deadline_from_now(self) -> float:
        return time.monotonic() + self.config.cleanup_margin_seconds

    def _container_name(self, request_id: str) -> str:
        if not _REQUEST_ID_RE.fullmatch(request_id):
            raise InvalidRequestError("request id is invalid")
        value = f"sakura-sandbox-{self.instance_id}-{request_id}"
        if len(value) > 240:
            raise InvalidRequestError("request id is too long")
        return value

    def _cleanup_deadline(self, execution_deadline: float) -> float:
        return min(
            execution_deadline + self.config.cleanup_margin_seconds,
            time.monotonic() + self.config.cleanup_margin_seconds,
        )

    async def _cleanup_state(self, state: _ContainerState, deadline: float) -> None:
        task: asyncio.Task[bool]
        waiter: asyncio.Future[bool]
        async with self._lock:
            if state.cleaned:
                return
            task = state.cleanup_task
            if task is None:
                state.cleaning = True
                state.cleanup_future = asyncio.get_running_loop().create_future()
                task = asyncio.create_task(
                    self._cleanup_state_worker(state, deadline),
                    name=f"sandbox-cleanup-{state.request_id}",
                )
                state.cleanup_task = task
                task.add_done_callback(
                    lambda done, cleanup_state=state: self._schedule_cleanup_reconciliation(
                        cleanup_state,
                        done,
                    )
                )
            waiter = state.cleanup_future
            if waiter is None:
                # A failed worker normally clears both fields in its finally
                # block.  Keep this defensive branch deterministic if a task
                # dies between those assignments and a retry enters here.
                waiter = asyncio.get_running_loop().create_future()
                state.cleanup_future = waiter
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            _detach_task(task)
            raise CleanupFailedError()
        try:
            succeeded = await asyncio.wait_for(asyncio.shield(waiter), timeout=remaining)
        except asyncio.CancelledError:
            if task.cancelled():
                # A task cancelled before its first instruction never enters
                # the worker's finally block.  Reconcile it synchronously so
                # the waiter is complete and a later call can retry kill/rm.
                await self._reconcile_cleanup_task(state, task)
                raise CleanupFailedError()
            # The worker is intentionally shielded/detached.  Service-level
            # hard deadlines must return, while the worker still reaches its
            # finally block and resolves the cleanup state deterministically.
            _detach_task(task)
            raise
        except TimeoutError as exc:
            _detach_task(task)
            raise CleanupFailedError() from exc
        if not succeeded:
            raise CleanupFailedError()

    def _schedule_cleanup_reconciliation(
        self,
        state: _ContainerState,
        task: asyncio.Task[bool],
    ) -> None:
        """Reconcile a worker that ended before its own ``finally`` ran.

        ``Task.add_done_callback`` executes on the task's owning event loop,
        but the callback can also be triggered by cancellation requested from
        another thread.  Only schedule work through that loop; all state and
        future mutation remains inside ``_reconcile_cleanup_task`` under the
        adapter lock.  The callback never releases a workspace lease because
        no kill/rm was proven to complete.
        """

        try:
            task.exception()
        except BaseException:
            pass
        loop = task.get_loop()
        if loop.is_closed():
            return

        def enqueue() -> None:
            if loop.is_closed():
                return
            try:
                reconciliation = loop.create_task(
                    self._reconcile_cleanup_task(state, task),
                    name=f"sandbox-cleanup-reconcile-{state.request_id}",
                )
            except RuntimeError:
                return
            _detach_task(reconciliation)

        try:
            loop.call_soon_threadsafe(enqueue)
        except RuntimeError:
            # The daemon loop is already closing; there is no safe async lock
            # operation left to perform, and shutdown owns the final boundary.
            return

    async def _reconcile_cleanup_task(
        self,
        state: _ContainerState,
        task: asyncio.Task[bool],
    ) -> None:
        """Make pre-start/early worker cancellation retryable and observable."""

        try:
            task.result()
        except BaseException:
            pass
        async with self._lock:
            if state.cleanup_task is not task or state.cleaned:
                return
            state.cleaning = False
            state.cleanup_task = None
            future = state.cleanup_future
            state.cleanup_future = None
            if future is not None and not future.done():
                future.set_result(False)

    async def _cleanup_state_worker(
        self,
        state: _ContainerState,
        deadline: float,
    ) -> bool:
        """Kill/remove in a non-cancellable worker and always close its future."""

        failure = False
        try:
            self._cancel_child_tasks(state)
            if state.container_id:
                try:
                    kill_result = await self._run_command(
                        (
                            self.config.docker_binary,
                            "kill",
                            "--signal",
                            "KILL",
                            state.container_id,
                        ),
                        deadline,
                    )
                    # An exited container normally returns a non-zero kill
                    # status; rm -f below is still authoritative.
                    if kill_result.returncode != 0 and not _container_missing(
                        kill_result.stderr
                    ):
                        failure = True
                except BaseException:
                    failure = True
                try:
                    remove_result = await self._run_command(
                        (
                            self.config.docker_binary,
                            "rm",
                            "--force",
                            "--volumes",
                            state.container_id,
                        ),
                        deadline,
                    )
                    if remove_result.returncode != 0 and not _container_missing(
                        remove_result.stderr
                    ):
                        failure = True
                except BaseException:
                    failure = True
        except BaseException:
            # Never leave ``cleaning`` or its waiter unresolved, even if the
            # worker itself is cancelled during daemon teardown.
            failure = True
        finally:
            async with self._lock:
                state.cleaning = False
                if not failure:
                    state.cleaned = True
                elif state.workspace_snapshot is not None:
                    lease = self._workspace_leases.get(
                        state.workspace_snapshot.relative_identity
                    )
                    if (
                        lease is not None
                        and lease.snapshot.file_identity
                        == state.workspace_snapshot.file_identity
                    ):
                        lease.request_ids.add(state.request_id)
                        lease.cleanup_pending = True
                future = state.cleanup_future
                if self._active.get(state.request_id) is state and not failure:
                    self._active.pop(state.request_id, None)
                if failure:
                    state.cleanup_task = None
                    state.cleanup_future = None
                if future is not None and not future.done():
                    future.set_result(not failure)
            if not failure and state.workspace_snapshot is not None:
                await self._release_workspace_lease(
                    state.request_id,
                    state.workspace_snapshot,
                )
        return not failure

    async def _finish_logs(
        self,
        state: _ContainerState,
        deadline: float,
    ) -> tuple[str, str, bool]:
        if state.logs_task is None:
            return "", "", False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            state.logs_task.cancel()
            _detach_task(state.logs_task)
            raise TimeoutError
        try:
            return await asyncio.wait_for(state.logs_task, timeout=remaining)
        except TimeoutError:
            state.logs_task.cancel()
            _detach_task(state.logs_task)
            raise

    async def _collect_logs(
        self,
        container_id: str,
        max_output_bytes: int,
        deadline: float,
    ) -> tuple[str, str, bool]:
        if self._log_runner is not None:
            stdout, stderr, truncated = await self._log_runner(
                container_id,
                max_output_bytes,
                deadline,
            )
            stdout, stderr, bounded = _bound_text_output(
                stdout,
                stderr,
                max_output_bytes,
            )
            return stdout, stderr, truncated or bounded
        process = await self._spawn_process(
            (
                self.config.docker_binary,
                "logs",
                "--follow",
                container_id,
            )
        )
        stdout, stderr, truncated = await _read_process_output(
            process,
            max_output_bytes,
            deadline,
        )
        # ``docker logs`` emits both container streams to its normal stdout;
        # it does not accept the Docker API's ``--stdout``/``--stderr`` flags.
        # A failed log process must not be converted into a successful empty
        # result, otherwise a CLI syntax error or daemon failure is hidden from
        # the Agent caller.  When the reader deliberately killed the process
        # after an output/deadline bound, the non-zero signal status is
        # expected and the bounded result remains authoritative.
        if not truncated and process.returncode != 0:
            raise RuntimeUnavailableError("sandbox log collection failed")
        return stdout, stderr, truncated

    async def _run_command(
        self,
        argv: tuple[str, ...],
        deadline: float,
    ) -> _CommandResult:
        if self._command_runner is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            command_task = asyncio.create_task(self._command_runner(argv, deadline))
            try:
                done, _ = await asyncio.wait(
                    (command_task,),
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                command_task.cancel()
                _detach_task(command_task)
                raise
            if not done:
                command_task.cancel()
                _detach_task(command_task)
                raise TimeoutError
            result = command_task.result()
            if isinstance(result, _CommandResult):
                return result
            raise RuntimeError("command runner returned an invalid result")
        process = await self._spawn_process(argv)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            await _kill_process(process, 0.1)
            raise TimeoutError
        communicate_task = asyncio.create_task(process.communicate())
        try:
            done, _ = await asyncio.wait(
                (communicate_task,),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            communicate_task.cancel()
            _detach_task(communicate_task)
            await _kill_process(process, 0.1)
            raise
        if not done:
            communicate_task.cancel()
            _detach_task(communicate_task)
            await _kill_process(process, 0.1)
            raise TimeoutError
        stdout, stderr = communicate_task.result()
        return _CommandResult(process.returncode or 0, stdout or b"", stderr or b"")

    async def _spawn_process(self, argv: tuple[str, ...]) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=DOCKER_CLI_ENV,
            )
        except FileNotFoundError as exc:
            raise RuntimeUnavailableError() from exc
        except OSError as exc:
            raise RuntimeUnavailableError() from exc

    async def _wait_tasks(
        self,
        tasks: tuple[asyncio.Task[Any], ...],
        deadline: float,
    ) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        done, pending = await asyncio.wait(
            tasks,
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise TimeoutError
        return done, pending

    def _owns_labels(self, labels: dict[str, object]) -> bool:
        return (
            labels.get("ai.sakura.managed-by") == "sandboxd"
            and labels.get(INSTANCE_LABEL_KEY) == self.instance_id
            and isinstance(labels.get(REQUEST_LABEL_KEY), str)
            and bool(_REQUEST_ID_RE.fullmatch(labels[REQUEST_LABEL_KEY]))
            and isinstance(labels.get(WORKSPACE_LABEL_KEY), str)
            and bool(_WORKSPACE_KEY_RE.fullmatch(labels[WORKSPACE_LABEL_KEY]))
        )

    def _cancel_child_tasks(self, state: _ContainerState) -> None:
        for task in (state.logs_task, state.wait_task):
            if task is not None and not task.done():
                task.cancel()
                _detach_task(task)


def _decode_container_id(value: bytes) -> str:
    try:
        # Docker's create output is one ID framed by optional CR/LF.  Do not
        # use ``strip`` here: it would silently accept spaces, VT/FF, and
        # other control bytes around an otherwise valid identifier.
        identifier = value.decode("ascii", errors="strict").rstrip("\r\n")
    except UnicodeDecodeError as exc:
        raise RuntimeUnavailableError() from exc
    if not _CONTAINER_ID_RE.fullmatch(identifier):
        raise RuntimeUnavailableError()
    return identifier


def _parse_container_ids(value: bytes) -> set[str]:
    """Parse a Docker ``ps -aq`` result without dropping malformed rows.

    Docker emits one ASCII container ID per line.  Filtering invalid rows is
    unsafe during orphan/pre-ID recovery: a truncated or tampered listing can
    otherwise make the daemon believe every owned container was removed and
    release a workspace lease while a runner remains alive.  Any non-empty
    invalid row, including non-ASCII bytes, therefore fails closed.
    """

    try:
        text = value.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeUnavailableError(
            "sandboxd container listing is not strict ASCII"
        ) from exc
    identifiers: set[str] = set()
    # ``str.splitlines`` treats VT/FF and several other control characters as
    # record separators.  Docker's ``ps`` contract is line-oriented and only
    # CR/LF framing is valid; keeping the delimiter set explicit prevents a
    # control byte from turning one malformed listing into several valid IDs.
    for raw_line in re.split(r"\r\n|\n|\r", text):
        # Do not strip any other bytes: a non-empty whitespace/garbage row is
        # an invalid Docker listing, not a row that may be silently filtered
        # during recovery.
        if not raw_line:
            continue
        if not _CONTAINER_ID_RE.fullmatch(raw_line):
            raise RuntimeUnavailableError(
                "sandboxd container listing contains an invalid identifier"
            )
        identifiers.add(raw_line)
    return identifiers


def _parse_exit_code(value: bytes) -> int:
    try:
        code = int(value.decode("ascii", errors="strict").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeUnavailableError() from exc
    if code < 0 or code > 255:
        raise RuntimeUnavailableError()
    return code


def _looks_like_missing_image(value: bytes) -> bool:
    text = value.decode("utf-8", errors="ignore").casefold()
    return any(marker in text for marker in _IMAGE_MISSING_MARKERS)


def _container_missing(value: bytes) -> bool:
    text = value.decode("utf-8", errors="ignore").casefold()
    return "no such container" in text or "is not running" in text


def _bound_text_output(
    stdout: str,
    stderr: str,
    max_output_bytes: int,
) -> tuple[str, str, bool]:
    out_bytes = stdout.encode("utf-8", errors="replace")
    err_bytes = stderr.encode("utf-8", errors="replace")
    if len(out_bytes) + len(err_bytes) <= max_output_bytes:
        return stdout, stderr, False
    bounded_out = out_bytes[:max_output_bytes].decode("utf-8", errors="ignore")
    remaining = max_output_bytes - len(bounded_out.encode("utf-8"))
    bounded_err = err_bytes[: max(remaining, 0)].decode("utf-8", errors="ignore")
    return bounded_out, bounded_err, True


def _runner_mode(mode: int, *, directory: bool) -> int:
    """Make a node usable by the fixed runner without widening other users."""

    mode = stat.S_IMODE(mode)
    # Preserve ordinary read/execute bits but strip group/other writes and
    # set-id bits.  A repository file must never become a privilege boundary
    # inside the runner after ownership changes.  The runner owns the node
    # after handoff, so this is never a blanket 0777 permission change.
    mode &= ~(stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
    if directory:
        mode |= stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
    else:
        mode |= stat.S_IRUSR | stat.S_IWUSR
    return mode


_MOUNTINFO_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True, slots=True)
class _HandoffNode:
    """A preflight-validated node addressed relative to an open root fd."""

    parts: tuple[str, ...]
    metadata: os.stat_result


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _decode_mountinfo_path(value: str) -> str:
    return _MOUNTINFO_ESCAPE_RE.sub(
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _nested_mountpoints(path: Path) -> set[str]:
    """Return mountpoints strictly below *path* in this Linux namespace.

    ``st_dev`` is insufficient for bind mounts: a bind of the same filesystem
    can deliberately retain the same device number.  Linux mountinfo records
    the namespace mountpoint independently of the device ID, so it is the
    authoritative preflight check for the root-owned handoff.  On non-POSIX
    hosts this helper is not used by the real adapter; tests use the portable
    fallback below.
    """

    if os.name != "posix":
        return set()
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkspaceOwnershipError("workspace mountinfo is unavailable") from exc
    root = Path(os.path.abspath(path))
    nested: set[str] = set()
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            raise WorkspaceOwnershipError("workspace mountinfo is malformed")
        mountpoint = Path(_decode_mountinfo_path(fields[4]))
        try:
            relative = mountpoint.relative_to(root)
        except ValueError:
            continue
        if relative != Path("."):
            nested.add(str(mountpoint))
    return nested


def _close_fd(file_descriptor: int) -> None:
    try:
        os.close(file_descriptor)
    except OSError:
        pass


def _open_no_follow(
    name: str,
    *,
    dir_fd: int | None = None,
    directory: bool = False,
) -> int:
    """Open one node without following a final or intermediate symlink."""

    if os.name == "posix" and not hasattr(os, "O_NOFOLLOW"):
        raise WorkspaceOwnershipError("platform cannot enforce no-follow opens")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise WorkspaceOwnershipError("workspace handoff no-follow open failed") from exc


def _same_node(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
    )


def _apply_handoff_fd(
    file_descriptor: int,
    metadata: os.stat_result,
    *,
    runner_uid: int,
    runner_gid: int,
) -> None:
    if not hasattr(os, "fchown") or not hasattr(os, "fchmod"):
        raise WorkspaceOwnershipError("platform lacks descriptor ownership operations")
    try:
        os.fchown(file_descriptor, runner_uid, runner_gid)
        os.fchmod(
            file_descriptor,
            _runner_mode(metadata.st_mode, directory=stat.S_ISDIR(metadata.st_mode)),
        )
    except OSError as exc:
        raise WorkspaceOwnershipError("workspace handoff descriptor update failed") from exc


def _scan_handoff_tree_posix(
    root_fd: int,
    root_path: Path,
    root_device: int,
    nested_mounts: set[str],
) -> list[_HandoffNode]:
    """Preflight every node before mutating any ownership or mode."""

    nodes = [_HandoffNode((), os.fstat(root_fd))]
    stack: list[tuple[int, Path, tuple[str, ...]]] = [
        (os.dup(root_fd), root_path, ())
    ]
    try:
        while stack:
            directory_fd, directory_path, directory_parts = stack.pop()
            try:
                try:
                    names = os.listdir(directory_fd)
                except OSError as exc:
                    raise WorkspaceOwnershipError(
                        "workspace handoff directory scan failed"
                    ) from exc
                for raw_name in names:
                    name = os.fsdecode(raw_name)
                    child_path = directory_path / name
                    child_parts = directory_parts + (name,)
                    try:
                        metadata = os.lstat(raw_name, dir_fd=directory_fd)
                    except OSError as exc:
                        raise WorkspaceOwnershipError(
                            "workspace handoff child lstat failed"
                        ) from exc
                    if _metadata_is_link_or_reparse(metadata):
                        raise WorkspaceOwnershipError(
                            "workspace handoff refuses descendant symlinks or reparse points"
                        )
                    normalized_child = str(Path(os.path.abspath(child_path)))
                    if normalized_child in nested_mounts:
                        raise WorkspaceOwnershipError(
                            "workspace handoff refuses nested filesystem mounts"
                        )
                    if metadata.st_dev != root_device:
                        raise WorkspaceOwnershipError(
                            "workspace handoff refuses nested filesystem mounts"
                        )
                    if stat.S_ISDIR(metadata.st_mode):
                        child_fd = _open_no_follow(
                            raw_name,
                            dir_fd=directory_fd,
                            directory=True,
                        )
                        try:
                            opened = os.fstat(child_fd)
                            if not _same_node(metadata, opened):
                                raise WorkspaceOwnershipError(
                                    "workspace handoff node changed during preflight"
                                )
                            nodes.append(_HandoffNode(child_parts, opened))
                            stack.append((child_fd, child_path, child_parts))
                        except BaseException:
                            _close_fd(child_fd)
                            raise
                    elif stat.S_ISREG(metadata.st_mode):
                        if metadata.st_nlink > 1:
                            raise WorkspaceOwnershipError(
                                "workspace handoff refuses hard-linked files"
                            )
                        child_fd = _open_no_follow(raw_name, dir_fd=directory_fd)
                        try:
                            opened = os.fstat(child_fd)
                            if not _same_node(metadata, opened):
                                raise WorkspaceOwnershipError(
                                    "workspace handoff node changed during preflight"
                                )
                            nodes.append(_HandoffNode(child_parts, opened))
                        finally:
                            _close_fd(child_fd)
                    else:
                        raise WorkspaceOwnershipError(
                            "workspace handoff refuses special filesystem nodes"
                        )
            finally:
                _close_fd(directory_fd)
    finally:
        for directory_fd, _path, _parts in stack:
            _close_fd(directory_fd)
    return nodes


def _open_relative_node(root_fd: int, parts: tuple[str, ...], *, directory: bool) -> int:
    current_fd = os.dup(root_fd)
    try:
        for index, name in enumerate(parts):
            child_fd = _open_no_follow(
                name,
                dir_fd=current_fd,
                directory=directory or index < len(parts) - 1,
            )
            _close_fd(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        _close_fd(current_fd)
        raise


def _open_absolute_no_follow(path: Path, *, directory: bool = False) -> int:
    """Open an absolute path without following any ancestor component."""

    if os.name != "posix":
        return _open_no_follow(str(path), directory=directory)
    absolute = Path(os.path.abspath(path))
    root_fd = _open_no_follow("/", directory=True)
    try:
        try:
            relative = absolute.relative_to(Path("/"))
        except ValueError as exc:
            raise WorkspaceOwnershipError(
                "workspace handoff path is not an absolute POSIX path"
            ) from exc
        return _open_relative_node(root_fd, relative.parts, directory=directory)
    finally:
        _close_fd(root_fd)


def _handoff_tree_posix(
    path: Path,
    *,
    runner_uid: int,
    runner_gid: int,
) -> None:
    _assert_no_link_components(path)
    root_path = Path(os.path.abspath(path))
    nested_mounts = _nested_mountpoints(root_path)
    if nested_mounts:
        raise WorkspaceOwnershipError(
            "workspace handoff refuses pre-existing nested filesystem mounts"
        )
    root_fd = _open_absolute_no_follow(root_path, directory=True)
    try:
        root_metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise WorkspaceOwnershipError("workspace handoff root is not a directory")
        nodes = _scan_handoff_tree_posix(
            root_fd,
            root_path,
            root_metadata.st_dev,
            nested_mounts,
        )
        if _nested_mountpoints(root_path) != nested_mounts:
            raise WorkspaceOwnershipError(
                "workspace mount topology changed during handoff preflight"
            )
        for node in nodes:
            node_fd = _open_relative_node(
                root_fd,
                node.parts,
                directory=stat.S_ISDIR(node.metadata.st_mode),
            )
            try:
                opened = os.fstat(node_fd)
                if not _same_node(node.metadata, opened):
                    raise WorkspaceOwnershipError(
                        "workspace handoff node changed before ownership update"
                    )
                if stat.S_ISREG(opened.st_mode) and opened.st_nlink > 1:
                    raise WorkspaceOwnershipError(
                        "workspace handoff refuses hard-linked files"
                    )
                _apply_handoff_fd(
                    node_fd,
                    opened,
                    runner_uid=runner_uid,
                    runner_gid=runner_gid,
                )
            finally:
                _close_fd(node_fd)
        if _nested_mountpoints(root_path) != nested_mounts:
            raise WorkspaceOwnershipError(
                "workspace mount topology changed during ownership handoff"
            )
        # The mount check alone does not notice a file/directory added below
        # the root while ownership updates were in progress.  Re-scan through
        # no-follow descriptors and compare every path's inode/type so a new
        # entry is never left with attacker-controlled ownership beside the
        # handed-off tree.
        final_nodes = _scan_handoff_tree_posix(
            root_fd,
            root_path,
            root_metadata.st_dev,
            nested_mounts,
        )
        expected_nodes = {node.parts: node.metadata for node in nodes}
        observed_nodes = {node.parts: node.metadata for node in final_nodes}
        if set(observed_nodes) != set(expected_nodes):
            raise WorkspaceOwnershipError(
                "workspace directory entries changed during ownership handoff"
            )
        for parts, expected in expected_nodes.items():
            observed = observed_nodes[parts]
            if not _same_node(expected, observed):
                raise WorkspaceOwnershipError(
                    "workspace handoff node changed after ownership update"
                )
            if stat.S_ISREG(observed.st_mode) and observed.st_nlink > 1:
                raise WorkspaceOwnershipError(
                    "workspace handoff refuses hard-linked files"
                )
    finally:
        _close_fd(root_fd)


def _handoff_tree_portable(
    path: Path,
    *,
    runner_uid: int,
    runner_gid: int,
) -> None:
    """Portable test/development fallback; production uses descriptor mode."""

    _assert_no_link_components(path)
    try:
        root_metadata = os.lstat(path)
    except OSError as exc:
        raise WorkspaceOwnershipError("workspace handoff root lstat failed") from exc
    if _metadata_is_link_or_reparse(root_metadata) or not stat.S_ISDIR(
        root_metadata.st_mode
    ):
        raise WorkspaceOwnershipError("workspace handoff root is not a directory")
    root_device = root_metadata.st_dev
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise WorkspaceOwnershipError("workspace handoff lstat failed") from exc
        if _metadata_is_link_or_reparse(metadata):
            raise WorkspaceOwnershipError(
                "workspace handoff refuses descendant symlinks or reparse points"
            )
        try:
            if metadata.st_dev != root_device:
                raise WorkspaceOwnershipError(
                    "workspace handoff refuses nested filesystem mounts"
                )
            if stat.S_ISDIR(metadata.st_mode):
                os.chown(str(current), runner_uid, runner_gid, follow_symlinks=False)
                os.chmod(str(current), _runner_mode(metadata.st_mode, directory=True))
                with os.scandir(current) as entries:
                    children = []
                    for entry in entries:
                        child = Path(entry.path)
                        if _is_link_or_reparse(child):
                            raise WorkspaceOwnershipError(
                                "workspace handoff refuses descendant symlinks or reparse points"
                            )
                        children.append(child)
                    stack.extend(children)
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink > 1:
                    raise WorkspaceOwnershipError(
                        "workspace handoff refuses hard-linked files"
                    )
                os.chown(str(current), runner_uid, runner_gid, follow_symlinks=False)
                os.chmod(str(current), _runner_mode(metadata.st_mode, directory=False))
            else:
                raise WorkspaceOwnershipError(
                    "workspace handoff refuses special filesystem nodes"
                )
        except OSError as exc:
            raise WorkspaceOwnershipError("workspace handoff failed") from exc


def _handoff_tree(path: Path, *, runner_uid: int, runner_gid: int) -> None:
    """Recursively hand off one task tree under a no-follow POSIX contract."""

    if os.name == "posix":
        _handoff_tree_posix(path, runner_uid=runner_uid, runner_gid=runner_gid)
    else:
        _handoff_tree_portable(path, runner_uid=runner_uid, runner_gid=runner_gid)


def _set_owner_readonly(path: Path) -> None:
    """Keep the linked-worktree pointer readable but immutable to the runner."""

    if os.name == "posix":
        _assert_no_link_components(path)
        file_descriptor = _open_absolute_no_follow(path)
        try:
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
                raise WorkspaceOwnershipError(
                    "workspace .git pointer is not a private regular file"
                )
            try:
                os.fchown(file_descriptor, 0, 0)
                os.fchmod(file_descriptor, 0o444)
            except OSError as exc:
                raise WorkspaceOwnershipError(
                    "workspace .git pointer protection failed"
                ) from exc
        finally:
            _close_fd(file_descriptor)
        return
    try:
        metadata = os.lstat(path)
        if _metadata_is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceOwnershipError("workspace .git pointer is not a regular file")
        if metadata.st_nlink > 1:
            raise WorkspaceOwnershipError("workspace .git pointer is hard-linked")
        # ``_handoff_tree`` has already made the complete task tree runner
        # owned.  Restore this discovery pointer to the root-owned daemon
        # before making it read-only; otherwise UID 65532 could chmod it back
        # to writable from inside the bind mount and poison a later trusted
        # Git operation on the host.
        os.chown(str(path), 0, 0, follow_symlinks=False)
        os.chmod(str(path), 0o444)
    except OSError as exc:
        raise WorkspaceOwnershipError("workspace .git pointer protection failed") from exc


def _protect_pointer_parent(path: Path) -> None:
    """Protect a pointer's directory while retaining runner write access.

    A root-owned ``0444`` file alone is insufficient: a runner that owns the
    parent directory could unlink it and install a replacement.  The parent
    therefore becomes ``root:65532`` with the sticky bit.  The runner keeps
    group read/write/execute access for Git's lock/index files, while sticky
    directory semantics prevent it from deleting or renaming the root-owned
    discovery pointers.  The descriptor path is opened without following any
    ancestor component, so a concurrent replacement cannot redirect chown or
    chmod to an attacker-selected directory.
    """

    if os.name != "posix":
        try:
            metadata = os.lstat(path)
            if _metadata_is_link_or_reparse(metadata) or not stat.S_ISDIR(
                metadata.st_mode
            ):
                raise WorkspaceOwnershipError("workspace pointer parent is not a directory")
            mode = stat.S_IMODE(metadata.st_mode)
            mode &= ~(stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
            mode |= (
                stat.S_IRUSR
                | stat.S_IWUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IWGRP
                | stat.S_IXGRP
                | stat.S_ISVTX
            )
            os.chown(str(path), 0, RUNNER_GID, follow_symlinks=False)
            os.chmod(str(path), mode)
        except OSError as exc:
            raise WorkspaceOwnershipError("workspace pointer parent protection failed") from exc
        return

    _assert_no_link_components(path)
    file_descriptor = _open_absolute_no_follow(path, directory=True)
    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceOwnershipError("workspace pointer parent is not a directory")
        mode = stat.S_IMODE(metadata.st_mode)
        mode &= ~(stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
        mode |= (
            stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR
            | stat.S_IRGRP
            | stat.S_IWGRP
            | stat.S_IXGRP
            | stat.S_ISVTX
        )
        try:
            os.fchown(file_descriptor, 0, RUNNER_GID)
            os.fchmod(file_descriptor, mode)
        except OSError as exc:
            raise WorkspaceOwnershipError(
                "workspace pointer parent protection failed"
            ) from exc
    finally:
        _close_fd(file_descriptor)


async def _read_process_output(
    process: _ProcessLike,
    max_output_bytes: int,
    deadline: float,
) -> tuple[str, str, bool]:
    """Read stdout/stderr concurrently under a combined UTF-8 byte budget."""

    streams = {
        "stdout": process.stdout,
        "stderr": process.stderr,
    }
    tasks: dict[asyncio.Task[bytes], str] = {}
    for name, stream in streams.items():
        if stream is not None:
            tasks[asyncio.create_task(stream.read(4096))] = name
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = False
    try:
        while tasks:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                truncated = True
                break
            done, _ = await asyncio.wait(
                tuple(tasks),
                timeout=remaining,
            )
            if not done:
                truncated = True
                break
            for task in done:
                name = tasks.pop(task)
                try:
                    chunk = task.result()
                except (asyncio.CancelledError, OSError):
                    chunk = b""
                if not chunk:
                    continue
                remaining_budget = max_output_bytes - sum(
                    len(item) for item in buffers.values()
                )
                if remaining_budget <= 0:
                    truncated = True
                    continue
                if len(chunk) > remaining_budget:
                    buffers[name].extend(chunk[:remaining_budget])
                    truncated = True
                    continue
                buffers[name].extend(chunk)
                stream = streams[name]
                tasks[asyncio.create_task(stream.read(4096))] = name
            if truncated and max_output_bytes <= sum(len(item) for item in buffers.values()):
                break
    finally:
        for task in tasks:
            task.cancel()
        for task in tuple(tasks):
            _detach_task(task)
        await _kill_process(process, max(deadline - asyncio.get_running_loop().time(), 0.1))
    return (
        bytes(buffers["stdout"]).decode("utf-8", errors="ignore"),
        bytes(buffers["stderr"]).decode("utf-8", errors="ignore"),
        truncated,
    )


async def _kill_process(process: _ProcessLike, timeout: float) -> None:
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=max(timeout, 0.01))
    except (TimeoutError, asyncio.CancelledError):
        pass


def _detach_task(task: asyncio.Task[Any]) -> None:
    if task.done():
        try:
            task.result()
        except BaseException:
            pass
        return

    def consume(done: asyncio.Task[Any]) -> None:
        try:
            done.result()
        except BaseException:
            pass

    task.add_done_callback(consume)


__all__ = [
    "CONTAINER_HOME",
    "CONTAINER_TMP",
    "CONTAINER_WORKSPACE",
    "DOCKER_CLI_ENV",
    "FIXED_ENVIRONMENT",
    "SERVICE_LABEL",
    "DockerRuntimeAdapter",
    "WorkspaceResolutionError",
    "WorkspaceResolver",
]
