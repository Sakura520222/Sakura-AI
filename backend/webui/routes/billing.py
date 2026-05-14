"""WebUI 付费配额路由"""

from datetime import datetime

from fastapi import APIRouter, Request, Depends, Form
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.payment_service import PaymentError, PaymentService, RedeemCodeStatus
from backend.services.quota_service import QuotaService
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
from backend.webui.helpers.admin_log import log_admin_action
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
    if db_user:
        await QuotaService(db).reset_user_quotas_if_expired(db_user)

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
            "/billing/", "toast.code_required", "error", lang=detect_language()
        )

    svc = PaymentService(db)
    try:
        order = await svc.redeem_code(user["user_id"], code)
        await db.commit()
        logger.info(f"User {user['sub']} redeemed code {code}, order {order.order_no}")
        return toast_redirect(
            "/billing/",
            "toast.redeem_success",
            lang=detect_language(),
            order_no=order.order_no,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


# ========== 管理员：套餐管理 ==========


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
        active_page="billing_admin_plans",
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
            "/billing/admin/plans", "toast.plan_created", lang=detect_language()
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/plans",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to create plan: {e}")
        return toast_redirect(
            "/billing/admin/plans",
            "toast.save_failed",
            "error",
            lang=detect_language(),
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
            "/billing/admin/plans",
            "toast.plan_not_found",
            "error",
            lang=detect_language(),
        )
    plan.is_active = not plan.is_active
    await db.commit()
    status = "enabled" if plan.is_active else "disabled"
    return toast_redirect(
        "/billing/admin/plans",
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
        active_page="billing_admin_codes",
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
            "/billing/admin/codes",
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
            "/billing/admin/codes",
            "toast.code_generated",
            lang=detect_language(),
            count=len(codes),
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/codes",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


# ========== 管理员：手动充值 ==========


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
            f"/users/{user_id}",
            "toast.grant_success",
            lang=detect_language(),
            order_no=order.order_no,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            f"/users/{user_id}",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


# ========== 管理员：套餐编辑/删除 ==========


@router.post("/admin/plans/{plan_id}/edit")
async def admin_edit_plan(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    name: str = Form(None),
    plan_type: str = Form(None),
    price_cents: int = Form(None),
    duration_days: int = Form(None),
    pr_quota_bonus: int = Form(None),
    pr_daily_add: int = Form(None),
    pr_weekly_add: int = Form(None),
    pr_monthly_add: int = Form(None),
    issue_quota_bonus: int = Form(None),
    issue_daily_add: int = Form(None),
    issue_weekly_add: int = Form(None),
    issue_monthly_add: int = Form(None),
    description: str = Form(None),
    sort_order: int = Form(None),
):
    """编辑套餐"""
    update_data = {}
    form_fields = {
        "name": name,
        "plan_type": plan_type,
        "price_cents": price_cents,
        "duration_days": duration_days,
        "pr_quota_bonus": pr_quota_bonus,
        "pr_daily_add": pr_daily_add,
        "pr_weekly_add": pr_weekly_add,
        "pr_monthly_add": pr_monthly_add,
        "issue_quota_bonus": issue_quota_bonus,
        "issue_daily_add": issue_daily_add,
        "issue_weekly_add": issue_weekly_add,
        "issue_monthly_add": issue_monthly_add,
        "description": description,
        "sort_order": sort_order,
    }
    for field, value in form_fields.items():
        if value is not None:
            update_data[field] = value

    svc = PaymentService(db)
    try:
        plan = await svc.update_plan(plan_id, **update_data)
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="edit_plan",
            target_type="plan",
            target_id=str(plan_id),
            detail={"name": plan.name, "updated_fields": list(update_data.keys())},
        )
        return toast_redirect(
            "/billing/admin/plans", "toast.plan_updated", lang=detect_language()
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/plans",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/admin/plans/{plan_id}/delete")
async def admin_delete_plan(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    hard_delete: str = Form(None),
):
    """删除套餐（默认软删除，勾选 hard_delete 时硬删除）"""
    is_hard = hard_delete == "on"
    svc = PaymentService(db)
    try:
        plan = await svc.delete_plan(plan_id, hard_delete=is_hard)
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="delete_plan",
            target_type="plan",
            target_id=str(plan_id),
            detail={"name": plan.name, "hard_delete": is_hard},
        )
        toast_key = "toast.plan_hard_deleted" if is_hard else "toast.plan_deleted"
        return toast_redirect("/billing/admin/plans", toast_key, lang=detect_language())
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/plans",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


