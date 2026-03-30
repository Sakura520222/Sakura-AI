"""Issue 管理服务"""

import json
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from sqlalchemy import select, func, desc, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from backend.models.database import Base, IssueAnalysis, IssueAnalysisStatus
from backend.core.github_app import GitHubAppClient
from backend.core.config import get_settings, get_strategy_config


class IssueService:
    """Issue 管理服务（单例模式）"""

    _instance = None
    _lock = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            import threading
            cls._lock = threading.Lock()
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.github_app = GitHubAppClient()
            self.__class__._initialized = True

    async def save_analysis_result(
        self, analysis_data: Dict[str, Any], issue_info: Dict[str, Any], db: AsyncSession
    ) -> Optional[IssueAnalysis]:
        """保存分析结果到数据库（更新已有的 PENDING 记录，而非创建新记录）"""
        from datetime import datetime

        # 查找已有的 PENDING/ANALYZING 记录
        result = await db.execute(
            select(IssueAnalysis).where(
                and_(
                    IssueAnalysis.repo_name == issue_info["repo_name"],
                    IssueAnalysis.issue_number == issue_info["issue_number"],
                    IssueAnalysis.status.in_([
                        IssueAnalysisStatus.PENDING.value,
                        IssueAnalysisStatus.ANALYZING.value,
                    ]),
                )
            ).order_by(IssueAnalysis.created_at.desc()).limit(1)
        )
        record = result.scalar_one_or_none()

        if not record:
            return None

        # 更新已有记录
        record.category = analysis_data.get("category")
        record.priority = analysis_data.get("priority")
        record.summary = analysis_data.get("summary")
        record.feasibility = analysis_data.get("feasibility")
        record.suggested_assignees = json.dumps(analysis_data.get("suggested_assignees", []), ensure_ascii=False)
        record.suggested_labels = json.dumps(analysis_data.get("suggested_labels", []), ensure_ascii=False)
        record.suggested_milestone = analysis_data.get("suggested_milestone")
        record.duplicate_of = analysis_data.get("duplicate_of")
        record.related_prs = json.dumps(analysis_data.get("related_prs", []), ensure_ascii=False)
        record.analysis_detail = json.dumps(analysis_data, ensure_ascii=False)
        record.status = IssueAnalysisStatus.COMPLETED.value
        record.prompt_tokens = analysis_data.get("prompt_tokens", 0)
        record.completion_tokens = analysis_data.get("completion_tokens", 0)
        record.estimated_cost = analysis_data.get("estimated_cost", 0)
        record.completed_at = datetime.utcnow()

        await db.commit()
        await db.refresh(record)
        return record

    async def get_analysis(
        self, repo_name: str, issue_number: int, db: AsyncSession
    ) -> Optional[IssueAnalysis]:
        """获取 Issue 的分析记录"""
        result = await db.execute(
            select(IssueAnalysis).where(
                and_(
                    IssueAnalysis.repo_name == repo_name,
                    IssueAnalysis.issue_number == issue_number,
                )
            ).order_by(desc(IssueAnalysis.created_at)).limit(1)
        )
        return result.scalar_one_or_none()

    async def get_analyses(
        self,
        db: AsyncSession,
        repo_name: str = None,
        category: str = None,
        priority: str = None,
        status: str = None,
        page: int = 1,
        per_page: int = 20,
    ) -> Tuple[List[IssueAnalysis], int]:
        """获取分析记录列表（带分页和过滤）"""
        query = select(IssueAnalysis)
        count_query = select(func.count(IssueAnalysis.id))

        if repo_name:
            query = query.where(IssueAnalysis.repo_name == repo_name)
            count_query = count_query.where(IssueAnalysis.repo_name == repo_name)
        if category:
            query = query.where(IssueAnalysis.category == category)
            count_query = count_query.where(IssueAnalysis.category == category)
        if priority:
            query = query.where(IssueAnalysis.priority == priority)
            count_query = count_query.where(IssueAnalysis.priority == priority)
        if status:
            query = query.where(IssueAnalysis.status == status)
            count_query = count_query.where(IssueAnalysis.status == status)

        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        query = query.order_by(desc(IssueAnalysis.created_at))
        query = query.offset((page - 1) * per_page).limit(per_page)
        result = await db.execute(query)
        items = result.scalars().all()

        return list(items), total

    async def post_analysis_comment(
        self,
        repo_owner: str, repo_name: str, issue_number: int,
        analysis: IssueAnalysis, db: AsyncSession,
    ) -> bool:
        """发布分析评论到 Issue"""
        config = get_strategy_config().get_issue_analysis_config()
        template = config.get("comment_template", "")

        if not template:
            return False

        labels = []
        try:
            labels_data = json.loads(analysis.suggested_labels) if analysis.suggested_labels else []
            labels = [f"`{l['name']}`" for l in labels_data[:5]]
        except (json.JSONDecodeError, TypeError):
            pass

        assignees = []
        try:
            assignees_data = json.loads(analysis.suggested_assignees) if analysis.suggested_assignees else []
            assignees = [f"@{a['username']}" for a in assignees_data[:3]]
        except (json.JSONDecodeError, TypeError):
            pass

        related_info = ""
        if analysis.duplicate_of:
            related_info += f"\n⚠️ 可能与 #{analysis.duplicate_of} 重复\n"
        try:
            prs = json.loads(analysis.related_prs) if analysis.related_prs else []
            if prs:
                related_info += f"\n🔗 相关 PR: {', '.join('#' + str(p.get('number', '')) for p in prs[:5])}\n"
        except (json.JSONDecodeError, TypeError):
            pass

        body = template.format(
            category=analysis.category or "unknown",
            priority=analysis.priority or "unknown",
            feasibility=analysis.feasibility or "暂无评估",
            summary=analysis.summary or "暂无摘要",
            labels=", ".join(labels) if labels else "无建议",
            assignees=", ".join(assignees) if assignees else "无建议",
            related_info=related_info,
        )

        success = self.github_app.create_issue_comment(
            repo_owner, repo_name, issue_number, body
        )

        if success:
            analysis.comment_posted = 1
            await db.commit()

        return success

    async def apply_suggested_labels(
        self,
        repo_owner: str, repo_name: str, issue_number: int,
        suggested_labels: list, db: AsyncSession,
    ) -> Dict[str, Any]:
        """应用建议标签"""
        settings = get_settings()
        threshold = settings.issue_confidence_threshold
        auto_create = settings.issue_auto_create_labels

        high_confidence = [l for l in suggested_labels if l.get("confidence", 0) >= threshold]
        applied = []
        suggested = []

        for label in suggested_labels:
            if label.get("confidence", 0) >= threshold:
                applied.append(label["name"])
            else:
                suggested.append(label["name"])

        if applied:
            success = self.github_app.add_labels_to_issue(
                repo_owner, repo_name, issue_number, applied
            )
            if success:
                return {"applied": applied, "suggested": suggested}
            logger.warning(f"应用标签失败: {applied}")

        return {"applied": [], "suggested": suggested}

    async def detect_duplicates(
        self, repo_owner: str, repo_name: str, title: str, body: str,
        current_issue_number: int = None,
    ) -> List[Dict[str, Any]]:
        """检测重复 Issue（GitHub Search API + AI 二次筛选）"""
        keywords = title.split()[:5]
        query = " ".join(keywords)
        issues = self.github_app.search_issues(repo_owner, repo_name, query, "open", 5)

        results = []
        for issue in issues[:5]:
            if current_issue_number and issue.number == current_issue_number:
                continue
            results.append({
                "issue_number": issue.number,
                "title": issue.title,
                "state": issue.state,
            })
        return results

    async def find_related_prs(
        self, repo_owner: str, repo_name: str, issue_number: int,
    ) -> List[Dict[str, Any]]:
        """查找与 Issue 相关的 PRs"""
        query = f"fixes #{issue_number}"
        results = self.github_app.search_issues(
            repo_owner, repo_name, query, "open", 10,
            search_type="pr",
        )
        prs = []
        for item in results:
            prs.append({
                "number": item.number,
                "title": item.title,
                "state": item.state,
            })
        return prs

    async def get_issue_stats(self, db: AsyncSession) -> Dict[str, Any]:
        """获取 Issue 统计数据"""
        total_result = await db.execute(select(func.count(IssueAnalysis.id)))
        total = total_result.scalar() or 0

        total_cost_result = await db.execute(
            select(func.coalesce(func.sum(IssueAnalysis.estimated_cost), 0))
        )
        total_cost = total_cost_result.scalar() or 0

        total_prompt_result = await db.execute(
            select(func.coalesce(func.sum(IssueAnalysis.prompt_tokens), 0))
        )
        total_prompt_tokens = total_prompt_result.scalar() or 0

        total_completion_result = await db.execute(
            select(func.coalesce(func.sum(IssueAnalysis.completion_tokens), 0))
        )
        total_completion_tokens = total_completion_result.scalar() or 0

        category_stats = {}
        for cat in ["bug", "feature", "question", "documentation", "enhancement", "performance", "security", "refactor", "other"]:
            result = await db.execute(
                select(func.count(IssueAnalysis.id)).where(IssueAnalysis.category == cat)
            )
            category_stats[cat] = result.scalar() or 0

        priority_stats = {}
        for pri in ["critical", "high", "medium", "low"]:
            result = await db.execute(
                select(func.count(IssueAnalysis.id)).where(IssueAnalysis.priority == pri)
            )
            priority_stats[pri] = result.scalar() or 0

        return {
            "total": total,
            "total_cost": total_cost,
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "by_category": category_stats,
            "by_priority": priority_stats,
        }


# 全局单例
issue_service = IssueService()
