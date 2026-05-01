"""API v1 付费配额端点"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.payment_models import Plan, Order
from backend.services.payment_service import PaymentService, PaymentError
from backend.webui.deps import get_db
from backend.api.v1.deps import require_api_auth, require_api_super_admin

router = APIRouter(prefix="/billing", tags=["Billing"])


# ========== Schemas ==========


class PlanCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    plan_type: str = Field(..., pattern="^(one_time|subscription)$")
    price_cents: int = Field(0, ge=0)
    currency: str = Field("CNY", max_length=10)
    duration_days: Optional[int] = None
    pr_quota_bonus: int = Field(0, ge=0)
    pr_daily_add: int = Field(0, ge=0)
    pr_weekly_add: int = Field(0, ge=0)
    pr_monthly_add: int = Field(0, ge=0)
    issue_quota_bonus: int = Field(0, ge=0)
    issue_daily_add: int = Field(0, ge=0)
    issue_weekly_add: int = Field(0, ge=0)
    issue_monthly_add: int = Field(0, ge=0)
    description: Optional[str] = None
    sort_order: int = Field(0, ge=0)


class RedeemRequest(BaseModel):
    code: str = Field(..., min_length=1)


class GrantRequest(BaseModel):
    user_id: int
    plan_id: int


class GenerateCodesRequest(BaseModel):
    plan_id: int
    count: int = Field(..., ge=1, le=100)
    batch_name: Optional[str] = None
    max_uses: int = Field(1, ge=1)


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
    orders, total = await svc.list_user_orders(user["user_id"], limit=limit, offset=offset)
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
