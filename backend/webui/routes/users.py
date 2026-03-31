"""WebUI 用户管理路由"""

from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from loguru import logger
from sqlalchemy import select, func, desc, or_, String, type_coerce
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.telegram_models import TelegramUser, QuotaUsageLog
from backend.webui.deps import require_admin, require_super_admin, get_db, get_templates, get_csrf_serializer, require_csrf, get_user_preferences, paginate, error_page, toast_redirect
from backend.core.config import get_settings
from backend.webui.helpers.admin_log import log_admin_action

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


@router.post("/add")
async def add_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    telegram_id: int = Form(...),
    github_username: str = Form(...),
    role: str = Form("user"),
    daily_quota: int = Form(10),
    weekly_quota: int = Form(50),
    monthly_quota: int = Form(200),
    issue_daily_quota: int = Form(20),
    issue_weekly_quota: int = Form(80),
    issue_monthly_quota: int = Form(300),
) -> RedirectResponse:
    """添加新用户（仅超级管理员）"""
    # 角色验证
    if role not in ("user", "admin", "super_admin"):
        return toast_redirect("/webui/users/", "无效的角色值", "error")

    # Telegram ID 验证
    if telegram_id <= 0:
        return toast_redirect("/webui/users/", "Telegram ID 必须为正整数", "error")

    # GitHub 用户名验证
    github_username = github_username.strip()
    if not github_username:
        return toast_redirect("/webui/users/", "GitHub 用户名不能为空", "error")

    # 配额验证
    for q in (daily_quota, weekly_quota, monthly_quota, issue_daily_quota, issue_weekly_quota, issue_monthly_quota):
        if q < 0:
            return toast_redirect("/webui/users/", "配额值不能为负数", "error")

    # 检查 Telegram ID 唯一性
    existing = await db.execute(
        select(TelegramUser).where(TelegramUser.telegram_id == telegram_id)
    )
    if existing.scalar_one_or_none():
        return toast_redirect("/webui/users/", f"Telegram ID {telegram_id} 已存在", "error")

    # 检查 GitHub 用户名唯一性
    existing_gh = await db.execute(
        select(TelegramUser).where(TelegramUser.github_username == github_username)
    )
    if existing_gh.scalar_one_or_none():
        return toast_redirect("/webui/users/", f"GitHub 用户名 {github_username} 已被使用", "error")

    # 超级管理员自动检测
    auto_super_admin = False
    settings = get_settings()
    if telegram_id in settings.telegram_admin_ids_list:
        role = "super_admin"
        auto_super_admin = True

    # 创建用户
    new_user = TelegramUser(
        telegram_id=telegram_id,
        github_username=github_username,
        role=role,
        daily_quota=daily_quota,
        weekly_quota=weekly_quota,
        monthly_quota=monthly_quota,
        issue_daily_quota=issue_daily_quota,
        issue_weekly_quota=issue_weekly_quota,
        issue_monthly_quota=issue_monthly_quota,
        is_active=True,
    )
    try:
        db.add(new_user)
        await db.commit()
    except IntegrityError as e:
        logger.error(f"用户创建失败（数据库冲突）: {e}")
        await db.rollback()
        return toast_redirect("/webui/users/", "用户创建失败（可能已存在重复）", "error")
    except Exception as e:
        logger.error(f"用户创建失败（未知错误）: {e}")
        await db.rollback()
        return toast_redirect("/webui/users/", "用户创建失败", "error")

    logger.info(f"用户已通过 WebUI 添加: telegram_id={telegram_id}, github={github_username}, role={role}, by={user['sub']}")
    await log_admin_action(db, user["user_id"], "user_add", "user", str(new_user.id), {
        "telegram_id": telegram_id,
        "github_username": github_username,
        "role": role,
        "daily_quota": daily_quota,
        "weekly_quota": weekly_quota,
        "monthly_quota": monthly_quota,
        "issue_daily_quota": issue_daily_quota,
        "issue_weekly_quota": issue_weekly_quota,
        "issue_monthly_quota": issue_monthly_quota,
        "auto_super_admin": auto_super_admin,
    })
    msg = f"用户 {github_username} 已成功添加"
    if auto_super_admin:
        msg += "（已自动提升为超级管理员）"
    return toast_redirect("/webui/users/", msg)


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
        return toast_redirect(f"/webui/users/{user_id}", "无效的角色值", "error")

    result = await db.execute(
        select(TelegramUser).where(TelegramUser.id == user_id)
    )
    target_user = result.scalar_one_or_none()
    if not target_user:
        return error_page(request, message="用户不存在", user=user)

    # 权限保护：不允许修改同级别或更高级别的用户
    if target_user.role in ("admin", "super_admin") and user["role"] != "super_admin":
        return toast_redirect(f"/webui/users/{user_id}", "权限不足，无法修改此用户的角色", "error")
    # 不允许设置比自己当前角色更高的权限
    if role == "super_admin" and user["role"] != "super_admin":
        return toast_redirect(f"/webui/users/{user_id}", "权限不足，无法设置为超级管理员", "error")

    old_role = target_user.role
    target_user.role = role
    await db.commit()

    logger.info(f"用户角色已变更: user={target_user.github_username}, {old_role} -> {role}, by={user['sub']}")
    await log_admin_action(db, user['user_id'], "user_role", "user", str(user_id), {"old_role": old_role, "new_role": role})
    return toast_redirect(f"/webui/users/{user_id}", f"用户角色已更改为 {role}")


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
        return toast_redirect(f"/webui/users/{user_id}", "配额值不能为负数", "error")

    result = await db.execute(
        select(TelegramUser).where(TelegramUser.id == user_id)
    )
    target_user = result.scalar_one_or_none()
    if not target_user:
        return error_page(request, message="用户不存在", user=user)

    old_daily, old_weekly, old_monthly = target_user.daily_quota, target_user.weekly_quota, target_user.monthly_quota
    target_user.daily_quota = daily_quota
    target_user.weekly_quota = weekly_quota
    target_user.monthly_quota = monthly_quota
    await db.commit()

    logger.info(f"用户配额已变更: user={target_user.github_username}, daily={daily_quota}, weekly={weekly_quota}, monthly={monthly_quota}, by={user['sub']}")
    await log_admin_action(db, user['user_id'], "user_quota", "user", str(user_id), {"old_daily": old_daily, "old_weekly": old_weekly, "old_monthly": old_monthly, "new_daily": daily_quota, "new_weekly": weekly_quota, "new_monthly": monthly_quota})
    return toast_redirect(f"/webui/users/{user_id}", "用户配额已更新")


