"""WebUI 审查日志路由"""

from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import PRReview, ReviewComment
from backend.models.telegram_models import RepoSubscription
from backend.webui.deps import require_auth, get_db, get_templates, get_csrf_serializer, get_user_preferences, paginate, error_page, get_active_repos, build_review_search_filter

router = APIRouter(prefix="/logs", tags=["WebUI Logs"])
templates = get_templates()


@router.get("/")
async def logs_page(
    request: Request,
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染审查日志页面"""
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "logs",
        "user_prefs": user_prefs,
    })


@router.get("/list-fragment")
async def logs_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
    search: str = Query("", description="搜索关键词（PR标题/仓库名/作者）"),
    repo: str = Query("", description="按仓库过滤"),
    status: str = Query("", description="按状态过滤"),
    date_from: str = Query("", description="开始日期 (YYYY-MM-DD)"),
    date_to: str = Query("", description="结束日期 (YYYY-MM-DD)"),
    page: int = Query(1, ge=1),
    per_page: int = Query(None, ge=1, le=100),
):
    """审查日志列表 HTMX 片段（支持搜索、过滤、分页）"""
    if per_page is None:
        per_page = user_prefs["items_per_page"]
    query = select(PRReview)
    count_query = select(func.count(PRReview.id))

    # 权限过滤：普通用户只能看到已启用仓库的审查记录
    if user["role"] not in ("admin", "super_admin"):
        enabled_repos = select(RepoSubscription.repo_name).where(
            RepoSubscription.is_active == True
        )
        query = query.where(PRReview.repo_name.in_(enabled_repos))
        count_query = count_query.where(PRReview.repo_name.in_(enabled_repos))

    # 搜索过滤
    search_filter = build_review_search_filter(search)
    if search_filter:
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    # 仓库过滤：验证 repo 在活跃仓库列表中
    available_repos = await get_active_repos(db)
    if repo and repo in available_repos:
        query = query.where(PRReview.repo_name == repo)
        count_query = count_query.where(PRReview.repo_name == repo)

    # 状态过滤
    if status:
        query = query.where(PRReview.status == status)
        count_query = count_query.where(PRReview.status == status)

    # 时间范围过滤
    if date_from:
        try:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d")
            query = query.where(PRReview.created_at >= dt_from)
            count_query = count_query.where(PRReview.created_at >= dt_from)
        except ValueError:
            pass

    if date_to:
        try:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            query = query.where(PRReview.created_at < dt_to)
            count_query = count_query.where(PRReview.created_at < dt_to)
        except ValueError:
            pass

    # 排序
    query = query.order_by(desc(PRReview.created_at))

    # 分页
    reviews, total, total_pages, page = await paginate(db, query, count_query, page, per_page)

    return templates.TemplateResponse("components/log_list_fragment.html", {
        "request": request,
        "reviews": reviews,
        "search": search,
        "repo": repo,
        "status": status,
        "date_from": date_from,
        "date_to": date_to,
        "available_repos": available_repos,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "per_page": per_page,
    })


@router.get("/{review_id}/detail-fragment")
async def log_detail_fragment(
    request: Request,
    review_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
) -> HTMLResponse:
    """单条审查详情展开片段"""
    review_result = await db.execute(
        select(PRReview).where(PRReview.id == review_id)
    )
    review = review_result.scalar_one_or_none()
    if not review:
        return error_page(request, message="记录不存在", user=user)

    # 查询关联评论
    comments_result = await db.execute(
        select(ReviewComment)
        .where(ReviewComment.review_id == review_id)
        .order_by(ReviewComment.created_at.asc())
        .limit(5)
    )
    comments = comments_result.scalars().all()

    return templates.TemplateResponse("components/log_detail_fragment.html", {
        "request": request,
        "review": review,
        "comments": comments,
    })
