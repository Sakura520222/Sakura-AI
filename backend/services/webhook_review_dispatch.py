"""Persist and fence review handoffs without replaying their webhook side effects."""

import json
from contextlib import contextmanager
from functools import wraps
from uuid import uuid4

from loguru import logger
from sqlalchemy import func, select, update

from backend.models import database
from backend.models.billing_models import BillingCallAttempt, BillingOperation
from backend.models.webhook_execution_models import WebhookExecutionReceipt
from backend.services.billing_context import BillingContext, context_for_payload
from backend.services.billing_service import BillingError, BillingService
from backend.services.service_execution_capacity import (
    has_live_service_execution_ownership,
)

DISPATCH_KEY = "_dispatch"
_PAYLOAD_FIELDS = frozenset(
    [
        "action",
        "repo_owner",
        "repo_name",
        "repo_full_name",
        "repository_external_id",
        "source_system_instance",
        "installation_id",
        "pr_id",
        "pr_number",
        "issue_number",
        "title",
        "body",
        "author",
        "branch",
        "base_branch",
        "head_sha",
        "base_sha",
        "before",
        "after",
        "diff_url",
        "patch_url",
        "html_url",
        "state",
        "draft",
        "merged",
        "labels",
        "sender",
        "user_id",
        "billing_user_id",
        "billing_platform_reason",
        "trigger_user_id",
        "delivery_id",
        "billing_context",
        "task_id",
    ]
)


def carrier(receipt):
    data = (receipt.response or {}).get(DISPATCH_KEY)
    return data if isinstance(data, dict) and data.get("version") == 1 else None


def public_response(receipt):
    return {
        key: value
        for key, value in (receipt.response or {}).items()
        if key != DISPATCH_KEY
    }


@contextmanager
def detached_delivery_context():
    from backend.services.webhook_execution_service import _delivery_claim

    token = _delivery_claim.set(None)
    try:
        yield
    finally:
        _delivery_claim.reset(token)


async def prepare_delivery_dispatch(payload, feature):
    from backend.services.webhook_execution_service import _delivery_claim

    if payload.get("_delivery_receipt_id"):
        return  # Restored server-owned carrier; do not overwrite its claim.
    claim = _delivery_claim.get()
    if claim is None:
        return
    context = context_for_payload(payload, feature)
    payload.setdefault("task_id", str(uuid4()))
    async with claim.session_factory() as db:
        receipt = await db.get(
            WebhookExecutionReceipt,
            claim.receipt_id,
            with_for_update=True,
            populate_existing=True,
        )
        if (
            receipt is None
            or receipt.owner_token != claim.owner_token
            or receipt.feature != feature
            or receipt.operation_id != context.operation_id
            or receipt.status not in {"processing", "pending_reconciliation"}
            or any(
                context.source.get(key) != value
                for key, value in receipt.source.items()
                if key != "action"
            )
        ):
            raise BillingError("Delivery dispatch source conflict")
        await BillingService(db).register_operation(
            context.user_id,
            context.operation_id,
            feature,
            source=dict(context.source),
            platform_reason=context.platform_reason,
        )
        snapshot = json.loads(
            json.dumps(
                {key: value for key, value in payload.items() if key in _PAYLOAD_FIELDS}
            )
        )
        receipt.response = {
            **public_response(receipt),
            "status": "accepted",
            "task_key"
            if feature == "pr_review"
            else "task_id": f"{payload['repo_full_name']}#{payload['pr_number']}"
            if feature == "pr_review"
            else payload["task_id"],
            DISPATCH_KEY: {"version": 1, "state": "queued", "payload": snapshot},
        }
        receipt.status = "pending_reconciliation"
        await db.commit()
    payload.update(
        _delivery_receipt_id=claim.receipt_id, _delivery_owner_token=claim.owner_token
    )
    claim.effects_started = True


