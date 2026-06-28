"""Tests for Issue analysis comment template localization."""

from types import SimpleNamespace

import pytest

from backend.services.issue_service import IssueService


def _analysis(**overrides):
    """Build a minimal IssueAnalysis-like record for rendering."""
    defaults = {
        "category": "bug",
        "priority": "high",
        "feasibility": "",
        "summary": "",
        "suggested_labels": "[]",
        "suggested_assignees": "[]",
        "suggested_milestone": None,
        "duplicate_of": None,
        "suggested_title": None,
        "related_prs": "[]",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_english_comment_has_no_chinese_fallbacks(monkeypatch):
    posted = {}

    async def fake_posted(self, **kwargs):
        # Capture the formatted body handed to create_issue_comment.
        posted["body"] = kwargs

    class _FakeApp:
        def create_issue_comment(self, owner, repo, number, body):
            posted["body"] = body
            return True

    class _StrategyConfig:
        def get_issue_analysis_config(self):
            return {
                "comment_template": "ZH\n{feasibility}|{summary}|{labels}|{assignees}|{related_info}|{suggested_title_section}",
                "comment_template_en": "EN\n{feasibility}|{summary}|{labels}|{assignees}|{related_info}|{suggested_title_section}",
            }

    monkeypatch.setattr(
        "backend.services.issue_service.get_strategy_config",
        lambda: _StrategyConfig(),
    )
    monkeypatch.setattr(
        "backend.services.issue_service.get_settings",
        lambda: SimpleNamespace(output_language="en"),
    )

    service = IssueService.__new__(IssueService)
    service.github_app = _FakeApp()

    class _NoCommit:
        async def commit(self):
            return None

    await service.post_analysis_comment("owner", "repo", 42, _analysis(), _NoCommit())

    body = posted["body"]
    assert body.startswith("EN\n")
    # English template must not leak Chinese fallback strings.
    for forbidden in (
        "暂无评估",
        "暂无摘要",
        "无建议",
        "可能与",
        "相关 PR",
        "建议标题",
    ):
        assert forbidden not in body, (
            f"English comment leaked Chinese text: {forbidden}"
        )


@pytest.mark.asyncio
async def test_chinese_comment_keeps_localized_fallbacks(monkeypatch):
    posted = {}

    class _FakeApp:
        def create_issue_comment(self, owner, repo, number, body):
            posted["body"] = body
            return True

    class _StrategyConfig:
        def get_issue_analysis_config(self):
            return {
                "comment_template": "ZH\n{feasibility}|{summary}|{labels}|{assignees}",
                "comment_template_en": "EN\n{feasibility}|{summary}|{labels}|{assignees}",
            }

    monkeypatch.setattr(
        "backend.services.issue_service.get_strategy_config",
        lambda: _StrategyConfig(),
    )
    monkeypatch.setattr(
        "backend.services.issue_service.get_settings",
        lambda: SimpleNamespace(output_language="zh-CN"),
    )

    service = IssueService.__new__(IssueService)
    service.github_app = _FakeApp()

    class _NoCommit:
        async def commit(self):
            return None

    await service.post_analysis_comment("owner", "repo", 42, _analysis(), _NoCommit())

    body = posted["body"]
    assert body.startswith("ZH\n")
    assert "暂无评估" in body
    assert "暂无摘要" in body
    assert "无建议" in body
