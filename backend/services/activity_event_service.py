"""实时活动事件服务 — 持久化事件 + SSE 推送，驱动前端对话流 UI。"""

import json
from typing import Any

from loguru import logger
from sqlalchemy import select

from backend.models import database as db_module
from backend.models.activity_event_models import ActivityEvent


class ActivityEventService:
    """实时活动事件服务。"""

    @staticmethod
    async def log_event(
        task_type: str,
        task_id: int,
        event_type: str,
        content: dict[str, Any] | None = None,
    ) -> ActivityEvent | None:
        """记录事件并发布 SSE 通知。

        Args:
            task_type: 任务类型 ('pr' | 'issue' | 'scan')
            task_id: 任务 ID（对应数据库主键）
            event_type: 事件类型 (status / thinking / tool_call / tool_result / ai_response / error / result)
            content: 事件内容（将被序列化为 JSON）
        """
        try:
            async with db_module.async_session() as db:
                event = ActivityEvent(
                    task_type=task_type,
                    task_id=task_id,
                    event_type=event_type,
                    content=json.dumps(content, ensure_ascii=False, default=str)
                    if content
                    else None,
                )
                db.add(event)
                await db.commit()
                await db.refresh(event)

            # 发布 SSE 事件通知前端
            await _publish_activity_event(event, content)
            return event
        except Exception as exc:
            logger.warning("活动事件记录失败（不影响主流程）: {}", exc)
            return None

    @staticmethod
    async def log_events_batch(
        task_type: str,
        task_id: int,
        events: list[tuple[str, dict[str, Any] | None]],
    ) -> list[ActivityEvent]:
        """批量记录事件（减少 DB 往返）。"""
        results: list[ActivityEvent] = []
        if not events:
            return results
        try:
            async with db_module.async_session() as db:
                for event_type, content in events:
                    event = ActivityEvent(
                        task_type=task_type,
                        task_id=task_id,
                        event_type=event_type,
                        content=json.dumps(content, ensure_ascii=False, default=str)
                        if content
                        else None,
                    )
                    db.add(event)
                    results.append(event)
                await db.commit()
                for event in results:
                    await db.refresh(event)

            # 批量发布 SSE 事件
            for event, (_, content) in zip(results, events):
                await _publish_activity_event(event, content)
            return results
        except Exception as exc:
            logger.warning("批量活动事件记录失败（不影响主流程）: {}", exc)
            return []

    @staticmethod
    async def get_events(
        task_type: str,
        task_id: int,
        after_id: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """查询指定任务的事件列表。"""
        async with db_module.async_session() as db:
            query = (
                select(ActivityEvent)
                .where(
                    ActivityEvent.task_type == task_type,
                    ActivityEvent.task_id == task_id,
                    ActivityEvent.id > after_id,
                )
                .order_by(ActivityEvent.id)
                .limit(limit)
            )
            result = await db.execute(query)
            rows = result.scalars().all()
            return [_event_to_dict(e) for e in rows]

    @staticmethod
    async def get_latest_event_id(
        task_type: str,
        task_id: int,
    ) -> int:
        """获取指定任务的最新事件 ID（用于增量拉取起点）。"""
        async with db_module.async_session() as db:
            result = await db.scalar(
                select(ActivityEvent.id)
                .where(
                    ActivityEvent.task_type == task_type,
                    ActivityEvent.task_id == task_id,
                )
                .order_by(ActivityEvent.id.desc())
                .limit(1)
            )
            return result or 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _publish_activity_event(
    event: ActivityEvent,
    content: dict[str, Any] | None,
) -> None:
    """发布 SSE 事件到前端。"""
    try:
        from backend.webui.sse import publish_event

        await publish_event(
            "activity:event",
            {
                "id": event.id,
                "task_type": event.task_type,
                "task_id": event.task_id,
                "event_type": event.event_type,
                "content": content,
                "created_at": (
                    event.created_at.isoformat() if event.created_at else None
                ),
            },
        )
    except Exception as exc:
        logger.debug("SSE 发布活动事件失败: {}", exc, exc_info=True)


def _event_to_dict(event: ActivityEvent) -> dict[str, Any]:
    """将 ActivityEvent 转为前端友好的 dict。"""
    content = None
    if event.content:
        try:
            content = json.loads(event.content)
        except json.JSONDecodeError, TypeError:
            content = {"raw": event.content}
    return {
        "id": event.id,
        "task_type": event.task_type,
        "task_id": event.task_id,
        "event_type": event.event_type,
        "content": content,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
