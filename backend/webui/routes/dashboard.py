"""WebUI 仪表盘路由"""

import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, desc, case, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import PRReview, ReviewComment
from backend.webui.deps import (
    require_auth,
    get_db,
    get_templates,
    get_csrf_serializer,
    get_user_preferences,
)

router = APIRouter(tags=["WebUI Dashboard"])
templates = get_templates()

_RECENT_REVIEW_LIMIT = 10

# stats 接口缓存（避免频繁聚合查询）
_stats_cache: tuple[dict, float] | None = None
_STATS_CACHE_TTL = 10  # 秒

# chart-data 接口缓存
_chart_cache: tuple[dict, float] | None = None
_CHART_CACHE_TTL = 20  # 秒


def _serialize_review(r: PRReview) -> dict:
    """将 PRReview ORM 对象序列化为字典"""
    return {
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


async def _fetch_recent_reviews(
    db: AsyncSession, limit: int = _RECENT_REVIEW_LIMIT
) -> list[PRReview]:
    """获取最近的审查记录"""
    result = await db.execute(
        select(PRReview).order_by(desc(PRReview.created_at)).limit(limit)
    )
    return result.scalars().all()


@router.get("/")
async def dashboard_page(
    request: Request,
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染仪表盘页面"""
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "current_user": user,
            "csrf_token": get_csrf_serializer().dumps({}),
            "active_page": "dashboard",
            "user_prefs": user_prefs,
        },
    )


@router.get("/api/webui/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """获取仪表盘统计数据"""
    global _stats_cache

    # 检查缓存
    if _stats_cache and time.time() - _stats_cache[1] < _STATS_CACHE_TTL:
        return _stats_cache[0]

    # 单次条件聚合查询（PRReview 表）
    stats_row = (
        await db.execute(
            select(
                func.count(PRReview.id).label("total"),
                func.sum(case((PRReview.status == "completed", 1), else_=0)).label(
                    "completed"
                ),
                func.sum(case((PRReview.status == "reviewing", 1), else_=0)).label(
                    "reviewing"
                ),
                func.sum(case((PRReview.status == "pending", 1), else_=0)).label(
                    "pending"
                ),
                func.sum(case((PRReview.status == "failed", 1), else_=0)).label(
                    "failed"
                ),
                func.sum(
                    case(
                        (
                            and_(
                                PRReview.status == "completed",
                                PRReview.decision == "approve",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("approved"),
                func.sum(
                    case(
                        (
                            and_(
                                PRReview.status == "completed",
                                PRReview.decision == "request_changes",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("changes_requested"),
                func.avg(
                    case(
                        (PRReview.status == "completed", PRReview.overall_score),
                        else_=None,
                    )
                ).label("avg_score"),
                # Token 消耗仅统计已完成的审查
                func.coalesce(
                    func.sum(
                        case(
                            (PRReview.status == "completed", PRReview.prompt_tokens),
                            else_=0,
                        )
                    ),
                    0,
                ).label("total_prompt_tokens"),
                func.coalesce(
                    func.sum(
                        case(
                            (PRReview.status == "completed", PRReview.completion_tokens),
                            else_=0,
                        )
                    ),
                    0,
                ).label("total_completion_tokens"),
                func.coalesce(
                    func.sum(
                        case(
                            (PRReview.status == "completed", PRReview.estimated_cost),
                            else_=0,
                        )
                    ),
                    0,
                ).label("total_estimated_cost"),
            )
        )
    ).one()

    # 评论总数查询
    comment_count = (
        await db.execute(select(func.count(ReviewComment.id)))
    ).scalar() or 0

    avg_score = round(stats_row.avg_score, 1) if stats_row.avg_score else 0

    result = {
        "total": int(stats_row.total or 0),
        "completed": int(stats_row.completed or 0),
        "reviewing": int(stats_row.reviewing or 0),
        "pending": int(stats_row.pending or 0),
        "failed": int(stats_row.failed or 0),
        "approved": int(stats_row.approved or 0),
        "changes_requested": int(stats_row.changes_requested or 0),
        "avg_score": avg_score,
        "comment_count": comment_count,
        "total_prompt_tokens": int(stats_row.total_prompt_tokens or 0),
        "total_completion_tokens": int(stats_row.total_completion_tokens or 0),
        "total_estimated_cost": int(stats_row.total_estimated_cost or 0),
    }

    _stats_cache = (result, time.time())
    return result


@router.get("/api/webui/recent-reviews")
async def get_recent_reviews(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """获取最近审查列表（最近 10 条）"""
    reviews = await _fetch_recent_reviews(db)
    return [_serialize_review(r) for r in reviews]


@router.get("/api/webui/recent-reviews-html")
async def get_recent_reviews_html(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
) -> HTMLResponse:
    """返回最近审查的 HTML 片段（供仪表盘 HTMX 加载）"""
    reviews = await _fetch_recent_reviews(db)
    return templates.TemplateResponse(
        "components/recent_reviews.html",
        {
            "request": request,
            "reviews": [_serialize_review(r) for r in reviews],
        },
    )


@router.get("/api/webui/chart-data")
async def get_chart_data(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """获取仪表盘图表数据"""
    global _chart_cache

    # 检查缓存
    if _chart_cache and time.time() - _chart_cache[1] < _CHART_CACHE_TTL:
        return _chart_cache[0]

    now = datetime.utcnow()
    thirty_days_ago = now - timedelta(days=30)

    # 1. 审查趋势（最近 30 天）
    trend_rows = (
        await db.execute(
            select(
                func.date(PRReview.created_at).label("day"),
                PRReview.status,
                func.count(PRReview.id).label("cnt"),
            )
            .where(PRReview.created_at >= thirty_days_ago)
            .group_by(func.date(PRReview.created_at), PRReview.status)
            .order_by(func.date(PRReview.created_at))
        )
    ).all()

    # 构建连续日期标签
    labels = []
    completed_data = []
    failed_data = []
    current = thirty_days_ago
    while current <= now:
        day_str = current.strftime("%m-%d")
        labels.append(day_str)
        completed_data.append(0)
        failed_data.append(0)
        current += timedelta(days=1)

    for row in trend_rows:
        if row.day:
            idx = (row.day - thirty_days_ago.date()).days
            if 0 <= idx < len(labels):
                if row.status == "completed":
                    completed_data[idx] = row.cnt
                elif row.status == "failed":
                    failed_data[idx] = row.cnt

    # 2. 决策分布
    decision_rows = (
        await db.execute(
            select(PRReview.decision, func.count(PRReview.id).label("cnt"))
            .where(PRReview.status == "completed", PRReview.decision.isnot(None))
            .group_by(PRReview.decision)
        )
    ).all()

    decision_labels = []
    decision_counts = []
    decision_map = {
        "approve": "通过",
        "request_changes": "需修改",
        "comment": "评论",
        "skip": "跳过",
    }
    for row in decision_rows:
        label = decision_map.get(row.decision, row.decision or "其他")
        decision_labels.append(label)
        decision_counts.append(row.cnt)

    # 3. 仓库排行 Top 10
    repo_rows = (
        await db.execute(
            select(PRReview.repo_name, func.count(PRReview.id).label("cnt"))
            .group_by(PRReview.repo_name)
            .order_by(desc(func.count(PRReview.id)))
            .limit(10)
        )
    ).all()

    repo_labels = [r.repo_name for r in repo_rows]
    repo_counts = [r.cnt for r in repo_rows]

    # 4. Token 消耗趋势（最近 30 天，仅已完成的审查）
    token_rows = (
        await db.execute(
            select(
                func.date(PRReview.created_at).label("day"),
                (
                    func.coalesce(func.sum(PRReview.prompt_tokens), 0)
                    + func.coalesce(func.sum(PRReview.completion_tokens), 0)
                ).label("tokens"),
            )
            .where(PRReview.created_at >= thirty_days_ago)
            .where(PRReview.status == "completed")
            .group_by(func.date(PRReview.created_at))
        )
    ).all()

    token_data = [0] * len(labels)
    for row in token_rows:
        if row.day:
            idx = (row.day - thirty_days_ago.date()).days
            if 0 <= idx < len(labels):
                token_data[idx] = int(row.tokens)

    result = {
        "trend": {
            "labels": labels,
            "completed": completed_data,
            "failed": failed_data,
        },
        "decisions": {
            "labels": decision_labels,
            "counts": decision_counts,
        },
        "top_repos": {
            "labels": repo_labels,
            "counts": repo_counts,
        },
        "tokens": {
            "labels": labels,
            "tokens": token_data,
        },
    }

    _chart_cache = (result, time.time())
    return result


@router.post("/api/webui/cache/refresh")
async def refresh_cache(user: dict = Depends(require_auth)):
    """手动刷新仪表盘缓存"""
    global _stats_cache, _chart_cache
    _stats_cache = None
    _chart_cache = None
    return {"status": "ok"}
