"""Authoritative provider-attempt lifecycle service.

Every row created by this service represents one application-owned provider send.
It deliberately does not infer attempts from ActivityEvent/compatibility metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import database as db_module
from backend.models.activity_observability_models import (
    ActivityCanonicalContextRevision,
    ActivityInvocation,
    ActivityInvocationWorkUnit,
    ActivityModelAttempt,
    ActivityObservabilitySession,
    ActivityThread,
)
from backend.models.database import utc_now
from backend.services.activity_observability.contracts import (
    EffectiveReasoningSnapshot,
    InvocationContext,
)
from backend.services.activity_observability.event_service import append_lifecycle_event

TERMINAL_ATTEMPT_STATUSES = frozenset({"completed", "failed", "cancelled"})
ATTEMPT_STATUSES = frozenset({"running", *TERMINAL_ATTEMPT_STATUSES})

_SAFE_ERROR_MESSAGES = {
    "auth_invalid": "provider_authentication_failed",
    "permission_denied": "provider_permission_denied",
    "model_not_found": "provider_model_not_found",
    "bad_request": "provider_bad_request",
    "context_overflow": "context_overflow",
    "rate_limited": "provider_rate_limited",
    "server_error": "provider_server_error",
    "overloaded": "provider_overloaded",
    "network": "provider_network_error",
    "empty_response": "provider_empty_response",
    "refusal": "provider_refusal",
    "unknown": "internal_provider_error",
    "cancelled": "request_cancelled",
}
_SAFE_USAGE_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cached_tokens",
        "cached_input_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "reasoning_tokens",
        "total_tokens",
    }
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


def _safe_error_category(error: BaseException | str, explicit: str | None) -> str:
    """Return only a stable allowlisted category, never provider text."""
    candidate: Any = explicit
    if candidate is None:
        value = getattr(error, "category", None)
        candidate = getattr(value, "value", value)
    if isinstance(candidate, str) and candidate in _SAFE_ERROR_MESSAGES:
        return candidate
    return "unknown"


def _safe_http_status(value: Any) -> int | None:
    """Persist only a conventional HTTP status integer."""
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return None


def _safe_identifier(value: Any) -> str | None:
    """Keep request identifiers opaque and reject URLs/credential-shaped text."""
    if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value):
        return value
    return None


def _safe_normalized_usage(value: Mapping[str, Any] | None) -> dict[str, int] | None:
    """Normalize usage through the same numeric allowlist as provider usage."""
    if value is None:
        return None
    result = {
        str(key): item
        for key, item in value.items()
        if str(key) in _SAFE_USAGE_FIELDS
        and isinstance(item, int)
        and not isinstance(item, bool)
        and item >= 0
    }
    return result or None


def _safe_error_message(category: str, status: str) -> str:
    if status == "cancelled":
        return _SAFE_ERROR_MESSAGES["cancelled"]
    return _SAFE_ERROR_MESSAGES.get(category, _SAFE_ERROR_MESSAGES["unknown"])


def _safe_reasoning_metadata(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Keep only scalar provider phase metadata, excluding raw payloads."""
    if not isinstance(value, Mapping):
        return None
    allowed = {
        "event",
        "block_type",
        "delta_type",
        "item_type",
        "response_status",
        "finish_reason",
        "index",
        "signature_present",
        "redacted",
        "encrypted",
        "usage_fields",
    }
    forbidden = {
        "headers",
        "endpoint",
        "url",
        "request",
        "body",
        "credential",
        "raw",
        "payload",
    }
    result: dict[str, Any] = {}
    for key, item in value.items():
        key = str(key)
        if key not in allowed or key.lower() in forbidden:
            continue
        if isinstance(item, (bool, int, str)) and (
            not isinstance(item, int) or 0 <= item <= 1_000_000
        ):
            if not isinstance(item, str) or len(item) <= 128:
                result[key] = item
        elif key == "usage_fields" and isinstance(item, (list, tuple, set, frozenset)):
            fields = sorted(
                str(field) for field in item if str(field) in _SAFE_USAGE_FIELDS
            )
            if fields:
                result[key] = fields
    return result or None


class AttemptConflictError(RuntimeError):
    """A lifecycle replay conflicts with the persisted attempt payload."""


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _candidate_value(candidate: Any, name: str, default: Any = None) -> Any:
    value = _field(candidate, name, default)
    if value is not default:
        return value
    return default


