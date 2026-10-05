"""Agent Team conversation checkpoint persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import database as db_module
from backend.models.agent_team_models import (
    AgentTeamMessage,
    AgentTeamSession,
    AgentTeamTask,
    AgentTeamToolCall,
    AgentTeamUserPrompt,
)
from backend.models.database import utc_now


@dataclass(frozen=True)
class ResumeCursor:
    """可恢复会话游标。"""

    session_id: int
    iteration_number: int
    role_name: str
    status: str


class ConversationCheckpointService:
    """追加式保存和恢复 Agent messages。"""

    def __init__(self, task_id: int):
        self.task_id = task_id

    async def create_session(
        self,
        iteration_number: int,
        role_name: str,
        model: str | None = None,
        resume_index: int = 0,
    ) -> AgentTeamSession:
        async with db_module.async_session() as session:
            agent_session = AgentTeamSession(
                task_id=self.task_id,
                iteration_number=iteration_number,
                role_name=role_name,
                resume_index=resume_index,
                status="running",
                model=model,
            )
            session.add(agent_session)
            await session.commit()
            await session.refresh(agent_session)

        await _publish(
            "agent:session_started",
            {
                "task_id": self.task_id,
                "session_id": agent_session.id,
                "iteration": iteration_number,
                "role_name": role_name,
            },
        )
        return agent_session

    async def create_session_from_history(
        self,
        iteration_number: int,
        messages: list[dict[str, Any]],
        tool_states: dict[str, dict[str, Any]],
        resume_index: int = 0,
    ) -> AgentTeamSession:
        """Publish a migrated session only after its entire ledger is durable.

        Session creation, message copying, result links and original call states
        share one transaction. A cancellation/crash before commit leaves the
        original session as the latest cursor; after commit the new session has
        all recovery evidence. In particular, a copied assistant can never
        expose a completed or uncertain mutation as a runnable pending call.
        """
        async with db_module.async_session() as session:
            agent_session = AgentTeamSession(
                task_id=self.task_id,
                iteration_number=iteration_number,
                role_name="agent",
                resume_index=resume_index,
                status="running",
            )
            session.add(agent_session)
            await session.flush()
            result_ids: dict[str, int] = {}
            for message in messages:
                row = await self.append_message_in_session(
                    session,
                    agent_session.id,
                    message,
                    publish_event=False,
                )
                if message.get("role") == "tool":
                    result_ids[message.get("tool_call_id")] = row.id
            rows = await session.execute(
                select(AgentTeamToolCall).where(
                    AgentTeamToolCall.session_id == agent_session.id
                )
            )
            for call in rows.scalars():
                state = tool_states.get(call.tool_call_id, {})
                # Missing original evidence is uncertain, never a fresh call.
                call.status = state.get("status") or "cancelled"
                call.result_message_id = result_ids.get(call.tool_call_id)
                if state.get("name") is not None:
                    call.name = state["name"]
                if state.get("arguments_hash") is not None:
                    call.arguments_hash = state["arguments_hash"]
                if call.status in {"completed", "failed", "cancelled"}:
                    call.completed_at = utc_now()
            await session.commit()
            await session.refresh(agent_session)

        await _publish(
            "agent:session_started",
            {
                "task_id": self.task_id,
                "session_id": agent_session.id,
                "iteration": iteration_number,
                "role_name": "agent",
            },
        )
        return agent_session

    async def load_messages(self, session_id: int) -> list[dict[str, Any]]:
        async with db_module.async_session() as session:
            result = await session.execute(
                select(AgentTeamMessage)
                .where(AgentTeamMessage.session_id == session_id)
                .order_by(AgentTeamMessage.seq)
            )
            messages = []
            for item in result.scalars():
                messages.append(json.loads(item.message_json))
            return messages

    async def append_message(
        self,
        session_id: int,
        message: dict[str, Any],
        finish_reason: str | None = None,
    ) -> int:
        async with db_module.async_session() as session:
            msg = await self.append_message_in_session(
                session,
                session_id=session_id,
                message=message,
                finish_reason=finish_reason,
            )
            await session.commit()
            return msg.id

    async def append_guidance_message(
        self,
        session_id: int,
        message: dict[str, Any],
        prompt_ids: tuple[int, ...] | list[int],
    ) -> int:
        """Persist a guidance user message and consume its queue atomically.

        The queue rows are deliberately updated in the same transaction as
        the checkpoint message. A retry can therefore inspect the stable
        ``guidance_ids`` on the message and never acknowledge text that was
        not actually admitted to the model context.
        """
        ids = tuple(int(item) for item in prompt_ids)
        async with db_module.async_session() as session:
            msg = await self.append_message_in_session(
                session,
                session_id=session_id,
                message=message,
            )
            if ids:
                result = await session.execute(
                    select(AgentTeamUserPrompt).where(
                        AgentTeamUserPrompt.task_id == self.task_id,
                        AgentTeamUserPrompt.id.in_(ids),
                        AgentTeamUserPrompt.status == "pending",
                    )
                )
                for prompt in result.scalars().all():
                    prompt.status = "consumed"
                    prompt.consumed_at = utc_now()
            await session.commit()
            return msg.id

    async def append_message_in_session(
        self,
        db: AsyncSession,
        session_id: int,
        message: dict[str, Any],
        finish_reason: str | None = None,
        *,
        publish_event: bool = True,
    ) -> AgentTeamMessage:
        # Lock allocation together with the insert, including guidance writers
        # in other transactions. A batch never allocates message seq concurrently.
        agent_session = await db.get(AgentTeamSession, session_id, with_for_update=True)
        if agent_session is None:
            raise ValueError(f"AgentTeamSession 不存在: {session_id}")

        seq = int(agent_session.last_seq or 0) + 1
        msg = AgentTeamMessage(
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

        agent_session.last_seq = seq
        task = await db.get(AgentTeamTask, self.task_id)
        if task is not None:
            task.last_checkpoint_at = utc_now()

        tool_calls = message.get("tool_calls") or []
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                parsed = _normalize_tool_call(tool_call)
                db.add(
                    AgentTeamToolCall(
                        session_id=session_id,
                        assistant_message_id=msg.id,
                        tool_call_id=parsed["id"],
                        name=parsed["name"],
                        arguments_json=parsed["arguments"],
                        arguments_hash=_hash_arguments(parsed["arguments"]),
                        status="pending",
                    )
                )

        if publish_event:
            await _publish(
                "agent:message_added",
                {
                    "task_id": self.task_id,
                    "session_id": session_id,
                    "msg_id": msg.id,
                    "role": msg.role,
                    "seq": seq,
                },
            )
        return msg

    async def mark_tool_call_running(self, session_id: int, tool_call_id: str) -> None:
        async with db_module.async_session() as session:
            tool_call = await self._get_tool_call(session, session_id, tool_call_id)
            if tool_call is None:
                raise ValueError("Tool call checkpoint is missing")
            if tool_call.status == "completed" or tool_call.result_message_id:
                raise ValueError("Tool call already completed; cannot execute again")
            tool_call.status = "running"
            tool_call.started_at = utc_now()
            await session.commit()
            tool_name = tool_call.name

        await _publish(
            "agent:tool_started",
            {
                "task_id": self.task_id,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
            },
        )

    async def load_tool_call_states(self, session_id: int) -> dict[str, dict[str, Any]]:
        async with db_module.async_session() as session:
            rows = await session.execute(
                select(AgentTeamToolCall).where(
                    AgentTeamToolCall.session_id == session_id
                )
            )
            return {
                row.tool_call_id: {
                    "name": row.name,
                    "arguments_hash": row.arguments_hash,
                    "status": row.status,
                    "result_message_id": row.result_message_id,
                }
                for row in rows.scalars()
            }

    async def record_tool_result(
        self,
        session_id: int,
        tool_call_id: str,
        message: dict[str, Any],
        status: str,
        error: str = "",
    ) -> int:
        """Atomically append a result and commit its call state.

        A crash cannot leave a successful mutation result with a stale running
        status (or vice versa). Callers serialize this method in model order.
        """
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("Invalid tool result status")
        async with db_module.async_session() as session:
            message_row = await self.append_message_in_session(
                session, session_id, message
            )
            call = await self._get_tool_call(session, session_id, tool_call_id)
            if call is None:
                raise ValueError("Tool call checkpoint is missing")
            call.status = status
            call.result_message_id = message_row.id
            call.completed_at = utc_now()
            call.error_message = error or None
            await session.commit()
            message_id = message_row.id
        await _publish(
            f"agent:tool_{status}",
            {
                "task_id": self.task_id,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
            },
        )
        return message_id

    async def mark_tool_call_cancelled(
        self, session_id: int, tool_call_id: str
    ) -> None:
        async with db_module.async_session() as session:
            call = await self._get_tool_call(session, session_id, tool_call_id)
            if call is None:
                return
            call.status = "cancelled"
            call.completed_at = utc_now()
            call.error_message = "Interrupted; reconcile mutations before retry"
            await session.commit()

    async def finish_session(
        self, session_id: int, outcome: str, payload: dict[str, Any]
    ) -> None:
        """Commit the structured outcome and matching session status together."""
        if outcome not in {"success", "cancelled", "blocked", "unrecoverable_error"}:
            raise ValueError("Invalid Agent outcome")
        status = "completed" if outcome == "success" else outcome
        async with db_module.async_session() as session:
            row = await session.get(AgentTeamSession, session_id)
            if row is None:
                raise ValueError("Agent session checkpoint is missing")
            row.status = status
            row.result_payload = json.dumps(payload, ensure_ascii=False, default=str)
            row.tool_calls_count = payload.get("tool_calls_count", 0)
            row.error_message = payload.get("error") or None
            row.completed_at = utc_now()
            await session.commit()
        await _publish(
            "agent:session_completed",
            {"task_id": self.task_id, "session_id": session_id, "status": status},
        )

    async def mark_tool_call_completed(
        self,
        session_id: int,
        tool_call_id: str,
        result_message_id: int,
    ) -> None:
        async with db_module.async_session() as session:
            tool_call = await self._get_tool_call(session, session_id, tool_call_id)
            if tool_call is None:
                return
            tool_call.status = "completed"
            tool_call.result_message_id = result_message_id
            tool_call.completed_at = utc_now()
            tool_call.error_message = None
            await session.commit()
            tool_name = tool_call.name

        await _publish(
            "agent:tool_completed",
            {
                "task_id": self.task_id,
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
        async with db_module.async_session() as session:
            tool_call = await self._get_tool_call(session, session_id, tool_call_id)
            if tool_call is None:
                return
            tool_call.status = "failed"
            tool_call.error_message = error_message
            await session.commit()
            tool_name = tool_call.name

        await _publish(
            "agent:tool_failed",
            {
                "task_id": self.task_id,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "error": error_message,
            },
        )

    async def complete_session(
        self,
        session_id: int,
        tool_calls_count: int = 0,
    ) -> None:
        await self._set_session_status(session_id, "completed", tool_calls_count)

    async def fail_session(self, session_id: int, error_message: str) -> None:
        await self._set_session_status(
            session_id, "failed", error_message=error_message
        )

    async def get_resume_cursor(self) -> ResumeCursor | None:
        async with db_module.async_session() as session:
            result = await session.execute(
                select(AgentTeamSession)
                .where(
                    AgentTeamSession.task_id == self.task_id,
                )
                .order_by(desc(AgentTeamSession.id))
                .limit(1)
            )
            agent_session = result.scalar_one_or_none()
            if agent_session is None:
                return None
            return ResumeCursor(
                session_id=agent_session.id,
                iteration_number=agent_session.iteration_number,
                role_name=agent_session.role_name,
                status=agent_session.status,
            )

    async def get_latest_completed_session(
        self, iteration_number: int, role_name: str
    ) -> int | None:
        async with db_module.async_session() as session:
            result = await session.execute(
                select(AgentTeamSession.id)
                .where(
                    AgentTeamSession.task_id == self.task_id,
                    AgentTeamSession.iteration_number == iteration_number,
                    AgentTeamSession.role_name == role_name,
                    AgentTeamSession.status == "completed",
                )
                .order_by(desc(AgentTeamSession.id))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def has_resume_state(self) -> bool:
        return await self.get_resume_cursor() is not None

    async def save_session_result(
        self, session_id: int, payload: dict[str, Any]
    ) -> None:
        """Persist a structured result payload (FullStackResult or ReviewResult)."""
        async with db_module.async_session() as session:
            agent_session = await session.get(AgentTeamSession, session_id)
            if agent_session is None:
                return
            agent_session.result_payload = json.dumps(
                payload, ensure_ascii=False, default=str
            )
            await session.commit()

    async def load_session_result(self, session_id: int) -> dict[str, Any] | None:
        """Load a previously persisted structured result from the session."""
        async with db_module.async_session() as session:
            agent_session = await session.get(AgentTeamSession, session_id)
            if agent_session is None or not agent_session.result_payload:
                return None
            try:
                return json.loads(agent_session.result_payload)
            except json.JSONDecodeError:
                return None

    async def _set_session_status(
        self,
        session_id: int,
        status: str,
        tool_calls_count: int = 0,
        error_message: str | None = None,
    ) -> None:
        async with db_module.async_session() as session:
            agent_session = await session.get(AgentTeamSession, session_id)
            if agent_session is None:
                return
            agent_session.status = status
            if tool_calls_count:
                agent_session.tool_calls_count = tool_calls_count
            agent_session.error_message = error_message
            agent_session.completed_at = utc_now()
            await session.commit()
            role_name = agent_session.role_name

        await _publish(
            "agent:session_completed",
            {
                "task_id": self.task_id,
                "session_id": session_id,
                "role_name": role_name,
                "status": status,
            },
        )

    async def _get_tool_call(
        self,
        db: AsyncSession,
        session_id: int,
        tool_call_id: str,
    ) -> AgentTeamToolCall | None:
        result = await db.execute(
            select(AgentTeamToolCall).where(
                AgentTeamToolCall.session_id == session_id,
                AgentTeamToolCall.tool_call_id == tool_call_id,
            )
        )
        return result.scalar_one_or_none()


def _normalize_tool_call(tool_call: Any) -> dict[str, str]:
    if isinstance(tool_call, dict):
        function = tool_call.get("function") or {}
        return {
            "id": str(tool_call.get("id") or ""),
            "name": str(function.get("name") or ""),
            "arguments": str(function.get("arguments") or ""),
        }
    function = getattr(tool_call, "function", None)
    return {
        "id": str(getattr(tool_call, "id", "")),
        "name": str(getattr(function, "name", "")),
        "arguments": str(getattr(function, "arguments", "")),
    }


async def _publish(event_type: str, data: dict[str, Any]) -> None:
    """发布 SSE 事件（延迟导入避免循环依赖）。"""
    try:
        from backend.webui.sse import publish_event

        await publish_event(event_type, data)
    except Exception as exc:
        logger.debug("SSE 发布事件失败: {}", exc)


def _hash_arguments(arguments: str) -> str:
    return hashlib.sha256(arguments.encode("utf-8")).hexdigest()
