"""WebUI PR 审查管理路由"""

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, desc, case
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import PRReview, ReviewComment
from backend.webui.deps import require_auth, get_db, get_templates, get_csrf_serializer, get_user_preferences, paginate, error_page, build_review_search_filter

router = APIRouter(prefix="/pr", tags=["WebUI PR"])
templates = get_templates()


@router.get("/")
async def pr_list_page(
    request: Request,
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染 PR 列表页面"""
    return templates.TemplateResponse("pr_list.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "pr",
        "user_prefs": user_prefs,
    })


@router.get("/list-fragment")
async def pr_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
    search: str = Query("", description="搜索关键词（PR标题/仓库名/作者）"),
    status: str = Query("", description="按状态过滤"),
    decision: str = Query("", description="按决策过滤"),
    page: int = Query(1, ge=1),
    per_page: int = Query(None, ge=1, le=100),
):
    """PR 列表 HTMX 片段（支持搜索、过滤、分页）"""
    if per_page is None:
        per_page = user_prefs["items_per_page"]
    query = select(PRReview)
    count_query = select(func.count(PRReview.id))

    # 搜索过滤
    search_filter = build_review_search_filter(search)
    if search_filter:
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

    # 分页
    reviews, total, total_pages, page = await paginate(db, query, count_query, page, per_page)

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
    user_prefs: dict = Depends(get_user_preferences),
) -> HTMLResponse:
    """PR 详情页面"""
    # 查询 PR 审查记录
    review = await db.execute(
        select(PRReview).where(PRReview.id == review_id)
    )
    review = review.scalar_one_or_none()
    if not review:
        return error_page(request, message="审查记录不存在", user=user)

    # 查询关联评论
    comments_result = await db.execute(
        select(ReviewComment)
        .where(ReviewComment.review_id == review_id)
        .order_by(
            case((ReviewComment.file_path.is_(None), 1), else_=0),
            ReviewComment.file_path,
            case((ReviewComment.line_number.is_(None), 1), else_=0),
            ReviewComment.line_number,
            ReviewComment.created_at.asc(),
        )
    )
    comments = comments_result.scalars().all()

    # 预处理时间格式
    created_at_str = review.created_at.strftime('%Y-%m-%d %H:%M:%S') if review.created_at else '-'
    completed_at_str = review.completed_at.strftime('%Y-%m-%d %H:%M:%S') if review.completed_at else None

    return templates.TemplateResponse("pr_detail.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "pr",
        "user_prefs": user_prefs,
        "review": review,
        "comments": comments,
        "created_at_str": created_at_str,
        "completed_at_str": completed_at_str,
    })
