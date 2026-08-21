"""Authorised projections and resumable cursors for activity observability.

This module is the single boundary between observability rows and REST clients.
It never trusts a browser-provided repository, role, or cursor claim: every child
object is resolved through its Session and the configured repository authorizer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.config import get_settings
from backend.core.time_service import format_rfc3339, now_utc
from backend.models.activity_observability_models import (
    ActivityCanonicalContextRevision,
    ActivityContextOperation,
    ActivityContextSnapshot,
    ActivityInvocation,
    ActivityInvocationTrigger,
    ActivityInvocationWorkUnit,
    ActivityModelAttempt,
    ActivityNativeArtifact,
    ActivityObservabilityEvent,
    ActivityObservabilitySession,
    ActivityThread,
    ActivityToolExecution,
    ActivityTrigger,
)
from backend.models.database import utc_now

_INVOCATION_TERMINAL_STATUSES = {
    "completed",
    "partial",
    "failed",
    "cancelled",
}

_SESSION_LIST_BATCH_MAX = 100


class ActivityNotFoundError(LookupError):
    """The resource is absent or not visible to the caller.

    Routes should map this to the same 404 for both cases, preventing resource
    enumeration through differing authorization errors.
    """


class CursorResetRequiredError(ValueError):
    """The client must discard its cursor and obtain a fresh snapshot."""


class RepositoryScopeAuthorizer(Protocol):
    """Application-provided repository and trace policy boundary."""

    async def authorize_session(
        self, db: AsyncSession, *, session: ActivityObservabilitySession, user: dict
    ) -> bool: ...

    async def authorization_version(self, db: AsyncSession, *, user: dict) -> str: ...

    async def may_view_trace(
        self,
        db: AsyncSession,
        *,
        session: ActivityObservabilitySession,
        user: dict,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class CursorConfig:
    """Signed cursor policy; the secret must be supplied by configuration."""

    secret: str
    ttl_seconds: int
    page_size: int
    projection_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.secret, str) or len(self.secret) < 32:
            raise ValueError(
                "activity cursor signing secret must be at least 32 characters"
            )
        if self.ttl_seconds <= 0:
            raise ValueError("cursor ttl must be positive")
        if self.page_size <= 0:
            raise ValueError("cursor page size must be positive")
        if self.projection_version <= 0:
            raise ValueError("projection version must be positive")


_SAFE_SESSION_KEYS = (
    "session_id",
    "session_kind",
    "status",
    "resource_type",
    "resource_number",
    "repo_full_name",
    "last_active_at",
    "created_at",
    "archived_at",
)
_SAFE_EVENT_PAYLOAD_KEYS = frozenset(
    {
        "status",
        "phase",
        "current_phase",
        "purpose",
        "work_unit_status",
        "attempt_status",
        "reasoning_availability",
        "reasoning_safe_summary",
        "safe_summary",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_input_tokens",
        "*_availability",
        "*_source",
    }
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("activity timestamps must be aware")
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    value = _as_utc(value)
    return format_rfc3339(value) if value is not None else None


def _is_admin(user: dict[str, Any]) -> bool:
    return user.get("role") in {"admin", "super_admin"}


def _is_super_admin(user: dict[str, Any]) -> bool:
    return user.get("role") == "super_admin"


def _safe_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    return value


def _safe_payload(payload: dict[str, Any], *, admin: bool) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _SAFE_EVENT_PAYLOAD_KEYS or (
            key.endswith(("_availability", "_source"))
        ):
            if isinstance(value, (str, int, float, bool)) or value is None:
                result[key] = value
    # Admins still receive a whitelist, never raw JSON or a secret-bearing field.
    if admin and isinstance(payload.get("effective_model"), str):
        result["effective_model"] = payload["effective_model"]
    return result


def project_event(
    event: ActivityObservabilityEvent, user: dict[str, Any]
) -> dict[str, Any]:
    """Return a REST-safe event projection, never the raw projection JSON."""
    try:
        payload = json.loads(event.projection_json or "{}")
    except TypeError, json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "event_id": event.event_uuid,
        "sequence": int(event.event_sequence),
        "event_type": event.event_type,
        "visibility": event.visibility,
        "created_at": _iso(event.created_at),
        "invocation_id": event.invocation_id,
        "work_unit_id": event.work_unit_id,
        "payload": _safe_payload(payload, admin=_is_admin(user)),
    }


def project_attempt(
    attempt: ActivityModelAttempt, user: dict[str, Any]
) -> dict[str, Any]:
    """Project only safe lifecycle, model, thinking, and availability values."""
    result: dict[str, Any] = {
        "attempt_id": attempt.id,
        "attempt_index": attempt.attempt_index,
        "logical_call_id": attempt.logical_call_id,
        "attempt_kind": attempt.attempt_kind,
        "purpose": attempt.purpose,
        "status": attempt.status,
        "requested_provider": attempt.requested_provider,
        "requested_model": attempt.requested_model,
        "requested_thinking_mode": attempt.requested_thinking_mode,
        "requested_effort": attempt.requested_effort,
        "effective_provider": attempt.effective_provider,
        "effective_model": attempt.effective_model,
        "effective_thinking_mode": attempt.effective_thinking_mode,
        "effective_effort": attempt.effective_effort,
        "protocol_family": attempt.protocol_family,
        "max_output_tokens": attempt.max_output_tokens,
        "temperature": attempt.temperature,
        "top_p": attempt.top_p,
        "top_k": attempt.top_k,
        "tool_choice": attempt.tool_choice,
        "started_at": _iso(attempt.started_at),
        "first_token_at": _iso(attempt.first_token_at),
        "completed_at": _iso(attempt.completed_at),
        "stop_reason": attempt.stop_reason,
        "http_status": attempt.http_status,
        "retryable": attempt.retryable,
        "input_tokens": attempt.input_tokens,
        "input_tokens_availability": attempt.input_tokens_availability,
        "input_tokens_source": attempt.input_tokens_source,
        "output_tokens": attempt.output_tokens,
        "output_tokens_availability": attempt.output_tokens_availability,
        "output_tokens_source": attempt.output_tokens_source,
        "reasoning_tokens": attempt.reasoning_tokens,
        "reasoning_tokens_availability": attempt.reasoning_tokens_availability,
        "reasoning_tokens_source": attempt.reasoning_tokens_source,
        "reasoning_availability": attempt.reasoning_availability,
        "reasoning_started_at": _iso(attempt.reasoning_started_at),
        "reasoning_completed_at": _iso(attempt.reasoning_completed_at),
        "cached_input_tokens": attempt.cached_input_tokens,
        "cached_input_tokens_availability": attempt.cached_input_tokens_availability,
        "cached_input_tokens_source": attempt.cached_input_tokens_source,
        "context_revision_id": attempt.context_revision_id,
        "retry_of_attempt_id": attempt.retry_of_attempt_id,
        "fallback_from_attempt_id": attempt.fallback_from_attempt_id,
        "error_category": attempt.error_category,
    }
    return result


def project_context_snapshot(
    snapshot: ActivityContextSnapshot,
    attempt: ActivityModelAttempt | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "snapshot_id": snapshot.id,
        "snapshot_kind": snapshot.snapshot_kind,
        "context_revision_id": snapshot.context_revision_id,
        "created_at": _iso(snapshot.created_at),
    }
    for name in (
        "context_tokens",
        "context_window_tokens",
        "reserved_output_tokens",
        "available_context_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_context_tokens",
    ):
        fields[name] = getattr(snapshot, name)
        fields[f"{name}_availability"] = getattr(snapshot, f"{name}_availability")
        fields[f"{name}_source"] = getattr(snapshot, f"{name}_source")
    if attempt is not None:
        provider_fields = {
            "context_tokens": "input_tokens",
            "cache_read_tokens": "cached_input_tokens",
            "reasoning_context_tokens": "reasoning_tokens",
        }
        for context_name, attempt_name in provider_fields.items():
            value = getattr(attempt, attempt_name)
            if value is None:
                continue
            fields[context_name] = int(value)
            fields[f"{context_name}_availability"] = getattr(
                attempt,
                f"{attempt_name}_availability",
            )
            fields[f"{context_name}_source"] = getattr(
                attempt,
                f"{attempt_name}_source",
            )
    return fields


def project_context_operation(operation: ActivityContextOperation) -> dict[str, Any]:
    return {
        "operation_id": operation.id,
        "work_unit_id": operation.work_unit_id,
        "thread_id": operation.thread_id,
        "operation_type": operation.operation_type,
        "trigger_reason": operation.trigger_reason,
        "status": operation.status,
        "before_revision_id": operation.before_revision_id,
        "after_revision_id": operation.after_revision_id,
        "created_at": _iso(operation.created_at),
        "completed_at": _iso(operation.completed_at),
    }


def project_tool_execution(execution: ActivityToolExecution) -> dict[str, Any]:
    return {
        "tool_execution_id": execution.id,
        "tool_call_id": execution.tool_call_id,
        "name": execution.name,
        "status": execution.status,
        "started_at": _iso(execution.started_at),
        "completed_at": _iso(execution.completed_at),
    }


def project_work_unit(
    work_unit: ActivityInvocationWorkUnit,
    attempts: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "work_unit_id": work_unit.id,
        "thread_id": work_unit.thread_id,
        "purpose": work_unit.purpose,
        "requirement": work_unit.requirement,
        "is_primary": bool(work_unit.is_primary),
        "status": work_unit.status,
        "current_phase": work_unit.current_phase,
        "requested_provider": work_unit.requested_provider,
        "requested_model": work_unit.requested_model,
        "requested_thinking_mode": work_unit.requested_thinking_mode,
        "final_provider": work_unit.final_provider,
        "final_model": work_unit.final_model,
        "final_thinking_mode": work_unit.final_thinking_mode,
        "started_at": _iso(work_unit.started_at),
        "completed_at": _iso(work_unit.completed_at),
        "attempts": attempts,
        "tools": tools,
    }


def project_invocation(
    invocation: ActivityInvocation,
    work_units: list[dict[str, Any]],
    triggers: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "invocation_id": invocation.id,
        "status": invocation.status,
        "current_phase": invocation.current_phase,
        "task_type": invocation.task_type,
        "task_id": invocation.task_id,
        "primary_work_unit_id": invocation.primary_work_unit_id,
        "base_sha": invocation.base_sha,
        "initial_head_sha": invocation.initial_head_sha,
        "final_head_sha": invocation.final_head_sha,
        "created_at": _iso(invocation.created_at),
        "started_at": _iso(invocation.started_at),
        "completed_at": _iso(invocation.completed_at),
        "work_units": work_units,
        "triggers": triggers,
    }


def project_session(
    session: ActivityObservabilitySession, user: dict[str, Any]
) -> dict[str, Any]:
    identity = session.resource_identity
    return {
        "session_id": session.id,
        "session_kind": session.session_kind,
        "status": session.status,
        "resource_type": identity.resource_type if identity else None,
        "resource_number": identity.resource_number if identity else None,
        "repo_full_name": identity.repo_full_name if identity else None,
        "event_sequence": int(session.session_event_sequence or 0),
        "last_active_at": _iso(session.last_active_at),
        "created_at": _iso(session.created_at),
        "archived_at": _iso(session.archived_at),
    }


def _select_display_invocation(
    invocations: list[ActivityInvocation],
) -> ActivityInvocation | None:
    """Prefer an active execution, otherwise return the newest terminal one."""
    return next(
        (
            invocation
            for invocation in invocations
            if invocation.status not in _INVOCATION_TERMINAL_STATUSES
        ),
        invocations[0] if invocations else None,
    )


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise CursorResetRequiredError("invalid cursor")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError) as exc:
        raise CursorResetRequiredError("invalid cursor") from exc


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


class ActivityAccessService:
    """Session authorization, safe projections, signed cursors and snapshots."""

    def __init__(
        self,
        db: AsyncSession | None = None,
        *,
        authorizer: RepositoryScopeAuthorizer | Callable[..., Any] | None = None,
        cursor_config: CursorConfig | None = None,
        now: Callable[[], datetime] = utc_now,
        super_admin_global_policy: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        self.db = db
        self.authorizer = authorizer
        self.cursor_config = cursor_config
        self.now = now
        self.super_admin_global_policy = super_admin_global_policy

    def _require_cursor_config(self) -> CursorConfig:
        if self.cursor_config is None:
            raise CursorResetRequiredError("activity cursor signing is not configured")
        return self.cursor_config

    @asynccontextmanager
    async def _session_scope(self):
        if self.db is not None:
            yield self.db
            return
        from backend.models import database as db_module

        if db_module.async_session is None:
            raise RuntimeError("异步数据库会话尚未初始化")
        async with db_module.async_session() as db:
            yield db

    async def _invoke(self, name: str, **kwargs: Any) -> Any:
        if self.authorizer is None:
            return None
        target = getattr(self.authorizer, name, None)
        if target is None and callable(self.authorizer):
            target = self.authorizer
        if target is None:
            return None
        try:
            params = inspect.signature(target).parameters
        except TypeError, ValueError:
            params = {}
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            value = target(**kwargs)
        else:
            value = target(**{k: v for k, v in kwargs.items() if k in params})
        if inspect.isawaitable(value):
            value = await value
        return value

    async def authorization_version(
        self, user: dict[str, Any], db: AsyncSession
    ) -> str:
        value = await self._invoke("authorization_version", db=db, user=user)
        if value is None:
            value = user.get("auth_version")
        if value is None:
            # A missing provider version is not a reason to make cursors
            # shareable; bind it to the authenticated principal's stable ID.
            value = f"user:{user.get('user_id', user.get('sub', ''))}"
        return str(value)

    async def _project_session_batch(
        self,
        rows: list[ActivityObservabilitySession],
        user: dict[str, Any],
        db: AsyncSession,
        projected: list[dict[str, Any]],
        page_size: int,
    ) -> None:
        """Project one bounded session batch after repository authorization."""
        session_ids = [int(item.id) for item in rows]
        invocation_rows = (
            list(
                (
                    await db.execute(
                        select(ActivityInvocation)
                        .where(ActivityInvocation.session_id.in_(session_ids))
                        .order_by(
                            ActivityInvocation.session_id,
                            desc(ActivityInvocation.id),
                        )
                    )
                ).scalars()
            )
            if session_ids
            else []
        )
        invocations_by_session: dict[int, list[ActivityInvocation]] = {}
        for invocation in invocation_rows:
            invocations_by_session.setdefault(
                int(invocation.session_id),
                [],
            ).append(invocation)
        display_invocation_by_session = {
            session_id: selected
            for session_id, items in invocations_by_session.items()
            if (selected := _select_display_invocation(items)) is not None
        }
        display_invocation_ids = [
            int(item.id) for item in display_invocation_by_session.values()
        ]
        work_units = (
            list(
                (
                    await db.execute(
                        select(ActivityInvocationWorkUnit)
                        .where(
                            ActivityInvocationWorkUnit.invocation_id.in_(
                                display_invocation_ids
                            )
                        )
                        .order_by(
                            ActivityInvocationWorkUnit.invocation_id,
                            desc(ActivityInvocationWorkUnit.is_primary),
                            desc(ActivityInvocationWorkUnit.id),
                        )
                    )
                ).scalars()
            )
            if display_invocation_ids
            else []
        )
        work_units_by_invocation: dict[int, list[ActivityInvocationWorkUnit]] = {}
        for work_unit in work_units:
            work_units_by_invocation.setdefault(
                int(work_unit.invocation_id),
                [],
            ).append(work_unit)
        work_unit_ids = [int(item.id) for item in work_units]
        attempts = (
            list(
                (
                    await db.execute(
                        select(ActivityModelAttempt)
                        .where(ActivityModelAttempt.work_unit_id.in_(work_unit_ids))
                        .order_by(
                            ActivityModelAttempt.work_unit_id,
                            desc(ActivityModelAttempt.attempt_index),
                        )
                    )
                ).scalars()
            )
            if work_unit_ids
            else []
        )
        latest_attempt_by_work_unit: dict[int, ActivityModelAttempt] = {}
        for attempt in attempts:
            latest_attempt_by_work_unit.setdefault(int(attempt.work_unit_id), attempt)

        for session in rows:
            if len(projected) >= page_size:
                return
            try:
                await self.require_session_access(session.id, user, db)
            except ActivityNotFoundError:
                continue
            item = project_session(session, user)
            invocation = display_invocation_by_session.get(int(session.id))
            item["session_status"] = session.status
            item["invocation_id"] = (
                int(invocation.id) if invocation is not None else None
            )
            if session.status != "archived" and invocation is not None:
                item["status"] = invocation.status
            selected_work_unit = None
            attempt = None
            if invocation is not None:
                invocation_work_units = work_units_by_invocation.get(
                    int(invocation.id),
                    [],
                )
                selected_work_unit = next(
                    (
                        work_unit
                        for work_unit in invocation_work_units
                        if work_unit.is_primary
                    ),
                    invocation_work_units[0] if invocation_work_units else None,
                )
                if selected_work_unit is not None:
                    attempt = latest_attempt_by_work_unit.get(
                        int(selected_work_unit.id)
                    )
            item.update(
                {
                    "current_phase": (
                        invocation.current_phase if invocation is not None else None
                    ),
                    "active_provider": (
                        attempt.effective_provider
                        if attempt is not None
                        else selected_work_unit.final_provider
                        if selected_work_unit is not None
                        else None
                    ),
                    "active_model": (
                        attempt.effective_model
                        if attempt is not None
                        else selected_work_unit.final_model
                        if selected_work_unit is not None
                        else None
                    ),
                    "thinking_mode": (
                        attempt.effective_thinking_mode
                        if attempt is not None
                        else selected_work_unit.final_thinking_mode
                        if selected_work_unit is not None
                        else None
                    ),
                    "attempt_kind": (
                        attempt.attempt_kind if attempt is not None else None
                    ),
                    "attempt_status": (attempt.status if attempt is not None else None),
                }
            )
            projected.append(item)

    async def list_sessions(
        self,
        user: dict[str, Any],
        *,
        limit: int = 20,
        scan_buffer: int = 3,
        db: AsyncSession | None = None,
    ) -> dict[str, Any]:
        """Return authorised sessions, most recent first.

        Authorization is deliberately evaluated after each bounded batch rather
        than after one global ``LIMIT``. Keyset scanning uses the timestamp/id
        pair for deterministic ordering and stops at the page size or data
        exhaustion. Each query is independently bounded; the scanner must
        continue through arbitrarily many unauthorized rows so an authorized
        session cannot be hidden behind a fixed global limit.
        """
        db = db or self.db
        if db is None:
            raise RuntimeError("db is required")
        page_size = max(1, min(int(limit), 100))
        scan_multiplier = max(1, min(int(scan_buffer), 10))
        batch_size = min(
            _SESSION_LIST_BATCH_MAX,
            max(page_size, page_size * scan_multiplier),
        )
        projected: list[dict[str, Any]] = []
        has_cursor = False
        last_active_value: datetime | None = None
        last_id: int | None = None

        while len(projected) < page_size:
            current_limit = batch_size
            statement = (
                select(ActivityObservabilitySession)
                .options(selectinload(ActivityObservabilitySession.resource_identity))
                .order_by(
                    desc(ActivityObservabilitySession.last_active_at),
                    desc(ActivityObservabilitySession.id),
                )
                .limit(current_limit)
            )
            if has_cursor:
                if last_active_value is None:
                    statement = statement.where(
                        and_(
                            ActivityObservabilitySession.last_active_at.is_(None),
                            ActivityObservabilitySession.id < last_id,
                        )
                    )
                else:
                    statement = statement.where(
                        or_(
                            ActivityObservabilitySession.last_active_at
                            < last_active_value,
                            and_(
                                ActivityObservabilitySession.last_active_at
                                == last_active_value,
                                ActivityObservabilitySession.id < last_id,
                            ),
                            ActivityObservabilitySession.last_active_at.is_(None),
                        )
                    )
            rows = list((await db.execute(statement)).scalars())
            if not rows:
                break
            await self._project_session_batch(
                rows,
                user,
                db,
                projected,
                page_size,
            )
            tail = rows[-1]
            last_active_value = tail.last_active_at
            last_id = int(tail.id)
            has_cursor = True
            if len(rows) < current_limit:
                break

        return {"sessions": projected}

    async def require_session_access(
        self, session_id: int, user: dict[str, Any], db: AsyncSession | None = None
    ) -> ActivityObservabilitySession:
        db = db or self.db
        if db is None:
            raise RuntimeError("db is required")
        session = (
            await db.execute(
                select(ActivityObservabilitySession)
                .options(selectinload(ActivityObservabilitySession.resource_identity))
                .where(ActivityObservabilitySession.id == session_id)
            )
        ).scalar_one_or_none()
        if session is None:
            raise ActivityNotFoundError("activity session not found")

        authorised = await self._invoke(
            "authorize_session", db=db, session=session, user=user
        )
        if authorised is None:
            # No repository authorizer means fail closed, including admins.
            if _is_super_admin(user) and self.super_admin_global_policy is not None:
                authorised = bool(self.super_admin_global_policy(user))
            else:
                authorised = False
        if not authorised:
            raise ActivityNotFoundError("activity session not found")
        return session

    async def require_child_access(
        self,
        model: type,
        object_id: int,
        user: dict[str, Any],
        *,
        session_id: int | None = None,
        db: AsyncSession | None = None,
    ) -> Any:
        db = db or self.db
        if db is None:
            raise RuntimeError("db is required")
        row = await db.get(model, object_id)
        if row is None:
            raise ActivityNotFoundError("activity object not found")
        resolved_session_id = session_id
        if resolved_session_id is None:
            if isinstance(row, (ActivityInvocation, ActivityInvocationWorkUnit)):
                resolved_session_id = row.session_id
            elif isinstance(row, (ActivityModelAttempt, ActivityToolExecution)):
                work_unit = await db.get(ActivityInvocationWorkUnit, row.work_unit_id)
                resolved_session_id = work_unit.session_id if work_unit else None
            elif isinstance(row, ActivityNativeArtifact):
                attempt = (
                    await db.get(ActivityModelAttempt, row.attempt_id)
                    if row.attempt_id
                    else None
                )
                work_unit = (
                    await db.get(ActivityInvocationWorkUnit, attempt.work_unit_id)
                    if attempt
                    else None
                )
                resolved_session_id = work_unit.session_id if work_unit else None
        if resolved_session_id is None:
            raise ActivityNotFoundError("activity object not found")
        await self.require_session_access(resolved_session_id, user, db)
        return row

    def create_cursor(
        self,
        *,
        session_id: int,
        last_scanned_sequence: int,
        authorization_version: str,
        projection_version: int | None = None,
        issued_at: datetime | None = None,
    ) -> str:
        config = self._require_cursor_config()
        issued = _as_utc(issued_at or self.now())
        assert issued is not None
        expires = issued.timestamp() + config.ttl_seconds
        body = {
            "v": 1,
            "session_id": int(session_id),
            "last_scanned_sequence": int(last_scanned_sequence),
            "auth_version": str(authorization_version),
            "projection_version": int(projection_version or config.projection_version),
            "issued_at": int(issued.timestamp()),
            "expires_at": int(expires),
        }
        encoded = _b64_encode(_canonical_json(body))
        signature = hmac.new(
            config.secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        return f"{encoded}.{_b64_encode(signature)}"

    def decode_cursor(
        self,
        cursor: str,
        *,
        session_id: int,
        authorization_version: str,
        projection_version: int | None = None,
    ) -> dict[str, Any]:
        config = self._require_cursor_config()
        try:
            encoded, signature = cursor.split(".", 1)
            expected = hmac.new(
                config.secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
            ).digest()
            actual = _b64_decode(signature)
            if not hmac.compare_digest(expected, actual):
                raise CursorResetRequiredError("invalid cursor signature")
            body = json.loads(_b64_decode(encoded))
            if not isinstance(body, dict):
                raise ValueError
            required = {
                "v",
                "session_id",
                "last_scanned_sequence",
                "auth_version",
                "projection_version",
                "issued_at",
                "expires_at",
            }
            if set(body) != required or body["v"] != 1:
                raise ValueError
            if body["session_id"] != int(session_id):
                raise ValueError
            if body["auth_version"] != str(authorization_version):
                raise CursorResetRequiredError("authorization version changed")
            if body["projection_version"] != int(
                projection_version or config.projection_version
            ):
                raise CursorResetRequiredError("projection version changed")
            now = (_as_utc(self.now()) or now_utc()).timestamp()
            if now >= int(body["expires_at"]):
                raise CursorResetRequiredError("cursor expired")
            if int(body["last_scanned_sequence"]) < 0:
                raise ValueError
            return body
        except CursorResetRequiredError:
            raise
        except (
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
            UnicodeError,
        ) as exc:
            raise CursorResetRequiredError("invalid cursor") from exc

    async def create_snapshot(
        self, session_id: int, user: dict[str, Any], *, db: AsyncSession | None = None
    ) -> dict[str, Any]:
        db = db or self.db
        if db is None:
            raise RuntimeError("db is required")
        session = await self.require_session_access(session_id, user, db)
        # append_event_and_outbox locks this same row, giving snapshot a stable
        # high-water mark relative to concurrent event commits.
        await db.refresh(session)
        auth_version = await self.authorization_version(user, db)
        high_water = int(session.session_event_sequence or 0)
        invocations = list(
            (
                await db.execute(
                    select(ActivityInvocation)
                    .where(ActivityInvocation.session_id == session_id)
                    .order_by(desc(ActivityInvocation.id))
                    .limit(25)
                )
            ).scalars()
        )
        invocation_ids = [int(item.id) for item in invocations]
        work_units = (
            list(
                (
                    await db.execute(
                        select(ActivityInvocationWorkUnit)
                        .where(
                            ActivityInvocationWorkUnit.invocation_id.in_(invocation_ids)
                        )
                        .order_by(ActivityInvocationWorkUnit.id)
                    )
                ).scalars()
            )
            if invocation_ids
            else []
        )
        work_unit_ids = [int(item.id) for item in work_units]
        attempts = (
            list(
                (
                    await db.execute(
                        select(ActivityModelAttempt)
                        .where(ActivityModelAttempt.work_unit_id.in_(work_unit_ids))
                        .order_by(
                            ActivityModelAttempt.work_unit_id,
                            ActivityModelAttempt.attempt_index,
                        )
                    )
                ).scalars()
            )
            if work_unit_ids
            else []
        )
        attempt_ids = [int(item.id) for item in attempts]
        context_snapshots = (
            list(
                (
                    await db.execute(
                        select(ActivityContextSnapshot)
                        .where(ActivityContextSnapshot.attempt_id.in_(attempt_ids))
                        .order_by(ActivityContextSnapshot.id)
                    )
                ).scalars()
            )
            if attempt_ids
            else []
        )
        tools = (
            list(
                (
                    await db.execute(
                        select(ActivityToolExecution)
                        .where(ActivityToolExecution.work_unit_id.in_(work_unit_ids))
                        .order_by(ActivityToolExecution.id)
                    )
                ).scalars()
            )
            if work_unit_ids
            else []
        )
        operations = (
            list(
                (
                    await db.execute(
                        select(ActivityContextOperation)
                        .where(ActivityContextOperation.work_unit_id.in_(work_unit_ids))
                        .order_by(desc(ActivityContextOperation.id))
                    )
                ).scalars()
            )
            if work_unit_ids
            else []
        )
        threads = list(
            (
                await db.execute(
                    select(ActivityThread)
                    .where(ActivityThread.session_id == session_id)
                    .order_by(ActivityThread.id)
                )
            ).scalars()
        )
        revision_ids = [
            int(item.current_revision_id)
            for item in threads
            if item.current_revision_id is not None
        ]
        revisions = (
            {
                int(item.id): item
                for item in (
                    await db.execute(
                        select(ActivityCanonicalContextRevision).where(
                            ActivityCanonicalContextRevision.id.in_(revision_ids)
                        )
                    )
                ).scalars()
            }
            if revision_ids
            else {}
        )
        trigger_links = (
            list(
                (
                    await db.execute(
                        select(ActivityInvocationTrigger, ActivityTrigger)
                        .join(
                            ActivityTrigger,
                            ActivityTrigger.id == ActivityInvocationTrigger.trigger_id,
                        )
                        .where(
                            ActivityInvocationTrigger.invocation_id.in_(invocation_ids)
                        )
                        .order_by(ActivityInvocationTrigger.id)
                    )
                ).all()
            )
            if invocation_ids
            else []
        )

        latest_snapshot_by_attempt: dict[int, ActivityContextSnapshot] = {}
        for item in context_snapshots:
            if item.attempt_id is not None:
                latest_snapshot_by_attempt[int(item.attempt_id)] = item
        attempts_by_work_unit: dict[int, list[dict[str, Any]]] = {}
        for attempt in attempts:
            projected_attempt = project_attempt(attempt, user)
            latest_context = latest_snapshot_by_attempt.get(int(attempt.id))
            projected_attempt["context"] = (
                project_context_snapshot(latest_context, attempt)
                if latest_context is not None
                else None
            )
            attempts_by_work_unit.setdefault(int(attempt.work_unit_id), []).append(
                projected_attempt
            )
        tools_by_work_unit: dict[int, list[dict[str, Any]]] = {}
        for tool in tools:
            tools_by_work_unit.setdefault(int(tool.work_unit_id), []).append(
                project_tool_execution(tool)
            )
        work_units_by_invocation: dict[int, list[dict[str, Any]]] = {}
        for work_unit in work_units:
            work_units_by_invocation.setdefault(
                int(work_unit.invocation_id), []
            ).append(
                project_work_unit(
                    work_unit,
                    attempts_by_work_unit.get(int(work_unit.id), []),
                    tools_by_work_unit.get(int(work_unit.id), []),
                )
            )
        triggers_by_invocation: dict[int, list[dict[str, Any]]] = {}
        for link, trigger in trigger_links:
            triggers_by_invocation.setdefault(int(link.invocation_id), []).append(
                {
                    "trigger_id": trigger.id,
                    "trigger_kind": trigger.trigger_kind,
                    "status": trigger.status,
                    "base_sha": trigger.base_sha,
                    "head_sha": trigger.head_sha,
                    "created_at": _iso(trigger.created_at),
                }
            )
        projected_invocations = [
            project_invocation(
                invocation,
                work_units_by_invocation.get(int(invocation.id), []),
                triggers_by_invocation.get(int(invocation.id), []),
            )
            for invocation in invocations
        ]
        active_attempts = [
            project_attempt(item, user) for item in attempts if item.status == "running"
        ]
        # The detail payload only carries the newest 25 Invocations, but the
        # diagnostic cards are explicitly session totals. Aggregate directly
        # over every Attempt in the Session so long-lived PR/Issue sessions do
        # not silently lose older or auxiliary model usage.
        usage_row = (
            await db.execute(
                select(
                    func.sum(ActivityModelAttempt.input_tokens).label("input_tokens"),
                    func.sum(ActivityModelAttempt.output_tokens).label("output_tokens"),
                    func.sum(ActivityModelAttempt.reasoning_tokens).label(
                        "reasoning_tokens"
                    ),
                    func.sum(ActivityModelAttempt.cached_input_tokens).label(
                        "cached_input_tokens"
                    ),
                )
                .select_from(ActivityModelAttempt)
                .join(
                    ActivityInvocationWorkUnit,
                    ActivityModelAttempt.work_unit_id == ActivityInvocationWorkUnit.id,
                )
                .where(ActivityInvocationWorkUnit.session_id == session_id)
            )
        ).one()
        usage_totals = {
            name: int(value) if value is not None else None
            for name in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cached_input_tokens",
            )
            if (value := getattr(usage_row, name)) is not None
        }
        for name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cached_input_tokens",
        ):
            usage_totals.setdefault(name, None)
        newest_invocation = _select_display_invocation(invocations)
        thread_projection = []
        for thread in threads:
            revision = (
                revisions.get(int(thread.current_revision_id))
                if thread.current_revision_id is not None
                else None
            )
            thread_projection.append(
                {
                    "thread_id": thread.id,
                    "purpose": thread.thread_purpose,
                    "current_revision_id": thread.current_revision_id,
                    "revision_number": revision.revision_number
                    if revision is not None
                    else None,
                    "revision_reason": revision.reason
                    if revision is not None
                    else None,
                    "message_count": int(thread.last_seq or 0),
                    "last_active_at": _iso(thread.last_active_at),
                }
            )
        session_projection = project_session(session, user)
        session_projection["session_status"] = session.status
        session_projection["invocation_id"] = (
            int(newest_invocation.id) if newest_invocation is not None else None
        )
        if session.status != "archived" and newest_invocation is not None:
            session_projection["status"] = newest_invocation.status
        return {
            "session": session_projection,
            "high_water_mark": high_water,
            "current_phase": (
                newest_invocation.current_phase
                if newest_invocation is not None
                else None
            ),
            "active_models": active_attempts,
            "usage_totals": usage_totals,
            "threads": thread_projection,
            "context_operations": [
                project_context_operation(item) for item in operations
            ],
            "invocations": projected_invocations,
            "cursor": self.create_cursor(
                session_id=session_id,
                last_scanned_sequence=high_water,
                authorization_version=auth_version,
            ),
        }

    async def list_events_after(
        self,
        session_id: int,
        user: dict[str, Any],
        *,
        cursor: str | None = None,
        db: AsyncSession | None = None,
    ) -> dict[str, Any]:
        db = db or self.db
        if db is None:
            raise RuntimeError("db is required")
        await self.require_session_access(session_id, user, db)
        auth_version = await self.authorization_version(user, db)
        config = self._require_cursor_config()
        if cursor is None:
            last_sequence = 0
        else:
            body = self.decode_cursor(
                cursor,
                session_id=session_id,
                authorization_version=auth_version,
            )
            last_sequence = int(body["last_scanned_sequence"])

        # Scan every sequence after the cursor (including hidden/internal rows)
        # so a hidden row can never block cursor progress or cause a replay loop.
        rows = (
            (
                await db.execute(
                    select(ActivityObservabilityEvent)
                    .where(
                        ActivityObservabilityEvent.session_id == session_id,
                        ActivityObservabilityEvent.event_sequence > last_sequence,
                    )
                    .order_by(ActivityObservabilityEvent.event_sequence)
                )
            )
            .scalars()
            .all()
        )
        visible: list[dict[str, Any]] = []
        scanned = last_sequence
        for event in rows:
            sequence = int(event.event_sequence)
            if event.visibility == "hidden" or (
                event.visibility in {"internal", "admin_only"} and not _is_admin(user)
            ):
                # Hidden rows may advance the cursor. They are not part of the
                # visible page, so consuming them here cannot make a visible
                # row disappear from a later page.
                scanned = max(scanned, sequence)
                continue
            if len(visible) >= config.page_size:
                # Do not advance past the first omitted visible event. The
                # next request must be able to return it using this cursor.
                break
            visible.append(project_event(event, user))
            scanned = max(scanned, sequence)
        return {
            "events": visible,
            "cursor": self.create_cursor(
                session_id=session_id,
                last_scanned_sequence=scanned,
                authorization_version=auth_version,
            ),
            "last_scanned_sequence": scanned,
        }

    async def may_view_reasoning_artifact(
        self,
        user: dict[str, Any],
        session: ActivityObservabilitySession,
        artifact: ActivityNativeArtifact,
        *,
        db: AsyncSession | None = None,
    ) -> bool:
        db = db or self.db
        if db is None:
            return False
        if not _is_super_admin(user):
            return False
        if not get_settings().activity_artifact_super_admin_read_enabled:
            return False
        if not await self._invoke(
            "authorize_session", db=db, session=session, user=user
        ):
            if not (
                _is_super_admin(user)
                and self.super_admin_global_policy
                and self.super_admin_global_policy(user)
            ):
                return False
        trace_allowed = await self._invoke(
            "may_view_trace", db=db, session=session, user=user
        )
        if trace_allowed is not True:
            return False
        if artifact.visibility not in {"admin_only", "public"}:
            return False
        if artifact.capture_mode != "artifact":
            return False
        return artifact.availability in {"summarized", "provider_exposed"}


async def require_session_access(
    session_id: int, user: dict[str, Any], db: AsyncSession, *, authorizer: Any
) -> ActivityObservabilitySession:
    return await ActivityAccessService(
        db, authorizer=authorizer
    ).require_session_access(session_id, user, db)


__all__ = [
    "ActivityAccessService",
    "ActivityNotFoundError",
    "CursorConfig",
    "CursorResetRequiredError",
    "RepositoryScopeAuthorizer",
    "project_attempt",
    "project_event",
    "project_session",
    "require_session_access",
]
