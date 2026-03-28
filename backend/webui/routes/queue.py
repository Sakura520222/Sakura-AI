"""WebUI 审查队列监控路由"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import PRReview
from backend.webui.deps import (
    require_admin, get_db, get_templates, get_csrf_serializer,
    get_user_preferences, paginate, error_page, build_review_search_filter,
    get_active_repos,
)

router = APIRouter(prefix="/queue", tags=["WebUI Queue"])
templates = get_templates()


@router.get("/")
async def queue_page(
    request: Request,
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """审查队列监控页面（管理员专用）"""
    return templates.TemplateResponse("queue.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "queue",
        "user_prefs": user_prefs,
    })


@router.get("/stats-fragment")
async def queue_stats_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
):
    """统计卡片 HTMX 片段"""
    # 各状态计数
    pending = (await db.execute(
        select(func.count(PRReview.id)).where(PRReview.status == "pending")
    )).scalar() or 0

    processing = (await db.execute(
        select(func.count(PRReview.id)).where(PRReview.status == "processing")
    )).scalar() or 0

    completed = (await db.execute(
        select(func.count(PRReview.id)).where(PRReview.status == "completed")
    )).scalar() or 0

    failed = (await db.execute(
        select(func.count(PRReview.id)).where(PRReview.status == "failed")
    )).scalar() or 0

    # 平均处理耗时（秒）
    avg_seconds = (await db.execute(
        select(func.avg(
            func.unix_timestamp(PRReview.completed_at) - func.unix_timestamp(PRReview.created_at)
        )).where(
            PRReview.status == "completed",
            PRReview.completed_at.isnot(None),
        )
    )).scalar()

    avg_duration = _format_duration(avg_seconds) if avg_seconds else "-"

    return templates.TemplateResponse("components/queue_stats_cards.html", {
        "request": request,
        "pending": pending,
        "processing": processing,
        "completed": completed,
        "failed": failed,
        "avg_duration": avg_duration,
    })


@router.get("/list-fragment")
async def queue_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_admin),
    user_prefs: dict = Depends(get_user_preferences),
    search: str = Query("", description="搜索关键词"),
    repo: str = Query("", description="仓库名过滤"),
    status: str = Query("", description="状态过滤"),
    start_date: str = Query("", description="开始日期"),
    end_date: str = Query("", description="结束日期"),
    page: int = Query(1, ge=1),
    per_page: int = Query(None, ge=1, le=100),
):
    """队列列表 HTMX 片段"""
    if per_page is None:
        per_page = user_prefs["items_per_page"]

    query = select(PRReview)
    count_query = select(func.count(PRReview.id))

    # 搜索过滤
    search_filter = build_review_search_filter(search)
    if search_filter:
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    # 仓库过滤：验证 repo 在活跃仓库列表中
    active_repos = await get_active_repos(db)
    if repo and repo in active_repos:
        query = query.where(PRReview.repo_name == repo)
        count_query = count_query.where(PRReview.repo_name == repo)

    # 状态过滤
    if status:
        query = query.where(PRReview.status == status)
        count_query = count_query.where(PRReview.status == status)

    # 日期范围
    if start_date:
        try:
            sd = datetime.strptime(start_date, "%Y-%m-%d")
            query = query.where(PRReview.created_at >= sd)
            count_query = count_query.where(PRReview.created_at >= sd)
        except ValueError:
            pass
    if end_date:
        try:
            ed = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            query = query.where(PRReview.created_at < ed)
            count_query = count_query.where(PRReview.created_at < ed)
        except ValueError:
            pass

    # 排序
    query = query.order_by(desc(PRReview.created_at))

    # 分页
    reviews, total, total_pages, page = await paginate(db, query, count_query, page, per_page)

    return templates.TemplateResponse("components/queue_list_fragment.html", {
        "request": request,
        "reviews": reviews,
        "active_repos": active_repos,
        "search": search,
        "repo": repo,
        "status": status,
        "start_date": start_date,
        "end_date": end_date,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "per_page": per_page,
    })


def _format_duration(seconds: float) -> str:
    """将秒数格式化为可读字符串"""
    if seconds is None:
        return "-"
    total_seconds = int(seconds)
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes}m"
