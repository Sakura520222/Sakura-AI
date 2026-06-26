"""GitHubAppClient CI 失败辅助方法测试。"""

from unittest.mock import MagicMock

from backend.core.github_app import GitHubAppClient


def test_get_pr_number_for_commit_uses_commits_pulls_endpoint(monkeypatch):
    client = GitHubAppClient()
    repo_client = MagicMock()
    repo_client._requester.requestJson.return_value = (
        {},
        [{"number": 42}],
    )
    monkeypatch.setattr(client, "get_repo_client", lambda _owner, _repo: repo_client)

    result = client.get_pr_number_for_commit("owner", "repo", "sha1")

    assert result == 42
    repo_client._requester.requestJson.assert_called_once_with(
        "GET",
        "/repos/owner/repo/commits/sha1/pulls",
    )


def test_get_pr_number_for_commit_returns_none_when_unassociated(monkeypatch):
    client = GitHubAppClient()
    repo_client = MagicMock()
    repo_client._requester.requestJson.return_value = ({}, [])
    monkeypatch.setattr(client, "get_repo_client", lambda _owner, _repo: repo_client)

    assert client.get_pr_number_for_commit("owner", "repo", "sha1") is None
