"""Durable, verified webhook admission for Agent business executions."""

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from starlette.responses import JSONResponse

from backend.models.agent_team_models import AgentTeamSourceType, AgentTeamTask
from backend.models.billing_models import BillingOperation
from backend.models.webhook_execution_models import WebhookExecutionReceipt
from backend.services.service_execution_capacity import (
    has_live_service_execution_ownership,
)


def delivery_operation_id(delivery_id: str) -> str:
    if not isinstance(delivery_id, str) or not delivery_id or len(delivery_id) > 191:
        raise ValueError("Invalid verified webhook delivery identifier")
    return str(uuid5(NAMESPACE_URL, f"sakura:agent:delivery:{delivery_id}"))


async def find_delivery_task(db, delivery_id, repo_full_name, number, *, is_pr=False):
    if delivery_id is None:
        return None
    delivery_operation_id(delivery_id)  # validate before querying
    task = (
        await db.execute(
            select(AgentTeamTask).where(
                AgentTeamTask.webhook_delivery_id == delivery_id
            )
        )
    ).scalar_one_or_none()
    if task is None:
        return None
    if (
        task.repo_full_name != repo_full_name
        or task.source_issue_number != number
        or (task.source_type == AgentTeamSourceType.PR_REVIEW.value) != is_pr
    ):
        raise ValueError("Verified Agent delivery source conflict")
    task._billing_delivery_replayed = True
    return task


async def persist_delivery_task(db, task):
    """Resolve concurrent insert replays through the database unique constraint."""
    delivery_id = task.webhook_delivery_id
    source = (
        task.repo_full_name,
        task.source_issue_number,
        task.source_type == AgentTeamSourceType.PR_REVIEW.value,
    )
    try:
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task._billing_delivery_replayed = False
        return task
    except IntegrityError:
        if not delivery_id:
            raise
        await db.rollback()
        existing = await find_delivery_task(
            db, delivery_id, source[0], source[1], is_pr=source[2]
        )
        if existing is None:
            raise
        return existing


@dataclass(frozen=True)
class AgentDeliveryClaim:
    receipt_id: str
    owner_token: str
    operation_id: str
    task_id: int


_worker_delivery_claim: ContextVar[AgentDeliveryClaim | None] = ContextVar(
    "sakura_agent_worker_delivery_claim", default=None
)


