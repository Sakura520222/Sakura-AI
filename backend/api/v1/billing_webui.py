"""JSON projections for the billing screens, using the existing payment services."""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.billing import PlanCreateRequest
from backend.api.v1.deps import (
    require_api_auth,
    require_api_super_admin,
    require_cookie_csrf,
)
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.core.config import get_dynamic_config
from backend.core.log_sanitizer import sanitize_exception_text
from backend.core.time_service import format_rfc3339, now_utc
from backend.models.payment_models import Order
from backend.services.billing_account_pricing_service import configured_pricing_sources
from backend.services.billing_view_service import add_billing_admin_audit
from backend.services.payment.currency_units import (
    CURRENCY_MINOR_EXPONENTS,
    safe_format_minor_amount,
    supported_currencies,
)
from backend.services.payment.gateway_factory import get_configured_providers
from backend.services.payment_service import PaymentError, PaymentService
from backend.webui.deps import get_db

router = APIRouter(prefix="/billing", tags=["Billing"], route_class=PrivateAPIRoute)


@router.get("/invoice/{order_no}")
async def payment_invoice(
    order_no: str,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    order = await db.scalar(
        select(Order).where(
            Order.order_no == order_no, Order.user_id == user["user_id"]
        )
    )
    if not order:
        raise HTTPException(404, "Order not found")
    try:
        metadata = json.loads(order.metadata_json or "{}")
    except TypeError, ValueError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    crypto = (
        order.payment_provider in {"tron", "nowpayments"} and order.status == "pending"
    )
    return {
        "id": order.id,
        "order_no": order.order_no,
        "status": order.status,
        "provider": order.payment_provider,
        "expires_at": format_rfc3339(order.expires_at) if order.expires_at else None,
        "payment": {
            key: str(metadata.get(key, ""))
            for key in ("pay_address", "pay_amount", "pay_currency")
        }
        if crypto
        else None,
    }


@router.post("/invoice-status", dependencies=[Depends(require_cookie_csrf)])
async def payment_invoice_status(
    request: Request,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    # Reuse provider confirmation and reconciliation; the browser cannot assert settlement.
    from backend.webui.routes.billing import crypto_payment_status

    return await crypto_payment_status(request, db, user)


class RefundNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field("", max_length=2000)


class ReviewNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_note: str = Field("", max_length=2000)


class BillingBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1)

    @field_validator("ids")
    @classmethod
    def distinct_ids(cls, values: list[int]) -> list[int]:
        # A selection is a set: duplicate IDs must not toggle the same row twice.
        return list(dict.fromkeys(values))


class BatchFailure(BaseModel):
    id: int
    reason: str


class BillingBatchResult(BaseModel):
    succeeded_ids: list[int]
    failed: list[BatchFailure]


def batch_result(result) -> BillingBatchResult:
    return BillingBatchResult(
        succeeded_ids=[row.id for row in result["success"]],
        failed=[
            BatchFailure(id=row["id"], reason=str(row["reason"]))
            for row in (*result.get("failed", []), *result.get("skipped", []))
        ],
    )


def _plan_view(plan):
    values = {name: getattr(plan, name) for name in PlanCreateRequest.model_fields}
    values["credit_grant"] = str(plan.credit_grant)
    return {
        **values,
        "id": plan.id,
        "is_active": plan.is_active,
        "formatted_price": safe_format_minor_amount(plan.price_cents, plan.currency),
    }


def _refund_view(row):
    return {
        "id": row.id,
        "order_id": row.order_id,
        "order_no": row.order.order_no if row.order else None,
        "user_id": row.user_id,
        "status": row.status,
        "reason": row.reason,
        "review_note": row.review_note,
        "amount_cents": row.amount_cents,
        "currency": row.currency,
        "formatted_amount": safe_format_minor_amount(row.amount_cents, row.currency),
        "requested_at": format_rfc3339(row.requested_at),
        "reviewed_at": format_rfc3339(row.reviewed_at) if row.reviewed_at else None,
    }


def _payment_error(exc):
    return HTTPException(
        400,
        detail={"code": exc.code, "message": sanitize_exception_text(exc)},
    )


@router.get("/context")
async def billing_context(user: dict = Depends(require_api_auth)):
    enabled = bool(await get_dynamic_config("payment_enabled"))
    return {
        "payment_enabled": enabled,
        "providers": await get_configured_providers() if enabled else [],
        "currencies": supported_currencies(),
        "currency_minor_exponents": CURRENCY_MINOR_EXPONENTS,
    }


@router.get("/admin/plans")
async def list_admin_plans(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    return [
        _plan_view(plan)
        for plan in await PaymentService(db).list_plans(active_only=False)
    ]


@router.post(
    "/admin/plans/batch/{action}",
    dependencies=[Depends(require_cookie_csrf)],
    response_model=BillingBatchResult,
)
async def batch_plans(
    action: Literal["toggle", "disable", "delete"],
    body: BillingBatch,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    service = PaymentService(db)
    try:
        result = (
            await service.batch_toggle_plans(body.ids)
            if action == "toggle"
            else await service.batch_delete_plans(
                body.ids, hard_delete=action == "delete"
            )
        )
        response = batch_result(result)
        add_billing_admin_audit(
            db,
            actor_id=user["user_id"],
            action=f"billing_batch_plan_{action}",
            target_id="batch:plans",
            detail={
                "ids": body.ids,
                "succeeded_ids": response.succeeded_ids,
                "failed_ids": [row.id for row in response.failed],
            },
        )
        await db.commit()
        return response
    except PaymentError as exc:
        await db.rollback()
        raise _payment_error(exc) from exc


@router.post(
    "/admin/codes/batch/{action}",
    dependencies=[Depends(require_cookie_csrf)],
    response_model=BillingBatchResult,
)
async def batch_codes(
    action: Literal["enable", "disable", "delete"],
    body: BillingBatch,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    service = PaymentService(db)
    try:
        result = (
            await service.batch_delete_redeem_codes(body.ids)
            if action == "delete"
            else await service.batch_update_redeem_codes(
                body.ids, status="active" if action == "enable" else "disabled"
            )
        )
        response = batch_result(result)
        add_billing_admin_audit(
            db,
            actor_id=user["user_id"],
            action=f"billing_batch_code_{action}",
            target_id="batch:codes",
            detail={
                "ids": body.ids,
                "succeeded_ids": response.succeeded_ids,
                "failed_ids": [row.id for row in response.failed],
            },
        )
        await db.commit()
        return response
    except PaymentError as exc:
        await db.rollback()
        raise _payment_error(exc) from exc


@router.get("/entitlements")
async def current_entitlements(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    from backend.models.telegram_models import TelegramUser
    from backend.services.legacy_entitlement_service import LegacyEntitlementService
    from backend.services.quota_service import QuotaService

    account = await db.get(TelegramUser, user["user_id"])
    if account is None:
        raise HTTPException(404, "User not found")
    await QuotaService(db).reset_user_quotas_if_expired(account)
    service = LegacyEntitlementService(db)
    features = {}
    for feature, prefix in (
        ("pr_review", ""),
        ("issue_analysis", "issue_"),
        ("agent", "agent_"),
    ):
        limits = await service.effective_limits(account, feature)
        features[feature] = [
            {
                "period": period,
                "limit": limits[index],
                "used": getattr(account, f"{prefix}{period}_used") or 0,
            }
            for index, period in enumerate(("daily", "weekly", "monthly"))
        ]
    return {
        "features": features,
        "legacy_remaining": await service.remaining(user["user_id"]),
    }


@router.get("/admin/codes")
async def list_admin_codes(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    rows, total = await PaymentService(db).list_redeem_codes(limit=limit, offset=offset)
    return {
        "items": [
            {
                "id": row.id,
                "code": row.code,
                "plan_id": row.plan_id,
                "plan_name": (row.plan_snapshot or {}).get("name")
                or (row.plan.name if row.plan else None),
                "batch_name": row.batch_name,
                "status": row.status,
                "used_count": row.used_count,
                "max_uses": row.max_uses,
                "expires_at": format_rfc3339(row.expires_at)
                if row.expires_at
                else None,
            }
            for row in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/admin/pricing-context")
async def pricing_context(user: dict = Depends(require_api_super_admin)):
    from backend.webui.routes.billing import _PRICING_CALL_KINDS, _PRICING_UNITS

    accounts, auxiliary = await configured_pricing_sources()
    return {
        "accounts": accounts,
        "auxiliary": auxiliary,
        "call_kinds": _PRICING_CALL_KINDS,
        "units": _PRICING_UNITS,
        "currencies": supported_currencies(),
    }


@router.get("/admin/pricing/accounts/{account_id}/models")
async def pricing_models(
    account_id: str,
    request: Request,
    refresh: bool = False,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    from backend.webui.config_feedback import config_api_language
    from backend.webui.routes.billing import admin_pricing_account_models

    return await admin_pricing_account_models(
        account_id,
        request,
        db=db,
        user=user,
        user_prefs={"language": await config_api_language(db, user)},
        refresh=refresh,
    )


@router.get("/orders/{order_id}/refund-requests")
async def order_refund_requests(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    rows = await PaymentService(db).list_refund_requests_for_orders(
        user["user_id"], [order_id]
    )
    row = rows.get(order_id)
    return {"request": _refund_view(row) if row else None}


@router.post(
    "/orders/{order_id}/refund-request", dependencies=[Depends(require_cookie_csrf)]
)
async def submit_refund_request(
    order_id: int,
    body: RefundNote,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    try:
        row = await PaymentService(db).submit_refund_request(
            order_id=order_id, user_id=user["user_id"], reason=body.reason
        )
        await db.commit()
        return _refund_view(row)
    except PaymentError as exc:
        await db.rollback()
        raise _payment_error(exc) from exc


@router.post("/orders/{order_id}/cancel", dependencies=[Depends(require_cookie_csrf)])
async def cancel_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    row = (
        await db.execute(
            select(Order).where(Order.id == order_id, Order.user_id == user["user_id"])
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Order not found")
    try:
        await PaymentService(db).cancel_order(row.order_no, user["user_id"])
        await db.commit()
        return {"success": True}
    except PaymentError as exc:
        await db.rollback()
        raise _payment_error(exc) from exc


@router.post("/orders/{order_id}/hide", dependencies=[Depends(require_cookie_csrf)])
async def hide_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    # Match the existing WebUI hide operation: preserve order/ledger evidence and
    # lock the current status before changing only its user-list visibility.
    row = (
        await db.execute(
            select(Order)
            .where(
                Order.id == order_id,
                Order.user_id == user["user_id"],
                Order.status.in_(["pending", "cancelled", "expired", "refunded"]),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Order not found or cannot be hidden")
    if row.hidden_by_user_at is None:
        row.hidden_by_user_at = now_utc()
    await db.commit()
    return {"success": True}


@router.get("/admin/refund-requests")
async def list_refund_requests(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
    status: str | None = Query(None, pattern="^(pending|approved|rejected|failed)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    rows, total = await PaymentService(db).list_refund_requests(
        status=status, limit=limit, offset=offset
    )
    return {
        "items": [_refund_view(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post(
    "/admin/refund-requests/{request_id}/{decision}",
    dependencies=[Depends(require_cookie_csrf)],
)
async def review_refund_request(
    request_id: int,
    decision: str,
    body: ReviewNote,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    if decision not in {"approve", "reject"}:
        raise HTTPException(404)
    service = PaymentService(db)
    handler = (
        service.approve_refund_request
        if decision == "approve"
        else service.reject_refund_request
    )
    try:
        row = await handler(
            request_id=request_id,
            reviewer_id=user["user_id"],
            review_note=body.review_note,
        )
        add_billing_admin_audit(
            db,
            actor_id=user["user_id"],
            action=f"{decision}_refund_request",
            target_id=str(request_id),
            detail={"status": row.status, "order_id": row.order_id},
        )
        await db.commit()
        return _refund_view(row)
    except PaymentError as exc:
        await db.rollback()
        raise _payment_error(exc) from exc