def _candidate_parts(candidate: Any) -> dict[str, Any]:
    """Extract safe requested/effective fields from ResolvedModel or a mapping."""
    provider = _field(candidate, "provider")
    model = _field(candidate, "model")
    endpoint = _field(candidate, "endpoint")
    provider_id = (
        provider
        if isinstance(provider, str)
        else _field(provider, "id", _field(candidate, "provider_id", ""))
    )
    model_id = (
        model
        if isinstance(model, str)
        else _field(model, "model_id", _field(candidate, "model_id", ""))
    )
    family = _field(candidate, "effective_protocol")
    if family in (None, ""):
        family = _field(candidate, "protocol_family")
    if family in (None, ""):
        family = _field(provider, "family", "")
    if hasattr(family, "value"):
        family = family.value
    thinking = _field(candidate, "thinking_mode")
    if thinking is None:
        params = _field(model, "reasoning_params")
        thinking = _field(params, "thinking")
        if isinstance(thinking, Mapping):
            thinking = json.dumps(thinking, ensure_ascii=False, sort_keys=True)
    account_id = _field(candidate, "account_id")
    endpoint_url = _field(endpoint, "base_url", _field(candidate, "endpoint_url", ""))
    fingerprint = _field(candidate, "endpoint_fingerprint")
    if fingerprint is None and endpoint_url:
        fingerprint = hashlib.sha256(str(endpoint_url).encode("utf-8")).hexdigest()
    return {
        "provider": str(provider_id or ""),
        "model": str(model_id or ""),
        "thinking": thinking,
        "protocol": str(family or ""),
        "account_id": account_id,
        "endpoint_fingerprint": fingerprint,
    }


