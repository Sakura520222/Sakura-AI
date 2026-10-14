"""Validate reviewed Usage links before suppressing retained audit observations."""

import hashlib

from sqlalchemy import and_, or_, select

from backend.models.ai_usage_models import AIUsageRecord
from backend.models.billing_models import BillingCallAttempt, BillingReconciliationEvent


def late_reference(record):
    if record.role != "late_actual" or record.actual_call_id is not None:
        return None
    ref = (
        record.source.get("billing_late_result")
        if isinstance(record.source, dict)
        else None
    )
    if not isinstance(ref, dict) or not isinstance(ref.get("call_id"), str):
        return None
    if type(ref.get("reconciliation_event_id")) is not int or not isinstance(
        ref.get("selected_usage_record_key"), str
    ):
        return None
    if ref.get("original_usage_record_key") is not None and not isinstance(
        ref["original_usage_record_key"], str
    ):
        return None
    return ref


def reviewed_shadow(record, attempt, selected, events, originals, *, user_id):
    """A label alone cannot hide actual usage; validate the complete audit chain."""
    if (
        attempt is None
        or selected is None
        or selected.role != "reconciliation"
        or selected.actual_call_id is not None
    ):
        return False
    if (
        attempt.usage_record_key != selected.record_key
        or record.record_key == selected.record_key
    ):
        return False
    if (
        record.user_id != user_id
        or selected.user_id != user_id
        or record.feature != selected.feature
    ):
        return False
    for field in ("operation_id", "provider_id", "account_id", "model_id", "call_kind"):
        if getattr(record, field) != getattr(selected, field) or getattr(
            record, field
        ) != getattr(attempt, field):
            return False
    matching = [
        event
        for event in events
        if event.call_id == attempt.call_id
        and selected.record_key
        == "reconciled:"
        + hashlib.sha256(f"{attempt.call_id}:{event.event_key}".encode()).hexdigest()
    ]
    if record.actual_call_id is not None:
        return bool(matching) and record.actual_call_id == attempt.call_id
    ref = late_reference(record)
    if (
        ref is None
        or ref["call_id"] != attempt.call_id
        or ref["selected_usage_record_key"] != selected.record_key
    ):
        return False
    original_key = ref.get("original_usage_record_key")
    original = originals.get(original_key)
    if original_key is not None and (
        original is None or original.actual_call_id != attempt.call_id
    ):
        return False
    return any(event.id == ref["reconciliation_event_id"] for event in matching)


async def reviewed_usage_shadows(db, *, filters, through_id):
    """Yield validated audit observations in bounded batches, never trust a role alone."""
    if through_id is None:
        return
    after_id = 0
    while True:
        records = (
            (
                await db.execute(
                    select(AIUsageRecord)
                    .outerjoin(
                        BillingCallAttempt,
                        BillingCallAttempt.call_id == AIUsageRecord.actual_call_id,
                    )
                    .where(
                        *filters,
                        AIUsageRecord.id > after_id,
                        AIUsageRecord.id <= through_id,
                        or_(
                            AIUsageRecord.role == "late_actual",
                            and_(
                                AIUsageRecord.actual_call_id.is_not(None),
                                BillingCallAttempt.usage_record_key
                                != AIUsageRecord.record_key,
                            ),
                        ),
                    )
                    .order_by(AIUsageRecord.id)
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        if not records:
            return
        after_id = records[-1].id
        refs = {record.id: late_reference(record) for record in records}
        call_ids = {
            record.actual_call_id or (refs[record.id] or {}).get("call_id")
            for record in records
        } - {None}
        if not call_ids:
            continue
        attempts = {
            item.call_id: item
            for item in (
                await db.execute(
                    select(BillingCallAttempt).where(
                        BillingCallAttempt.call_id.in_(call_ids)
                    )
                )
            )
            .scalars()
            .all()
        }
        keys = {
            item.usage_record_key for item in attempts.values() if item.usage_record_key
        }
        keys.update(
            ref["original_usage_record_key"]
            for ref in refs.values()
            if ref and ref.get("original_usage_record_key")
        )
        evidence = {
            item.record_key: item
            for item in (
                await db.execute(
                    select(AIUsageRecord).where(AIUsageRecord.record_key.in_(keys))
                )
            )
            .scalars()
            .all()
        }
        events = (
            (
                await db.execute(
                    select(BillingReconciliationEvent).where(
                        BillingReconciliationEvent.call_id.in_(call_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        for record in records:
            call_id = record.actual_call_id or (refs[record.id] or {}).get("call_id")
            attempt = attempts.get(call_id)
            selected = evidence.get(attempt.usage_record_key) if attempt else None
            # A selection appended after the aggregate snapshot must not remove
            # an original whose replacement was not included in that snapshot.
            if selected is not None and selected.id > through_id:
                continue
            if reviewed_shadow(
                record, attempt, selected, events, evidence, user_id=record.user_id
            ):
                yield record
