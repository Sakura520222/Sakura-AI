"""API v1 付费配额端点"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_auth, require_api_super_admin
from backend.core.time_service import format_rfc3339
from backend.services.billing_service import BillingError, BillingService
from backend.services.billing_view_service import (
    BILLING_FEATURES,
    TRANSACTION_KINDS,
    BillingViewService,
    add_billing_admin_audit,
    payment_event_summary,
    pending_payment_events,
)
from backend.services.payment import SUPPORTED_PROVIDERS
from backend.services.payment.currency_units import safe_format_minor_amount
from backend.services.payment_service import PaymentError, PaymentService
from backend.webui.deps import get_db, require_payment_enabled

router = APIRouter(
    prefix="/billing",
    tags=["Billing"],
    dependencies=[Depends(require_payment_enabled)],
)


# ========== Schemas ==========


class BillingPlanRequest(BaseModel):
    @field_validator("credit_grant", mode="before", check_fields=False)
    @classmethod
    def validate_credit_precision(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("Credits must be a quoted decimal string")
        return value

    @field_validator("rate_limits", mode="before", check_fields=False)
    @classmethod
    def validate_rate_limits(cls, value):
        allowed = {
            "pr_daily",
            "pr_weekly",
            "pr_monthly",
            "issue_daily",
            "issue_weekly",
            "issue_monthly",
            "agent_daily",
            "agent_weekly",
            "agent_monthly",
            "repo_scan_daily",
        }
        if value is None:
            return value
        if not isinstance(value, dict) or any(
            key not in allowed
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 0
            for key, limit in value.items()
        ):
            raise ValueError(
                "Rate limits must be known keys with nonnegative integer values"
            )
        return value


class PlanCreateRequest(BillingPlanRequest):
    name: str = Field(..., min_length=1, max_length=100)
    plan_type: str = Field(..., pattern="^(one_time|subscription)$")
    price_cents: int = Field(
        0, ge=0, description="Integer minor units of the selected currency"
    )
    currency: str = Field("CNY", max_length=10)
    duration_days: int | None = None
    credit_grant: Decimal = Field(Decimal(0), ge=0, decimal_places=6, max_digits=24)
    rate_limits: dict[str, int] | None = None
    concurrency_limit: int | None = Field(
        None,
        ge=1,
        description="Per-user business execution limit across PR, Issue, Agent and Repo Scan; registered queued/running executions count until a terminal outcome is recorded, even if billing remains pending afterwards",
    )
    pr_quota_bonus: int = Field(0, ge=0)
    pr_daily_add: int = Field(0, ge=0)
    pr_weekly_add: int = Field(0, ge=0)
    pr_monthly_add: int = Field(0, ge=0)
    issue_quota_bonus: int = Field(0, ge=0)
    issue_daily_add: int = Field(0, ge=0)
    issue_weekly_add: int = Field(0, ge=0)
    issue_monthly_add: int = Field(0, ge=0)
    agent_quota_bonus: int = Field(0, ge=0)
    agent_daily_add: int = Field(0, ge=0)
    agent_weekly_add: int = Field(0, ge=0)
    agent_monthly_add: int = Field(0, ge=0)
    description: str | None = None
    sort_order: int = Field(0, ge=0)


class RedeemRequest(BaseModel):
    code: str = Field(..., min_length=1)


class GrantRequest(BaseModel):
    user_id: int = Field(..., ge=1)
    plan_id: int = Field(..., ge=1)
    idempotency_key: str | None = Field(None, min_length=1, max_length=191)


class GenerateCodesRequest(BaseModel):
    plan_id: int
    count: int = Field(..., ge=1, le=100)
    batch_name: str | None = None
    max_uses: int = Field(1, ge=1)


class PlanUpdateRequest(BillingPlanRequest):
    name: str | None = Field(None, min_length=1, max_length=100)
    plan_type: str | None = Field(None, pattern="^(one_time|subscription)$")
    price_cents: int | None = Field(
        None, ge=0, description="Integer minor units of the selected currency"
    )
    currency: str | None = Field(None, max_length=10)
    duration_days: int | None = None
    credit_grant: Decimal | None = Field(None, ge=0, decimal_places=6, max_digits=24)
    rate_limits: dict[str, int] | None = None
    concurrency_limit: int | None = Field(
        None,
        ge=1,
        description="Per-user business execution limit across PR, Issue, Agent and Repo Scan; registered queued/running executions count until a terminal outcome is recorded, even if billing remains pending afterwards",
    )
    pr_quota_bonus: int | None = Field(None, ge=0)
    pr_daily_add: int | None = Field(None, ge=0)
    pr_weekly_add: int | None = Field(None, ge=0)
    pr_monthly_add: int | None = Field(None, ge=0)
    issue_quota_bonus: int | None = Field(None, ge=0)
    issue_daily_add: int | None = Field(None, ge=0)
    issue_weekly_add: int | None = Field(None, ge=0)
    issue_monthly_add: int | None = Field(None, ge=0)
    agent_quota_bonus: int | None = Field(None, ge=0)
    agent_daily_add: int | None = Field(None, ge=0)
    agent_weekly_add: int | None = Field(None, ge=0)
    agent_monthly_add: int | None = Field(None, ge=0)
    is_active: bool | None = None
    sort_order: int | None = Field(None, ge=0)
    description: str | None = None


class RedeemCodeUpdateRequest(BaseModel):
    status: str | None = Field(None, pattern="^(active|disabled)$")
    expires_at: datetime | None = None
    max_uses: int | None = Field(None, ge=1)
    plan_id: int | None = None

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: datetime | None) -> datetime | None:
        """Require an explicit offset and normalize the instant to UTC."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include an explicit timezone offset")
        return value.astimezone(UTC)


