"""Reviewed recovery for durable incremental handoffs; never sends an AI call."""

import json

from sqlalchemy import select

from backend.core.time_service import now_utc
from backend.models.admin_action_log import AdminActionLog
from backend.models.billing_models import BillingCallAttempt
from backend.models.database import PRReview, PRReviewIncrementalQueue
from backend.models.telegram_models import TelegramUser
from backend.services.billing_service import BillingError, BillingService
from backend.services.service_execution_capacity import (
    has_live_service_execution_ownership,
)


async def has_recoverable_incremental_carrier(db, operation_id):
    """Prove a never-called admission still has its original durable PR queue.

    Callers classify this as dispatch recovery, rather than silently discarding
    its purchased allowance. There is no new money, deadline or financial entry.
    """
    from backend.services.billing_context import BillingContext

    operation = await BillingService(db)._operation(operation_id, allow_missing=True)
    if (
        operation is None
        or operation.feature != "pr_review"
        or operation.outcome is not None
    ):
        return False
    if (
        await db.execute(
            select(BillingCallAttempt.call_id)
            .where(BillingCallAttempt.operation_id == operation_id)
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none() is not None:
        return False
    rows = (
        (
            await db.execute(
                select(PRReviewIncrementalQueue)
                .where(
                    PRReviewIncrementalQueue.billing_context["operation_id"].as_string()
                    == operation_id,
                    PRReviewIncrementalQueue.status.in_(("pending", "dispatching")),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        try:
            context = BillingContext.from_payload(row.billing_context)
        except ValueError, TypeError, KeyError:
            continue
        if (
            context.operation_id == operation_id
            and context.user_id == operation.user_id
            and context.feature == operation.feature
            and context.platform_reason == operation.platform_reason
            and context.source.get("repo_full_name") == row.repo_full_name
            and context.source.get("pr_number") == row.pr_number
            and operation.source.get("repo_full_name") == row.repo_full_name
            and operation.source.get("pr_number") == row.pr_number
        ):
            return True
    return False


async def recover_increment_dispatch(
    db, queue_id, *, dry_run=True, actor_id=None, evidence=None, reason=None
):
    """Reset only expired handoffs that have not entered a worker.

    A running worker without a live owner may have an unknown external result.
    It requires financial recovery/reconciliation first, not automatic replay.
    """
    if not dry_run:
        actor = await db.get(TelegramUser, actor_id)
        if actor is None or not actor.is_active or actor.role != "super_admin":
            raise BillingError("Incremental recovery requires an active super-admin")
        if (
            not isinstance(evidence, str)
            or not evidence.strip()
            or len(evidence) > 2000
        ):
            raise BillingError("Reviewed handoff evidence is required")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise BillingError("Recovery reason is required")
    item = await db.get(PRReviewIncrementalQueue, queue_id)
    if item is None:
        raise BillingError("Incremental queue row does not exist")
    operation_id = (item.billing_context or {}).get("operation_id")
    operation = (
        await BillingService(db)._operation(operation_id, allow_missing=True)
        if operation_id
        else None
    )
    # Financial rows precede queue row locks on every recovery/cancellation path.
    group = (
        (
            await db.execute(
                select(PRReviewIncrementalQueue)
                .where(
                    PRReviewIncrementalQueue.repo_full_name == item.repo_full_name,
                    PRReviewIncrementalQueue.pr_number == item.pr_number,
                    PRReviewIncrementalQueue.status.in_(
                        ("pending", "dispatching", "running")
                    ),
                )
                .order_by(PRReviewIncrementalQueue.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    group = [
        row
        for row in group
        if (
            (row.billing_context or {}).get("operation_id") == operation_id
            if operation_id
            else row.id == queue_id
        )
    ]
    report = {
        "queue_id": queue_id,
        "operation_id": operation_id,
        "status": "pending_reconciliation",
    }
    if operation is not None and operation.outcome is not None:
        report["status"] = "terminal"
        target = "consumed" if operation.outcome == "completed" else operation.outcome
    elif operation_id and await has_live_service_execution_ownership(
        db, operation_id, for_update=True
    ):
        report["status"] = "owned"
        return report
    elif (
        operation_id
        and (
            await db.execute(
                select(BillingCallAttempt.call_id)
                .where(BillingCallAttempt.operation_id == operation_id)
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        is not None
    ):
        return report
    elif not group:
        report["status"] = "terminal"
        return report
    elif all(row.status == "pending" and row.dispatch_token is None for row in group):
        report["status"] = "ready"
        target = "pending"
    elif all(row.status in {"pending", "dispatching"} for row in group):
        if any(
            row.status == "dispatching" and row.dispatch_expires_at is None
            for row in group
        ):
            return report
        if any(
            row.dispatch_expires_at is not None and row.dispatch_expires_at > now_utc()
            for row in group
        ):
            report["status"] = "leased"
            return report
        report["status"] = "ready"
        target = "pending"
    else:
        return report
    if not dry_run:
        previous = {str(row.id): row.status for row in group}
        for row in group:
            row.status = target
            row.dispatch_token = None
            row.dispatch_expires_at = None
            if target != "pending":
                row.consumed_at = now_utc()
        db.add(
            AdminActionLog(
                admin_id=actor_id,
                action="billing.incremental_recovery",
                target_type="pr_incremental",
                target_id=str(queue_id),
                detail=json.dumps(
                    {
                        "operation_id": operation_id,
                        "queue_ids": [row.id for row in group],
                        "previous_status": previous,
                        "status": target,
                        "evidence": evidence,
                        "reason": reason,
                    }
                ),
            )
        )
        await db.flush()
    return report


async def incremental_recovery_payload(db, queue_id):
    """Reconstruct only trusted stored source; clients cannot nominate a payer."""
    item = await db.get(PRReviewIncrementalQueue, queue_id)
    if item is None:
        raise BillingError("Incremental queue row does not exist")
    review = (
        await db.execute(
            select(PRReview)
            .where(
                PRReview.repo_owner == item.repo_owner,
                PRReview.repo_name == item.repo_name,
                PRReview.pr_number == item.pr_number,
            )
            .order_by(PRReview.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if review is None:
        raise BillingError("Trusted PR source record is missing")
    return {
        "repo_owner": item.repo_owner,
        "repo_name": item.repo_name,
        "repo_full_name": item.repo_full_name,
        "pr_id": review.pr_id,
        "pr_number": item.pr_number,
        "author": review.author,
        "title": review.title,
        "branch": review.branch,
        "action": "synchronize",
        "before": item.base_sha,
        "after": item.head_sha,
        "head_sha": item.head_sha,
        "incremental_resume_queue_id": item.id,
    }
