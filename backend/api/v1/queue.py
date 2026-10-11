"""API v1 队列监控端点"""

import asyncio

from fastapi import APIRouter, Depends, Query
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_admin
from backend.api.v1.responses import (
    error_response,
    paginated_response,
    success_response,
)
from backend.core.time_service import format_rfc3339
from backend.models.database import ReviewQueue
from backend.models.telegram_models import TelegramUser
from backend.webui.deps import get_db, paginate

router = APIRouter(prefix="/queue", tags=["Queue"])


class IncrementalResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=1000)


@router.post("/increments/{item_id}/resume")
async def resume_incremental_queue(
    item_id: int,
    body: IncrementalResumeRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """Resume a verified, never-started increment under its original payer."""
    from backend.core.github_app import GitHubAppClient
    from backend.services.billing_service import BillingError
    from backend.services.pr_review_incremental_queue import (
        PRReviewIncrementalQueueService,
    )
    from backend.services.pr_review_incremental_recovery import (
        incremental_recovery_payload,
        recover_increment_dispatch,
    )
    from backend.workers.review_worker import _drain_pending_incremental

    actor = await db.get(TelegramUser, user.get("user_id"))
    if actor is None or not actor.is_active or actor.role != "super_admin":
        return error_response("forbidden", status_code=403)
    if not body.evidence.strip() or not body.reason.strip():
        return error_response("incremental_recovery_evidence_required", status_code=400)
    actor_id = actor.id
    try:
        payload = await incremental_recovery_payload(db, item_id)
        await db.rollback()  # No financial or queue locks during GitHub I/O.
        github = GitHubAppClient()
        client = await asyncio.to_thread(
            github.get_repo_client, payload["repo_owner"], payload["repo_name"]
        )
        if client is None:
            raise RuntimeError("Repository authorization unavailable")
        repo = await asyncio.to_thread(client.get_repo, payload["repo_full_name"])
        pr = await asyncio.to_thread(repo.get_pull, payload["pr_number"])
        if pr.state != "open" or pr.merged:
            await PRReviewIncrementalQueueService().cancel_pending_for_pr(
                payload["repo_full_name"],
                payload["pr_number"],
                actor_id=actor_id,
                evidence=body.evidence,
                reason=body.reason,
            )
            return success_response(data={"queue_id": item_id, "status": "cancelled"})
        report = await recover_increment_dispatch(
            db,
            item_id,
            dry_run=False,
            actor_id=actor_id,
            evidence=body.evidence,
            reason=body.reason,
        )
        await db.commit()
        if report["status"] != "ready":
            return success_response(data=report)
        dispatched = await _drain_pending_incremental(payload)
        return success_response(
            data={
                **report,
                "status": "resume_requested" if dispatched else report["status"],
                "dispatch_accepted": dispatched,
            }
        )
    except BillingError:
        await db.rollback()
        return error_response("incremental_recovery_invalid", status_code=400)
    except Exception as exc:
        await db.rollback()
        logger.warning(
            "Incremental recovery unavailable queue={} type={}",
            item_id,
            type(exc).__name__,
        )
        return error_response("incremental_recovery_unavailable", status_code=503)


@router.get("/stats")
async def queue_stats(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """队列统计"""
    status_counts = (
        await db.execute(
            select(ReviewQueue.status, func.count(ReviewQueue.id)).group_by(
                ReviewQueue.status
            )
        )
    ).all()

    stats = {row[0]: row[1] for row in status_counts}
    stats["total"] = sum(stats.values())

    return success_response(data=stats)


@router.get("/items")
async def list_queue_items(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
    search: str = Query("", description="搜索关键词"),
    repo: str = Query("", description="仓库名过滤"),
    status: str = Query("", description="状态过滤"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """队列列表"""
    query = select(ReviewQueue)
    count_query = select(func.count(ReviewQueue.id))

    if search:
        escaped = search.replace("%", r"\%").replace("_", r"\_")
        search_filter = or_(
            ReviewQueue.repo_name.ilike(f"%{escaped}%", escape="\\"),
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    if repo:
        query = query.where(ReviewQueue.repo_name == repo)
        count_query = count_query.where(ReviewQueue.repo_name == repo)
    if status:
        query = query.where(ReviewQueue.status == status)
        count_query = count_query.where(ReviewQueue.status == status)

    query = query.order_by(desc(ReviewQueue.created_at))

    items, total, total_pages, page = await paginate(
        db, query, count_query, page, per_page
    )

    data = [
        {
            "id": item.id,
            "pr_id": item.pr_id,
            "repo_name": item.repo_name,
            "action": item.action,
            "priority": item.priority,
            "status": item.status,
            "retry_count": item.retry_count,
            "max_retries": item.max_retries,
            "error_message": item.error_message,
            "created_at": format_rfc3339(item.created_at) if item.created_at else None,
            "updated_at": format_rfc3339(item.updated_at) if item.updated_at else None,
        }
        for item in items
    ]

    return paginated_response(data, total, page, total_pages, per_page)


@router.get("/items/{item_id}")
async def get_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """队列项详情"""
    result = await db.execute(select(ReviewQueue).where(ReviewQueue.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        return error_response("队列项不存在", status_code=404)

    return success_response(
        data={
            "id": item.id,
            "pr_id": item.pr_id,
            "repo_name": item.repo_name,
            "action": item.action,
            "priority": item.priority,
            "status": item.status,
            "retry_count": item.retry_count,
            "max_retries": item.max_retries,
            "error_message": item.error_message,
            "created_at": format_rfc3339(item.created_at) if item.created_at else None,
            "updated_at": format_rfc3339(item.updated_at) if item.updated_at else None,
        }
    )


@router.post("/items/{item_id}/retry")
async def retry_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """重试失败的队列项"""
    result = await db.execute(select(ReviewQueue).where(ReviewQueue.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        return error_response("队列项不存在", status_code=404)
    if item.status != "failed":
        return error_response("只能重试失败的队列项", status_code=400)

    item.status = "pending"
    item.error_message = None
    await db.commit()

    return success_response(message="队列项已重新加入队列")


@router.delete("/items/{item_id}")
async def delete_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """删除队列项"""
    result = await db.execute(select(ReviewQueue).where(ReviewQueue.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        return error_response("队列项不存在", status_code=404)

    await db.delete(item)
    await db.commit()

    return success_response(message="队列项已删除")


@router.post("/purge")
async def purge_queue(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
    status: str = Query("completed", description="清理状态（completed/failed）"),
):
    """批量清理已完成/失败的队列项"""
    valid_statuses = ("completed", "failed")
    if status not in valid_statuses:
        return error_response(
            f"无效的状态，可选值: {', '.join(valid_statuses)}", status_code=400
        )

    count_result = await db.execute(
        select(func.count(ReviewQueue.id)).where(ReviewQueue.status == status)
    )
    count = count_result.scalar() or 0

    if count == 0:
        return success_response(data={"deleted": 0}, message="无需清理的队列项")

    # 批量删除
    from sqlalchemy import delete

    await db.execute(delete(ReviewQueue).where(ReviewQueue.status == status))
    await db.commit()

    return success_response(
        data={"deleted": count},
        message=f"已清理 {count} 个{('已完成' if status == 'completed' else '失败')}的队列项",
    )
