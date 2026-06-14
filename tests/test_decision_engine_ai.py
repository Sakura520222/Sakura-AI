"""Test AI decision support in decision_engine.py"""

from unittest.mock import patch

import pytest

from backend.models.database import ReviewDecision
from backend.services.decision_engine import DecisionEngine


@pytest.fixture
def engine():
    with patch("backend.services.decision_engine.get_strategy_config") as mock_config:
        mock_config.return_value.config = {
            "review_policy": {
                "enabled": True,
                "approve_threshold": 8,
                "block_threshold": 4,
                "block_on_critical": True,
                "max_major_issues": 1,
                "trust_ai_decision": True,
                "ai_decision_block_on_critical": True,
            }
        }
        return DecisionEngine()


def _review_result(
    score=7,
    ai_decision=None,
    ai_decision_reason=None,
    critical_count=0,
    major_count=0,
    minor_count=0,
    suggestion_count=0,
):
    """Helper: build review result dict"""
    return {
        "overall_score": score,
        "ai_decision": ai_decision,
        "ai_decision_reason": ai_decision_reason,
        "issues": {
            "critical": [f"critical-{i}" for i in range(critical_count)],
            "major": [f"major-{i}" for i in range(major_count)],
            "minor": [f"minor-{i}" for i in range(minor_count)],
            "suggestions": [f"suggestion-{i}" for i in range(suggestion_count)],
        },
    }


# =============================================================================
# AI decision tests
# =============================================================================


class TestAiDecision:
    def test_ai_request_changes_adopted_directly(self, engine):
        """AI says request_changes → directly adopted"""
        result = _review_result(
            score=9, ai_decision="request_changes", ai_decision_reason="安全漏洞"
        )
        decision, reason = engine.make_decision(result, "owner/repo")
        assert decision == ReviewDecision.REQUEST_CHANGES
        assert "安全漏洞" in reason

    def test_ai_approve_without_critical(self, engine):
        """AI says approve, no critical → adopted"""
        result = _review_result(
            score=9, ai_decision="approve", ai_decision_reason="代码优秀"
        )
        decision, reason = engine.make_decision(result, "owner/repo")
        assert decision == ReviewDecision.APPROVE
        assert "代码优秀" in reason

    def test_ai_approve_with_critical_overridden(self, engine):
        """AI says approve but critical issues exist → safety guardrail overrides"""
        result = _review_result(
            score=9,
            ai_decision="approve",
            ai_decision_reason="看起来不错",
            critical_count=1,
        )
        decision, reason = engine.make_decision(result, "owner/repo")
        assert decision == ReviewDecision.REQUEST_CHANGES
        assert "严重问题" in reason

    def test_ai_comment_adopted(self, engine):
        """AI says comment → adopted"""
        result = _review_result(
            score=6, ai_decision="comment", ai_decision_reason="建议人工复审"
        )
        decision, reason = engine.make_decision(result, "owner/repo")
        assert decision == ReviewDecision.COMMENT
        assert "人工复审" in reason

    def test_no_ai_decision_fallback_to_rules(self, engine):
        """No AI decision → falls back to rule-based logic"""
        # High score, no issues → should approve
        result = _review_result(score=9, major_count=0)
        decision, reason = engine.make_decision(result, "owner/repo")
        assert decision == ReviewDecision.APPROVE

    def test_no_ai_decision_critical_blocks(self, engine):
        """No AI decision, critical issue → rule blocks"""
        result = _review_result(score=9, critical_count=1)
        decision, reason = engine.make_decision(result, "owner/repo")
        assert decision == ReviewDecision.REQUEST_CHANGES

    def test_no_ai_decision_low_score_blocks(self, engine):
        """No AI decision, low score → rule blocks"""
        result = _review_result(score=2)
        decision, reason = engine.make_decision(result, "owner/repo")
        assert decision == ReviewDecision.REQUEST_CHANGES

    def test_missing_validated_score_requires_manual_review(self, engine):
        result = _review_result(
            score=None,
            ai_decision="approve",
            ai_decision_reason="Looks good",
        )
        decision, reason = engine.make_decision(result, "owner/repo")
        assert decision == ReviewDecision.COMMENT
        assert "人工复审" in reason


