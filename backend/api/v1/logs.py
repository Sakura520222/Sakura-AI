"""API v1 日志查询端点"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, desc, case, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import PRReview, ReviewComment
from backend.models.admin_action_log import AdminActionLog
from backend.models.telegram_models import TelegramUser
from backend.webui.deps import (
    get_db,
    paginate,
    build_user_scope_filter,
)

from backend.api.v1.deps import require_api_auth, require_api_admin
from backend.api.v1.responses import (
    success_response,
    error_response,
    paginated_response,
)

router = APIRouter(prefix="/logs", tags=["Logs"])


@router.get("/reviews")
async def list_review_logs(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
    search: str = Query("", description="搜索关键词"),
    repo: str = Query("", description="按仓库过滤"),
    status: str = Query("", description="按状态过滤"),
    date_from: str = Query("", description="开始日期 YYYY-MM-DD"),
    date_to: str = Query("", description="结束日期 YYYY-MM-DD"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """审查日志列表"""
    query = select(PRReview)
    count_query = select(func.count(PRReview.id))

    scope_filter = build_user_scope_filter(user, PRReview)
    if scope_filter is not None:
        query = query.where(scope_filter)
        count_query = count_query.where(scope_filter)

    if search:
        escaped = search.replace("%", r"\%").replace("_", r"\_")
        search_filter = or_(
            PRReview.title.ilike(f"%{escaped}%", escape="\\"),
            PRReview.repo_name.ilike(f"%{escaped}%", escape="\\"),
            PRReview.author.ilike(f"%{escaped}%", escape="\\"),
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    if repo:
        query = query.where(PRReview.repo_name == repo)
        count_query = count_query.where(PRReview.repo_name == repo)
    if status:
        query = query.where(PRReview.status == status)
        count_query = count_query.where(PRReview.status == status)
    if date_from:
        from datetime import datetime

        try:
            df = datetime.strptime(date_from, "%Y-%m-%d")
            query = query.where(PRReview.created_at >= df)
            count_query = count_query.where(PRReview.created_at >= df)
        except ValueError:
            return error_response(
                "date_from 格式错误，请使用 YYYY-MM-DD", status_code=400
            )
    if date_to:
        from datetime import datetime, timedelta

        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            query = query.where(PRReview.created_at < dt)
            count_query = count_query.where(PRReview.created_at < dt)
        except ValueError:
            return error_response(
                "date_to 格式错误，请使用 YYYY-MM-DD", status_code=400
            )

    query = query.order_by(desc(PRReview.created_at))

    reviews, total, total_pages, page = await paginate(
        db, query, count_query, page, per_page
    )

    items = [
        {
            "id": r.id,
            "pr_id": r.pr_id,
            "repo_name": r.repo_name,
            "repo_owner": r.repo_owner,
            "title": r.title,
            "author": r.author,
            "status": r.status,
            "decision": r.decision,
            "overall_score": r.overall_score,
            "strategy": r.strategy,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in reviews
    ]

    return paginated_response(items, total, page, total_pages, per_page)


@router.get("/reviews/{review_id}")
async def get_review_log(
    review_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """审查日志详情"""
    query = select(PRReview).where(PRReview.id == review_id)
    scope_filter = build_user_scope_filter(user, PRReview)
    if scope_filter is not None:
        query = query.where(scope_filter)

    review = (await db.execute(query)).scalar_one_or_none()
    if not review:
        return error_response("审查记录不存在", status_code=404)

    # 关联评论
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

    data = {
        "id": review.id,
        "pr_id": review.pr_id,
        "repo_name": review.repo_name,
        "repo_owner": review.repo_owner,
        "title": review.title,
        "author": review.author,
        "status": review.status,
        "decision": review.decision,
        "overall_score": review.overall_score,
        "review_summary": review.review_summary,
        "error_message": review.error_message,
        "strategy": review.strategy,
        "prompt_tokens": review.prompt_tokens,
        "completion_tokens": review.completion_tokens,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "completed_at": review.completed_at.isoformat()
        if review.completed_at
        else None,
        "comments": [
            {
                "id": c.id,
                "file_path": c.file_path,
                "line_number": c.line_number,
                "severity": c.severity,
                "content": c.content,
            }
            for c in comments
        ],
    }

    return success_response(data=data)


@router.get("/actions")
async def list_action_logs(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
    action: str = Query("", description="操作类型过滤"),
    start_date: str = Query("", description="开始日期"),
    end_date: str = Query("", description="结束日期"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """操作日志列表（管理员）"""
    query = select(AdminActionLog)
    count_query = select(func.count(AdminActionLog.id))

    if action:
        query = query.where(AdminActionLog.action == action)
        count_query = count_query.where(AdminActionLog.action == action)

    if start_date:
        from datetime import datetime

        try:
            sd = datetime.strptime(start_date, "%Y-%m-%d")
            query = query.where(AdminActionLog.created_at >= sd)
            count_query = count_query.where(AdminActionLog.created_at >= sd)
        except ValueError:
            return error_response(
                "start_date 格式错误，请使用 YYYY-MM-DD", status_code=400
            )

    if end_date:
        from datetime import datetime, timedelta

        try:
            ed = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            query = query.where(AdminActionLog.created_at < ed)
            count_query = count_query.where(AdminActionLog.created_at < ed)
        except ValueError:
            return error_response(
                "end_date 格式错误，请使用 YYYY-MM-DD", status_code=400
            )

    query = query.order_by(desc(AdminActionLog.created_at))

    logs, total, total_pages, page = await paginate(
        db, query, count_query, page, per_page
    )

    items = [
        {
            "id": log.id,
            "admin_id": log.admin_id,
            "action": log.action,
            "target_type": log.target_type,
            "target_id": log.target_id,
            "detail": log.detail,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]

    return paginated_response(items, total, page, total_pages, per_page)


@router.get("/actions/{log_id}")
async def get_action_log(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """操作日志详情"""
    result = await db.execute(
        select(AdminActionLog, TelegramUser.github_username)
        .outerjoin(TelegramUser, AdminActionLog.admin_id == TelegramUser.id)
        .where(AdminActionLog.id == log_id)
    )
    row = result.first()
    if not row:
        return error_response("操作日志不存在", status_code=404)

    log, admin_name = row
    return success_response(
        data={
            "id": log.id,
            "admin_id": log.admin_id,
            "admin_username": admin_name,
            "action": log.action,
            "target_type": log.target_type,
            "target_id": log.target_id,
            "detail": log.detail,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
    )
