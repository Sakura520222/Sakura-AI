"""WebUI 仪表盘路由"""

import asyncio
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import select, func, desc, case, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import PRReview, ReviewComment
from backend.services.dashboard_stats_service import (
    fetch_module_token_stats,
    fetch_token_trend,
)
from backend.webui.deps import (
    require_auth,
    get_db,
    get_templates,
    get_csrf_serializer,
    get_user_preferences,
    build_user_scope_filter,
    render_template,
)

router = APIRouter(tags=["WebUI Dashboard"])
templates = get_templates()

_RECENT_REVIEW_LIMIT = 10

# stats 接口按用户缓存（避免频繁聚合查询 & 用户间数据串扰）
_stats_cache: OrderedDict[int, tuple[dict, float]] = OrderedDict()
_STATS_CACHE_TTL = 10  # 秒
_MAX_STATS_CACHE_SIZE = 100

# chart-data 接口按用户缓存
_chart_cache: OrderedDict[int, tuple[dict, float]] = OrderedDict()
_CHART_CACHE_TTL = 20  # 秒
_MAX_CHART_CACHE_SIZE = 100


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
    db: AsyncSession,
    user: dict,
    limit: int = _RECENT_REVIEW_LIMIT,
) -> list[PRReview]:
    """获取最近的审查记录（按用户权限过滤）"""
    query = select(PRReview).order_by(desc(PRReview.created_at)).limit(limit)

    scope_filter = build_user_scope_filter(user, PRReview)
    if scope_filter is not None:
        query = query.where(scope_filter)

    result = await db.execute(query)
    return result.scalars().all()


_APP_INSTALL_CACHE_TTL = 1800  # 30 分钟（已安装）
_APP_INSTALL_CACHE_TTL_NEGATIVE = 60  # 60 秒（未安装，便于安装后快速刷新）
_APP_INSTALL_CACHE_TTL_UNKNOWN = (
    30  # 30 秒（无法检测，避免 Integration 异常时反复请求）
)

# GitHub App slug 缓存（不变量，启动后获取一次即可）
_app_slug_cache: str | None = None


async def _get_github_app_install_url() -> str | None:
    """获取 GitHub App 安装链接"""
    global _app_slug_cache

    if _app_slug_cache:
        return f"https://github.com/apps/{_app_slug_cache}/installations/new"

    try:
        from backend.core.github_app import GitHubAppClient

        github_app = GitHubAppClient()
        slug = await asyncio.to_thread(github_app.get_bot_username)
        if slug and slug != "unknown-bot":
            # 去掉 bot_username 中可能携带的 [bot] 后缀
            slug = slug.removesuffix("[bot]")
            _app_slug_cache = slug
            return f"https://github.com/apps/{slug}/installations/new"
    except Exception as e:
        logger.warning(f"获取 GitHub App slug 失败: {e}")

    return None


async def _check_github_app_installed(github_username: str) -> Optional[bool]:
    """检查用户是否已安装 GitHub App

    Returns:
        True: 已安装, False: 未安装, None: 无法检测
    """
    from backend.core.github_app import GitHubAppClient
    from backend.core.redis import get_async_redis

    # Redis 缓存检查
    try:
        r = await get_async_redis()
        cache_key = f"github_app_installed:{github_username.lower()}"
        cached = await r.get(cache_key)
        if cached is not None:
            if cached == "unknown":
                return None
            return cached == "1"
    except Exception as e:
        logger.debug(f"Redis 读取安装状态缓存失败: {e}")

    # 查询 GitHub App installations
    try:
        github_app = GitHubAppClient()
        installed = await asyncio.to_thread(
            github_app.check_user_installed, github_username
        )

        if installed is None:
            try:
                r = await get_async_redis()
                await r.setex(cache_key, _APP_INSTALL_CACHE_TTL_UNKNOWN, "unknown")
            except Exception as e:
                logger.debug(f"Redis 缓存写入 unknown 状态失败: {e}")
            return None

        # 写入 Redis 缓存（已安装 30 分钟，未安装 60 秒）
        try:
            r = await get_async_redis()
            ttl = (
                _APP_INSTALL_CACHE_TTL if installed else _APP_INSTALL_CACHE_TTL_NEGATIVE
            )
            await r.setex(cache_key, ttl, "1" if installed else "0")
        except Exception as e:
            logger.debug(f"Redis 写入安装状态缓存失败: {e}")

        return installed
    except Exception as e:
        logger.warning(f"检测 GitHub App 安装状态失败: {e}")
        return None


