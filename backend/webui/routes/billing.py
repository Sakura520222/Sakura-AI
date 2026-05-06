"""WebUI 付费配额路由"""

from fastapi import APIRouter, Request, Depends, Form
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.payment_service import PaymentError, PaymentService
from backend.webui.deps import (
    require_auth,
    require_super_admin,
    get_db,
    get_templates,
    get_csrf_serializer,
    require_csrf,
    get_user_preferences,
    require_payment_enabled,
    toast_redirect,
    render_template,
)
from backend.webui.i18n import detect_language

router = APIRouter(
    prefix="/billing",
    tags=["WebUI Billing"],
    dependencies=[Depends(require_payment_enabled)],
)
templates = get_templates()


def _parse_page(value: str | None) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


@router.get("/")
async def billing_index(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """套餐中心首页"""
    svc = PaymentService(db)
    plans = await svc.list_plans(active_only=True)

    from backend.models.telegram_models import TelegramUser

    db_user = await db.get(TelegramUser, user["user_id"])

    page = _parse_page(request.query_params.get("page"))
    per_page = user_prefs.get("items_per_page", 20)
    offset = (page - 1) * per_page
    orders, total = await svc.list_user_orders(
        user["user_id"], limit=per_page, offset=offset
    )

    return render_template(
        "billing/index.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="billing",
        plans=plans,
        db_user=db_user,
        orders=orders,
        total=total,
        page=page,
        per_page=per_page,
    )


@router.post("/redeem")
async def redeem_code(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
    code: str = Form(...),
):
    """兑换码兑换"""
    code = code.strip().upper()
    if not code:
        return toast_redirect(
            "/webui/billing/", "toast.code_required", "error", lang=detect_language()
        )

    svc = PaymentService(db)
    try:
        order = await svc.redeem_code(user["user_id"], code)
        await db.commit()
        logger.info(f"User {user['sub']} redeemed code {code}, order {order.order_no}")
        return toast_redirect(
            "/webui/billing/",
            "toast.redeem_success",
            lang=detect_language(),
            order_no=order.order_no,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/webui/billing/",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


# ========== Admin: Plan Management ==========


@router.get("/admin/plans")
async def admin_plans(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """管理员套餐管理页面"""
    svc = PaymentService(db)
    plans = await svc.list_plans(active_only=False)

    return render_template(
        "billing/admin_plans.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="billing_admin",
        plans=plans,
    )


@router.post("/admin/plans")
async def admin_create_plan(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    name: str = Form(...),
    plan_type: str = Form(...),
    price_cents: int = Form(0),
    duration_days: int = Form(None),
    pr_quota_bonus: int = Form(0),
    pr_daily_add: int = Form(0),
    pr_weekly_add: int = Form(0),
    pr_monthly_add: int = Form(0),
    issue_quota_bonus: int = Form(0),
    issue_daily_add: int = Form(0),
    issue_weekly_add: int = Form(0),
    issue_monthly_add: int = Form(0),
    description: str = Form(None),
    sort_order: int = Form(0),
):
    """创建套餐"""
    svc = PaymentService(db)
    try:
        await svc.create_plan(
            name=name,
            plan_type=plan_type,
            price_cents=price_cents,
            duration_days=duration_days if duration_days else None,
            pr_quota_bonus=pr_quota_bonus,
            pr_daily_add=pr_daily_add,
            pr_weekly_add=pr_weekly_add,
            pr_monthly_add=pr_monthly_add,
            issue_quota_bonus=issue_quota_bonus,
            issue_daily_add=issue_daily_add,
            issue_weekly_add=issue_weekly_add,
            issue_monthly_add=issue_monthly_add,
            description=description if description else None,
            sort_order=sort_order,
        )
        await db.commit()
        return toast_redirect(
            "/webui/billing/admin/plans", "toast.plan_created", lang=detect_language()
        )
    except Exception as e:
        await db.rollback()
        return toast_redirect(
            "/webui/billing/admin/plans",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/admin/plans/{plan_id}/toggle")
async def admin_toggle_plan(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """启用/禁用套餐"""
    svc = PaymentService(db)
    plan = await svc.get_plan(plan_id)
    if not plan:
        return toast_redirect(
            "/webui/billing/admin/plans",
            "toast.plan_not_found",
            "error",
            lang=detect_language(),
        )
    plan.is_active = not plan.is_active
    await db.commit()
    status = "enabled" if plan.is_active else "disabled"
    return toast_redirect(
        "/webui/billing/admin/plans",
        "toast.plan_toggled",
        lang=detect_language(),
        status=status,
    )


# ========== 管理员：兑换码管理 ==========


@router.get("/admin/codes")
async def admin_codes(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """管理员兑换码管理页面"""
    svc = PaymentService(db)
    plans = await svc.list_plans(active_only=True)
    page = _parse_page(request.query_params.get("page"))
    per_page = user_prefs.get("items_per_page", 20)
    offset = (page - 1) * per_page
    codes, total = await svc.list_redeem_codes(limit=per_page, offset=offset)

    return render_template(
        "billing/admin_codes.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="billing_admin",
        plans=plans,
        codes=codes,
        total=total,
        page=page,
        per_page=per_page,
    )


@router.post("/admin/codes/generate")
async def admin_generate_codes(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    plan_id: int = Form(...),
    count: int = Form(...),
    batch_name: str = Form(None),
    max_uses: int = Form(1),
):
    """批量生成兑换码"""
    if count < 1 or count > 100:
        return toast_redirect(
            "/webui/billing/admin/codes",
            "toast.code_count_range",
            "error",
            lang=detect_language(),
        )

    svc = PaymentService(db)
    try:
        codes = await svc.generate_redeem_codes(
            plan_id=plan_id,
            count=count,
            batch_name=batch_name if batch_name else None,
            max_uses=max_uses,
            created_by=user["user_id"],
        )
        await db.commit()
        logger.info(f"Admin {user['sub']} generated {count} codes for plan {plan_id}")
        return toast_redirect(
            "/webui/billing/admin/codes",
            "toast.code_generated",
            lang=detect_language(),
            count=len(codes),
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/webui/billing/admin/codes",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


# ========== Admin: Manual Grant ==========


@router.post("/admin/grant")
async def admin_grant(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_id: int = Form(...),
    plan_id: int = Form(...),
):
    """手动为用户充值套餐"""
    svc = PaymentService(db)
    try:
        order = await svc.grant_plan_to_user(
            user_id=user_id,
            plan_id=plan_id,
            operator_id=user["user_id"],
        )
        await db.commit()
        logger.info(f"Admin {user['sub']} granted plan {plan_id} to user {user_id}")
        return toast_redirect(
            f"/webui/users/{user_id}",
            "toast.grant_success",
            lang=detect_language(),
            order_no=order.order_no,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            f"/webui/users/{user_id}",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )
