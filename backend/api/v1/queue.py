"""API v1 队列监控端点"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_admin
from backend.api.v1.responses import (
    error_response,
    paginated_response,
    success_response,
)
from backend.models.database import ReviewQueue
from backend.webui.deps import get_db, paginate

router = APIRouter(prefix="/queue", tags=["Queue"])


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
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
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
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
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