@router.get("/")
async def dashboard_page(
    request: Request,
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染仪表盘页面"""
    github_app_installed = await _check_github_app_installed(user["sub"])
    github_app_install_url = (
        await _get_github_app_install_url() if github_app_installed is False else None
    )

    return render_template(
        "dashboard.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="dashboard",
        github_app_installed=github_app_installed,
        github_app_install_url=github_app_install_url,
    )


@router.get("/api/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """获取仪表盘统计数据（按用户权限过滤）"""
    uid = user["user_id"]

    # 检查按用户缓存
    if uid in _stats_cache:
        cached_data, ts = _stats_cache[uid]
        if time.time() - ts < _STATS_CACHE_TTL:
            _stats_cache.move_to_end(uid)
            return cached_data

    # 构建用户过滤条件
    scope_filter = build_user_scope_filter(user, PRReview)

    # 单次条件聚合查询（PRReview 表）
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
    if scope_filter is not None:
        stats_query = stats_query.where(scope_filter)

    stats_row = (await db.execute(stats_query)).one()

    # 评论总数查询（通过 join PRReview 进行用户过滤）
    comment_query = select(func.count(ReviewComment.id))
    if scope_filter is not None:
        comment_query = comment_query.join(
            PRReview, ReviewComment.review_id == PRReview.id
        ).where(scope_filter)

    comment_count = (await db.execute(comment_query)).scalar() or 0

    avg_score = round(stats_row.avg_score, 1) if stats_row.avg_score else 0

    # 合并所有模块的 token / cost
    module_stats = await fetch_module_token_stats(db)
    total_prompt = int(stats_row.total_prompt_tokens or 0) + module_stats["total_prompt"]
    total_completion = int(stats_row.total_completion_tokens or 0) + module_stats["total_completion"]
    total_cost = int(stats_row.total_estimated_cost or 0) + module_stats["total_cost"]

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
    }

    # LRU 淘汰 + 写入缓存
    if len(_stats_cache) >= _MAX_STATS_CACHE_SIZE:
        _stats_cache.popitem(last=False)
    _stats_cache[uid] = (result, time.time())
    return result


@router.get("/api/recent-reviews")
async def get_recent_reviews(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """获取最近审查列表（最近 10 条）"""
    reviews = await _fetch_recent_reviews(db, user)
    return [_serialize_review(r) for r in reviews]


@router.get("/api/recent-reviews-html")
async def get_recent_reviews_html(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
) -> HTMLResponse:
    """返回最近审查的 HTML 片段（供仪表盘 HTMX 加载）"""
    reviews = await _fetch_recent_reviews(db, user)
    return templates.TemplateResponse(
        request,
        "components/recent_reviews.html",
        {
            "request": request,
            "reviews": [_serialize_review(r) for r in reviews],
        },
    )


@router.get("/api/chart-data")
async def get_chart_data(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """获取仪表盘图表数据（按用户权限过滤）"""
    uid = user["user_id"]

    # 检查按用户缓存
    if uid in _chart_cache:
        cached_data, ts = _chart_cache[uid]
        if time.time() - ts < _CHART_CACHE_TTL:
            _chart_cache.move_to_end(uid)
            return cached_data

    now = datetime.utcnow()
    thirty_days_ago = now - timedelta(days=30)

    # 构建用户过滤条件
    scope_filter = build_user_scope_filter(user, PRReview)

    # 1. 审查趋势（最近 30 天）
    trend_query = (
        select(
            func.date(PRReview.created_at).label("day"),
            PRReview.status,
            func.count(PRReview.id).label("cnt"),
        )
        .where(PRReview.created_at >= thirty_days_ago)
        .group_by(func.date(PRReview.created_at), PRReview.status)
        .order_by(func.date(PRReview.created_at))
    )
    if scope_filter is not None:
        trend_query = trend_query.where(scope_filter)
    trend_rows = (await db.execute(trend_query)).all()

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
    decision_query = (
        select(PRReview.decision, func.count(PRReview.id).label("cnt"))
        .where(PRReview.status == "completed", PRReview.decision.isnot(None))
        .group_by(PRReview.decision)
    )
    if scope_filter is not None:
        decision_query = decision_query.where(scope_filter)
    decision_rows = (await db.execute(decision_query)).all()

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
    repo_query = (
        select(PRReview.repo_name, func.count(PRReview.id).label("cnt"))
        .group_by(PRReview.repo_name)
        .order_by(desc(func.count(PRReview.id)))
        .limit(10)
    )
    if scope_filter is not None:
        repo_query = repo_query.where(scope_filter)
    repo_rows = (await db.execute(repo_query)).all()

    repo_labels = [r.repo_name for r in repo_rows]
    repo_counts = [r.cnt for r in repo_rows]

    # 4. Token 消耗趋势（合并所有模块）
    token_data = await fetch_token_trend(db, thirty_days_ago, labels, scope_filter)

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

    # LRU 淘汰 + 写入缓存
    if len(_chart_cache) >= _MAX_CHART_CACHE_SIZE:
        _chart_cache.popitem(last=False)
    _chart_cache[uid] = (result, time.time())
    return result


@router.post("/api/cache/refresh")
async def refresh_cache(user: dict = Depends(require_auth)):
    """手动刷新仪表盘缓存（仅清除当前用户）"""
    uid = user["user_id"]
    _stats_cache.pop(uid, None)
    _chart_cache.pop(uid, None)
    return {"status": "ok"}


@router.get("/api/system-info")
async def get_system_info(user: dict = Depends(require_auth)):
    """获取系统运行信息（启动时间、启动耗时、运行时长）"""
    # 延迟导入：避免 webui.routes → main 循环依赖（main 已在模块级导入 webui.routes）
    from backend.main import get_system_info_dict

    return get_system_info_dict()
