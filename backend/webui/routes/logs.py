"""WebUI 审查日志路由"""

from datetime import datetime
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import PRReview, ReviewComment
from backend.models.telegram_models import RepoSubscription
from backend.webui.deps import require_auth, get_db, get_templates, get_csrf_serializer, get_user_preferences

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
    if search:
        escaped = search.replace("%", r"\%").replace("_", r"\_")
        search_filter = or_(
            PRReview.title.ilike(f"%{escaped}%", escape="\\"),
            PRReview.repo_name.ilike(f"%{escaped}%", escape="\\"),
            PRReview.repo_owner.ilike(f"%{escaped}%", escape="\\"),
            PRReview.author.ilike(f"%{escaped}%", escape="\\"),
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    # 仓库过滤
    if repo:
        repo_filter = PRReview.repo_name == repo
        query = query.where(repo_filter)
        count_query = count_query.where(repo_filter)

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
            dt_to = datetime.strptime(date_to, "%Y-%m-%d")
            # 包含当天结束时间
            from datetime import timedelta
            dt_to = dt_to + timedelta(days=1)
            query = query.where(PRReview.created_at < dt_to)
            count_query = count_query.where(PRReview.created_at < dt_to)
        except ValueError:
            pass

    # 排序
    query = query.order_by(desc(PRReview.created_at))

    # 总数
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    # 分页
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    result = await db.execute(query)
    reviews = result.scalars().all()

    # 获取所有活跃仓库列表用于下拉框
    repos_result = await db.execute(
        select(RepoSubscription.repo_name)
        .where(RepoSubscription.is_active == True)
        .order_by(RepoSubscription.repo_name)
    )
    available_repos = [r[0] for r in repos_result.all()]

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
):
    """单条审查详情展开片段"""
    review_result = await db.execute(
        select(PRReview).where(PRReview.id == review_id)
    )
    review = review_result.scalar_one_or_none()
    if not review:
        return HTMLResponse("<p class='text-gray-500'>记录不存在</p>", status_code=404)

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
