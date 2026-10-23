"""Durable WebUI handoff using the existing Agent delivery receipt and fence."""

import hashlib
import json
from uuid import uuid4

from sqlalchemy import exists, select, update

from backend.models.agent_team_models import AgentTeamSourceType, AgentTeamTask
from backend.models.webhook_execution_models import WebhookExecutionReceipt
from backend.services.agent_team.billing_admission import AgentDeliveryClaim
from backend.services.billing_service import BillingError, BillingService


class ManualAgentDispatchPending(BillingError):
    def __init__(self):
        super().__init__(
            "Agent dispatch requires reconciliation", "agent_dispatch_pending"
        )


def manual_dispatch_receipt_id(operation_id: str) -> str:
    return hashlib.sha256(f"webui-agent:{operation_id}".encode()).hexdigest()


async def stage_manual_agent_dispatch(
    db, task: AgentTeamTask, *, unknown_handoff=False
) -> str:
    """Stage task, admission and receipt in the caller's single transaction."""
    operation_id = task.billing_operation_id
    if not operation_id or task.status != "queued":
        raise ManualAgentDispatchPending()
    receipt_id = manual_dispatch_receipt_id(operation_id)
    source = {
        "origin": "webui",
        "task_id": task.id,
        "repo_full_name": task.repo_full_name,
        "issue_number": task.source_issue_number,
        "is_pr": task.source_type == AgentTeamSourceType.PR_REVIEW.value,
        "started_by": task.started_by,
    }
    receipt = await db.get(
        WebhookExecutionReceipt,
        receipt_id,
        with_for_update=True,
        populate_existing=True,
    )
    if receipt is not None and (
        receipt.source != source or receipt.status != "retryable"
    ):
        raise ManualAgentDispatchPending()
    billing = BillingService(db)
    if unknown_handoff:
        operation = await billing._operation(operation_id, allow_missing=True)
        if (
            operation is None
            or operation.user_id != task.billing_user_id
            or operation.feature != "agent"
            or operation.source.get("repo_full_name") != task.repo_full_name
            or operation.source.get("task_id") != task.id
        ):
            raise ManualAgentDispatchPending()
    else:
        operation = await billing.register_operation(
            task.billing_user_id,
            operation_id,
            "agent",
            {"repo_full_name": task.repo_full_name, "task_id": task.id},
            platform_reason=task.billing_platform_reason,
        )
    if operation.outcome is not None:
        raise ManualAgentDispatchPending()
    if receipt is None:
        db.add(
            WebhookExecutionReceipt(
                receipt_id=receipt_id,
                feature="agent",
                delivery_id=f"webui:{operation_id}",
                operation_id=operation_id,
                source=source,
                payload_digest=hashlib.sha256(
                    json.dumps(source, sort_keys=True).encode()
                ).hexdigest(),
                owner_token=str(uuid4()),
                status="pending_reconciliation" if unknown_handoff else "retryable",
            )
        )
    await db.flush()
    return receipt_id


async def claim_manual_agent_dispatch(db, receipt_id: str, task_id: int):
    receipt = await db.get(WebhookExecutionReceipt, receipt_id)
    if (
        receipt is None
        or receipt.feature != "agent"
        or receipt.source.get("origin") != "webui"
        or receipt.source.get("task_id") != task_id
    ):
        return None
    token = str(uuid4())
    operation_id = receipt.operation_id
    result = await db.execute(
        update(WebhookExecutionReceipt)
        .where(
            WebhookExecutionReceipt.receipt_id == receipt_id,
            WebhookExecutionReceipt.status == "retryable",
            exists(
                select(AgentTeamTask.id).where(
                    AgentTeamTask.id == task_id,
                    AgentTeamTask.billing_operation_id == operation_id,
                    AgentTeamTask.status == "queued",
                )
            ),
        )
        .values(status="pending_reconciliation", owner_token=token)
    )
    await db.commit()  # Release locks before the worker or any external action.
    if result.rowcount != 1:
        return None
    return AgentDeliveryClaim(receipt_id, token, operation_id, task_id)