@router.post("/{user_id}/issue-quota")
async def update_user_issue_quota(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Depends(require_csrf),
    issue_daily_quota: int = Form(...),
    issue_weekly_quota: int = Form(...),
    issue_monthly_quota: int = Form(...),
) -> RedirectResponse:
    """修改用户 Issue 分析配额"""
    if issue_daily_quota < 0 or issue_weekly_quota < 0 or issue_monthly_quota < 0:
        return toast_redirect(f"/webui/users/{user_id}", "配额值不能为负数", "error")

    result = await db.execute(
        select(TelegramUser).where(TelegramUser.id == user_id)
    )
    target_user = result.scalar_one_or_none()
    if not target_user:
        return error_page(request, message="用户不存在", user=user)

    old_daily = target_user.issue_daily_quota
    old_weekly = target_user.issue_weekly_quota
    old_monthly = target_user.issue_monthly_quota
    target_user.issue_daily_quota = issue_daily_quota
    target_user.issue_weekly_quota = issue_weekly_quota
    target_user.issue_monthly_quota = issue_monthly_quota
    await db.commit()

    logger.info(f"用户 Issue 配额已变更: user={target_user.github_username}, daily={issue_daily_quota}, weekly={issue_weekly_quota}, monthly={issue_monthly_quota}, by={user['sub']}")
    await log_admin_action(db, user['user_id'], "user_issue_quota", "user", str(user_id), {"old_daily": old_daily, "old_weekly": old_weekly, "old_monthly": old_monthly, "new_daily": issue_daily_quota, "new_weekly": issue_weekly_quota, "new_monthly": issue_monthly_quota})
    return toast_redirect(f"/webui/users/{user_id}", "Issue 配额已更新")


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
        return toast_redirect("/webui/users/", "不能禁用自己", "error")
    if target_user.role in ("admin", "super_admin") and user["role"] != "super_admin":
        return toast_redirect("/webui/users/", "权限不足，无法修改此用户状态", "error")

    target_user.is_active = not target_user.is_active
    await db.commit()

    status = "启用" if target_user.is_active else "禁用"
    logger.info(f"用户状态已变更: user={target_user.github_username}, status={status}, by={user['sub']}")
    await log_admin_action(db, user['user_id'], "user_toggle", "user", str(user_id), {"is_active": target_user.is_active})
    return toast_redirect("/webui/users/", f"用户 {target_user.github_username} 已{status}")


