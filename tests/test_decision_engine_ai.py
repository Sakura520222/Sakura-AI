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
