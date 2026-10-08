"""Claim verified deliveries before admission or irreversible side effects.

This receipt fences parallel handlers and remembers acknowledged delivery results.
The in-process queue is not an atomic outbox: a crash around an external write or
queue handoff leaves a visible processing/pending receipt for reconciliation. We
never infer that a lost acknowledgement means the external action did not occur.
"""

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from backend.models.webhook_execution_models import WebhookExecutionReceipt


class DeliveryConflict(ValueError):
    pass


class ReviewHandoffUncertain(RuntimeError):
    """A queue error did not prove whether a background task was created."""


@dataclass
class DeliveryClaim:
    receipt_id: str
    owner_token: str
    session_factory: object
    effects_started: bool = False


_delivery_claim: ContextVar[DeliveryClaim | None] = ContextVar(
    "sakura_verified_delivery_claim", default=None
)


def _identity(payload, feature, delivery_id):
    if not isinstance(delivery_id, str) or not delivery_id or len(delivery_id) > 191:
        raise DeliveryConflict("Invalid verified delivery identifier")
    repository = payload.get("repository") or {}
    repo = repository.get("full_name")
    resource = payload.get("pull_request") or payload.get("issue") or {}
    number = resource.get("number")
    if not isinstance(repo, str) or not repo or not isinstance(number, int):
        raise DeliveryConflict("Missing verified delivery source")
    source = {
        "repo_full_name": repo,
        "pr_number" if feature == "pr_review" else "issue_number": number,
        "action": payload.get("action"),
    }
    clean_payload = {k: v for k, v in payload.items() if k != "_sakura_delivery_id"}
    digest = hashlib.sha256(
        json.dumps(clean_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    operation_id = str(uuid5(NAMESPACE_URL, f"sakura:{feature}:delivery:{delivery_id}"))
    receipt_id = hashlib.sha256(f"{feature}:{delivery_id}".encode()).hexdigest()
    return receipt_id, operation_id, source, digest


def _replay_response(receipt):
    if receipt.status == "accepted":
        return JSONResponse(
            content={
                **(receipt.response or {}),
                "status": "deduplicated",
                "original_status": (receipt.response or {}).get("status"),
            }
        )
    return JSONResponse(
        status_code=503,
        content={
            "status": "error",
            "reason": "delivery_pending_reconciliation"
            if receipt.status == "pending_reconciliation"
            else "delivery_processing",
            "operation_id": receipt.operation_id,
        },
    )


async def _claim(payload, feature, delivery_id, session_factory):
    from backend.services.billing_service import BillingService

    receipt_id, operation_id, source, digest = _identity(payload, feature, delivery_id)
    token = str(uuid4())
    async with session_factory() as db:
        receipt = (
            await db.execute(
                select(WebhookExecutionReceipt)
                .where(WebhookExecutionReceipt.receipt_id == receipt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if receipt is not None:
            if receipt.payload_digest != digest or receipt.source != source:
                raise DeliveryConflict(
                    "Verified delivery payload conflicts with receipt"
                )
            if receipt.status in {"processing", "pending_reconciliation", "retryable"}:
                operation = await BillingService(db)._operation(
                    operation_id, allow_missing=True
                )
                if operation is not None and operation.outcome is not None:
                    expected = {k: v for k, v in source.items() if k != "action"}
                    if operation.feature != feature or any(
                        (operation.source or {}).get(k) != v
                        for k, v in expected.items()
                    ):
                        raise DeliveryConflict(
                            "Verified delivery operation source conflict"
                        )
                    receipt.status = "accepted"
                    receipt.response = {
                        "status": "accepted",
                        "operation_id": operation_id,
                        "outcome": operation.outcome,
                    }
                    await db.commit()
            if receipt.status != "retryable":
                return _replay_response(receipt)
            result = await db.execute(
                update(WebhookExecutionReceipt)
                .where(
                    WebhookExecutionReceipt.receipt_id == receipt_id,
                    WebhookExecutionReceipt.status == "retryable",
                    WebhookExecutionReceipt.owner_token == receipt.owner_token,
                )
                .values(status="processing", owner_token=token, response=None)
            )
            await db.commit()
            if not result.rowcount:
                return _replay_response(receipt)
            return DeliveryClaim(receipt_id, token, session_factory)
        operation = await BillingService(db)._operation(
            operation_id, allow_missing=True
        )
        if operation is not None:
            # Upgrade compatibility: pre-receipt terminal operations prove a
            # delivery already ran. Missing or contradictory source cannot prove
            # which signed resource it belongs to and must never be guessed.
            expected = {k: v for k, v in source.items() if k != "action"}
            if operation.feature != feature or any(
                (operation.source or {}).get(k) != v for k, v in expected.items()
            ):
                raise DeliveryConflict("Verified delivery operation source conflict")
            if operation.outcome is not None:
                return JSONResponse(
                    content={"status": "deduplicated", "operation_id": operation_id}
                )
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "reason": "delivery_pending_reconciliation",
                    "operation_id": operation_id,
                },
            )
        receipt = WebhookExecutionReceipt(
            receipt_id=receipt_id,
            feature=feature,
            delivery_id=delivery_id,
            payload_digest=digest,
            source=source,
            operation_id=operation_id,
            owner_token=token,
            status="processing",
        )
        db.add(receipt)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            winner = await db.get(WebhookExecutionReceipt, receipt_id)
            if winner is None:
                raise
            if winner.payload_digest != digest or winner.source != source:
                raise DeliveryConflict(
                    "Verified delivery payload conflicts with receipt"
                )
            return _replay_response(winner)
    return DeliveryClaim(receipt_id, token, session_factory)


async def mark_delivery_side_effects():
    """Persist the uncertain external/queue window before entering it."""
    claim = _delivery_claim.get()
    if claim is None or claim.effects_started:
        return
    async with claim.session_factory() as db:
        result = await db.execute(
            update(WebhookExecutionReceipt)
            .where(
                WebhookExecutionReceipt.receipt_id == claim.receipt_id,
                WebhookExecutionReceipt.owner_token == claim.owner_token,
                WebhookExecutionReceipt.status == "processing",
            )
            .values(status="pending_reconciliation")
        )
        await db.commit()
        if result.rowcount != 1:
            raise DeliveryConflict("Webhook delivery claim ownership changed")
    claim.effects_started = True


async def _finish(claim, response=None):
    success = response is not None and response.status_code < 500
    async with claim.session_factory() as db:
        result = await db.execute(
            update(WebhookExecutionReceipt)
            .where(
                WebhookExecutionReceipt.receipt_id == claim.receipt_id,
                WebhookExecutionReceipt.owner_token == claim.owner_token,
                WebhookExecutionReceipt.status.in_(
                    ["processing", "pending_reconciliation"]
                ),
            )
            .values(
                status="accepted"
                if success
                else (
                    "pending_reconciliation" if claim.effects_started else "retryable"
                ),
                response=json.loads(response.body) if success else None,
            )
        )
        await db.commit()
        if result.rowcount != 1:
            current = await db.get(WebhookExecutionReceipt, claim.receipt_id)
            if (
                current is not None
                and current.owner_token == claim.owner_token
                and current.status == "accepted"
            ):
                return  # A committed acknowledgement whose response was lost.
            raise DeliveryConflict("Webhook receipt acknowledgement was not persisted")


def verified_delivery(feature, session_factory, *, predicate=None):
    """Wrap a server handler; a verified header is bound to its signed payload."""

    def decorate(handler):
        @wraps(handler)
        async def wrapped(payload, *args, **kwargs):
            if predicate is not None and not predicate(payload):
                return await handler(payload, *args, **kwargs)
            delivery_id = kwargs.get("delivery_id") or (
                args[0] if args else payload.get("_sakura_delivery_id")
            )
            if not delivery_id:
                return await handler(payload, *args, **kwargs)
            try:
                claim = await _claim(payload, feature, delivery_id, session_factory)
            except DeliveryConflict:
                logger.warning(
                    "Verified webhook delivery rejected for source conflict: feature={}",
                    feature,
                )
                return JSONResponse(
                    status_code=409,
                    content={"status": "error", "reason": "delivery_source_conflict"},
                )
            except Exception as exc:
                logger.error(
                    "Verified webhook admission unavailable: feature={}, error_type={}",
                    feature,
                    type(exc).__name__,
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "error",
                        "reason": "delivery_admission_unavailable",
                    },
                )
            if isinstance(claim, JSONResponse):
                return claim
            context_token = _delivery_claim.set(claim)
            try:
                response = await handler(payload, *args, **kwargs)
                await _finish(claim, response)
                return response
            except BaseException:
                await _finish(claim)
                raise
            finally:
                _delivery_claim.reset(context_token)

        return wrapped

    return decorate


async def admit_manual_review(payload, session_factory):
    """Reserve and check user capacity before removing any existing result."""
    from backend.services.billing_context import context_for_payload
    from backend.services.billing_service import BillingService

    context = context_for_payload(payload, "pr_review")
    if context.user_id is None:
        # Administrator/system cost ownership is metered when the worker starts;
        # no user wallet or package admission is needed before cleanup.
        return None
    async with session_factory() as db:
        operation = await BillingService(db).register_operation(
            context.user_id,
            context.operation_id,
            context.feature,
            source=dict(context.source),
            platform_reason=context.platform_reason,
        )
        if operation.outcome is not None:
            raise DeliveryConflict("Manual review delivery already ended")
        await db.commit()
    return context.operation_id


async def compensate_unstarted_review(operation_id, session_factory, *, db=None):
    """Release a failed handoff, preserving its durable no-call outcome."""
    from backend.core.time_service import now_utc
    from backend.models.billing_models import BillingCallAttempt
    from backend.models.service_execution_models import ServiceExecutionOwnership
    from backend.services.billing_service import BillingService

    if operation_id is None:
        return
    if db is None:
        async with session_factory() as owned_db:
            await compensate_unstarted_review(
                operation_id, session_factory, db=owned_db
            )
            await owned_db.commit()
        return
    service = BillingService(db)
    operation = await service._operation(operation_id, allow_missing=True)
    if operation is None:
        return
    attempts = (
        (
            await db.execute(
                select(BillingCallAttempt.call_id)
                .where(BillingCallAttempt.operation_id == operation_id)
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
                    ServiceExecutionOwnership.operation_id == operation_id,
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
        # A queue failure after actually starting a worker is not a known
        # no-call failure. Leave its operation recoverable and do not race
        # the owner's finalization or fabricate a refund.
        raise DeliveryConflict(
            "Review handoff has calls or live ownership; reconciliation required"
        )
    if operation.outcome is None:
        await service.finish_operation(operation_id, "failed")


async def compensate_unstarted_agent(task_id, session_factory):
    """Atomically release a known rejected handoff and fail its queued carrier."""
    from backend.models.agent_team_models import AgentTeamTask

    async with session_factory() as db:
        task = (
            await db.execute(
                select(AgentTeamTask)
                .where(AgentTeamTask.id == task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        await compensate_unstarted_review(
            task.billing_operation_id, session_factory, db=db
        )
        task.status = "failed"
        task.current_phase = "error"
        task.error_message = "Agent queue admission failed before scheduling"
        await db.commit()
