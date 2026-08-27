"""Agent Worker - Agent 状态机

状态流转：
  queued → cloning → editing → validating → pushing → pr_opened → completed
                                                                                         ↗
                                               iterating ─────────────────────────────────┘
  任何阶段失败 → failed
  任何阶段取消 → cancelled
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from backend.core.config import get_dynamic_config_fresh, get_settings
from backend.models import database as db_module
from backend.models.agent_team_models import (
    AgentTeamFeedback,
    AgentTeamFeedbackSource,
    AgentTeamIteration,
    AgentTeamPatchFile,
    AgentTeamTask,
    AgentTeamTaskStatus,
    AgentTeamUserPrompt,
)
from backend.models.database import utc_now as _utc_now
from backend.services.agent_team.conversation_checkpoint import (
    ConversationCheckpointService,
)
from backend.services.agent_team.execution import ExecutionRunner
from backend.services.agent_team.git_workspace_service import (
    AgentTeamGitWorkspaceService,
)
from backend.services.agent_team.iteration_loop import IterationLoopService
from backend.services.agent_team.pr_service import AgentTeamPRService
from backend.services.agent_team.sandbox_client import create_ready_execution_runner
from backend.services.agent_team.submission_context import (
    build_agent_task_summary,
    load_agent_task_reference_context,
    load_sakura_memory,
    load_skills_context,
)
from backend.services.ai_reviewer.token_tracker import TokenTracker


def _format_failure_reason(reason: str, modified_files: list[str]) -> str:
    # A guidance admission failure is deliberately retryable even when the
    # Agent has not modified a file. Preserve the marker so the worker can
    # keep pending guidance queued for the next attempt.
    if "guidance_admission_failed" in reason:
        return reason
    if modified_files:
        return reason
    return "Agent 未能生成有效的代码修改"


class AgentTeamWorker:
    """Agent 专家团队任务处理器 - 完整状态机。"""

    async def _create_agent_execution_runner(
        self,
        workspace: Path,
        workspace_service,
    ) -> ExecutionRunner:
        """Admit one workspace-scoped runner before any Agent code runs."""

        # Backend selection is an admission/security decision.  Do not use
        # the process-local Settings snapshot here: another Web worker may
        # have just switched the deployment from local to sandbox.  The fresh
        # helper reads AppConfig directly and raises on an active DB failure,
        # so an old worker cannot silently continue with a stale local value.
        try:
            backend_value = await get_dynamic_config_fresh(
                "agent_team_execution_backend"
            )
        except Exception as exc:
            raise RuntimeError(
                "Agent execution backend configuration is unavailable; refusing runner admission"
            ) from exc
        if not isinstance(backend_value, str) or not backend_value.strip():
            raise RuntimeError(
                "Agent execution backend configuration is missing; refusing runner admission"
            )

        settings = get_settings()
        return await create_ready_execution_runner(
            str(workspace),
            workspace_service,
            backend=backend_value,
            deploy_mode=getattr(settings, "sakura_deploy_mode", "unknown"),
            expected_runtime=getattr(settings, "agent_team_sandbox_runtime", None),
            expected_instance_id=getattr(
                settings,
                "agent_team_sandbox_expected_instance_id",
                None,
            ),
            expected_workspace_root=getattr(
                settings,
                "agent_team_sandbox_expected_workspace_root",
                None,
            ),
            expected_digest=getattr(
                settings,
                "agent_team_sandbox_runner_image_digest",
                None,
            ),
        )

    async def _admit_workspace_runner(
        self,
        git_service: AgentTeamGitWorkspaceService,
        workspace: Path,
    ) -> ExecutionRunner:
        """Create runner then install untrusted dependencies through it.

        Every production-shaped workspace service must expose the installer;
        omitting it is an infrastructure contract error rather than a reason
        to skip runner admission or dependency isolation.
        """

        install_dependencies = git_service.install_workspace_dependencies
        runner = await self._create_agent_execution_runner(
            workspace,
            git_service.workspace_service,
        )
        await install_dependencies(workspace, runner)
        return runner

    async def process_task(self, task_id: int, resume: bool = False) -> int:
        """处理 Agent 专家团队任务，完整执行闭环。"""
        # 注册取消信号
        cancel_event = _cancel_events.get(task_id)
        if cancel_event is None:
            cancel_event = asyncio.Event()
            _cancel_events[task_id] = cancel_event

        try:
            task = await self._load_task(task_id)
            (
                skills_summary,
                skills_context,
                _skills_snapshot,
            ) = await load_skills_context()

            # ── Phase 1: CLONING ──
            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.CLONING.value,
                current_phase="cloning",
                started_at=_utc_now(),
            )

            git_service = AgentTeamGitWorkspaceService()
            if resume:
                if not task.workspace_path or not task.branch_name:
                    raise RuntimeError("任务缺少可续跑的工作区或分支信息")
                workspace_info = await git_service.resume_workspace(
                    task.repo_owner,
                    task.repo_name,
                    task.workspace_path,
                    task.branch_name,
                    task.base_branch,
                    task.base_commit_sha,
                )
            else:
                workspace_info = await git_service.prepare_workspace(
                    task.repo_owner,
                    task.repo_name,
                    task.source_issue_number,
                    task.source_id,
                    task.base_branch,
                    task.id,
                    task.source_type,
                )

            # 取消检查点
            if cancel_event.is_set():
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.CANCELLED.value,
                    current_phase="cancelled",
                    error_message="任务在 CLONING 阶段被取消",
                )
                return task_id

            await self._update_task(
                task_id,
                branch_name=workspace_info.branch_name,
                base_branch=workspace_info.default_branch,
                base_commit_sha=workspace_info.commit_sha,
                workspace_path=str(workspace_info.workspace),
            )

            workspace = workspace_info.workspace
            repo_owner = task.repo_owner
            repo_name = task.repo_name

            execution_runner = await self._admit_workspace_runner(
                git_service,
                workspace,
            )
            if cancel_event.is_set():
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.CANCELLED.value,
                    current_phase="cancelled",
                    error_message="任务在 sandbox admission 阶段被取消",
                )
                return task_id

            # ── Phase 2: EDITING + SELF_REVIEWING (迭代循环) ──
            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.EDITING.value,
                current_phase="editing",
            )

            sakura_info = await load_sakura_memory(repo_owner, repo_name)
            task_context = build_agent_task_summary(task.summary or "")
            reference_context = await self._load_task_reference_context(task)

            checkpoint = ConversationCheckpointService(task_id)
            resume_cursor = await checkpoint.get_resume_cursor() if resume else None
            loop_service = IterationLoopService(
                workspace,
                workspace_service=git_service.workspace_service,
                task_id=task_id,
                checkpoint=checkpoint,
                resume_cursor=resume_cursor,
                resume_index=task.resume_count or 0,
                execution_runner=execution_runner,
            )
            outcome = await loop_service.run(
                task_title=task.title,
                task_summary=task_context,
                source_type=task.source_type,
                source_issue_number=task.source_issue_number,
                sakura_memory=sakura_info["text"],
                skills_summary=skills_summary,
                skills_context=skills_context,
                reference_context=reference_context,
                github_repo=sakura_info["github_repo"],
                sakura_ref=sakura_info["sakura_ref"],
                cancel_check=cancel_event.is_set,
                cancel_event=cancel_event,
            )

            # 提前计算 estimated_cost（供成功/失败两分支共用）
            s = get_settings()
            cost_tracker = TokenTracker()
            cost_tracker.add_tokens(outcome.prompt_tokens, outcome.completion_tokens)
            estimated_cost = cost_tracker.calculate_cost(
                s.review_price_per_1k_prompt,
                s.review_price_per_1k_completion,
            )

            logger.info(
                "Agent 迭代循环完成: success={}, iterations={}, tool_calls={}, "
                "tokens={}+{}, cost={}",
                outcome.success,
                outcome.iterations,
                outcome.total_tool_calls,
                outcome.prompt_tokens,
                outcome.completion_tokens,
                estimated_cost,
            )

            # 迭代循环被取消
            if not outcome.success and cancel_event.is_set():
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.CANCELLED.value,
                    current_phase="cancelled",
                    error_message="任务在 EDITING 阶段被取消",
                )
                return task_id

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
                prompt_tokens=outcome.prompt_tokens,
                completion_tokens=outcome.completion_tokens,
                current_phase="iteration_complete",
            )

            # ── Phase 3: VALIDATING ──
            if outcome.success and outcome.modified_files:
                # 取消检查点
                if cancel_event.is_set():
                    await self._update_task(
                        task_id,
                        status=AgentTeamTaskStatus.CANCELLED.value,
                        current_phase="cancelled",
                        error_message="任务在 VALIDATING 阶段被取消",
                    )
                    return task_id

                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.VALIDATING.value,
                    current_phase="validating",
                )

                # ── Phase 4: PUSHING ──
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.PUSHING.value,
                    current_phase="pushing",
                )

                pr_service = AgentTeamPRService()
                fallback_msg = self._build_commit_message(task, outcome)
                commit_message = await pr_service.generate_commit_message(
                    task_title=task.title,
                    task_summary=task.summary or "",
                    modified_files=outcome.modified_files or [],
                    fullstack_summary=outcome.fullstack_result.summary
                    if outcome.fullstack_result
                    else "",
                    fallback_message=fallback_msg,
                )
                if commit_message == fallback_msg:
                    logger.info(
                        "Agent commit message: 使用 fallback 模板 (AI 生成未返回)"
                    )
                else:
                    logger.info("Agent commit message: 使用 AI 生成结果")
                await pr_service.commit_and_push(
                    workspace=str(workspace),
                    branch_name=workspace_info.branch_name,
                    commit_message=commit_message,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                )

                # 等待 GitHub 完成分支索引
                await asyncio.sleep(get_settings().agent_team_branch_index_delay)

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

                # 获取 git diff summary 作为 AI 生成 PR body 的额外上下文
                git_service = AgentTeamGitWorkspaceService()
                diff_summary = ""
                try:
                    diff_summary = await git_service.get_diff_summary(str(workspace))
                except Exception as exc:
                    logger.warning("获取 diff summary 失败: {}", exc)

                # 预计算 fallback body（AI 生成失败时使用）
                fallback_body = pr_service.build_pr_body(
                    task_title=task.title,
                    task_summary=task.summary or "",
                    fullstack_analysis=outcome.fullstack_result.summary
                    if outcome.fullstack_result
                    else "",
                    fullstack_plan="",
                    review_summary=outcome.review_result.summary
                    if outcome.review_result
                    else "",
                    iteration_count=outcome.iterations,
                    source_type=task.source_type,
                    source_issue_number=task.source_issue_number,
                )

                # AI 生成 PR body
                pr_body = await pr_service.generate_pr_body(
                    task_title=task.title,
                    task_summary=task.summary or "",
                    fullstack_analysis=outcome.fullstack_result.summary
                    if outcome.fullstack_result
                    else "",
                    review_summary=outcome.review_result.summary
                    if outcome.review_result
                    else "",
                    review_verdict=outcome.review_result.verdict
                    if outcome.review_result
                    else "",
                    review_score=outcome.review_result.score
                    if outcome.review_result
                    else 0,
                    review_findings=[
                        {"severity": f.severity, "file": f.file, "message": f.message}
                        for f in (outcome.review_result.findings or [])
                    ]
                    if outcome.review_result
                    else [],
                    modified_files=outcome.modified_files,
                    iteration_count=outcome.iterations,
                    source_type=task.source_type,
                    source_issue_number=task.source_issue_number,
                    diff_summary=diff_summary,
                    fallback_body=fallback_body,
                )

                pr_title = await pr_service.generate_pr_title(
                    task_title=task.title,
                    task_summary=task.summary or "",
                    modified_files=outcome.modified_files,
                    review_verdict=outcome.review_result.verdict
                    if outcome.review_result
                    else "",
                    issue_number=task.source_issue_number,
                )

                pr_result = await pr_service.create_pull_request(
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    title=pr_title,
                    body=pr_body,
                    head_branch=workspace_info.branch_name,
                    base_branch=workspace_info.default_branch,
                    draft=is_draft,
                )

                # ── Phase 6: PR_OPENED / COMPLETED ──
                closed_loop_enabled = await self._resolve_bool_config(
                    "agent_team_pr_closed_loop_enabled",
                    get_settings().agent_team_pr_closed_loop_enabled,
                )
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.PR_OPENED.value,
                    current_phase="pr_opened",
                    pr_number=pr_result.pr_number,
                    pr_url=pr_result.pr_url,
                    pr_head_sha=getattr(pr_result, "head_sha", "") or None,
                    estimated_cost=estimated_cost,
                    error_message=None,
                    failed_phase=None,
                    failed_role=None,
                    rate_limit_reset_at=None,
                )

                if not closed_loop_enabled:
                    await self._update_task(
                        task_id,
                        status=AgentTeamTaskStatus.COMPLETED.value,
                        current_phase="completed",
                        completed_at=_utc_now(),
                        estimated_cost=estimated_cost,
                        error_message=None,
                        failed_phase=None,
                        failed_role=None,
                        rate_limit_reset_at=None,
                    )

                logger.info(
                    "Agent PR 已创建: task_id={}, pr=#{} ({}) | "
                    "closed_loop={}, iterations={}, tool_calls={}, tokens={}+{}, cost={}",
                    task_id,
                    pr_result.pr_number,
                    pr_result.pr_url,
                    closed_loop_enabled,
                    outcome.iterations,
                    outcome.total_tool_calls,
                    outcome.prompt_tokens,
                    outcome.completion_tokens,
                    estimated_cost,
                )
            else:
                # 迭代未能通过审查
                reason = _format_failure_reason(outcome.reason, outcome.modified_files)

                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.FAILED.value,
                    current_phase="iteration_failed",
                    error_message=reason,
                    failed_phase="iteration_failed",
                    estimated_cost=estimated_cost,
                )
                logger.warning(
                    "Agent 任务失败: task_id={}, reason={} | "
                    "iterations={}, tool_calls={}, tokens={}+{}, cost={}",
                    task_id,
                    reason,
                    outcome.iterations,
                    outcome.total_tool_calls,
                    outcome.prompt_tokens,
                    outcome.completion_tokens,
                    estimated_cost,
                )

        except Exception as e:
            logger.error(
                "Agent 任务异常: task_id={}, error={}", task_id, e, exc_info=True
            )
            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.FAILED.value,
                current_phase="error",
                error_message=f"{type(e).__name__}: {e}",
                failed_phase="error",
                rate_limit_reset_at=_parse_rate_limit_reset_at(str(e)),
            )
        finally:
            _cancel_events.pop(task_id, None)
            await self._expire_pending_prompts_if_terminal(task_id)

        return task_id

    async def process_external_review_iteration(
        self, task_id: int, review_id: int
    ) -> int:
        """根据 Sakura PR Review 反馈继续同一分支的 Agent 闭环迭代。"""
        cancel_event = _cancel_events.get(task_id)
        if cancel_event is None:
            cancel_event = asyncio.Event()
            _cancel_events[task_id] = cancel_event

        terminal = False
        try:
            task = await self._load_task(task_id)
            if task.status != AgentTeamTaskStatus.ITERATING.value:
                logger.info(
                    "跳过 Agent PR 闭环迭代: task_id={}, status={}",
                    task_id,
                    task.status,
                )
                return task_id

            if not task.workspace_path or not task.branch_name or not task.pr_number:
                terminal = True
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.WAITING_HUMAN.value,
                    current_phase="waiting_human",
                    error_message="缺少可继续 PR 闭环的 workspace/branch/pr 信息",
                )
                return task_id

            review_feedback = await self._load_sakura_pr_review_feedback(
                task_id,
                review_id,
            )

            git_service = AgentTeamGitWorkspaceService()
            workspace_info = await git_service.resume_workspace(
                task.repo_owner,
                task.repo_name,
                task.workspace_path,
                task.branch_name,
                task.base_branch,
                task.base_commit_sha,
            )

            if cancel_event.is_set():
                terminal = True
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.CANCELLED.value,
                    current_phase="cancelled",
                    error_message="任务在 PR 闭环恢复阶段被取消",
                )
                return task_id

            execution_runner = await self._admit_workspace_runner(
                git_service,
                workspace_info.workspace,
            )
            if cancel_event.is_set():
                terminal = True
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.CANCELLED.value,
                    current_phase="cancelled",
                    error_message="任务在 sandbox admission 阶段被取消",
                )
                return task_id

            (
                skills_summary,
                skills_context,
                _skills_snapshot,
            ) = await load_skills_context()
            sakura_info = await load_sakura_memory(task.repo_owner, task.repo_name)
            task_context = build_agent_task_summary(task.summary or "")
            reference_context = await self._load_task_reference_context(task)

            checkpoint = ConversationCheckpointService(task_id)
            loop_service = IterationLoopService(
                workspace_info.workspace,
                workspace_service=git_service.workspace_service,
                task_id=task_id,
                checkpoint=checkpoint,
                resume_index=task.resume_count or 0,
                execution_runner=execution_runner,
            )
            outcome = await loop_service.run(
                task_title=task.title,
                task_summary=task_context,
                source_type=task.source_type,
                source_issue_number=task.source_issue_number,
                sakura_memory=sakura_info["text"],
                skills_summary=skills_summary,
                skills_context=skills_context,
                reference_context=reference_context,
                github_repo=sakura_info["github_repo"],
                sakura_ref=sakura_info["sakura_ref"],
                initial_feedback=review_feedback,
                cancel_check=cancel_event.is_set,
                cancel_event=cancel_event,
                iteration_offset=task.iteration_count or 0,
            )

            (
                new_iteration_count,
                prompt_tokens,
                completion_tokens,
                estimated_cost,
            ) = self._accumulate_iteration_cost(task, outcome)

            if not outcome.success and cancel_event.is_set():
                terminal = True
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.CANCELLED.value,
                    current_phase="cancelled",
                    iteration_count=new_iteration_count,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost=estimated_cost,
                    error_message="任务在 PR 闭环迭代阶段被取消",
                )
                return task_id

            await self._save_iteration(
                task_id=task_id,
                iteration_number=new_iteration_count,
                fullstack_result=outcome.fullstack_result,
                review_result=outcome.review_result,
                modified_files=outcome.modified_files,
                workspace=workspace_info.workspace,
            )
            await self._update_task(
                task_id,
                iteration_count=new_iteration_count,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                current_phase="iteration_complete",
            )

            if not outcome.modified_files:
                terminal = True
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.WAITING_HUMAN.value,
                    current_phase="waiting_human",
                    estimated_cost=estimated_cost,
                    error_message="Agent 未根据 Sakura PR Review 产生新修改",
                )
                return task_id

            if not outcome.success:
                terminal = True
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.WAITING_HUMAN.value,
                    current_phase="waiting_human",
                    estimated_cost=estimated_cost,
                    error_message=_format_failure_reason(
                        outcome.reason,
                        outcome.modified_files,
                    ),
                )
                return task_id

            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.VALIDATING.value,
                current_phase="validating",
            )

            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.PUSHING.value,
                current_phase="pushing",
            )
            pr_service = AgentTeamPRService()
            fallback_msg = self._build_commit_message(task, outcome)
            commit_message = await pr_service.generate_commit_message(
                task_title=task.title,
                task_summary=task.summary or "",
                modified_files=outcome.modified_files or [],
                fullstack_summary=outcome.fullstack_result.summary
                if outcome.fullstack_result
                else "",
                review_feedback=review_feedback,
                fallback_message=fallback_msg,
            )
            if commit_message == fallback_msg:
                logger.info("Agent PR 闭环 commit message: 使用 fallback 模板")
            else:
                logger.info("Agent PR 闭环 commit message: 使用 AI 生成结果")
            new_sha = await pr_service.commit_and_push(
                workspace=str(workspace_info.workspace),
                branch_name=task.branch_name,
                commit_message=commit_message,
                repo_owner=task.repo_owner,
                repo_name=task.repo_name,
            )

            await self._update_pr_body(pr_service, task, outcome, new_iteration_count)

            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.EXTERNAL_REVIEWING.value,
                current_phase="external_reviewing",
                pr_head_sha=new_sha,
                iteration_count=new_iteration_count,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost=estimated_cost,
                error_message=None,
                failed_phase=None,
                failed_role=None,
                rate_limit_reset_at=None,
            )
            return task_id
        except Exception as e:
            terminal = True
            logger.error(
                "Agent PR 闭环迭代异常: task_id={}, review_id={}, error={}",
                task_id,
                review_id,
                e,
                exc_info=True,
            )
            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.FAILED.value,
                current_phase="error",
                error_message=f"{type(e).__name__}: {e}",
                failed_phase="error",
                rate_limit_reset_at=_parse_rate_limit_reset_at(str(e)),
            )
            return task_id
        finally:
            _cancel_events.pop(task_id, None)
            if terminal:
                await self._expire_pending_prompts_if_terminal(task_id)

    async def process_human_followup_iteration(self, task_id: int) -> int:
        """管理员后续要求：复用同一 worktree/branch/PR 继续迭代。

        由 submit_user_prompt 在可续跑终态时调度。
        """
        cancel_event = _cancel_events.get(task_id)
        if cancel_event is None:
            cancel_event = asyncio.Event()
            _cancel_events[task_id] = cancel_event

        terminal = False
        try:
            task = await self._load_task(task_id)
            if task.status != AgentTeamTaskStatus.ITERATING.value:
                logger.info(
                    "跳过 Agent follow-up: task_id={}, status={}",
                    task_id,
                    task.status,
                )
                return task_id

            if not task.workspace_path or not task.branch_name or not task.pr_number:
                terminal = True
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.WAITING_HUMAN.value,
                    current_phase="waiting_human",
                    error_message="缺少可继续 follow-up 的 workspace/branch/pr 信息",
                )
                return task_id

            # 恢复 workspace
            git_service = AgentTeamGitWorkspaceService()
            workspace_info = await git_service.resume_workspace(
                task.repo_owner,
                task.repo_name,
                task.workspace_path,
                task.branch_name,
                task.base_branch,
                task.base_commit_sha,
            )

            if cancel_event.is_set():
                terminal = True
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.CANCELLED.value,
                    current_phase="cancelled",
                    error_message="任务在 follow-up 恢复阶段被取消",
                )
                return task_id

            execution_runner = await self._admit_workspace_runner(
                git_service,
                workspace_info.workspace,
            )
            if cancel_event.is_set():
                terminal = True
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.CANCELLED.value,
                    current_phase="cancelled",
                    error_message="任务在 sandbox admission 阶段被取消",
                )
                return task_id

            (
                skills_summary,
                skills_context,
                _skills_snapshot,
            ) = await load_skills_context()
            sakura_info = await load_sakura_memory(task.repo_owner, task.repo_name)
            task_context = build_agent_task_summary(task.summary or "")
            reference_context = await self._load_task_reference_context(task)

            checkpoint = ConversationCheckpointService(task_id)
            loop_service = IterationLoopService(
                workspace_info.workspace,
                workspace_service=git_service.workspace_service,
                task_id=task_id,
                checkpoint=checkpoint,
                resume_index=task.resume_count or 0,
                execution_runner=execution_runner,
            )

            # follow-up 的 initial_feedback 留空，由 _consume_pending_prompts 消费管理员要求
            outcome = await loop_service.run(
                task_title=task.title,
                task_summary=task_context,
                source_type=task.source_type,
                source_issue_number=task.source_issue_number,
                sakura_memory=sakura_info["text"],
                skills_summary=skills_summary,
                skills_context=skills_context,
                reference_context=reference_context,
                github_repo=sakura_info["github_repo"],
                sakura_ref=sakura_info["sakura_ref"],
                cancel_check=cancel_event.is_set,
                cancel_event=cancel_event,
                iteration_offset=task.iteration_count or 0,
            )

            (
                new_iteration_count,
                prompt_tokens,
                completion_tokens,
                estimated_cost,
            ) = self._accumulate_iteration_cost(task, outcome)

            if not outcome.success and cancel_event.is_set():
                terminal = True
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.CANCELLED.value,
                    current_phase="cancelled",
                    iteration_count=new_iteration_count,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost=estimated_cost,
                    error_message="任务在 follow-up 迭代阶段被取消",
                )
                return task_id

            await self._save_iteration(
                task_id=task_id,
                iteration_number=new_iteration_count,
                fullstack_result=outcome.fullstack_result,
                review_result=outcome.review_result,
                modified_files=outcome.modified_files,
                workspace=workspace_info.workspace,
            )
            await self._update_task(
                task_id,
                iteration_count=new_iteration_count,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                current_phase="followup_complete",
            )

            if not outcome.modified_files:
                terminal = True
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.WAITING_HUMAN.value,
                    current_phase="waiting_human",
                    estimated_cost=estimated_cost,
                    error_message="Agent 未根据管理员要求产生新修改",
                )
                return task_id

            if not outcome.success:
                terminal = True
                await self._update_task(
                    task_id,
                    status=AgentTeamTaskStatus.WAITING_HUMAN.value,
                    current_phase="waiting_human",
                    estimated_cost=estimated_cost,
                    error_message=_format_failure_reason(
                        outcome.reason,
                        outcome.modified_files,
                    ),
                )
                return task_id

            # push 到同一 Agent PR 分支
            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.VALIDATING.value,
                current_phase="validating",
            )

            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.PUSHING.value,
                current_phase="pushing",
            )
            pr_service = AgentTeamPRService()
            fallback_msg = self._build_commit_message(task, outcome)
            commit_message = await pr_service.generate_commit_message(
                task_title=task.title,
                task_summary=task.summary or "",
                modified_files=outcome.modified_files or [],
                fullstack_summary=outcome.fullstack_result.summary
                if outcome.fullstack_result
                else "",
                fallback_message=fallback_msg,
            )
            new_sha = await pr_service.commit_and_push(
                workspace=str(workspace_info.workspace),
                branch_name=task.branch_name,
                commit_message=commit_message,
                repo_owner=task.repo_owner,
                repo_name=task.repo_name,
            )

            await self._update_pr_body(pr_service, task, outcome, new_iteration_count)

            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.EXTERNAL_REVIEWING.value,
                current_phase="external_reviewing",
                pr_head_sha=new_sha,
                iteration_count=new_iteration_count,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost=estimated_cost,
                error_message=None,
                failed_phase=None,
                failed_role=None,
                rate_limit_reset_at=None,
            )
            return task_id
        except Exception as e:
            terminal = True
            logger.error(
                "Agent follow-up 迭代异常: task_id={}, error={}",
                task_id,
                e,
                exc_info=True,
            )
            await self._update_task(
                task_id,
                status=AgentTeamTaskStatus.FAILED.value,
                current_phase="error",
                error_message=f"{type(e).__name__}: {e}",
                failed_phase="error",
                rate_limit_reset_at=_parse_rate_limit_reset_at(str(e)),
            )
            return task_id
        finally:
            _cancel_events.pop(task_id, None)
            if terminal:
                await self._expire_pending_prompts_if_terminal(task_id)

    # ── 辅助方法 ──────────────────────────────────────────

    async def _load_task_reference_context(self, task: AgentTeamTask) -> str:
        """Rebuild source material for ``<reference_context>`` at run time."""
        try:
            async with db_module.async_session() as session:
                return await load_agent_task_reference_context(
                    session,
                    source_type=task.source_type,
                    source_id=task.source_id,
                    repo_owner=task.repo_owner,
                    repo_name=task.repo_name,
                    repo_full_name=task.repo_full_name,
                    issue_number=task.source_issue_number,
                )
        except Exception as exc:
            logger.debug(
                "加载 Agent 任务引用上下文失败，继续使用用户目标: task_id={}, error={}",
                task.id,
                exc,
            )
            return ""

    def _accumulate_iteration_cost(
        self, task: AgentTeamTask, outcome
    ) -> tuple[int, int, int, int]:
        """计算累计 token 和成本，返回 (new_iteration_count, prompt_tokens, completion_tokens, estimated_cost)。"""
        new_iteration_count = (task.iteration_count or 0) + outcome.iterations
        prompt_tokens = (task.prompt_tokens or 0) + outcome.prompt_tokens
        completion_tokens = (task.completion_tokens or 0) + outcome.completion_tokens
        s = get_settings()
        cost_tracker = TokenTracker()
        cost_tracker.add_tokens(prompt_tokens, completion_tokens)
        estimated_cost = cost_tracker.calculate_cost(
            s.review_price_per_1k_prompt,
            s.review_price_per_1k_completion,
        )
        return new_iteration_count, prompt_tokens, completion_tokens, estimated_cost

    async def _update_pr_body(
        self,
        pr_service: AgentTeamPRService,
        task: AgentTeamTask,
        outcome,
        new_iteration_count: int,
    ) -> None:
        """更新已有 PR body。

        尽力而为语义：失败仅 logger.warning，不向调用方传播异常。
        """
        try:
            fallback_body = pr_service.build_pr_body(
                task_title=task.title,
                task_summary=task.summary or "",
                fullstack_analysis=outcome.fullstack_result.summary
                if outcome.fullstack_result
                else "",
                fullstack_plan="",
                review_summary=outcome.review_result.summary
                if outcome.review_result
                else "",
                iteration_count=new_iteration_count,
                source_type=task.source_type,
                source_issue_number=task.source_issue_number,
            )
            body = await pr_service.generate_pr_body(
                task_title=task.title,
                task_summary=task.summary or "",
                fullstack_analysis=outcome.fullstack_result.summary
                if outcome.fullstack_result
                else "",
                review_summary=outcome.review_result.summary
                if outcome.review_result
                else "",
                review_verdict=outcome.review_result.verdict
                if outcome.review_result
                else "",
                review_score=outcome.review_result.score
                if outcome.review_result
                else 0,
                review_findings=[],
                modified_files=outcome.modified_files or [],
                iteration_count=new_iteration_count,
                source_type=task.source_type,
                source_issue_number=task.source_issue_number,
                fallback_body=fallback_body,
            )
            await pr_service.update_pull_request_body(
                repo_owner=task.repo_owner,
                repo_name=task.repo_name,
                pr_number=task.pr_number,
                body=body,
            )
        except Exception as exc:
            logger.warning("更新 Agent PR body 失败: {}", exc)

    async def _expire_pending_prompts_if_terminal(self, task_id: int) -> None:
        try:
            task = await self._load_task(task_id)
        except Exception as exc:
            logger.debug("检查 Agent pending prompts 过期状态失败: {}", exc)
            return
        if task.status in {
            AgentTeamTaskStatus.COMPLETED.value,
            AgentTeamTaskStatus.FAILED.value,
            AgentTeamTaskStatus.CANCELLED.value,
            AgentTeamTaskStatus.ABANDONED.value,
        } and not (
            task.status == AgentTeamTaskStatus.FAILED.value
            and "guidance_admission_failed" in str(task.error_message or "")
        ):
            await self._expire_pending_prompts(task_id)

    async def _load_sakura_pr_review_feedback(
        self, task_id: int, review_id: int
    ) -> str:
        """读取 Sakura PR Review 反馈内容。"""
        from sqlalchemy import select

        async with db_module.async_session() as session:
            result = await session.execute(
                select(AgentTeamFeedback.content).where(
                    AgentTeamFeedback.task_id == task_id,
                    AgentTeamFeedback.source
                    == AgentTeamFeedbackSource.SAKURA_PR_REVIEW.value,
                    AgentTeamFeedback.external_id == f"pr_review:{review_id}",
                )
            )
            return result.scalar_one_or_none() or ""

    async def _expire_pending_prompts(self, task_id: int) -> None:
        """任务结束时将未消费的 pending prompts 标记为 expired。"""
        from sqlalchemy import update

        async with db_module.async_session() as session:
            await session.execute(
                update(AgentTeamUserPrompt)
                .where(
                    AgentTeamUserPrompt.task_id == task_id,
                    AgentTeamUserPrompt.status == "pending",
                )
                .values(status="expired")
            )
            await session.commit()

    async def _load_task(self, task_id: int) -> AgentTeamTask:
        async with db_module.async_session() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(AgentTeamTask).where(AgentTeamTask.id == task_id)
            )
            task = result.scalar_one_or_none()
            if not task:
                raise ValueError(f"AgentTeamTask 不存在: {task_id}")
            return task

    async def _update_task(self, task_id: int, **kwargs) -> None:
        async with db_module.async_session() as session:
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
            task.updated_at = _utc_now()
            await session.commit()

        # SSE: 通知前端任务状态/阶段变更
        if "status" in kwargs or "current_phase" in kwargs:
            try:
                from backend.webui.sse import publish_event

                await publish_event(
                    "agent:task_updated",
                    {
                        "task_id": task_id,
                        "status": kwargs.get("status"),
                        "current_phase": kwargs.get("current_phase"),
                    },
                )
            except Exception as exc:
                logger.debug("SSE 发布任务更新事件失败: {}", exc)

    async def _save_iteration(
        self,
        task_id: int,
        iteration_number: int,
        fullstack_result=None,
        review_result=None,
        modified_files: list[str] | None = None,
        workspace: str | Path | None = None,
    ) -> None:
        patch_stats = (
            await self._collect_patch_file_stats(workspace) if workspace else {}
        )
        async with db_module.async_session() as session:
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
                completed_at=_utc_now(),
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

    async def _collect_patch_file_stats(
        self, workspace: str | Path | None
    ) -> dict[str, dict]:
        """读取工作区未提交变更的逐文件行数统计。"""
        if workspace is None:
            return {}
        try:
            git_service = AgentTeamGitWorkspaceService()
            return await git_service.get_changed_file_stats(workspace)
        except Exception as exc:
            logger.debug("读取 Agent 变更文件统计失败，使用默认 0: {}", exc)
            return {}

    async def _get_config(self, key: str) -> str | None:
        from backend.core.config import get_dynamic_config

        return await get_dynamic_config(key)

    async def _resolve_bool_config(self, key: str, fallback: bool) -> bool:
        """读取布尔动态配置，保留显式 False。"""
        from backend.services.agent_team.ai_client import resolve_agent_team_bool_config

        return await resolve_agent_team_bool_config(key, fallback)

    def _build_commit_message(self, task, outcome) -> str:
        parts = [f"feat(agent): {task.title}"]
        if outcome.fullstack_result:
            parts.append("")
            parts.append(f"Agent 自动修改（第 {outcome.iterations} 次执行）")
            parts.append(f"修改文件: {', '.join(outcome.modified_files)}")
            if outcome.review_result:
                parts.append(
                    f"审查分数: {outcome.review_result.score}/10 ({outcome.review_result.verdict})"
                )
        return "\n".join(parts)


def _parse_rate_limit_reset_at(error_text: str) -> datetime | None:
    match = re.search(
        r"限额将在\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*重置",
        error_text,
    )
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
    except ValueError:
        return None


def _normalize_modified_file_path(file_path: str) -> str:
    normalized = str(file_path).strip().replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _merge_modified_files(
    modified_files: list[str], patch_stats: dict[str, dict]
) -> list[str]:
    """合并 AI 追踪文件和 Git 真实变更文件，优先保证 UI 有真实变更可展示。"""
    merged = {_normalize_modified_file_path(path) for path in modified_files if path}
    merged.update(patch_stats.keys())
    return sorted(merged)


_worker: AgentTeamWorker | None = None

# 任务取消信号：task_id → asyncio.Event，设置时表示任务应尽快停止
_cancel_events: dict[int, asyncio.Event] = {}


def get_worker() -> AgentTeamWorker:
    global _worker
    if _worker is None:
        _worker = AgentTeamWorker()
    return _worker


async def submit_agent_team_task(task_id: int) -> int:
    return await get_worker().process_task(task_id)


async def resume_agent_team_task(task_id: int) -> int:
    return await get_worker().process_task(task_id, resume=True)


async def submit_agent_team_pr_review_iteration(task_id: int, review_id: int) -> int:
    return await get_worker().process_external_review_iteration(task_id, review_id)


async def submit_agent_team_human_followup(task_id: int) -> int:
    """管理员在 Live View 提交后续要求后，复用同一 worktree/branch/PR 继续迭代。"""
    return await get_worker().process_human_followup_iteration(task_id)


def request_task_cancel(task_id: int) -> None:
    """请求取消指定任务。Worker 在关键阶段会检查此信号。"""
    event = _cancel_events.get(task_id)
    if event is None:
        _cancel_events[task_id] = asyncio.Event()
        _cancel_events[task_id].set()
    else:
        event.set()


def is_task_cancel_requested(task_id: int) -> bool:
    """检查任务是否已被请求取消。"""
    event = _cancel_events.get(task_id)
    return event is not None and event.is_set()
