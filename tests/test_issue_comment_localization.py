"""Tests for Issue analysis comment template localization."""

import json
from types import SimpleNamespace

import pytest

from backend.core.config_sections import get_section_defaults
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


def _render_default(monkeypatch, language, **overrides):
    config = get_section_defaults("strategy.issue_analysis")
    monkeypatch.setattr(
        "backend.services.issue_service.get_strategy_config",
        lambda: SimpleNamespace(get_issue_analysis_config=lambda: config),
    )
    monkeypatch.setattr(
        "backend.services.issue_service.get_settings",
        lambda: SimpleNamespace(output_language=language),
    )
    return object.__new__(IssueService).build_analysis_comment(_analysis(**overrides))


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_default_comment_leads_with_conclusions_and_hides_empty_metadata(
    monkeypatch, language
):
    summary, advice = (
        ("没有与项目相关的可处理事项。", "无需技术处理。")
        if language == "zh-CN"
        else (
            "There is no project-related actionable matter.",
            "No technical action needed.",
        )
    )
    body = _render_default(
        monkeypatch, language, summary=summary, feasibility=advice, category="other"
    )
    assert body.startswith("### Sakura AI · Issue ")
    assert body.index(summary) < body.index(advice) < body.index("<details>")
    assert "<summary>" in body and "</details>" in body
    assert "https://github.com/Sakura520222/Sakura-AI" in body
    for absent in (
        "建议标签",
        "建议指派",
        "建议标题",
        "相关 PR",
        "暂无摘要",
        "无建议",
        "Suggested labels",
        "Suggested assignees",
        "Suggested title",
        "Related PRs",
        "No summary",
        "No suggestions",
        "\n\n\n",
    ):
        assert absent not in body


@pytest.mark.parametrize("language", ["zh-CN", "en"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("suggested_labels", '[{"name": "bug"}]'),
        ("suggested_assignees", '[{"username": "alice"}]'),
        ("suggested_title", "Correct the title"),
        ("related_prs", '[{"number": 17}]'),
        ("duplicate_of", 23),
    ],
)
def test_default_comment_shows_each_available_optional_item(
    monkeypatch, language, field, value
):
    body = _render_default(monkeypatch, language, **{field: value})
    expected = {
        "suggested_labels": "`bug`",
        "suggested_assignees": "@alice",
        "suggested_title": "`Correct the title`",
        "related_prs": "#17",
        "duplicate_of": "#23",
    }[field]
    assert expected in body
    assert body.index("<details>") < body.index(expected) < body.index("</details>")
    assert "\n\n\n" not in body


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_default_comment_keeps_verified_relations_and_analysis_markdown(
    monkeypatch, language
):
    summary = "Conclusion.\n\n```python\nfirst = 1\n\n\nsecond = 2\n```"
    relations = {
        "primary": {
            "number": 21,
            "relation": "regression",
            "reason": "Prior fix regressed.",
        },
        "related": [
            {"number": 22, "relation": "related", "reason": "Shares the component."}
        ],
    }
    body = _render_default(
        monkeypatch,
        language,
        summary=summary,
        feasibility="Verify the failure path.",
        issue_relations=json.dumps(relations),
    )
    assert summary in body  # Preserve code-fence whitespace when cleaning the layout.
    for expected in ("#21", "#22", "Prior fix regressed.", "Shares the component."):
        assert expected in body
        assert body.index("<details>") < body.index(expected) < body.index("</details>")


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_default_comment_handles_whitespace_and_empty_legacy_fields(
    monkeypatch, language
):
    body = _render_default(
        monkeypatch,
        language,
        summary=" \n ",
        feasibility=" \n ",
        suggested_title="  ",
        suggested_labels='[{"name": " "}, {}]',
        suggested_assignees='[{"username": " "}, {}]',
    )
    assert ("Unable to assess yet" if language == "en" else "暂无法评估") in body
    for absent in (
        "暂无摘要",
        "No summary",
        "建议标签",
        "Suggested labels",
        "建议标题",
        "Suggested title",
        "@",
        "\n\n\n",
    ):
        assert absent not in body


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_historical_custom_template_keeps_formatting_and_fallbacks(
    monkeypatch, language
):
    config = get_section_defaults("strategy.issue_analysis")
    template = "Legacy\n\n\n{summary}|{feasibility}|{labels}|{assignees}|{category}|{priority}\n{suggested_title_section}\n{related_info}\n"
    config["comment_template_en" if language == "en" else "comment_template"] = template
    monkeypatch.setattr(
        "backend.services.issue_service.get_strategy_config",
        lambda: SimpleNamespace(get_issue_analysis_config=lambda: config),
    )
    monkeypatch.setattr(
        "backend.services.issue_service.get_settings",
        lambda: SimpleNamespace(output_language=language),
    )
    body = object.__new__(IssueService).build_analysis_comment(_analysis())
    fallback = (
        "No summary|No assessment|No suggestions|No suggestions"
        if language == "en"
        else "暂无摘要|暂无评估|无建议|无建议"
    )
    assert body == f"Legacy\n\n\n{fallback}|bug|high\n\n\n"


def test_english_custom_template_still_falls_back_to_chinese_template(monkeypatch):
    monkeypatch.setattr(
        "backend.services.issue_service.get_strategy_config",
        lambda: SimpleNamespace(
            get_issue_analysis_config=lambda: {
                "comment_template": "Custom: {summary} / {feasibility}",
                "comment_template_en": "",
            }
        ),
    )
    monkeypatch.setattr(
        "backend.services.issue_service.get_settings",
        lambda: SimpleNamespace(output_language="en"),
    )
    assert (
        object.__new__(IssueService).build_analysis_comment(_analysis())
        == "Custom: No summary / No assessment"
    )
