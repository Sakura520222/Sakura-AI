"""Check Run 服务：将 PR 审查生命周期映射到 GitHub Check Runs。

提供语义化的 report_* 方法，内部处理：
- PRStatus / ReviewDecision → Check Run status/conclusion 映射
- 中英双语 output 文本（跟随用户 output_language，复用 _is_english 模式）
- find_or_create 幂等定位（按 head_sha + name）
- 配置开关（enable_check_runs）
- 异常吞掉（绝不影响主审查流程）

所有方法均为 async 且返回 None；output 文本一律纯文本，不使用 emoji。
"""

import asyncio
from typing import Any, Iterable, Optional

from loguru import logger

from backend.core.config import get_settings
from backend.core.github_app import GitHubAppClient


class CheckRunService:
    """无状态的 Check Run 报告服务。"""

    CHECK_RUN_NAME = "Sakura AI Review"

    # 阶段描述（用于 summary / 当前阶段）：stage -> (zh, en)
    _STAGE_DESC: dict[str, tuple[str, str]] = {
        "indexing": ("正在索引代码变更", "Indexing code changes"),
        "summary": ("正在生成 PR 总结", "Generating PR summary"),
        "reviewing": ("AI 审查进行中", "AI review in progress"),
        "reporting": ("正在生成报告", "Generating report"),
    }
    # 阶段名词（用于"已完成"清单）：stage -> (zh, en)
    _STAGE_NAME: dict[str, tuple[str, str]] = {
        "indexing": ("代码索引", "Code indexing"),
        "summary": ("PR 总结", "PR summary"),
        "reviewing": ("AI 审查", "AI review"),
        "reporting": ("生成报告", "Report generation"),
    }

    # 决策中英文本 + conclusion 映射
    _DECISION_TEXT: dict[str, tuple[str, str]] = {
        "approve": ("通过", "Approve"),
        "comment": ("仅评论", "Comment"),
        "request_changes": ("建议修改", "Request changes"),
    }
    _DECISION_CONCLUSION: dict[str, str] = {
        "approve": "success",
        "comment": "neutral",
        "request_changes": "neutral",
    }

    def __init__(self) -> None:
        self._app = GitHubAppClient()

    # ------------------------------------------------------------------ utils

    @staticmethod
    def _is_english(output_language: Optional[str]) -> bool:
        """判断输出语言是否为英文（与 CommentService._is_english 一致）。"""
        lang = (
            output_language
            if output_language is not None
            else get_settings().output_language
        )
        return lang == "en"

    @staticmethod
    def _decision_key(decision: Any) -> str:
        """归一化 decision 为小写字符串键。"""
        if decision is None:
            return ""
        if hasattr(decision, "value"):
            return str(decision.value)
        return str(decision)

    async def _find_or_create(
        self,
        repo_owner: str,
        repo_name: str,
        head_sha: str,
        *,
        status: Optional[str] = None,
        conclusion: Optional[str] = None,
        output_title: Optional[str] = None,
        output_summary: Optional[str] = None,
        output_text: Optional[str] = None,
    ) -> Optional[int]:
        """按 head_sha + name 定位 Check Run：命中则 update，未命中则 create。

        返回 Check Run id 或 None（所有底层异常由 GitHubAppClient 吞掉）。
        head_sha 为空时直接返回 None（GitHub 创建 Check Run 必须绑定 commit）。
        """
        if not head_sha:
            logger.debug("CheckRunService: head_sha 为空，跳过 Check Run 操作")
            return None
        existing_id = await asyncio.to_thread(
            self._app.cleanup_stale_check_runs,
            repo_owner,
            repo_name,
            head_sha,
            self.CHECK_RUN_NAME,
        )
        if existing_id:
            await asyncio.to_thread(
                self._app.update_check_run,
                repo_owner,
                repo_name,
                existing_id,
                status=status,
                conclusion=conclusion,
                output_title=output_title,
                output_summary=output_summary,
                output_text=output_text,
            )
            return existing_id
        result = await asyncio.to_thread(
            self._app.create_check_run,
            repo_owner,
            repo_name,
            self.CHECK_RUN_NAME,
            head_sha,
            status=status or "queued",
            conclusion=conclusion,
            output_title=output_title,
            output_summary=output_summary,
            output_text=output_text,
        )
        return result.get("id") if result else None

    # ------------------------------------------------------------------ API

    async def report_queued(
        self,
        repo_owner: str,
        repo_name: str,
        head_sha: str,
        *,
        pr_number: Any,
        output_language: Optional[str] = None,
    ) -> None:
        """审查已排队（queued）。幂等：find 命中则重置为 queued。"""
        if not get_settings().enable_check_runs:
            return
        try:
            is_en = self._is_english(output_language)
            title = "Review Queued" if is_en else "Sakura AI 审查已排队"
            summary = (
                f"PR #{pr_number} queued"
                if is_en
                else f"PR #{pr_number} 已排队，等待处理"
            )
            await self._find_or_create(
                repo_owner,
                repo_name,
                head_sha,
                status="queued",
                output_title=title,
                output_summary=summary,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_queued 失败: {}", exc)

    async def report_progress(
        self,
        repo_owner: str,
        repo_name: str,
        head_sha: str,
        *,
        stage: str,
        completed_stages: Optional[Iterable[str]] = None,
        output_language: Optional[str] = None,
    ) -> None:
        """审查进行中（in_progress），更新当前阶段 output。"""
        if not get_settings().enable_check_runs:
            return
        try:
            is_en = self._is_english(output_language)
            desc_zh, desc_en = self._STAGE_DESC.get(stage, (stage, stage))
            stage_desc = desc_en if is_en else desc_zh
            title = "Reviewing" if is_en else "Sakura AI 正在审查"
            summary = stage_desc

            if is_en:
                lines = [f"Current stage: {stage_desc}"]
                if completed_stages:
                    done = [
                        self._STAGE_NAME.get(s, (s, s))[1] for s in completed_stages
                    ]
                    lines.append("Completed: " + ", ".join(done))
            else:
                lines = [f"当前阶段: {stage_desc}"]
                if completed_stages:
                    done = [
                        self._STAGE_NAME.get(s, (s, s))[0] for s in completed_stages
                    ]
                    lines.append("已完成: " + "、".join(done))
            text = "\n".join(lines)

            await self._find_or_create(
                repo_owner,
                repo_name,
                head_sha,
                status="in_progress",
                output_title=title,
                output_summary=summary,
                output_text=text,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_progress 失败: {}", exc)

    async def report_completed(
        self,
        repo_owner: str,
        repo_name: str,
        head_sha: str,
        *,
        decision: Any,
        overall_score: Any,
        comment_count: int,
        summary_excerpt: str = "",
        output_language: Optional[str] = None,
    ) -> None:
        """审查完成（completed），conclusion 由 decision 映射。"""
        if not get_settings().enable_check_runs:
            return
        try:
            is_en = self._is_english(output_language)
            key = self._decision_key(decision)
            decision_zh, decision_en = self._DECISION_TEXT.get(
                key, (key or "N/A", key or "N/A")
            )
            decision_label = decision_en if is_en else decision_zh
            conclusion = self._DECISION_CONCLUSION.get(key, "neutral")

            score_str = overall_score if overall_score is not None else "N/A"
            title = "Review Completed" if is_en else "Sakura AI 审查完成"
            if is_en:
                summary = (
                    f"Decision: {decision_label}, "
                    f"Score: {score_str}/10, Comments: {comment_count}"
                )
            else:
                summary = (
                    f"决策: {decision_label}, "
                    f"评分: {score_str}/10, 评论: {comment_count} 条"
                )

            await self._find_or_create(
                repo_owner,
                repo_name,
                head_sha,
                status="completed",
                conclusion=conclusion,
                output_title=title,
                output_summary=summary,
                output_text=summary_excerpt or None,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_completed 失败: {}", exc)

    async def report_failed(
        self,
        repo_owner: str,
        repo_name: str,
        head_sha: str,
        *,
        error_message: str,
        output_language: Optional[str] = None,
    ) -> None:
        """审查失败（completed + failure）。错误信息脱敏，不直接写入 output。"""
        if not get_settings().enable_check_runs:
            return
        try:
            is_en = self._is_english(output_language)
            title = "Review Failed" if is_en else "Sakura AI 审查失败"
            summary = "Review errored (sanitized)" if is_en else "审查过程出错（脱敏）"
            logger.debug("CheckRunService.report_failed 原始错误: {}", error_message)
            await self._find_or_create(
                repo_owner,
                repo_name,
                head_sha,
                status="completed",
                conclusion="failure",
                output_title=title,
                output_summary=summary,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_failed 失败: {}", exc)

    async def report_cancelled(
        self,
        repo_owner: str,
        repo_name: str,
        head_sha: str,
        *,
        output_language: Optional[str] = None,
    ) -> None:
        """审查取消（completed + cancelled）。"""
        if not get_settings().enable_check_runs:
            return
        try:
            is_en = self._is_english(output_language)
            title = "Review Cancelled" if is_en else "Sakura AI 审查已取消"
            summary = "PR closed or merged" if is_en else "PR 已关闭或合并"
            await self._find_or_create(
                repo_owner,
                repo_name,
                head_sha,
                status="completed",
                conclusion="cancelled",
                output_title=title,
                output_summary=summary,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_cancelled 失败: {}", exc)

    async def report_skipped(
        self,
        repo_owner: str,
        repo_name: str,
        head_sha: str,
        *,
        reason: str,
        output_language: Optional[str] = None,
    ) -> None:
        """审查跳过（completed + neutral）。reason 直接使用原值（中英一致）。"""
        if not get_settings().enable_check_runs:
            return
        try:
            is_en = self._is_english(output_language)
            title = "Review Skipped" if is_en else "Sakura AI 审查已跳过"
            summary = reason if reason else ("Skipped" if is_en else "已跳过")
            await self._find_or_create(
                repo_owner,
                repo_name,
                head_sha,
                status="completed",
                conclusion="neutral",
                output_title=title,
                output_summary=summary,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_skipped 失败: {}", exc)
