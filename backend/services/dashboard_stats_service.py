"""仪表盘统计公共服务。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.time_service import get_time_service, local_date
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

    # Convert UTC instants in Python so the same application-calendar bucket
    # and DST behavior is used on every supported SQL dialect.
    ledger_query = select(
        AIUsageRecord.occurred_at,
        AIUsageRecord.input_tokens,
        AIUsageRecord.output_tokens,
    ).where(
        AIUsageRecord.occurred_at >= thirty_days_ago,
        AIUsageRecord.call_kind.in_(ACCOUNTED_CALL_KINDS),
    )
    zone = get_time_service().zone
    start_date = local_date(thirty_days_ago, zone)
    for row in (await db.execute(ledger_query)).all():
        if row.occurred_at:
            idx = (local_date(row.occurred_at, zone) - start_date).days
            if 0 <= idx < len(labels):
                token_data[idx] += int(row.input_tokens or 0) + int(
                    row.output_tokens or 0
                )
    return token_data
