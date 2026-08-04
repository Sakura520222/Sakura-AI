"""API v1 付费配额端点"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_auth, require_api_super_admin
from backend.services.payment import SUPPORTED_PROVIDERS
from backend.services.payment_service import PaymentError, PaymentService
from backend.webui.deps import get_db, require_payment_enabled

router = APIRouter(
    prefix="/billing",
    tags=["Billing"],
    dependencies=[Depends(require_payment_enabled)],
)


# ========== Schemas ==========


class PlanCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    plan_type: str = Field(..., pattern="^(one_time|subscription)$")
    price_cents: int = Field(0, ge=0)
    currency: str = Field("CNY", max_length=10)
    duration_days: int | None = None
    pr_quota_bonus: int = Field(0, ge=0)
    pr_daily_add: int = Field(0, ge=0)
    pr_weekly_add: int = Field(0, ge=0)
    pr_monthly_add: int = Field(0, ge=0)
    issue_quota_bonus: int = Field(0, ge=0)
    issue_daily_add: int = Field(0, ge=0)
    issue_weekly_add: int = Field(0, ge=0)
    issue_monthly_add: int = Field(0, ge=0)
    description: str | None = None
    sort_order: int = Field(0, ge=0)


class RedeemRequest(BaseModel):
    code: str = Field(..., min_length=1)


class GrantRequest(BaseModel):
    user_id: int
    plan_id: int


class GenerateCodesRequest(BaseModel):
    plan_id: int
    count: int = Field(..., ge=1, le=100)
    batch_name: str | None = None
    max_uses: int = Field(1, ge=1)


class PlanUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    plan_type: str | None = Field(None, pattern="^(one_time|subscription)$")
    price_cents: int | None = Field(None, ge=0)
    currency: str | None = Field(None, max_length=10)
    duration_days: int | None = None
    pr_quota_bonus: int | None = Field(None, ge=0)
    pr_daily_add: int | None = Field(None, ge=0)
    pr_weekly_add: int | None = Field(None, ge=0)
    pr_monthly_add: int | None = Field(None, ge=0)
    issue_quota_bonus: int | None = Field(None, ge=0)
    issue_daily_add: int | None = Field(None, ge=0)
    issue_weekly_add: int | None = Field(None, ge=0)
    issue_monthly_add: int | None = Field(None, ge=0)
    is_active: bool | None = None
    sort_order: int | None = Field(None, ge=0)
    description: str | None = None


class RedeemCodeUpdateRequest(BaseModel):
    status: str | None = Field(None, pattern="^(active|disabled)$")
    expires_at: datetime | None = None
    max_uses: int | None = Field(None, ge=1)
    plan_id: int | None = None


class CreateOrderRequest(BaseModel):
    plan_id: int
    provider: str = Field(
        "stripe",
        pattern="^(" + "|".join(SUPPORTED_PROVIDERS) + ")$",
    )


class RefundRequest(BaseModel):
    amount_cents: int | None = None


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
            "currency": p.currency,
            "duration_days": p.duration_days,
            "pr_quota_bonus": p.pr_quota_bonus,
            "pr_daily_add": p.pr_daily_add,
            "pr_weekly_add": p.pr_weekly_add,
            "pr_monthly_add": p.pr_monthly_add,
            "issue_quota_bonus": p.issue_quota_bonus,
            "issue_daily_add": p.issue_daily_add,
            "issue_weekly_add": p.issue_weekly_add,
            "issue_monthly_add": p.issue_monthly_add,
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
                "plan_name": o.plan.name if o.plan else None,
                "amount_cents": o.amount_cents,
                "currency": o.currency,
                "status": o.status,
                "payment_provider": o.payment_provider,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "fulfilled_at": o.fulfilled_at.isoformat() if o.fulfilled_at else None,
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
            "currency": order.currency,
            "provider": order.payment_provider,
            "checkout_url": checkout_url,
            "expires_at": order.expires_at.isoformat() if order.expires_at else None,
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
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "id": order.id,
        "order_no": order.order_no,
        "status": order.status,
        "amount_cents": order.amount_cents,
        "currency": order.currency,
        "payment_provider": order.payment_provider,
        "provider_tx_id": order.provider_tx_id,
        "checkout_url": checkout_url,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "fulfilled_at": order.fulfilled_at.isoformat() if order.fulfilled_at else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "expires_at": order.expires_at.isoformat() if order.expires_at else None,
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
    plan = await svc.create_plan(
        name=req.name,
        plan_type=req.plan_type,
        price_cents=req.price_cents,
        currency=req.currency,
        duration_days=req.duration_days,
        pr_quota_bonus=req.pr_quota_bonus,
        pr_daily_add=req.pr_daily_add,
        pr_weekly_add=req.pr_weekly_add,
        pr_monthly_add=req.pr_monthly_add,
        issue_quota_bonus=req.issue_quota_bonus,
        issue_daily_add=req.issue_daily_add,
        issue_weekly_add=req.issue_weekly_add,
        issue_monthly_add=req.issue_monthly_add,
        description=req.description,
        sort_order=req.sort_order,
    )
    await db.commit()
    return {"id": plan.id, "name": plan.name}


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
        plan = await svc.update_plan(plan_id, **req.model_dump(exclude_none=True))
        await db.commit()
        return {
            "id": plan.id,
            "name": plan.name,
            "plan_type": plan.plan_type,
            "is_active": plan.is_active,
        }
    except PaymentError as e:
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
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """手动为用户充值"""
    svc = PaymentService(db)
    try:
        order = await svc.grant_plan_to_user(
            user_id=req.user_id,
            plan_id=req.plan_id,
            operator_id=user["user_id"],
        )
        await db.commit()
        return {"success": True, "order_no": order.order_no}
    except PaymentError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
