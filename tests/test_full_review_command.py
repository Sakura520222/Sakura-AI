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
