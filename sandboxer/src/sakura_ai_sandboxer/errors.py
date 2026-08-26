"""Typed daemon errors and HTTP status mapping."""

from __future__ import annotations

from dataclasses import dataclass

from .models import ErrorCode


@dataclass(slots=True)
class SandboxError(Exception):
    """An expected, structured sandboxd failure."""

    code: ErrorCode
    detail: str | None = None
    status_code: int = 500

    def __str__(self) -> str:
        return self.detail or self.code.value


class InvalidRequestError(SandboxError):
    def __init__(self, detail: str = "invalid execution request") -> None:
        super().__init__(ErrorCode.INVALID_REQUEST, detail, 422)


class PolicyDeniedError(SandboxError):
    def __init__(self, detail: str = "execution denied by sandbox policy") -> None:
        super().__init__(ErrorCode.POLICY_DENIED, detail, 403)


class RuntimeUnavailableError(SandboxError):
    def __init__(self, detail: str = "sandbox runtime unavailable") -> None:
        super().__init__(ErrorCode.RUNTIME_UNAVAILABLE, detail, 503)


class ImageUnavailableError(SandboxError):
    def __init__(self, detail: str = "sandbox runner image unavailable") -> None:
        super().__init__(ErrorCode.IMAGE_UNAVAILABLE, detail, 503)


class RequestConflictError(SandboxError):
    def __init__(self, detail: str = "request id is already active") -> None:
        super().__init__(ErrorCode.REQUEST_CONFLICT, detail, 409)


class ConcurrencyLimitError(SandboxError):
    def __init__(self, detail: str = "sandbox concurrency limit reached") -> None:
        super().__init__(ErrorCode.CONCURRENCY_LIMIT, detail, 429)


class DaemonShuttingDownError(SandboxError):
    def __init__(self, detail: str = "sandbox daemon is shutting down") -> None:
        super().__init__(ErrorCode.DAEMON_SHUTTING_DOWN, detail, 503)


class CleanupFailedError(SandboxError):
    def __init__(self, detail: str = "sandbox execution cleanup failed") -> None:
        super().__init__(ErrorCode.CLEANUP_FAILED, detail, 500)


__all__ = [
    "CleanupFailedError",
    "ConcurrencyLimitError",
    "DaemonShuttingDownError",
    "ImageUnavailableError",
    "InvalidRequestError",
    "PolicyDeniedError",
    "RequestConflictError",
    "RuntimeUnavailableError",
    "SandboxError",
]
