"""仪表盘统计公共服务

提取 WebUI 和 API v1 仪表盘共享的 Token 聚合与趋势合并逻辑，
避免跨文件重复。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.agent_team_models import AgentTeamTask
from backend.models.database import IssueAnalysis, PRReview
from backend.models.scan_models import RepoScan


async def fetch_module_token_stats(
    db: AsyncSession,
    scope_user: str | None = None,
) -> dict[str, int]:
    """聚合所有模块（PR / Issue / Agent / Scan）的累计 token 和 cost。

    Args:
        db: 数据库 session
        scope_user: 非 admin 用户的 GitHub 用户名，用于权限过滤；
            None 表示管理员或无需过滤。

    Returns:
        {"total_prompt": int, "total_completion": int, "total_cost": int}
    """

    # ── 构建各模块的 scope 过滤条件 ──
    # PRReview / IssueAnalysis 有 repo_owner + author，双向匹配
    # AgentTeamTask / RepoScan 仅有 repo_owner
    def _scope_for(model, *, has_author: bool = True):
        if scope_user is None:
            return None
        conditions = [model.repo_owner == scope_user]
        if has_author:
            conditions.append(model.author == scope_user)
        return or_(*conditions) if len(conditions) > 1 else conditions[0]

    def _aggregate(model, scope):
        q = select(
            func.coalesce(func.sum(model.prompt_tokens), 0).label("p"),
            func.coalesce(func.sum(model.completion_tokens), 0).label("c"),
            func.coalesce(func.sum(model.estimated_cost), 0).label("e"),
        ).where(model.status == "completed")
        if scope is not None:
            q = q.where(scope)
        return q

    # PR 审查聚合
    pr_row = (
        await db.execute(_aggregate(PRReview, _scope_for(PRReview, has_author=True)))
    ).one()

    # Issue 分析聚合
    issue_row = (
        await db.execute(
            _aggregate(IssueAnalysis, _scope_for(IssueAnalysis, has_author=True))
        )
    ).one()

    # Agent 任务聚合
    agent_row = (
        await db.execute(
            _aggregate(AgentTeamTask, _scope_for(AgentTeamTask, has_author=False))
        )
    ).one()

    # 仓库扫描聚合
    scan_row = (
        await db.execute(_aggregate(RepoScan, _scope_for(RepoScan, has_author=False)))
    ).one()

    return {
        "total_prompt": (
            int(pr_row.p or 0)
            + int(issue_row.p or 0)
            + int(agent_row.p or 0)
            + int(scan_row.p or 0)
        ),
        "total_completion": (
            int(pr_row.c or 0)
            + int(issue_row.c or 0)
            + int(agent_row.c or 0)
            + int(scan_row.c or 0)
        ),
        "total_cost": (
            int(pr_row.e or 0)
            + int(issue_row.e or 0)
            + int(agent_row.e or 0)
            + int(scan_row.e or 0)
        ),
    }


async def fetch_token_trend(
    db: AsyncSession,
    thirty_days_ago: datetime,
    labels: list[str],
    scope_user: str | None = None,
) -> list[int]:
    """获取最近 30 天全模块 Token 消耗趋势。

    Args:
        db: 数据库 session
        thirty_days_ago: 30 天前的时间戳
        labels: 日期标签列表（用于确定数组长度）
        scope_user: 非 admin 用户的 GitHub 用户名，用于权限过滤；
            None 表示管理员或无需过滤。

    Returns:
        长度与 labels 一致的 token 总量列表。
    """
    token_data = [0] * len(labels)

    # ── PR 审查 token 趋势（repo_owner / author 均可匹配）──
    pr_scope = None
    if scope_user is not None:
        pr_scope = or_(
            PRReview.repo_owner == scope_user,
            PRReview.author == scope_user,
        )
    pr_query = (
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
    if pr_scope is not None:
        pr_query = pr_query.where(pr_scope)
    for row in (await db.execute(pr_query)).all():
        if row.day:
            idx = (row.day - thirty_days_ago.date()).days
            if 0 <= idx < len(labels):
                token_data[idx] += int(row.tokens)

    # ── 其他模块 token 趋势（按 repo_owner 过滤）──
    # IssueAnalysis 同时有 repo_owner 和 author，可以同 PRReview 一样双向匹配；
    # AgentTeamTask / RepoScan 仅有 repo_owner，按 repo_owner 过滤。
    module_scopes: list[tuple[type, Any, Any | None]] = [
        (
            IssueAnalysis,
            IssueAnalysis.completed_at,
            or_(
                IssueAnalysis.repo_owner == scope_user,
                IssueAnalysis.author == scope_user,
            )
            if scope_user is not None
            else None,
        ),
        (
            AgentTeamTask,
            AgentTeamTask.completed_at,
            (AgentTeamTask.repo_owner == scope_user)
            if scope_user is not None
            else None,
        ),
        (
            RepoScan,
            RepoScan.completed_at,
            (RepoScan.repo_owner == scope_user) if scope_user is not None else None,
        ),
    ]
    for model_cls, date_col, scope in module_scopes:
        q = (
            select(
                func.date(date_col).label("day"),
                (
                    func.coalesce(func.sum(model_cls.prompt_tokens), 0)
                    + func.coalesce(func.sum(model_cls.completion_tokens), 0)
                ).label("tokens"),
            )
            .where(date_col >= thirty_days_ago)
            .where(model_cls.status == "completed")
            .group_by(func.date(date_col))
        )
        if scope is not None:
            q = q.where(scope)
        rows = (await db.execute(q)).all()
        for row in rows:
            if row.day:
                idx = (row.day - thirty_days_ago.date()).days
                if 0 <= idx < len(labels):
                    token_data[idx] += int(row.tokens)

    return token_data
