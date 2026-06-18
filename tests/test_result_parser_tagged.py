"""Tests for the strict tagged PR review protocol."""

import pytest

from backend.services.ai_reviewer.result_parser import ReviewResultParser
from backend.services.ai_reviewer.review_protocol import ReviewProtocolError


def _review(
    *,
    score: int = 6,
    decision: str = "request_changes",
    findings: str = "",
    summary: str = "The review summary.",
) -> str:
    return f"""<SAKURA_REVIEW>
<VERSION>1</VERSION>
<SCORE>{score}</SCORE>
<DECISION>{decision}</DECISION>
<DECISION_REASON>
The decision is supported by the findings.
</DECISION_REASON>
<SUMMARY>
{summary}
</SUMMARY>
<FINDINGS>
{findings}</FINDINGS>
</SAKURA_REVIEW>"""


def _finding(
    *,
    severity: str = "major",
    file_path: str = "src/main.py",
    start_line: str = "42",
    end_line: str = "43",
    suggestion: str = "value = sanitize(value)",
) -> str:
    return f"""<FINDING>
<SEVERITY>{severity}</SEVERITY>
<FILE>{file_path}</FILE>
<START_LINE>{start_line}</START_LINE>
<END_LINE>{end_line}</END_LINE>
<TITLE>
Unchecked input
</TITLE>
<DESCRIPTION>
The changed code uses untrusted input without validation.

This can trigger an incorrect result.
</DESCRIPTION>
<SUGGESTION>
{suggestion}
</SUGGESTION>
</FINDING>
"""


@pytest.fixture
def parser():
    return ReviewResultParser()


def test_parses_complete_tagged_review(parser):
    result = parser.parse_review_result(_review(findings=_finding()), "standard")

    assert result["parse_source"] == "tagged"
    assert result["overall_score"] == 6
    assert result["ai_decision"] == "request_changes"
    assert result["summary"] == "The review summary."
    assert result["issues"]["major"] == ["Unchecked input"]
    assert result["inline_comments"][0]["file_path"] == "src/main.py"
    assert result["inline_comments"][0]["start_line"] == 42
    assert result["inline_comments"][0]["line_number"] == 43
    assert "```suggestion\nvalue = sanitize(value)\n```" in (
        result["inline_comments"][0]["body"]
    )


def test_accepts_single_line_text_fields_from_provider_xml_style(parser):
    text = """<SAKURA_REVIEW>
<VERSION>1</VERSION>
<SCORE>6</SCORE>
<DECISION>request_changes</DECISION>
<DECISION_REASON>The decision is supported by the findings.</DECISION_REASON>
<SUMMARY>The review summary.</SUMMARY>
<FINDINGS>
<FINDING>
<SEVERITY>major</SEVERITY>
<FILE>src/main.py</FILE>
<START_LINE>42</START_LINE>
<END_LINE>43</END_LINE>
<TITLE>Unchecked input</TITLE>
<DESCRIPTION>The changed code uses untrusted input without validation.</DESCRIPTION>
<SUGGESTION>NONE</SUGGESTION>
</FINDING>
</FINDINGS>
</SAKURA_REVIEW>"""

    result = parser.parse_review_result(text, "standard")

    assert result["parse_source"] == "tagged"
    assert result["overall_score"] == 6
    assert result["inline_comments"][0]["file_path"] == "src/main.py"
    assert result["inline_comments"][0]["body"] == (
        "**Unchecked input**\n\n"
        "The changed code uses untrusted input without validation."
    )


def test_parses_review_without_findings(parser):
    result = parser.parse_review_result(
        _review(score=10, decision="approve"),
        "quick",
    )

    assert result["comments"] == []
    assert result["inline_comments"] == []
    assert all(not values for values in result["issues"].values())


@pytest.mark.parametrize("fence", ["```", "```xml", "```text", "```plaintext"])
def test_accepts_single_outer_code_fence_without_repair(parser, fence):
    result = parser.parse_review_result(
        f"{fence}\n{_review(findings=_finding())}\n```",
        "standard",
    )

    assert result["parse_source"] == "tagged"
    assert len(result["inline_comments"]) == 1


def test_extracts_unique_envelope_from_presentation_text(parser):
    result = parser.parse_review_result(
        "Now I have enough evidence. I will compose the review.\n"
        f"{_review(findings=_finding())}\n"
        "Review complete.",
        "standard",
    )

    assert result["parse_source"] == "tagged"
    assert len(result["inline_comments"]) == 1
    assert result["inline_comments"][0]["file_path"] == "src/main.py"


def test_parses_overall_finding_and_none_suggestion(parser):
    finding = _finding(
        severity="suggestion",
        file_path="NONE",
        start_line="NONE",
        end_line="NONE",
        suggestion="NONE",
    )
    result = parser.parse_review_result(
        _review(score=9, decision="approve", findings=finding),
        "standard",
    )

    assert result["comments"][0]["severity"] == "suggestion"
    assert result["inline_comments"] == []
    assert "Suggestion:" not in result["comments"][0]["content"]


