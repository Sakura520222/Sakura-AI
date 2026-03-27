"""WebUI 仪表盘路由"""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import PRReview, ReviewComment
from backend.webui.deps import require_auth, get_db, get_templates, get_csrf_serializer, get_user_preferences

router = APIRouter(tags=["WebUI Dashboard"])
templates = get_templates()


@router.get("/")
async def dashboard_page(
    request: Request,
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染仪表盘页面"""
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "dashboard",
        "user_prefs": user_prefs,
    })


@router.get("/api/webui/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """获取仪表盘统计数据"""
    # 总审查数
    total = await db.execute(select(func.count(PRReview.id)))
    total_count = total.scalar() or 0

    # 已完成数
    completed = await db.execute(
        select(func.count(PRReview.id)).where(PRReview.status == "completed")
    )
    completed_count = completed.scalar() or 0

    # 审查中
    reviewing = await db.execute(
        select(func.count(PRReview.id)).where(PRReview.status == "reviewing")
    )
    reviewing_count = reviewing.scalar() or 0

    # 失败
    failed = await db.execute(
        select(func.count(PRReview.id)).where(PRReview.status == "failed")
    )
    failed_count = failed.scalar() or 0

    # 通过（decision = approve）
    approved = await db.execute(
        select(func.count(PRReview.id)).where(
            PRReview.status == "completed",
            PRReview.decision == "approve",
        )
    )
    approved_count = approved.scalar() or 0

    # 需修改（decision = request_changes）
    changes_requested = await db.execute(
        select(func.count(PRReview.id)).where(
            PRReview.status == "completed",
            PRReview.decision == "request_changes",
        )
    )
    changes_count = changes_requested.scalar() or 0

    # 平均评分
    avg_score_result = await db.execute(
        select(func.avg(PRReview.overall_score)).where(PRReview.status == "completed")
    )
    avg_score = avg_score_result.scalar()
    avg_score = round(avg_score, 1) if avg_score else 0

    # 评论总数
    comment_count_result = await db.execute(select(func.count(ReviewComment.id)))
    comment_count = comment_count_result.scalar() or 0

    return {
        "total": total_count,
        "completed": completed_count,
        "reviewing": reviewing_count,
        "failed": failed_count,
        "approved": approved_count,
        "changes_requested": changes_count,
        "avg_score": avg_score,
        "comment_count": comment_count,
    }


@router.get("/api/webui/recent-reviews")
async def get_recent_reviews(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """获取最近审查列表（最近 10 条）"""
    result = await db.execute(
        select(PRReview)
        .order_by(desc(PRReview.created_at))
        .limit(10)
    )
    reviews = result.scalars().all()
    return [
        {
            "id": r.id,
            "pr_id": r.pr_id,
            "repo_name": r.repo_name,
            "repo_owner": r.repo_owner,
            "title": r.title,
            "author": r.author,
            "status": r.status,
            "overall_score": r.overall_score,
            "decision": r.decision,
            "strategy": r.strategy,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in reviews
    ]


@router.get("/api/webui/recent-reviews-html")
async def get_recent_reviews_html(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """返回最近审查的 HTML 片段（供仪表盘 HTMX 加载）"""
    result = await db.execute(
        select(PRReview)
        .order_by(desc(PRReview.created_at))
        .limit(10)
    )
    reviews = result.scalars().all()

    review_data = [
        {
            "id": r.id,
            "pr_id": r.pr_id,
            "repo_name": r.repo_name,
            "repo_owner": r.repo_owner,
            "title": r.title,
            "author": r.author,
            "status": r.status,
            "overall_score": r.overall_score,
            "decision": r.decision,
            "strategy": r.strategy,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in reviews
    ]

    return templates.TemplateResponse("components/recent_reviews.html", {
        "request": request,
        "reviews": review_data,
    })
