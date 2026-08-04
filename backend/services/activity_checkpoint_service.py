"""Activity Checkpoint Service — mirrors ConversationCheckpointService
for PR review, Issue analysis, and Repo scan tasks.

Uses the same Session / Message / ToolCall pattern and publishes
the same SSE events so the Agent Team live-view frontend component
renders them identically.
"""

import json
from typing import Any

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import database as db_module
from backend.models.activity_conversation_models import (
    ActivityMessage,
    ActivitySession,
    ActivityToolCall,
)
from backend.models.database import utc_now


def _normalize_tool_call(tc: Any) -> dict[str, str]:
    """Normalize a tool_call object from OpenAI response into a dict."""
    if isinstance(tc, dict):
        return {
            "id": tc.get("id", ""),
            "name": (tc.get("function") or {}).get("name", ""),
            "arguments": (tc.get("function") or {}).get("arguments", ""),
        }
    return {
        "id": getattr(tc, "id", ""),
        "name": getattr(getattr(tc, "function", None), "name", ""),
        "arguments": getattr(getattr(tc, "function", None), "arguments", ""),
    }


async def _publish(event_type: str, data: dict[str, Any]) -> None:
    """Publish SSE event (lazy import to avoid circular deps)."""
    try:
        from backend.webui.sse import publish_event

        await publish_event(event_type, data)
    except Exception as exc:
        logger.debug("SSE publish failed: {}", exc)


