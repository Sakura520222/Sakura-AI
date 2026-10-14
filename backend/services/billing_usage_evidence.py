"""Validate reviewed Usage links before suppressing retained audit observations."""

import hashlib


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
