"""外部 CI 失败采集与查询服务 / External CI failure collection & query service.

由 check_run.completed / workflow_job.completed webhook 调用 record_* 方法采集
失败详情（主动拉 annotations、提取失败 step），写入 ci_failures 表；审查启动时
调 fetch_for_review 按 repo + head_sha 读取并注入审查上下文。

所有公开方法异常吞掉（对齐 CheckRunService 模式），绝不影响主审查流程。
限额遵循「禁止截断」硬规则：只做条数限额（+ 计数提示），绝不对单条文本做
字符级 [:N] 截断。
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select

from backend.core.config import get_strategy_config
from backend.core.github_app import GitHubAppClient
from backend.models import database as db_module
from backend.models.database import CIFailure, HeadShaPRMap


def _utcnow_naive() -> datetime:
    """返回 UTC naive datetime，兼容现有 TIMESTAMP 字段且避免 utcnow 弃用警告。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CIFailureService:
    """外部 CI 失败采集与查询服务。"""

    SELF_CHECK_NAME = "Sakura AI Review"
    # 值得记录的失败结论 / Failure conclusions worth recording
    FAILURE_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required"}

    def __init__(self) -> None:
        self._app = GitHubAppClient()

    # ------------------------------------------------------------------ config

    def _config(self) -> dict:
        """读取 ci_failure_injection 配置段。"""
        return (
            get_strategy_config()
            .get_context_enhancement_config()
            .get("ci_failure_injection", {})
        )

    def _is_enabled(self) -> bool:
        return bool(self._config().get("enabled", True))

    # ------------------------------------------------------------------ record

    async def record_check_run_failure(
        self,
        repo_owner: str,
        repo_name: str,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
        check_run_payload: dict,
    ) -> None:
        """处理 check_run.completed：过滤自身/非失败结论，拉 annotations，写表。"""
        if not self._is_enabled():
            return
        try:
            name = check_run_payload.get("name", "")
            # 过滤自身 Check Run / Filter self check run
            if name == self.SELF_CHECK_NAME:
                return
            conclusion = check_run_payload.get("conclusion", "")
            if conclusion not in self.FAILURE_CONCLUSIONS:
                return
            external_id = str(check_run_payload.get("id", ""))
            # 主动拉结构化 annotations（方案 B）/ Fetch structured annotations
            annotations = await asyncio.to_thread(
                self._app.get_check_run_annotations,
                repo_owner,
                repo_name,
                external_id,
            )
            output = check_run_payload.get("output") or {}
            await self._upsert_failure(
                repo_owner=repo_owner,
                repo_name=repo_name,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                source="check_run",
                name=name,
                conclusion=conclusion,
                external_id=external_id,
                output_title=output.get("title"),
                output_summary=output.get("summary"),
                output_text=output.get("text"),
                annotations=annotations,
                failed_steps=None,
                details_url=check_run_payload.get("details_url")
                or check_run_payload.get("html_url"),
            )
        except Exception as exc:
            logger.debug("CIFailureService.record_check_run_failure 失败: {}", exc)

    async def record_workflow_job_failure(
        self,
        repo_owner: str,
        repo_name: str,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
        workflow_job_payload: dict,
    ) -> None:
        """处理 workflow_job.completed：提取失败 step，写表（不拉原始日志）。"""
        if not self._is_enabled():
            return
        try:
            conclusion = workflow_job_payload.get("conclusion", "")
            if conclusion not in self.FAILURE_CONCLUSIONS:
                return
            name = workflow_job_payload.get("name", "")
            external_id = str(workflow_job_payload.get("id", ""))
            # payload 已含 steps（name + conclusion），无需额外 API
            failed_steps = [
                {"name": s.get("name", ""), "conclusion": s.get("conclusion", "")}
                for s in (workflow_job_payload.get("steps") or [])
                if s.get("conclusion") == "failure"
            ]
            await self._upsert_failure(
                repo_owner=repo_owner,
                repo_name=repo_name,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                source="workflow_job",
                name=name,
                conclusion=conclusion,
                external_id=external_id,
                output_title=None,
                output_summary=None,
                output_text=None,
                annotations=None,
                failed_steps=failed_steps,
                details_url=workflow_job_payload.get("html_url"),
            )
        except Exception as exc:
            logger.debug("CIFailureService.record_workflow_job_failure 失败: {}", exc)

    async def _upsert_failure(
        self,
        *,
        repo_owner: str,
        repo_name: str,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
        source: str,
        name: str,
        conclusion: str,
        external_id: str,
        output_title: Optional[str],
        output_summary: Optional[str],
        output_text: Optional[str],
        annotations: Optional[list],
        failed_steps: Optional[list],
        details_url: Optional[str],
    ) -> None:
        """按 (repo, head_sha, source, external_id) 去重 upsert。"""
        annotations_json = (
            json.dumps(annotations, ensure_ascii=False) if annotations else None
        )
        failed_steps_json = (
            json.dumps(failed_steps, ensure_ascii=False) if failed_steps else None
        )
        async with db_module.async_session() as session:
            existing = await session.execute(
                select(CIFailure).where(
                    CIFailure.repo_full_name == repo_full_name,
                    CIFailure.head_sha == head_sha,
                    CIFailure.source == source,
                    CIFailure.external_id == external_id,
                )
            )
            row = existing.scalar_one_or_none()
            if row is not None:
                row.repo_owner = repo_owner
                row.repo_name = repo_name
                row.pr_number = pr_number
                row.name = name
                row.conclusion = conclusion
                row.output_title = output_title
                row.output_summary = output_summary
                row.output_text = output_text
                row.failed_steps_json = failed_steps_json
                row.annotations_json = annotations_json
                row.details_url = details_url
            else:
                session.add(
                    CIFailure(
                        repo_owner=repo_owner,
                        repo_name=repo_name,
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        head_sha=head_sha,
                        source=source,
                        name=name,
                        conclusion=conclusion,
                        external_id=external_id,
                        output_title=output_title,
                        output_summary=output_summary,
                        output_text=output_text,
                        failed_steps_json=failed_steps_json,
                        annotations_json=annotations_json,
                        details_url=details_url,
                    )
                )
            await session.commit()

    # ------------------------------------------------------------------ fetch

    async def fetch_for_review(
        self, repo_full_name: str, head_sha: str
    ) -> list[dict]:
        """审查时调用：按 repo + head_sha 查询失败记录，返回结构化 dict 列表。

        条数限额（max_records / max_annotations_per_record）+ 计数提示；
        不对文本做字符级截断。
        """
        if not self._is_enabled():
            return []
        try:
            cfg = self._config()
            max_records = int(cfg.get("max_records", 10))
            max_annotations = int(cfg.get("max_annotations_per_record", 8))
            async with db_module.async_session() as session:
                result = await session.execute(
                    select(CIFailure)
                    .where(
                        CIFailure.repo_full_name == repo_full_name,
                        CIFailure.head_sha == head_sha,
                    )
                    .order_by(CIFailure.created_at)
                )
                rows = list(result.scalars().all())
            if not rows:
                return []
            omitted_records = max(0, len(rows) - max_records)
            shown = rows[:max_records]
            return [
                self._row_to_dict(row, max_annotations, omitted_records)
                for row in shown
            ]
        except Exception as exc:
            logger.debug("CIFailureService.fetch_for_review 失败: {}", exc)
            return []

    def _row_to_dict(
        self, row: CIFailure, max_annotations: int, omitted_records: int
    ) -> dict:
        """单条记录转 dict。annotations 做条数限额（非文本截断）。"""
        annotations = self._load_json(row.annotations_json) or []
        omitted_annotations = max(0, len(annotations) - max_annotations)
        # 条数限额（列表切片，非文本截断）；保留的 annotation 文本字段全量
        shown_annotations = annotations[:max_annotations]
        return {
            "source": row.source,
            "name": row.name,
            "conclusion": row.conclusion,
            "output_title": row.output_title,
            "output_summary": row.output_summary,
            "output_text": row.output_text,
            "failed_steps": self._load_json(row.failed_steps_json) or [],
            "annotations": shown_annotations,
            "details_url": row.details_url,
            "omitted_annotations": omitted_annotations,
            "omitted_records": omitted_records,
        }

    @staticmethod
    def _load_json(raw: Optional[str]) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    # ------------------------------------------------------------------ head_sha map

    async def upsert_head_sha_pr_map(
        self,
        repo_owner: str,
        repo_name: str,
        repo_full_name: str,
        head_sha: str,
        pr_number: int,
    ) -> None:
        """维护 head_sha → pr_number 映射（pull_request 事件调用）。"""
        try:
            async with db_module.async_session() as session:
                existing = await session.execute(
                    select(HeadShaPRMap).where(
                        HeadShaPRMap.repo_full_name == repo_full_name,
                        HeadShaPRMap.head_sha == head_sha,
                    )
                )
                row = existing.scalar_one_or_none()
                if row is not None:
                    row.pr_number = pr_number
                    row.repo_owner = repo_owner
                    row.repo_name = repo_name
                else:
                    session.add(
                        HeadShaPRMap(
                            repo_full_name=repo_full_name,
                            head_sha=head_sha,
                            pr_number=pr_number,
                            repo_owner=repo_owner,
                            repo_name=repo_name,
                        )
                    )
                await session.commit()
        except Exception as exc:
            logger.debug("CIFailureService.upsert_head_sha_pr_map 失败: {}", exc)

    async def lookup_pr_number(
        self, repo_full_name: str, head_sha: str
    ) -> Optional[int]:
        """查映射表解 pr_number（三层降级第二层）。"""
        try:
            async with db_module.async_session() as session:
                result = await session.execute(
                    select(HeadShaPRMap).where(
                        HeadShaPRMap.repo_full_name == repo_full_name,
                        HeadShaPRMap.head_sha == head_sha,
                    )
                )
                row = result.scalar_one_or_none()
                return row.pr_number if row else None
        except Exception as exc:
            logger.debug("CIFailureService.lookup_pr_number 失败: {}", exc)
            return None

    # ------------------------------------------------------------------ cleanup

    async def cleanup_for_pr(
        self, repo_full_name: str, pr_number: int
    ) -> int:
        """PR closed/merged 时清理该 PR 的全部失败记录。返回清理条数。"""
        try:
            async with db_module.async_session() as session:
                result = await session.execute(
                    select(CIFailure).where(
                        CIFailure.repo_full_name == repo_full_name,
                        CIFailure.pr_number == pr_number,
                    )
                )
                rows = list(result.scalars().all())
                for row in rows:
                    await session.delete(row)
                await session.commit()
                return len(rows)
        except Exception as exc:
            logger.debug("CIFailureService.cleanup_for_pr 失败: {}", exc)
            return 0

    async def cleanup_expired(self) -> int:
        """按 TTL 清理过期记录。返回清理条数。"""
        retention_days = int(self._config().get("retention_days", 7))
        cutoff = _utcnow_naive() - timedelta(days=retention_days)
        try:
            async with db_module.async_session() as session:
                result = await session.execute(select(CIFailure))
                rows = list(result.scalars().all())
                expired = [
                    row
                    for row in rows
                    if row.created_at and row.created_at < cutoff
                ]
                for row in expired:
                    await session.delete(row)
                await session.commit()
                return len(expired)
        except Exception as exc:
            logger.debug("CIFailureService.cleanup_expired 失败: {}", exc)
            return 0
