"""Agent 专家团队 Worker（当前仅支持超级管理员手动触发）"""

import json
from datetime import datetime

from loguru import logger

from backend.models.agent_team_models import AgentTeamTaskStatus
from backend.models.database import async_session
from backend.services.agent_team.ai_client import load_agent_team_ai_config


class AgentTeamWorker:
    """Agent 专家团队任务处理器骨架。"""

    async def process_task(self, task_id: int) -> int:
        """处理 Agent 专家团队任务。

        当前阶段仅完成状态流转与专用 AI 配置校验，为后续两角色执行器预留入口。
        """
        config = await load_agent_team_ai_config()
        config.validate()

        async with async_session() as session:
            from sqlalchemy import select

            from backend.models.agent_team_models import AgentTeamTask

            result = await session.execute(
                select(AgentTeamTask).where(AgentTeamTask.id == task_id)
            )
            task = result.scalar_one_or_none()
            if not task:
                raise ValueError(f"AgentTeamTask 不存在: {task_id}")

            task.status = AgentTeamTaskStatus.WAITING_HUMAN.value
            task.current_phase = "bootstrap"
            task.started_at = task.started_at or datetime.utcnow()
            task.ai_config_snapshot = json.dumps(
                config.safe_snapshot(), ensure_ascii=False
            )
            task.error_message = "Agent 专家团队执行器骨架已创建，代码修改执行将在后续阶段启用。"
            await session.commit()

        logger.info("Agent 专家团队任务已进入等待人工状态: {}", task_id)
        return task_id


_worker: AgentTeamWorker | None = None


def get_worker() -> AgentTeamWorker:
    """获取 AgentTeamWorker 单例。"""
    global _worker
    if _worker is None:
        _worker = AgentTeamWorker()
    return _worker


async def submit_agent_team_task(task_id: int) -> int:
    """提交 Agent 专家团队任务（同步等待骨架处理完成）。"""
    return await get_worker().process_task(task_id)
