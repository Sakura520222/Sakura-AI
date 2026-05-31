"""Webhook automatic review toggle coverage."""

import json

import pytest

from backend.api import webhook
from backend.core.config import get_settings


class _FakeWorker:
    def __init__(self):
        self.registered = []

    def _register_task(self, task_key, force_new=False):
        self.registered.append((task_key, force_new))


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


def _agent_pr_payload(action="opened", draft=False, head_sha="sha-test"):
    return {
        "action": action,
        "pull_request": {
            "id": 1001,
            "number": 1,
            "title": "Agent PR",
            "body": "",
            "user": {"login": "sakura-bot[bot]"},
            "head": {"ref": "sakura-agent/issue-1", "sha": head_sha},
            "base": {"ref": "develop"},
            "diff_url": "https://example.invalid/diff",
            "patch_url": "https://example.invalid/patch",
            "html_url": "https://example.invalid/owner/repo/pull/1",
            "state": "open",
            "draft": draft,
            "merged": False,
        },
        "repository": {
            "name": "repo",
            "full_name": "owner/repo",
            "owner": {"login": "owner"},
        },
        "installation": {"id": 123},
        "sender": {"login": "sakura-bot[bot]"},
    }


def _enable_auto_review_for_agent_pr(settings):
    old_auto_review = settings.enable_auto_review
    old_bot_username = settings.bot_username
    settings.enable_auto_review = True
    settings.bot_username = "sakura-bot[bot]"
    return old_auto_review, old_bot_username


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


@pytest.mark.asyncio
async def test_agent_draft_opened_is_skipped_without_review(monkeypatch):
    settings = get_settings()
    old_auto_review, old_bot_username = _enable_auto_review_for_agent_pr(settings)
    submitted = []
    fake_worker = _FakeWorker()

    async def fake_submit_review_task(pr_info):
        submitted.append(pr_info)
        return "owner/repo#1"

    try:
        monkeypatch.setattr("backend.workers.review_worker.get_worker", lambda: fake_worker)
        monkeypatch.setattr(webhook, "submit_review_task", fake_submit_review_task)

        response = await webhook.handle_pull_request_event(
            _agent_pr_payload(action="opened", draft=True)
        )

        assert response.status_code == 200
        assert json.loads(response.body) == {"status": "skipped", "reason": "draft PR"}
        assert submitted == []
    finally:
        settings.enable_auto_review = old_auto_review
        settings.bot_username = old_bot_username


@pytest.mark.asyncio
async def test_agent_ready_for_review_auto_submits_review_and_marks_external_reviewing(
    monkeypatch,
):
    settings = get_settings()
    old_auto_review, old_bot_username = _enable_auto_review_for_agent_pr(settings)
    submitted = []
    marked = []
    fake_worker = _FakeWorker()

    async def fake_submit_review_task(pr_info):
        submitted.append(pr_info.copy())
        return "owner/repo#1"

    async def fake_mark_external_reviewing(pr_info):
        marked.append(pr_info.copy())

    try:
        monkeypatch.setattr("backend.workers.review_worker.get_worker", lambda: fake_worker)
        monkeypatch.setattr(webhook, "get_notification_sender", lambda: None)
        monkeypatch.setattr(webhook, "get_async_session", lambda: _FakeSession())
        monkeypatch.setattr(webhook, "TelegramService", _FakeTelegramService)
        monkeypatch.setattr(webhook, "submit_review_task", fake_submit_review_task)
        monkeypatch.setattr(
            webhook,
            "_mark_agent_task_external_reviewing",
            fake_mark_external_reviewing,
            raising=False,
        )

        response = await webhook.handle_pull_request_event(
            _agent_pr_payload(
                action="ready_for_review",
                draft=False,
                head_sha="sha-ready",
            )
        )

        assert response.status_code == 200
        assert json.loads(response.body)["status"] == "accepted"
        assert len(submitted) == 1
        assert submitted[0]["head_sha"] == "sha-ready"
        assert len(marked) == 1
        assert marked[0]["head_sha"] == "sha-ready"
    finally:
        settings.enable_auto_review = old_auto_review
        settings.bot_username = old_bot_username


@pytest.mark.asyncio
async def test_closed_action_cancels_active_review_task(monkeypatch):
    cancelled_keys = []

    class _FakeCancelWorker:
        def cancel_task(self, task_key):
            cancelled_keys.append(task_key)
            return True

    monkeypatch.setattr(
        'backend.workers.review_worker.get_worker', lambda: _FakeCancelWorker()
    )

    payload = {
        'action': 'closed',
        'repository': {
            'name': 'repo',
            'full_name': 'owner/repo',
            'owner': {'login': 'owner'},
        },
        'installation': {'id': 123},
        'sender': {'login': 'alice'},
        'pull_request': {
            'id': 999,
            'number': 7,
            'user': {'login': 'alice'},
            'title': 'Fix bug',
            'body': '',
            'head': {'ref': 'feature/fix', 'sha': 'abc123'},
            'base': {'ref': 'develop'},
            'diff_url': '',
            'patch_url': '',
            'html_url': '',
            'state': 'closed',
            'draft': False,
            'merged': False,
        },
    }

    response = await webhook.handle_pull_request_event(payload)
    body = json.loads(response.body)
    assert body['status'] == 'accepted'
    assert body['action'] == 'cancelled'
    assert len(cancelled_keys) == 1
    assert cancelled_keys[0] == 'owner/repo#7'
