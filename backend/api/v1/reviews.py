"""API v1 PR 审查端点"""

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, desc, case
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import PRReview, ReviewComment
from backend.webui.deps import (
    get_db,
    paginate,
    build_review_search_filter,
    build_user_scope_filter,
)

from backend.api.v1.deps import require_api_auth
from backend.api.v1.schemas import ReviewResponse, ReviewFileStatsResponse
from backend.api.v1.responses import success_response, error_response, paginated_response

router = APIRouter(prefix="/reviews", tags=["Reviews"])

OVERALL_COMMENT = "__overall__"


@router.get("")
async def list_reviews(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
    search: str = Query("", description="搜索关键词"),
    status: str = Query("", description="按状态过滤"),
    decision: str = Query("", description="按决策过滤"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """PR 审查列表（分页、搜索、过滤）"""
    query = select(PRReview)
    count_query = select(func.count(PRReview.id))

    # 用户数据范围过滤
    scope_filter = build_user_scope_filter(user, PRReview)
    if scope_filter is not None:
        query = query.where(scope_filter)
        count_query = count_query.where(scope_filter)

    # 搜索过滤
    search_filter = build_review_search_filter(search)
    if search_filter is not None:
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

    query = query.order_by(desc(PRReview.created_at))

    reviews, total, total_pages, page = await paginate(
        db, query, count_query, page, per_page
    )

    items = [
        ReviewResponse.model_validate(r, from_attributes=True).model_dump()
        for r in reviews
    ]

    return paginated_response(items, total, page, total_pages, per_page)


@router.get("/export")
async def export_reviews_csv(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
    search: str = Query("", description="搜索关键词"),
    status: str = Query("", description="按状态过滤"),
    decision: str = Query("", description="按决策过滤"),
):
    """导出 PR 审查列表为 CSV"""
    query = select(PRReview)

    scope_filter = build_user_scope_filter(user, PRReview)
    if scope_filter is not None:
        query = query.where(scope_filter)

    search_filter = build_review_search_filter(search)
    if search_filter is not None:
        query = query.where(search_filter)

    if status:
        query = query.where(PRReview.status == status)
    if decision:
        query = query.where(PRReview.decision == decision)

    query = query.order_by(desc(PRReview.created_at)).limit(1000)
    result = await db.execute(query)
    reviews = result.scalars().all()

    output = io.StringIO()
    output.write("\ufeff")  # UTF-8 BOM
    writer = csv.writer(output)
    writer.writerow([
        "PR ID", "仓库名", "PR 标题", "作者", "状态", "决策", "评分",
        "创建时间", "完成时间",
    ])

    for r in reviews:
        writer.writerow([
            r.pr_id,
            r.repo_name,
            r.title or "",
            r.author or "",
            r.status,
            r.decision or "",
            r.overall_score or "",
            r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
            r.completed_at.strftime("%Y-%m-%d %H:%M") if r.completed_at else "",
        ])

    output.seek(0)
    filename = f"pr_reviews_{datetime.now().strftime('%Y%m%d')}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/{review_id}")
async def get_review(
    review_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """审查详情（含评论）"""
    query = select(PRReview).where(PRReview.id == review_id)
    scope_filter = build_user_scope_filter(user, PRReview)
    if scope_filter is not None:
        query = query.where(scope_filter)

    review = (await db.execute(query)).scalar_one_or_none()
    if not review:
        return error_response("审查记录不存在或无权访问", status_code=404)

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

    review_data = ReviewResponse.model_validate(review, from_attributes=True).model_dump()
    review_data["comments"] = [
        {
            "id": c.id,
            "file_path": c.file_path,
            "line_number": c.line_number,
            "comment_type": c.comment_type,
            "severity": c.severity,
            "content": c.content,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in comments
    ]

    return success_response(data=review_data)


@router.get("/{review_id}/files")
async def get_review_files(
    review_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """审查文件级统计（按 file_path 分组，含 severity 统计）"""
    # 验证审查记录权限
    query = select(PRReview).where(PRReview.id == review_id)
    scope_filter = build_user_scope_filter(user, PRReview)
    if scope_filter is not None:
        query = query.where(scope_filter)
    review = (await db.execute(query)).scalar_one_or_none()
    if not review:
        return error_response("审查记录不存在或无权访问", status_code=404)

    # 文件级统计
    file_stats = (
        await db.execute(
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
        )
    ).all()

    items = []
    for row in file_stats:
        items.append(
            ReviewFileStatsResponse(
                file_path=row.file_path or OVERALL_COMMENT,
                severity_counts={
                    "critical": row.critical,
                    "major": row.major,
                    "minor": row.minor,
                    "suggestion": row.suggestion,
                },
                comment_count=row.total,
            ).model_dump()
        )

    return success_response(data=items)


@router.get("/{review_id}/comments")
async def get_review_comments(
    review_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
    file_path: str = Query("", description="文件路径过滤"),
):
    """审查评论列表（可按 file_path 过滤）"""
    # 验证权限
    query = select(PRReview).where(PRReview.id == review_id)
    scope_filter = build_user_scope_filter(user, PRReview)
    if scope_filter is not None:
        query = query.where(scope_filter)
    review = (await db.execute(query)).scalar_one_or_none()
    if not review:
        return error_response("审查记录不存在或无权访问", status_code=404)

    # 构建评论查询
    comment_query = select(ReviewComment).where(ReviewComment.review_id == review_id)

    if file_path == OVERALL_COMMENT:
        comment_query = comment_query.where(ReviewComment.file_path.is_(None))
    elif file_path:
        comment_query = comment_query.where(ReviewComment.file_path == file_path)

    comment_query = comment_query.order_by(
        case((ReviewComment.line_number.is_(None), 1), else_=0),
        ReviewComment.line_number.asc(),
        ReviewComment.created_at.asc(),
    )

    comments = (await db.execute(comment_query)).scalars().all()

    items = [
        {
            "id": c.id,
            "file_path": c.file_path,
            "line_number": c.line_number,
            "comment_type": c.comment_type,
            "severity": c.severity,
            "content": c.content,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in comments
    ]

    return success_response(data=items)


@router.get("/{review_id}/files/{file_path:path}")
async def get_file_comments(
    review_id: int,
    file_path: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """获取特定文件的审查评论"""
    # 验证权限
    query = select(PRReview).where(PRReview.id == review_id)
    scope_filter = build_user_scope_filter(user, PRReview)
    if scope_filter is not None:
        query = query.where(scope_filter)
    review = (await db.execute(query)).scalar_one_or_none()
    if not review:
        return error_response("审查记录不存在或无权访问", status_code=404)

    # 查询该文件的评论
    comment_query = (
        select(ReviewComment)
        .where(
            ReviewComment.review_id == review_id,
            ReviewComment.file_path == file_path,
        )
        .order_by(
            case((ReviewComment.line_number.is_(None), 1), else_=0),
            ReviewComment.line_number.asc(),
            ReviewComment.created_at.asc(),
        )
    )
    comments = (await db.execute(comment_query)).scalars().all()

    return success_response(data={
        "file_path": file_path,
        "comment_count": len(comments),
        "comments": [
            {
                "id": c.id,
                "line_number": c.line_number,
                "comment_type": c.comment_type,
                "severity": c.severity,
                "content": c.content,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in comments
        ],
    })
