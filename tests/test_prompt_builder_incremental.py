"""PromptBuilder 增量审查规则触发测试。

锁定：增量审查指导规则基于 analysis.is_incremental 触发，不再依赖
review_history_summary；user message 不再注入「历史审查上下文」段。
"""

from types import SimpleNamespace

from backend.services.ai_reviewer.prompt_builder import PromptBuilder


SAMPLE_FILES = [
    {
        "path": "backend/example.py",
        "status": "modified",
        "additions": 2,
        "deletions": 1,
        "changes": 3,
    }
]


def test_system_prompt_includes_incremental_rules_when_incremental():
    """增量审查（即使无 review_history_summary）也应注入增量指导规则。"""
    pb = PromptBuilder()
    context = {"analysis": SimpleNamespace(is_incremental=True)}
    prompt = pb.build_system_prompt("focus", context, include_tools=True)
    assert "## Incremental review history" in prompt
    assert "FILE=NONE" in prompt


def test_system_prompt_excludes_incremental_rules_when_not_incremental():
    """非增量审查不应注入增量指导规则。"""
    pb = PromptBuilder()
    context = {"analysis": SimpleNamespace(is_incremental=False)}
    prompt = pb.build_system_prompt("focus", context, include_tools=True)
    assert "## Incremental review history" not in prompt


def test_user_message_no_longer_injects_history_summary():
    """user message 不再注入历史摘要段，忽略残留的 review_history_summary。"""
    pb = PromptBuilder()
    context = {
        "files": SAMPLE_FILES,
        "review_history_summary": "stale summary that must not leak",
    }
    message = pb.build_user_message(
        context, "standard", include_tools=True, compact=True
    )
    assert "## 历史审查上下文" not in message
    assert "stale summary that must not leak" not in message
