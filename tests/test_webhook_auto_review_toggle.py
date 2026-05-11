"""Webhook automatic review toggle coverage."""

import pytest

from backend.api import webhook
from backend.core.config import get_settings


@pytest.mark.asyncio
async def test_pull_request_event_skips_when_auto_review_disabled():
    settings = get_settings()
    old_value = settings.enable_auto_review
    try:
        settings.enable_auto_review = False
        payload = {
            "action": "opened",
            "pull_request": {
                "id": 1001,
                "number": 1,
                "title": "Test PR",
                "body": "",
                "user": {"login": "alice"},
                "head": {"ref": "feature/test"},
                "base": {"ref": "main"},
                "diff_url": "https://example.invalid/diff",
                "patch_url": "https://example.invalid/patch",
                "html_url": "https://example.invalid/owner/repo/pull/1",
                "state": "open",
                "draft": False,
                "merged": False,
            },
            "repository": {
                "name": "repo",
                "full_name": "owner/repo",
                "owner": {"login": "owner"},
            },
            "installation": {"id": 123},
            "sender": {"login": "alice"},
        }

        response = await webhook.handle_pull_request_event(payload)

        assert response.status_code == 200
        assert response.body == b'{"status":"skipped","reason":"auto review disabled"}'
    finally:
        settings.enable_auto_review = old_value