async def claim_queued_dispatch(db, receipt, token):
    data = carrier(receipt)
    if data is None or data.get("state") != "queued":
        return None
    payload = json.loads(json.dumps(data["payload"]))
    context = BillingContext.from_payload(payload["billing_context"])
    operation = await BillingService(db)._operation(receipt.operation_id)
    if (
        operation.outcome is not None
        or operation.user_id != context.user_id
        or operation.feature != receipt.feature
        or context.operation_id != receipt.operation_id
        or any(
            context.source.get(key) != value
            for key, value in receipt.source.items()
            if key != "action"
        )
    ):
        raise BillingError("Queued delivery needs reconciliation")
    calls = (
        await db.execute(
            select(BillingCallAttempt.call_id)
            .where(BillingCallAttempt.operation_id == receipt.operation_id)
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if calls or await has_live_service_execution_ownership(
        db, receipt.operation_id, for_update=True
    ):
        raise BillingError("Queued delivery has execution evidence")
    changed = await db.execute(
        update(WebhookExecutionReceipt)
        .where(
            WebhookExecutionReceipt.receipt_id == receipt.receipt_id,
            WebhookExecutionReceipt.owner_token == receipt.owner_token,
            WebhookExecutionReceipt.status == receipt.status,
        )
        .values(owner_token=token, status="pending_reconciliation")
    )
    if changed.rowcount != 1:
        raise BillingError("Delivery dispatch claim changed")
    await db.commit()
    payload.update(_delivery_receipt_id=receipt.receipt_id, _delivery_owner_token=token)
    return payload


async def dispatch_restored(payload, feature):
    if feature == "pr_review":
        from backend.workers.review_worker import submit_review_task

        return await submit_review_task(payload)
    from backend.workers.issue_worker import submit_issue_analysis_task

    return await submit_issue_analysis_task(payload)


async def has_recoverable_review_carrier(db, operation):
    rows = (
        (
            await db.execute(
                select(WebhookExecutionReceipt).where(
                    WebhookExecutionReceipt.operation_id == operation.operation_id
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        data = carrier(row)
        if data is None or data.get("state") != "queued":
            continue
        try:
            context = BillingContext.from_payload(data["payload"]["billing_context"])
        except ValueError, TypeError, KeyError:
            continue
        if (
            context.user_id != operation.user_id
            or context.operation_id != operation.operation_id
            or context.feature != operation.feature
            or row.feature != operation.feature
        ):
            continue
        if any(
            context.source.get(key) != value or operation.source.get(key) != value
            for key, value in row.source.items()
            if key != "action"
        ):
            continue
        called = (
            await db.execute(
                select(BillingCallAttempt.call_id)
                .where(BillingCallAttempt.operation_id == operation.operation_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if called is None:
            return True
    return False


async def cancel_queued_deliveries(feature, task_key, *, session_factory=None):
    """Invalidate proven unstarted carriers before acknowledging cancellation."""
    from backend.services.webhook_execution_service import compensate_unstarted_review

    repo, separator, number = task_key.rpartition("#")
    if not separator or not number.isdecimal():
        return 0
    factory = session_factory or database.async_session
    if factory is None:
        return 0
    async with factory() as db:
        rows = (
            (
                await db.execute(
                    select(WebhookExecutionReceipt)
                    .where(
                        WebhookExecutionReceipt.feature == feature,
                        WebhookExecutionReceipt.source["repo_full_name"].as_string()
                        == repo,
                        WebhookExecutionReceipt.source[
                            "pr_number" if feature == "pr_review" else "issue_number"
                        ].as_integer()
                        == int(number),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        cancelled = 0
        for receipt in rows:
            data = carrier(receipt)
            if data is None or data.get("state") != "queued":
                continue
            operation = await BillingService(db)._operation(
                receipt.operation_id, allow_missing=True
            )
            if (
                operation is None
                or operation.outcome is not None
                or not await has_recoverable_review_carrier(db, operation)
            ):
                continue
            await compensate_unstarted_review(
                receipt.operation_id, None, db=db, outcome="cancelled"
            )
            receipt.owner_token = str(uuid4())
            receipt.status = "accepted"
            receipt.response = {
                **public_response(receipt),
                "status": "cancelled",
                "outcome": "cancelled",
                DISPATCH_KEY: {**data, "state": "terminal"},
            }
            cancelled += 1
        await db.commit()
        return cancelled


def delivery_worker(feature):
    def decorate(function):
        @wraps(function)
        async def wrapped(worker, payload, *args, **kwargs):
            receipt_id = payload.get("_delivery_receipt_id")
            if not receipt_id:
                return await function(worker, payload, *args, **kwargs)
            async with database.async_session() as db:
                receipt = await db.get(
                    WebhookExecutionReceipt,
                    receipt_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                data = carrier(receipt) if receipt else None
                context = BillingContext.from_payload(payload["billing_context"])
                snapshot = {
                    key: value
                    for key, value in payload.items()
                    if key in _PAYLOAD_FIELDS
                }
                operation = await db.get(BillingOperation, context.operation_id)
                if (
                    receipt is None
                    or data is None
                    or data.get("state") != "queued"
                    or receipt.owner_token != payload.get("_delivery_owner_token")
                    or receipt.feature != feature
                    or receipt.operation_id != context.operation_id
                    or data.get("payload") != snapshot
                    or operation is None
                    or operation.outcome is not None
                    or operation.user_id != context.user_id
                ):
                    return "deduplicated_delivery"
                changed = await db.execute(
                    update(WebhookExecutionReceipt)
                    .where(
                        WebhookExecutionReceipt.receipt_id == receipt_id,
                        WebhookExecutionReceipt.owner_token == receipt.owner_token,
                        WebhookExecutionReceipt.status == receipt.status,
                    )
                    .values(
                        status="processing",
                        response={
                            **public_response(receipt),
                            DISPATCH_KEY: {**data, "state": "started"},
                        },
                    )
                )
                if changed.rowcount != 1:
                    await db.rollback()
                    return "deduplicated_delivery"
                await db.commit()
            try:
                return await function(worker, payload, *args, **kwargs)
            finally:
                async with database.async_session() as db:
                    receipt = await db.get(
                        WebhookExecutionReceipt,
                        receipt_id,
                        with_for_update=True,
                        populate_existing=True,
                    )
                    operation = await db.get(BillingOperation, context.operation_id)
                    if (
                        receipt
                        and receipt.owner_token == payload.get("_delivery_owner_token")
                        and operation
                        and operation.outcome is not None
                    ):
                        data = carrier(receipt)
                        receipt.response = {
                            **public_response(receipt),
                            "outcome": operation.outcome,
                            DISPATCH_KEY: {**data, "state": "terminal"},
                        }
                        receipt.status = "accepted"
                        await db.commit()

        return wrapped

    return decorate


async def resume_queued_deliveries(*, session_factory=None):
    """One startup pass; only durable not-started carriers may be dispatched."""
    factory = session_factory or database.async_session
    last_id = ""
    resumed = 0
    async with factory() as db:
        high_water = (
            await db.execute(select(func.max(WebhookExecutionReceipt.receipt_id)))
        ).scalar_one()
    if not high_water:
        return 0
    while True:
        async with factory() as db:
            rows = (
                (
                    await db.execute(
                        select(WebhookExecutionReceipt)
                        .where(
                            WebhookExecutionReceipt.receipt_id > last_id,
                            WebhookExecutionReceipt.receipt_id <= high_water,
                            WebhookExecutionReceipt.feature.in_(
                                ("pr_review", "issue_analysis")
                            ),
                            WebhookExecutionReceipt.response[DISPATCH_KEY][
                                "state"
                            ].as_string()
                            == "queued",
                        )
                        .order_by(WebhookExecutionReceipt.receipt_id)
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            return resumed
        for row in rows:
            last_id = row.receipt_id
            async with factory() as db:
                receipt = await db.get(
                    WebhookExecutionReceipt,
                    row.receipt_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                try:
                    payload = await claim_queued_dispatch(db, receipt, str(uuid4()))
                except BillingError:
                    await db.rollback()
                    continue  # Persisted live/called/terminal evidence is not replayed.
                feature = receipt.feature
            if payload is not None:
                try:
                    await dispatch_restored(payload, feature)
                except Exception as exc:
                    async with factory() as db:
                        receipt = await db.get(
                            WebhookExecutionReceipt,
                            row.receipt_id,
                            with_for_update=True,
                            populate_existing=True,
                        )
                        data = carrier(receipt)
                        if (
                            receipt.owner_token == payload["_delivery_owner_token"]
                            and data
                            and data.get("state") == "queued"
                        ):
                            receipt.status = "pending_reconciliation"
                            receipt.response = {
                                **public_response(receipt),
                                DISPATCH_KEY: {
                                    **data,
                                    "last_dispatch_error": type(exc).__name__,
                                },
                            }
                            await db.commit()
                    logger.warning(
                        "Queued webhook dispatch recovery failed: type={}",
                        type(exc).__name__,
                    )
                    continue
                resumed += 1
