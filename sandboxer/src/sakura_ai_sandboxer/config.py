"""Configuration owned by the independent sandbox daemon."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from . import PROTOCOL_VERSION, __version__
from .models import ErrorCode

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:/")
_UPDATER_ROOT = "/run/sakura-ai/"

DEFAULT_SOCKET_PATH = "/run/sakura-ai-sandbox/sandboxd.sock"
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_MAX_TIMEOUT_SECONDS = 3_600.0
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_CLEANUP_MARGIN_SECONDS = 5.0
DEFAULT_LEDGER_CAPACITY = 4096
DEFAULT_LEDGER_TTL_SECONDS = 3600.0
DEFAULT_RUNNER_IMAGE = "sakura-ai-agent-runner:dev"
DEFAULT_DOCKER_BINARY = "docker"
DEFAULT_PIDS_LIMIT = 256
DEFAULT_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_CPUS = 2.0
DEFAULT_NOFILE_SOFT = 1024
DEFAULT_NOFILE_HARD = 1024
DEFAULT_TMPFS_BYTES = 256 * 1024 * 1024
DEFAULT_HOME_TMPFS_BYTES = 128 * 1024 * 1024
DEFAULT_SOCKET_OWNER = 0
DEFAULT_SOCKET_GROUP = 9473
DEFAULT_SOCKET_MODE = 0o660
# The daemon owns the concrete Docker network.  ``bridge`` is the only
# default and is intentionally not exposed to Backend request payloads.
DEFAULT_EGRESS_NETWORK = "bridge"

# Production references carry a repository/name and digest.  A source
# checkout may instead use Docker's content-addressed local image ID after a
# local build.  Both forms are immutable; a tag is deliberately not accepted
# as a runner digest.
_IMAGE_DIGEST_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._/-]{0,254}@)?sha256:[0-9a-f]{64}$"
)
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_SAFE_BINARY_NAMES = frozenset({"docker", "podman"})
_INSTANCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
_NETWORK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def _versioned_envelope_bytes(payload: dict[str, object]) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


# These are the smallest valid versioned response envelopes.  The daemon must
# still be able to return a protocol-shaped error when a normal response is
# rejected by the byte budget, so the larger of the two is the hard floor.
MIN_VERSIONED_SUCCESS_ENVELOPE_BYTES = _versioned_envelope_bytes(
    {
        "protocol_version": PROTOCOL_VERSION,
        "sandboxd_version": __version__,
        "data": {},
    }
)
MIN_VERSIONED_ERROR_ENVELOPE_BYTES = max(
    _versioned_envelope_bytes(
        {
            "protocol_version": PROTOCOL_VERSION,
            "sandboxd_version": __version__,
            "error": error.value,
        }
    )
    for error in ErrorCode
)
MIN_VERSIONED_ENVELOPE_BYTES = max(
    MIN_VERSIONED_SUCCESS_ENVELOPE_BYTES,
    MIN_VERSIONED_ERROR_ENVELOPE_BYTES,
)


def _require_finite_number(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _require_int(value: object, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in the daemon policy range")


def canonicalize_socket_path(value: str) -> str:
    """Canonicalize a socket path lexically without following filesystem links.

    UDS paths are deployment-owned.  They must be absolute, contain no ``.``
    or ``..`` aliases, and never point into the updater IPC namespace.  The
    filesystem-level symlink/reparse check is performed by :mod:`server` just
    before binding.
    """

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
    compare = normalized.casefold()
    if compare == "/run/sakura-ai/updater.sock" or compare.startswith(_UPDATER_ROOT):
        raise ValueError("sandboxd must not use the updater IPC namespace")
    return normalized


def socket_parent(path: str) -> str:
    """Return the lexical parent of an already canonical absolute path."""

    index = path.rfind("/")
    if index <= 0:
        return path[:2] + "/" if _WINDOWS_DRIVE_RE.match(path) else "/"
    return path[:index]


def is_immutable_image_reference(value: object) -> bool:
    """Return whether *value* is a registry digest or local image ID.

    This helper is intentionally shared by the trusted runtime factory and
    the Docker adapter so a mutable tag can never reach ``docker create``.
    """

    return isinstance(value, str) and bool(_IMAGE_DIGEST_RE.fullmatch(value))


@dataclass(frozen=True, slots=True)
class SandboxdConfig:
    """Daemon policy knobs; execution requests cannot override these values."""

    socket_path: str = DEFAULT_SOCKET_PATH
    socket_root: str | None = None
    socket_owner: int = DEFAULT_SOCKET_OWNER
    socket_group: int = DEFAULT_SOCKET_GROUP
    socket_mode: int = DEFAULT_SOCKET_MODE
    state_dir: str | None = None
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_timeout_seconds: float = DEFAULT_MAX_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_response_bytes: int | None = None
    cleanup_margin_seconds: float = DEFAULT_CLEANUP_MARGIN_SECONDS
    request_ledger_capacity: int = DEFAULT_LEDGER_CAPACITY
    request_ledger_ttl_seconds: float = DEFAULT_LEDGER_TTL_SECONDS
    shutdown_timeout_seconds: float = 10.0
    runtime_name: str = "unavailable"
    runner_image_digest: str | None = None
    runner_image: str = DEFAULT_RUNNER_IMAGE
    docker_binary: str = DEFAULT_DOCKER_BINARY
    workspace_root: str | None = None
    instance_id: str | None = None
    oci_runtime: str | None = None
    # Agent/Dependency requests always use ``none`` unless the request
    # explicitly carries the constrained ``egress`` capability.  The daemon
    # then resolves that capability to this server-owned Docker network.
    # ``host``/container/namespace forms and arbitrary Docker arguments are
    # rejected.
    egress_network: str = DEFAULT_EGRESS_NETWORK
    pids_limit: int = DEFAULT_PIDS_LIMIT
    memory_bytes: int = DEFAULT_MEMORY_BYTES
    cpus: float = DEFAULT_CPUS
    nofile_soft: int = DEFAULT_NOFILE_SOFT
    nofile_hard: int = DEFAULT_NOFILE_HARD
    tmpfs_bytes: int = DEFAULT_TMPFS_BYTES
    home_tmpfs_bytes: int = DEFAULT_HOME_TMPFS_BYTES

    def __post_init__(self) -> None:
        path = canonicalize_socket_path(self.socket_path)
        root = canonicalize_socket_path(self.socket_root or socket_parent(path))
        if path == root or not path.startswith(root + "/"):
            raise ValueError("sandbox socket must stay inside its independent root")
        object.__setattr__(self, "socket_path", path)
        object.__setattr__(self, "socket_root", root)
        _require_int(self.socket_owner, "socket_owner", 0, 2_147_483_647)
        _require_int(self.socket_group, "socket_group", 1, 2_147_483_647)
        if self.socket_group == 9472:
            raise ValueError("socket_group must be independent from updater GID 9472")
        if type(self.socket_mode) is not int or self.socket_mode != 0o660:
            raise ValueError("socket_mode must be exactly 0660")
        if self.state_dir is not None:
            if not isinstance(self.state_dir, str) or not Path(self.state_dir).is_absolute():
                raise ValueError("state_dir must be an absolute path")
            if any(char in self.state_dir for char in ("\x00", "\n", "\r")):
                raise ValueError("state_dir contains an unsupported character")
            object.__setattr__(self, "state_dir", str(Path(self.state_dir)))
        _require_int(self.max_concurrent, "max_concurrent", 1, 256)
        _require_finite_number(self.timeout_seconds, "timeout_seconds")
        _require_finite_number(self.max_timeout_seconds, "max_timeout_seconds")
        if self.timeout_seconds <= 0 or self.timeout_seconds > self.max_timeout_seconds:
            raise ValueError("timeout_seconds is outside the daemon policy")
        if self.max_timeout_seconds <= 0 or self.max_timeout_seconds > 3_600:
            raise ValueError("max_timeout_seconds is outside the protocol limit")
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
        if response_limit < MIN_VERSIONED_ENVELOPE_BYTES:
            raise ValueError(
                "max_response_bytes is smaller than the minimum versioned envelope"
            )
        if response_limit < self.max_output_bytes or response_limit > 128 * 1024 * 1024:
            raise ValueError("max_response_bytes is outside the daemon policy")
        _require_finite_number(self.cleanup_margin_seconds, "cleanup_margin_seconds")
        if self.cleanup_margin_seconds <= 0 or self.cleanup_margin_seconds > 120:
            raise ValueError("cleanup_margin_seconds is outside the daemon policy")
        _require_int(
            self.request_ledger_capacity,
            "request_ledger_capacity",
            1,
            1_000_000,
        )
        _require_finite_number(
            self.request_ledger_ttl_seconds,
            "request_ledger_ttl_seconds",
        )
        if self.request_ledger_ttl_seconds <= 0 or self.request_ledger_ttl_seconds > 86_400:
            raise ValueError("request_ledger_ttl_seconds is outside the daemon policy")
        _require_finite_number(self.shutdown_timeout_seconds, "shutdown_timeout_seconds")
        if self.shutdown_timeout_seconds < 0 or self.shutdown_timeout_seconds > 120:
            raise ValueError("shutdown_timeout_seconds is outside the daemon policy")
        if not isinstance(self.runner_image, str) or not _IMAGE_TAG_RE.fullmatch(
            self.runner_image
        ):
            raise ValueError("runner_image is invalid")
        if self.runner_image_digest is not None and not is_immutable_image_reference(
            self.runner_image_digest
        ):
            raise ValueError("runner_image_digest must be an immutable sha256 reference")
        if (
            not isinstance(self.docker_binary, str)
            or Path(self.docker_binary).name != self.docker_binary
            or self.docker_binary not in _SAFE_BINARY_NAMES
        ):
            raise ValueError("docker_binary must be docker or podman")
        if self.workspace_root is not None:
            if not isinstance(self.workspace_root, str):
                raise ValueError("workspace_root must be an absolute path")
            root = Path(self.workspace_root)
            if not root.is_absolute() or "\x00" in self.workspace_root:
                raise ValueError("workspace_root must be an absolute path")
            if any(char in self.workspace_root for char in ("\n", "\r", ",")):
                raise ValueError("workspace_root contains an unsupported character")
            object.__setattr__(self, "workspace_root", str(root))
        if self.instance_id is not None and not _INSTANCE_ID_RE.fullmatch(
            self.instance_id
        ):
            raise ValueError("instance_id is invalid")
        if self.oci_runtime is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}", self.oci_runtime
        ):
            raise ValueError("oci_runtime is invalid")
        if not isinstance(self.egress_network, str):
            raise ValueError("egress_network must be a string")
        egress_network = self.egress_network.strip()
        if egress_network != "none" and (
            egress_network.casefold() in {"host", "bridge"}
            and egress_network != DEFAULT_EGRESS_NETWORK
            or egress_network.casefold().startswith(("container:", "ns:"))
            or not _NETWORK_NAME_RE.fullmatch(egress_network)
        ):
            raise ValueError(
                "egress_network must be bridge, none, or an explicitly administered network name"
            )
        object.__setattr__(self, "egress_network", egress_network)
        _require_int(self.pids_limit, "pids_limit", 1, 4096)
        _require_int(
            self.memory_bytes,
            "memory_bytes",
            64 * 1024 * 1024,
            64 * 1024 * 1024 * 1024,
        )
        _require_finite_number(self.cpus, "cpus")
        if self.cpus <= 0 or self.cpus > 64:
            raise ValueError("cpus is outside the daemon policy")
        _require_int(self.nofile_soft, "nofile_soft", 64, 1_000_000)
        _require_int(self.nofile_hard, "nofile_hard", self.nofile_soft, 1_000_000)
        _require_int(
            self.tmpfs_bytes,
            "tmpfs_bytes",
            16 * 1024 * 1024,
            8 * 1024 * 1024 * 1024,
        )
        _require_int(
            self.home_tmpfs_bytes,
            "home_tmpfs_bytes",
            16 * 1024 * 1024,
            8 * 1024 * 1024 * 1024,
        )


__all__ = [
    "DEFAULT_CLEANUP_MARGIN_SECONDS",
    "DEFAULT_EGRESS_NETWORK",
    "DEFAULT_LEDGER_CAPACITY",
    "DEFAULT_LEDGER_TTL_SECONDS",
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_TIMEOUT_SECONDS",
    "DEFAULT_RUNNER_IMAGE",
    "DEFAULT_SOCKET_GROUP",
    "DEFAULT_SOCKET_MODE",
    "DEFAULT_SOCKET_OWNER",
    "DEFAULT_SOCKET_PATH",
    "DEFAULT_TIMEOUT_SECONDS",
    "MIN_VERSIONED_ENVELOPE_BYTES",
    "MIN_VERSIONED_ERROR_ENVELOPE_BYTES",
    "MIN_VERSIONED_SUCCESS_ENVELOPE_BYTES",
    "SandboxdConfig",
    "canonicalize_socket_path",
    "is_immutable_image_reference",
    "socket_parent",
]
