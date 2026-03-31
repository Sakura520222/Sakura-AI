"""Issue 分析异步任务处理器"""

import asyncio
import json
import uuid
from typing import Dict, Any, Optional
from loguru import logger

from backend.core.config import get_settings
from backend.core.github_app import GitHubAppClient
from backend.models.database import (
    async_session,
    IssueAnalysis,
    IssueAnalysisStatus,
)
from sqlalchemy import select, and_
from backend.services.issue_analyzer import IssueAnalyzer
from backend.services.issue_service import issue_service


class IssueWorker:
    """Issue 分析任务处理器"""

    def __init__(self):
        self.analyzer = IssueAnalyzer()
        self.github_app = GitHubAppClient()
        settings = get_settings()

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

        async with async_session() as db:
            try:
                # 1. 创建分析记录（PENDING）
                record = IssueAnalysis(
                    issue_number=issue_number,
                    repo_name=repo_name,
                    repo_owner=repo_owner,
                    author=issue_info.get("author", ""),
                    title=issue_info.get("title", ""),
                    body=issue_info.get("body", ""),
                    status=IssueAnalysisStatus.PENDING.value,
                    analysis_version=issue_info.get("analysis_version", 1),
                )
                db.add(record)
                await db.commit()
                await db.refresh(record)

                # 2. 更新状态为 ANALYZING
                record.status = IssueAnalysisStatus.ANALYZING.value
                await db.commit()

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

                # 5. 保存分析结果（更新已有的 PENDING 记录）
                analysis_record = await issue_service.save_analysis_result(
                    analysis_result, issue_info, db
                )

                if not analysis_record:
                    logger.error(f"[{task_id}] 未找到待更新的分析记录")
                    return task_id

                # 6. 重复检测
                if settings.issue_detect_duplicates:
                    try:
                        duplicates = await issue_service.detect_duplicates(
                            repo_owner,
                            repo_name,
                            issue_info.get("title", ""),
                            issue_info.get("body", ""),
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

                # 8. 自动评论
                if settings.issue_auto_comment:
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
                if settings.issue_auto_create_labels:
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
                                    f"[{task_id}] 已应用标签: {result['applied']}"
                                )
                    except Exception as e:
                        logger.warning(f"[{task_id}] 应用标签失败: {e}")

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
                    from backend.telegram.notifications import get_notification_sender

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

                logger.info(
                    f"[{task_id}] Issue 分析完成: {repo_full_name}#{issue_number}"
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
                except Exception:
                    pass

        return task_id


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
