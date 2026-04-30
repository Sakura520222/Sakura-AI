"""Test structured JSON extraction and AI decision in result_parser.py"""

import json

import pytest

from backend.services.ai_reviewer.constants import (
    JSON_BLOCK_END_MARKER,
    JSON_BLOCK_START_MARKER,
    JSON_SCHEMA_VERSION,
)
from backend.services.ai_reviewer.result_parser import ReviewResultParser


@pytest.fixture
def parser():
    return ReviewResultParser()


def _make_json_block(data: dict) -> str:
    """Helper: wrap JSON data in markers"""
    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    return (
        f"{JSON_BLOCK_START_MARKER}\n```json\n{json_str}\n```\n{JSON_BLOCK_END_MARKER}"
    )


def _valid_json_data(**overrides) -> dict:
    """Helper: create valid JSON review data"""
    data = {
        "schema_version": JSON_SCHEMA_VERSION,
        "overall_score": 7,
        "decision": "approve",
        "decision_reason": "代码质量良好",
        "issues": [
            {
                "severity": "major",
                "file_path": "src/main.py",
                "line_number": 42,
                "title": "命名不规范",
                "description": "变量名 x 不够描述性",
                "suggestion": "使用更具描述性的名称如 user_count",
            }
        ],
        "summary": "代码整体质量良好，有一个命名问题建议修复",
    }
    data.update(overrides)
    return data


# =============================================================================
# _extract_structured_json tests
# =============================================================================


class TestExtractStructuredJson:
    def test_valid_json_block(self, parser):
        data = _valid_json_data()
        text = f"Review text here\n{_make_json_block(data)}"
        result = parser._extract_structured_json(text)
        assert result is not None
        assert result["overall_score"] == 7
        assert result["decision"] == "approve"
        assert len(result["issues"]) == 1

    def test_missing_start_marker(self, parser):
        data = _valid_json_data()
        json_str = json.dumps(data)
        text = f"Review text\n```json\n{json_str}\n```\n{JSON_BLOCK_END_MARKER}"
        result = parser._extract_structured_json(text)
        assert result is None

    def test_missing_end_marker(self, parser):
        data = _valid_json_data()
        json_str = json.dumps(data)
        text = f"{JSON_BLOCK_START_MARKER}\n```json\n{json_str}\n```"
        result = parser._extract_structured_json(text)
        assert result is None

    def test_invalid_json_content(self, parser):
        text = f"{JSON_BLOCK_START_MARKER}\n```json\nnot valid json\n```\n{JSON_BLOCK_END_MARKER}"
        result = parser._extract_structured_json(text)
        assert result is None

    def test_missing_issues_field(self, parser):
        data = {"schema_version": JSON_SCHEMA_VERSION, "overall_score": 7}
        text = _make_json_block(data)
        result = parser._extract_structured_json(text)
        assert result is None

    def test_missing_overall_score_field(self, parser):
        data = {"schema_version": JSON_SCHEMA_VERSION, "issues": []}
        text = _make_json_block(data)
        result = parser._extract_structured_json(text)
        assert result is None

    def test_wrong_schema_version(self, parser):
        data = _valid_json_data(schema_version=99)
        text = _make_json_block(data)
        result = parser._extract_structured_json(text)
        assert result is None

    def test_invalid_severity(self, parser):
        data = _valid_json_data()
        data["issues"][0]["severity"] = "blocker"
        text = _make_json_block(data)
        result = parser._extract_structured_json(text)
        assert result is None

    def test_invalid_decision(self, parser):
        data = _valid_json_data(decision="reject")
        text = _make_json_block(data)
        result = parser._extract_structured_json(text)
        assert result is None

    def test_empty_text(self, parser):
        result = parser._extract_structured_json("")
        assert result is None

    def test_none_text(self, parser):
        result = parser._extract_structured_json(None)
        assert result is None

    def test_json_without_code_fence(self, parser):
        """Markers without ```json fence should still work"""
        data = _valid_json_data()
        json_str = json.dumps(data)
        text = f"{JSON_BLOCK_START_MARKER}\n{json_str}\n{JSON_BLOCK_END_MARKER}"
        result = parser._extract_structured_json(text)
        assert result is not None
        assert result["overall_score"] == 7

    def test_no_decision_is_valid(self, parser):
        """decision field is optional"""
        data = _valid_json_data()
        del data["decision"]
        del data["decision_reason"]
        text = _make_json_block(data)
        result = parser._extract_structured_json(text)
        assert result is not None


# =============================================================================
# _apply_json_result tests
# =============================================================================


