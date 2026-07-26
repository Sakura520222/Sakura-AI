"""GitHubAppClient CI 失败辅助方法测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import backend.core.github_app as github_app_module
from backend.core.github_app import GitHubAppClient, get_pr_info_from_url
from backend.services.activity_observability.integration_service import (
    ActivityIntegrationService,
)


def test_get_pr_number_for_commit_uses_commits_pulls_endpoint(monkeypatch):
    client = GitHubAppClient()
    repo_client = MagicMock()
    repo_client._requester.requestJsonAndCheck.return_value = (
        {},
        [{"number": 42}],
    )
    monkeypatch.setattr(client, "get_repo_client", lambda _owner, _repo: repo_client)

    result = client.get_pr_number_for_commit("owner", "repo", "sha1")

    assert result == 42
    repo_client._requester.requestJsonAndCheck.assert_called_once_with(
        "GET",
        "/repos/owner/repo/commits/sha1/pulls",
    )


def test_get_pr_number_for_commit_returns_none_when_unassociated(monkeypatch):
    client = GitHubAppClient()
    repo_client = MagicMock()
    repo_client._requester.requestJsonAndCheck.return_value = ({}, [])
    monkeypatch.setattr(client, "get_repo_client", lambda _owner, _repo: repo_client)

    assert client.get_pr_number_for_commit("owner", "repo", "sha1") is None


def test_publication_review_sender_can_propagate_transport_error(monkeypatch):
    client = GitHubAppClient()
    pull = MagicMock()
    pull.create_review.side_effect = TimeoutError("transport timeout")
    repo = MagicMock()
    repo.get_pull.return_value = pull
    repo_client = MagicMock()
    repo_client.get_repo.return_value = repo
    monkeypatch.setattr(client, "get_repo_client", lambda _owner, _repo: repo_client)

    with pytest.raises(TimeoutError, match="transport timeout"):
        client.submit_review_with_inline_comments(
            "owner",
            "repo",
            42,
            "COMMENT",
            "body",
            enable_idempotency_check=False,
            raise_on_error=True,
        )


def test_publication_issue_sender_can_propagate_transport_error(monkeypatch):
    client = GitHubAppClient()
    issue = MagicMock()
    issue.create_comment.side_effect = ConnectionError("connection reset")
    repo = MagicMock()
    repo.get_issue.return_value = issue
    repo_client = MagicMock()
    repo_client.get_repo.return_value = repo
    monkeypatch.setattr(client, "get_repo_client", lambda _owner, _repo: repo_client)

    with pytest.raises(ConnectionError, match="connection reset"):
        client.create_issue_comment(
            "owner",
            "repo",
            7,
            "body",
            raise_on_error=True,
        )


@pytest.mark.asyncio
async def test_manual_pr_info_contains_immutable_identity_for_admission(monkeypatch):
    class FakePullRequest:
        id = 1234
        number = 42
        user = SimpleNamespace(login="alice")
        title = "PR"
        body = ""
        head = SimpleNamespace(ref="feature", sha="head")
        base = SimpleNamespace(ref="main")
        diff_url = "https://github.com/owner/repo.diff"
        patch_url = "https://github.com/owner/repo.patch"
        html_url = "https://github.com/owner/repo/pull/42"
        state = "open"
        draft = False
        merged = False

    class FakeRepo:
        id = 9876
        html_url = "https://github.com/owner/repo"

        def get_pull(self, _number):
            return FakePullRequest()

    class FakeRepoClient:
        def get_repo(self, _full_name):
            return FakeRepo()

    class FakeGitHubApp:
        integration = SimpleNamespace(
            get_installation=lambda **_kwargs: SimpleNamespace(id=77)
        )

        def get_repo_client(self, _owner, _repo):
            return FakeRepoClient()

    monkeypatch.setattr(github_app_module, "GitHubAppClient", FakeGitHubApp)

    pr_info = await get_pr_info_from_url(
        "https://github.com/owner/repo/pull/42"
    )

    assert pr_info["repository_external_id"] == 9876
    assert pr_info["source_system_instance"] == "github.com"
    ActivityIntegrationService.normalize_resource(pr_info, resource_type="pr")
