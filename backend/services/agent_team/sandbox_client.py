"""Backend client for the independent sandboxd HTTP-over-UDS protocol.

This module deliberately does not import the sandbox daemon package.  The Web
container only speaks the small wire contract and never gains access to a
container runtime, Docker argv, image, mount, network or runtime selector.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr

from backend.core.config import get_settings
from backend.services.agent_team.execution import (
    ExecutionError,
    ExecutionProfile,
    ExecutionRequest,
    ExecutionResult,
    execution_workspace_key,
)
from backend.services.agent_team.network_policy import (
    get_agent_team_network_policy_state,
    network_mode_for_policy,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

# Keep this wire constant independent from the sandboxer package so the Web
# container never imports host-side daemon code.  v2 requires the explicit
# network capability fields introduced by the Agent network policy contract.
PROTOCOL_VERSION = 2
DEFAULT_SOCKET_PATH = "/run/sakura-ai-sandbox/sandboxd.sock"
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_CLEANUP_MARGIN_SECONDS = 5.0
_FULL_IMAGE_DIGEST_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}@sha256:[0-9a-f]{64}$"
)
_LOCAL_CONTENT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:/")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
VALID_EXECUTION_BACKENDS = frozenset({"sandbox", "local"})
_ERROR_CODES = frozenset(
    {
        "INVALID_REQUEST",
        "POLICY_DENIED",
        "RUNTIME_UNAVAILABLE",
        "IMAGE_UNAVAILABLE",
        "EXECUTION_TIMEOUT",
        "OUTPUT_LIMIT",
        "CLEANUP_FAILED",
        "REQUEST_CONFLICT",
        "CONCURRENCY_LIMIT",
        "DAEMON_SHUTTING_DOWN",
        "INTERNAL_ERROR",
    }
)


def _require_int(value: object, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in the client policy range")


def _is_production_deploy_mode(deploy_mode: object) -> bool:
    return isinstance(deploy_mode, str) and deploy_mode.strip().lower() in {
        "image",
        "production",
    }


def _is_valid_digest(value: str, deploy_mode: object) -> bool:
    if _FULL_IMAGE_DIGEST_RE.fullmatch(value):
        return True
    return deploy_mode == "source" and _LOCAL_CONTENT_DIGEST_RE.fullmatch(value) is not None


def _canonicalize_socket_path(value: str) -> str:
    """Lexically canonicalize an absolute sandbox socket path."""

    raw = str(value).replace("\\", "/")
    if not raw or "\x00" in raw:
        raise ValueError("sandbox socket path is invalid")
    if not raw.startswith("/") and not _WINDOWS_DRIVE_RE.match(raw):
        raise ValueError("sandbox socket path must be absolute")
    if raw.startswith("//"):
        raise ValueError("UNC socket paths are not supported")
    drive = raw[:2] if _WINDOWS_DRIVE_RE.match(raw) else ""
    tail = raw[2:] if drive else raw
    components = tail.split("/")
    if not components or components[0] != "":
        raise ValueError("sandbox socket path must be absolute")
    if any(part in {"", ".", ".."} for part in components[1:]):
        raise ValueError("sandbox socket path contains an alias component")
    joined = "/".join(components[1:])
    normalized = f"{drive}/{joined}" if drive else f"/{joined}"
    if not normalized or normalized.endswith("/"):
        raise ValueError("sandbox socket path must name a socket")
    folded = normalized.casefold()
    if folded == "/run/sakura-ai/updater.sock" or folded.startswith(
        "/run/sakura-ai/"
    ):
        raise ValueError("sandbox client must use its independent socket")
    return normalized


def _socket_parent(path: str) -> str:
    """Return a canonical lexical parent for POSIX and drive-letter paths."""

    index = path.rfind("/")
    if _WINDOWS_DRIVE_RE.match(path) and index <= 2:
        return f"{path[:2]}/"
    if index <= 0:
        return "/"
    return path[:index]


def _is_link_or_reparse(path: Path) -> bool:
    """Inspect a path without following symlinks or Windows reparse points."""

    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _assert_no_link_components(path: Path) -> None:
    """Reject symlink/reparse aliases in every existing path component."""

    current = Path(path.anchor or path.root or ".")
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current /= part
        if _is_link_or_reparse(current):
            raise ValueError("sandbox socket path contains a symlink or reparse point")


def _validate_socket_filesystem(socket_path: str, socket_root: str) -> None:
    """Fail closed if the UDS path aliases outside its independent root.

    This check intentionally runs in the Backend process as well as in
    sandboxd.  It catches a replaced socket, a parent symlink/junction, and a
    real-path escape before httpx opens the UDS.  Missing final socket files
    are allowed because the daemon may not be started yet.
    """

    socket = Path(socket_path)
    root = Path(socket_root)
    try:
        socket.relative_to(root)
    except ValueError as exc:
        raise ValueError("sandbox socket is outside its independent root") from exc
    _assert_no_link_components(root)
    _assert_no_link_components(socket.parent)
    if _is_link_or_reparse(socket):
        raise ValueError("sandbox socket path contains a symlink or reparse point")
    if root.exists() and not root.is_dir():
        raise ValueError("sandbox socket root is not a directory")
    if socket.exists():
        metadata = os.lstat(socket)
        if not stat.S_ISSOCK(metadata.st_mode):
            raise ValueError("sandbox socket path exists but is not a socket")

    # ``realpath`` resolves the existing prefix while leaving a not-yet-created
    # final socket name in place.  Comparing both real paths prevents a
    # junction/symlink alias from escaping the configured root even if the
    # lexical strings themselves contain no ``..`` component.
    real_root = Path(os.path.realpath(root))
    real_socket = Path(os.path.realpath(socket))
    try:
        real_socket.relative_to(real_root)
    except ValueError as exc:
        raise ValueError("sandbox socket real path is outside its independent root") from exc


class SandboxClientError(ExecutionError):
    """Base class for fail-closed Backend-to-sandboxd failures."""


class SandboxUnavailableError(SandboxClientError):
    """The dedicated sandboxd socket or service cannot be reached."""


class SandboxCleanupError(SandboxClientError):
    """The daemon did not confirm cancellation within the cleanup budget."""


class SandboxProtocolError(SandboxClientError):
    """The daemon returned an incompatible or malformed protocol response."""


class SandboxPolicyError(SandboxClientError):
    """The daemon rejected an execution request by policy."""


class SandboxRemoteError(SandboxClientError):
    """The daemon returned a typed execution/runtime error."""

    def __init__(self, code: str, detail: str | None = None, status_code: int = 500):
        self.code = code
        self.detail = detail
        self.status_code = status_code
        super().__init__(f"sandboxd {code}: {detail or 'request failed'}")


@dataclass(frozen=True, slots=True)
class SandboxExecutionConfig:
    """Client-side limits; the daemon remains the policy authority."""

    socket_path: str = DEFAULT_SOCKET_PATH
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    request_timeout_seconds: float | None = None
    max_response_bytes: int | None = None
    cleanup_margin_seconds: float = DEFAULT_CLEANUP_MARGIN_SECONDS
    socket_root: str | None = None
    deploy_mode: str = "unknown"
    expected_runtime: str | None = None
    expected_instance_id: str | None = None
    expected_workspace_root: str | None = None
    expected_runner_image_digest: str | None = None

    def __post_init__(self) -> None:
        socket_path = _canonicalize_socket_path(self.socket_path)
        socket_root = _canonicalize_socket_path(
            self.socket_root or _socket_parent(socket_path)
        )
        if socket_path == socket_root or not socket_path.startswith(socket_root + "/"):
            raise ValueError("sandbox socket must stay inside its independent root")
        object.__setattr__(self, "socket_path", socket_path)
        object.__setattr__(self, "socket_root", socket_root)
        _validate_socket_filesystem(socket_path, socket_root)
        if not isinstance(self.deploy_mode, str):
            raise ValueError("deploy_mode must be a string")
        if not math.isfinite(self.timeout_seconds) or (
            self.timeout_seconds <= 0 or self.timeout_seconds > 3_600
        ):
            raise ValueError("sandbox timeout is outside the protocol limit")
        _require_int(
            self.max_output_bytes,
            "max_output_bytes",
            1,
            64 * 1024 * 1024,
        )
        response_limit = self.max_response_bytes
        if response_limit is None:
            response_limit = min(
                128 * 1024 * 1024,
                self.max_output_bytes * 8 + 65_536,
            )
            object.__setattr__(self, "max_response_bytes", response_limit)
        else:
            _require_int(
                response_limit,
                "max_response_bytes",
                1,
                128 * 1024 * 1024,
            )
        if response_limit < self.max_output_bytes or response_limit > 128 * 1024 * 1024:
            raise ValueError("sandbox response limit is outside the client policy")
        if not math.isfinite(self.cleanup_margin_seconds) or (
            self.cleanup_margin_seconds <= 0 or self.cleanup_margin_seconds > 120
        ):
            raise ValueError("sandbox cleanup margin is outside the client policy")
        if self.request_timeout_seconds is not None and (
            not math.isfinite(self.request_timeout_seconds)
            or self.request_timeout_seconds
            < self.timeout_seconds + self.cleanup_margin_seconds
        ):
            raise ValueError(
                "sandbox request timeout must cover execution timeout and cleanup margin"
            )
        for name in (
            "expected_runtime",
            "expected_instance_id",
            "expected_workspace_root",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip() or "\x00" in value
            ):
                raise ValueError(f"{name} must be a non-empty string when configured")
        if self.expected_runner_image_digest is not None and not _is_valid_digest(
            self.expected_runner_image_digest,
            self.deploy_mode,
        ):
            raise ValueError(
                "expected_runner_image_digest is invalid for the deploy mode"
            )

    @property
    def http_timeout(self) -> float:
        # A default timeout must cover the daemon's full execution window and
        # its bounded cancellation drain even when callers increase the
        # cleanup margin.  The additional 30 seconds leaves room for UDS and
        # JSON transport overhead without weakening the explicit validation
        # applied to ``request_timeout_seconds`` above.
        return self.request_timeout_seconds or self.timeout_seconds + max(
            30.0,
            self.cleanup_margin_seconds,
        )


class _ExecutionData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: StrictStr
    exit_code: StrictInt | None = None
    stdout: StrictStr = ""
    stderr: StrictStr = ""
    timed_out: StrictBool = False
    cancelled: StrictBool = False
    output_truncated: StrictBool = False


class _HealthData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ready: StrictBool
    runtime: StrictStr
    profiles: list[StrictStr]
    egress_capability: StrictStr
    instance_id: StrictStr
    workspace_root: StrictStr
    runner_image_digest: StrictStr | None


class _CancelData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: StrictStr
    cancelled: StrictBool
    state: StrictStr


def validate_execution_backend(backend: str, deploy_mode: str = "unknown") -> str:
    """Validate an explicit backend and keep local execution source-only."""

    normalized = str(backend).strip().lower()
    if normalized not in VALID_EXECUTION_BACKENDS:
        raise ValueError("agent_team_execution_backend must be sandbox or local")
    if normalized == "local" and deploy_mode != "source":
        raise ValueError("local Agent execution requires deploy_mode='source'")
    return normalized


def resolve_execution_backend(
    backend: str | None = None,
    *,
    deploy_mode: str = "unknown",
) -> str:
    """Resolve only an explicit backend selection.

    There is deliberately no source/image implicit default here.  A missing
    setting is an admission error, because inferring ``local`` would make a
    production worker silently execute on the Web host.
    """

    if backend is None or not isinstance(backend, str) or not backend.strip():
        raise ValueError(
            "agent_team_execution_backend must be explicitly configured"
        )
    return validate_execution_backend(backend, deploy_mode)


def create_execution_runner(
    workspace: str,
    workspace_service: AgentTeamWorkspaceService,
    *,
    backend: str | None,
    deploy_mode: str,
    socket_path: str | None = None,
    socket_root: str | None = None,
    timeout_seconds: float | None = None,
    max_output_bytes: int | None = None,
    expected_runtime: str | None = None,
    expected_instance_id: str | None = None,
    expected_workspace_root: str | None = None,
    expected_digest: str | None = None,
) -> Any:
    """Create one workspace-scoped runner from an explicit deployment choice.

    The factory is intentionally synchronous: readiness is checked by
    ``create_ready_execution_runner`` before the worker admits an Agent run.
    No caller-provided Docker/runtime/mount arguments are accepted.
    """

    selected = resolve_execution_backend(backend, deploy_mode=deploy_mode)
    if selected == "local":
        # Local is an explicit source-development escape hatch only.
        from backend.services.agent_team.execution import LocalExecutionRunner

        return LocalExecutionRunner(workspace, workspace_service)
    return SandboxExecutionRunner(
        workspace,
        workspace_service,
        deploy_mode=deploy_mode,
        socket_path=socket_path,
        socket_root=socket_root,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        expected_runtime=expected_runtime,
        expected_instance_id=expected_instance_id,
        expected_workspace_root=expected_workspace_root,
        expected_runner_image_digest=expected_digest,
    )


async def create_ready_execution_runner(
    workspace: str,
    workspace_service: AgentTeamWorkspaceService,
    *,
    backend: str | None,
    deploy_mode: str,
    expected_runtime: str | None = None,
    expected_instance_id: str | None = None,
    expected_workspace_root: str | None = None,
    expected_digest: str | None = None,
) -> Any:
    """Create a runner and complete the production admission gate."""

    runner = create_execution_runner(
        workspace,
        workspace_service,
        backend=backend,
        deploy_mode=deploy_mode,
        expected_runtime=expected_runtime,
        expected_instance_id=expected_instance_id,
        expected_workspace_root=expected_workspace_root,
        expected_digest=expected_digest,
    )
    if isinstance(runner, SandboxExecutionRunner):
        await runner.ensure_ready(
            expected_runtime=expected_runtime,
            expected_instance_id=expected_instance_id,
            expected_workspace_root=expected_workspace_root,
            expected_digest=expected_digest,
            require_digest=_is_production_deploy_mode(deploy_mode),
        )
    return runner


class SandboxExecutionRunner:
    """``ExecutionRunner`` implementation backed only by sandboxd UDS."""

    def __init__(
        self,
        workspace: str,
        workspace_service: AgentTeamWorkspaceService | None = None,
        *,
        socket_path: str | None = None,
        socket_root: str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int | None = None,
        request_timeout_seconds: float | None = None,
        max_response_bytes: int | None = None,
        cleanup_margin_seconds: float = DEFAULT_CLEANUP_MARGIN_SECONDS,
        deploy_mode: str | None = None,
        expected_runtime: str | None = None,
        expected_instance_id: str | None = None,
        expected_workspace_root: str | None = None,
        expected_runner_image_digest: str | None = None,
    ) -> None:
        settings = get_settings()
        self.workspace_service = workspace_service or AgentTeamWorkspaceService()
        self.workspace = self.workspace_service.resolve_inside_workspace(workspace)
        self.workspace_key = execution_workspace_key(self.workspace, self.workspace_service)
        configured_socket = getattr(
            settings,
            "agent_team_sandbox_socket",
            DEFAULT_SOCKET_PATH,
        )
        configured_timeout = float(
            getattr(
                settings,
                "agent_team_sandbox_timeout_seconds",
                DEFAULT_TIMEOUT_SECONDS,
            )
        )
        configured_output = int(
            getattr(
                settings,
                "agent_team_sandbox_max_output_bytes",
                DEFAULT_MAX_OUTPUT_BYTES,
            )
        )
        configured_deploy_mode = getattr(settings, "sakura_deploy_mode", "unknown")
        configured_runtime = getattr(settings, "agent_team_sandbox_runtime", None)
        configured_instance_id = getattr(
            settings,
            "agent_team_sandbox_expected_instance_id",
            None,
        )
        configured_workspace_root = getattr(
            settings,
            "agent_team_sandbox_expected_workspace_root",
            None,
        )
        configured_digest = getattr(
            settings,
            "agent_team_sandbox_runner_image_digest",
            None,
        )
        self.config = SandboxExecutionConfig(
            socket_path=socket_path if socket_path is not None else configured_socket,
            socket_root=socket_root,
            timeout_seconds=(
                timeout_seconds if timeout_seconds is not None else configured_timeout
            ),
            max_output_bytes=(
                max_output_bytes if max_output_bytes is not None else configured_output
            ),
            request_timeout_seconds=request_timeout_seconds,
            max_response_bytes=max_response_bytes,
            cleanup_margin_seconds=cleanup_margin_seconds,
            deploy_mode=(
                deploy_mode if deploy_mode is not None else configured_deploy_mode
            ),
            expected_runtime=(
                expected_runtime
                if expected_runtime is not None
                else configured_runtime
            ),
            expected_instance_id=(
                expected_instance_id
                if expected_instance_id is not None
                else configured_instance_id
            ),
            expected_workspace_root=(
                expected_workspace_root
                if expected_workspace_root is not None
                else configured_workspace_root
            ),
            expected_runner_image_digest=(
                expected_runner_image_digest
                if expected_runner_image_digest is not None
                else configured_digest
            ),
        )
        # Filled only by ``ensure_ready`` after the daemon's strict health
        # identity/capability contract has been validated.  Dependency
        # installation must never infer a network mode from a request or a
        # local default.
        self._egress_capability: str | None = None

    def supports_profile(self, profile: ExecutionProfile) -> bool:
        return profile in {ExecutionProfile.AGENT, ExecutionProfile.DEPENDENCY}

    @property
    def egress_capability(self) -> str | None:
        """The server-advertised egress capability, never a network name."""

        return self._egress_capability

    async def ensure_ready(
        self,
        *,
        expected_runtime: str | None = None,
        expected_instance_id: str | None = None,
        expected_workspace_root: str | None = None,
        expected_digest: str | None = None,
        require_digest: bool = False,
    ) -> _HealthData:
        """Run the Agent admission gate against the independent daemon.

        ``_request`` validates the protocol version and strict response
        envelope.  This method adds the daemon readiness and identity checks
        that cannot be inferred from a successful HTTP response.
        """

        health = await self.health()
        if not health.ready:
            raise SandboxUnavailableError("sandboxd health check is not ready")
        runtime = health.runtime.strip().lower()
        if not runtime or runtime in {"unknown", "unavailable", "none"}:
            raise SandboxUnavailableError("sandboxd runtime is unavailable")
        expected_runtime = (
            expected_runtime
            if expected_runtime is not None
            else self.config.expected_runtime
        )
        expected_instance_id = (
            expected_instance_id
            if expected_instance_id is not None
            else self.config.expected_instance_id
        )
        expected_workspace_root = (
            expected_workspace_root
            if expected_workspace_root is not None
            else self.config.expected_workspace_root
        )
        expected_digest = (
            expected_digest
            if expected_digest is not None
            else self.config.expected_runner_image_digest
        )
        if require_digest and any(
            not isinstance(value, str) or not value.strip()
            for value in (
                expected_runtime,
                expected_instance_id,
                expected_workspace_root,
                expected_digest,
            )
        ):
            raise SandboxProtocolError(
                "image sandbox admission requires all expected identities"
            )
        if expected_runtime is not None:
            configured_runtime = expected_runtime.strip().lower()
            if not configured_runtime or runtime != configured_runtime:
                raise SandboxProtocolError("sandboxd runtime identity does not match")
        profiles = {str(profile).lower() for profile in health.profiles}
        if not {ExecutionProfile.AGENT.value, ExecutionProfile.DEPENDENCY.value}.issubset(
            profiles
        ):
            raise SandboxProtocolError("sandboxd does not advertise required profiles")
        instance_id = health.instance_id.strip()
        if not instance_id:
            raise SandboxProtocolError("sandboxd instance identity is missing")
        if expected_instance_id is not None and instance_id != expected_instance_id.strip():
            raise SandboxProtocolError("sandboxd instance identity does not match")
        try:
            workspace_root = _canonical_workspace_root(health.workspace_root)
        except ValueError as exc:
            raise SandboxProtocolError("sandboxd workspace identity is invalid") from exc
        if expected_workspace_root is not None:
            try:
                configured_workspace_root = _canonical_workspace_root(
                    expected_workspace_root
                )
            except ValueError as exc:
                raise SandboxProtocolError(
                    "configured sandbox workspace identity is invalid"
                ) from exc
            if workspace_root != configured_workspace_root:
                raise SandboxProtocolError("sandboxd workspace identity does not match")
        digest = health.runner_image_digest
        if require_digest and not digest:
            raise SandboxProtocolError("sandboxd runner image digest is missing")
        if digest and not _is_valid_digest(digest, self.config.deploy_mode):
            raise SandboxProtocolError(
                "sandboxd runner image digest is invalid for the deploy mode"
            )
        if expected_digest is not None and not _is_valid_digest(
            expected_digest,
            self.config.deploy_mode,
        ):
            raise SandboxProtocolError(
                "expected sandbox runner image digest is invalid for the deploy mode"
            )
        if expected_digest is not None and digest != expected_digest:
            raise SandboxProtocolError("sandboxd runner image digest does not match")
        egress_capability = health.egress_capability.strip().lower()
        if egress_capability not in {"none", "egress"}:
            raise SandboxProtocolError("sandboxd egress capability is invalid")
        self._egress_capability = egress_capability
        return health

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        request_id = uuid.uuid4().hex
        audit_digest = self.config.expected_runner_image_digest or "unbound"

        if not self.supports_profile(request.profile):
            _audit_execution(
                task=request.workspace_key,
                request_id=request_id,
                profile=request.profile.value,
                policy="unavailable",
                mode="none",
                revision="unknown",
                digest=audit_digest,
                result="denied_unsupported_profile",
            )
            raise SandboxPolicyError(
                f"sandbox runner does not support profile {request.profile.value}"
            )
        if request.workspace_key != self.workspace_key:
            _audit_execution(
                task=request.workspace_key,
                request_id=request_id,
                profile=request.profile.value,
                policy="unavailable",
                mode="none",
                revision="unknown",
                digest=audit_digest,
                result="denied_workspace_mismatch",
            )
            raise SandboxPolicyError("request workspace does not match the runner workspace")
        if request.env:
            _audit_execution(
                task=request.workspace_key,
                request_id=request_id,
                profile=request.profile.value,
                policy="unavailable",
                mode="none",
                revision="unknown",
                digest=audit_digest,
                result="denied_environment",
            )
            raise SandboxPolicyError("sandbox requests cannot inject environment variables")

        try:
            policy_state = await get_agent_team_network_policy_state()
            network_policy = policy_state.policy
            network_mode = network_mode_for_policy(network_policy)
        except Exception as exc:
            # Do not include the exception text: database/driver errors can
            # echo connection details, and the audit contract must never
            # become a command/secret side channel.
            logger.bind(error_type=type(exc).__name__).error(
                "Agent sandbox network policy could not be read; execution denied"
            )
            _audit_execution(
                task=request.workspace_key,
                request_id=request_id,
                profile=request.profile.value,
                policy="unavailable",
                mode="none",
                revision="unknown",
                digest=audit_digest,
                result="denied_policy_unavailable",
            )
            raise SandboxPolicyError(
                "Agent network policy could not be read; execution was denied"
            ) from exc

        _audit_execution(
            task=request.workspace_key,
            request_id=request_id,
            profile=request.profile.value,
            policy=network_policy.value,
            mode=network_mode,
            revision=policy_state.revision,
            digest=audit_digest,
            result="admitted",
        )

        timeout = min(float(request.timeout_seconds), self.config.timeout_seconds)
        payload: dict[str, Any] = {
            "request_id": request_id,
            "workspace_key": request.workspace_key,
            "cwd": str(request.cwd),
            "profile": request.profile.value,
            "timeout_seconds": timeout,
            "env": {},
            "network_mode": network_mode,
        }
        if request.command is not None:
            payload["command"] = request.command
        else:
            payload["argv"] = list(request.argv or ())

        # ``_request`` issues the bounded best-effort cancel when transport or
        # task cancellation interrupts this POST.  There is intentionally no
        # local subprocess fallback here.
        request_task = asyncio.create_task(
            self._request(
                "POST",
                "/v1/executions",
                payload,
                request_id=request_id,
            ),
            name=f"sandbox-client-execution-{request_id}",
        )
        cancel_waiter: asyncio.Task[bool] | None = None
        try:
            try:
                if request.cancel_event is None:
                    envelope = await request_task
                else:
                    cancel_waiter = asyncio.create_task(
                        request.cancel_event.wait(),
                        name=f"sandbox-client-cancel-wait-{request_id}",
                    )
                    done, _ = await asyncio.wait(
                        {request_task, cancel_waiter},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_waiter in done and request_task not in done:
                        # The daemon owns the runtime process.  Always send the
                        # exact request id and require its explicit acknowledgement;
                        # a timeout or transport failure is infrastructure failure,
                        # never a successful cancellation result.
                        try:
                            await self._cancel_request_bounded(request_id)
                        except asyncio.CancelledError:
                            # The independent delivery task remains shielded and
                            # owns its own deadline; preserve the caller's cancel.
                            raise
                        except SandboxCleanupError:
                            request_task.cancel()
                            _detach_task(request_task)
                            raise
                        await self._drain_request_after_cancel(request_task)
                        _audit_execution(
                            task=request.workspace_key,
                            request_id=request_id,
                            profile=request.profile.value,
                            policy=network_policy.value,
                            mode=network_mode,
                            revision=policy_state.revision,
                            digest=audit_digest,
                            result="cancelled",
                        )
                        return ExecutionResult(
                            command=request.command or " ".join(request.argv or ()),
                            cwd=str(request.cwd),
                            cancelled=True,
                        )
                    else:
                        envelope = await request_task
            except asyncio.CancelledError:
                if not request_task.done():
                    request_task.cancel()
                _detach_task(request_task)
                raise
            finally:
                if cancel_waiter is not None and not cancel_waiter.done():
                    cancel_waiter.cancel()
                    _detach_task(cancel_waiter)

            data = self._parse_data(envelope, _ExecutionData)
            if data.request_id != request_id:
                raise SandboxProtocolError("sandboxd returned a mismatched request id")
            stdout, stderr, truncated = _bound_output(
                data.stdout,
                data.stderr,
                self.config.max_output_bytes,
            )
            result = ExecutionResult(
                command=request.command or " ".join(request.argv or ()),
                cwd=str(request.cwd),
                exit_code=data.exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=data.timed_out,
                cancelled=data.cancelled,
                output_truncated=data.output_truncated or truncated,
            )
            _audit_execution(
                task=request.workspace_key,
                request_id=request_id,
                profile=request.profile.value,
                policy=network_policy.value,
                mode=network_mode,
                revision=policy_state.revision,
                digest=audit_digest,
                result=(
                    "completed_timeout"
                    if result.timed_out
                    else "completed_nonzero"
                    if result.exit_code not in (None, 0)
                    else "completed"
                ),
            )
            return result
        except BaseException as exc:
            _audit_execution(
                task=request.workspace_key,
                request_id=request_id,
                profile=request.profile.value,
                policy=network_policy.value,
                mode=network_mode,
                revision=policy_state.revision,
                digest=audit_digest,
                result=f"error_{type(exc).__name__}",
            )
            raise

    async def cancel(self, request_id: str) -> _CancelData:
        if not request_id or len(request_id) > 128 or "/" in request_id or "\\" in request_id:
            raise SandboxProtocolError("invalid sandbox request id")
        envelope = await self._request(
            "POST",
            f"/v1/executions/{request_id}/cancel",
        )
        return self._parse_data(envelope, _CancelData)

    async def health(self) -> _HealthData:
        envelope = await self._request("GET", "/v1/health")
        return self._parse_data(envelope, _HealthData)

    async def _cancel_request_bounded(self, request_id: str) -> _CancelData:
        """Deliver and confirm cancellation under an independent hard budget."""

        async def deliver() -> _CancelData:
            cancel_task = asyncio.create_task(
                self.cancel(request_id),
                name=f"sandbox-client-cancel-post-{request_id}",
            )
            try:
                done, _ = await asyncio.wait(
                    {cancel_task},
                    timeout=self.config.cleanup_margin_seconds,
                )
                if not done:
                    cancel_task.cancel()
                    _detach_task(cancel_task)
                    raise SandboxCleanupError(
                        "sandbox cancellation delivery exceeded cleanup margin"
                    )
                try:
                    result = cancel_task.result()
                except BaseException as exc:
                    raise SandboxCleanupError(
                        f"sandbox cancellation delivery failed: {exc}"
                    ) from exc
                if result.request_id != request_id:
                    raise SandboxCleanupError(
                        "sandbox cancellation acknowledgement request id mismatch"
                    )
                if not result.cancelled or result.state != "cancelled":
                    raise SandboxCleanupError(
                        "sandbox daemon did not acknowledge cancellation"
                    )
                return result
            except asyncio.CancelledError:
                cancel_task.cancel()
                _detach_task(cancel_task)
                raise

        delivery_task = asyncio.create_task(
            deliver(),
            name=f"sandbox-client-cancel-delivery-{request_id}",
        )
        try:
            return await asyncio.shield(delivery_task)
        except asyncio.CancelledError:
            # Shielding prevents outer task cancellation from interrupting the
            # POST.  The detached delivery task still reaches its hard margin
            # and consumes any delivery failure without leaking a task.
            _detach_task(delivery_task)
            raise

    async def _drain_request_after_cancel(
        self,
        request_task: asyncio.Task[dict[str, Any]],
    ) -> None:
        """Bound the original execution POST after daemon cancel acknowledgement."""

        async def drain() -> None:
            done, _ = await asyncio.wait(
                {request_task},
                timeout=self.config.cleanup_margin_seconds,
            )
            if not done:
                request_task.cancel()
                _detach_task(request_task)
                return
            try:
                request_task.result()
            except BaseException:
                # The cancel acknowledgement is authoritative; the original
                # execution response is intentionally not exposed to callers.
                pass

        drain_task = asyncio.create_task(
            drain(),
            name=f"sandbox-client-execution-drain-{request_task.get_name()}",
        )
        try:
            await asyncio.shield(drain_task)
        except asyncio.CancelledError:
            _detach_task(drain_task)
            raise

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        _validate_socket_filesystem(
            self.config.socket_path,
            self.config.socket_root or _socket_parent(self.config.socket_path),
        )
        transport = httpx.AsyncHTTPTransport(uds=self.config.socket_path)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://sandboxd",
                timeout=self.config.http_timeout,
            ) as client:
                response = await client.request(method, path, json=json_body)
        except asyncio.CancelledError:
            if request_id:
                await self._best_effort_cancel(request_id)
            raise
        except (TimeoutError, httpx.HTTPError, OSError, AttributeError) as exc:
            if request_id:
                await self._best_effort_cancel(request_id)
            raise SandboxUnavailableError(f"sandboxd UDS unavailable: {exc}") from exc

        if len(response.content) > (self.config.max_response_bytes or 0):
            raise SandboxProtocolError("sandboxd response exceeds the byte limit")

        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise SandboxProtocolError("sandboxd returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise SandboxProtocolError("sandboxd returned a non-object response")
        success_keys = {"protocol_version", "sandboxd_version", "data"}
        error_keys = {"protocol_version", "sandboxd_version", "error", "detail"}
        if response.status_code >= 200 and response.status_code < 300:
            if set(payload).difference(success_keys):
                raise SandboxProtocolError("sandboxd response contains unknown fields")
        elif set(payload).difference(error_keys):
            raise SandboxProtocolError("sandboxd error response contains unknown fields")
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise SandboxProtocolError("sandboxd protocol version is incompatible")
        if not isinstance(payload.get("sandboxd_version"), str):
            raise SandboxProtocolError("sandboxd version is missing")
        if response.status_code < 200 or response.status_code >= 300:
            code = payload.get("error")
            if not isinstance(code, str) or code not in _ERROR_CODES:
                raise SandboxProtocolError("sandboxd error response is malformed")
            detail = payload.get("detail")
            if detail is not None and not isinstance(detail, str):
                raise SandboxProtocolError("sandboxd error detail is malformed")
            if code in {"POLICY_DENIED", "INVALID_REQUEST"}:
                raise SandboxPolicyError(f"sandboxd {code}: {detail or 'request rejected'}")
            if code in {"RUNTIME_UNAVAILABLE", "DAEMON_SHUTTING_DOWN"}:
                raise SandboxUnavailableError(
                    f"sandboxd {code}: {detail or 'service unavailable'}"
                )
            raise SandboxRemoteError(code, detail, response.status_code)
        return payload

    async def _best_effort_cancel(self, request_id: str) -> None:
        """Preserve an original transport/task error while cleaning up boundedly."""

        try:
            await self._cancel_request_bounded(request_id)
        except BaseException:
            # The original transport or caller cancellation remains
            # authoritative.  The strict path used for an explicit cancel
            # event raises ``SandboxCleanupError`` instead of calling this
            # compatibility cleanup helper.
            pass

    @staticmethod
    def _parse_data(payload: dict[str, Any], model: type[BaseModel]) -> Any:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SandboxProtocolError("sandboxd response data is malformed")
        try:
            return model.model_validate(data)
        except Exception as exc:
            raise SandboxProtocolError("sandboxd response data violates the contract") from exc


async def read_sandbox_capability_status() -> dict[str, Any]:
    """Read a UI-safe sandbox readiness projection without a workspace.

    The configuration page needs to show whether the independent daemon is
    reachable, but it must not create a workspace or expose Docker network
    names.  Health is a control-plane request and therefore uses a lightweight
    uninitialised runner whose only required field is the immutable client
    configuration.
    """

    settings = get_settings()
    try:
        config = SandboxExecutionConfig(
            socket_path=getattr(settings, "agent_team_sandbox_socket", DEFAULT_SOCKET_PATH),
            timeout_seconds=min(
                float(
                    getattr(
                        settings,
                        "agent_team_sandbox_timeout_seconds",
                        DEFAULT_TIMEOUT_SECONDS,
                    )
                ),
                30.0,
            ),
            max_output_bytes=int(
                getattr(
                    settings,
                    "agent_team_sandbox_max_output_bytes",
                    DEFAULT_MAX_OUTPUT_BYTES,
                )
            ),
            deploy_mode=getattr(settings, "sakura_deploy_mode", "unknown"),
        )
        probe = SandboxExecutionRunner.__new__(SandboxExecutionRunner)
        probe.config = config
        health = await probe.health()
        runtime = health.runtime.strip().lower()
        capability = health.egress_capability.strip().lower()
        if capability not in {"none", "egress"}:
            raise SandboxProtocolError("sandboxd egress capability is invalid")
        available = bool(health.ready and runtime not in {"", "unknown", "none", "unavailable"})
        return {
            "available": available,
            "egress_capability": capability,
        }
    except Exception as exc:
        logger.bind(error_type=type(exc).__name__).warning(
            "sandbox capability status is unavailable"
        )
        return {
            "available": False,
            "egress_capability": "unavailable",
        }


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


def _audit_execution(
    *,
    task: str,
    request_id: str,
    profile: str,
    policy: str,
    mode: str,
    revision: str,
    digest: str,
    result: str,
) -> None:
    """Emit the bounded execution audit projection.

    This intentionally uses structured logger context and a constant message.
    In particular, command/argv/cwd/env and exception details are not included
    so an audit sink cannot accidentally persist execution secrets.
    """

    logger.bind(
        event="agent_sandbox_execution",
        task=str(task),
        request=str(request_id),
        profile=str(profile),
        policy=str(policy),
        mode=str(mode),
        revision=str(revision),
        digest=str(digest),
        result=str(result),
    ).info("agent sandbox execution")


def _canonical_workspace_root(value: str) -> str:
    """Canonicalize the daemon's deployment-owned workspace identity."""

    raw = str(value).replace("\\", "/")
    if not raw or "\x00" in raw:
        raise ValueError("workspace root is invalid")
    if not raw.startswith("/") and not _WINDOWS_DRIVE_RE.match(raw):
        raise ValueError("workspace root must be absolute")
    if raw.startswith("//"):
        raise ValueError("UNC workspace roots are not supported")
    if len(raw) > 1 and raw.endswith("/") and not (
        _WINDOWS_DRIVE_RE.match(raw) and len(raw) == 3
    ):
        raw = raw.rstrip("/")
    drive = raw[:2] if _WINDOWS_DRIVE_RE.match(raw) else ""
    tail = raw[2:] if drive else raw
    components = tail.split("/")
    if not components or components[0] != "":
        raise ValueError("workspace root must be absolute")
    if any(part in {"", ".", ".."} for part in components[1:]):
        raise ValueError("workspace root contains an alias component")
    joined = "/".join(components[1:])
    normalized = f"{drive}/{joined}" if drive else f"/{joined}"
    if normalized.endswith("/") and normalized != "/":
        normalized = normalized.rstrip("/")
    return normalized


def _bound_output(
    stdout: str,
    stderr: str,
    max_bytes: int,
) -> tuple[str, str, bool]:
    """Truncate response data by combined UTF-8 bytes, not characters."""

    stdout_bytes = stdout.encode("utf-8", errors="replace")
    stderr_bytes = stderr.encode("utf-8", errors="replace")
    if len(stdout_bytes) + len(stderr_bytes) <= max_bytes:
        return stdout, stderr, False
    bounded_stdout = stdout_bytes[:max_bytes].decode("utf-8", errors="ignore")
    remaining = max_bytes - len(bounded_stdout.encode("utf-8"))
    bounded_stderr = stderr_bytes[: max(remaining, 0)].decode("utf-8", errors="ignore")
    return bounded_stdout, bounded_stderr, True


__all__ = [
    "SandboxCleanupError",
    "SandboxClientError",
    "SandboxExecutionConfig",
    "SandboxExecutionRunner",
    "SandboxPolicyError",
    "SandboxProtocolError",
    "SandboxRemoteError",
    "SandboxUnavailableError",
    "create_execution_runner",
    "create_ready_execution_runner",
    "read_sandbox_capability_status",
    "resolve_execution_backend",
    "validate_execution_backend",
]