class TestApplyJsonResult:
    def test_basic_application(self, parser):
        result = {
            "summary": "",
            "comments": [],
            "inline_comments": [],
            "overall_score": None,
            "issues": {"critical": [], "major": [], "minor": [], "suggestions": []},
        }
        json_data = _valid_json_data()
        success = parser._apply_json_result(result, json_data)
        assert success
        assert result["overall_score"] == 7
        # summary 不再由 _apply_json_result 设置，由 parse_review_result 统一处理
        assert result["ai_decision"] == "approve"
        assert result["ai_decision_reason"] == "代码质量良好"

    def test_inline_comment_with_file_path(self, parser):
        result = {
            "summary": "",
            "comments": [],
            "inline_comments": [],
            "overall_score": None,
            "issues": {"critical": [], "major": [], "minor": [], "suggestions": []},
        }
        json_data = _valid_json_data()
        parser._apply_json_result(result, json_data)
        assert len(result["inline_comments"]) == 1
        assert result["inline_comments"][0]["file_path"] == "src/main.py"
        assert result["inline_comments"][0]["line_number"] == 42
        assert result["inline_comments"][0]["severity"] == "major"

    def test_overall_comment_without_file_path(self, parser):
        result = {
            "summary": "",
            "comments": [],
            "inline_comments": [],
            "overall_score": None,
            "issues": {"critical": [], "major": [], "minor": [], "suggestions": []},
        }
        json_data = _valid_json_data(
            issues=[
                {
                    "severity": "suggestion",
                    "file_path": None,
                    "line_number": None,
                    "title": "建议拆分函数",
                    "description": "该函数过长，建议拆分",
                    "suggestion": None,
                }
            ]
        )
        parser._apply_json_result(result, json_data)
        assert len(result["comments"]) == 1
        assert result["comments"][0]["severity"] == "suggestion"
        assert result["comments"][0]["type"] == "overall"

    def test_severity_grouping(self, parser):
        result = {
            "summary": "",
            "comments": [],
            "inline_comments": [],
            "overall_score": None,
            "issues": {"critical": [], "major": [], "minor": [], "suggestions": []},
        }
        json_data = _valid_json_data(
            issues=[
                {
                    "severity": "critical",
                    "file_path": "a.py",
                    "line_number": 1,
                    "title": "Bug",
                    "description": "desc",
                },
                {
                    "severity": "major",
                    "file_path": "b.py",
                    "line_number": 2,
                    "title": "Quality",
                    "description": "desc",
                },
                {
                    "severity": "minor",
                    "file_path": "c.py",
                    "line_number": 3,
                    "title": "Style",
                    "description": "desc",
                },
                {
                    "severity": "suggestion",
                    "file_path": None,
                    "line_number": None,
                    "title": "Idea",
                    "description": "desc",
                },
            ]
        )
        parser._apply_json_result(result, json_data)
        assert len(result["issues"]["critical"]) == 1
        assert len(result["issues"]["major"]) == 1
        assert len(result["issues"]["minor"]) == 1
        assert len(result["issues"]["suggestions"]) == 1


# =============================================================================
# parse_review_result integration tests
# =============================================================================


class TestParseReviewResult:
    def test_json_priority_over_emoji(self, parser):
        """When both JSON and emoji present, JSON takes priority"""
        json_data = _valid_json_data(overall_score=9, decision="approve")
        review_text = "### 🔴 严重问题\n有 bug\n" + _make_json_block(json_data)
        result = parser.parse_review_result(review_text, "quick")
        assert result["parse_source"] == "json"
        assert result["overall_score"] == 9
        assert result["ai_decision"] == "approve"

    def test_emoji_fallback_when_no_json(self, parser):
        """When no JSON block, fall back to emoji parsing"""
        review_text = "评分：5/10\n\n### 🔴 严重问题\n\n- 有 bug 需要修复\n"
        result = parser.parse_review_result(review_text, "quick")
        assert result["parse_source"] == "emoji"

    def test_emoji_fallback_on_invalid_json(self, parser):
        """When JSON is invalid, fall back to emoji parsing"""
        review_text = (
            "评分：5/10\n\n### 🔴 严重问题\n\n- 有 bug\n"
            f"{JSON_BLOCK_START_MARKER}\n```json\n{{invalid}}\n```\n{JSON_BLOCK_END_MARKER}"
        )
        result = parser.parse_review_result(review_text, "quick")
        assert result["parse_source"] == "emoji"

    def test_result_dict_has_all_fields(self, parser):
        """Result dict should always have all expected fields"""
        result = parser.parse_review_result("simple text", "quick")
        assert "parse_source" in result
        assert "ai_decision" in result
        assert "ai_decision_reason" in result
        assert "issues" in result
        assert "comments" in result
        assert "inline_comments" in result
        assert "overall_score" in result
        assert "summary" in result

    def test_json_with_decision_fields(self, parser):
        """AI decision fields extracted from JSON"""
        json_data = _valid_json_data(
            decision="request_changes",
            decision_reason="存在安全漏洞必须修复",
        )
        review_text = _make_json_block(json_data)
        result = parser.parse_review_result(review_text, "standard")
        assert result["parse_source"] == "json"
        assert result["ai_decision"] == "request_changes"
        assert result["ai_decision_reason"] == "存在安全漏洞必须修复"
