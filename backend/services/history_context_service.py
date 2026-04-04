"""PR 增量审查历史上下文服务

为增量审查提供历史审查摘要，让 AI 能看到之前的审查结果，
包含评分趋势、关键问题演变等信息。
"""

from typing import List, Optional

from loguru import logger
from sqlalchemy import and_, select
from sqlalchemy.orm import selectinload

from backend.models.database import PRReview, PRStatus

# 摘要生成常量
HISTORY_SUMMARY_MAX_TOKENS = 1500
HISTORY_SUMMARY_TEMPERATURE = 0.2
MAX_HISTORY_REVIEWS = 5
MAX_COMMENTS_PER_REVIEW = 10
MAX_COMMENT_CONTENT_LENGTH = 200


class HistoryContextService:
    """PR 增量审查历史上下文服务"""

    def __init__(self, api_client):
        """初始化

        Args:
            api_client: AIApiClient 实例，复用已有的 API 客户端
        """
        self.api_client = api_client

    async def fetch_history_summary(
        self,
        pr_id: int,
        repo_name: str,
        repo_owner: str,
    ) -> Optional[str]:
        """查询并生成历史审查摘要

        Args:
            pr_id: GitHub PR ID
            repo_name: 仓库名称
            repo_owner: 仓库所有者

        Returns:
            AI 生成的历史审查摘要文本，查询失败返回 None
        """
        from backend.core.config import get_settings

        settings = get_settings()

        # 检查是否启用
        if not settings.enable_incremental_history_context:
            return None

        try:
            history_reviews = await self._query_history_reviews(
                pr_id, repo_name, repo_owner
            )

            if not history_reviews:
                logger.info(
                    f"PR {repo_owner}/{repo_name}#{pr_id} 无历史审查记录，跳过摘要生成"
                )
                return None

            logger.info(
                f"PR {repo_owner}/{repo_name}#{pr_id} 找到 {len(history_reviews)} 轮历史审查"
            )

            history_text = self._format_history_for_summary(history_reviews)
            summary = await self._generate_ai_summary(history_text)

            if summary:
                logger.info(f"历史审查摘要生成成功，长度: {len(summary)} 字符")

            return summary

        except Exception as e:
            logger.warning(f"历史审查摘要生成失败（不影响审查）: {e}", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _query_history_reviews(
        self,
        pr_id: int,
        repo_name: str,
        repo_owner: str,
    ) -> List[PRReview]:
        """从数据库查询该 PR 的历史审查记录

        查询已完成的审查记录（含关联评论），按时间正序排列。
        """
        from backend.core.config import get_settings
        from backend.models.database import async_session

        settings = get_settings()
        max_reviews = settings.incremental_history_max_reviews

        async with async_session() as session:
            stmt = (
                select(PRReview)
                .options(selectinload(PRReview.comments))
                .where(
                    and_(
                        PRReview.pr_id == pr_id,
                        PRReview.repo_name == repo_name,
                        PRReview.repo_owner == repo_owner,
                        PRReview.status == PRStatus.COMPLETED.value,
                        PRReview.overall_score.isnot(None),
                    )
                )
                .order_by(PRReview.created_at.asc())
                .limit(max_reviews)
            )

            result = await session.execute(stmt)
            return list(result.scalars().all())

    def _format_history_for_summary(self, reviews: List[PRReview]) -> str:
        """将历史审查记录格式化为待摘要的文本"""
        parts = []

        for idx, review in enumerate(reviews, 1):
            section = [
                f"### 第{idx}轮审查",
                f"- 时间: {review.created_at.strftime('%Y-%m-%d %H:%M') if review.created_at else 'N/A'}",
                f"- 策略: {review.strategy}",
                f"- 评分: {review.overall_score}/10",
                f"- 决策: {review.decision or 'N/A'}",
            ]

            if review.review_summary:
                summary_text = review.review_summary
                if len(summary_text) > 500:
                    summary_text = summary_text[:500] + "..."
                section.append(f"- 审查摘要: {summary_text}")

            # 按严重程度优先选取评论
            if review.comments:
                critical_comments = [
                    c for c in review.comments if c.severity in ("critical", "major")
                ]
                other_comments = [
                    c
                    for c in review.comments
                    if c.severity not in ("critical", "major")
                ]
                half = MAX_COMMENTS_PER_REVIEW // 2
                selected = critical_comments[:half] + other_comments[
                    : MAX_COMMENTS_PER_REVIEW - len(critical_comments[:half])
                ]

                if selected:
                    section.append("- 关键评论:")
                    for comment in selected:
                        content = comment.content
                        if len(content) > MAX_COMMENT_CONTENT_LENGTH:
                            content = content[:MAX_COMMENT_CONTENT_LENGTH] + "..."
                        location = ""
                        if comment.file_path:
                            location = f" [{comment.file_path}"
                            if comment.line_number:
                                location += f":{comment.line_number}"
                            location += "]"
                        section.append(
                            f"  - [{comment.severity}]{location}: {content}"
                        )

            parts.append("\n".join(section))

        return "\n\n".join(parts)

    async def _generate_ai_summary(self, history_text: str) -> Optional[str]:
        """调用 AI 生成历史审查的自然语言摘要"""
        from backend.core.config import get_settings

        settings = get_settings()

        response = await self.api_client.call_with_retry(
            messages=[
                {"role": "system", "content": self._build_summary_system_prompt()},
                {"role": "user", "content": self._build_summary_user_prompt(history_text)},
            ],
            model=settings.openai_model,
            temperature=HISTORY_SUMMARY_TEMPERATURE,
            max_tokens=settings.incremental_history_summary_max_tokens,
        )

        if not response or not response.choices:
            logger.warning("AI 摘要生成返回空响应")
            return None
        content = response.choices[0].message.content if response.choices[0].message else None
        return content.strip() if content else None

    @staticmethod
    def _build_summary_system_prompt() -> str:
        """构建摘要生成的系统提示词"""
        return """你是一个代码审查历史分析助手。你的任务是将多轮历史审查记录压缩为一段简洁的自然语言摘要，供下一轮增量审查的 AI 审查员参考。

## 摘要要求

1. **评分趋势**：用一句话概括评分的变化趋势（如"从6分逐渐提升到8分"）
2. **关键问题演变**：列出核心问题的变化（哪些已修复、哪些持续存在、哪些是新出现的）
3. **仍需关注的问题**：明确标注在最近一轮审查中仍未解决的关键问题
4. **文件热点**：哪些文件被频繁标记问题

## 输出格式

请直接输出摘要文本，不要使用 Markdown 标题（如 # 或 ##），不要使用 JSON，不要加任何前缀说明。
摘要应控制在 300-500 字以内。"""

    @staticmethod
    def _build_summary_user_prompt(history_text: str) -> str:
        """构建摘要生成的用户提示词"""
        return f"""以下是该 PR 之前所有轮次的审查记录，请生成一段增量审查所需的历史摘要。

{history_text}

请生成摘要，重点关注：评分趋势、问题演变、仍未修复的关键问题。"""