class ActivityCheckpointService:
    """追加式保存 activity conversation messages.

    Mirrors ConversationCheckpointService but for PR/Issue/Scan tasks.
    Publishes ``activity:*`` SSE events that the frontend listens to.
    """

    def __init__(self, source_type: str, source_task_id: int):
        self.source_type = source_type
        self.source_task_id = source_task_id

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def create_session(
        self,
        iteration_number: int = 1,
        role_name: str = "reviewer",
        model: str | None = None,
    ) -> ActivitySession:
        async with db_module.async_session() as db:
            session = ActivitySession(
                source_type=self.source_type,
                source_task_id=self.source_task_id,
                iteration_number=iteration_number,
                role_name=role_name,
                status="running",
                model=model,
            )
            db.add(session)
            await db.commit()
            await db.refresh(session)

            # session.id 已由 refresh 填充，在块内发布事件确保数据一致性
            await _publish(
                "activity:session_started",
                {
                    "task_type": self.source_type,
                    "task_id": self.source_task_id,
                    "session_id": session.id,
                    "iteration": iteration_number,
                    "role_name": role_name,
                },
            )
        return session

    async def complete_session(
        self,
        session_id: int,
        tool_calls_count: int = 0,
    ) -> None:
        await self._set_session_status(session_id, "completed", tool_calls_count)

    async def fail_session(
        self,
        session_id: int,
        error_message: str,
    ) -> None:
        await self._set_session_status(
            session_id, "failed", error_message=error_message
        )

    async def save_session_result(
        self,
        session_id: int,
        payload: dict[str, Any],
    ) -> None:
        async with db_module.async_session() as db:
            session = await db.get(ActivitySession, session_id)
            if session is None:
                return
            session.result_payload = json.dumps(
                payload, ensure_ascii=False, default=str
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def append_message(
        self,
        session_id: int,
        message: dict[str, Any],
        finish_reason: str | None = None,
    ) -> ActivityMessage:
        """Append a message and persist. Returns the new ActivityMessage."""
        async with db_module.async_session() as db:
            msg = await self._append_message_in_db(
                db, session_id, message, finish_reason
            )
            await db.commit()
            return msg

    async def load_messages(self, session_id: int) -> list[dict[str, Any]]:
        """Load persisted messages in original OpenAI-compatible shape."""
        async with db_module.async_session() as db:
            result = await db.execute(
                select(ActivityMessage)
                .where(ActivityMessage.session_id == session_id)
                .order_by(ActivityMessage.seq)
            )
            rows = result.scalars().all()

        messages: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row.message_json)
            except TypeError, json.JSONDecodeError:
                payload = {
                    "role": row.role,
                    "content": row.content,
                }
                if row.tool_call_id:
                    payload["tool_call_id"] = row.tool_call_id
            if isinstance(payload, dict):
                messages.append(payload)
        return messages

    async def get_latest_completed_session(
        self,
        source_task_id: int | None = None,
        role_name: str = "reviewer",
    ) -> ActivitySession | None:
        """Return the latest completed session for this activity source."""
        async with db_module.async_session() as db:
            result = await db.execute(
                select(ActivitySession)
                .where(
                    ActivitySession.source_type == self.source_type,
                    ActivitySession.source_task_id
                    == (source_task_id or self.source_task_id),
                    ActivitySession.role_name == role_name,
                    ActivitySession.status == "completed",
                )
                .order_by(
                    desc(ActivitySession.completed_at),
                    desc(ActivitySession.id),
                )
            )
            return result.scalars().first()

    async def copy_messages_to_session(
        self,
        source_session_id: int,
        target_session_id: int,
        messages: list[dict[str, Any]] | None = None,
    ) -> int:
        """Copy full message history into another activity session.

        Args:
            source_session_id: 源会话 ID。
            target_session_id: 目标会话 ID。
            messages: 可选的预加载消息列表。传入时跳过内部 load_messages 查询，
                避免调用方已加载过同一会话时的重复 DB 往返；为 None 时内部自行加载。
        """
        copied = 0
        if messages is None:
            messages = await self.load_messages(source_session_id)
        async with db_module.async_session() as db:
            for message in messages:
                await self._append_message_in_db(db, target_session_id, message)
                copied += 1
            await db.commit()
        return copied

    async def _append_message_in_db(
        self,
        db: AsyncSession,
        session_id: int,
        message: dict[str, Any],
        finish_reason: str | None = None,
    ) -> ActivityMessage:
        """Core append — caller must commit."""
        act_session = await db.get(ActivitySession, session_id)
        if act_session is None:
            raise ValueError(f"ActivitySession not found: {session_id}")

        seq = int(act_session.last_seq or 0) + 1
        msg = ActivityMessage(
            session_id=session_id,
            seq=seq,
            role=str(message.get("role") or ""),
            content=message.get("content"),
            message_json=json.dumps(message, ensure_ascii=False, default=str),
            tool_call_id=message.get("tool_call_id"),
            finish_reason=finish_reason,
        )
        db.add(msg)
        await db.flush()

        act_session.last_seq = seq

        # Create tool-call tracking rows for assistant messages with tool_calls
        tool_calls = message.get("tool_calls") or []
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                parsed = _normalize_tool_call(tc)
                db.add(
                    ActivityToolCall(
                        session_id=session_id,
                        assistant_message_id=msg.id,
                        tool_call_id=parsed["id"],
                        name=parsed["name"],
                        arguments_json=parsed["arguments"],
                        status="pending",
                    )
                )

        await _publish(
            "activity:message_added",
            {
                "task_type": self.source_type,
                "task_id": self.source_task_id,
                "session_id": session_id,
                "msg_id": msg.id,
                "role": msg.role,
                "seq": seq,
            },
        )
        return msg

    # ------------------------------------------------------------------
    # Tool call status
    # ------------------------------------------------------------------

    async def mark_tool_call_running(self, session_id: int, tool_call_id: str) -> None:
        async with db_module.async_session() as db:
            tc = await self._get_tool_call(db, session_id, tool_call_id)
            if tc is None:
                return
            tc.status = "running"
            tc.started_at = utc_now()
            await db.commit()
            tool_name = tc.name

        await _publish(
            "activity:tool_started",
            {
                "task_type": self.source_type,
                "task_id": self.source_task_id,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
            },
        )

    async def mark_tool_call_completed(
        self,
        session_id: int,
        tool_call_id: str,
        result_message_id: int,
    ) -> None:
        async with db_module.async_session() as db:
            tc = await self._get_tool_call(db, session_id, tool_call_id)
            if tc is None:
                return
            tc.status = "completed"
            tc.result_message_id = result_message_id
            tc.completed_at = utc_now()
            tc.error_message = None
            await db.commit()
            tool_name = tc.name

        await _publish(
            "activity:tool_completed",
            {
                "task_type": self.source_type,
                "task_id": self.source_task_id,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
            },
        )

    async def mark_tool_call_failed(
        self,
        session_id: int,
        tool_call_id: str,
        error_message: str,
    ) -> None:
        async with db_module.async_session() as db:
            tc = await self._get_tool_call(db, session_id, tool_call_id)
            if tc is None:
                return
            tc.status = "failed"
            tc.error_message = error_message
            await db.commit()
            tool_name = tc.name

        await _publish(
            "activity:tool_failed",
            {
                "task_type": self.source_type,
                "task_id": self.source_task_id,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "error": error_message,
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _set_session_status(
        self,
        session_id: int,
        status: str,
        tool_calls_count: int = 0,
        error_message: str | None = None,
    ) -> None:
        async with db_module.async_session() as db:
            session = await db.get(ActivitySession, session_id)
            if session is None:
                return
            session.status = status
            session.tool_calls_count = tool_calls_count
            if error_message:
                session.error_message = error_message
            if status in ("completed", "failed"):
                session.completed_at = utc_now()
            await db.commit()

        await _publish(
            "activity:session_completed",
            {
                "task_type": self.source_type,
                "task_id": self.source_task_id,
                "session_id": session_id,
                "status": status,
            },
        )

    @staticmethod
    async def _get_tool_call(
        db: AsyncSession, session_id: int, tool_call_id: str
    ) -> ActivityToolCall | None:
        result = await db.execute(
            select(ActivityToolCall).where(
                ActivityToolCall.session_id == session_id,
                ActivityToolCall.tool_call_id == tool_call_id,
            )
        )
        return result.scalar_one_or_none()
