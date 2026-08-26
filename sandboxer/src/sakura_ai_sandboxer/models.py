"""Strict wire models for the sandboxd v1 protocol."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from . import PROTOCOL_VERSION, __version__

WORKSPACE_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
REQUEST_ID_PATTERN = WORKSPACE_KEY_PATTERN
MAX_COMMAND_LENGTH = 32_768
MAX_ARG_LENGTH = 8_192
MAX_ARGV_COUNT = 256
MAX_CWD_LENGTH = 4_096
MAX_ENV_KEY_LENGTH = 128
MAX_ENV_VALUE_LENGTH = 8_192
MAX_TIMEOUT_SECONDS = 3_600.0


class ExecutionProfile(StrEnum):
    """Public execution profiles; Docker/runtime knobs are server-owned."""

    AGENT = "agent"
    DEPENDENCY = "dependency"


class ErrorCode(StrEnum):
    """Stable protocol error categories consumed by the Backend client."""

    INVALID_REQUEST = "INVALID_REQUEST"
    POLICY_DENIED = "POLICY_DENIED"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
    IMAGE_UNAVAILABLE = "IMAGE_UNAVAILABLE"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    REQUEST_CONFLICT = "REQUEST_CONFLICT"
    CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"
    DAEMON_SHUTTING_DOWN = "DAEMON_SHUTTING_DOWN"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class StrictModel(BaseModel):
    """Reject unknown fields at the protocol boundary."""

    # ``extra=forbid`` is the security-critical strictness here.  Pydantic's
    # global ``strict=True`` would reject the JSON string representation of
    # ``ExecutionProfile`` (and integer JSON values for a float timeout) before
    # our semantic validators can run.  Fields that must never coerce use the
    # explicit Strict* types below.
    model_config = ConfigDict(extra="forbid")


class ExecutionRequest(StrictModel):
    """A deliberately small request; all OCI parameters stay server-side."""

    request_id: StrictStr = Field(pattern=REQUEST_ID_PATTERN)
    workspace_key: StrictStr = Field(pattern=WORKSPACE_KEY_PATTERN)
    command: StrictStr | None = None
    argv: list[StrictStr] | None = None
    cwd: StrictStr = "."
    profile: ExecutionProfile
    timeout_seconds: StrictFloat = Field(gt=0, le=MAX_TIMEOUT_SECONDS)
    # An empty object is retained for forward protocol compatibility.  v1 does
    # not allow callers to pass even a single environment key.
    env: dict[StrictStr, StrictStr] = Field(default_factory=dict)

    @field_validator("request_id", "workspace_key", "command", "cwd")
    @classmethod
    def reject_nul_and_empty(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if "\x00" in value:
            raise ValueError("string contains NUL")
        if value == "" and cls.model_fields.get("command"):
            raise ValueError("string must not be empty")
        return value

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > MAX_COMMAND_LENGTH):
            raise ValueError("command is empty or exceeds the limit")
        return value

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value or len(value) > MAX_ARGV_COUNT:
            raise ValueError("argv is empty or exceeds the count limit")
        if any(not arg or len(arg) > MAX_ARG_LENGTH or "\x00" in arg for arg in value):
            raise ValueError("argv contains an invalid argument")
        return value

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        if value:
            raise ValueError("environment injection is not supported by protocol v1")
        for key, item in value.items():
            if (
                not key
                or len(key) > MAX_ENV_KEY_LENGTH
                or len(item) > MAX_ENV_VALUE_LENGTH
                or "\x00" in key
                or "\x00" in item
            ):
                raise ValueError("invalid environment field")
        return value

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str) -> str:
        if len(value) > MAX_CWD_LENGTH or "\\" in value:
            raise ValueError("cwd must be a relative POSIX path")
        parts = value.split("/")
        if value.startswith("/") or any(part == ".." for part in parts):
            raise ValueError("cwd must stay inside the workspace")
        if value == "" or any(part == "" for part in parts if value != "."):
            raise ValueError("cwd contains an empty path component")
        return value

    @model_validator(mode="after")
    def validate_command_form(self) -> ExecutionRequest:
        if (self.command is None) == (self.argv is None):
            raise ValueError("exactly one of command or argv is required")
        return self


class ExecutionData(StrictModel):
    """Result data returned inside the successful response envelope."""

    request_id: StrictStr
    exit_code: StrictInt | None = None
    stdout: StrictStr = ""
    stderr: StrictStr = ""
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False


class HealthData(StrictModel):
    """The exact non-sensitive daemon readiness contract.

    Keep this payload deliberately small.  In particular, no request counts,
    host paths other than the configured workspace root, or runtime details
    are allowed to grow the public health surface.  The Backend admission
    gate validates this strict shape before accepting an Agent execution.
    """

    ready: StrictBool
    runtime: StrictStr
    profiles: list[ExecutionProfile]
    instance_id: StrictStr
    workspace_root: StrictStr
    runner_image_digest: StrictStr


class CancelData(StrictModel):
    request_id: StrictStr
    cancelled: bool
    state: StrictStr


class ResponseEnvelope(StrictModel):
    """Successful response envelope, analogous in shape but independent."""

    protocol_version: StrictInt = PROTOCOL_VERSION
    sandboxd_version: StrictStr = __version__
    data: dict[str, Any]


class ErrorEnvelope(StrictModel):
    """Structured error envelope; no command or host-path echoing."""

    protocol_version: StrictInt = PROTOCOL_VERSION
    sandboxd_version: StrictStr = __version__
    error: ErrorCode
    detail: StrictStr | None = None


def validate_protocol_envelope(payload: object) -> dict[str, Any]:
    """Validate only the stable top-level shape used by Backend clients."""

    if not isinstance(payload, dict):
        raise ValueError("sandboxd response is not an object")
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("sandboxd protocol version is incompatible")
    if not isinstance(payload.get("sandboxd_version"), str):
        raise ValueError("sandboxd_version is missing")
    return payload


__all__ = [
    "CancelData",
    "ErrorCode",
    "ErrorEnvelope",
    "ExecutionData",
    "ExecutionProfile",
    "ExecutionRequest",
    "HealthData",
    "ResponseEnvelope",
    "validate_protocol_envelope",
]
