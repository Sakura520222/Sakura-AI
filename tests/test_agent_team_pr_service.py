"""Agent 专家团队 PR 服务测试"""

from backend.services.agent_team.pr_service import _ensure_bot_suffix


def test_ensure_bot_suffix_keeps_existing_suffix():
    assert _ensure_bot_suffix("sakura-ai-reviewer[bot]") == "sakura-ai-reviewer[bot]"


def test_ensure_bot_suffix_adds_missing_suffix():
    assert _ensure_bot_suffix("sakura-ai-reviewer") == "sakura-ai-reviewer[bot]"


def test_ensure_bot_suffix_uses_fallback_name():
    assert _ensure_bot_suffix(None) == "Sakura Agent[bot]"
