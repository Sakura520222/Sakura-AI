"""审查决策引擎"""

from typing import Dict, Any, Tuple
from loguru import logger

from backend.models.database import ReviewDecision
from backend.core.config import get_settings, get_strategy_config
from backend.core.language_utils import output_text


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
                "trust_ai_decision": True,
                "ai_decision_block_on_critical": True,
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
                "trust_ai_decision": True,
                "ai_decision_block_on_critical": True,
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

            # 主 PR 审查只信任已通过结构化协议校验的评分。
            score = review_result.get("overall_score")
            if score is None:
                logger.warning("缺少已验证评分，降级为人工复审")
                return (
                    ReviewDecision.COMMENT,
                    output_text(
                        "缺少有效的结构化评分，建议人工复审",
                        "The structured score is missing or invalid; manual review is required",
                    ),
                )

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

            # AI 建议决策路径
            ai_decision = review_result.get("ai_decision")
            ai_decision_reason = review_result.get("ai_decision_reason", "")

            if ai_decision and policy.get("trust_ai_decision", True):
                final_decision, final_reason = self._apply_ai_decision(
                    ai_decision=ai_decision,
                    ai_reason=ai_decision_reason,
                    critical_count=critical_count,
                    policy=policy,
                )
                logger.info(
                    f"AI 决策: {ai_decision} → 最终决策: {final_decision.value}"
                )
                return (final_decision, final_reason)

            # Fallback: 规则引擎决策
            return self._rule_based_decision(
                score=score,
                critical_count=critical_count,
                major_count=major_count,
                minor_count=minor_count,
                suggestion_count=suggestion_count,
                policy=policy,
            )

        except Exception as e:
            logger.error(f"决策引擎执行失败: {e}", exc_info=True)
            # 出错时默认为COMMENT，避免阻断
            return (ReviewDecision.COMMENT, f"决策过程出现异常: {str(e)}")

    def _apply_ai_decision(
        self,
        ai_decision: str,
        ai_reason: str,
        critical_count: int,
        policy: Dict[str, Any],
    ) -> Tuple[ReviewDecision, str]:
        """处理 AI 建议决策，应用安全护栏

        Args:
            ai_decision: AI 建议的决策
            ai_reason: AI 决策理由
            critical_count: critical 问题数量
            policy: 策略配置

        Returns:
            (最终决策, 决策理由)
        """
        if ai_decision == "request_changes":
            reason = ai_reason or output_text(
                "AI 建议驳回，存在需要修复的问题",
                "AI suggests changes, issues need fixing",
            )
            return (ReviewDecision.REQUEST_CHANGES, reason)

        if ai_decision == "approve":
            # 安全护栏：有 critical issue 时覆盖为 REQUEST_CHANGES
            if critical_count > 0 and policy.get("ai_decision_block_on_critical", True):
                logger.info(
                    f"AI 建议 approve 但存在 {critical_count} 个 critical issue，安全护栏覆盖为 REQUEST_CHANGES"
                )
                return (
                    ReviewDecision.REQUEST_CHANGES,
                    output_text(
                        f"AI 建议通过，但发现 {critical_count} 个严重问题必须修复",
                        f"AI approved but found {critical_count} critical issues that must be fixed",
                    ),
                )
            reason = ai_reason or output_text(
                "代码质量良好，符合合并标准",
                "Code quality is good, meets merge standards",
            )
            return (ReviewDecision.APPROVE, reason)

        if ai_decision == "comment":
            reason = ai_reason or output_text(
                "建议人工复审", "Manual review recommended"
            )
            return (ReviewDecision.COMMENT, reason)

        # 未知决策类型，fallback
        logger.warning(f"未知的 AI 决策类型: {ai_decision}")
        return (
            ReviewDecision.COMMENT,
            ai_reason or output_text("未知的 AI 决策类型", "Unknown AI decision type"),
        )

    def _rule_based_decision(
        self,
        score: int,
        critical_count: int,
        major_count: int,
        minor_count: int,
        suggestion_count: int,
        policy: Dict[str, Any],
    ) -> Tuple[ReviewDecision, str]:
        """基于规则的决策（原有逻辑，作为 fallback）

        Args:
            score: 代码质量评分
            critical_count: critical 问题数量
            major_count: major 问题数量
            minor_count: minor 问题数量
            suggestion_count: suggestion 问题数量
            policy: 策略配置

        Returns:
            (决策类型, 决策理由)
        """
        # 规则1: Critical问题阻断（一票否决）
        if critical_count > 0 and policy.get("block_on_critical", True):
            return (
                ReviewDecision.REQUEST_CHANGES,
                output_text(
                    f"发现 {critical_count} 个严重问题必须修复后才能合并",
                    f"Found {critical_count} critical issues that must be fixed before merging",
                ),
            )

        # 规则2: 低分阻断
        block_threshold = policy.get("block_threshold", 4)
        if score < block_threshold:
            return (
                ReviewDecision.REQUEST_CHANGES,
                output_text(
                    f"代码质量评分 ({score}/10) 低于最低要求 ({block_threshold}/10)",
                    f"Code quality score ({score}/10) is below the minimum requirement ({block_threshold}/10)",
                ),
            )

        # 规则3: 高分批准
        approve_threshold = policy.get("approve_threshold", 8)
        max_major = policy.get("max_major_issues", 1)

        if score >= approve_threshold and major_count <= max_major:
            return (
                ReviewDecision.APPROVE,
                output_text(
                    "代码质量优秀，符合合并标准",
                    "Excellent code quality, meets merge standards",
                ),
            )

        # 规则4: 中间状态 - 中立评论
        return (
            ReviewDecision.COMMENT,
            output_text(
                f"代码质量评分 ({score}/10) 处于中间状态，建议人工复审",
                f"Code quality score ({score}/10) is in the middle range, manual review recommended",
            ),
        )

    def format_review_body(
        self,
        decision: ReviewDecision,
        review_result: Dict[str, Any],
        decision_reason: str,
        label_results: Dict[str, Any] = None,
        strategy_name: str = "代码审查",
        template_vars: Dict[str, Any] = None,
        output_language: str | None = None,
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
            # 根据 output_language 选择模板 / Select template based on output_language
            output_lang = (
                output_language
                if output_language is not None
                else get_settings().output_language
            )
            if output_lang == "en":
                templates = self.policy.get("review_templates_en", {})
            else:
                templates = self.policy.get("review_templates", {})

            template_key = decision.value
            fallback_template = (
                "{summary}\n\nScore: {score}/10\n\nDecision: {decision_reason}"
                if output_lang == "en"
                else "{summary}\n\n评分: {score}/10\n\n决策: {decision_reason}"
            )
            template = templates.get(template_key, fallback_template)

            # 准备变量（改进评分显示逻辑）
            score = review_result.get("overall_score")
            if score is None or score == "N/A":
                # 尝试提取评分（最后一道防线）
                from backend.services.score_extractor import score_extractor

                extracted = score_extractor.extract_score(review_result)
                score = extracted if extracted is not None else "N/A"

            no_summary_text = (
                "No summary available" if output_lang == "en" else "暂无摘要"
            )
            view_detail_text = (
                "View detailed review report"
                if output_lang == "en"
                else "查看详细审查报告"
            )
            summary = review_result.get("summary", no_summary_text)
            if summary.strip():
                summary = (
                    f"<details><summary>📋 {view_detail_text}</summary>\n\n"
                    f"{summary}\n\n"
                    f"</details>"
                )

            # 构建问题摘要
            issues = review_result.get("issues", {})
            comment_parts = []

            section_config = (
                (
                    "critical",
                    "🔴",
                    "Critical Issues" if output_lang == "en" else "严重问题",
                    "issues" if output_lang == "en" else "个",
                ),
                (
                    "major",
                    "🟡",
                    "Major Issues" if output_lang == "en" else "重要问题",
                    "issues" if output_lang == "en" else "个",
                ),
                (
                    "minor",
                    "🔵",
                    "Minor Issues" if output_lang == "en" else "次要问题",
                    "issues" if output_lang == "en" else "个",
                ),
                (
                    "suggestions",
                    "💡",
                    "Suggestions" if output_lang == "en" else "优化建议",
                    "suggestions" if output_lang == "en" else "条",
                ),
            )

            for key, icon, title, unit in section_config:
                values = [
                    str(issue).strip()
                    for issue in issues.get(key, [])
                    if str(issue).strip()
                ]
                if not values:
                    continue

                count_text = (
                    f"{len(values)} {unit}"
                    if output_lang == "en"
                    else f"{len(values)}{unit}"
                )
                comment_parts.append(f"\n### {icon} {title} ({count_text})\n")
                for issue in values[:3]:
                    issue_text = issue[:150] + "..." if len(issue) > 150 else issue
                    comment_parts.append(f"- {issue_text}\n")

                remaining = len(values) - 3
                if remaining > 0:
                    remaining_text = (
                        f"- ...and {remaining} more\n"
                        if output_lang == "en"
                        else f"- ...还有 {remaining} 条\n"
                    )
                    comment_parts.append(remaining_text)

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

            # 行内评论始终镜像到 Review Body。即使模板没有使用 comment_summary，
            # 或评论无法附着到 GitHub Diff，审查内容也不会丢失。
            inline_comments = review_result.get(
                "review_body_inline_comments",
                review_result.get("inline_comments", []),
            )
            if inline_comments:
                inline_title = (
                    "Inline Comments" if output_lang == "en" else "行内评论"
                )
                inline_unit = (
                    "comment" if len(inline_comments) == 1 else "comments"
                ) if output_lang == "en" else "条"
                count_text = (
                    f"{len(inline_comments)} {inline_unit}"
                    if output_lang == "en"
                    else f"{len(inline_comments)}{inline_unit}"
                )
                inline_parts = [f"### 📍 {inline_title} ({count_text})"]
                for comment in inline_comments:
                    file_path = str(comment.get("file_path") or "unknown")
                    end_line = comment.get("line_number")
                    start_line = comment.get("start_line")
                    if start_line and end_line and start_line != end_line:
                        location = f"{file_path}:{start_line}-{end_line}"
                    elif end_line:
                        location = f"{file_path}:{end_line}"
                    else:
                        location = file_path
                    severity = str(comment.get("severity", "suggestion"))
                    comment_body = str(comment.get("body", "")).strip()
                    inline_parts.append(
                        f"#### `{location}` · `{severity}`\n\n{comment_body}"
                    )
                body += "\n\n" + "\n\n".join(inline_parts)

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
