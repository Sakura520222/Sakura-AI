"""Tests for label checkbox toggle feature.

Covers:
- LabelService.parse_label_checkboxes
- LabelService.parse_checkbox_changes
- LabelService.is_sakura_label_comment
- LabelService.format_label_results (marker presence)
- Webhook handler routing for issue_comment edited and pull_request_review edited
"""

import pytest

from backend.services.label_service import LabelService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_label_service() -> LabelService:
    """Create a LabelService instance bypassing singleton init."""
    svc = object.__new__(LabelService)
    return svc


# ---------------------------------------------------------------------------
# is_sakura_label_comment
# ---------------------------------------------------------------------------

class TestIsSakuraLabelComment:
    def test_positive(self):
        body = "Some text\n<!-- sakura-label-section -->\n## 🏷️ 标签建议\n"
        assert LabelService.is_sakura_label_comment(body) is True

    def test_negative(self):
        body = "Just a regular comment without any marker"
        assert LabelService.is_sakura_label_comment(body) is False

    def test_empty_body(self):
        assert LabelService.is_sakura_label_comment("") is False


# ---------------------------------------------------------------------------
# parse_label_checkboxes
# ---------------------------------------------------------------------------

class TestParseLabelCheckboxes:
    def test_checked_checkbox(self):
        body = "- [x] **bug** (85%) - fix critical issue"
        result = LabelService.parse_label_checkboxes(body)
        assert result == {"bug": True}

    def test_unchecked_checkbox(self):
        body = "- [ ] **enhancement** (60%) - new feature"
        result = LabelService.parse_label_checkboxes(body)
        assert result == {"enhancement": False}

    def test_mixed_checkboxes(self):
        body = (
            "- [x] **bug** (85%) - fix\n"
            "- [ ] **enhancement** (60%) - new\n"
            "- [x] **documentation** (90%) - docs\n"
        )
        result = LabelService.parse_label_checkboxes(body)
        assert result == {
            "bug": True,
            "enhancement": False,
            "documentation": True,
        }

    def test_no_checkboxes(self):
        body = "Just some regular text\n- not a checkbox\n"
        result = LabelService.parse_label_checkboxes(body)
        assert result == {}

    def test_label_with_special_chars(self):
        body = "- [ ] **good first issue** (50%) - beginner friendly"
        result = LabelService.parse_label_checkboxes(body)
        assert result == {"good first issue": False}

    def test_uppercase_X_checked(self):
        body = "- [X] **test** (80%) - test label"
        result = LabelService.parse_label_checkboxes(body)
        assert result == {"test": True}

    def test_real_world_label_section(self):
        body = (
            "<!-- sakura-label-section -->\n"
            "## 🏷️ 标签建议\n"
            "\n"
            "### ✅ 已自动应用的标签\n"
            "\n"
            "- [x] **bug** (90%) - 修复了关键错误\n"
            "\n"
            "### 💡 建议的标签（需确认）\n"
            "\n"
            "- [ ] **performance** (65%) - 优化了查询性能\n"
            "- [ ] **test** (55%) - 增加了测试覆盖\n"
        )
        result = LabelService.parse_label_checkboxes(body)
        assert result == {
            "bug": True,
            "performance": False,
            "test": False,
        }


# ---------------------------------------------------------------------------
# parse_checkbox_changes
# ---------------------------------------------------------------------------

class TestParseCheckboxChanges:
    def test_check_one_label(self):
        svc = _make_label_service()
        old = "- [ ] **enhancement** (60%) - new feature"
        new = "- [x] **enhancement** (60%) - new feature"
        to_add, to_remove = svc.parse_checkbox_changes(old, new)
        assert to_add == ["enhancement"]
        assert to_remove == []

    def test_uncheck_one_label(self):
        svc = _make_label_service()
        old = "- [x] **bug** (85%) - fix"
        new = "- [ ] **bug** (85%) - fix"
        to_add, to_remove = svc.parse_checkbox_changes(old, new)
        assert to_add == []
        assert to_remove == ["bug"]

    def test_multiple_changes(self):
        svc = _make_label_service()
        old = (
            "- [x] **bug** (85%) - fix\n"
            "- [ ] **enhancement** (60%) - new\n"
            "- [ ] **test** (55%) - tests\n"
        )
        new = (
            "- [ ] **bug** (85%) - fix\n"
            "- [x] **enhancement** (60%) - new\n"
            "- [x] **test** (55%) - tests\n"
        )
        to_add, to_remove = svc.parse_checkbox_changes(old, new)
        assert set(to_add) == {"enhancement", "test"}
        assert to_remove == ["bug"]

    def test_no_changes(self):
        svc = _make_label_service()
        old = "- [x] **bug** (85%) - fix\n- [ ] **test** (55%) - tests\n"
        new = old
        to_add, to_remove = svc.parse_checkbox_changes(old, new)
        assert to_add == []
        assert to_remove == []

    def test_label_added_in_new_body(self):
        """A new checkbox line appears in the new body (wasn't in old)."""
        svc = _make_label_service()
        old = ""
        new = "- [x] **enhancement** (60%) - new\n"
        to_add, to_remove = svc.parse_checkbox_changes(old, new)
        assert to_add == ["enhancement"]
        assert to_remove == []

    def test_label_removed_from_new_body(self):
        """A checkbox line disappears from the new body (was in old as checked)."""
        svc = _make_label_service()
        old = "- [x] **bug** (85%) - fix\n"
        new = ""
        to_add, to_remove = svc.parse_checkbox_changes(old, new)
        # Label was checked and now is gone → treat as uncheck/remove
        assert to_add == []
        assert to_remove == ["bug"]


