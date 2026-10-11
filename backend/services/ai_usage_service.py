"""Central provider-usage accounting for every AI request path.

Business result tables only describe the primary PR/Issue/Agent/Scan result and
therefore cannot account for summaries, label selection, compression, RAG, or
other auxiliary model calls.  This module writes one idempotent ledger row at
the common provider-success boundaries and exposes safe aggregate queries for
the dashboard.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

import httpx
from loguru import logger
from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.time_service import now_utc
from backend.models.ai_usage_models import AIUsageRecord
from backend.services.billing_context import get_billing_context

# Only these call kinds are part of the provider-usage ledger.  The explicit
# allow-list also ensures that a temporary record from an older deployment can
# never be displayed as a current usage record.
ACCOUNTED_CALL_KINDS = (
    "chat",
    "chat_stream",
    "context_compression",
    "embedding",
    "rerank",
)


@dataclass(frozen=True, slots=True)
class ProviderUsageCounters:
    """Only counters explicitly reported by a provider response."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None

    @property
    def usage_reported(self) -> bool:
        return any(
            value is not None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cached_input_tokens,
                self.cache_creation_tokens,
                self.reasoning_tokens,
            )
        )


@dataclass(frozen=True, slots=True)
class GlobalTokenTotals:
    input_tokens: int
    output_tokens: int
    recorded_calls: int
    unreported_usage_calls: int


