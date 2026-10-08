"""Durable, verified webhook admission for Agent business executions."""

from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.models.agent_team_models import AgentTeamSourceType, AgentTeamTask


def delivery_operation_id(delivery_id: str) -> str:
    if not isinstance(delivery_id, str) or not delivery_id or len(delivery_id) > 191:
        raise ValueError("Invalid verified webhook delivery identifier")
    return str(uuid5(NAMESPACE_URL, f"sakura:agent:delivery:{delivery_id}"))


async def find_delivery_task(db, delivery_id, repo_full_name, number, *, is_pr=False):
    if delivery_id is None:
        return None
    delivery_operation_id(delivery_id)  # validate before querying
    task = (
        await db.execute(
            select(AgentTeamTask).where(
                AgentTeamTask.webhook_delivery_id == delivery_id
            )
        )
    ).scalar_one_or_none()
    if task is None:
        return None
    if (
        task.repo_full_name != repo_full_name
        or task.source_issue_number != number
        or (task.source_type == AgentTeamSourceType.PR_REVIEW.value) != is_pr
    ):
        raise ValueError("Verified Agent delivery source conflict")
    task._billing_delivery_replayed = True
    return task


async def persist_delivery_task(db, task):
    """Resolve concurrent insert replays through the database unique constraint."""
    delivery_id = task.webhook_delivery_id
    source = (
        task.repo_full_name,
        task.source_issue_number,
        task.source_type == AgentTeamSourceType.PR_REVIEW.value,
    )
    try:
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task._billing_delivery_replayed = False
        return task
    except IntegrityError:
        if not delivery_id:
            raise
        await db.rollback()
        existing = await find_delivery_task(
            db, delivery_id, source[0], source[1], is_pr=source[2]
        )
        if existing is None:
            raise
        return existing
