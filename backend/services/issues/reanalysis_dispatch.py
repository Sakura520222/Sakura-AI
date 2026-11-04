"""Durable manual Issue handoff; financial admission and carrier commit together."""

import hashlib
import json
from functools import wraps
from uuid import uuid4

from sqlalchemy import select, update

from backend.core.time_service import now_utc
from backend.models import database
from backend.models.billing_models import BillingOperation
from backend.models.database import IssueAnalysis
from backend.models.webhook_execution_models import WebhookExecutionReceipt
from backend.services.billing_context import BillingContext
from backend.services.billing_service import BillingError


def reanalysis_receipt_id(operation_id):
    return hashlib.sha256(f"issue-reanalysis:{operation_id}".encode()).hexdigest()


def _input_digest(record):
    source = {
        name: getattr(record, name)
        for name in (
            "id",
            "repo_owner",
            "repo_name",
            "issue_number",
            "analysis_version",
            "title",
            "body",
            "author",
        )
    }
    return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()


async def stage_reanalysis_receipt(db, context):
    record = await db.get(IssueAnalysis, context.source["issue_analysis_id"])
    db.add(
        WebhookExecutionReceipt(
            receipt_id=reanalysis_receipt_id(context.operation_id),
            feature="issue_analysis",
            delivery_id=f"reanalysis:{context.operation_id}",
            operation_id=context.operation_id,
            source={
                "origin": "issue_reanalysis",
                "issue_analysis_id": record.id,
                "task_id": context.source["task_id"],
                "repo_full_name": context.source["repo_full_name"],
                "issue_number": record.issue_number,
                "analysis_version": record.analysis_version,
                "trigger_user_id": context.source.get("trigger_user_id"),
            },
            payload_digest=_input_digest(record),
            owner_token=str(uuid4()),
            status="retryable",
        )
    )
    await db.flush()


async def claim_reanalysis_dispatch(db, context, payload):
    receipt_id = reanalysis_receipt_id(context.operation_id)
    token = str(uuid4())
    result = await db.execute(
        update(WebhookExecutionReceipt)
        .where(
            WebhookExecutionReceipt.receipt_id == receipt_id,
            WebhookExecutionReceipt.status == "retryable",
        )
        .values(status="pending_reconciliation", owner_token=token)
    )
    await db.commit()
    if result.rowcount != 1:
        raise BillingError(
            "Issue handoff needs reconciliation", "reanalysis_handoff_unconfirmed"
        )
    payload.update(_reanalysis_receipt_id=receipt_id, _reanalysis_owner_token=token)


