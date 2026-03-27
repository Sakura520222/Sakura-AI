"""审查决策引擎"""

from typing import Dict, Any, Tuple
from loguru import logger

from backend.models.database import ReviewDecision
from backend.core.config import get_strategy_config


class DecisionEngine:
    """审查决策引擎 - 根据AI评分和问题严重程度做出审查决策"""

    def __init__(self):
        """初始化决策引擎"""
        self.policy = self._load_policy()

    def _load_policy(self) -> Dict[str, Any]:
        """从配置加载审查策略"""
        try:
            policy = get_strategy_config().config.get("review_policy", {})

            # 设置默认值
            defaults = {
                "enabled": False,
                "approve_threshold": 8,
                "block_threshold": 4,
                "block_on_critical": True,
                "max_major_issues": 1,
                "enable_idempotency_check": True,
                "ignored_patterns": [],
                "repo_overrides": {},
            }

            # 合并配置
            for key, value in defaults.items():
                if key not in policy:
                    policy[key] = value

            logger.info(f"审查策略配置加载成功: enabled={policy['enabled']}")
            return policy

        except Exception as e:
            logger.error(f"加载审查策略配置失败: {e}")
            # 返回默认配置
            return {
                "enabled": False,
                "approve_threshold": 8,
                "block_threshold": 4,
                "block_on_critical": True,
                "max_major_issues": 1,
                "enable_idempotency_check": True,
                "ignored_patterns": [],
                "repo_overrides": {},
            }

    def _get_repo_policy(self, repo_full_name: str) -> Dict[str, Any]:
        """获取特定仓库的策略配置"""
        # 检查是否有仓库级别的覆盖配置
        repo_overrides = self.policy.get("repo_overrides", {})
        if repo_full_name in repo_overrides:
            repo_config = repo_overrides[repo_full_name]
            logger.info(f"使用仓库专属配置: {repo_full_name}")
            # 合并配置
            policy = self.policy.copy()
            policy.update(repo_config)
            return policy

        return self.policy

    def make_decision(
        self,
        review_result: Dict[str, Any],
        repo_full_name: str,
    ) -> Tuple[ReviewDecision, str]:
        """根据审查结果做出决策

        Args:
            review_result: AI审查结果
            repo_full_name: 仓库全名（用于获取特定配置）

        Returns:
            (决策类型, 决策理由)
        """
        try:
            # 获取该仓库的策略
            policy = self._get_repo_policy(repo_full_name)

            # 检查是否启用自动批准
            if not policy.get("enabled", False):
                return (ReviewDecision.COMMENT, "自动批准功能未启用，仅提供评论")

            # 提取评分和问题统计（使用ScoreExtractor支持fallback）
            from backend.services.score_extractor import score_extractor

            score = review_result.get("overall_score")
            if score is None:
                logger.warning("overall_score为None，尝试从summary提取评分")
                score = score_extractor.extract_score(review_result)

            if score is None:
                logger.warning("无法提取评分，使用默认值0")
                score = 0

            issues = review_result.get("issues", {})

            critical_count = len(issues.get("critical", []))
            major_count = len(issues.get("major", []))
            minor_count = len(issues.get("minor", []))
            suggestion_count = len(issues.get("suggestions", []))

            logger.info(
                f"决策分析: score={score}, "
                f"critical={critical_count}, major={major_count}, "
                f"minor={minor_count}, suggestions={suggestion_count}"
            )

            # 如果没有评分，记录警告
            if review_result.get("overall_score") is None:
                logger.warning("AI未返回评分，使用默认值0进行决策")

            # 规则1: Critical问题阻断（一票否决）
            if critical_count > 0 and policy.get("block_on_critical", True):
                return (
                    ReviewDecision.REQUEST_CHANGES,
                    f"发现 {critical_count} 个严重问题必须修复后才能合并",
                )

            # 规则2: 低分阻断
            block_threshold = policy.get("block_threshold", 4)
            if score < block_threshold:
                return (
                    ReviewDecision.REQUEST_CHANGES,
                    f"代码质量评分 ({score}/10) 低于最低要求 ({block_threshold}/10)",
                )

            # 规则3: 高分批准
            approve_threshold = policy.get("approve_threshold", 8)
            max_major = policy.get("max_major_issues", 1)

            if score >= approve_threshold and major_count <= max_major:
                # 构建批准理由
                reason_parts = [
                    f"代码质量评分: {score}/10 (达到批准标准 {approve_threshold}/10)",
                    f"严重问题: {critical_count} 个",
                    f"重要问题: {major_count} 个 (上限 {max_major} 个)",
                ]

                if minor_count > 0:
                    reason_parts.append(f"次要问题: {minor_count} 个")
                if suggestion_count > 0:
                    reason_parts.append(f"优化建议: {suggestion_count} 条")

                return (ReviewDecision.APPROVE, "代码质量优秀，符合合并标准")

            # 规则4: 中间状态 - 中立评论
            return (
                ReviewDecision.COMMENT,
                f"代码质量评分 ({score}/10) 处于中间状态，建议人工复审",
            )

        except Exception as e:
            logger.error(f"决策引擎执行失败: {e}", exc_info=True)
            # 出错时默认为COMMENT，避免阻断
            return (ReviewDecision.COMMENT, f"决策过程出现异常: {str(e)}")

    def format_review_body(
        self,
        decision: ReviewDecision,
        review_result: Dict[str, Any],
        decision_reason: str,
        label_results: Dict[str, Any] = None,
        strategy_name: str = "代码审查",
        template_vars: Dict[str, Any] = None,
    ) -> str:
        """格式化审查评论内容

        Args:
            decision: 审查决策
            review_result: 审查结果
            decision_reason: 决策理由
            label_results: 标签应用结果
            strategy_name: 策略名称
            template_vars: 模板变量

        Returns:
            格式化后的评论内容
        """
        try:
            # 获取模板
            templates = self.policy.get("review_templates", {})

            template_key = decision.value
            template = templates.get(
                template_key, "{summary}\n\n评分: {score}/10\n\n决策: {decision_reason}"
            )

            # 准备变量（改进评分显示逻辑）
            score = review_result.get("overall_score")
            if score is None or score == "N/A":
                # 尝试提取评分（最后一道防线）
                from backend.services.score_extractor import score_extractor

                extracted = score_extractor.extract_score(review_result)
                score = extracted if extracted is not None else "N/A"

            summary = review_result.get("summary", "暂无摘要")

            # 构建问题摘要
            issues = review_result.get("issues", {})
            comment_parts = []

            # 严重问题：显示标题和具体内容（最多3个）
            if issues.get("critical"):
                critical_issues = issues["critical"][:3]  # 最多显示3个
                comment_parts.append(
                    f"\n### 🔴 严重问题 ({len(issues['critical'])}个)\n"
                )
                for issue in critical_issues:
                    # 截断过长的描述
                    issue_str = issue[:150] + "..." if len(issue) > 150 else issue
                    comment_parts.append(f"- {issue_str}\n")
                if len(issues["critical"]) > 3:
                    comment_parts.append(
                        f"- ...还有 {len(issues['critical']) - 3} 个严重问题\n"
                    )

            # 重要问题：显示标题和具体内容（最多3个）
            if issues.get("major"):
                major_issues = issues["major"][:3]  # 最多显示3个
                comment_parts.append(f"\n### 🟡 重要问题 ({len(issues['major'])}个)\n")
                for issue in major_issues:
                    issue_str = issue[:150] + "..." if len(issue) > 150 else issue
                    comment_parts.append(f"- {issue_str}\n")
                if len(issues["major"]) > 3:
                    comment_parts.append(
                        f"- ...还有 {len(issues['major']) - 3} 个重要问题\n"
                    )

            # 次要问题：只显示标题
            if issues.get("minor"):
                comment_parts.append(f"\n### 🔵 次要问题 ({len(issues['minor'])}个)\n")

            # 优化建议：只显示标题
            if issues.get("suggestions"):
                comment_parts.append(
                    f"\n### 💡 优化建议 ({len(issues['suggestions'])}条)\n"
                )

            comment_summary = "\n".join(comment_parts)

            # 填充模板
            body = template.format(
                summary=summary,
                score=score,
                decision_reason=decision_reason,
                comment_summary=comment_summary,
                strategy_name=strategy_name,
                **(template_vars or {}),
            )

            # 如果有标签结果，添加到评论末尾
            if label_results:
                from backend.services.label_service import label_service

                label_section = label_service.format_label_results(label_results)
                body += "\n\n" + label_section

            return body

        except Exception as e:
            logger.error(f"格式化审查评论失败: {e}")
            # 返回简单格式（尝试提取评分）
            from backend.services.score_extractor import score_extractor

            score = review_result.get("overall_score")
            if score is None:
                score = score_extractor.extract_score(review_result)
            score_display = f"{score}/10" if score is not None else "N/A"

            return (
                f"**AI审查决策**: {decision.value}\n\n"
                f"**理由**: {decision_reason}\n\n"
                f"**评分**: {score_display}\n\n"
                f"{review_result.get('summary', '')}"
            )


# 全局实例
_decision_engine = None


def get_decision_engine() -> DecisionEngine:
    """获取决策引擎实例"""
    global _decision_engine
    if _decision_engine is None:
        _decision_engine = DecisionEngine()
    return _decision_engine
