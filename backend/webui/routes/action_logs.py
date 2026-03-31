"""WebUI 操作日志路由"""

from fastapi import APIRouter, Request, Depends, Query
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.admin_action_log import AdminActionLog
from backend.models.telegram_models import TelegramUser
from backend.webui.deps import (
    require_admin,
    get_db,
    get_templates,
    get_csrf_serializer,
    get_user_preferences,
)

router = APIRouter(prefix="/logs/actions", tags=["WebUI Action Logs"])
templates = get_templates()

ACTION_LABELS = {
    "user_role": "修改角色",
    "user_quota": "修改配额",
    "user_toggle": "启用/禁用用户",
    "repo_add": "添加仓库",
    "repo_toggle": "启用/禁用仓库",
    "repo_remove": "删除仓库",
    "config_save": "保存配置",
}

TARGET_TYPE_LABELS = {
    "user": "用户",
    "repo": "仓库",
    "config": "配置",
}


@router.get("/")
async def action_logs_page(
    request: Request,
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """操作日志页面（管理员专用）"""
    return templates.TemplateResponse(
        "action_logs.html",
        {
            "request": request,
            "current_user": user,
            "csrf_token": get_csrf_serializer().dumps({}),
            "active_page": "action_logs",
            "user_prefs": user_prefs,
        },
    )


@router.get("/list-fragment")
async def action_log_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
    action: str = Query("", description="操作类型"),
    start_date: str = Query("", description="开始日期"),
    end_date: str = Query("", description="结束日期"),
    page: int = Query(1, ge=1),
    per_page: int = Query(None, ge=1, le=100),
):
    """操作日志 HTMX 片段"""
    from datetime import datetime

    if per_page is None:
        per_page = user_prefs["items_per_page"]

    query = select(AdminActionLog, TelegramUser.github_username).outerjoin(
        TelegramUser, AdminActionLog.admin_id == TelegramUser.id
    )
    count_query = select(func.count(AdminActionLog.id))

    if action:
        query = query.where(AdminActionLog.action == action)
        count_query = count_query.where(AdminActionLog.action == action)
    if start_date:
        try:
            sd = datetime.strptime(start_date, "%Y-%m-%d")
            query = query.where(AdminActionLog.created_at >= sd)
            count_query = count_query.where(AdminActionLog.created_at >= sd)
        except ValueError:
            pass
    if end_date:
        try:
            ed = datetime.strptime(end_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59
            )
            query = query.where(AdminActionLog.created_at <= ed)
            count_query = count_query.where(AdminActionLog.created_at <= ed)
        except ValueError:
            pass

    query = query.order_by(desc(AdminActionLog.created_at))

    # 自定义分页（join 查询不能使用 scalars）
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    result = await db.execute(query.offset((page - 1) * per_page).limit(per_page))
    logs = result.all()

    return templates.TemplateResponse(
        "components/action_log_list_fragment.html",
        {
            "request": request,
            "logs": logs,
            "action": action,
            "start_date": start_date,
            "end_date": end_date,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "per_page": per_page,
            "action_labels": ACTION_LABELS,
            "target_type_labels": TARGET_TYPE_LABELS,
        },
    )
