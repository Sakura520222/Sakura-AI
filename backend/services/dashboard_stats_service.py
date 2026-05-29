"""仪表盘统计公共服务

提取 WebUI 和 API v1 仪表盘共享的 Token 聚合与趋势合并逻辑，
避免跨文件重复。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import IssueAnalysis, PRReview
from backend.models.agent_team_models import AgentTeamTask
from backend.models.scan_models import RepoScan


async def fetch_module_token_stats(db: AsyncSession) -> dict[str, int]:
    """聚合所有模块（PR / Issue / Agent / Scan）的累计 token 和 cost。

    Returns:
        {"total_prompt": int, "total_completion": int, "total_cost": int}
    """
    # PR 审查聚合
    pr_row = (await db.execute(
        select(
            func.coalesce(func.sum(PRReview.prompt_tokens), 0).label("p"),
            func.coalesce(func.sum(PRReview.completion_tokens), 0).label("c"),
            func.coalesce(func.sum(PRReview.estimated_cost), 0).label("e"),
        ).where(PRReview.status == "completed")
    )).one()

    # Issue 分析聚合
    issue_row = (await db.execute(
        select(
            func.coalesce(func.sum(IssueAnalysis.prompt_tokens), 0).label("p"),
            func.coalesce(func.sum(IssueAnalysis.completion_tokens), 0).label("c"),
            func.coalesce(func.sum(IssueAnalysis.estimated_cost), 0).label("e"),
        ).where(IssueAnalysis.status == "completed")
    )).one()

    # Agent 任务聚合
    agent_row = (await db.execute(
        select(
            func.coalesce(func.sum(AgentTeamTask.prompt_tokens), 0).label("p"),
            func.coalesce(func.sum(AgentTeamTask.completion_tokens), 0).label("c"),
            func.coalesce(func.sum(AgentTeamTask.estimated_cost), 0).label("e"),
        ).where(AgentTeamTask.status == "completed")
    )).one()

    # 仓库扫描聚合
    scan_row = (await db.execute(
        select(
            func.coalesce(func.sum(RepoScan.prompt_tokens), 0).label("p"),
            func.coalesce(func.sum(RepoScan.completion_tokens), 0).label("c"),
            func.coalesce(func.sum(RepoScan.estimated_cost), 0).label("e"),
        ).where(RepoScan.status == "completed")
    )).one()

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
    scope_filter: Any | None = None,
) -> list[int]:
    """获取最近 30 天全模块 Token 消耗趋势。

    Args:
        db: 数据库 session
        thirty_days_ago: 30 天前的时间戳
        labels: 日期标签列表（用于确定数组长度）
        scope_filter: 用户权限过滤条件（仅应用于 PRReview）

    Returns:
        长度与 labels 一致的 token 总量列表。
    """
    token_data = [0] * len(labels)

    # ── PR 审查 token 趋势（支持用户权限过滤）──
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
    if scope_filter is not None:
        pr_query = pr_query.where(scope_filter)
    for row in (await db.execute(pr_query)).all():
        if row.day:
            idx = (row.day - thirty_days_ago.date()).days
            if 0 <= idx < len(labels):
                token_data[idx] += int(row.tokens)

    # ── 其他模块 token 趋势（无权限过滤，全局数据）──
    for model_cls, date_col in [
        (IssueAnalysis, IssueAnalysis.completed_at),
        (AgentTeamTask, AgentTeamTask.completed_at),
        (RepoScan, RepoScan.completed_at),
    ]:
        rows = (await db.execute(
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
        )).all()
        for row in rows:
            if row.day:
                idx = (row.day - thirty_days_ago.date()).days
                if 0 <= idx < len(labels):
                    token_data[idx] += int(row.tokens)

    return token_data
