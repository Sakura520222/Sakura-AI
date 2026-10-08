"""Operator-reviewed receipt recovery, without replaying an external action."""

import json
from uuid import uuid4

from sqlalchemy import select

from backend.core.time_service import now_utc
from backend.models.admin_action_log import AdminActionLog
from backend.models.billing_models import BillingCallAttempt
from backend.models.service_execution_models import ServiceExecutionOwnership
from backend.models.telegram_models import TelegramUser
from backend.models.webhook_execution_models import WebhookExecutionReceipt
from backend.services.billing_service import BillingError, BillingService


async def list_pending_receipts(db, *, limit=100, offset=0):
    if not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("Invalid pagination")
    rows = (
        (
            await db.execute(
                select(WebhookExecutionReceipt)
                .where(
                    WebhookExecutionReceipt.status.in_(
                        ["processing", "pending_reconciliation", "retryable"]
                    )
                )
                .order_by(
                    WebhookExecutionReceipt.created_at,
                    WebhookExecutionReceipt.receipt_id,
                )
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "receipt_id": row.receipt_id,
            "feature": row.feature,
            "delivery_id": row.delivery_id,
            "operation_id": row.operation_id,
            "status": row.status,
            "source": row.source,
        }
        for row in rows
    ]


async def reconcile_receipt(db, receipt_id, *, actor_id, resolution, evidence, reason):
    """Explicitly acknowledge or cancel; unknown results are never resubmitted.

    ``cancelled_unstarted`` is the operator's verified statement that no queue
    handoff occurred. It still checks durable attempts and worker ownership and
    releases admission. A new requested execution then needs a new delivery.
    """
    actor = await db.get(TelegramUser, actor_id)
    if actor is None or not actor.is_active or actor.role != "super_admin":
        raise BillingError("Receipt reconciliation requires an active super-admin")
    if resolution not in {"terminal", "accepted", "cancelled_unstarted"}:
        raise BillingError("Invalid receipt resolution")
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 2000:
        raise BillingError("Reviewed evidence is required")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise BillingError("Receipt resolution reason is required")
    receipt = (
        await db.execute(
            select(WebhookExecutionReceipt)
            .where(WebhookExecutionReceipt.receipt_id == receipt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if receipt is None:
        raise BillingError("Webhook receipt does not exist")
    if receipt.status == "accepted":
        return receipt  # Repeating the same operator action has no new effect.
    previous_status = receipt.status
    operation = await BillingService(db)._operation(
        receipt.operation_id, allow_missing=True
    )
    if resolution == "terminal":
        if operation is None or operation.outcome is None:
            raise BillingError("No durable terminal operation proves completion")
        response = {"status": "accepted", "outcome": operation.outcome}
    elif resolution == "accepted":
        response = {"status": "accepted", "reason": "operator_verified_handoff"}
    else:
        attempts = (
            (
                await db.execute(
                    select(BillingCallAttempt.call_id)
                    .where(BillingCallAttempt.operation_id == receipt.operation_id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        owners = (
            (
                await db.execute(
                    select(ServiceExecutionOwnership.token)
                    .where(
                        ServiceExecutionOwnership.operation_id == receipt.operation_id,
                        ServiceExecutionOwnership.state != "released",
                        ServiceExecutionOwnership.expires_at > now_utc(),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if attempts or owners:
            raise BillingError("Cannot cancel a called or live execution as unstarted")
        if operation is not None:
            if operation.outcome is not None:
                raise BillingError(
                    "Existing terminal execution requires terminal resolution"
                )
            await BillingService(db).finish_operation(receipt.operation_id, "cancelled")
        response = {"status": "cancelled", "reason": "operator_verified_unstarted"}
    receipt.status = "accepted"
    # Fence a late original handler acknowledgement after an operator resolves
    # its crashed/unknown window. That handler cannot overwrite the audit result.
    receipt.owner_token = str(uuid4())
    receipt.response = {**response, "operation_id": receipt.operation_id}
    db.add(
        AdminActionLog(
            admin_id=actor_id,
            action="webhook_execution_reconcile",
            target_type="webhook_execution_receipt",
            target_id=receipt.receipt_id,
            detail=json.dumps(
                {
                    "resolution": resolution,
                    "previous_status": previous_status,
                    "operation_id": receipt.operation_id,
                    "evidence": evidence,
                    "reason": reason,
                },
                ensure_ascii=False,
            ),
        )
    )
    await db.flush()
    return receipt
