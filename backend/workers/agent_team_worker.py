"""Agent 专家团队 Worker - 完整状态机

状态流转：
  queued → cloning → editing → self_reviewing → validating → pushing → pr_opened → completed
                                                                                         ↗
                                               iterating ─────────────────────────────────┘
  任何阶段失败 → failed
  任何阶段取消 → cancelled
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from loguru import logger

from backend.core.config import get_settings
from backend.models.agent_team_models import (
    AgentTeamIteration,
    AgentTeamPatchFile,
    AgentTeamTask,
    AgentTeamTaskStatus,
)
from backend.models.database import async_session
from backend.services.agent_team.ai_client import load_agent_team_ai_config
from backend.services.agent_team.git_workspace_service import AgentTeamGitWorkspaceService
from backend.services.agent_team.iteration_loop import IterationLoopService
from backend.services.agent_team.pr_service import AgentTeamPRService


class AgentTeamWorker:
    """Agent 专家团队任务处理器 - 完整状态机。"""

    async def process_task(self, task_id: int) -> int:
        """处理 Agent 专家团队任务，完整执行闭环。"""
        config = await load_agent_team_ai_config()
        config.validate()

        try:
            task = await self._load_task(task_id)

            # ── Phase 1: CLONING ──
            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.CLONING.value,
                current_phase="cloning",
                started_at=datetime.utcnow(),
                ai_config_snapshot=json.dumps(config.safe_snapshot(), ensure_ascii=False),
            )

            git_service = AgentTeamGitWorkspaceService()
            workspace_info = await git_service.prepare_workspace(
                task.repo_owner,
                task.repo_name,
                task.source_issue_number,
                task.source_id,
            )

            await self._update_task(
                task_id,
                working_branch=workspace_info.branch_name,
                base_branch=workspace_info.default_branch,
                base_commit_sha=workspace_info.commit_sha,
                workspace_path=str(workspace_info.workspace),
            )

            workspace = workspace_info.workspace
            repo_owner = task.repo_owner
            repo_name = task.repo_name

            # ── Phase 2: EDITING + SELF_REVIEWING (迭代循环) ──
            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.EDITING.value,
                current_phase="editing",
            )

            max_iterations = await self._resolve_max_iterations(task.max_iterations)
            if task.max_iterations != max_iterations:
                await self._update_task(task_id, max_iterations=max_iterations)
            sakura_memory = await self._load_sakura_memory(repo_owner, repo_name)

            loop_service = IterationLoopService(workspace)
            outcome = await loop_service.run(
                task_title=task.title,
                task_summary=task.summary or "",
                source_type=task.source_type,
                source_issue_number=task.source_issue_number,
                max_iterations=max_iterations,
                sakura_memory=sakura_memory,
            )

            logger.info(
                "Agent 迭代循环完成: success={}, iterations={}, tool_calls={}",
                outcome.success,
                outcome.iterations,
                outcome.total_tool_calls,
            )

            # ── 记录迭代 ──
            await self._save_iteration(
                task_id=task_id,
                iteration_number=outcome.iterations,
                fullstack_result=outcome.fullstack_result,
                review_result=outcome.review_result,
                modified_files=outcome.modified_files,
                workspace=workspace,
            )

            await self._update_task(
                task_id,
                iteration_count=outcome.iterations,
                current_phase="iteration_complete",
            )

            # ── Phase 3: VALIDATING ──
            if outcome.success and outcome.modified_files:
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.VALIDATING.value,
                    current_phase="validating",
                )

                # 检查修改文件数量限制
                max_files = int(
                    await self._get_config("agent_team_max_files_changed") or 30
                )

                if len(outcome.modified_files) > max_files:
                    await self._update_task(
                        task_id,
                        status=AgentTeamTaskStatus.FAILED.value,
                        current_phase="validation_failed",
                        error_message=(
                            f"修改文件数 {len(outcome.modified_files)} 超过限制 {max_files}"
                        ),
                    )
                    return task_id

                # ── Phase 4: PUSHING ──
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.PUSHING.value,
                    current_phase="pushing",
                )

                pr_service = AgentTeamPRService()
                commit_message = self._build_commit_message(task, outcome)
                await pr_service.commit_and_push(
                    workspace=str(workspace),
                    branch_name=workspace_info.branch_name,
                    commit_message=commit_message,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                )

                # ── Phase 5: CREATE PR ──
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.PUSHING.value,
                    current_phase="creating_pr",
                )

                is_draft = await self._resolve_bool_config(
                    "agent_team_draft_pr",
                    get_settings().agent_team_draft_pr,
                )
                pr_body = pr_service.build_pr_body(
                    task_title=task.title,
                    task_summary=task.summary or "",
                    fullstack_analysis=outcome.fullstack_result.summary if outcome.fullstack_result else "",
                    fullstack_plan="",
                    review_summary=outcome.review_result.summary if outcome.review_result else "",
                    iteration_count=outcome.iterations,
                    source_type=task.source_type,
                    source_issue_number=task.source_issue_number,
                )

                pr_result = await pr_service.create_pull_request(
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    title=f"🤖 {task.title}",
                    body=pr_body,
                    head_branch=workspace_info.branch_name,
                    base_branch=workspace_info.default_branch,
                    draft=is_draft,
                )

                # ── Phase 6: PR_OPENED → COMPLETED ──
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.PR_OPENED.value,
                    current_phase="pr_opened",
                    pr_number=pr_result.pr_number,
                    pr_url=pr_result.pr_url,
                )

                # 短暂等待后标记为 completed（外部 PR 审查将通过 webhook 异步处理）
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.COMPLETED.value,
                    current_phase="completed",
                    completed_at=datetime.utcnow(),
                    error_message=None,
                )

                logger.info(
                    "Agent 任务完成: task_id={}, pr=#{} ({})",
                    task_id,
                    pr_result.pr_number,
                    pr_result.pr_url,
                )
            else:
                # 迭代未能通过审查
                reason = outcome.reason
                if not outcome.modified_files:
                    reason = "全栈专家未能生成有效的代码修改"

                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.FAILED.value,
                    current_phase="iteration_failed",
                    error_message=reason,
                )
                logger.warning("Agent 任务失败: task_id={}, reason={}", task_id, reason)

        except Exception as e:
            logger.error("Agent 任务异常: task_id={}, error={}", task_id, e, exc_info=True)
            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.FAILED.value,
                current_phase="error",
                error_message=f"{type(e).__name__}: {e}",
            )

        return task_id

    # ── 辅助方法 ──────────────────────────────────────────

    async def _load_task(self, task_id: int) -> AgentTeamTask:
        async with async_session() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(AgentTeamTask).where(AgentTeamTask.id == task_id)
            )
            task = result.scalar_one_or_none()
            if not task:
                raise ValueError(f"AgentTeamTask 不存在: {task_id}")
            return task

    async def _update_task(self, task_id: int, **kwargs) -> None:
        async with async_session() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(AgentTeamTask).where(AgentTeamTask.id == task_id)
            )
            task = result.scalar_one_or_none()
            if not task:
                return
            for key, value in kwargs.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            task.updated_at = datetime.utcnow()
            await session.commit()

    async def _save_iteration(
        self,
        task_id: int,
        iteration_number: int,
        fullstack_result=None,
        review_result=None,
        modified_files: list[str] | None = None,
        workspace: str | Path | None = None,
    ) -> None:
        patch_stats = await self._collect_patch_file_stats(workspace) if workspace else {}
        async with async_session() as session:
            iteration = AgentTeamIteration(
                task_id=task_id,
                iteration_number=iteration_number,
                fullstack_plan=fullstack_result.summary if fullstack_result else None,
                fullstack_result=json.dumps(
                    {
                        "success": fullstack_result.success,
                        "modified_files": fullstack_result.modified_files,
                        "risk_level": fullstack_result.risk_level,
                        "tool_calls_count": fullstack_result.tool_calls_count,
                    },
                    ensure_ascii=False,
                )
                if fullstack_result
                else None,
                professional_review=review_result.summary if review_result else None,
                review_passed=1 if (review_result and review_result.passed) else 0,
                decision=review_result.verdict if review_result else None,
                diff_summary="\n".join(modified_files or []),
                completed_at=datetime.utcnow(),
            )
            session.add(iteration)
            await session.flush()

            # 保存 patch 文件记录。AI 可能返回 ./path 或 Windows 分隔符，需归一化匹配 Git 统计；
            # 如果 AI 没有返回文件列表但 Git 有变更，也回落到 Git 真实变更集合。
            tracked_files = _merge_modified_files(modified_files or [], patch_stats)
            for file_path in tracked_files:
                stats = patch_stats.get(file_path, {})
                patch = AgentTeamPatchFile(
                    iteration_id=iteration.id,
                    file_path=file_path,
                    change_type=stats.get("change_type", "modify"),
                    additions=stats.get("additions", 0),
                    deletions=stats.get("deletions", 0),
                )
                session.add(patch)

            await session.commit()

    async def _collect_patch_file_stats(self, workspace: str | Path | None) -> dict[str, dict]:
        """读取工作区未提交变更的逐文件行数统计。"""
        if workspace is None:
            return {}
        try:
            git_service = AgentTeamGitWorkspaceService()
            return await git_service.get_changed_file_stats(workspace)
        except Exception as exc:
            logger.debug("读取 Agent 变更文件统计失败，使用默认 0: {}", exc)
            return {}

    async def _load_sakura_memory(self, repo_owner: str, repo_name: str) -> str:
        try:
            from backend.services.sakura_memory_service import SakuraMemoryService

            service = SakuraMemoryService()
            context = await service.get_sakura_context(repo_owner, repo_name)
            if context:
                return str(context)
        except Exception as e:
            logger.debug("加载 Sakura 记忆失败 (非致命): {}", e)
        return ""

    async def _get_config(self, key: str) -> str | None:
        from backend.core.config import get_dynamic_config

        return await get_dynamic_config(key)

    async def _resolve_max_iterations(self, task_max_iterations: int | None) -> int:
        """解析任务最大迭代轮数。"""
        configured = await self._get_config("agent_team_max_iterations_per_task")
        fallback = get_settings().agent_team_max_iterations_per_task
        raw_value = configured if configured is not None else task_max_iterations or fallback
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = fallback
        return max(1, value)

    async def _resolve_bool_config(self, key: str, fallback: bool) -> bool:
        """读取布尔动态配置，保留显式 False。"""
        value = await self._get_config(key)
        if value is None:
            return fallback
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "启用", "是"}

    def _build_commit_message(self, task, outcome) -> str:
        parts = [f"feat(agent): {task.title}"]
        if outcome.fullstack_result:
            parts.append("")
            parts.append(f"Agent 全栈专家自动修改 ({outcome.iterations} 轮迭代)")
            parts.append(f"修改文件: {', '.join(outcome.modified_files)}")
            if outcome.review_result:
                parts.append(
                    f"审查分数: {outcome.review_result.score}/10 ({outcome.review_result.verdict})"
                )
        return "\n".join(parts)


def _normalize_modified_file_path(file_path: str) -> str:
    normalized = str(file_path).strip().replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _merge_modified_files(modified_files: list[str], patch_stats: dict[str, dict]) -> list[str]:
    """合并 AI 追踪文件和 Git 真实变更文件，优先保证 UI 有真实变更可展示。"""
    merged = {_normalize_modified_file_path(path) for path in modified_files if path}
    merged.update(patch_stats.keys())
    return sorted(merged)


_worker: AgentTeamWorker | None = None


def get_worker() -> AgentTeamWorker:
    global _worker
    if _worker is None:
        _worker = AgentTeamWorker()
    return _worker


async def submit_agent_team_task(task_id: int) -> int:
    return await get_worker().process_task(task_id)
