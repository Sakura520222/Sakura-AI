"""Issue 分析异步任务处理器"""

import asyncio
import json
import uuid
from typing import Dict, Any, Optional
from loguru import logger

from backend.core.config import get_settings, get_dynamic_config
from backend.core.github_app import GitHubAppClient
from backend.models.database import (
    async_session,
    IssueAnalysis,
    IssueAnalysisStatus,
)
from sqlalchemy import select, and_, func
from backend.services.issue_analyzer import IssueAnalyzer
from backend.services.issue_service import issue_service

# Issue 分析并发控制信号量
_issue_semaphore: asyncio.Semaphore | None = None


async def _get_issue_semaphore() -> asyncio.Semaphore:
    """获取 Issue 分析并发信号量（懒初始化，支持动态更新）"""
    global _issue_semaphore
    if _issue_semaphore is None:
        max_concurrent = await _load_max_concurrent()
        _issue_semaphore = asyncio.Semaphore(max_concurrent)
        logger.info(f"Issue 分析并发信号量初始化: 最大 {max_concurrent} 个并发任务")
    return _issue_semaphore


def reset_issue_semaphore():
    """重置 Issue 分析信号量（配置更新时调用）"""
    global _issue_semaphore
    _issue_semaphore = None
    logger.info("Issue 分析并发信号量已重置，下次任务将重新初始化")


async def _load_max_concurrent() -> int:
    """从动态配置读取最大并发 Issue 分析数"""
    try:
        val = await get_dynamic_config("max_concurrent_issues")
        return int(val) if val is not None else get_settings().max_concurrent_issues
    except Exception as e:
        logger.warning(f"读取 max_concurrent_issues 配置失败，使用默认值: {e}")
        return get_settings().max_concurrent_issues


