"""Persisted context for the single Agent.

The database table intentionally keeps its historical role columns. New rows,
however, are written with ``source_role='agent'`` and ``target_role='agent'``;
old ``fullstack``/``reviewer`` rows are read as historical context only.
"""

from __future__ import annotations

import json

from sqlalchemy import desc, select

from backend.models import database as db_module
from backend.models.agent_team_models import AgentTeamConversationContext
from backend.services.agent_team.fullstack_expert import FullStackResult
from backend.services.ai_reviewer.message_utils import estimate_messages_tokens


class AgentTeamConversationContextService:
    """Store and load execution context for one Agent."""

    def __init__(self, task_id: int | None):
        self.task_id = task_id

    async def record_agent_turn(
        self,
        iteration_number: int,
        result: FullStackResult,
    ) -> None:
        """Record one implementation run without creating a role handoff."""
        if not self.task_id:
            return
        await self._record_context(
            iteration_number=iteration_number,
            source_role="agent",
            target_role="agent",
            summary=_build_agent_summary(iteration_number, result),
            modified_files=list(result.modified_files or []),
            unresolved_items=[],
        )

    async def record_fullstack_turn(
        self,
        iteration_number: int,
        result: FullStackResult,
    ) -> None:
        """Compatibility alias for callers from before the role migration."""
        await self.record_agent_turn(iteration_number, result)

    async def build_agent_context(
        self,
        before_iteration: int,
        limit: int = 6,
    ) -> str:
        """Build context from both new and legacy role rows.

        Reading historical ``fullstack``/``reviewer`` rows keeps checkpoint and
        context recovery useful after deployment, while the resulting text is
        presented to the Agent as ordinary user-layer context.
        """
        if not self.task_id:
            return ""
        contexts = await self._load_all_contexts(before_iteration, limit)
        return _format_contexts(contexts, target_role="agent")

    async def build_handoff_context(
        self,
        target_role: str,
        before_iteration: int,
        limit: int = 6,
    ) -> str:
        """Compatibility loader for old target-role queries.

        New callers should use :meth:`build_agent_context`. A request for the
        new ``agent`` role reads all historical context; old role labels retain
        their filtered read behavior but never cause a new reviewer session.
        """
        if target_role == "agent":
            return await self.build_agent_context(before_iteration, limit)
        if not self.task_id:
            return ""
        contexts = await self._load_contexts(target_role, before_iteration, limit)
        return _format_contexts(contexts, target_role=target_role)

    async def build_role_memory(
        self,
        role_name: str,
        before_iteration: int,
        limit: int = 4,
    ) -> str:
        """Load historical role memory without writing a role handoff."""
        if not self.task_id:
            return ""
        if role_name == "agent":
            contexts = await self._load_all_contexts(before_iteration, limit)
        else:
            # Legacy callers may ask for fullstack/reviewer memory while
            # restoring old data. This is read-only compatibility behavior.
            contexts = await self._load_role_contexts(role_name, before_iteration, limit)
        if not contexts:
            return ""
        parts = ["## Agent 历史执行上下文"]
        for item in contexts:
            parts.append(f"### 第 {item.iteration_number} 次执行\n{item.summary}")
        return "\n\n".join(parts)

    async def _record_context(
        self,
        iteration_number: int,
        source_role: str,
        target_role: str | None,
        summary: str,
        modified_files: list[str],
        unresolved_items: list[str],
    ) -> None:
        async with db_module.async_session() as session:
            context = AgentTeamConversationContext(
                task_id=self.task_id,
                iteration_number=iteration_number,
                source_role=source_role,
                target_role=target_role,
                summary=summary,
                unresolved_items_json=json.dumps(unresolved_items, ensure_ascii=False),
                modified_files_json=json.dumps(modified_files, ensure_ascii=False),
                token_estimate=estimate_messages_tokens(
                    [{"role": "user", "content": summary}]
                ),
            )
            session.add(context)
            await session.commit()

    async def _load_all_contexts(
        self,
        before_iteration: int,
        limit: int,
    ) -> list[AgentTeamConversationContext]:
        async with db_module.async_session() as session:
            result = await session.execute(
                select(AgentTeamConversationContext)
                .where(
                    AgentTeamConversationContext.task_id == self.task_id,
                    AgentTeamConversationContext.iteration_number < before_iteration,
                )
                .order_by(desc(AgentTeamConversationContext.iteration_number))
                .limit(limit)
            )
            return list(reversed(result.scalars().all()))

    async def _load_contexts(
        self,
        target_role: str,
        before_iteration: int,
        limit: int,
    ) -> list[AgentTeamConversationContext]:
        async with db_module.async_session() as session:
            result = await session.execute(
                select(AgentTeamConversationContext)
                .where(
                    AgentTeamConversationContext.task_id == self.task_id,
                    AgentTeamConversationContext.target_role == target_role,
                    AgentTeamConversationContext.iteration_number < before_iteration,
                )
                .order_by(desc(AgentTeamConversationContext.iteration_number))
                .limit(limit)
            )
            return list(reversed(result.scalars().all()))

    async def _load_role_contexts(
        self,
        role_name: str,
        before_iteration: int,
        limit: int,
    ) -> list[AgentTeamConversationContext]:
        async with db_module.async_session() as session:
            result = await session.execute(
                select(AgentTeamConversationContext)
                .where(
                    AgentTeamConversationContext.task_id == self.task_id,
                    AgentTeamConversationContext.source_role == role_name,
                    AgentTeamConversationContext.iteration_number < before_iteration,
                )
                .order_by(desc(AgentTeamConversationContext.iteration_number))
                .limit(limit)
            )
            return list(reversed(result.scalars().all()))


def _build_agent_summary(iteration_number: int, result: FullStackResult) -> str:
    parts = [
        f"第 {iteration_number} 次 Agent 执行完成。",
        f"执行结果: {'成功' if result.success else '未完全成功'}",
        f"总结: {result.summary}",
        f"风险等级: {result.risk_level}",
    ]
    if result.test_result:
        parts.append(f"测试结果: {result.test_result}")
    return "\n".join(parts)


def _format_contexts(
    contexts: list[AgentTeamConversationContext],
    target_role: str,
) -> str:
    if not contexts:
        return ""
    parts = ["## Agent 历史执行上下文"]
    for item in contexts:
        source_role = item.source_role or "legacy"
        parts.append(
            f"### 第 {item.iteration_number} 次执行 ({source_role} → {target_role})\n"
            f"{item.summary}"
        )
        unresolved = _loads_list(item.unresolved_items_json)
        if unresolved:
            parts.append("未解决事项:\n" + "\n".join(f"- {x}" for x in unresolved))
        modified_files = _loads_list(item.modified_files_json)
        if modified_files:
            parts.append("相关文件:\n" + "\n".join(f"- `{x}`" for x in modified_files))
    return "\n\n".join(parts)


def _loads_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


__all__ = ["AgentTeamConversationContextService"]
