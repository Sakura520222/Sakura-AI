"""WebUI PR 审查管理路由"""

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, StreamingResponse
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


@router.get("/export-csv")
async def export_pr_csv(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    search: str = Query("", description="搜索关键词"),
    status: str = Query("", description="按状态过滤"),
    decision: str = Query("", description="按决策过滤"),
):
    """导出 PR 审查列表为 CSV"""
    query = select(PRReview)

    # 搜索过滤
    search_filter = build_review_search_filter(search)
    if search_filter:
        query = query.where(search_filter)

    # 状态过滤
    if status:
        query = query.where(PRReview.status == status)

    # 决策过滤
    if decision:
        query = query.where(PRReview.decision == decision)

    # 排序 + 限制
    query = query.order_by(desc(PRReview.created_at)).limit(1000)

    result = await db.execute(query)
    reviews = result.scalars().all()

    # 生成 CSV
    output = io.StringIO()
    output.write('\ufeff')  # UTF-8 BOM for Excel
    writer = csv.writer(output)
    writer.writerow(["PR ID", "仓库名", "PR 标题", "作者", "状态", "决策", "评分", "创建时间", "完成时间"])

    for r in reviews:
        writer.writerow([
            r.pr_id,
            r.repo_name,
            r.title or "",
            r.author or "",
            r.status,
            r.decision or "",
            r.overall_score or "",
            r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else "",
            r.completed_at.strftime('%Y-%m-%d %H:%M') if r.completed_at else "",
        ])

    output.seek(0)
    filename = f"pr_reviews_{datetime.now().strftime('%Y%m%d')}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


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


@router.get("/{review_id}/files")
async def pr_files_page(
    request: Request,
    review_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
) -> HTMLResponse:
    """PR 文件级审查页面"""
    review = (await db.execute(
        select(PRReview).where(PRReview.id == review_id)
    )).scalar_one_or_none()
    if not review:
        return error_page(request, message="审查记录不存在", user=user)

    return templates.TemplateResponse("pr_files.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "pr",
        "user_prefs": user_prefs,
        "review": review,
    })


@router.get("/{review_id}/files/file-fragment")
async def pr_file_list_fragment(
    request: Request,
    review_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
) -> HTMLResponse:
    """文件列表 HTMX 片段（按 file_path 分组，含 severity 统计）"""
    file_stats = (await db.execute(
        select(
            ReviewComment.file_path,
            func.count(ReviewComment.id).label("total"),
            func.count(case((ReviewComment.severity == "critical", 1))).label("critical"),
            func.count(case((ReviewComment.severity == "major", 1))).label("major"),
            func.count(case((ReviewComment.severity == "minor", 1))).label("minor"),
            func.count(case((ReviewComment.severity == "suggestion", 1))).label("suggestion"),
        )
        .where(ReviewComment.review_id == review_id)
        .group_by(ReviewComment.file_path)
        .order_by(func.count(ReviewComment.id).desc())
    )).all()

    return templates.TemplateResponse("components/pr_file_list_fragment.html", {
        "request": request,
        "file_stats": file_stats,
    })


def _build_comments_query(review_id: int, file_path: str):
    """构建评论查询，根据文件路径返回不同的查询对象"""
    if file_path == "__overall__":
        return (
            select(ReviewComment)
            .where(ReviewComment.review_id == review_id, ReviewComment.file_path.is_(None))
            .order_by(ReviewComment.created_at.asc())
        )
    return (
        select(ReviewComment)
        .where(ReviewComment.review_id == review_id, ReviewComment.file_path == file_path)
        .order_by(
            case((ReviewComment.line_number.is_(None), 1), else_=0),
            ReviewComment.line_number.asc(),
            ReviewComment.created_at.asc(),
        )
    )


@router.get("/{review_id}/files/comment-fragment")
async def pr_file_comments_fragment(
    request: Request,
    review_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    file_path: str = Query("", description="文件路径，__overall__ 表示总体评论"),
) -> HTMLResponse:
    """选中文件的评论 HTMX 片段"""
    query = _build_comments_query(review_id, file_path)
    comments = (await db.execute(query)).scalars().all()
    display_path = None if file_path == "__overall__" else file_path

    return templates.TemplateResponse("components/pr_file_comments_fragment.html", {
        "request": request,
        "comments": comments,
        "file_path": display_path,
    })