class IssueWorker:
    """Issue 分析任务处理器"""

    def __init__(self):
        self.analyzer = IssueAnalyzer()
        self.github_app = GitHubAppClient()
        self._background_tasks: set[asyncio.Task] = set()

    @staticmethod
    async def _log_activity(
        analysis_id: int,
        event_type: str,
        content: dict[str, Any] | None = None,
    ) -> None:
        """记录 Issue 分析活动事件（持久化 + SSE 推送）。"""
        try:
            from backend.services.activity_event_service import ActivityEventService

            await ActivityEventService.log_event(
                "issue", analysis_id, event_type, content
            )
        except Exception as exc:
            logger.debug("Issue 活动事件记录失败: {}", exc)

    async def process_issue_analysis(self, issue_info: Dict[str, Any]) -> str:
        """处理 Issue 分析任务

        Args:
            issue_info: Issue 信息（来自 webhook）

        Returns:
            任务ID
        """
        task_id = issue_info.get("task_id", str(uuid.uuid4()))
        settings = get_settings()

        repo_owner = issue_info.get("repo_owner", "")
        repo_name = issue_info.get("repo_name", "")
        issue_number = issue_info.get("issue_number", 0)
        repo_full_name = issue_info.get("repo_full_name", f"{repo_owner}/{repo_name}")

        logger.info(f"[{task_id}] 开始处理 Issue 分析: {repo_full_name}#{issue_number}")

        # 获取并发信号量，限制同时运行的 Issue 分析任务数
        semaphore = await _get_issue_semaphore()
        async with semaphore:
            async with async_session() as db:
                try:
                    # 1. 计算下一个分析版本号
                    max_version = await db.scalar(
                        select(func.max(IssueAnalysis.analysis_version)).where(
                            and_(
                                IssueAnalysis.repo_name == repo_name,
                                IssueAnalysis.issue_number == issue_number,
                            )
                        )
                    )
                    next_version = (max_version or 0) + 1

                    # 归档超出上限的旧版本
                    try:
                        max_versions = int(
                            await get_dynamic_config("issue_max_analysis_versions") or 10
                        )
                    except (ValueError, TypeError):
                        max_versions = 10
                    if next_version > max_versions:
                        await self._archive_old_versions(
                            db, repo_name, issue_number, next_version - max_versions
                        )

                    # 创建分析记录（PENDING）
                    record = IssueAnalysis(
                        issue_number=issue_number,
                        repo_name=repo_name,
                        repo_owner=repo_owner,
                        author=issue_info.get("author", ""),
                        title=issue_info.get("title", ""),
                        body=issue_info.get("body", ""),
                        status=IssueAnalysisStatus.PENDING.value,
                        analysis_version=next_version,
                        issue_state=issue_info.get("state", "open"),
                    )
                    db.add(record)
                    await db.commit()
                    await db.refresh(record)

                    # 2. 更新状态为 ANALYZING
                    record.status = IssueAnalysisStatus.ANALYZING.value
                    await db.commit()

                    await self._log_activity(record.id, "status", {
                        "status": "analyzing",
                        "message": f"开始分析 Issue #{issue_number}",
                        "repo_name": repo_name,
                    })
                    await self._log_activity(record.id, "thinking", {
                        "message": "AI 正在分析 Issue ...",
                    })

                    # 发布 SSE 事件通知前端
                    try:
                        from backend.webui.sse import publish_event

                        await publish_event(
                            "issue:status_changed",
                            {
                                "issue_number": issue_info.get("issue_number"),
                                "repo_name": issue_info.get("repo_name"),
                                "status": "analyzing",
                            },
                        )
                    except Exception as e:
                        logger.warning(f"发布 SSE 事件失败（不影响主流程）: {e}")

                    # 3. 获取 repo 对象
                    client = self.github_app.get_repo_client(repo_owner, repo_name)
                    repo = None
                    if client:
                        repo = client.get_repo(repo_full_name)

                    # 4. 调用 AI 分析
                    analysis_result = await self.analyzer.analyze_issue(
                        issue_info=issue_info,
                        repo_owner=repo_owner,
                        repo_name=repo_name,
                        repo=repo,
                    )

                    # 4.5 记录 AI 分析完成事件
                    await self._log_activity(record.id, "ai_response", {
                        "message": "AI 分析完成",
                        "category": analysis_result.get("category", ""),
                        "priority": analysis_result.get("priority", ""),
                        "content_preview": str(analysis_result.get("summary", ""))[:500],
                    })

                    # 5. 保存分析结果（更新已有的 PENDING 记录）
                    analysis_record = await issue_service.save_analysis_result(
                        analysis_result, issue_info, db
                    )

                    if not analysis_record:
                        logger.error(f"[{task_id}] 未找到待更新的分析记录")
                        return task_id

                    # 5.1 关联扫描记录（如果此 Issue 来自仓库扫描）
                    try:
                        from backend.models.scan_models import RepoScan

                        scan = await db.scalar(
                            select(RepoScan).where(
                                RepoScan.report_issue_number == issue_number
                            )
                        )
                        if scan and not scan.issue_analysis_id:
                            scan.issue_analysis_id = analysis_record.id
                            await db.commit()
                            logger.info(
                                f"[{task_id}] 已关联扫描记录到分析: scan_id={scan.id}"
                            )
                    except Exception as e:
                        logger.warning(f"[{task_id}] 关联扫描记录失败: {e}")

                    # 5.5 使用 AI 摘要更新 Issue 向量
                    try:
                        from backend.services.issue_embedding_service import (
                            IssueEmbeddingService,
                        )

                        summary = analysis_result.get("summary", "")
                        if summary:
                            emb_service = IssueEmbeddingService()
                            analysis_metadata = {
                                "category": analysis_result.get("category", ""),
                                "priority": analysis_result.get("priority", ""),
                                "feasibility": analysis_result.get("feasibility", ""),
                            }
                            await emb_service.upsert_issue(
                                repo_owner,
                                repo_name,
                                issue_number,
                                title=issue_info.get("title", ""),
                                body=summary,
                                state=issue_info.get("state", "open"),
                                analysis_metadata=analysis_metadata,
                            )
                            logger.info(f"[{task_id}] 已使用 AI 摘要更新 Issue 向量")
                    except Exception as e:
                        logger.warning(f"[{task_id}] 使用 AI 摘要更新向量失败: {e}")

                    # 6. 重复检测（优先使用 AI 摘要）
                    if await get_dynamic_config("issue_detect_duplicates"):
                        try:
                            summary = analysis_result.get("summary", "")
                            duplicates = await issue_service.detect_duplicates(
                                repo_owner,
                                repo_name,
                                issue_info.get("title", ""),
                                summary or issue_info.get("body", ""),
                                current_issue_number=issue_number,
                            )
                            if duplicates:
                                analysis_record.duplicate_of = duplicates[0].get(
                                    "issue_number"
                                )
                        except Exception as e:
                            logger.warning(f"[{task_id}] 重复检测失败: {e}")

                    # 7. 查找关联 PR
                    try:
                        related_prs = await issue_service.find_related_prs(
                            repo_owner, repo_name, issue_number
                        )
                        if related_prs:
                            analysis_record.related_prs = json.dumps(
                                related_prs, ensure_ascii=False
                            )
                    except Exception as e:
                        logger.warning(f"[{task_id}] 查找关联 PR 失败: {e}")

                    await db.commit()

                    # 发布 SSE 事件通知前端（完成）
                    try:
                        from backend.webui.sse import publish_event

                        await publish_event(
                            "issue:status_changed",
                            {
                                "issue_number": issue_info.get("issue_number"),
                                "repo_name": issue_info.get("repo_name"),
                                "status": "completed",
                            },
                        )
                    except Exception as e:
                        logger.warning(f"发布 SSE 事件失败（不影响主流程）: {e}")

                    await self._log_activity(record.id, "result", {
                        "status": "completed",
                        "message": f"Issue #{issue_number} 分析完成",
                        "category": analysis_result.get("category", ""),
                        "priority": analysis_result.get("priority", ""),
                    })

                    # 8. 自动评论
                    if await get_dynamic_config("issue_auto_comment"):
                        try:
                            success = await issue_service.post_analysis_comment(
                                repo_owner,
                                repo_name,
                                issue_number,
                                analysis_record,
                                db,
                            )
                            if success:
                                logger.info(f"[{task_id}] 已发布分析评论")
                        except Exception as e:
                            logger.warning(f"[{task_id}] 发布评论失败: {e}")

                    # 10. 应用建议标签
                    if await get_dynamic_config("issue_auto_create_labels"):
                        try:
                            labels_data = json.loads(
                                analysis_record.suggested_labels or "[]"
                            )
                            if labels_data:
                                result = await issue_service.apply_suggested_labels(
                                    repo_owner,
                                    repo_name,
                                    issue_number,
                                    labels_data,
                                    db,
                                )
                                if result.get("applied"):
                                    logger.info(
                                        f"[{task_id}] 已应用标签: "
                                        f"{[label['name'] for label in result['applied']]}"
                                    )
                                if result.get("created"):
                                    logger.info(
                                        f"[{task_id}] 已创建标签: {result['created']}"
                                    )
                                if result.get("failed"):
                                    logger.warning(
                                        f"[{task_id}] 标签应用失败: {result['failed']}"
                                    )
                        except Exception as e:
                            logger.warning(f"[{task_id}] 应用标签失败: {e}")

                    # 10.5 应用建议指派人
                    if await get_dynamic_config("issue_auto_assign"):
                        try:
                            assignees_data = json.loads(
                                analysis_record.suggested_assignees or "[]"
                            )
                            if assignees_data:
                                assign_result = (
                                    await issue_service.apply_suggested_assignees(
                                        repo_owner,
                                        repo_name,
                                        issue_number,
                                        assignees_data,
                                    )
                                )
                                if assign_result.get("applied"):
                                    logger.info(
                                        f"[{task_id}] 已指派: "
                                        f"{[a['username'] for a in assign_result['applied']]}"
                                    )
                                if assign_result.get("failed"):
                                    logger.warning(
                                        f"[{task_id}] 指派失败: "
                                        f"{[a['username'] for a in assign_result['failed']]}"
                                    )
                        except Exception as e:
                            logger.warning(f"[{task_id}] 应用指派人失败: {e}")

                    # 10.7 自动改写标题（优先从 DB 读取配置）
                    issue_auto_rewrite_title = await get_dynamic_config(
                        "issue_auto_rewrite_title"
                    )

                    if issue_auto_rewrite_title:
                        try:
                            suggested_title = analysis_record.suggested_title
                            original_title = issue_info.get("title", "")
                            if suggested_title and suggested_title != original_title:
                                if len(suggested_title) > 256:
                                    logger.info(
                                        f"[{task_id}] 建议标题超过 256 字符，已截断"
                                    )
                                    suggested_title = suggested_title[:256]
                                success = await asyncio.to_thread(
                                    self.github_app.update_issue_title,
                                    repo_owner,
                                    repo_name,
                                    issue_number,
                                    suggested_title,
                                )
                                if success:
                                    logger.info(
                                        f"[{task_id}] 已改写标题: {suggested_title}"
                                    )
                        except Exception as e:
                            logger.warning(f"[{task_id}] 改写标题失败: {e}")

                    # 11. Critical 告警
                    category = analysis_result.get("category", "")
                    priority = analysis_result.get("priority", "")

                    # 收集通知目标：作者 + 订阅者
                    notification_chat_ids = []
                    try:
                        from backend.services.telegram_service import TelegramService

                        ts = TelegramService(db)
                        notification_chat_ids = await ts.get_notification_targets(
                            repo_full_name, issue_info.get("author", "")
                        )
                    except Exception as e:
                        logger.warning(f"[{task_id}] 获取通知目标失败: {e}")

                    if priority == "critical":
                        try:
                            from backend.telegram.notifications import (
                                get_notification_sender,
                            )

                            sender = get_notification_sender()
                            if sender and notification_chat_ids:
                                await sender.send_critical_issue_alert(
                                    repo_name=repo_full_name,
                                    issue_number=issue_number,
                                    title=issue_info.get("title", ""),
                                    category=category,
                                    summary=analysis_result.get("summary", ""),
                                    feasibility=analysis_result.get("feasibility", ""),
                                    issue_url=issue_info.get("html_url", ""),
                                    suggested_labels=analysis_result.get(
                                        "suggested_labels", []
                                    ),
                                    chat_ids=notification_chat_ids,
                                )
                                logger.info(f"[{task_id}] 已发送 Critical Issue 告警")
                        except Exception as e:
                            logger.warning(f"[{task_id}] 发送告警失败: {e}")

                    # 12. 发送完成通知
                    try:
                        from backend.telegram.notifications import (
                            get_notification_sender,
                        )

                        sender = get_notification_sender()
                        if sender and notification_chat_ids:
                            await sender.send_issue_analysis_complete(
                                repo_name=repo_full_name,
                                issue_number=issue_number,
                                category=category,
                                priority=priority,
                                issue_url=issue_info.get("html_url", ""),
                                summary=analysis_result.get("summary"),
                                chat_ids=notification_chat_ids,
                            )
                    except Exception as e:
                        logger.warning(f"[{task_id}] 发送完成通知失败: {e}")

                    # 13. 异步触发 .sakura/ Issue 反思 / Trigger .sakura/ issue reflection async
                    try:
                        if (
                            settings.sakura_memory_enabled
                            and settings.sakura_issue_reflection_enabled
                        ):
                            from backend.services.sakura_memory_service import (
                                get_sakura_memory_service,
                            )

                            sakura_memory_service = get_sakura_memory_service()
                            task = asyncio.create_task(
                                sakura_memory_service.reflect_issue(
                                    repo=repo,
                                    repo_full_name=repo_full_name,
                                    issue_number=issue_number,
                                    issue_info=issue_info,
                                    analysis_result=analysis_result,
                                    analysis_record=analysis_record,
                                )
                            )
                            self._background_tasks.add(task)
                            task.add_done_callback(self._background_tasks.discard)
                            logger.info(f"[{task_id}] 已触发 .sakura/ Issue 反思任务")
                    except Exception as e:
                        logger.warning(
                            f"[{task_id}] 触发 .sakura/ Issue 反思失败（不影响分析）: {e}"
                        )

                    logger.info(
                        "[{}] Issue 分析完成: {}#{} | "
                        "轮数={}, tokens={}+{}, cost={}",
                        task_id,
                        repo_full_name,
                        issue_number,
                        analysis_result.get("tool_rounds", "?"),
                        analysis_result.get("prompt_tokens", 0),
                        analysis_result.get("completion_tokens", 0),
                        analysis_result.get("estimated_cost", 0),
                    )

                except Exception as e:
                    logger.error(f"[{task_id}] Issue 分析失败: {e}", exc_info=True)

                    # 更新状态为 FAILED（仅更新本次任务的 PENDING/ANALYZING 记录）
                    try:
                        result = await db.execute(
                            select(IssueAnalysis)
                            .where(
                                and_(
                                    IssueAnalysis.issue_number == issue_number,
                                    IssueAnalysis.repo_name == repo_name,
                                    IssueAnalysis.status.in_(
                                        [
                                            IssueAnalysisStatus.PENDING.value,
                                            IssueAnalysisStatus.ANALYZING.value,
                                        ]
                                    ),
                                )
                            )
                            .order_by(IssueAnalysis.created_at.desc())
                            .limit(1)
                        )
                        record = result.scalar_one_or_none()
                        if record:
                            record.status = IssueAnalysisStatus.FAILED.value
                            record.error_message = str(e)[:1000]
                            await db.commit()
                            await self._log_activity(record.id, "error", {
                                "message": f"Issue 分析失败: {str(e)[:500]}",
                            })

                            # 发布 SSE 事件通知前端（失败）
                            try:
                                from backend.webui.sse import publish_event

                                await publish_event(
                                    "issue:status_changed",
                                    {
                                        "issue_number": issue_info.get("issue_number"),
                                        "repo_name": issue_info.get("repo_name"),
                                        "status": "failed",
                                    },
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass

        return task_id

    async def _archive_old_versions(
        self, db, repo_name: str, issue_number: int, archive_count: int
    ):
        """归档超出上限的旧版本分析记录（标记为 archived 状态）"""
        try:
            old_records = await db.execute(
                select(IssueAnalysis)
                .where(
                    and_(
                        IssueAnalysis.repo_name == repo_name,
                        IssueAnalysis.issue_number == issue_number,
                    )
                )
                .order_by(IssueAnalysis.analysis_version.asc())
                .limit(archive_count)
            )
            for record in old_records.scalars().all():
                if record.status not in (
                    IssueAnalysisStatus.PENDING.value,
                    IssueAnalysisStatus.ANALYZING.value,
                ):
                    record.status = "archived"
            await db.commit()
            logger.info(
                f"已归档 {archive_count} 条旧版本分析: {repo_name}#{issue_number}"
            )
        except Exception as e:
            logger.warning(f"归档旧版本分析失败: {e}")


_worker_instance: Optional[IssueWorker] = None


def get_issue_worker() -> IssueWorker:
    """获取 IssueWorker 实例"""
    global _worker_instance
    if _worker_instance is None:
        _worker_instance = IssueWorker()
    return _worker_instance


async def submit_issue_analysis_task(issue_info: Dict[str, Any]) -> str:
    """提交 Issue 分析任务"""
    task_id = str(uuid.uuid4())
    issue_info["task_id"] = task_id
    worker = get_issue_worker()
    asyncio.create_task(worker.process_issue_analysis(issue_info))
    return task_id
