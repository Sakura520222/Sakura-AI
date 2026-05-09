"""Agent 专家团队 Worker（当前仅支持超级管理员手动触发）"""

import json
from datetime import datetime

from loguru import logger

from backend.models.agent_team_models import AgentTeamTaskStatus
from backend.models.database import async_session
from backend.services.agent_team.ai_client import load_agent_team_ai_config
from backend.services.agent_team.git_workspace_service import AgentTeamGitWorkspaceService


class AgentTeamWorker:
    """Agent 专家团队任务处理器骨架。"""

    async def process_task(self, task_id: int) -> int:
        """处理 Agent 专家团队任务。

        当前阶段完成专用 AI 配置校验、仓库 clone/fetch 和 Agent 分支准备，为后续两角色执行器预留入口。
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

            task.status = AgentTeamTaskStatus.CLONING.value
            task.current_phase = "preparing_git_workspace"
            task.started_at = task.started_at or datetime.utcnow()
            task.ai_config_snapshot = json.dumps(
                config.safe_snapshot(), ensure_ascii=False
            )
            await session.commit()

            git_workspace_service = AgentTeamGitWorkspaceService()
            workspace_info = await git_workspace_service.prepare_workspace(
                task.repo_owner,
                task.repo_name,
                task.source_issue_number,
                task.source_id,
            )

            task.status = AgentTeamTaskStatus.WAITING_HUMAN.value
            task.current_phase = "git_workspace_ready"
            task.working_branch = workspace_info.branch_name
            task.base_branch = workspace_info.default_branch
            task.base_commit_sha = workspace_info.commit_sha
            task.workspace_path = str(workspace_info.workspace)
            task.error_message = (
                "Agent 专家团队 Git 工作区已准备完成，代码修改执行将在后续阶段启用。"
                f" workspace={workspace_info.workspace}; branch={workspace_info.branch_name}; "
                f"base={workspace_info.default_branch}; sha={workspace_info.commit_sha}"
            )
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