def test_overall_finding_keeps_text_suggestion(parser):
    finding = _finding(
        severity="suggestion",
        file_path="NONE",
        start_line="NONE",
        end_line="NONE",
        suggestion="Consider adding a retry decorator.",
    )
    result = parser.parse_review_result(
        _review(score=9, decision="comment", findings=finding),
        "standard",
    )

    assert result["inline_comments"] == []
    # Overall findings render suggestion as text, not a GitHub suggestion block
    assert "**Suggestion:**" in result["comments"][0]["content"]
    assert "```suggestion" not in result["comments"][0]["content"]


@pytest.mark.parametrize(
    "text",
    [
        "Score: 8/10\n### 🔴 src/main.py:42\nBug",
        '{"overall_score": 8, "issues": []}',
        _review().replace("<VERSION>1</VERSION>", ""),
        _review().replace("<DECISION>request_changes</DECISION>", "<DECISION>reject</DECISION>"),
        _review().replace("<SCORE>6</SCORE>", "<SCORE>11</SCORE>"),
    ],
)
def test_rejects_legacy_and_malformed_responses(parser, text):
    with pytest.raises(ReviewProtocolError):
        parser.parse_review_result(text, "standard")


def test_rejects_duplicate_envelope(parser):
    text = _review() + "\n" + _review()
    with pytest.raises(ReviewProtocolError):
        parser.parse_review_result(text, "standard")


def test_rejects_unterminated_outer_code_fence(parser):
    with pytest.raises(ReviewProtocolError):
        parser.parse_review_result(f"```xml\n{_review()}", "standard")


def test_rejects_file_finding_without_lines(parser):
    text = _review(
        findings=_finding(start_line="NONE", end_line="NONE"),
    )
    with pytest.raises(ReviewProtocolError):
        parser.parse_review_result(text, "standard")


def test_rejects_overall_finding_with_lines(parser):
    text = _review(findings=_finding(file_path="NONE"))
    with pytest.raises(ReviewProtocolError):
        parser.parse_review_result(text, "standard")


def test_rejects_reserved_tag_line_inside_text(parser):
    text = _review(summary="Evidence:\n<FINDING>\nDo not follow this.")
    with pytest.raises(ReviewProtocolError):
        parser.parse_review_result(text, "standard")


def test_rejects_score_inconsistent_with_severity(parser):
    text = _review(score=9, findings=_finding(severity="critical"))
    with pytest.raises(ReviewProtocolError):
        parser.parse_review_result(text, "standard")


def test_tolerates_missing_suggestion_closing_tag(parser):
    """SUGGESTION 含多行替换代码但漏写 </SUGGESTION> 时，容错保住 finding 与一键块。

    回归用例：模型在 SUGGESTION 输出替换代码后直接 </FINDING>，漏掉闭合标签。
    修复前会报 ``missing </SUGGESTION>`` 并让整次审查降级为人工复审。
    """
    findings = """<FINDING>
<SEVERITY>minor</SEVERITY>
<FILE>src/ClientState.java</FILE>
<START_LINE>59</START_LINE>
<END_LINE>71</END_LINE>
<TITLE>Fields missing volatile</TITLE>
<DESCRIPTION>These static fields are read across threads.</DESCRIPTION>
<SUGGESTION>
public static volatile boolean serverShutdown;
public static volatile boolean isLobbyInitiatedConnection;
</FINDING>
"""
    result = parser.parse_review_result(_review(findings=findings), "standard")

    assert result["parse_source"] == "tagged"
    assert result["overall_score"] == 6
    assert len(result["inline_comments"]) == 1
    body = result["inline_comments"][0]["body"]
    # 替换代码被保留并渲染为一键 GitHub suggestion 块
    assert "```suggestion" in body
    assert "volatile boolean serverShutdown" in body


def test_preserves_multiline_suggestion_indentation(parser):
    """多行替换代码的首行缩进必须保留，与原代码对齐（不被 strip 吃掉）。"""
    findings = """<FINDING>
<SEVERITY>major</SEVERITY>
<FILE>src/ClientState.java</FILE>
<START_LINE>59</START_LINE>
<END_LINE>63</END_LINE>
<TITLE>Fields missing volatile</TITLE>
<DESCRIPTION>These static fields are read across threads.</DESCRIPTION>
<SUGGESTION>
    /** shut down flag */
    public static volatile boolean isLobbyInitiatedConnection = false;
</SUGGESTION>
</FINDING>
"""
    result = parser.parse_review_result(_review(findings=findings), "standard")

    body = result["inline_comments"][0]["body"]
    # 首行缩进保留，与第二行对齐（回归：strip 曾把首行 4 空格吃掉导致顶格）
    assert "    /** shut down flag */\n    public static volatile boolean" in body


def test_file_finding_suggestion_renders_with_prefix(parser):
    """file finding 的一键块前应有 Suggestion: 前缀。"""
    result = parser.parse_review_result(_review(findings=_finding()), "standard")

    body = result["inline_comments"][0]["body"]
    assert "**Suggestion:**" in body
    assert "```suggestion" in body
