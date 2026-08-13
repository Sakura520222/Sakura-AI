"""Static guards for user-visible WebUI instant rendering."""

import re
from pathlib import Path

TEMPLATE_ROOT = Path("backend/webui/templates")

# These fields are known instants, not arbitrary strings.  The allowlist is
# deliberately structural: every field must pass the shared Jinja formatter;
# a future direct interpolation or string slice fails this test.
TIME_FIELDS = (
    "created_at",
    "updated_at",
    "last_used_at",
    "last_security_event_at",
    "last_checked",
    "published_at",
)
TIME_OBJECTS = (
    "event",
    "item",
    "summary",
    "passkey",
    "skill",
    "r",
    "version_info",
    "rel",
)
DIRECT_TIME_INTERPOLATION = re.compile(
    r"\{\{\s*(?:"
    + "|".join(TIME_OBJECTS)
    + r")\."
    + "(?:"
    + "|".join(TIME_FIELDS)
    + r")(?!(?:\s*\|\s*format_datetime(?:_short)?))"
)


def test_sensitive_templates_use_the_shared_datetime_filter():
    expected_filters = {
        "security.html": ("event.created_at|format_datetime_short",),
        "security_user_detail.html": (
            "summary.last_security_event_at|format_datetime_short",
            "passkey.created_at|format_datetime_short",
            "passkey.last_used_at|format_datetime_short",
            "event.created_at|format_datetime_short",
        ),
        "settings.html": (
            "passkey.created_at|format_datetime_short",
            "passkey.last_used_at|format_datetime_short",
        ),
        "version_manager.html": (
            "version_info.last_checked|format_datetime_short",
            "rel.published_at|format_datetime_short",
        ),
        "components/agent_skills_list_fragment.html": (
            "skill.updated_at|format_datetime_short",
        ),
        "components/recent_reviews.html": ("r.created_at|format_datetime_short",),
        "pr_detail.html": (
            "review.created_at|format_datetime_short",
            "review.completed_at|format_datetime_short",
        ),
    }

    for relative_path, markers in expected_filters.items():
        text = (TEMPLATE_ROOT / relative_path).read_text(encoding="utf-8")
        assert not DIRECT_TIME_INTERPOLATION.search(text), relative_path
        for marker in markers:
            assert marker in text, (relative_path, marker)


def test_pr_detail_does_not_reintroduce_preformatted_time_strings():
    text = (TEMPLATE_ROOT / "pr_detail.html").read_text(encoding="utf-8")
    assert "created_at_str" not in text
    assert "completed_at_str" not in text


def test_datetime_filter_accepts_rfc3339_protocol_strings_without_guessing():
    from backend.webui.time_filters import format_datetime_short

    assert format_datetime_short("2026-08-12T12:00:00Z")
    assert format_datetime_short("not-a-time") == "not-a-time"
