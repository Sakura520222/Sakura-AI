"""Issue edited 事件不再自动触发 AI 分析的回归测试

/ Regression tests: issue "edited" webhook events must not auto-trigger analysis.
"""

import json
from unittest.mock import AsyncMock

import pytest


def _issue_payload(action: str) -> dict:
    """构造最小可用的 issues webhook payload / minimal issues webhook payload"""
    return {
        "action": action,
        "issue": {
            "number": 1,
            "title": "测试 Issue",
            "body": "内容",
            "state": "open",
            "user": {"login": "Sakura520222"},
            "labels": [],
            "html_url": "https://github.com/Sakura520222/sakura-ai-test/issues/1",
        },
        "repository": {
            "name": "sakura-ai-test",
            "full_name": "Sakura520222/sakura-ai-test",
            "id": 123456,
            "owner": {"login": "Sakura520222"},
            "html_url": "https://github.com/Sakura520222/sakura-ai-test",
        },
        "sender": {"login": "Sakura520222"},
        "installation": {"id": 153684089},
    }


class TestIssueEditedIgnored:
    @pytest.mark.asyncio
    async def test_edited_returns_ignored_and_never_submits(self, monkeypatch):
        """edited 无论 sender 是谁都应被忽略，且绝不提交分析任务 / edited must be ignored for any sender"""
        from backend.api.webhook import handle_issue_event
        from backend.workers import issue_worker

        submit_mock = AsyncMock(return_value="should-not-happen")
        monkeypatch.setattr(issue_worker, "submit_issue_analysis_task", submit_mock)

        for sender in ("Sakura520222", "sakura-ai[bot]"):
            payload = _issue_payload("edited")
            payload["sender"]["login"] = sender
            response = await handle_issue_event(payload)

            assert response.status_code == 200
            body = json.loads(response.body.decode())
            assert body == {"status": "ignored", "action": "edited"}

        submit_mock.assert_not_awaited()
