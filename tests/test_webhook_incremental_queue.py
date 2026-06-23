import json
from types import SimpleNamespace

import pytest

from backend.api import webhook
from backend.core.config import get_settings


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeUser:
    id = 42
    telegram_id = 4242


class _FakeTelegramService:
    def __init__(self, session):
        self.session = session

    async def get_user_by_github_username(self, github_username):
        return _FakeUser()

    async def check_and_consume_quota(self, github_username, repo_name, pr_number):
        return True, "ok"

    async def get_repo_subscribers(self, repo_full_name):
        return []


class _FakeGitHubAppClient:
    def get_bot_username(self, repo_owner, repo_name):
        return None


def _payload(action="synchronize"):
    return {
        "action": action,
        "before": "base1",
        "after": "head2",
        "pull_request": {
            "id": 1001,
            "number": 7,
            "title": "Test PR",
            "body": "",
            "user": {"login": "alice"},
            "head": {"ref": "feature/test", "sha": "head2"},
            "base": {"ref": "main"},
            "diff_url": "https://example.invalid/diff",
            "patch_url": "https://example.invalid/patch",
            "html_url": "https://example.invalid/owner/repo/pull/7",
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


def _enable_auto_review(settings):
    old_auto_review = settings.enable_auto_review
    old_bot_username = settings.bot_username
    settings.enable_auto_review = True
    settings.bot_username = "sakura-bot[bot]"
    return old_auto_review, old_bot_username


def _patch_common(monkeypatch):
    monkeypatch.setattr(webhook, "get_notification_sender", lambda: None)
    monkeypatch.setattr(webhook, "get_async_session", lambda: _FakeSession())
    monkeypatch.setattr(webhook, "TelegramService", _FakeTelegramService)
    monkeypatch.setattr(webhook, "GitHubAppClient", _FakeGitHubAppClient)


@pytest.mark.asyncio
async def test_synchronize_active_review_queues_incremental_without_submitting(
    monkeypatch,
):
    settings = get_settings()
    old_auto_review, old_bot_username = _enable_auto_review(settings)
    submitted = []
    queued = []
    marked = []

    class _QueueService:
        async def enqueue_from_webhook(self, pr_info, delivery_id=None):
            queued.append((pr_info.copy(), delivery_id))
            return SimpleNamespace(id=1)

    async def fake_submit_review_task(pr_info):
        submitted.append(pr_info)
        return "owner/repo#7"

    async def fake_mark_external_reviewing(pr_info):
        marked.append(pr_info)

    try:
        _patch_common(monkeypatch)
        monkeypatch.setattr(webhook, "submit_review_task", fake_submit_review_task)
        monkeypatch.setattr(
            webhook,
            "_mark_agent_task_external_reviewing",
            fake_mark_external_reviewing,
            raising=False,
        )
        monkeypatch.setattr(
            "backend.services.pr_review_incremental_queue.PRReviewIncrementalQueueService",
            _QueueService,
        )

        response = await webhook.handle_pull_request_event(
            _payload(),
            delivery_id="delivery-1",
        )

        assert response.status_code == 200
        assert json.loads(response.body) == {
            "status": "accepted",
            "action": "queued_incremental",
            "pr": "owner/repo#7",
            "head_sha": "head2",
        }
        assert queued[0][1] == "delivery-1"
        assert submitted == []
        assert marked == []
    finally:
        settings.enable_auto_review = old_auto_review
        settings.bot_username = old_bot_username


@pytest.mark.asyncio
async def test_synchronize_without_active_review_submits_new_task(monkeypatch):
    settings = get_settings()
    old_auto_review, old_bot_username = _enable_auto_review(settings)
    submitted = []
    queued = []

    class _QueueService:
        async def enqueue_from_webhook(self, pr_info, delivery_id=None):
            queued.append((pr_info.copy(), delivery_id))
            return None

    async def fake_submit_review_task(pr_info):
        submitted.append(pr_info.copy())
        return "owner/repo#7"

    async def fake_mark_external_reviewing(pr_info):
        return None

    try:
        _patch_common(monkeypatch)
        monkeypatch.setattr(webhook, "submit_review_task", fake_submit_review_task)
        monkeypatch.setattr(
            webhook,
            "_mark_agent_task_external_reviewing",
            fake_mark_external_reviewing,
            raising=False,
        )
        monkeypatch.setattr(
            "backend.services.pr_review_incremental_queue.PRReviewIncrementalQueueService",
            _QueueService,
        )

        response = await webhook.handle_pull_request_event(_payload())

        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["status"] == "accepted"
        assert body["action"] == "synchronize"
        assert len(queued) == 1
        assert len(submitted) == 1
        assert submitted[0]["head_sha"] == "head2"
    finally:
        settings.enable_auto_review = old_auto_review
        settings.bot_username = old_bot_username