def _valid_counter(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _mapping_candidates(value: dict[str, Any]):
    """Yield common provider usage envelopes without recursively scanning payloads."""

    yield value
    for key in ("usage", "token_usage", "tokens"):
        child = value.get(key)
        if isinstance(child, dict):
            yield child
    meta = value.get("meta")
    if isinstance(meta, dict):
        yield meta
        for key in ("usage", "token_usage", "tokens"):
            child = meta.get(key)
            if isinstance(child, dict):
                yield child


def _counter_from_mapping(
    value: dict[str, Any],
    aliases: tuple[str, ...],
) -> int | None:
    merged = dict(value)
    for detail_key in (
        "input_tokens_details",
        "output_tokens_details",
        "prompt_tokens_details",
        "completion_tokens_details",
    ):
        details = value.get(detail_key)
        if isinstance(details, dict):
            merged.update(details)
    for alias in aliases:
        parsed = _valid_counter(merged.get(alias))
        if parsed is not None:
            return parsed
    return None


def _counter_from_object(
    value: Any,
    canonical_name: str,
    aliases: tuple[str, ...],
) -> int | None:
    reported_fields = getattr(value, "reported_fields", None)
    if reported_fields is not None:
        if canonical_name not in reported_fields:
            return None
        return _valid_counter(getattr(value, canonical_name, None))

    for alias in aliases:
        parsed = _valid_counter(getattr(value, alias, None))
        if parsed is not None:
            return parsed

    for detail_name in (
        "input_tokens_details",
        "output_tokens_details",
        "prompt_tokens_details",
        "completion_tokens_details",
    ):
        details = getattr(value, detail_name, None)
        if details is None:
            continue
        for alias in aliases:
            parsed = _valid_counter(getattr(details, alias, None))
            if parsed is not None:
                return parsed
    return None


def extract_provider_usage(
    usage: Any,
    *,
    input_only: bool = False,
) -> ProviderUsageCounters:
    """Extract exact provider counters while preserving missing-vs-zero.

    ``cached_input_tokens`` and ``reasoning_tokens`` are diagnostic dimensions;
    they are not added to input/output totals because providers normally include
    them in those parent counters already.
    """

    if usage is None:
        return ProviderUsageCounters()

    input_aliases = ("input_tokens", "prompt_tokens")
    output_aliases = ("output_tokens", "completion_tokens")
    cache_read_aliases = (
        "cache_read_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cached_tokens",
        "prompt_cache_hit_tokens",
    )
    cache_creation_aliases = (
        "cache_creation_tokens",
        "cache_creation_input_tokens",
    )
    reasoning_aliases = ("reasoning_tokens",)

    if isinstance(usage, dict):
        candidates = list(_mapping_candidates(usage))

        def from_candidates(aliases: tuple[str, ...]) -> int | None:
            for candidate in candidates:
                parsed = _counter_from_mapping(candidate, aliases)
                if parsed is not None:
                    return parsed
            return None

        input_tokens = from_candidates(input_aliases)
        if input_tokens is None and input_only:
            input_tokens = from_candidates(("total_tokens",))
        return ProviderUsageCounters(
            input_tokens=input_tokens,
            output_tokens=None if input_only else from_candidates(output_aliases),
            cached_input_tokens=from_candidates(cache_read_aliases),
            cache_creation_tokens=from_candidates(cache_creation_aliases),
            reasoning_tokens=from_candidates(reasoning_aliases),
        )

    input_tokens = _counter_from_object(usage, "input_tokens", input_aliases)
    if input_tokens is None and input_only:
        input_tokens = _counter_from_object(usage, "input_tokens", ("total_tokens",))
    return ProviderUsageCounters(
        input_tokens=input_tokens,
        output_tokens=(
            None
            if input_only
            else _counter_from_object(usage, "output_tokens", output_aliases)
        ),
        cached_input_tokens=_counter_from_object(
            usage,
            "cache_read_tokens",
            cache_read_aliases,
        ),
        cache_creation_tokens=_counter_from_object(
            usage,
            "cache_creation_tokens",
            cache_creation_aliases,
        ),
        reasoning_tokens=_counter_from_object(
            usage,
            "reasoning_tokens",
            reasoning_aliases,
        ),
    )


def build_usage_record_key(call_kind: str, logical_call_id: str) -> str:
    raw = f"{call_kind}:{logical_call_id}"
    if len(raw) <= 191:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{raw[:120]}:{digest}"


def _safe_identifier(value: Any, *, fallback: str, limit: int) -> str:
    normalized = str(value or fallback).strip() or fallback
    return normalized[:limit]


async def _insert_record(db: AsyncSession, record: AIUsageRecord) -> bool:
    try:
        async with db.begin_nested():
            db.add(record)
            await db.flush()
        return True
    except IntegrityError:
        # record_key is an idempotency key.  A duplicate means another worker
        # already durably accounted for the same logical call.  Re-read with a
        # current/locking read so unrelated constraint errors are not swallowed.
        existing = (
            await db.execute(
                select(AIUsageRecord.id)
                .where(AIUsageRecord.record_key == record.record_key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False
        raise


async def record_ai_usage(
    *,
    record_key: str,
    call_kind: str,
    role: str,
    provider_id: str,
    account_id: str | None = None,
    model_id: str,
    protocol_family: str,
    usage: Any = None,
    input_only: bool = False,
    occurred_at: datetime | None = None,
    session_factory: Any = None,
    actual_call_id: str | None = None,
    logical_call_id: str | None = None,
    outcome: str = "completed",
    usage_complete: bool = True,
) -> bool:
    """Persist one idempotent AI usage row.

    Returns ``False`` when the database is not initialized or the record was
    already present.  Operational failures are intentionally left to the
    best-effort wrapper so tests and administrative jobs can opt into strict
    behavior.
    """

    if session_factory is None:
        from backend.models import database as database_module

        session_factory = database_module.async_session
    if session_factory is None:
        context = get_billing_context()
        if context is not None and context.user_id is not None:
            raise RuntimeError("Billing database is unavailable")
        return False

    counters = extract_provider_usage(usage, input_only=input_only)
    context = get_billing_context()
    record = AIUsageRecord(
        record_key=_safe_identifier(record_key, fallback="unknown", limit=191),
        call_kind=_safe_identifier(call_kind, fallback="unknown", limit=32),
        role=_safe_identifier(role, fallback="unknown", limit=64),
        provider_id=_safe_identifier(provider_id, fallback="unknown", limit=128),
        account_id=account_id,
        model_id=_safe_identifier(model_id, fallback="unknown", limit=255),
        protocol_family=_safe_identifier(
            protocol_family,
            fallback="unknown",
            limit=64,
        ),
        user_id=context.user_id if context else None,
        operation_id=context.operation_id if context else None,
        feature=context.feature if context else None,
        source=dict(context.source) if context else None,
        platform_reason=context.platform_reason
        if context
        else "unattributed_legacy_observation",
        actual_call_id=actual_call_id,
        logical_call_id=logical_call_id,
        raw_usage=safe_usage_snapshot(usage),
        usage_semantics=usage_semantics(protocol_family, call_kind),
        billing_units={
            **normalized_billing_units(counters, protocol_family),
            **extract_reported_billing_units(
                usage,
                actual_response=actual_call_id is not None
                and usage_complete
                and (outcome == "completed" or usage is not None),
            ),
        },
        outcome=outcome,
        input_tokens=counters.input_tokens,
        output_tokens=counters.output_tokens,
        cached_input_tokens=counters.cached_input_tokens,
        cache_creation_tokens=counters.cache_creation_tokens,
        reasoning_tokens=counters.reasoning_tokens,
        usage_reported=counters.usage_reported,
        usage_complete=usage_complete,
        occurred_at=occurred_at or now_utc(),
    )

    async with session_factory() as db:
        inserted = await _insert_record(db, record)
        await db.commit()
        return inserted


async def record_ai_usage_best_effort(**kwargs: Any) -> bool:
    """Record usage without putting observability on the AI request critical path."""

    context = get_billing_context()
    if context is not None and context.user_id is not None:
        return await record_ai_usage(**kwargs)
    try:
        return await record_ai_usage(**kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.bind(error_type=type(exc).__name__).warning(
            "AI usage 账本写入失败，业务调用继续: error_type={}",
            type(exc).__name__,
        )
        return False


async def record_unified_ai_usage_best_effort(
    *,
    logical_call_id: str,
    call_kind: str,
    role: str,
    candidate: Any,
    usage: Any,
) -> bool:
    provider = getattr(candidate, "provider", None)
    model = getattr(candidate, "model", None)
    family = getattr(candidate, "effective_protocol", None)
    if family in (None, ""):
        family = getattr(candidate, "protocol_family", None)
    if family in (None, ""):
        family = getattr(provider, "family", "unknown")
    return await record_ai_usage_best_effort(
        record_key=build_usage_record_key(call_kind, logical_call_id),
        call_kind=call_kind,
        role=role,
        provider_id=getattr(provider, "id", "unknown"),
        account_id=getattr(candidate, "account_id", None),
        model_id=getattr(model, "model_id", "unknown"),
        protocol_family=getattr(family, "value", family),
        usage=usage,
    )


async def fetch_global_token_totals(db: AsyncSession) -> GlobalTokenTotals:
    """Return global Token totals exclusively from the provider-usage ledger."""

    accounted_record = AIUsageRecord.call_kind.in_(ACCOUNTED_CALL_KINDS)
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(AIUsageRecord.input_tokens), 0).label(
                    "input_tokens"
                ),
                func.coalesce(func.sum(AIUsageRecord.output_tokens), 0).label(
                    "output_tokens"
                ),
                func.coalesce(func.sum(case((accounted_record, 1), else_=0)), 0).label(
                    "recorded_calls"
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    accounted_record,
                                    AIUsageRecord.usage_reported.is_(False),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("unreported_usage_calls"),
            ).where(accounted_record)
        )
    ).one()
    return GlobalTokenTotals(
        input_tokens=int(row.input_tokens or 0),
        output_tokens=int(row.output_tokens or 0),
        recorded_calls=int(row.recorded_calls or 0),
        unreported_usage_calls=int(row.unreported_usage_calls or 0),
    )


__all__ = [
    "ACCOUNTED_CALL_KINDS",
    "GlobalTokenTotals",
    "ProviderUsageCounters",
    "build_usage_record_key",
    "extract_provider_usage",
    "fetch_global_token_totals",
    "record_ai_usage",
    "record_ai_usage_best_effort",
    "record_unified_ai_usage_best_effort",
]


def usage_semantics(protocol_family: str, call_kind: str = "chat") -> dict[str, Any]:
    """Capture adapter protocol rules instead of assuming all cache counters agree."""
    family = str(protocol_family).lower()
    if not any(
        name in family for name in ("anthropic", "openai", "gemini", "siliconflow")
    ):
        return {}
    return {
        "input_includes_cache_read": "anthropic" not in family,
        "input_includes_cache_creation": "anthropic" not in family,
        "output_includes_reasoning": "gemini" not in family,
        "normalization_version": "1",
        **({"cache_creation_supported": False} if "anthropic" not in family else {}),
        **(
            {"cache_read_supported": False}
            if call_kind in {"embedding", "rerank"}
            else {}
        ),
    }


def normalized_billing_units(
    counters: ProviderUsageCounters, protocol_family: str
) -> dict[str, int | None]:
    # Token dimensions are retained verbatim. Pricing uses usage_semantics to
    # split inclusive subsets; no guessed count is promoted to provider usage.
    return {
        "input_tokens": counters.input_tokens,
        "output_tokens": counters.output_tokens,
        "cached_input_tokens": counters.cached_input_tokens,
        "cache_creation_tokens": counters.cache_creation_tokens,
        "reasoning_tokens": counters.reasoning_tokens,
    }


def extract_reported_billing_units(
    usage: Any, *, actual_response: bool
) -> dict[str, int]:
    """Read exact provider-native non-token meters; count a received request.

    The request count is our measured successful response boundary, not a token
    estimate. Unknown external outcomes have no fabricated successful request.
    """
    if not isinstance(usage, dict):
        dump = getattr(usage, "model_dump", None)
        usage = (
            dump()
            if callable(dump)
            else {key: getattr(usage, key, None) for key in ("meta", "billed_units")}
        )
    units: dict[str, int] = {"requests": 1} if actual_response else {}
    envelopes = list(_mapping_candidates(usage))
    for envelope in envelopes:
        billed = envelope.get("billed_units")
        if isinstance(billed, dict):
            for name in ("requests", "documents", "search_units"):
                parsed = _valid_counter(billed.get(name))
                if parsed is not None:
                    units[name] = parsed
    return units


def safe_usage_snapshot(usage: Any) -> dict[str, Any]:
    """Retain reported integer counters only; never copy a response body."""
    if usage is None:
        return {}
    raw_usage = getattr(usage, "raw_usage", None)
    if isinstance(raw_usage, dict) and raw_usage:
        return safe_usage_snapshot(raw_usage)
    if not isinstance(usage, dict):
        dump = getattr(usage, "model_dump", None)
        if callable(dump):
            usage = dump()
        else:
            usage = {
                key: getattr(usage, key, None)
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_creation_tokens",
                    "reasoning_tokens",
                    "details",
                )
                if key in getattr(usage, "reported_fields", ()) or key == "details"
            }
    allowed = {
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_read_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cache_creation_tokens",
        "reasoning_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "input_tokens_details",
        "output_tokens_details",
        "prompt_tokens_details",
        "completion_tokens_details",
        "usage",
        "token_usage",
        "tokens",
        "meta",
        "details",
        "billed_units",
        "promptTokenCount",
        "candidatesTokenCount",
        "cachedContentTokenCount",
        "thoughtsTokenCount",
        "totalTokenCount",
        "search_units",
        "documents",
        "requests",
    }
    result = {}
    for key, value in usage.items():
        if key not in allowed:
            continue
        if isinstance(value, dict):
            result[key] = safe_usage_snapshot(value)
        elif _valid_counter(value) is not None:
            result[key] = value
    return result


def _http_failure_usage(exc: BaseException | None, *, input_only: bool) -> dict | None:
    """Extract only reported meters from an already received SDK/HTTP error.

    OpenAI APIStatusError keeps usage in body/response rather than ``exc.usage``.
    Rerank HTTPStatusError likewise exposes its received response. No response
    text or arbitrary error fields enter the financial evidence snapshot, and an
    absent meter remains unknown instead of manufacturing a successful request.
    """
    if exc is None:
        return None
    candidates = []
    response = getattr(exc, "response", None)
    if isinstance(response, httpx.Response):
        try:
            candidates.append(response.json())
        except ValueError, httpx.ResponseNotRead:
            pass
    body = getattr(exc, "body", None)
    if isinstance(body, dict) and any(
        name in body
        for name in ("usage", "token_usage", "tokens", "meta", "billed_units")
    ):
        # The SDK can reduce ``body`` to the error object rather than the full
        # response. A numeric field in that error is not reported Usage.
        candidates.append(body)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        snapshot = safe_usage_snapshot(candidate)
        if extract_provider_usage(snapshot, input_only=input_only).usage_reported or (
            extract_reported_billing_units(snapshot, actual_response=False)
        ):
            return snapshot
    return None


async def admit_billing_operation(*, session_factory: Any = None) -> bool:
    """Persist trusted user admission before waiting for service execution capacity.

    Registration checks per-user limits and reserves according to the existing
    policy. It creates neither an AI attempt nor a consumption charge.
    """
    from backend.services.billing_context import (
        get_pending_billing_failure,
        has_registered_operation,
        mark_billing_failure,
        mark_registered_operation,
    )

    pending_failure = get_pending_billing_failure()
    if pending_failure is not None:
        raise pending_failure
    context = get_billing_context()
    if context is None:
        return False
    if has_registered_operation(context.operation_id):
        return True
    if session_factory is None:
        from backend.models import database as database_module

        session_factory = database_module.async_session
    try:
        if session_factory is None:
            if context.user_id is not None:
                raise RuntimeError("Billing database is unavailable")
            return False
        from backend.services.billing_service import BillingService

        async with session_factory() as db:
            await BillingService(db).register_operation(
                user_id=context.user_id,
                operation_id=context.operation_id,
                feature=context.feature,
                source=dict(context.source),
                platform_reason=context.platform_reason,
            )
            await db.commit()
    except BaseException as exc:
        mark_billing_failure(exc)
        raise
    mark_registered_operation(context.operation_id)
    return True


async def refresh_billing_admission_liveness(*, session_factory: Any = None) -> bool:
    """Protect an owned worker's handoff to settlement without changing amounts."""
    from datetime import timedelta

    from backend.services.billing_context import has_registered_operation
    from backend.services.billing_service import BillingError, BillingService

    context = get_billing_context()
    if context is None or not has_registered_operation(context.operation_id):
        return False
    if session_factory is None:
        from backend.models import database as database_module

        session_factory = database_module.async_session
    if session_factory is None:
        raise RuntimeError("Billing database is unavailable")
    async with session_factory() as db:
        service = BillingService(db)
        operation = await service._operation(context.operation_id)
        if operation.user_id != context.user_id or operation.feature != context.feature:
            raise BillingError("Operation attribution conflict")
        if operation.outcome is not None:
            return False
        ttl = int(await service._setting("billing_reservation_ttl_seconds") or 3600)
        operation.expires_at = now_utc() + timedelta(seconds=ttl)
        await db.commit()
    return True


async def begin_ai_call(
    *,
    call_id: str,
    logical_call_id: str,
    provider_id: str,
    account_id: str | None = None,
    model_id: str,
    call_kind: str,
    protocol_family: str,
    session_factory: Any = None,
) -> bool:
    """Commit an attempt and its frozen price BEFORE sending an external request.

    A crash after this commit has an explicit unknown-result reconciliation row.
    A financial write failure prevents sending rather than becoming free usage.
    """
    from backend.services.billing_context import get_pending_billing_failure

    pending_failure = get_pending_billing_failure()
    if pending_failure is not None:
        raise pending_failure
    context = get_billing_context()
    if context is None:
        return False
    if session_factory is None:
        from backend.models import database as database_module

        session_factory = database_module.async_session
    if session_factory is None:
        if context.user_id is not None:
            raise RuntimeError("Billing database is unavailable")
        return False
    from backend.services.billing_service import BillingService

    async with session_factory() as db:
        service = BillingService(db)
        await service.register_operation(
            user_id=context.user_id,
            operation_id=context.operation_id,
            feature=context.feature,
            source=dict(context.source),
            platform_reason=context.platform_reason,
        )
        await service.start_call(
            context.operation_id,
            call_id,
            provider_id,
            model_id,
            call_kind,
            logical_call_id=logical_call_id,
            protocol_family=protocol_family,
            account_id=account_id,
        )
        await db.commit()
    from backend.services.billing_context import mark_registered_operation

    mark_registered_operation(context.operation_id)
    return True


async def finish_ai_call(
    *,
    call_id: str,
    logical_call_id: str,
    provider_id: str,
    account_id: str | None = None,
    model_id: str,
    call_kind: str,
    protocol_family: str,
    role: str,
    usage: Any,
    outcome: str = "completed",
    input_only: bool = False,
    usage_complete: bool = True,
    session_factory: Any = None,
) -> bool:
    """Atomically persist actual usage with the attempt's recoverable result state."""
    context = get_billing_context()
    if context is None:
        return await record_ai_usage_best_effort(
            record_key=build_usage_record_key(call_kind, call_id),
            call_kind=call_kind,
            role=role,
            provider_id=provider_id,
            account_id=account_id,
            model_id=model_id,
            protocol_family=protocol_family,
            usage=usage,
            input_only=input_only,
            actual_call_id=call_id,
            logical_call_id=logical_call_id,
            outcome=outcome,
            usage_complete=usage_complete,
            session_factory=session_factory,
        )
    if session_factory is None:
        from backend.models import database as database_module

        session_factory = database_module.async_session
    if session_factory is None:
        if context.user_id is not None:
            raise RuntimeError("Billing database is unavailable")
        return False
    from backend.models.billing_models import BillingCallAttempt
    from backend.services.billing_service import BillingService

    counters = extract_provider_usage(usage, input_only=input_only)
    key = build_usage_record_key(call_kind, call_id)
    record = AIUsageRecord(
        record_key=key,
        actual_call_id=call_id,
        logical_call_id=logical_call_id,
        call_kind=call_kind,
        role=role,
        provider_id=provider_id,
        account_id=account_id,
        model_id=model_id,
        protocol_family=protocol_family,
        user_id=context.user_id,
        operation_id=context.operation_id,
        feature=context.feature,
        source=dict(context.source),
        platform_reason=context.platform_reason,
        input_tokens=counters.input_tokens,
        output_tokens=counters.output_tokens,
        cached_input_tokens=counters.cached_input_tokens,
        cache_creation_tokens=counters.cache_creation_tokens,
        reasoning_tokens=counters.reasoning_tokens,
        usage_reported=counters.usage_reported,
        usage_complete=usage_complete,
        raw_usage=safe_usage_snapshot(usage),
        usage_semantics=usage_semantics(protocol_family, call_kind),
        billing_units={
            **normalized_billing_units(counters, protocol_family),
            **extract_reported_billing_units(
                usage,
                actual_response=usage_complete
                and (outcome == "completed" or usage is not None),
            ),
        },
        outcome=outcome,
        occurred_at=now_utc(),
    )
    async with session_factory() as db:
        service = BillingService(db)
        # Match settlement/reconciliation lock ordering: wallet -> operation ->
        # actual attempt. No lock is held while the external request runs.
        await service._operation(context.operation_id)
        attempt = (
            await db.execute(
                select(BillingCallAttempt)
                .where(BillingCallAttempt.call_id == call_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if attempt is None or attempt.operation_id != context.operation_id:
            raise RuntimeError(
                "Billing call attempt is missing or has a different owner"
            )
        if (
            attempt.provider_id != provider_id
            or attempt.account_id != account_id
            or attempt.model_id != model_id
            or attempt.call_kind != call_kind
        ):
            raise RuntimeError("Billing attempt routing does not match its result")
        inserted = await _insert_record(db, record)
        if not inserted:
            existing = (
                await db.execute(
                    select(AIUsageRecord).where(AIUsageRecord.record_key == key)
                )
            ).scalar_one()
            immutable_fields = (
                "user_id",
                "operation_id",
                "feature",
                "provider_id",
                "account_id",
                "model_id",
                "call_kind",
                "protocol_family",
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "cache_creation_tokens",
                "reasoning_tokens",
                "usage_reported",
                "usage_complete",
                "outcome",
                "billing_units",
            )
            if any(
                getattr(existing, field) != getattr(record, field)
                for field in immutable_fields
            ):
                raise RuntimeError(
                    "Repeated usage result changed immutable financial intent"
                )
        # Duplicate delivery must never downgrade a resolved attempt.
        if inserted or attempt.usage_record_key is None:
            attempt.usage_record_key = key
            attempt.state = (
                "usage_known"
                if usage_complete
                and (
                    counters.usage_reported
                    or any(
                        record.billing_units.get(name) is not None
                        for name in ("requests", "documents", "search_units")
                    )
                )
                else "pending_usage"
                if outcome == "completed" and usage_complete
                else "pending_reconciliation"
            )
            attempt.updated_at = now_utc()
        await service.settle_operation(context.operation_id)
        await db.commit()
        return inserted


async def finish_billing_operation(status: str, *, session_factory: Any = None) -> None:
    from backend.services.billing_context import (
        get_pending_billing_failure,
        mark_billing_outcome,
    )

    pending_failure = get_pending_billing_failure()
    if pending_failure is not None:
        status = "failed"

    mark_billing_outcome(status)
    context = get_billing_context()
    from backend.services.billing_context import has_registered_operation

    if context is None or not has_registered_operation(context.operation_id):
        if pending_failure is not None:
            raise pending_failure
        return
    if session_factory is None:
        from backend.models import database as database_module

        session_factory = database_module.async_session
    if session_factory is None:
        if context.user_id is not None:
            raise RuntimeError("Billing database is unavailable")
        return
    from backend.services.billing_service import BillingService

    async with session_factory() as db:
        service = BillingService(db)
        await service.register_operation(
            user_id=context.user_id,
            operation_id=context.operation_id,
            feature=context.feature,
            source=dict(context.source),
            platform_reason=context.platform_reason,
        )
        await service.finish_operation(context.operation_id, status)
        await db.commit()
    if pending_failure is not None:
        raise pending_failure


class ProviderUsageMeter:
    """Durable boundary for a single actual external request (not a logical retry)."""

    def __init__(
        self,
        *,
        provider_id: str,
        account_id: str | None = None,
        model_id: str,
        protocol_family: str,
        call_kind: str,
        role: str,
        logical_call_id: str,
        input_only: bool = False,
    ):
        from uuid import uuid4

        self.call_id = str(uuid4())
        self.usage: Any = None
        self.usage_complete = True
        self.kwargs = {
            "call_id": self.call_id,
            "logical_call_id": logical_call_id,
            "provider_id": provider_id,
            "account_id": account_id,
            "model_id": model_id,
            "protocol_family": protocol_family,
            "call_kind": call_kind,
        }
        self.role = role
        self.input_only = input_only
        self._owned_context = None

    @classmethod
    def for_candidate(
        cls, candidate: Any, *, call_kind: str, role: str, logical_call_id: str
    ) -> ProviderUsageMeter:
        family = getattr(candidate, "effective_protocol", None) or getattr(
            candidate.provider, "family", "unknown"
        )
        return cls(
            provider_id=candidate.provider.id,
            account_id=getattr(candidate, "account_id", None),
            model_id=candidate.model.model_id,
            protocol_family=getattr(family, "value", family),
            call_kind=call_kind,
            role=role,
            logical_call_id=logical_call_id,
        )

    async def __aenter__(self) -> Self:
        if get_billing_context() is None:
            from uuid import uuid4

            from backend.services.billing_context import (
                BillingContext,
                bind_billing_context,
            )

            self._owned_context = bind_billing_context(
                BillingContext(
                    user_id=None,
                    operation_id=str(uuid4()),
                    feature="system_task",
                    source={"role": self.role},
                    platform_reason="unscoped_internal_task",
                )
            )
            self._owned_context.__enter__()
        try:
            await begin_ai_call(**self.kwargs)
        except Exception as exc:
            exc.billing_failure = True
            from backend.services.billing_context import mark_billing_failure

            mark_billing_failure(exc)
            if self._owned_context is not None:
                self._owned_context.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    async def __aexit__(
        self, exc_type: object, exc: BaseException | None, tb: object
    ) -> None:
        if exc is not None:
            exception_usage = getattr(exc, "usage", None)
            if exception_usage is None:
                exception_usage = getattr(
                    getattr(exc, "__cause__", None), "usage", None
                )
            if exception_usage is None:
                exception_usage = _http_failure_usage(exc, input_only=self.input_only)
            if exception_usage is None:
                exception_usage = _http_failure_usage(
                    getattr(exc, "__cause__", None), input_only=self.input_only
                )
            if exception_usage is not None:
                self.usage = (
                    self.usage.merge_snapshot(exception_usage)
                    if self.usage is not None and hasattr(self.usage, "merge_snapshot")
                    else exception_usage
                )
                self.usage_complete = bool(getattr(exc, "usage_complete", True))
        from backend.core.ai_protocol.errors import ReviewCancelledError

        outcome = (
            "completed"
            if exc is None
            else (
                "cancelled"
                if isinstance(
                    exc, (asyncio.CancelledError, GeneratorExit, ReviewCancelledError)
                )
                else "failed"
            )
        )
        try:
            await finish_ai_call(
                **self.kwargs,
                role=self.role,
                usage=self.usage,
                outcome=outcome,
                input_only=self.input_only,
                usage_complete=self.usage_complete,
            )
            if self._owned_context is not None:
                await finish_billing_operation(outcome)
        except Exception as write_error:
            write_error.billing_failure = True
            from backend.services.billing_context import mark_billing_failure

            mark_billing_failure(write_error)
            raise
        finally:
            if self._owned_context is not None:
                self._owned_context.__exit__(exc_type, exc, tb)


async def metered_stream(
    events: Any, *, candidate: Any, role: str, logical_call_id: str
):
    """Keep partial/final reported usage on stream cancellation and timeouts."""
    async with ProviderUsageMeter.for_candidate(
        candidate, call_kind="chat_stream", role=role, logical_call_id=logical_call_id
    ) as meter:
        meter.usage_complete = False
        try:
            async for event in events:
                usage = getattr(event, "usage", None)
                if usage is not None and extract_provider_usage(usage).usage_reported:
                    # Adapter usage events are snapshots, not token deltas.
                    if meter.usage is not None and hasattr(
                        meter.usage, "merge_snapshot"
                    ):
                        meter.usage = meter.usage.merge_snapshot(usage)
                    else:
                        meter.usage = usage
                if getattr(event, "type", None) == "done":
                    meter.usage_complete = True
                yield event
        finally:
            close = getattr(events, "aclose", None)
            if callable(close):
                await close()


def is_billing_failure(exc: BaseException) -> bool:
    return bool(getattr(exc, "billing_failure", False))


async def fetch_usage_summary(
    db: AsyncSession,
    *,
    user_id: int,
    operation_id: str | None = None,
    feature: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Aggregate only one authorized payer's usage, including unknown counts.

    The caller resolves user_id from authentication. SQL SUM preserves NULL
    when every report lacks that dimension; no missing usage becomes exact zero.
    """
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("Usage aggregation requires a trusted user id")
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("Invalid usage aggregation pagination")
    dimensions = (
        AIUsageRecord.operation_id,
        AIUsageRecord.feature,
        AIUsageRecord.provider_id,
        AIUsageRecord.model_id,
        AIUsageRecord.call_kind,
    )
    filters = [AIUsageRecord.user_id == user_id]
    if operation_id is not None:
        filters.append(AIUsageRecord.operation_id == operation_id)
    if feature is not None:
        filters.append(AIUsageRecord.feature == feature)
    statement = (
        select(
            *dimensions,
            func.count(AIUsageRecord.id).label("calls"),
            func.sum(AIUsageRecord.input_tokens).label("input_tokens"),
            func.sum(AIUsageRecord.output_tokens).label("output_tokens"),
            func.sum(AIUsageRecord.cached_input_tokens).label("cached_input_tokens"),
            func.sum(AIUsageRecord.cache_creation_tokens).label(
                "cache_creation_tokens"
            ),
            func.sum(AIUsageRecord.reasoning_tokens).label("reasoning_tokens"),
            func.sum(case((AIUsageRecord.usage_reported.is_(False), 1), else_=0)).label(
                "unknown_calls"
            ),
            func.sum(case((AIUsageRecord.usage_complete.is_(False), 1), else_=0)).label(
                "incomplete_calls"
            ),
            func.max(AIUsageRecord.occurred_at).label("last_occurred_at"),
        )
        .where(*filters)
        .group_by(*dimensions)
        .order_by(
            func.max(AIUsageRecord.occurred_at).desc(),
            AIUsageRecord.operation_id,
            AIUsageRecord.provider_id,
            AIUsageRecord.model_id,
            AIUsageRecord.call_kind,
        )
        .offset(offset)
        .limit(limit)
    )
    return [dict(row) for row in (await db.execute(statement)).mappings().all()]