class CreateOrderRequest(BaseModel):
    plan_id: int
    provider: str = Field(
        "stripe",
        pattern="^(" + "|".join(SUPPORTED_PROVIDERS) + ")$",
    )


class RefundRequest(BaseModel):
    amount_cents: int | None = Field(
        None, ge=1, description="Integer minor units of the order currency"
    )
    idempotency_key: str | None = Field(None, min_length=1, max_length=160)


# ========== Public endpoints ==========


@router.get("/plans")
async def list_plans(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """列出可用套餐"""
    svc = PaymentService(db)
    plans = await svc.list_plans(active_only=True)
    return [
        {
            "id": p.id,
            "name": p.name,
            "plan_type": p.plan_type,
            "price_cents": p.price_cents,
            "formatted_price": safe_format_minor_amount(p.price_cents, p.currency),
            "currency_supported": safe_format_minor_amount(p.price_cents, p.currency)
            is not None,
            "currency": p.currency,
            "duration_days": p.duration_days,
            "credit_grant": str(p.credit_grant or Decimal(0)),
            "rate_limits": p.rate_limits or {},
            "concurrency_limit": p.concurrency_limit,
            "pr_quota_bonus": p.pr_quota_bonus,
            "pr_daily_add": p.pr_daily_add,
            "pr_weekly_add": p.pr_weekly_add,
            "pr_monthly_add": p.pr_monthly_add,
            "issue_quota_bonus": p.issue_quota_bonus,
            "issue_daily_add": p.issue_daily_add,
            "issue_weekly_add": p.issue_weekly_add,
            "issue_monthly_add": p.issue_monthly_add,
            "agent_quota_bonus": p.agent_quota_bonus,
            "agent_daily_add": p.agent_daily_add,
            "agent_weekly_add": p.agent_weekly_add,
            "agent_monthly_add": p.agent_monthly_add,
            "description": p.description,
        }
        for p in plans
    ]


@router.post("/redeem")
async def redeem_code(
    req: RedeemRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """兑换码兑换"""
    svc = PaymentService(db)
    try:
        order = await svc.redeem_code(user["user_id"], req.code.strip().upper())
        await db.commit()
        return {
            "success": True,
            "order_no": order.order_no,
            "plan_name": order.plan.name if order.plan else None,
        }
    except PaymentError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders")
async def list_orders(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """用户订单历史"""
    svc = PaymentService(db)
    orders, total = await svc.list_user_orders(
        user["user_id"], limit=limit, offset=offset
    )
    return {
        "total": total,
        "orders": [
            {
                "id": o.id,
                "order_no": o.order_no,
                "plan_name": (o.plan_snapshot or {}).get("name")
                or (o.plan.name if o.plan else None),
                "amount_cents": o.amount_cents,
                "formatted_amount": safe_format_minor_amount(
                    o.amount_cents, o.currency
                ),
                "currency_supported": safe_format_minor_amount(
                    o.amount_cents, o.currency
                )
                is not None,
                "formatted_refunded_amount": safe_format_minor_amount(
                    int(getattr(o, "refunded_amount_cents", 0) or 0), o.currency
                ),
                "refunded_amount_cents": int(
                    getattr(o, "refunded_amount_cents", 0) or 0
                ),
                "currency": o.currency,
                "status": o.status,
                "payment_provider": o.payment_provider,
                "created_at": format_rfc3339(o.created_at) if o.created_at else None,
                "fulfilled_at": format_rfc3339(o.fulfilled_at)
                if o.fulfilled_at
                else None,
            }
            for o in orders
        ],
    }


@router.post("/orders")
async def create_order(
    req: CreateOrderRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """Create a payment order and return checkout URL"""
    svc = PaymentService(db)
    try:
        order = await svc.create_order(
            user_id=user["user_id"],
            plan_id=req.plan_id,
            provider=req.provider,
        )
        await db.commit()

        checkout_url = getattr(order, "_checkout_url", "")
        return {
            "success": True,
            "order_no": order.order_no,
            "status": order.status,
            "amount_cents": order.amount_cents,
            "formatted_amount": safe_format_minor_amount(
                order.amount_cents, order.currency
            ),
            "currency_supported": safe_format_minor_amount(
                order.amount_cents, order.currency
            )
            is not None,
            "formatted_refunded_amount": safe_format_minor_amount(
                int(getattr(order, "refunded_amount_cents", 0) or 0), order.currency
            ),
            "currency": order.currency,
            "provider": order.payment_provider,
            "checkout_url": checkout_url,
            "expires_at": format_rfc3339(order.expires_at)
            if order.expires_at
            else None,
        }
    except PaymentError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders/{order_id}")
async def get_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """Query order status"""
    from sqlalchemy import select

    from backend.models.payment_models import Order

    stmt = select(Order).where(Order.id == order_id, Order.user_id == user["user_id"])
    result = await db.execute(stmt)
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    import json

    checkout_url = ""
    if order.metadata_json:
        try:
            meta = json.loads(order.metadata_json)
            checkout_url = meta.get("checkout_url", "")
        except json.JSONDecodeError, TypeError:
            pass

    return {
        "id": order.id,
        "order_no": order.order_no,
        "status": order.status,
        "amount_cents": order.amount_cents,
        "formatted_amount": safe_format_minor_amount(
            order.amount_cents, order.currency
        ),
        "currency_supported": safe_format_minor_amount(
            order.amount_cents, order.currency
        )
        is not None,
        "formatted_refunded_amount": safe_format_minor_amount(
            int(getattr(order, "refunded_amount_cents", 0) or 0), order.currency
        ),
        "refunded_amount_cents": int(getattr(order, "refunded_amount_cents", 0) or 0),
        "currency": order.currency,
        "payment_provider": order.payment_provider,
        "provider_tx_id": order.provider_tx_id,
        "checkout_url": checkout_url,
        "paid_at": format_rfc3339(order.paid_at) if order.paid_at else None,
        "fulfilled_at": format_rfc3339(order.fulfilled_at)
        if order.fulfilled_at
        else None,
        "created_at": format_rfc3339(order.created_at) if order.created_at else None,
        "expires_at": format_rfc3339(order.expires_at) if order.expires_at else None,
    }


@router.post("/orders/{order_id}/refund")
async def refund_order(
    order_id: int,
    req: RefundRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """Admin: refund an order"""
    svc = PaymentService(db)
    try:
        order = await svc.process_refund(
            order_id=order_id,
            amount_cents=req.amount_cents,
            operator_id=user["user_id"],
            idempotency_key=req.idempotency_key,
        )
        await db.commit()
        return {
            "success": True,
            "order_no": order.order_no,
            "status": order.status,
        }
    except PaymentError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


# ========== Admin endpoints ==========


@router.post("/admin/plans")
async def create_plan(
    req: PlanCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """创建套餐"""
    svc = PaymentService(db)
    try:
        plan = await svc.create_plan(
            name=req.name,
            plan_type=req.plan_type,
            price_cents=req.price_cents,
            currency=req.currency,
            duration_days=req.duration_days,
            credit_grant=req.credit_grant,
            rate_limits=req.rate_limits,
            concurrency_limit=req.concurrency_limit,
            pr_quota_bonus=req.pr_quota_bonus,
            pr_daily_add=req.pr_daily_add,
            pr_weekly_add=req.pr_weekly_add,
            pr_monthly_add=req.pr_monthly_add,
            issue_quota_bonus=req.issue_quota_bonus,
            issue_daily_add=req.issue_daily_add,
            issue_weekly_add=req.issue_weekly_add,
            issue_monthly_add=req.issue_monthly_add,
            agent_quota_bonus=req.agent_quota_bonus,
            agent_daily_add=req.agent_daily_add,
            agent_weekly_add=req.agent_weekly_add,
            agent_monthly_add=req.agent_monthly_add,
            description=req.description,
            sort_order=req.sort_order,
        )
        add_billing_admin_audit(
            db,
            actor_id=user["user_id"],
            action="billing_create_plan",
            target_id=str(plan.id),
            detail={
                "credit_grant": str(plan.credit_grant),
                "rate_limits": plan.rate_limits,
                "concurrency_limit": plan.concurrency_limit,
            },
        )
        await db.commit()
        return {"id": plan.id, "name": plan.name}
    except (PaymentError, ValueError) as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/admin/plans/{plan_id}")
async def update_plan(
    plan_id: int,
    req: PlanUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """编辑套餐"""
    svc = PaymentService(db)
    try:
        updates = req.model_dump(exclude_unset=True, exclude_none=True)
        if "concurrency_limit" in req.model_fields_set:
            updates["concurrency_limit"] = req.concurrency_limit
        plan = await svc.update_plan(plan_id, **updates)
        add_billing_admin_audit(
            db,
            actor_id=user["user_id"],
            action="billing_update_plan",
            target_id=str(plan.id),
            detail={
                "credit_grant": str(plan.credit_grant),
                "rate_limits": plan.rate_limits,
                "concurrency_limit": plan.concurrency_limit,
            },
        )
        await db.commit()
        return {
            "id": plan.id,
            "name": plan.name,
            "plan_type": plan.plan_type,
            "is_active": plan.is_active,
        }
    except (PaymentError, ValueError) as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/admin/plans/{plan_id}")
async def delete_plan(
    plan_id: int,
    hard: bool = Query(False, description="Hard delete (remove from database)"),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """删除套餐（默认软删除，hard=true 时硬删除）"""
    svc = PaymentService(db)
    try:
        plan = await svc.delete_plan(plan_id, hard_delete=hard)
        await db.commit()
        return {
            "success": True,
            "id": plan_id,
            "name": plan.name,
            "hard_delete": hard,
        }
    except PaymentError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/admin/codes/generate")
async def generate_codes(
    req: GenerateCodesRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """批量生成兑换码"""
    svc = PaymentService(db)
    try:
        codes = await svc.generate_redeem_codes(
            plan_id=req.plan_id,
            count=req.count,
            batch_name=req.batch_name,
            max_uses=req.max_uses,
            created_by=user["user_id"],
        )
        await db.commit()
        return {"count": len(codes), "codes": [c.code for c in codes]}
    except PaymentError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/admin/codes/{code_id}")
async def update_redeem_code(
    code_id: int,
    req: RedeemCodeUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """编辑兑换码"""
    svc = PaymentService(db)
    try:
        code = await svc.update_redeem_code(
            code_id, **req.model_dump(exclude_none=True)
        )
        await db.commit()
        return {
            "success": True,
            "id": code.id,
            "code": code.code,
            "status": code.status,
        }
    except PaymentError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/admin/codes/{code_id}")
async def delete_redeem_code(
    code_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """删除兑换码"""
    svc = PaymentService(db)
    try:
        code = await svc.delete_redeem_code(code_id)
        await db.commit()
        return {"success": True, "id": code_id, "code": code.code}
    except PaymentError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/admin/grant")
async def grant_plan(
    req: GrantRequest,
    request_key: str | None = Header(
        None, alias="Idempotency-Key", min_length=1, max_length=191
    ),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """手动为用户充值"""
    if req.idempotency_key and request_key and req.idempotency_key != request_key:
        raise HTTPException(status_code=400, detail="Conflicting idempotency keys")
    # Old clients may omit the key. Each independent request is a new grant;
    # replay clients must reuse the key returned here (or send their own).
    key = req.idempotency_key or request_key or str(uuid4())
    svc = PaymentService(db)
    try:
        order = await svc.grant_plan_to_user(
            user_id=req.user_id,
            plan_id=req.plan_id,
            operator_id=user["user_id"],
            idempotency_key=key,
        )
        await db.commit()
        return {"success": True, "order_no": order.order_no, "idempotency_key": key}
    except PaymentError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


class WalletThresholdRequest(BaseModel):
    credits: Decimal = Field(..., ge=0, decimal_places=6, max_digits=24)

    @field_validator("credits", mode="before")
    @classmethod
    def reject_float(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("Credits must be a quoted decimal string")
        return value


class CreditAdjustmentRequest(BaseModel):
    credits: Decimal = Field(..., decimal_places=6, max_digits=24)
    idempotency_key: str = Field(..., min_length=1, max_length=191)
    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("credits", mode="before")
    @classmethod
    def reject_float(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("Credits must be a quoted decimal string")
        return value


class PriceProfileRequest(BaseModel):
    account_id: str | None = Field(None, min_length=1, max_length=128)
    provider_id: str = Field("", max_length=128)
    model_id: str = Field(..., min_length=1, max_length=255)
    call_kind: str = Field(..., min_length=1, max_length=32)
    config: dict


@router.get("/wallet")
async def get_wallet(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """The authenticated user's wallet; no client-specified owner."""
    return await BillingViewService(db).wallet(user["user_id"])


@router.put("/wallet/threshold")
async def set_wallet_threshold(
    req: WalletThresholdRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    try:
        await BillingService(db).set_low_balance_threshold(user["user_id"], req.credits)
        await db.commit()
        return await BillingViewService(db).wallet(user["user_id"])
    except (BillingError, ValueError) as exc:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail={
                "code": getattr(exc, "code", "invalid_billing_config"),
                "message": str(exc),
            },
        ) from exc


@router.get("/transactions")
async def list_transactions(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    feature: str | None = Query(
        None, pattern="^(|" + "|".join(BILLING_FEATURES) + ")$"
    ),
    kind: str | None = Query(None, pattern="^(|" + "|".join(TRANSACTION_KINDS) + ")$"),
):
    return await BillingViewService(db).transactions(
        user,
        offset=offset,
        limit=limit,
        feature=feature,
        kind=kind,
    )


@router.get("/operations")
async def list_billing_operations(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    feature: str | None = Query(
        None, pattern="^(|" + "|".join(BILLING_FEATURES) + ")$"
    ),
    status: str | None = Query(None, max_length=40),
):
    return await BillingViewService(db).operations(
        user,
        offset=offset,
        limit=limit,
        feature=feature,
        status=status,
    )


@router.get("/admin/pricing")
async def list_price_profiles(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return await BillingViewService(db).prices(offset=offset, limit=limit)


@router.post("/admin/pricing")
async def publish_price_profile(
    req: PriceProfileRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    try:
        from backend.services.billing_account_pricing_service import get_pricing_account

        provider_id = req.provider_id
        if req.account_id:
            account = await get_pricing_account(req.account_id)
            if account is None or not account.enabled:
                raise BillingError(
                    "Configured AI account is unavailable", "account_unavailable"
                )
            provider_id = account.provider_id
        profile = await BillingService(db).publish_price(
            provider_id,
            req.model_id,
            req.call_kind,
            req.config,
            actor_id=user["user_id"],
            account_id=req.account_id,
        )
        add_billing_admin_audit(
            db,
            actor_id=user["user_id"],
            action="billing_publish_price",
            target_id=str(profile.id),
            detail={
                "provider_id": provider_id,
                "account_id": req.account_id,
                "model_id": req.model_id,
                "call_kind": profile.call_kind,
                "requested_call_kind": req.call_kind,
                "version": profile.version,
            },
        )
        await db.commit()
        return {"id": profile.id, "version": profile.version}
    except (BillingError, ValueError) as exc:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail={
                "code": getattr(exc, "code", "invalid_billing_config"),
                "message": str(exc),
            },
        ) from exc


@router.post("/admin/wallets/{user_id}/adjust")
async def adjust_wallet(
    user_id: int,
    req: CreditAdjustmentRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    from backend.models.telegram_models import TelegramUser

    if await db.get(TelegramUser, user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        transaction = await BillingService(db).adjust(
            user_id,
            req.credits,
            idempotency_key=req.idempotency_key,
            actor_id=user["user_id"],
            reason=req.reason,
        )
        add_billing_admin_audit(
            db,
            actor_id=user["user_id"],
            action="billing_adjust_wallet",
            target_id=str(user_id),
            detail={
                "transaction_id": transaction.id,
                "credits": str(req.credits),
                "reason": req.reason,
            },
        )
        await db.commit()
        return {
            "transaction_id": transaction.id,
            "wallet": await BillingViewService(db).wallet(user_id),
        }
    except (BillingError, ValueError) as exc:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail={
                "code": getattr(exc, "code", "invalid_billing_config"),
                "message": str(exc),
            },
        ) from exc


@router.get("/usage")
async def list_owned_usage(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    operation_id: str | None = Query(None, max_length=128),
    feature: str | None = Query(
        None, pattern="^(|" + "|".join(BILLING_FEATURES) + ")$"
    ),
):
    """Own metering metadata, with no raw prompts/responses or endpoint secrets."""
    from backend.services.ai_usage_service import fetch_usage_summary

    rows = await fetch_usage_summary(
        db,
        user_id=user["user_id"],
        operation_id=operation_id,
        feature=feature or None,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    for item in items:
        if item.get("last_occurred_at"):
            item["last_occurred_at"] = format_rfc3339(item["last_occurred_at"])
    return {"items": items, "offset": offset, "limit": limit, "has_more": has_more}


@router.get("/notices")
async def list_billing_notices(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    return await BillingViewService(db).notices(
        user["user_id"], offset=offset, limit=limit
    )


@router.post("/notices/{notice_id}/read")
async def mark_billing_notice_read(
    notice_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    if not await BillingViewService(db).read_notice(user["user_id"], notice_id):
        raise HTTPException(status_code=404, detail="Notice not found")
    await db.commit()
    return {"success": True}


class PaymentEventResolutionRequest(BaseModel):
    evidence: str = Field(..., min_length=1, max_length=2000)
    order_id: int | None = Field(None, ge=1)
    checkout_amount_cents: int | None = Field(None, ge=1)
    checkout_currency: str | None = Field(None, min_length=3, max_length=10)
    refund_reference_id: str | None = Field(None, min_length=1, max_length=191)
    refund_amount_cents: int | None = Field(None, ge=1)
    refund_currency: str | None = Field(None, min_length=3, max_length=10)

    @field_validator("checkout_amount_cents", "refund_amount_cents", mode="before")
    @classmethod
    def reject_float_money(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("Payment amounts must be integer currency minor units")
        return value


@router.get("/admin/payment-events")
async def list_pending_payment_events(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return await pending_payment_events(db, limit=limit, offset=offset)


@router.post("/admin/payment-events/{event_id}/replay")
async def replay_payment_event(
    event_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    from backend.services.payment_event_service import PaymentEventService

    try:
        record = await PaymentEventService(db).replay(event_id)
        add_billing_admin_audit(
            db,
            actor_id=user["user_id"],
            action="billing_replay_payment",
            target_id=str(event_id),
            detail={"status": record.status},
        )
        await db.commit()
        return payment_event_summary(record)
    except (PaymentError, BillingError, ValueError) as exc:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail={
                "code": getattr(exc, "code", "invalid_payment_evidence"),
                "message": str(exc),
            },
        ) from exc


@router.post("/admin/payment-events/{event_id}/resolve")
async def resolve_payment_event(
    event_id: int,
    req: PaymentEventResolutionRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    from backend.services.payment_event_service import PaymentEventService

    try:
        record = await PaymentEventService(db).resolve(
            event_id, operator_id=user["user_id"], **req.model_dump(exclude_none=True)
        )
        await db.commit()
        return payment_event_summary(record)
    except (PaymentError, BillingError, ValueError) as exc:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail={
                "code": getattr(exc, "code", "invalid_payment_evidence"),
                "message": str(exc),
            },
        ) from exc
