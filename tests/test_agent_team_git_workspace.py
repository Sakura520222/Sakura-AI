from datetime import datetime

import pytest

from backend.services.agent_team.git_workspace_service import AgentTeamGitWorkspaceService


def test_make_branch_name_for_issue(monkeypatch):
    class FixedDatetime:
        @staticmethod
        def utcnow():
            return datetime(2026, 5, 9, 12, 34, 56)

    monkeypatch.setattr("backend.services.agent_team.git_workspace_service.datetime", FixedDatetime)

    service = AgentTeamGitWorkspaceService(github_app=object(), workspace_service=object())

    assert service.make_branch_name(source_issue_number=42) == "sakura-agent/issue-42-20260509-123456"


def test_make_branch_name_for_source(monkeypatch):
    class FixedDatetime:
        @staticmethod
        def utcnow():
            return datetime(2026, 5, 9, 12, 34, 56)

    monkeypatch.setattr("backend.services.agent_team.git_workspace_service.datetime", FixedDatetime)

    service = AgentTeamGitWorkspaceService(github_app=object(), workspace_service=object())

    assert service.make_branch_name(source_id=7) == "sakura-agent/source-7-20260509-123456"


def test_quote_escapes_single_quote():
    service = AgentTeamGitWorkspaceService(github_app=object(), workspace_service=object())

    assert service._quote("feature/test's") == "'feature/test'\"'\"'s'"


def test_get_installation_token_returns_empty_on_error():
    class BrokenGithubApp:
        @property
        def integration(self):
            raise RuntimeError("not configured")

    service = AgentTeamGitWorkspaceService(github_app=BrokenGithubApp(), workspace_service=object())

    assert service._get_installation_token("owner", "repo") == ""


@pytest.mark.asyncio
async def test_get_repo_info_uses_tokenized_clone_url():
    class Repo:
        default_branch = "develop"
        clone_url = "https://github.com/owner/repo.git"

    class RepoClient:
        def get_repo(self, full_name):
            assert full_name == "owner/repo"
            return Repo()

    class GithubApp:
        def get_repo_client(self, owner, repo):
            assert (owner, repo) == ("owner", "repo")
            return RepoClient()

    service = AgentTeamGitWorkspaceService(github_app=GithubApp(), workspace_service=object())
    service._get_installation_token = lambda owner, repo: "token"  # type: ignore[method-assign]

    default_branch, clone_url = await service._get_repo_info("owner", "repo", "owner/repo")

    assert default_branch == "develop"
    assert clone_url == "https://x-access-token:token@github.com/owner/repo.git"
