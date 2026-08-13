"""仪表盘统计公共服务。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.time_service import get_time_service, local_date, start_of_local_day
from backend.models.agent_team_models import AgentTeamTask
from backend.models.ai_usage_models import AIUsageRecord
from backend.models.database import IssueAnalysis, PRReview
from backend.models.scan_models import RepoScan
from backend.services.ai_usage_service import (
    ACCOUNTED_CALL_KINDS,
    fetch_global_token_totals,
)


async def fetch_estimated_cost(
    db: AsyncSession,
    scope_user: str | None = None,
) -> int:
    """Return the existing business-result cost estimate.

    Cost is not a provider-usage counter: providers do not expose one stable,
    cross-provider monetary amount.  It therefore remains a distinct estimate
    and must never be used as a Token source.
    """

    def scope_for(model: Any, *, has_author: bool = True) -> Any | None:
        if scope_user is None:
            return None
        conditions = [model.repo_owner == scope_user]
        if has_author:
            conditions.append(model.author == scope_user)
        return or_(*conditions) if len(conditions) > 1 else conditions[0]

    total_cost = 0
    for model, has_author in (
        (PRReview, True),
        (IssueAnalysis, True),
        (AgentTeamTask, False),
        (RepoScan, False),
    ):
        query = select(func.coalesce(func.sum(model.estimated_cost), 0)).where(
            model.status == "completed"
        )
        scope = scope_for(model, has_author=has_author)
        if scope is not None:
            query = query.where(scope)
        total_cost += int((await db.execute(query)).scalar() or 0)
    return total_cost


async def fetch_module_token_stats(
    db: AsyncSession,
    scope_user: str | None = None,
) -> dict[str, Any]:
    """Return dashboard Token totals from the new global ledger only.

    The ledger is deliberately global and does not contain an end-user owner
    column.  A user-scoped dashboard must consequently not receive a global
    aggregate, and it also must not silently fall back to legacy Token fields.
    """

    total_cost = await fetch_estimated_cost(db, scope_user)
    if scope_user is not None:
        return {
            "total_prompt": None,
            "total_completion": None,
            "total_cost": total_cost,
            "token_usage_available": False,
        }

    totals = await fetch_global_token_totals(db)
    return {
        "total_prompt": totals.input_tokens,
        "total_completion": totals.output_tokens,
        "total_cost": total_cost,
        "token_usage_available": True,
    }


async def fetch_token_trend(
    db: AsyncSession,
    thirty_days_ago: datetime,
    labels: list[str],
    scope_user: str | None = None,
) -> list[int]:
    """Return the latest 30-day Token trend from ``ai_usage_records`` only."""

    token_data = [0] * len(labels)
    # See ``fetch_module_token_stats``: the global ledger cannot be safely
    # projected into a non-admin user's dashboard until it has an owner scope.
    if scope_user is not None:
        return token_data
    if not labels:
        return token_data

    zone = get_time_service().zone
    start_date = local_date(thirty_days_ago, zone)
    boundaries = [
        start_of_local_day(start_date + timedelta(days=index), zone)
        for index in range(len(labels) + 1)
    ]
    token_total = func.coalesce(AIUsageRecord.input_tokens, 0) + func.coalesce(
        AIUsageRecord.output_tokens, 0
    )
    bucket_columns = [
        func.coalesce(
            func.sum(
                case(
                    (
                        and_(
                            AIUsageRecord.occurred_at >= boundaries[index],
                            AIUsageRecord.occurred_at < boundaries[index + 1],
                        ),
                        token_total,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label(f"bucket_{index}")
        for index in range(len(labels))
    ]
    ledger_query = select(*bucket_columns).where(
        AIUsageRecord.occurred_at >= boundaries[0],
        AIUsageRecord.occurred_at < boundaries[-1],
        AIUsageRecord.call_kind.in_(ACCOUNTED_CALL_KINDS),
    )
    row = (await db.execute(ledger_query)).one()
    return [int(getattr(row, f"bucket_{index}") or 0) for index in range(len(labels))]


async def fetch_review_trend(
    db: AsyncSession,
    thirty_days_ago: datetime,
    labels: list[str],
    scope_filter: Any | None = None,
) -> tuple[list[int], list[int]]:
    """Aggregate review outcomes into application-calendar buckets in SQL."""

    completed_data = [0] * len(labels)
    failed_data = [0] * len(labels)
    if not labels:
        return completed_data, failed_data

    zone = get_time_service().zone
    start_date = local_date(thirty_days_ago, zone)
    boundaries = [
        start_of_local_day(start_date + timedelta(days=index), zone)
        for index in range(len(labels) + 1)
    ]

    def bucket_column(index: int, status: str) -> Any:
        return func.coalesce(
            func.sum(
                case(
                    (
                        and_(
                            PRReview.created_at >= boundaries[index],
                            PRReview.created_at < boundaries[index + 1],
                            PRReview.status == status,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label(f"{status}_{index}")

    bucket_columns = [
        bucket_column(index, status)
        for status in ("completed", "failed")
        for index in range(len(labels))
    ]
    query = select(*bucket_columns).where(
        PRReview.created_at >= boundaries[0],
        PRReview.created_at < boundaries[-1],
        PRReview.status.in_(("completed", "failed")),
    )
    if scope_filter is not None:
        query = query.where(scope_filter)

    row = (await db.execute(query)).one()
    completed_data = [
        int(getattr(row, f"completed_{index}") or 0) for index in range(len(labels))
    ]
    failed_data = [
        int(getattr(row, f"failed_{index}") or 0) for index in range(len(labels))
    ]
    return completed_data, failed_data
