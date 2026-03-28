"""WebUI 用户管理路由"""

from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from loguru import logger
from sqlalchemy import select, func, desc, or_, String, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.telegram_models import TelegramUser, QuotaUsageLog
from backend.webui.deps import require_admin, get_db, get_templates, get_csrf_serializer, require_csrf, get_user_preferences, paginate, error_page

router = APIRouter(prefix="/users", tags=["WebUI Users"])
templates = get_templates()


@router.get("/")
async def user_list_page(
    request: Request,
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染用户列表页面"""
    return templates.TemplateResponse("users.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "users",
        "user_prefs": user_prefs,
    })


@router.get("/list-fragment")
async def user_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
    search: str = Query("", description="搜索关键词（用户名/Telegram ID）"),
    role: str = Query("", description="按角色过滤"),
    page: int = Query(1, ge=1),
    per_page: int = Query(None, ge=1, le=100),
) -> HTMLResponse:
    """用户列表 HTMX 片段（支持搜索、过滤、分页）"""
    if per_page is None:
        per_page = user_prefs["items_per_page"]
    query = select(TelegramUser)
    count_query = select(func.count(TelegramUser.id))

    # 搜索过滤
    if search:
        escaped = search.replace("%", r"\%").replace("_", r"\_")
        search_filter = or_(
            TelegramUser.github_username.ilike(f"%{escaped}%", escape="\\"),
            type_coerce(TelegramUser.telegram_id, String).ilike(f"%{escaped}%", escape="\\"),
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    # 角色过滤
    if role:
        query = query.where(TelegramUser.role == role)
        count_query = count_query.where(TelegramUser.role == role)

    # 排序
    query = query.order_by(desc(TelegramUser.created_at))

    # 分页
    users, total, total_pages, page = await paginate(db, query, count_query, page, per_page)

    return templates.TemplateResponse("components/user_list_fragment.html", {
        "request": request,
        "users": users,
        "search": search,
        "role": role,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "per_page": per_page,
    })


@router.get("/{user_id}")
async def user_detail_page(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
) -> HTMLResponse:
    """用户详情页面"""
    result = await db.execute(
        select(TelegramUser).where(TelegramUser.id == user_id)
    )
    target_user = result.scalar_one_or_none()
    if not target_user:
        return error_page(request, message="用户不存在", user=user)

    # 查询配额使用历史（最近 20 条）
    logs_result = await db.execute(
        select(QuotaUsageLog)
        .where(QuotaUsageLog.telegram_user_id == user_id)
        .order_by(desc(QuotaUsageLog.created_at))
        .limit(20)
    )
    usage_logs = logs_result.scalars().all()

    return templates.TemplateResponse("user_detail.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "users",
        "user_prefs": user_prefs,
        "target_user": target_user,
        "usage_logs": usage_logs,
    })


@router.post("/{user_id}/role")
async def update_user_role(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Depends(require_csrf),  # 依赖注入，非表单字段
    role: str = Form(...),
) -> RedirectResponse:
    """修改用户角色"""
    if role not in ("user", "admin", "super_admin"):
        return RedirectResponse(url=f"/webui/users/{user_id}?error=1", status_code=302)

    result = await db.execute(
        select(TelegramUser).where(TelegramUser.id == user_id)
    )
    target_user = result.scalar_one_or_none()
    if not target_user:
        return error_page(request, message="用户不存在", user=user)

    # 权限保护：不允许修改同级别或更高级别的用户
    if target_user.role in ("admin", "super_admin") and user["role"] != "super_admin":
        return RedirectResponse(url=f"/webui/users/{user_id}?error=1", status_code=302)
    # 不允许设置比自己当前角色更高的权限
    if role == "super_admin" and user["role"] != "super_admin":
        return RedirectResponse(url=f"/webui/users/{user_id}?error=1", status_code=302)

    old_role = target_user.role
    target_user.role = role
    await db.commit()

    logger.info(f"用户角色已变更: user={target_user.github_username}, {old_role} -> {role}, by={user['sub']}")
    return RedirectResponse(url=f"/webui/users/{user_id}?saved=1", status_code=302)


@router.post("/{user_id}/quota")
async def update_user_quota(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Depends(require_csrf),  # 依赖注入，非表单字段
    daily_quota: int = Form(...),
    weekly_quota: int = Form(...),
    monthly_quota: int = Form(...),
) -> RedirectResponse:
    """修改用户配额"""
    if daily_quota < 0 or weekly_quota < 0 or monthly_quota < 0:
        return RedirectResponse(url=f"/webui/users/{user_id}?error=1", status_code=302)

    result = await db.execute(
        select(TelegramUser).where(TelegramUser.id == user_id)
    )
    target_user = result.scalar_one_or_none()
    if not target_user:
        return error_page(request, message="用户不存在", user=user)

    target_user.daily_quota = daily_quota
    target_user.weekly_quota = weekly_quota
    target_user.monthly_quota = monthly_quota
    await db.commit()

    logger.info(f"用户配额已变更: user={target_user.github_username}, daily={daily_quota}, weekly={weekly_quota}, monthly={monthly_quota}, by={user['sub']}")
    return RedirectResponse(url=f"/webui/users/{user_id}?saved=1", status_code=302)


@router.post("/{user_id}/toggle")
async def toggle_user_status(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Depends(require_csrf),  # 依赖注入，非表单字段
) -> RedirectResponse:
    """启用/禁用用户"""
    result = await db.execute(
        select(TelegramUser).where(TelegramUser.id == user_id)
    )
    target_user = result.scalar_one_or_none()
    if not target_user:
        return error_page(request, message="用户不存在", user=user)

    # 权限保护：不允许修改同级别或更高级别的用户，不允许禁用自己
    if user_id == user["user_id"]:
        return RedirectResponse(url="/webui/users/?error=1", status_code=302)
    if target_user.role in ("admin", "super_admin") and user["role"] != "super_admin":
        return RedirectResponse(url="/webui/users/?error=1", status_code=302)

    target_user.is_active = not target_user.is_active
    await db.commit()

    status = "启用" if target_user.is_active else "禁用"
    logger.info(f"用户状态已变更: user={target_user.github_username}, status={status}, by={user['sub']}")
    return RedirectResponse(url="/webui/users/?saved=1", status_code=302)
