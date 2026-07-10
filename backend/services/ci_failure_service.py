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
from backend.services.check_run_service import CheckRunService


def _utcnow_naive() -> datetime:
    """返回 UTC naive datetime，兼容现有 TIMESTAMP 字段且避免 utcnow 弃用警告。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CIFailureService:
    """外部 CI 失败采集与查询服务。"""

    # 自身拥有的全部 Check 名（主 Review + 副 Analysis/Findings），避免副 Check
    # failure 被误记为外部 CI 失败。从 CheckRunService 取单一真相，保持同步。
    SELF_CHECK_NAMES = frozenset(CheckRunService.OWNED_CHECK_NAMES)
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
            # 过滤自身 Check Run（含副 Analysis/Findings）/ Filter self check runs
            if name in self.SELF_CHECK_NAMES:
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
            # record 时即按配置限额存储 annotations（条数切片，非文本截断），
            # 避免 CI 发大量 annotations 导致 TEXT 列超限；原始总数记入 annotations_total
            max_annotations = int(self._config().get("max_annotations_per_record", 8))
            annotations_total = len(annotations) if annotations else 0
            stored_annotations = (annotations or [])[:max_annotations]
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
                annotations=stored_annotations,
                annotations_total=annotations_total,
                failed_steps=None,
                details_url=check_run_payload.get("details_url")
                or check_run_payload.get("html_url"),
            )
            logger.info(
                "[ci_failure] record check_run: name={!r}, pr={}, "
                "annotations={}/{} (stored/total), output_summary={!r}".format(
                    name,
                    pr_number,
                    len(stored_annotations),
                    annotations_total,
                    (output.get("summary") or "")[:80],
                )
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
                annotations_total=0,
                failed_steps=failed_steps,
                details_url=workflow_job_payload.get("html_url"),
            )
            logger.info(
                "[ci_failure] record workflow_job: name={!r}, pr={}, "
                "failed_steps={}".format(name, pr_number, len(failed_steps))
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
        annotations_total: int,
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
                row.annotations_total = annotations_total
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
                        annotations_total=annotations_total,
                        details_url=details_url,
                    )
                )
            await session.commit()

    # ------------------------------------------------------------------ fetch

    async def fetch_for_review(self, repo_full_name: str, head_sha: str) -> list[dict]:
        """审查时调用：按 repo + head_sha 查询失败记录，返回结构化 dict 列表。

        条数限额（max_records / max_annotations_per_record）+ 计数提示；
        不对文本做字符级截断。
        """
        if not self._is_enabled():
            return []
        try:
            cfg = self._config()
            max_records = int(cfg.get("max_records", 10))
            retention_days = int(cfg.get("retention_days", 7))
            cutoff = _utcnow_naive() - timedelta(days=retention_days)
            async with db_module.async_session() as session:
                result = await session.execute(
                    select(CIFailure)
                    .where(
                        CIFailure.repo_full_name == repo_full_name,
                        CIFailure.head_sha == head_sha,
                        CIFailure.created_at >= cutoff,
                    )
                    .order_by(CIFailure.created_at.desc())
                )
                rows = list(result.scalars().all())
            if not rows:
                return []
            # 按 (source, name) 去重：同名 CI 失败多次触发（不同 external_id）时
            # 只保留最新一条（rows 已按 created_at desc 排序，首次出现即最新）
            seen_keys: set[tuple[str, str]] = set()
            deduped = []
            for row in rows:
                key = (row.source, row.name)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                deduped.append(row)
            omitted_records = max(0, len(deduped) - max_records)
            shown = deduped[:max_records]
            logger.info(
                "[ci_failure] fetch_for_review: repo={}, head_sha={!r}, "
                "total={}, deduped={}, shown={}, names={}".format(
                    repo_full_name,
                    head_sha[:8],
                    len(rows),
                    len(deduped),
                    len(shown),
                    [r.name for r in shown],
                )
            )
            return [self._row_to_dict(row, omitted_records) for row in shown]
        except Exception as exc:
            logger.debug("CIFailureService.fetch_for_review 失败: {}", exc)
            return []

    def _row_to_dict(self, row: CIFailure, omitted_records: int) -> dict:
        """单条记录转 dict。

        annotations 已在 record 时按 max_annotations_per_record 限额存储，
        这里用 annotations_total（原始总数）计算省略数，不做任何文本截断。
        """
        annotations = self._load_json(row.annotations_json) or []
        total = int(getattr(row, "annotations_total", 0) or len(annotations))
        omitted_annotations = max(0, total - len(annotations))
        return {
            "source": row.source,
            "name": row.name,
            "conclusion": row.conclusion,
            "output_title": row.output_title,
            "output_summary": row.output_summary,
            "output_text": row.output_text,
            "failed_steps": self._load_json(row.failed_steps_json) or [],
            "annotations": annotations,
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

    async def delete_failures(
        self,
        repo_full_name: str,
        head_sha: str,
        source: str,
        name: str,
    ) -> int:
        """删除同 (repo, head_sha, source, name) 的失败记录。

        用于 CI 重跑成功后清除旧的失败记录，避免向 AI 注入过时失败。
        返回删除条数。
        """
        try:
            async with db_module.async_session() as session:
                result = await session.execute(
                    select(CIFailure).where(
                        CIFailure.repo_full_name == repo_full_name,
                        CIFailure.head_sha == head_sha,
                        CIFailure.source == source,
                        CIFailure.name == name,
                    )
                )
                rows = list(result.scalars().all())
                for row in rows:
                    await session.delete(row)
                await session.commit()
                return len(rows)
        except Exception as exc:
            logger.debug("CIFailureService.delete_failures 失败: {}", exc)
            return 0

    async def cleanup_for_pr(self, repo_full_name: str, pr_number: int) -> int:
        """PR closed/merged 时清理该 PR 的全部失败记录与 head_sha 映射。返回清理失败记录条数。"""
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
                # 同步清理该 PR 的 head_sha → pr_number 映射（避免表无限增长）
                map_result = await session.execute(
                    select(HeadShaPRMap).where(
                        HeadShaPRMap.repo_full_name == repo_full_name,
                        HeadShaPRMap.pr_number == pr_number,
                    )
                )
                for row in list(map_result.scalars().all()):
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
                result = await session.execute(
                    select(CIFailure).where(CIFailure.created_at < cutoff)
                )
                rows = list(result.scalars().all())
                for row in rows:
                    await session.delete(row)
                await session.commit()
                return len(rows)
        except Exception as exc:
            logger.debug("CIFailureService.cleanup_expired 失败: {}", exc)
            return 0
