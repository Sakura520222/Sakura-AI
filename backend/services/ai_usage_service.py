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
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.ai_usage_models import AIUsageRecord

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
        "cached_tokens",
        "prompt_cache_hit_tokens",
    )
    cache_creation_aliases = (
        "cache_creation_tokens",
        "prompt_cache_miss_tokens",
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
    model_id: str,
    protocol_family: str,
    usage: Any = None,
    input_only: bool = False,
    occurred_at: datetime | None = None,
    session_factory: Any = None,
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
        return False

    counters = extract_provider_usage(usage, input_only=input_only)
    record = AIUsageRecord(
        record_key=_safe_identifier(record_key, fallback="unknown", limit=191),
        call_kind=_safe_identifier(call_kind, fallback="unknown", limit=32),
        role=_safe_identifier(role, fallback="unknown", limit=64),
        provider_id=_safe_identifier(provider_id, fallback="unknown", limit=128),
        model_id=_safe_identifier(model_id, fallback="unknown", limit=255),
        protocol_family=_safe_identifier(
            protocol_family,
            fallback="unknown",
            limit=64,
        ),
        input_tokens=counters.input_tokens,
        output_tokens=counters.output_tokens,
        cached_input_tokens=counters.cached_input_tokens,
        cache_creation_tokens=counters.cache_creation_tokens,
        reasoning_tokens=counters.reasoning_tokens,
        usage_reported=counters.usage_reported,
        occurred_at=occurred_at or datetime.now(UTC),
    )

    async with session_factory() as db:
        inserted = await _insert_record(db, record)
        await db.commit()
        return inserted


async def record_ai_usage_best_effort(**kwargs: Any) -> bool:
    """Record usage without putting observability on the AI request critical path."""

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
