"""Activity 事件 API 权限校验工具。

验证用户是否有权访问指定 task_type + task_id 对应的任务事件，
防止越权查看其他用户的活动日志。
"""

from typing import Any

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import (
    IssueAnalysis,
    PRReview,
)
from backend.models.scan_models import RepoScan
from backend.webui.deps import (
    build_user_scope_filter,
    get_db,
    require_auth,
)


async def verify_task_access(
    task_type: str,
    task_id: int,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any] | None:
    """验证用户是否有权访问指定任务，返回任务元数据或 None。"""
    if task_type == "pr":
        return await _verify_pr_access(task_id, user, db)
    if task_type == "issue":
        return await _verify_issue_access(task_id, user, db)
    if task_type == "scan":
        return await _verify_scan_access(task_id, user, db)
    return None


async def _verify_pr_access(
    task_id: int, user: dict, db: AsyncSession
) -> dict[str, Any] | None:
    query = select(PRReview).where(PRReview.id == task_id)
    scope_filter = build_user_scope_filter(user, PRReview)
    if scope_filter is not None:
        query = query.where(scope_filter)
    result = await db.execute(query)
    record = result.scalar_one_or_none()
    if not record:
        return None
    return {
        "id": record.id,
        "type": "pr",
        "status": record.status,
        "repo_name": record.repo_name,
    }


async def _verify_issue_access(
    task_id: int, user: dict, db: AsyncSession
) -> dict[str, Any] | None:
    query = select(IssueAnalysis).where(IssueAnalysis.id == task_id)
    scope_filter = build_user_scope_filter(user, IssueAnalysis)
    if scope_filter is not None:
        query = query.where(scope_filter)
    result = await db.execute(query)
    record = result.scalar_one_or_none()
    if not record:
        return None
    return {
        "id": record.id,
        "type": "issue",
        "status": record.status,
        "repo_name": record.repo_name,
    }


async def _verify_scan_access(
    task_id: int, user: dict, db: AsyncSession
) -> dict[str, Any] | None:
    query = select(RepoScan).where(RepoScan.id == task_id)
    if user.get("role") not in ("admin", "super_admin"):
        query = query.where(RepoScan.repo_owner == user["sub"])
    result = await db.execute(query)
    record = result.scalar_one_or_none()
    if not record:
        return None
    return {
        "id": record.id,
        "type": "scan",
        "status": record.status,
        "repo_name": record.repo_name,
    }
