"""Audited append-only resolution of an unknown upstream result.

No upstream request is reissued here. Operators supply provider invoice/Usage
evidence; historical quoted records require a separate financial adjustment.
"""

import hashlib
import json

from sqlalchemy import select

from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import (
    BillingCallAttempt,
    BillingPriceProfile,
    BillingReconciliationEvent,
    BillingUsageCharge,
)
from backend.models.telegram_models import TelegramUser
from backend.services.ai_usage_service import (
    extract_provider_usage,
    normalized_billing_units,
    safe_usage_snapshot,
    usage_semantics,
)
from backend.services.billing_price_identity import canonical_price_call_kind
from backend.services.billing_pricing import calculate_price
from backend.services.billing_service import BillingError, BillingService


async def resolve_billing_call(
    session,
    *,
    call_id,
    event_key,
    actor_id,
    reason,
    usage,
    outcome="completed",
    price_profile_id=None,
    billing_units=None,
):
    actor = await session.get(TelegramUser, actor_id)
    if actor is None or not actor.is_active or actor.role != "super_admin":
        raise BillingError(
            "Reconciliation requires an active super administrator", "forbidden"
        )
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or outcome not in {"completed", "failed", "cancelled"}
    ):
        raise BillingError("Reconciliation requires evidence and a known outcome")
    if not event_key or len(event_key) > 191:
        raise BillingError("Invalid reconciliation key")
    snapshot = {
        "usage": safe_usage_snapshot(usage),
        "outcome": outcome,
        "price_profile_id": price_profile_id,
        "billing_units": billing_units or {},
    }
    previous = (
        await session.execute(
            select(BillingReconciliationEvent)
            .where(BillingReconciliationEvent.event_key == event_key)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if previous:
        if (
            previous.call_id != call_id
            or previous.evidence.get("request", previous.evidence) != snapshot
        ):
            raise BillingError("Conflicting reconciliation evidence")
        return previous
    attempt = await session.get(BillingCallAttempt, call_id)
    if attempt is None:
        raise BillingError("Unknown call attempt")
    service = BillingService(session)
    operation = await service._operation(attempt.operation_id)
    attempt = (
        await session.execute(
            select(BillingCallAttempt)
            .where(BillingCallAttempt.call_id == call_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    frozen = (
        await session.execute(
            select(BillingUsageCharge.id)
            .where(BillingUsageCharge.usage_record_key == attempt.usage_record_key)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if frozen is not None:
        raise BillingError(
            "Quoted Usage is immutable; append a referenced financial adjustment"
        )
    original_profile_id = attempt.price_profile_id
    # An unquoted pending request can receive an explicitly reviewed correction;
    # never select today's tariff automatically or rewrite a frozen fee above.
    profile_id = price_profile_id or original_profile_id
    profile = await session.get(BillingPriceProfile, profile_id) if profile_id else None
    if (
        profile is None
        or (
            profile.provider_id,
            profile.account_id,
            profile.model_id,
            canonical_price_call_kind(profile.call_kind),
        )
        != (
            attempt.provider_id,
            attempt.account_id,
            attempt.model_id,
            canonical_price_call_kind(attempt.call_kind),
        )
        or (profile.call_kind == "chat_stream" and profile.id != original_profile_id)
    ):
        raise BillingError(
            "Reconciliation requires an exact matching reviewed price version"
        )
    counters = extract_provider_usage(
        usage, input_only=attempt.call_kind in {"embedding", "rerank"}
    )
    units = normalized_billing_units(counters, attempt.protocol_family)
    if billing_units:
        if any(
            key not in {"requests", "documents", "search_units"}
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in billing_units.items()
        ):
            raise BillingError("Invalid reviewed usage units")
        units.update(billing_units)
    record_key = (
        "reconciled:" + hashlib.sha256(f"{call_id}:{event_key}".encode()).hexdigest()
    )
    record = AIUsageRecord(
        record_key=record_key,
        actual_call_id=None,
        logical_call_id=attempt.logical_call_id,
        operation_id=operation.operation_id,
        user_id=operation.user_id,
        feature=operation.feature,
        source=operation.source,
        platform_reason=operation.platform_reason,
        provider_id=attempt.provider_id,
        account_id=attempt.account_id,
        model_id=attempt.model_id,
        call_kind=attempt.call_kind,
        role="reconciliation",
        protocol_family=attempt.protocol_family or "unknown",
        input_tokens=counters.input_tokens,
        output_tokens=counters.output_tokens,
        cached_input_tokens=counters.cached_input_tokens,
        cache_creation_tokens=counters.cache_creation_tokens,
        reasoning_tokens=counters.reasoning_tokens,
        usage_reported=counters.usage_reported or bool(billing_units),
        usage_complete=True,
        raw_usage=snapshot["usage"],
        usage_semantics=usage_semantics(
            attempt.protocol_family or "unknown", attempt.call_kind
        ),
        billing_units=units,
        outcome=outcome,
    )
    event = BillingReconciliationEvent(
        call_id=call_id,
        event_key=event_key,
        actor_id=actor_id,
        reason=reason.strip(),
        evidence=json.loads(
            json.dumps(
                {
                    "request": snapshot,
                    "account_id": attempt.account_id,
                    "original_price_profile_id": original_profile_id,
                    "resolved_price_profile_id": profile_id,
                }
            )
        ),
    )
    calculate_price(record, profile.config)
    session.add(record)
    session.add(event)
    attempt.usage_record_key = record_key
    attempt.state = "usage_known"
    attempt.price_profile_id = profile_id
    await session.flush()
    await service.settle_operation(operation.operation_id)
    return event