async def _claim_worker(db, payload):
    context = BillingContext.from_payload(payload["billing_context"])
    receipt = await db.get(
        WebhookExecutionReceipt,
        payload["_reanalysis_receipt_id"],
        with_for_update=True,
        populate_existing=True,
    )
    if (
        receipt is None
        or receipt.status != "pending_reconciliation"
        or receipt.owner_token != payload.get("_reanalysis_owner_token")
        or receipt.operation_id != context.operation_id
        or receipt.feature != "issue_analysis"
        or receipt.source.get("origin") != "issue_reanalysis"
        or receipt.source.get("task_id") != payload.get("task_id")
    ):
        return None
    record = await db.get(
        IssueAnalysis,
        receipt.source["issue_analysis_id"],
        with_for_update=True,
        populate_existing=True,
    )
    operation = await db.get(BillingOperation, receipt.operation_id)
    if (
        record is None
        or _input_digest(record) != receipt.payload_digest
        or operation is None
        or operation.outcome is not None
        or operation.user_id != context.user_id
        or operation.feature != "issue_analysis"
        or context.source.get("issue_analysis_id") != record.id
        or context.source.get("repo_full_name")
        != f"{record.repo_owner}/{record.repo_name}"
        or context.source.get("issue_number") != record.issue_number
        or context.source.get("task_id") != receipt.source.get("task_id")
        or context.platform_reason != operation.platform_reason
        or payload.get("_reanalysis_analysis_id") != record.id
        or payload.get("repo_full_name") != f"{record.repo_owner}/{record.repo_name}"
        or payload.get("state") != "open"
        or operation.source.get("issue_analysis_id") != record.id
        or operation.source.get("task_id") != receipt.source.get("task_id")
        or operation.source.get("repo_full_name")
        != f"{record.repo_owner}/{record.repo_name}"
        or any(
            payload.get(field) != getattr(record, field)
            for field in (
                "repo_owner",
                "repo_name",
                "issue_number",
                "title",
                "body",
                "author",
                "analysis_version",
            )
        )
    ):
        return None
    cancelled_before_claim = record.status == "cancelled" or (
        record.status == "pending" and record.issue_state == "closed"
    )
    if not cancelled_before_claim and (
        record.status != "pending" or record.issue_state != "open"
    ):
        return None
    claimed = await db.execute(
        update(WebhookExecutionReceipt)
        .where(
            WebhookExecutionReceipt.receipt_id == receipt.receipt_id,
            WebhookExecutionReceipt.status == "pending_reconciliation",
            WebhookExecutionReceipt.owner_token == receipt.owner_token,
        )
        .values(status="processing")
    )
    if claimed.rowcount != 1:
        await db.rollback()
        return None
    if cancelled_before_claim:
        from backend.services.webhook_execution_service import (
            compensate_unstarted_review,
        )

        # The current owner has not started this Worker. Keep lifecycle changes
        # terminal, and still reject a legacy/inconsistent called or live task.
        await compensate_unstarted_review(
            context.operation_id, None, db=db, outcome="cancelled"
        )
        record.status = "cancelled"
        record.completed_at = record.completed_at or now_utc()
        receipt.status = "accepted"
        receipt.owner_token = str(uuid4())
        receipt.response = {
            "status": "cancelled",
            "task_id": payload["task_id"],
            "outcome": "cancelled",
        }
        await db.commit()
        return None
    await db.commit()
    return record


async def _finish_receipt(payload):
    async with database.async_session() as db:
        receipt = await db.get(
            WebhookExecutionReceipt,
            payload["_reanalysis_receipt_id"],
            with_for_update=True,
            populate_existing=True,
        )
        if (
            receipt is None
            or receipt.owner_token != payload.get("_reanalysis_owner_token")
            or receipt.status != "processing"
        ):
            return
        operation = await db.get(BillingOperation, receipt.operation_id)
        if operation is None or operation.outcome is None:
            return  # Financial write failure/unknown execution remains recoverable.
        record = await db.get(
            IssueAnalysis, receipt.source["issue_analysis_id"], with_for_update=True
        )
        if record is not None and record.status in {"pending", "analyzing"}:
            # A coalesced observability trigger may do no new analysis. Keep the
            # request terminal without inventing an AI result for its pending row.
            if operation.outcome == "completed":
                record.status = "cancelled" if record.status == "pending" else "failed"
            else:
                record.status = operation.outcome
            record.completed_at = now_utc()
        receipt.status = "accepted"
        receipt.response = {
            "status": "accepted",
            "task_id": payload["task_id"],
            "outcome": operation.outcome,
        }
        await db.commit()


def durable_reanalysis_worker(function):
    @wraps(function)
    async def wrapped(worker, payload, *args, **kwargs):
        if not payload.get("_reanalysis_receipt_id"):
            return await function(worker, payload, *args, **kwargs)
        async with database.async_session() as db:
            record = await _claim_worker(db, payload)
        if record is None:
            # Do not remove a sibling's registry entry on duplicate delivery.
            import asyncio

            key = worker._make_task_key(payload)
            handle = (
                getattr(worker, "_task_handles", {})
                .get(key, {})
                .get(str(payload.get("task_id")))
            )
            if handle is asyncio.current_task():
                worker._unregister_task(key, str(payload["task_id"]))
            return str(payload.get("task_id", ""))
        worker._bind_analysis_record(
            worker._make_task_key(payload), str(payload["task_id"]), record
        )
        try:
            return await function(worker, payload, *args, **kwargs)
        finally:
            await _finish_receipt(payload)

    return wrapped


