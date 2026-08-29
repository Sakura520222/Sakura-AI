"""Webhook automatic review toggle coverage."""

import json
from types import SimpleNamespace

import pytest

from backend.api import webhook
from backend.core.config import get_settings
from backend.models.agent_team_models import AgentTeamTaskStatus


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
        monkeypatch.setattr(
            "backend.workers.review_worker.get_worker", lambda: fake_worker
        )
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
        monkeypatch.setattr(
            "backend.workers.review_worker.get_worker", lambda: fake_worker
        )
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
async def test_original_pr_head_agent_sync_is_not_filtered_as_bot_pr(monkeypatch):
    """原 PR head 上的 Agent 提交也必须进入自动审查流程。"""
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

    async def fake_is_original_pr_agent_task(pr_info):
        return pr_info["branch"] == "feature/original"

    class _FakeGithubApp:
        def get_bot_username(self, repo_owner, repo_name):
            return None

    class _QueueService:
        async def enqueue_from_webhook(self, pr_info, delivery_id=None):
            return None

    try:
        payload = _agent_pr_payload(
            action="synchronize",
            draft=False,
            head_sha="sha-original-head",
        )
        payload["pull_request"]["user"] = {"login": "alice"}
        payload["pull_request"]["head"]["ref"] = "feature/original"

        monkeypatch.setattr(
            "backend.workers.review_worker.get_worker", lambda: fake_worker
        )
        monkeypatch.setattr(webhook, "get_notification_sender", lambda: None)
        monkeypatch.setattr(webhook, "get_async_session", lambda: _FakeSession())
        monkeypatch.setattr(webhook, "TelegramService", _FakeTelegramService)
        monkeypatch.setattr(webhook, "GitHubAppClient", _FakeGithubApp)
        monkeypatch.setattr(
            "backend.services.pr_review_incremental_queue.PRReviewIncrementalQueueService",
            _QueueService,
        )
        monkeypatch.setattr(webhook, "submit_review_task", fake_submit_review_task)
        monkeypatch.setattr(
            webhook,
            "_mark_agent_task_external_reviewing",
            fake_mark_external_reviewing,
            raising=False,
        )
        monkeypatch.setattr(
            webhook,
            "_is_original_pr_agent_task",
            fake_is_original_pr_agent_task,
            raising=False,
        )

        response = await webhook.handle_pull_request_event(payload)

        assert response.status_code == 200
        assert json.loads(response.body)["status"] == "accepted"
        assert len(submitted) == 1
        assert submitted[0]["branch"] == "feature/original"
        assert len(marked) == 1
    finally:
        settings.enable_auto_review = old_auto_review
        settings.bot_username = old_bot_username


class _AgentTaskResult:
    def __init__(self, tasks):
        self.tasks = list(tasks)

    def scalars(self):
        return self

    def all(self):
        return self.tasks

    def first(self):
        return self.tasks[0] if self.tasks else None


class _AgentTaskSession:
    def __init__(self, tasks):
        self.tasks = tasks
        self.statement = None
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement):
        self.statement = statement
        return _AgentTaskResult(self.tasks)

    async def commit(self):
        self.committed = True


def _direct_pr_task(status, head_sha):
    return SimpleNamespace(
        id=1,
        status=status,
        pr_head_sha=head_sha,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "incoming_sha", "expected"),
    [
        (AgentTeamTaskStatus.EXTERNAL_REVIEWING.value, "human-sha", True),
        (AgentTeamTaskStatus.COMPLETED.value, "agent-sha", True),
        (AgentTeamTaskStatus.COMPLETED.value, "human-sha", False),
    ],
)
async def test_original_pr_agent_task_matches_completed_head_sha_only(
    monkeypatch, status, incoming_sha, expected
):
    session = _AgentTaskSession([_direct_pr_task(status, "agent-sha")])
    monkeypatch.setattr(webhook, "get_async_session", lambda: session)

    result = await webhook._is_original_pr_agent_task(
        {
            "repo_owner": "owner",
            "repo_name": "repo",
            "pr_number": 42,
            "branch": "feature/original",
            "head_sha": incoming_sha,
        }
    )

    assert result is expected
    assert session.statement is not None
    assert "pr_head_branch" in str(session.statement)


@pytest.mark.asyncio
async def test_mark_external_reviewing_limits_match_to_active_task(monkeypatch):
    task = SimpleNamespace(
        id=2,
        status=AgentTeamTaskStatus.PR_OPENED.value,
        current_phase="pr_opened",
        pr_head_sha="old-sha",
    )
    session = _AgentTaskSession([task])
    monkeypatch.setattr(webhook, "get_async_session", lambda: session)

    await webhook._mark_agent_task_external_reviewing(
        {
            "repo_owner": "owner",
            "repo_name": "repo",
            "pr_number": 42,
            "branch": "feature/original",
            "head_sha": "new-sha",
        }
    )

    assert task.status == AgentTeamTaskStatus.EXTERNAL_REVIEWING.value
    assert task.current_phase == "external_reviewing"
    assert task.pr_head_sha == "new-sha"
    assert session.committed is True
    statement = str(session.statement)
    assert "LIMIT" in statement.upper()
    assert "pr_head_branch" in statement


@pytest.mark.asyncio
async def test_mark_external_reviewing_refreshes_waiting_direct_task_head(monkeypatch):
    task = SimpleNamespace(
        id=3,
        status=AgentTeamTaskStatus.WAITING_HUMAN.value,
        current_phase="waiting_human",
        pr_head_sha="old-sha",
    )
    session = _AgentTaskSession([task])
    monkeypatch.setattr(webhook, "get_async_session", lambda: session)

    await webhook._mark_agent_task_external_reviewing(
        {
            "repo_owner": "owner",
            "repo_name": "repo",
            "pr_number": 42,
            "branch": "feature/original",
            "head_sha": "human-sha",
        }
    )

    assert task.status == AgentTeamTaskStatus.WAITING_HUMAN.value
    assert task.current_phase == "waiting_human"
    assert task.pr_head_sha == "human-sha"


@pytest.mark.asyncio
async def test_closed_action_cancels_active_review_task(monkeypatch):
    cancelled_keys = []

    class _FakeCancelWorker:
        def cancel_task(self, task_key):
            cancelled_keys.append(task_key)
            return True

    monkeypatch.setattr(
        "backend.workers.review_worker.get_worker", lambda: _FakeCancelWorker()
    )

    payload = {
        "action": "closed",
        "repository": {
            "name": "repo",
            "full_name": "owner/repo",
            "owner": {"login": "owner"},
        },
        "installation": {"id": 123},
        "sender": {"login": "alice"},
        "pull_request": {
            "id": 999,
            "number": 7,
            "user": {"login": "alice"},
            "title": "Fix bug",
            "body": "",
            "head": {"ref": "feature/fix", "sha": "abc123"},
            "base": {"ref": "develop"},
            "diff_url": "",
            "patch_url": "",
            "html_url": "",
            "state": "closed",
            "draft": False,
            "merged": False,
        },
    }

    response = await webhook.handle_pull_request_event(payload)
    body = json.loads(response.body)
    assert body["status"] == "accepted"
    assert body["action"] == "cancelled"
    assert len(cancelled_keys) == 1
    assert cancelled_keys[0] == "owner/repo#7"
