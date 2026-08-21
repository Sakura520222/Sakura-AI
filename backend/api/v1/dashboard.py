"""API v1 仪表盘端点"""

from collections import OrderedDict
from datetime import UTC, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import and_, case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_auth
from backend.api.v1.responses import success_response
from backend.core.time_service import (
    format_rfc3339,
    get_time_service,
    monotonic,
    start_of_local_day,
)
from backend.models.database import PRReview, ReviewComment
from backend.services.dashboard_stats_service import (
    fetch_module_token_stats,
    fetch_review_trend,
    fetch_token_trend,
)
from backend.webui.deps import (
    build_user_scope_filter,
    get_db,
)

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

# 复用 dashboard 路由中的缓存策略（注意：多 worker 部署时各 worker 缓存独立，不共享）
_stats_cache: OrderedDict[int, tuple[dict, float]] = OrderedDict()
_STATS_CACHE_TTL = 10
_MAX_STATS_CACHE_SIZE = 100

_chart_cache: OrderedDict[int, tuple[dict, float]] = OrderedDict()
_CHART_CACHE_TTL = 20
_MAX_CHART_CACHE_SIZE = 100


@router.get("/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """获取仪表盘统计数据（按用户权限过滤）"""
    uid = user["user_id"]

    # 缓存检查
    if uid in _stats_cache:
        cached_data, ts = _stats_cache[uid]
        if monotonic() - ts < _STATS_CACHE_TTL:
            _stats_cache.move_to_end(uid)
            return success_response(data=cached_data)

    scope_filter = build_user_scope_filter(user, PRReview)

    # 聚合查询
    stats_query = select(
        func.count(PRReview.id).label("total"),
        func.sum(case((PRReview.status == "completed", 1), else_=0)).label("completed"),
        func.sum(case((PRReview.status == "reviewing", 1), else_=0)).label("reviewing"),
        func.sum(case((PRReview.status == "pending", 1), else_=0)).label("pending"),
        func.sum(case((PRReview.status == "failed", 1), else_=0)).label("failed"),
        func.sum(
            case(
                (
                    and_(
                        PRReview.status == "completed", PRReview.decision == "approve"
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
            case((PRReview.status == "completed", PRReview.overall_score), else_=None)
        ).label("avg_score"),
    )
    if scope_filter is not None:
        stats_query = stats_query.where(scope_filter)

    stats_row = (await db.execute(stats_query)).one()

    # 评论总数
    comment_query = select(func.count(ReviewComment.id))
    if scope_filter is not None:
        comment_query = comment_query.join(
            PRReview, ReviewComment.review_id == PRReview.id
        ).where(scope_filter)
    comment_count = (await db.execute(comment_query)).scalar() or 0

    avg_score = float(round(stats_row.avg_score, 1)) if stats_row.avg_score else 0.0

    # 合并所有模块的 token / cost（按用户权限过滤）
    module_scope_user = None if scope_filter is None else user["sub"]
    module_stats = await fetch_module_token_stats(db, module_scope_user)
    total_prompt = module_stats["total_prompt"]
    total_completion = module_stats["total_completion"]
    total_cost = module_stats["total_cost"]

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
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_estimated_cost": total_cost,
        "token_usage_available": module_stats["token_usage_available"],
    }

    # LRU 缓存
    if len(_stats_cache) >= _MAX_STATS_CACHE_SIZE:
        _stats_cache.popitem(last=False)
    _stats_cache[uid] = (result, monotonic())

    return success_response(data=result)


@router.get("/recent-reviews")
async def get_recent_reviews(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """获取最近审查列表（最近 10 条）"""
    query = select(PRReview).order_by(desc(PRReview.created_at)).limit(10)
    scope_filter = build_user_scope_filter(user, PRReview)
    if scope_filter is not None:
        query = query.where(scope_filter)

    result = await db.execute(query)
    reviews = result.scalars().all()

    items = []
    for r in reviews:
        items.append(
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
                "created_at": format_rfc3339(r.created_at) if r.created_at else None,
                "completed_at": format_rfc3339(r.completed_at)
                if r.completed_at
                else None,
            }
        )

    return success_response(data=items)


@router.get("/chart-data")
async def get_chart_data(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_auth),
):
    """获取仪表盘图表数据（按用户权限过滤）"""
    uid = user["user_id"]

    # 缓存检查
    if uid in _chart_cache:
        cached_data, ts = _chart_cache[uid]
        if monotonic() - ts < _CHART_CACHE_TTL:
            _chart_cache.move_to_end(uid)
            return success_response(data=cached_data)

    time_service = get_time_service()
    now = time_service.now_utc()
    local_now = time_service.to_app_timezone(now)
    start_date = local_now.date() - timedelta(days=30)
    bucket_start_utc = start_of_local_day(start_date, time_service.zone).astimezone(UTC)
    scope_filter = build_user_scope_filter(user, PRReview)

    labels = []
    current_date = start_date
    end_date = local_now.date()
    while current_date <= end_date:
        labels.append(f"{current_date.month:02d}-{current_date.day:02d}")
        current_date += timedelta(days=1)

    # 1. 审查趋势（最近 30 天）
    completed_data, failed_data = await fetch_review_trend(
        db,
        bucket_start_utc,
        labels,
        scope_filter,
    )

    # 2. 决策分布
    decision_query = (
        select(PRReview.decision, func.count(PRReview.id).label("cnt"))
        .where(PRReview.status == "completed", PRReview.decision.isnot(None))
        .group_by(PRReview.decision)
    )
    if scope_filter is not None:
        decision_query = decision_query.where(scope_filter)
    decision_rows = (await db.execute(decision_query)).all()

    decision_map = {
        "approve": "通过",
        "request_changes": "需修改",
        "comment": "评论",
        "skip": "跳过",
    }
    decision_labels = [
        decision_map.get(r.decision, r.decision or "其他") for r in decision_rows
    ]
    decision_counts = [r.cnt for r in decision_rows]

    # 3. 仓库排行 Top 10
    repo_query = (
        select(PRReview.repo_name, func.count(PRReview.id).label("cnt"))
        .group_by(PRReview.repo_name)
        .order_by(desc(func.count(PRReview.id)))
        .limit(10)
    )
    if scope_filter is not None:
        repo_query = repo_query.where(scope_filter)
    repo_rows = (await db.execute(repo_query)).all()

    # 4. Token 消耗趋势（合并所有模块，按用户权限过滤）
    scope_user = None if scope_filter is None else user["sub"]
    token_data = await fetch_token_trend(db, bucket_start_utc, labels, scope_user)

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
            "labels": [r.repo_name for r in repo_rows],
            "counts": [r.cnt for r in repo_rows],
        },
        "tokens": {
            "labels": labels,
            "tokens": token_data,
        },
    }

    if len(_chart_cache) >= _MAX_CHART_CACHE_SIZE:
        _chart_cache.popitem(last=False)
    _chart_cache[uid] = (result, monotonic())

    return success_response(data=result)


@router.post("/cache/refresh")
async def refresh_cache(user: dict = Depends(require_api_auth)):
    """手动刷新仪表盘缓存（仅清除当前用户）"""
    uid = user["user_id"]
    _stats_cache.pop(uid, None)
    _chart_cache.pop(uid, None)
    return success_response(message="缓存已刷新")


@router.get("/system-info")
async def get_system_info(user: dict = Depends(require_api_auth)):
    """获取系统运行信息（启动时间、启动耗时、运行时长）"""
    # 延迟导入：避免 api.v1 → main 循环依赖（main 已在模块级导入 api.v1）
    from backend.main import get_system_info_dict

    return success_response(data=get_system_info_dict())
