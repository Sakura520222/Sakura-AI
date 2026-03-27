"""WebUI PR 审查管理路由"""

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, desc, or_, case
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import PRReview, ReviewComment
from backend.webui.deps import require_auth, get_db, get_templates, get_csrf_serializer

router = APIRouter(prefix="/pr", tags=["WebUI PR"])
templates = get_templates()


@router.get("/")
async def pr_list_page(
    request: Request,
    user: dict = Depends(require_auth),
):
    """渲染 PR 列表页面"""
    return templates.TemplateResponse("pr_list.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "pr",
    })


@router.get("/list-fragment")
async def pr_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    search: str = Query("", description="搜索关键词（PR标题/仓库名/作者）"),
    status: str = Query("", description="按状态过滤"),
    decision: str = Query("", description="按决策过滤"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """PR 列表 HTMX 片段（支持搜索、过滤、分页）"""
    # 构建查询
    query = select(PRReview)
    count_query = select(func.count(PRReview.id))

    # 搜索（转义 LIKE 通配符防止注入）
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

    # 状态过滤
    if status:
        query = query.where(PRReview.status == status)
        count_query = count_query.where(PRReview.status == status)

    # 决策过滤
    if decision:
        query = query.where(PRReview.decision == decision)
        count_query = count_query.where(PRReview.decision == decision)

    # 排序
    query = query.order_by(desc(PRReview.created_at))

    # 总数
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    total_pages = max(1, (total + per_page - 1) // per_page)

    # 分页
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    result = await db.execute(query)
    reviews = result.scalars().all()

    # 渲染 HTMX 片段
    return templates.TemplateResponse("components/pr_list_fragment.html", {
        "request": request,
        "reviews": reviews,
        "search": search,
        "status": status,
        "decision": decision,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "per_page": per_page,
    })


@router.get("/{review_id}")
async def pr_detail_page(
    request: Request,
    review_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """PR 详情页面"""
    # 查询 PR 审查记录
    review = await db.execute(
        select(PRReview).where(PRReview.id == review_id)
    )
    review = review.scalar_one_or_none()
    if not review:
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<h1>审查记录不存在</h1>", status_code=404)

    # 查询关联评论
    def null_last(col):
        return case((col.is_(None), 1), else_=0), col

    comments_result = await db.execute(
        select(ReviewComment)
        .where(ReviewComment.review_id == review_id)
        .order_by(
            *null_last(ReviewComment.file_path),
            *null_last(ReviewComment.line_number),
            ReviewComment.created_at.asc(),
        )
    )
    comments = comments_result.scalars().all()

    return templates.TemplateResponse("pr_detail.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "pr",
        "review": review,
        "comments": comments,
    })