# ---------------------------------------------------------------------------
# format_label_results (marker presence)
# ---------------------------------------------------------------------------

class TestFormatLabelResults:
    def test_marker_present_in_output(self):
        svc = _make_label_service()
        results = {
            "applied": [{"name": "bug", "confidence": 0.9, "reason": "fix"}],
            "suggested": [],
            "created": [],
            "failed": [],
            "conflict_blocked": [],
        }
        output = svc.format_label_results(results)
        assert "sakura-label-section" in output

    def test_suggested_section_has_interactive_hint(self):
        svc = _make_label_service()
        results = {
            "applied": [],
            "suggested": [
                {"name": "enhancement", "confidence": 0.6, "reason": "new"},
            ],
            "created": [],
            "failed": [],
            "conflict_blocked": [],
        }
        output = svc.format_label_results(results)
        assert "勾选复选框即可应用标签" in output

    def test_applied_labels_have_checked_checkboxes(self):
        svc = _make_label_service()
        results = {
            "applied": [{"name": "bug", "confidence": 0.9, "reason": "fix"}],
            "suggested": [],
            "created": [],
            "failed": [],
            "conflict_blocked": [],
        }
        output = svc.format_label_results(results)
        assert "- [x] **bug**" in output

    def test_suggested_labels_have_unchecked_checkboxes(self):
        svc = _make_label_service()
        results = {
            "applied": [],
            "suggested": [
                {"name": "test", "confidence": 0.5, "reason": "tests"},
            ],
            "created": [],
            "failed": [],
            "conflict_blocked": [],
        }
        output = svc.format_label_results(results)
        assert "- [ ] **test**" in output


# ---------------------------------------------------------------------------
# Webhook handler routing (unit-level)
# ---------------------------------------------------------------------------


class TestWebhookRouting:
    """Test that webhook handlers correctly route issue_comment edited events."""

    @pytest.mark.anyio
    async def test_issue_comment_edited_routes_to_handler(self):
        """Verify that issue_comment edited triggers the checkbox handler."""
        from unittest.mock import AsyncMock, patch

        from fastapi.responses import JSONResponse

        from backend.api.webhook import handle_issue_comment_event

        payload = {
            "action": "edited",
            "issue": {"number": 42, "pull_request": {}, "user": {"login": "author"}},
            "comment": {"body": "new body", "user": {"login": "editor"}},
            "changes": {"body": {"from": "old body"}},
            "repository": {
                "owner": {"login": "owner"},
                "name": "repo",
            },
        }

        with patch(
            "backend.api.webhook.handle_comment_edited_event",
            new_callable=AsyncMock,
        ) as mock_edited:
            mock_edited.return_value = JSONResponse(content={"status": "test_ok"})

            response = await handle_issue_comment_event(payload)
            mock_edited.assert_called_once_with(payload)
            import json

            body = json.loads(response.body.decode())
            assert body["status"] == "test_ok"

    @pytest.mark.anyio
    async def test_issue_comment_created_still_works(self):
        """Verify that issue_comment created still routes to /full-review etc."""
        from backend.api.webhook import handle_issue_comment_event

        payload = {
            "action": "created",
            "comment": {"body": "just a regular comment"},
            "issue": {"number": 42},
        }

        response = await handle_issue_comment_event(payload)
        body = response.body.decode()
        # Should be ignored since it's not a command
        assert "ignored" in body

    @pytest.mark.anyio
    async def test_pull_request_review_edited_routes(self):
        """Verify pull_request_review edited triggers checkbox handler."""
        from unittest.mock import AsyncMock, patch

        from fastapi.responses import JSONResponse

        from backend.api.webhook import handle_pull_request_review_event

        payload = {
            "action": "edited",
            "review": {"body": "new body", "user": {"login": "editor"}},
            "changes": {"body": {"from": "old body"}},
            "pull_request": {
                "number": 42,
                "user": {"login": "author"},
            },
            "repository": {
                "owner": {"login": "owner"},
                "name": "repo",
            },
        }

        with patch(
            "backend.api.webhook._handle_label_checkbox_toggle_inner",
            new_callable=AsyncMock,
        ) as mock_inner:
            mock_inner.return_value = JSONResponse(content={"status": "test_ok"})

            await handle_pull_request_review_event(payload)
            mock_inner.assert_called_once()

    @pytest.mark.anyio
    async def test_pull_request_review_submitted_ignored(self):
        """Non-edited PR review events should be ignored."""
        from backend.api.webhook import handle_pull_request_review_event

        payload = {
            "action": "submitted",
            "review": {"body": "LGTM"},
        }

        response = await handle_pull_request_review_event(payload)
        body = response.body.decode()
        assert "ignored" in body