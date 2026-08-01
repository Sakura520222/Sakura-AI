"""Immutable contracts for activity observability services."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any


_ENDPOINT_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_SNAPSHOT_PATTERN = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.-]*://|\b(?:authorization|bearer|basic)\b|\b(?:api[_-]?key|access[_-]?token|password|passwd|secret|credential|private[_-]?key|token)\s*[:=])",
    re.IGNORECASE,
)

# 业务级消息标识 allowlist：写入侧与投影侧共享，避免任意 metadata 借道公开
# 时间线泄漏。稳定标识让实时监控能区分"标签推荐请求/响应"等卡片，而不依
# 赖受限正文或对 summary 等技术角色的推断。Public business-kind allowlist
# shared by writers and the projection so arbitrary metadata cannot leak
# through the public timeline and cards stay distinguishable without touching
# restricted content or guessing from technical roles.
ALLOWED_MESSAGE_KINDS = frozenset(
    {"label_recommendation_request", "label_recommendation_response"}
)


def _require_string(value: object, field_name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_int(value: object, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")


def _require_datetime(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")


@dataclass(frozen=True, slots=True)
class RoleConfigSnapshot:
    """Credential-free role configuration captured before execution."""

    role: str
    requested_provider: str
    requested_model: str
    requested_thinking_mode: str | None
    candidate_chain: tuple[tuple[str, str], ...]
    account_id: str
    protocol_family: str
    endpoint_fingerprint: str
    config_snapshot_version: int
    captured_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "role",
            "requested_provider",
            "requested_model",
            "account_id",
            "protocol_family",
            "endpoint_fingerprint",
        ):
            _require_string(getattr(self, field_name), field_name)
        _require_string(
            self.requested_thinking_mode,
            "requested_thinking_mode",
            optional=True,
        )
        _require_int(self.config_snapshot_version, "config_snapshot_version")
        _require_datetime(self.captured_at, "captured_at")
        if not _ENDPOINT_FINGERPRINT_PATTERN.fullmatch(self.endpoint_fingerprint):
            raise ValueError(
                "endpoint_fingerprint must be a lowercase 64-character "
                "SHA-256 hex digest"
            )

        chain = self.candidate_chain
        if not isinstance(chain, tuple) or any(
            not isinstance(candidate, tuple)
            or len(candidate) != 2
            or any(not isinstance(value, str) for value in candidate)
            for candidate in chain
        ):
            raise TypeError(
                "candidate_chain must be tuple[tuple[str, str], ...]; "
                "list and dict values are not allowed"
            )


@dataclass(frozen=True, slots=True)
class InvocationContext:
    """Stable identifiers propagated through one model execution."""

    invocation_id: int
    work_unit_id: int
    thread_id: int | None
    role_snapshot: RoleConfigSnapshot

    def __post_init__(self) -> None:
        _require_int(self.invocation_id, "invocation_id")
        _require_int(self.work_unit_id, "work_unit_id")
        if self.thread_id is not None:
            _require_int(self.thread_id, "thread_id")
        if not isinstance(self.role_snapshot, RoleConfigSnapshot):
            raise TypeError("role_snapshot must be a RoleConfigSnapshot")


@dataclass(frozen=True, slots=True)
class EffectiveReasoningSnapshot:
    """Credential-free final request settings for one concrete HTTP attempt."""

    requested_thinking_mode: str
    effective_thinking_mode: str
    requested_effort: str
    effective_effort: str
    protocol_family: str
    max_output_tokens: int
    temperature: float | None
    top_p: float | None
    top_k: int | None
    tool_choice: str | None

    def __post_init__(self) -> None:
        for field_name in (
            "requested_thinking_mode",
            "effective_thinking_mode",
            "requested_effort",
            "effective_effort",
            "protocol_family",
        ):
            _require_string(getattr(self, field_name), field_name)
        _require_int(self.max_output_tokens, "max_output_tokens")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        for field_name in ("temperature", "top_p"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, (int, float)) or isinstance(value, bool)
            ):
                raise TypeError(f"{field_name} must be numeric or None")
        if self.top_k is not None:
            _require_int(self.top_k, "top_k")
        _require_string(self.tool_choice, "tool_choice", optional=True)

    def safe_dict(self) -> dict[str, Any]:
        """Return the metadata-only representation suitable for persistence/logs."""
        return {
            "requested_thinking_mode": self.requested_thinking_mode,
            "effective_thinking_mode": self.effective_thinking_mode,
            "requested_effort": self.requested_effort,
            "effective_effort": self.effective_effort,
            "protocol_family": self.protocol_family,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "tool_choice": self.tool_choice,
        }


@dataclass(frozen=True, slots=True)
class PublicActivityNotification:
    """Complete internal notification routing contract.

    The Task 7 SSE projection deliberately selects only ``event_id``,
    ``sequence`` and ``projection_version`` from this internal contract.
    """

    event_id: str
    session_id: int
    invocation_id: int | None
    work_unit_id: int | None
    sequence: int
    projection_version: int
    created_at: datetime

    def __post_init__(self) -> None:
        _require_string(self.event_id, "event_id")
        _require_int(self.session_id, "session_id")
        if self.invocation_id is not None:
            _require_int(self.invocation_id, "invocation_id")
        if self.work_unit_id is not None:
            _require_int(self.work_unit_id, "work_unit_id")
        _require_int(self.sequence, "sequence")
        _require_int(self.projection_version, "projection_version")
        _require_datetime(self.created_at, "created_at")

    def to_sse_data(self) -> dict[str, object]:
        """Return the deliberately tiny public notification projection."""
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "projection_version": self.projection_version,
        }
