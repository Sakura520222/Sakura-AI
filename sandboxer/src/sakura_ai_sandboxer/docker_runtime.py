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
from .models import REQUEST_ID_PATTERN, WORKSPACE_KEY_PATTERN, ExecutionRequest
from .runtime import RuntimeAdapter, RuntimeResult

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
FIXED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
FIXED_ENVIRONMENT = (
    f"HOME={CONTAINER_HOME}",
    f"PATH={FIXED_PATH}",
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
                "none",
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
                f"type=bind,src={source},dst={CONTAINER_WORKSPACE},rw,bind-propagation=rprivate",
                "--workdir",
                f"{CONTAINER_WORKSPACE}/{workdir}" if workdir != "." else CONTAINER_WORKSPACE,
                "--entrypoint",
                "",
            )
        )
        for item in FIXED_ENVIRONMENT:
            argv.extend(("--env", item))
        if self.config.oci_runtime:
            argv.extend(("--runtime", self.config.oci_runtime))
        argv.append(self.image_reference)
        argv.extend(command)
        return tuple(argv)

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
            container_name = self._container_name(request.request_id)
            create_argv = self.build_create_argv(
                request,
                workspace=workspace,
                container_name=container_name,
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
                )
            except CleanupFailedError as cleanup_exc:
                await self._release_workspace_lease(request.request_id, snapshot)
                raise cleanup_exc from exc
            except BaseException:
                await self._release_workspace_lease(request.request_id, snapshot)
                raise
            await self._release_workspace_lease(request.request_id, snapshot)
            raise
        if create_result.returncode != 0:
            try:
                await self._cleanup_owned_request_bounded(
                    request.request_id,
                    request.workspace_key,
                    self._cleanup_deadline_from_now(),
                )
            finally:
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
                )
            finally:
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
        identifiers = {
            line.strip()
            for line in result.stdout.decode("ascii", errors="ignore").splitlines()
            if _CONTAINER_ID_RE.fullmatch(line.strip())
        }
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
                continue
            try:
                labels = json.loads(inspect.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(labels, dict) or not self._owns_labels(labels):
                continue
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
        identifiers = {
            line.strip()
            for line in result.stdout.decode("ascii", errors="ignore").splitlines()
            if _CONTAINER_ID_RE.fullmatch(line.strip())
        }
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
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CleanupFailedError() from exc
            if (
                isinstance(labels, dict)
                and self._owns_labels(labels)
                and labels.get(REQUEST_LABEL_KEY) == request_id
                and labels.get(WORKSPACE_LABEL_KEY) == workspace_key
            ):
                await self._cleanup_state(
                    _ContainerState("recovery", "", container_id),
                    deadline,
                )

    async def _cleanup_owned_request_bounded(
        self,
        request_id: str,
        workspace_key: str,
        deadline: float,
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
            _detach_task(task)
            raise CleanupFailedError()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except asyncio.CancelledError:
            _detach_task(task)
            raise
        except TimeoutError as exc:
            _detach_task(task)
            raise CleanupFailedError() from exc

    def _resolve_workspace(self, request: ExecutionRequest) -> WorkspaceSnapshot:
        try:
            return self.workspace_resolver.resolve_snapshot(request.workspace_key)
        except WorkspaceResolutionError as exc:
            raise InvalidRequestError("workspace key cannot be resolved") from exc

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
            lease.request_ids.add(request_id)

    async def _release_workspace_lease(
        self,
        request_id: str,
        snapshot: WorkspaceSnapshot,
    ) -> None:
        async with self._lock:
            lease = self._workspace_leases.get(snapshot.relative_identity)
            if lease is None:
                return
            lease.request_ids.discard(request_id)
            if not lease.request_ids:
                self._workspace_leases.pop(snapshot.relative_identity, None)

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
                "--stdout",
                "--stderr",
                container_id,
            )
        )
        stdout, stderr, truncated = await _read_process_output(
            process,
            max_output_bytes,
            deadline,
        )
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
    identifier = value.decode("ascii", errors="ignore").strip()
    if not _CONTAINER_ID_RE.fullmatch(identifier):
        raise RuntimeUnavailableError()
    return identifier


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