# ========== 管理员：兑换码编辑/删除 ==========


@router.post("/admin/codes/{code_id}/edit")
async def admin_edit_code(
    code_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    status: str = Form(None),
    expires_at: str = Form(None),
    max_uses: int = Form(None),
    plan_id: int = Form(None),
):
    """编辑兑换码"""
    update_data = {}
    if status is not None:
        update_data["status"] = status
    if expires_at is not None and expires_at.strip():
        try:
            update_data["expires_at"] = datetime.fromisoformat(expires_at.strip())
        except (ValueError, TypeError):
            return toast_redirect(
                "/billing/admin/codes",
                "toast.invalid_param",
                "error",
                lang=detect_language(),
            )
    if max_uses is not None:
        update_data["max_uses"] = max_uses
    if plan_id is not None:
        update_data["plan_id"] = plan_id

    if not update_data:
        return toast_redirect(
            "/billing/admin/codes", "toast.no_changes", lang=detect_language()
        )

    svc = PaymentService(db)
    try:
        code = await svc.update_redeem_code(code_id, **update_data)
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="edit_redeem_code",
            target_type="redeem_code",
            target_id=str(code_id),
            detail={"code": code.code, "updated_fields": list(update_data.keys())},
        )
        return toast_redirect(
            "/billing/admin/codes", "toast.code_updated", lang=detect_language()
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/codes",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/admin/codes/{code_id}/delete")
async def admin_delete_code(
    code_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """删除兑换码"""
    svc = PaymentService(db)
    try:
        code = await svc.delete_redeem_code(code_id)
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="delete_redeem_code",
            target_type="redeem_code",
            target_id=str(code_id),
            detail={"code": code.code},
        )
        return toast_redirect(
            "/billing/admin/codes", "toast.code_deleted", lang=detect_language()
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/codes",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


# ========== 管理员：套餐批量操作 ==========


@router.post("/admin/plans/batch-toggle")
async def admin_batch_toggle_plans(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """批量切换套餐启用/禁用状态"""
    form = await request.form()
    raw = form.get("plan_ids", "")
    plan_ids = [int(v.strip()) for v in raw.split(",") if v.strip()]
    if not plan_ids:
        return toast_redirect(
            "/billing/admin/plans",
            "toast.batch_no_selection",
            "error",
            lang=detect_language(),
        )

    svc = PaymentService(db)
    try:
        result = await svc.batch_toggle_plans(plan_ids)
        await db.commit()
        success_count = len(result["success"])
        skipped_count = len(result["skipped"])
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="batch_toggle_plans",
            target_type="plan",
            detail={"plan_ids": plan_ids, "success_count": success_count, "skipped_count": skipped_count},
        )
        toast_key = "toast.batch_partial_success" if skipped_count > 0 else "toast.batch_toggle_success"
        return toast_redirect(
            "/billing/admin/plans",
            toast_key,
            lang=detect_language(),
            count=success_count,
            skipped=skipped_count,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/plans",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/admin/plans/batch-delete")
async def admin_batch_delete_plans(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    hard_delete: str = Form(None),
):
    """批量删除套餐"""
    form = await request.form()
    raw = form.get("plan_ids", "")
    plan_ids = [int(v.strip()) for v in raw.split(",") if v.strip()]
    if not plan_ids:
        return toast_redirect(
            "/billing/admin/plans",
            "toast.batch_no_selection",
            "error",
            lang=detect_language(),
        )

    is_hard = hard_delete == "on"
    svc = PaymentService(db)
    try:
        result = await svc.batch_delete_plans(plan_ids, hard_delete=is_hard)
        success_count = len(result["success"])
        failed_count = len(result["failed"])
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="batch_delete_plans",
            target_type="plan",
            detail={"plan_ids": plan_ids, "hard_delete": is_hard, "success_count": success_count, "failed_count": failed_count},
        )
        toast_key = "toast.batch_partial_success" if failed_count > 0 else ("toast.batch_hard_deleted" if is_hard else "toast.batch_deleted")
        return toast_redirect(
            "/billing/admin/plans",
            toast_key,
            lang=detect_language(),
            count=success_count,
            skipped=failed_count,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/plans",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


# ========== 管理员：兑换码批量操作 ==========


@router.post("/admin/codes/batch-disable")
async def admin_batch_disable_codes(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """批量禁用兑换码"""
    form = await request.form()
    raw = form.get("code_ids", "")
    code_ids = [int(v.strip()) for v in raw.split(",") if v.strip()]
    if not code_ids:
        return toast_redirect(
            "/billing/admin/codes",
            "toast.batch_no_selection",
            "error",
            lang=detect_language(),
        )

    svc = PaymentService(db)
    try:
        result = await svc.batch_update_redeem_codes(
            code_ids, status=RedeemCodeStatus.DISABLED.value
        )
        success_count = len(result["success"])
        skipped_count = len(result["skipped"])
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="batch_disable_codes",
            target_type="redeem_code",
            detail={"code_ids": code_ids, "success_count": success_count, "skipped_count": skipped_count},
        )
        toast_key = "toast.batch_partial_success" if skipped_count > 0 else "toast.batch_codes_disabled"
        return toast_redirect(
            "/billing/admin/codes",
            toast_key,
            lang=detect_language(),
            count=success_count,
            skipped=skipped_count,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/codes",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/admin/codes/batch-enable")
async def admin_batch_enable_codes(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """批量启用兑换码"""
    form = await request.form()
    raw = form.get("code_ids", "")
    code_ids = [int(v.strip()) for v in raw.split(",") if v.strip()]
    if not code_ids:
        return toast_redirect(
            "/billing/admin/codes",
            "toast.batch_no_selection",
            "error",
            lang=detect_language(),
        )

    svc = PaymentService(db)
    try:
        result = await svc.batch_update_redeem_codes(
            code_ids, status=RedeemCodeStatus.ACTIVE.value
        )
        success_count = len(result["success"])
        skipped_count = len(result["skipped"])
        await db.commit()
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="batch_enable_codes",
            target_type="redeem_code",
            detail={"code_ids": code_ids, "success_count": success_count, "skipped_count": skipped_count},
        )
        toast_key = "toast.batch_partial_success" if skipped_count > 0 else "toast.batch_codes_enabled"
        return toast_redirect(
            "/billing/admin/codes",
            toast_key,
            lang=detect_language(),
            count=success_count,
            skipped=skipped_count,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/codes",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )


@router.post("/admin/codes/batch-delete")
async def admin_batch_delete_codes(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """批量删除兑换码（仅删除未使用的）"""
    form = await request.form()
    raw = form.get("code_ids", "")
    code_ids = [int(v.strip()) for v in raw.split(",") if v.strip()]
    if not code_ids:
        return toast_redirect(
            "/billing/admin/codes",
            "toast.batch_no_selection",
            "error",
            lang=detect_language(),
        )

    svc = PaymentService(db)
    try:
        result = await svc.batch_delete_redeem_codes(code_ids)
        success_count = len(result["success"])
        skipped_count = len(result["skipped"])
        await log_admin_action(
            db,
            admin_id=user["user_id"],
            action="batch_delete_codes",
            target_type="redeem_code",
            detail={"code_ids": code_ids, "success_count": success_count, "skipped_count": skipped_count},
        )
        toast_key = "toast.batch_partial_success" if skipped_count > 0 else "toast.batch_codes_deleted"
        return toast_redirect(
            "/billing/admin/codes",
            toast_key,
            lang=detect_language(),
            count=success_count,
            skipped=skipped_count,
        )
    except PaymentError as e:
        await db.rollback()
        return toast_redirect(
            "/billing/admin/codes",
            "toast.payment_error",
            "error",
            lang=detect_language(),
            error=str(e),
        )