async def reanalysis_payload(db, receipt):
    """Restore only a known pre-dispatch carrier; never replay an unknown send."""
    if (
        receipt.status != "retryable"
        or receipt.source.get("origin") != "issue_reanalysis"
    ):
        raise BillingError(
            "Issue handoff needs reconciliation", "reanalysis_handoff_unconfirmed"
        )
    record = await db.get(IssueAnalysis, receipt.source["issue_analysis_id"])
    operation = await db.get(BillingOperation, receipt.operation_id)
    if (
        record is None
        or record.status != "pending"
        or record.issue_state != "open"
        or _input_digest(record) != receipt.payload_digest
        or operation is None
        or operation.outcome is not None
        or operation.feature != "issue_analysis"
        or operation.source.get("issue_analysis_id") != record.id
        or operation.source.get("task_id") != receipt.source.get("task_id")
        or operation.source.get("repo_full_name")
        != f"{record.repo_owner}/{record.repo_name}"
        or receipt.source.get("repo_full_name")
        != f"{record.repo_owner}/{record.repo_name}"
    ):
        raise BillingError(
            "Issue carrier needs reconciliation", "reanalysis_handoff_unconfirmed"
        )
    context = BillingContext(
        operation.user_id,
        operation.operation_id,
        operation.feature,
        operation.source,
        operation.platform_reason,
    )
    payload = {
        "repo_owner": record.repo_owner,
        "repo_name": record.repo_name,
        "repo_full_name": receipt.source["repo_full_name"],
        "issue_number": record.issue_number,
        "title": record.title,
        "body": record.body,
        "author": record.author,
        "state": record.issue_state,
        "analysis_version": record.analysis_version,
        "task_id": receipt.source["task_id"],
        "user_id": receipt.source.get("trigger_user_id"),
        "billing_user_id": operation.user_id,
        "billing_context": context.to_payload(),
        "_reanalysis_analysis_id": record.id,
    }
    return payload


async def reanalysis_task_status(db, user, task_id):
    receipt = (
        await db.execute(
            select(WebhookExecutionReceipt).where(
                WebhookExecutionReceipt.feature == "issue_analysis",
                WebhookExecutionReceipt.source["origin"].as_string()
                == "issue_reanalysis",
                WebhookExecutionReceipt.source["task_id"].as_string() == task_id,
            )
        )
    ).scalar_one_or_none()
    if receipt is None or (
        user.get("role") not in {"admin", "super_admin"}
        and receipt.source.get("trigger_user_id") != user.get("user_id")
    ):
        return None
    record = await db.get(IssueAnalysis, receipt.source["issue_analysis_id"])
    if record is None:
        return None
    return {
        "task_id": task_id,
        "issue_analysis_id": record.id,
        "status": record.status,
        "dispatch_status": receipt.status,
    }


async def retry_reanalysis_dispatch(db, user, task_id):
    from backend.services.issues.reanalysis_admission import (
        dispatch_admitted_reanalysis,
    )

    status = await reanalysis_task_status(db, user, task_id)
    if status is None:
        raise BillingError("Issue task not found", "reanalysis_task_unavailable")
    receipt = (
        await db.execute(
            select(WebhookExecutionReceipt)
            .where(
                WebhookExecutionReceipt.feature == "issue_analysis",
                WebhookExecutionReceipt.source["task_id"].as_string() == task_id,
                WebhookExecutionReceipt.source["origin"].as_string()
                == "issue_reanalysis",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    payload = await reanalysis_payload(db, receipt)
    context = BillingContext.from_payload(payload["billing_context"])
    return await dispatch_admitted_reanalysis(db, context, payload)
