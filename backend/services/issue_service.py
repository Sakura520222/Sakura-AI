"""Issue 管理服务"""

import asyncio
import json
import math
import threading
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from sqlalchemy import select, func, desc, and_
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from backend.models.database import IssueAnalysis, IssueAnalysisStatus
from backend.core.github_app import GitHubAppClient
from backend.core.config import get_settings, get_strategy_config


class IssueService:
    """Issue 管理服务（单例模式）"""

    _instance = None
    _lock = threading.Lock()
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.github_app = GitHubAppClient()
            self.__class__._initialized = True

    async def save_analysis_result(
        self,
        analysis_data: Dict[str, Any],
        issue_info: Dict[str, Any],
        db: AsyncSession,
    ) -> Optional[IssueAnalysis]:
        """保存分析结果到数据库（更新已有的 PENDING 记录，而非创建新记录）"""

        # 查找已有的 PENDING/ANALYZING 记录
        conditions = [
            IssueAnalysis.repo_name == issue_info["repo_name"],
            IssueAnalysis.issue_number == issue_info["issue_number"],
            IssueAnalysis.status.in_(
                [
                    IssueAnalysisStatus.PENDING.value,
                    IssueAnalysisStatus.ANALYZING.value,
                ]
            ),
        ]
        if "analysis_version" in issue_info:
            conditions.append(
                IssueAnalysis.analysis_version == issue_info["analysis_version"]
            )
        result = await db.execute(
            select(IssueAnalysis)
            .where(and_(*conditions))
            .order_by(IssueAnalysis.created_at.desc())
            .limit(1)
        )
        record = result.scalar_one_or_none()

        if not record:
            return None

        # 更新已有记录
        record.category = analysis_data.get("category")
        record.priority = analysis_data.get("priority")
        record.summary = analysis_data.get("summary")
        record.feasibility = analysis_data.get("feasibility")
        record.suggested_assignees = json.dumps(
            analysis_data.get("suggested_assignees", []), ensure_ascii=False
        )
        record.suggested_labels = json.dumps(
            analysis_data.get("suggested_labels", []), ensure_ascii=False
        )
        record.suggested_milestone = analysis_data.get("suggested_milestone")
        record.duplicate_of = analysis_data.get("duplicate_of")
        record.related_prs = json.dumps(
            analysis_data.get("related_prs", []), ensure_ascii=False
        )
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
            select(IssueAnalysis)
            .where(
                and_(
                    IssueAnalysis.repo_name == repo_name,
                    IssueAnalysis.issue_number == issue_number,
                )
            )
            .order_by(desc(IssueAnalysis.created_at))
            .limit(1)
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
        repo_owner: str,
        repo_name: str,
        issue_number: int,
        analysis: IssueAnalysis,
        db: AsyncSession,
    ) -> bool:
        """发布分析评论到 Issue"""
        config = get_strategy_config().get_issue_analysis_config()
        template = config.get("comment_template", "")

        if not template:
            return False

        labels = []
        try:
            labels_data = (
                json.loads(analysis.suggested_labels)
                if analysis.suggested_labels
                else []
            )
            labels = [f"`{label['name']}`" for label in labels_data[:5]]
        except (json.JSONDecodeError, TypeError):
            pass

        assignees = []
        try:
            assignees_data = (
                json.loads(analysis.suggested_assignees)
                if analysis.suggested_assignees
                else []
            )
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

        success = await asyncio.to_thread(
            self.github_app.create_issue_comment,
            repo_owner,
            repo_name,
            issue_number,
            body,
        )

        if success:
            analysis.comment_posted = 1
            await db.commit()

        return success

    async def apply_suggested_labels(
        self,
        repo_owner: str,
        repo_name: str,
        issue_number: int,
        suggested_labels: list,
        db: AsyncSession,
    ) -> Dict[str, Any]:
        """应用建议标签到 Issue（集成 LabelService，支持自动创建和置信度过滤）

        Args:
            repo_owner: 仓库所有者
            repo_name: 仓库名称
            issue_number: Issue 编号
            suggested_labels: AI 建议的标签列表 [{"name": str, "confidence": float, "reason": str}]
            db: 数据库会话

        Returns:
            应用结果字典
        """
        settings = get_settings()
        threshold = settings.issue_confidence_threshold
        result = {"applied": [], "suggested": [], "created": [], "failed": []}

        if not suggested_labels:
            return result

        # 使用 LabelService 获取仓库现有标签（带缓存）
        from backend.services.label_service import label_service

        existing_labels = await label_service.get_repo_labels(repo_owner, repo_name)
        existing_labels_lower = {k.lower(): k for k in existing_labels}

        for label in suggested_labels:
            label_name = label.get("name", "")
            confidence = label.get("confidence", 0)

            if not label_name:
                continue

            # 大小写不敏感匹配
            matched_name = existing_labels_lower.get(label_name.lower())

            if not matched_name:
                # 标签不存在：使用 LabelService 默认标签信息自动创建
                default_info = label_service.DEFAULT_LABELS.get(
                    label_name, {"color": "0366d6", "description": ""}
                )
                success = await asyncio.to_thread(
                    self.github_app.create_label,
                    repo_owner,
                    repo_name,
                    label_name,
                    default_info["color"],
                    default_info["description"],
                )
                if success:
                    result["created"].append(label_name)
                    logger.info(f"Issue #{issue_number} 自动创建标签: {label_name}")
                else:
                    result["failed"].append(label_name)
                    logger.warning(f"Issue #{issue_number} 创建标签失败: {label_name}")
                    continue
            else:
                label_name = matched_name

            # 根据置信度决定是否自动应用
            if confidence >= threshold:
                success = await asyncio.to_thread(
                    self.github_app.add_labels_to_issue,
                    repo_owner,
                    repo_name,
                    issue_number,
                    [label_name],
                )
                if success:
                    result["applied"].append(
                        {
                            "name": label_name,
                            "confidence": confidence,
                            "reason": label.get("reason", ""),
                        }
                    )
                else:
                    result["failed"].append(label_name)
                    logger.warning(f"Issue #{issue_number} 应用标签失败: {label_name}")
            else:
                result["suggested"].append(
                    {
                        "name": label_name,
                        "confidence": confidence,
                        "reason": label.get("reason", ""),
                    }
                )

        return result

    async def detect_duplicates(
        self,
        repo_owner: str,
        repo_name: str,
        title: str,
        body: str,
        current_issue_number: int = None,
    ) -> List[Dict[str, Any]]:
        """检测重复 Issue（GitHub Search API + AI 相似度二次筛选）"""
        keywords = title.split()[:5]
        query = " ".join(keywords)
        issues = await asyncio.to_thread(
            self.github_app.search_issues, repo_owner, repo_name, query, "open", 10
        )

        # 过滤当前 Issue
        candidates = []
        for issue in issues:
            if current_issue_number and issue.number == current_issue_number:
                continue
            candidates.append(issue)

        if not candidates:
            return []

        # AI 相似度二次筛选
        try:
            from backend.services.embedding_service import get_embedding_service

            embedding_service = get_embedding_service()

            current_text = f"{title}\n{body or ''}"
            candidate_texts = [f"{c.title}\n{c.body or ''}" for c in candidates]

            all_texts = [current_text] + candidate_texts
            embeddings = await embedding_service.embed_texts(all_texts)
            current_emb = embeddings[0]

            results = []
            for i, candidate in enumerate(candidates):
                sim = self._cosine_similarity(current_emb, embeddings[i + 1])
                if sim >= 0.75:
                    results.append(
                        {
                            "issue_number": candidate.number,
                            "title": candidate.title,
                            "state": candidate.state,
                            "similarity": round(sim, 3),
                        }
                    )

            results.sort(key=lambda x: x["similarity"], reverse=True)
            return results[:5]

        except Exception as e:
            logger.warning(f"AI 重复检测失败，回退到关键词匹配: {e}")
            return [
                {"issue_number": c.number, "title": c.title, "state": c.state}
                for c in candidates[:5]
            ]

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        """计算两个向量的余弦相似度"""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def find_related_prs(
        self,
        repo_owner: str,
        repo_name: str,
        issue_number: int,
    ) -> List[Dict[str, Any]]:
        """查找与 Issue 相关的 PRs"""
        query = f"fixes #{issue_number}"
        results = await asyncio.to_thread(
            self.github_app.search_issues,
            repo_owner,
            repo_name,
            query,
            "open",
            10,
            search_type="pr",
        )
        prs = []
        for item in results:
            prs.append(
                {
                    "number": item.number,
                    "title": item.title,
                    "state": item.state,
                }
            )
        return prs

    async def get_issue_stats(
        self, db: AsyncSession, scope_filter=None
    ) -> Dict[str, Any]:
        """获取 Issue 统计数据

        Args:
            db: 数据库会话
            scope_filter: 可选的用户数据范围过滤条件
        """

        def _apply_filter(query):
            if scope_filter is not None:
                return query.where(scope_filter)
            return query

        total_result = await db.execute(
            _apply_filter(select(func.count(IssueAnalysis.id)))
        )
        total = total_result.scalar() or 0

        total_cost_result = await db.execute(
            _apply_filter(
                select(func.coalesce(func.sum(IssueAnalysis.estimated_cost), 0))
            )
        )
        total_cost = total_cost_result.scalar() or 0

        total_prompt_result = await db.execute(
            _apply_filter(
                select(func.coalesce(func.sum(IssueAnalysis.prompt_tokens), 0))
            )
        )
        total_prompt_tokens = total_prompt_result.scalar() or 0

        total_completion_result = await db.execute(
            _apply_filter(
                select(func.coalesce(func.sum(IssueAnalysis.completion_tokens), 0))
            )
        )
        total_completion_tokens = total_completion_result.scalar() or 0

        category_stats = {}
        for cat in [
            "bug",
            "feature",
            "question",
            "documentation",
            "enhancement",
            "performance",
            "security",
            "refactor",
            "other",
        ]:
            result = await db.execute(
                _apply_filter(
                    select(func.count(IssueAnalysis.id)).where(
                        IssueAnalysis.category == cat
                    )
                )
            )
            category_stats[cat] = result.scalar() or 0

        priority_stats = {}
        for pri in ["critical", "high", "medium", "low"]:
            result = await db.execute(
                _apply_filter(
                    select(func.count(IssueAnalysis.id)).where(
                        IssueAnalysis.priority == pri
                    )
                )
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