class TestTrustAiDecisionConfig:
    def test_trust_ai_decision_false_ignores_ai(self, engine):
        """trust_ai_decision=false → ignore AI decision, use rules"""
        # Patch policy to disable trust_ai_decision
        engine.policy["trust_ai_decision"] = False
        result = _review_result(
            score=9, ai_decision="request_changes", ai_decision_reason="不好"
        )
        decision, _ = engine.make_decision(result, "owner/repo")
        # Score=9, no critical/major → should APPROVE by rules
        assert decision == ReviewDecision.APPROVE


class TestAiDecisionBlockOnCriticalConfig:
    def test_block_on_critical_false_allows_approve(self, engine):
        """ai_decision_block_on_critical=false → AI approve passes even with critical"""
        engine.policy["ai_decision_block_on_critical"] = False
        result = _review_result(
            score=9,
            ai_decision="approve",
            ai_decision_reason="可以合并",
            critical_count=1,
        )
        decision, reason = engine.make_decision(result, "owner/repo")
        assert decision == ReviewDecision.APPROVE
        assert "可以合并" in reason


class TestReviewBodyFormatting:
    def test_minor_and_suggestions_include_entries(self, engine):
        engine.policy["review_templates"] = {
            "approve": "{summary}\n{comment_summary}"
        }
        result = _review_result(score=9, minor_count=1, suggestion_count=1)

        body = engine.format_review_body(
            ReviewDecision.APPROVE,
            result,
            "可以合并",
            output_language="zh-CN",
        )

        assert "### 🔵 次要问题 (1个)" in body
        assert "- minor-0" in body
        assert "### 💡 优化建议 (1条)" in body
        assert "- suggestion-0" in body

    def test_empty_issue_values_do_not_create_blank_sections(self, engine):
        engine.policy["review_templates"] = {
            "approve": "{summary}\n{comment_summary}"
        }
        result = _review_result(score=9)
        result["issues"]["suggestions"] = ["", "   "]

        body = engine.format_review_body(
            ReviewDecision.APPROVE,
            result,
            "可以合并",
            output_language="zh-CN",
        )

        assert "优化建议" not in body

    def test_english_sections_render_entries(self, engine):
        engine.policy["review_templates_en"] = {
            "approve": "{summary}\n{comment_summary}"
        }
        result = _review_result(score=9, suggestion_count=4)

        body = engine.format_review_body(
            ReviewDecision.APPROVE,
            result,
            "Approved",
            output_language="en",
        )

        assert "### 💡 Suggestions (4 suggestions)" in body
        assert "- suggestion-0" in body
        assert "- ...and 1 more" in body

    def test_inline_comments_are_appended_when_template_omits_summary(self, engine):
        engine.policy["review_templates"] = {"approve": "{summary}"}
        result = _review_result(score=9)
        result["inline_comments"] = [
            {
                "file_path": "backend/example.py",
                "start_line": 10,
                "line_number": 12,
                "severity": "minor",
                "body": "**边界错误**\n\n循环会多执行一次。",
            }
        ]

        body = engine.format_review_body(
            ReviewDecision.APPROVE,
            result,
            "可以合并",
            output_language="zh-CN",
        )

        assert "### 📍 行内评论" not in body
        assert "#### 🔵 `backend/example.py:10-12` · `minor`" in body
        assert "**边界错误**" in body
        assert "循环会多执行一次。" in body

    def test_inline_comment_entry_prefixed_with_severity_emoji(self, engine):
        """Each inline comment heading is prefixed with its severity emoji."""
        engine.policy["review_templates"] = {"approve": "{summary}"}
        result = _review_result(score=9)
        result["inline_comments"] = [
            {
                "file_path": "backend/example.py",
                "line_number": 10,
                "severity": "minor",
                "body": "**问题**\n\n描述。",
            }
        ]

        body = engine.format_review_body(
            ReviewDecision.APPROVE,
            result,
            "可以合并",
            output_language="zh-CN",
        )

        # 每条行内评论标题前缀对应 severity 的 emoji / Each inline
        # comment heading is prefixed with its severity emoji.
        assert "#### 🔵 `backend/example.py:10` · `minor`" in body

    def test_severity_emoji_constant_unified(self):
        """权威 severity→emoji 映射值统一 / Canonical severity→emoji mapping.

        所有模块（comment_service / scan_report_service / decision_engine）
        都从此常量取 emoji，值变更会波及全局，需锁定。
        """
        from backend.services.ai_reviewer.constants import SEVERITY_EMOJI

        assert SEVERITY_EMOJI["critical"] == "🔴"
        assert SEVERITY_EMOJI["major"] == "🟡"
        assert SEVERITY_EMOJI["minor"] == "🔵"
        assert SEVERITY_EMOJI["suggestion"] == "💡"

    def test_filtered_inline_comments_remain_visible_in_review_body(self, engine):
        engine.policy["review_templates_en"] = {"approve": "{summary}"}
        result = _review_result(score=9)
        result["inline_comments"] = []
        result["review_body_inline_comments"] = [
            {
                "file_path": "backend/example.py",
                "line_number": 59,
                "severity": "suggestion",
                "body": "**Repeated conversion**\n\nAvoid calling int() twice.",
            }
        ]

        body = engine.format_review_body(
            ReviewDecision.APPROVE,
            result,
            "Approved",
            output_language="en",
        )

        assert "### 📍 Inline Comments" not in body
        assert "#### 💡 `backend/example.py:59` · `suggestion`" in body
        assert "Avoid calling int() twice." in body

    def test_inline_comments_rendered_inside_details_block(self, engine):
        """Inline comments must render inside the <details> block, not after it."""
        engine.policy["review_templates"] = {
            "approve": "{summary}\n\n{comment_summary}"
        }
        result = _review_result(score=9)
        result["summary"] = "## 审查总结\n\n代码质量良好。"
        result["inline_comments"] = [
            {
                "file_path": "backend/example.py",
                "line_number": 42,
                "severity": "suggestion",
                "body": "**小建议**\n\n可简化此逻辑。",
            }
        ]

        body = engine.format_review_body(
            ReviewDecision.APPROVE,
            result,
            "可以合并",
            output_language="zh-CN",
        )

        # 行内评论应位于 <details> 展开块之内；标题已移除，改用评论正文定位 /
        # Inline comments must render inside the <details> block. Locate by
        # the comment body text since the section heading is no longer rendered.
        details_close_idx = body.index("</details>")
        inline_idx = body.index("可简化此逻辑。")
        assert inline_idx < details_close_idx, (
            "inline comments should render inside the <details> block"
        )

    def test_inline_comments_survive_when_template_omits_summary_placeholder(
        self, engine
    ):
        """Inline comments must still mirror when the template lacks {summary}."""
        engine.policy["review_templates"] = {"approve": "评分: {score}/10"}
        result = _review_result(score=9)
        result["inline_comments"] = [
            {
                "file_path": "backend/example.py",
                "line_number": 42,
                "severity": "suggestion",
                "body": "**提示**\n\n可优化。",
            }
        ]

        body = engine.format_review_body(
            ReviewDecision.APPROVE,
            result,
            "可以合并",
            output_language="zh-CN",
        )

        # 模板未渲染 {summary}，但行内评论仍必须镜像到 body /
        # Even without {summary}, inline comments must still be mirrored.
        assert "### 📍 行内评论" not in body
        assert "#### 💡 `backend/example.py:42` · `suggestion`" in body

    def test_unvalidated_summary_score_is_not_displayed(self, engine):
        engine.policy["review_templates"] = {
            "comment": "{summary}\n评分: {score}/10"
        }
        result = _review_result(score=None)
        result["summary"] = "旧格式摘要声称评分：7"

        body = engine.format_review_body(
            ReviewDecision.COMMENT,
            result,
            "建议人工复审",
            output_language="zh-CN",
        )

        assert "评分: N/A/10" in body
        assert "评分: 7/10" not in body

    def test_malformed_inline_comment_is_still_mirrored(self, engine):
        engine.policy["review_templates"] = {"approve": "{missing_variable}"}
        result = _review_result(score=9)
        result["summary"] = "Summary"
        result["review_body_inline_comments"] = ["raw malformed comment"]

        body = engine.format_review_body(
            ReviewDecision.APPROVE,
            result,
            "可以合并",
            output_language="zh-CN",
        )

        assert "**AI审查决策**: approve" in body
        assert "### 📍 行内评论" not in body
        assert "#### 💡 `unknown` · `suggestion`" in body
        assert "raw malformed comment" in body