@router.post("/{user_id}/delete")
async def delete_user(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
) -> RedirectResponse:
    """删除用户（仅超级管理员）"""
    if user_id == user["user_id"]:
        return toast_redirect("/webui/users/", "不能删除自己", "error")

    result = await db.execute(
        select(TelegramUser).where(TelegramUser.id == user_id)
    )
    target_user = result.scalar_one_or_none()
    if not target_user:
        return error_page(request, message="用户不存在", user=user)

    github = target_user.github_username
    tg_id = target_user.telegram_id

    try:
        await db.delete(target_user)
        await db.commit()
    except Exception as e:
        logger.error(f"用户删除失败: {e}")
        await db.rollback()
        return toast_redirect(f"/webui/users/{user_id}", "用户删除失败", "error")

    logger.info(f"用户已通过 WebUI 删除: id={user_id}, github={github}, telegram_id={tg_id}, by={user['sub']}")
    await log_admin_action(db, user['user_id'], "user_delete", "user", str(user_id), {
        "github_username": github,
        "telegram_id": tg_id,
        "role": target_user.role,
    })
    return toast_redirect("/webui/users/", f"用户 {github or tg_id} 已删除")


@router.post("/{user_id}/info")
async def update_user_info(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    telegram_id: int = Form(...),
    github_username: str = Form(...),
) -> RedirectResponse:
    """修改用户基本信息（Telegram ID、GitHub 用户名）"""
    if telegram_id <= 0:
        return toast_redirect(f"/webui/users/{user_id}", "Telegram ID 必须为正整数", "error")

    github_username = github_username.strip()
    if not github_username:
        return toast_redirect(f"/webui/users/{user_id}", "GitHub 用户名不能为空", "error")

    result = await db.execute(
        select(TelegramUser).where(TelegramUser.id == user_id)
    )
    target_user = result.scalar_one_or_none()
    if not target_user:
        return error_page(request, message="用户不存在", user=user)

    # 检查 Telegram ID 唯一性（排除自身）
    existing = await db.execute(
        select(TelegramUser).where(TelegramUser.telegram_id == telegram_id, TelegramUser.id != user_id)
    )
    if existing.scalar_one_or_none():
        return toast_redirect(f"/webui/users/{user_id}", f"Telegram ID {telegram_id} 已被其他用户使用", "error")

    # 检查 GitHub 用户名唯一性（排除自身）
    existing_gh = await db.execute(
        select(TelegramUser).where(TelegramUser.github_username == github_username, TelegramUser.id != user_id)
    )
    if existing_gh.scalar_one_or_none():
        return toast_redirect(f"/webui/users/{user_id}", f"GitHub 用户名 {github_username} 已被其他用户使用", "error")

    old_tg_id = target_user.telegram_id
    old_github = target_user.github_username
    target_user.telegram_id = telegram_id
    target_user.github_username = github_username
    await db.commit()

    logger.info(f"用户信息已变更: id={user_id}, {old_github}->{github_username}, {old_tg_id}->{telegram_id}, by={user['sub']}")
    await log_admin_action(db, user['user_id'], "user_info", "user", str(user_id), {
        "old_telegram_id": old_tg_id, "new_telegram_id": telegram_id,
        "old_github_username": old_github, "new_github_username": github_username,
    })
    return toast_redirect(f"/webui/users/{user_id}", "用户基本信息已更新")


@router.post("/{user_id}/reset-quota")
async def reset_user_quota(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    csrf_token: str = Depends(require_csrf),
) -> RedirectResponse:
    """重置用户配额使用量"""
    result = await db.execute(
        select(TelegramUser).where(TelegramUser.id == user_id)
    )
    target_user = result.scalar_one_or_none()
    if not target_user:
        return error_page(request, message="用户不存在", user=user)

    from datetime import datetime
    now = datetime.utcnow()

    old_used = {
        "daily": target_user.daily_used, "weekly": target_user.weekly_used, "monthly": target_user.monthly_used,
        "issue_daily": target_user.issue_daily_used, "issue_weekly": target_user.issue_weekly_used, "issue_monthly": target_user.issue_monthly_used,
    }

    target_user.daily_used = 0
    target_user.weekly_used = 0
    target_user.monthly_used = 0
    target_user.issue_daily_used = 0
    target_user.issue_weekly_used = 0
    target_user.issue_monthly_used = 0
    target_user.last_reset_daily = now
    target_user.last_reset_weekly = now
    target_user.last_reset_monthly = now
    target_user.last_reset_issue_daily = now
    target_user.last_reset_issue_weekly = now
    target_user.last_reset_issue_monthly = now
    await db.commit()

    logger.info(f"用户配额已重置: user={target_user.github_username}, by={user['sub']}")
    await log_admin_action(db, user['user_id'], "user_reset_quota", "user", str(user_id), {"old_used": old_used})
    return toast_redirect(f"/webui/users/{user_id}", f"用户 {target_user.github_username} 的配额使用量已重置")
