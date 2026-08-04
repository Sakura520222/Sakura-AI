"""Agent Team conversational handoff context service."""

from __future__ import annotations

import json

from sqlalchemy import desc, select

from backend.models import database as db_module
from backend.models.agent_team_models import AgentTeamConversationContext
from backend.services.agent_team.fullstack_expert import FullStackResult
from backend.services.agent_team.professional_reviewer import (
    ReviewFinding,
    ReviewResult,
)
from backend.services.ai_reviewer.message_utils import estimate_messages_tokens


class AgentTeamConversationContextService:
    """Persist and build structured handoff context between Agent roles."""

    def __init__(self, task_id: int | None):
        self.task_id = task_id

    async def record_fullstack_turn(
        self,
        iteration_number: int,
        result: FullStackResult,
    ) -> None:
        if not self.task_id:
            return
        summary = _build_fullstack_summary(iteration_number, result)
        await self._record_context(
            iteration_number=iteration_number,
            source_role="fullstack",
            target_role="reviewer",
            summary=summary,
            modified_files=result.modified_files,
            unresolved_items=[],
        )

    async def record_reviewer_turn(
        self,
        iteration_number: int,
        result: ReviewResult,
    ) -> None:
        if not self.task_id:
            return
        unresolved_items = _review_unresolved_items(result)
        await self._record_context(
            iteration_number=iteration_number,
            source_role="reviewer",
            target_role="fullstack",
            summary=_build_reviewer_summary(iteration_number, result),
            modified_files=[],
            unresolved_items=unresolved_items,
        )

    async def build_handoff_context(
        self,
        target_role: str,
        before_iteration: int,
        limit: int = 6,
    ) -> str:
        if not self.task_id:
            return ""
        contexts = await self._load_contexts(target_role, before_iteration, limit)
        if not contexts:
            return ""
        parts = ["## 专家对话上下文"]
        for item in contexts:
            parts.append(
                f"### 第 {item.iteration_number} 轮 {item.source_role} → {target_role}\n"
                f"{item.summary}"
            )
            unresolved = _loads_list(item.unresolved_items_json)
            if unresolved:
                parts.append("未解决事项:\n" + "\n".join(f"- {x}" for x in unresolved))
            modified_files = _loads_list(item.modified_files_json)
            if modified_files:
                parts.append(
                    "相关文件:\n" + "\n".join(f"- `{x}`" for x in modified_files)
                )
        return "\n\n".join(parts)

    async def build_role_memory(
        self,
        role_name: str,
        before_iteration: int,
        limit: int = 4,
    ) -> str:
        if not self.task_id:
            return ""
        contexts = await self._load_role_contexts(role_name, before_iteration, limit)
        if not contexts:
            return ""
        parts = [f"## {role_name} 历史记忆"]
        for item in contexts:
            parts.append(f"### 第 {item.iteration_number} 轮\n{item.summary}")
        return "\n\n".join(parts)

    async def _record_context(
        self,
        iteration_number: int,
        source_role: str,
        target_role: str,
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


def _build_fullstack_summary(iteration_number: int, result: FullStackResult) -> str:
    parts = [
        f"第 {iteration_number} 轮全栈专家完成代码修改。",
        f"执行结果: {'成功' if result.success else '未完全成功'}",
        f"总结: {result.summary}",
        f"风险等级: {result.risk_level}",
    ]
    if result.test_result:
        parts.append(f"测试结果: {result.test_result}")
    return "\n".join(parts)


def _build_reviewer_summary(iteration_number: int, result: ReviewResult) -> str:
    parts = [
        f"第 {iteration_number} 轮专业审查完成。",
        f"审查结论: {result.verdict}，分数: {result.score}/10",
        f"总结: {result.summary}",
    ]
    if result.findings:
        parts.append("发现的问题:")
        parts.extend(_format_finding(item) for item in result.findings)
    if result.improvement_suggestions:
        parts.append("改进建议:")
        parts.extend(f"- {item}" for item in result.improvement_suggestions)
    return "\n".join(parts)


def _review_unresolved_items(result: ReviewResult) -> list[str]:
    items = [_format_finding(item) for item in result.findings]
    items.extend(result.improvement_suggestions)
    return items


def _format_finding(finding: ReviewFinding) -> str:
    suggestion = f" 建议: {finding.suggestion}" if finding.suggestion else ""
    return f"- [{finding.severity}] {finding.file}: {finding.message}{suggestion}"


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