def _safe_usage_payload(value: Any) -> dict[str, Any] | None:
    """Keep only provider usage counters; never persist a response/request object."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        allowed = _SAFE_USAGE_FIELDS
        result = {
            str(k): v
            for k, v in value.items()
            if str(k) in allowed
            and isinstance(v, int)
            and not isinstance(v, bool)
            and v >= 0
        }
        return result or None
    result: dict[str, Any] = {}
    reported_fields = getattr(value, "reported_fields", None)
    if reported_fields is not None and not reported_fields:
        return None
    for name in (
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "reasoning_tokens",
    ):
        canonical = {
            "prompt_tokens": "input_tokens",
            "completion_tokens": "output_tokens",
        }.get(name, name)
        if reported_fields is not None and canonical not in reported_fields:
            continue
        value_part = getattr(value, name, None)
        if (
            isinstance(value_part, int)
            and not isinstance(value_part, bool)
            and value_part >= 0
        ):
            result[canonical] = value_part
    return result or None


def _response_usage(response: Any) -> tuple[dict[str, Any] | None, Any]:
    usage = getattr(response, "usage", None)
    raw = getattr(response, "raw", None)
    raw_usage = None
    if raw is not None:
        try:
            payload = raw.json() if callable(getattr(raw, "json", None)) else raw
            if isinstance(payload, Mapping):
                raw_usage = payload.get("usage")
        except Exception:  # provider raw response is never authoritative for lifecycle
            raw_usage = None
    return _safe_usage_payload(raw_usage), usage


class AttemptService:
    """Persist and transition one Work Unit's provider attempts."""

    def __init__(self, db: AsyncSession | None = None) -> None:
        self._db = db

    @asynccontextmanager
    async def _session_scope(self):
        if self._db is not None:
            yield self._db
            return
        if db_module.async_session is None:
            raise RuntimeError("异步数据库会话尚未初始化")
        async with db_module.async_session() as db:
            yield db

    async def _finish_owned(self, db: AsyncSession) -> None:
        if self._db is None:
            await db.commit()

    @staticmethod
    async def _set_execution_phase(
        db: AsyncSession,
        work_unit: ActivityInvocationWorkUnit,
        invocation: ActivityInvocation,
        phase: str,
        *,
        mark_started: bool = False,
    ) -> None:
        now = utc_now()
        if mark_started:
            if work_unit.status == "queued":
                work_unit.status = "running"
            if work_unit.started_at is None:
                work_unit.started_at = now
            if invocation.status == "queued":
                invocation.status = "running"
            if invocation.started_at is None:
                invocation.started_at = now
        work_unit.current_phase = phase
        invocation.current_phase = phase
        session = await db.get(ActivityObservabilitySession, invocation.session_id)
        if session is not None:
            session.last_active_at = now

    async def begin_attempt(
        self,
        context: InvocationContext,
        logical_call_id: str,
        attempt_kind: str,
        purpose: str,
        requested: Any,
        effective: Any,
        context_revision_id: int | None,
        retry_of: int | None = None,
        fallback_from: int | None = None,
        reasoning_snapshot: EffectiveReasoningSnapshot | None = None,
    ) -> ActivityModelAttempt:
        if not isinstance(context, InvocationContext):
            raise TypeError("context must be an InvocationContext")
        logical_call_id = _nonempty(logical_call_id, "logical_call_id")
        attempt_kind = _nonempty(attempt_kind, "attempt_kind")
        purpose = _nonempty(purpose, "purpose")
        if (
            retry_of is not None
            and fallback_from is not None
            and retry_of == fallback_from
        ):
            raise ValueError(
                "retry_of and fallback_from must be distinct relationships"
            )
        if purpose == "embedding":
            # Embeddings are contextless Work Units by contract: they never
            # inherit a transcript thread or a stale context revision.
            if context.thread_id is not None:
                raise ValueError("embedding attempts require thread_id=None")

        req = _candidate_parts(requested)
        eff = _candidate_parts(effective)
        for name, values in (("requested", req), ("effective", eff)):
            _nonempty(values["provider"], f"{name}.provider")
            _nonempty(values["model"], f"{name}.model")
        async with self._session_scope() as db:
            # Parent-first locking prevents attempts from being attached across
            # Invocation/Session/Thread chains and serializes attempt allocation.
            invocation = await db.get(
                ActivityInvocation, context.invocation_id, with_for_update=True
            )
            work_unit = await db.get(
                ActivityInvocationWorkUnit, context.work_unit_id, with_for_update=True
            )
            if invocation is None or work_unit is None:
                raise ValueError("InvocationContext parent does not exist")
            if (
                work_unit.invocation_id != invocation.id
                or work_unit.session_id != invocation.session_id
            ):
                raise ValueError("work unit does not belong to invocation")
            if context.thread_id != work_unit.thread_id:
                raise ValueError("InvocationContext thread does not match work unit")
            if purpose == "embedding" and work_unit.purpose != "embedding":
                raise ValueError("embedding attempts require an embedding work unit")
            if purpose != "embedding" and work_unit.purpose == "embedding":
                raise ValueError("embedding work units require purpose=embedding")

            revision = None
            if work_unit.thread_id is not None:
                thread = await db.get(
                    ActivityThread, work_unit.thread_id, with_for_update=True
                )
                if thread is None or thread.session_id != work_unit.session_id:
                    raise ValueError(
                        "work unit thread does not belong to invocation session"
                    )
                if context_revision_id is None:
                    raise ValueError("threaded attempts require context_revision_id")
                if thread.current_revision_id != context_revision_id:
                    raise ValueError(
                        "context_revision_id is not the thread current revision"
                    )
                revision = await db.get(
                    ActivityCanonicalContextRevision, context_revision_id
                )
                if revision is None or revision.thread_id != thread.id:
                    raise ValueError(
                        "context revision does not belong to work unit thread"
                    )
            elif context_revision_id is not None:
                raise ValueError("threadless attempts cannot have a context revision")

            parents: dict[str, ActivityModelAttempt] = {}
            for field_name, parent_id in (
                ("retry_of", retry_of),
                ("fallback_from", fallback_from),
            ):
                if parent_id is None:
                    continue
                parent = await db.get(
                    ActivityModelAttempt, parent_id, with_for_update=True
                )
                if parent is None or parent.work_unit_id != work_unit.id:
                    raise ValueError(
                        f"{field_name} attempt must belong to same work unit"
                    )
                parents[field_name] = parent
            if (
                retry_of is not None
                and parents["retry_of"].logical_call_id != logical_call_id
            ):
                raise ValueError("retry attempt must share logical_call_id")
            if (
                fallback_from is not None
                and parents["fallback_from"].logical_call_id != logical_call_id
            ):
                raise ValueError("fallback attempt must share logical_call_id")
            if retry_of is not None and attempt_kind not in {
                "retry",
                "compression_retry",
            }:
                raise ValueError("retry_of requires retry attempt_kind")
            if fallback_from is not None and attempt_kind not in {
                "fallback",
                "primary",
            }:
                raise ValueError("fallback_from requires fallback/primary attempt_kind")

            max_index = await db.execute(
                select(func.max(ActivityModelAttempt.attempt_index)).where(
                    ActivityModelAttempt.work_unit_id == work_unit.id
                )
            )
            attempt_index = int(max_index.scalar_one_or_none() or 0) + 1
            contextless_reason = None
            if work_unit.thread_id is None:
                contextless_reason = (
                    "threadless_embedding"
                    if purpose == "embedding"
                    else "transcript_not_applicable"
                )
            endpoint_fingerprint = (
                eff["endpoint_fingerprint"] or req["endpoint_fingerprint"]
            )
            if endpoint_fingerprint is not None and (
                not isinstance(endpoint_fingerprint, str)
                or len(endpoint_fingerprint) != 64
                or any(ch not in "0123456789abcdef" for ch in endpoint_fingerprint)
            ):
                raise ValueError("endpoint_fingerprint must be lowercase SHA-256 hex")
            attempt = ActivityModelAttempt(
                work_unit_id=work_unit.id,
                attempt_index=attempt_index,
                logical_call_id=logical_call_id,
                attempt_kind=attempt_kind,
                purpose=purpose,
                status="running",
                requested_provider=req["provider"],
                requested_model=req["model"],
                requested_thinking_mode=(
                    reasoning_snapshot.requested_thinking_mode
                    if reasoning_snapshot is not None
                    else req["thinking"]
                ),
                requested_effort=(
                    reasoning_snapshot.requested_effort
                    if reasoning_snapshot is not None
                    else None
                ),
                effective_provider=eff["provider"],
                effective_model=eff["model"],
                effective_thinking_mode=(
                    reasoning_snapshot.effective_thinking_mode
                    if reasoning_snapshot is not None
                    else eff["thinking"]
                ),
                effective_effort=(
                    reasoning_snapshot.effective_effort
                    if reasoning_snapshot is not None
                    else None
                ),
                protocol_family=(
                    reasoning_snapshot.protocol_family
                    if reasoning_snapshot is not None
                    else (eff["protocol"] or req["protocol"] or None)
                ),
                max_output_tokens=(
                    reasoning_snapshot.max_output_tokens
                    if reasoning_snapshot is not None
                    else None
                ),
                temperature=(
                    reasoning_snapshot.temperature
                    if reasoning_snapshot is not None
                    else None
                ),
                top_p=(
                    reasoning_snapshot.top_p if reasoning_snapshot is not None else None
                ),
                top_k=(
                    reasoning_snapshot.top_k if reasoning_snapshot is not None else None
                ),
                tool_choice=(
                    reasoning_snapshot.tool_choice
                    if reasoning_snapshot is not None
                    else None
                ),
                account_id=eff["account_id"]
                or req["account_id"]
                or context.role_snapshot.account_id,
                endpoint_fingerprint=endpoint_fingerprint,
                retry_of_attempt_id=retry_of,
                fallback_from_attempt_id=fallback_from,
                context_revision_id=context_revision_id,
                contextless_reason=contextless_reason,
                started_at=utc_now(),
            )
            db.add(attempt)
            await self._set_execution_phase(
                db,
                work_unit,
                invocation,
                "embedding_request" if purpose == "embedding" else "model_request",
                mark_started=True,
            )
            await db.flush()
            await append_lifecycle_event(
                db,
                session_id=invocation.session_id,
                invocation_id=invocation.id,
                work_unit_id=work_unit.id,
                event_type="attempt_started",
                payload={
                    "status": "running",
                    "attempt_status": "running",
                    "phase": work_unit.current_phase,
                    "purpose": purpose,
                },
            )
            await self._finish_owned(db)
            return attempt

    async def _load(self, db: AsyncSession, attempt_id: int) -> ActivityModelAttempt:
        attempt = await db.get(ActivityModelAttempt, attempt_id, with_for_update=True)
        if attempt is None:
            raise ValueError(f"ActivityModelAttempt not found: {attempt_id}")
        return attempt

    @staticmethod
    def _same(current: Any, value: Any) -> bool:
        return current == value

    async def record_reasoning_event(
        self,
        attempt_id: int,
        *,
        event_type: str,
        availability: str,
        provider_event_metadata: Mapping[str, Any] | None = None,
    ) -> ActivityModelAttempt:
        """Persist reasoning phase metadata only; never persist reasoning text."""
        allowed = {
            "unavailable",
            "omitted",
            "summarized",
            "provider_exposed",
            "encrypted_opaque",
        }
        availability = availability if availability in allowed else "omitted"
        safe_metadata = _safe_reasoning_metadata(provider_event_metadata)
        async with self._session_scope() as db:
            attempt = await self._load(db, attempt_id)
            if event_type == "reasoning_start" and attempt.reasoning_started_at is None:
                attempt.reasoning_started_at = utc_now()
            if event_type == "reasoning_end":
                attempt.reasoning_completed_at = utc_now()
            attempt.reasoning_availability = availability
            if safe_metadata:
                attempt.provider_event_metadata_json = json.dumps(
                    safe_metadata, separators=(",", ":"), sort_keys=True
                )
            await db.flush()
            work_unit = await db.get(ActivityInvocationWorkUnit, attempt.work_unit_id)
            if work_unit is not None:
                await append_lifecycle_event(
                    db,
                    session_id=work_unit.session_id,
                    invocation_id=work_unit.invocation_id,
                    work_unit_id=work_unit.id,
                    event_type="reasoning_status",
                    payload={
                        "attempt_status": attempt.status,
                        "reasoning_availability": availability,
                    },
                )
            await self._finish_owned(db)
            return attempt

    async def first_token(self, attempt_id: int) -> ActivityModelAttempt:
        """Record the first effective streamed token for an active attempt."""
        async with self._session_scope() as db:
            attempt = await self._load(db, attempt_id)
            if attempt.first_token_at is None:
                if attempt.status != "running":
                    raise AttemptConflictError(
                        "first token cannot follow terminal attempt"
                    )
                attempt.first_token_at = utc_now()
                await db.flush()
                await self._finish_owned(db)
            return attempt

    async def finish(
        self,
        attempt_id: int,
        response: Any = None,
        *,
        provider_request_id: str | None = None,
        http_status: int | None = None,
        stop_reason: str | None = None,
        raw_usage: Any = None,
        normalized_usage: Mapping[str, Any] | None = None,
    ) -> ActivityModelAttempt:
        usage_raw_from_response, usage = (
            _response_usage(response) if response is not None else (None, None)
        )
        safe_raw = _safe_usage_payload(raw_usage) or usage_raw_from_response
        if safe_raw is None and usage is not None:
            # UnifiedUsage's compatibility defaults are deliberately not reported.
            safe_raw = None
        safe_normalized = _safe_normalized_usage(normalized_usage)
        if safe_normalized is None and usage is not None:
            # ``UnifiedUsage`` has already normalized provider-specific nested
            # counters such as completion_tokens_details.reasoning_tokens and
            # DeepSeek's prompt_cache_hit_tokens. Prefer that normalized view
            # when applying columns; the flat raw allowlist intentionally does
            # not copy arbitrary nested response objects.
            safe_normalized = _safe_usage_payload(usage)
        async with self._session_scope() as db:
            attempt = await self._load(db, attempt_id)
            values = {
                "provider_request_id": _safe_identifier(provider_request_id),
                "http_status": _safe_http_status(http_status),
                "stop_reason": _safe_identifier(stop_reason),
                "provider_usage_json": json.dumps(
                    safe_raw, separators=(",", ":"), sort_keys=True
                )
                if safe_raw
                else None,
                "normalized_usage_json": json.dumps(
                    safe_normalized, separators=(",", ":"), sort_keys=True
                )
                if safe_normalized
                else None,
            }
            if attempt.status in TERMINAL_ATTEMPT_STATUSES:
                if all(
                    self._same(getattr(attempt, key), value)
                    for key, value in values.items()
                ):
                    return attempt
                raise AttemptConflictError(f"attempt {attempt_id} already terminal")
            attempt.status = "completed"
            attempt.completed_at = utc_now()
            for key, value in values.items():
                setattr(attempt, key, value)
            self._apply_usage(attempt, safe_raw, safe_normalized)
            work_unit = await db.get(
                ActivityInvocationWorkUnit,
                attempt.work_unit_id,
                with_for_update=True,
            )
            if work_unit is not None:
                invocation = await db.get(
                    ActivityInvocation,
                    work_unit.invocation_id,
                    with_for_update=True,
                )
                if invocation is not None:
                    await self._set_execution_phase(
                        db,
                        work_unit,
                        invocation,
                        "processing_response",
                    )
                    await append_lifecycle_event(
                        db,
                        session_id=invocation.session_id,
                        invocation_id=invocation.id,
                        work_unit_id=work_unit.id,
                        event_type="attempt_completed",
                        payload={
                            "status": "completed",
                            "attempt_status": "completed",
                            "phase": "processing_response",
                            "input_tokens": attempt.input_tokens,
                            "output_tokens": attempt.output_tokens,
                            "reasoning_tokens": attempt.reasoning_tokens,
                            "cached_input_tokens": attempt.cached_input_tokens,
                        },
                    )
            await db.flush()
            await self._finish_owned(db)
            return attempt

    async def fail(
        self,
        attempt_id: int,
        error: BaseException | str,
        *,
        error_category: str | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
        status: str = "failed",
        provider_request_id: str | None = None,
    ) -> ActivityModelAttempt:
        if status not in {"failed", "cancelled"}:
            raise ValueError("status must be failed or cancelled")
        category = _safe_error_category(error, error_category)
        message = _safe_error_message(category, status)
        async with self._session_scope() as db:
            attempt = await self._load(db, attempt_id)
            values = {
                "error_category": category,
                "error_message": message,
                "retryable": bool(retryable) if isinstance(retryable, bool) else None,
                "http_status": _safe_http_status(http_status),
                "provider_request_id": _safe_identifier(provider_request_id),
            }
            if attempt.status in TERMINAL_ATTEMPT_STATUSES:
                if attempt.status == status and all(
                    self._same(getattr(attempt, key), value)
                    for key, value in values.items()
                ):
                    return attempt
                raise AttemptConflictError(f"attempt {attempt_id} already terminal")
            attempt.status = status
            attempt.completed_at = utc_now()
            if status == "cancelled":
                attempt.error_category = category
            for key, value in values.items():
                setattr(attempt, key, value)
            work_unit = await db.get(
                ActivityInvocationWorkUnit,
                attempt.work_unit_id,
                with_for_update=True,
            )
            if work_unit is not None:
                invocation = await db.get(
                    ActivityInvocation,
                    work_unit.invocation_id,
                    with_for_update=True,
                )
                if invocation is not None:
                    await self._set_execution_phase(
                        db,
                        work_unit,
                        invocation,
                        "retry_wait" if retryable else "provider_error",
                    )
                    await append_lifecycle_event(
                        db,
                        session_id=invocation.session_id,
                        invocation_id=invocation.id,
                        work_unit_id=work_unit.id,
                        event_type="attempt_failed",
                        payload={
                            "status": status,
                            "attempt_status": status,
                            "phase": "retry_wait" if retryable else "provider_error",
                        },
                    )
            await db.flush()
            await self._finish_owned(db)
            return attempt

    @staticmethod
    def _apply_usage(
        attempt: ActivityModelAttempt,
        raw: dict[str, Any] | None,
        normalized: dict[str, Any] | None,
    ) -> None:
        if not raw and not normalized:
            return
        values = normalized or raw or {}
        aliases = {
            "input_tokens": ("input_tokens", "prompt_tokens"),
            "output_tokens": ("output_tokens", "completion_tokens"),
            "reasoning_tokens": ("reasoning_tokens",),
            "cached_input_tokens": (
                "cached_input_tokens",
                "cache_read_tokens",
                "cached_tokens",
            ),
        }
        for field, keys in aliases.items():
            number = next(
                (values.get(key) for key in keys if isinstance(values.get(key), int)),
                None,
            )
            if number is not None:
                setattr(attempt, field, number)
                setattr(attempt, f"{field}_availability", "reported")
                setattr(attempt, f"{field}_source", "provider")


__all__ = ["ATTEMPT_STATUSES", "AttemptConflictError", "AttemptService"]
