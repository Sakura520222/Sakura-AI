"""Unit tests for /full-review command in webhook handler."""

import json

import pytest

from backend.api import webhook
from backend.core.github_app import GitHubAppClient


class _CommentCapturingClient:
    def __init__(self, comments):
        self._comments = comments

    def get_repo(self, _full_name):
        return self

    def get_issue(self, _number):
        return self

    def create_comment(self, body):
        self._comments.append(body)


class _FakeGitHubApp:
    def __init__(self, permission):
        self._permission = permission
        self.comments = []
        self._client = _CommentCapturingClient(self.comments)

    def check_collaborator_permission(self, *args, **kwargs):
        return self._permission

    def get_repo_client(self, *args, **kwargs):
        return self._client


def _full_review_payload(body="/full-review"):
    return {
        "action": "created",
        "comment": {
            "body": body,
            "user": {"login": "Sakura520222"},
        },
        "issue": {
            "number": 2,
            "user": {"login": "sakura-ai-agent"},
            "pull_request": {"url": "https://github.com/Sakura520222/repo/pull/2"},
        },
        "repository": {
            "name": "repo",
            "full_name": "Sakura520222/repo",
            "owner": {"login": "Sakura520222"},
        },
        "installation": {"id": 123},
    }


@pytest.mark.asyncio
async def test_full_review_permission_unknown_returns_retryable_message(monkeypatch):
    fake_app = _FakeGitHubApp(permission="unknown")
    monkeypatch.setattr(webhook, "GitHubAppClient", lambda: fake_app)

    response = await webhook.handle_issue_comment_event(_full_review_payload())
    body = json.loads(response.body.decode())

    assert response.status_code == 503
    assert body["status"] == "error"
    assert body["reason"] == "permission check unavailable"
    assert fake_app.comments == [
        "⚠️ @Sakura520222，暂时无法连接 GitHub 校验权限，请稍后重试。"
    ]


@pytest.mark.asyncio
async def test_revoke_permission_unknown_returns_retryable_message(monkeypatch):
    fake_app = _FakeGitHubApp(permission="unknown")
    monkeypatch.setattr(webhook, "GitHubAppClient", lambda: fake_app)

    response = await webhook.handle_issue_comment_event(_full_review_payload("/revoke"))
    body = json.loads(response.body.decode())

    assert response.status_code == 503
    assert body["status"] == "error"
    assert body["reason"] == "permission check unavailable"
    assert fake_app.comments == [
        "⚠️ @Sakura520222，暂时无法连接 GitHub 校验权限，请稍后重试。"
    ]


@pytest.mark.asyncio
async def test_full_review_forwards_repository_identity_to_review_worker(monkeypatch):
    class FakePullRequest:
        id = 1234
        number = 2
        user = type("User", (), {"login": "alice"})()
        title = "PR"
        body = ""
        head = type("Head", (), {"ref": "feature", "sha": "head"})()
        base = type("Base", (), {"ref": "main"})()
        diff_url = "https://github.com/owner/repo.diff"
        patch_url = "https://github.com/owner/repo.patch"
        html_url = "https://github.com/owner/repo/pull/2"
        state = "open"
        draft = False
        merged = False

    class FakeRepo:
        id = 9876
        html_url = "https://github.com/owner/repo"

        def get_pull(self, _number):
            return FakePullRequest()

    class FakeClient:
        def get_repo(self, _full_name):
            return FakeRepo()

    class FakeGitHubApp:
        def check_collaborator_permission(self, *_args, **_kwargs):
            return "admin"

        def get_repo_client(self, *_args, **_kwargs):
            return FakeClient()

        def get_bot_username(self, *_args, **_kwargs):
            return None

        def delete_all_bot_comments(self, *_args, **_kwargs):
            return {"issue_comments": 0, "review_comments": 0}

        def dismiss_bot_reviews(self, *_args, **_kwargs):
            return 0

    class FakeResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class FakeSession:
        async def execute(self, _statement):
            return FakeResult()

        async def commit(self):
            return None

        async def delete(self, _value):
            return None

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, *_args):
            return False

    class FakeTelegramService:
        def __init__(self, _session):
            pass

        async def get_user_by_github_username(self, _username):
            return None

    captured = {}

    async def capture_task(pr_info):
        captured.update(pr_info)
        return "owner/repo#2"

    monkeypatch.setattr(webhook, "GitHubAppClient", FakeGitHubApp)
    monkeypatch.setattr(webhook, "get_async_session", lambda: SessionContext())
    monkeypatch.setattr(webhook, "TelegramService", FakeTelegramService)
    monkeypatch.setattr(webhook, "get_notification_sender", lambda: None)
    monkeypatch.setattr(webhook, "submit_review_task", capture_task)

    response = await webhook.handle_issue_comment_event(_full_review_payload())

    assert response.status_code == 200
    assert captured["repository_external_id"] == 9876
    assert captured["source_system_instance"] == "github.com"


def test_collaborator_permission_is_unknown_when_repo_client_unavailable(monkeypatch):
    github_app = GitHubAppClient()
    monkeypatch.setattr(github_app, "get_repo_client", lambda *_args, **_kwargs: None)

    permission = github_app.check_collaborator_permission("owner", "repo", "alice")

    assert permission == "unknown"


def test_collaborator_permission_is_unknown_when_github_permission_api_fails(
    monkeypatch,
):
    class _FailingClient:
        def get_repo(self, _full_name):
            raise RuntimeError("GitHub timeout")

    github_app = GitHubAppClient()
    monkeypatch.setattr(
        github_app,
        "get_repo_client",
        lambda *_args, **_kwargs: _FailingClient(),
    )

    permission = github_app.check_collaborator_permission("owner", "repo", "alice")

    assert permission == "unknown"