async def claim_agent_delivery_worker(db, task_id):
    """Fence at the worker's locked carrier read, before context or side effects.

    Receipt -> Task matches the reconciliation lock order. Financial admission
    already exists; this guard never takes wallet locks or changes its amount.
    Once processing is committed, operator recovery cannot label it unstarted.
    """
    claim = _worker_delivery_claim.get()
    if claim is None:
        return True
    if claim.task_id != task_id:
        return False
    receipt = (
        await db.execute(
            select(WebhookExecutionReceipt)
            .where(
                WebhookExecutionReceipt.receipt_id == claim.receipt_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        receipt is None
        or receipt.owner_token != claim.owner_token
        or receipt.status != "pending_reconciliation"
        or receipt.operation_id != claim.operation_id
    ):
        return False
    task = (
        await db.execute(
            select(AgentTeamTask)
            .where(
                AgentTeamTask.id == task_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    operation = await db.get(BillingOperation, claim.operation_id)
    if (
        task is None
        or task.billing_operation_id != claim.operation_id
        or task.status != "queued"
        or task.repo_full_name != receipt.source.get("repo_full_name")
        or task.source_issue_number != receipt.source.get("issue_number")
        or (task.source_type == AgentTeamSourceType.PR_REVIEW.value)
        != receipt.source.get("is_pr")
        or operation is None
        or operation.feature != "agent"
        or operation.outcome is not None
        or task.billing_user_id != operation.user_id
    ):
        return False
    receipt.status = "processing"
    await db.flush()
    return True


def _delivery_identity(payload, *, is_pr):
    delivery_id = payload.get("_sakura_delivery_id")
    operation_id = delivery_operation_id(delivery_id)
    repository = payload.get("repository") or {}
    issue = payload.get("issue") or {}
    source = {
        "repo_full_name": repository.get("full_name"),
        "issue_number": issue.get("number"),
        "is_pr": is_pr,
        "started_by": (payload.get("comment") or {}).get("user", {}).get("login"),
    }
    if not source["repo_full_name"] or not isinstance(source["issue_number"], int):
        raise ValueError("Missing verified Agent delivery source")
    digest = hashlib.sha256(
        json.dumps(
            {k: v for k, v in payload.items() if k != "_sakura_delivery_id"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    receipt_id = hashlib.sha256(f"agent:{delivery_id}".encode()).hexdigest()
    return receipt_id, operation_id, source, digest


def _pending_response(operation_id, task_id):
    return JSONResponse(
        status_code=503,
        content={
            "status": "error",
            "reason": "delivery_pending_reconciliation",
            "operation_id": operation_id,
            "task_id": task_id,
        },
    )


async def _handoff_proven(db, operation_id, task):
    operation = await db.get(BillingOperation, operation_id)
    if operation is not None and operation.outcome is not None:
        return True
    if await has_live_service_execution_ownership(db, operation_id):
        return True
    return bool(
        task is not None
        and task.billing_operation_id == operation_id
        and task.status in {"completed", "failed", "cancelled", "abandoned"}
    )


async def inspect_agent_delivery(db, payload, *, is_pr, task=None):
    """A task row alone proves neither quota admission nor background handoff.

    Retryable receipts have not entered the dispatch window and can safely redo
    the same durable admission. Pending receipts require actual worker liveness,
    a terminal result, or operator review; absence of liveness is not no-send
    evidence. The original delivery identity survives a later manual task retry.
    """
    receipt_id, operation_id, source, digest = _delivery_identity(payload, is_pr=is_pr)
    receipt = await db.get(WebhookExecutionReceipt, receipt_id)
    if receipt is not None:
        if (
            receipt.payload_digest != digest
            or any(receipt.source.get(k) != v for k, v in source.items())
            or receipt.operation_id != operation_id
        ):
            raise ValueError("Verified Agent delivery payload conflict")
        task_id = receipt.source["task_id"]
        if task is not None and task.id != task_id:
            raise ValueError("Verified Agent delivery task conflict")
        if task is None:
            task = await db.get(AgentTeamTask, task_id)
        if receipt.status == "accepted":
            return JSONResponse(content={**(receipt.response or {}), "duplicate": True})
        if task is not None and (
            task.repo_full_name != source["repo_full_name"]
            or task.source_issue_number != source["issue_number"]
            or (task.source_type == AgentTeamSourceType.PR_REVIEW.value) != is_pr
        ):
            raise ValueError("Verified Agent delivery task source conflict")
        proven = await _handoff_proven(db, operation_id, task)
        if proven:
            response = {"status": "accepted", "task_id": task_id, "duplicate": True}
            result = await db.execute(
                update(WebhookExecutionReceipt)
                .where(
                    WebhookExecutionReceipt.receipt_id == receipt_id,
                    WebhookExecutionReceipt.owner_token == receipt.owner_token,
                    WebhookExecutionReceipt.status == receipt.status,
                )
                .values(status="accepted", response=response)
            )
            await db.commit()
            if result.rowcount != 1:
                current = (
                    await db.execute(
                        select(WebhookExecutionReceipt)
                        .where(WebhookExecutionReceipt.receipt_id == receipt_id)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if current.status == "accepted":
                    return JSONResponse(
                        content={**(current.response or {}), "duplicate": True}
                    )
                return _pending_response(operation_id, task_id)
            return JSONResponse(content=response)
        if (
            receipt.status != "retryable"
            or task is None
            or task.billing_operation_id != operation_id
        ):
            return _pending_response(operation_id, task_id)
        return None
    if task is None:
        return None
    if (
        task.webhook_delivery_id != payload["_sakura_delivery_id"]
        or task.repo_full_name != source["repo_full_name"]
        or task.source_issue_number != source["issue_number"]
        or (task.source_type == AgentTeamSourceType.PR_REVIEW.value) != is_pr
        or (task.started_by is not None and task.started_by != source["started_by"])
    ):
        raise ValueError("Verified Agent delivery task source conflict")
    if await _handoff_proven(db, operation_id, task) or task.status in {
        "completed",
        "failed",
        "cancelled",
        "abandoned",
    }:
        # Pre-receipt completed carriers are upgrade evidence. They are never
        # recharged or rerun, including installations predating stable op IDs.
        return JSONResponse(
            content={"status": "accepted", "task_id": task.id, "duplicate": True}
        )
    if task.billing_operation_id != operation_id or task.status != "queued":
        return _pending_response(operation_id, task.id)
    return None


async def prepare_agent_delivery(db, payload, task, *, is_pr):
    response = await inspect_agent_delivery(db, payload, is_pr=is_pr, task=task)
    if response is not None:
        return response
    receipt_id, operation_id, source, digest = _delivery_identity(payload, is_pr=is_pr)
    receipt = await db.get(WebhookExecutionReceipt, receipt_id)
    if receipt is None:
        db.add(
            WebhookExecutionReceipt(
                receipt_id=receipt_id,
                feature="agent",
                delivery_id=payload["_sakura_delivery_id"],
                operation_id=operation_id,
                source={**source, "task_id": task.id},
                payload_digest=digest,
                owner_token=str(uuid4()),
                status="retryable",
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            if await db.get(WebhookExecutionReceipt, receipt_id) is None:
                raise
            # Rollback expires ORM carriers even with expire_on_commit=False.
            # Reload through the winning receipt rather than implicit attribute
            # I/O on the expired object under a native async session.
            return await inspect_agent_delivery(db, payload, is_pr=is_pr)
    return None


async def claim_agent_dispatch(db, payload, task_id, *, is_pr):
    receipt_id, operation_id, _, _ = _delivery_identity(payload, is_pr=is_pr)
    token = str(uuid4())
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
    await db.commit()
    if result.rowcount != 1:
        return None
    return AgentDeliveryClaim(receipt_id, token, operation_id, task_id)


async def reject_agent_delivery(db, payload, response, *, is_pr):
    receipt_id, _, _, _ = _delivery_identity(payload, is_pr=is_pr)
    await db.execute(
        update(WebhookExecutionReceipt)
        .where(
            WebhookExecutionReceipt.receipt_id == receipt_id,
            WebhookExecutionReceipt.status == "retryable",
        )
        .values(status="accepted", response=json.loads(response.body))
    )
    await db.commit()


async def run_agent_delivery(claim, submit, session_factory):
    """Observe the complete worker; local task creation is not a durable ack."""
    async with session_factory() as db:
        receipt = await db.get(WebhookExecutionReceipt, claim.receipt_id)
        task = await db.get(AgentTeamTask, claim.task_id)
        if (
            receipt is None
            or receipt.owner_token != claim.owner_token
            or receipt.status != "pending_reconciliation"
            or task is None
            or task.billing_operation_id != claim.operation_id
        ):
            return  # Operator recovery/manual retry fenced this late coroutine.
    token = _worker_delivery_claim.set(claim)
    try:
        return await submit(claim.task_id)
    finally:
        _worker_delivery_claim.reset(token)
        async with session_factory() as db:
            task = await db.get(AgentTeamTask, claim.task_id)
            if await _handoff_proven(db, claim.operation_id, task):
                await db.execute(
                    update(WebhookExecutionReceipt)
                    .where(
                        WebhookExecutionReceipt.receipt_id == claim.receipt_id,
                        WebhookExecutionReceipt.owner_token == claim.owner_token,
                        WebhookExecutionReceipt.status.in_(
                            ("pending_reconciliation", "processing")
                        ),
                    )
                    .values(
                        status="accepted",
                        response={
                            "status": "accepted",
                            "task_id": claim.task_id,
                        },
                    )
                )
                await db.commit()
