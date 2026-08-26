import pytest

from backend.services.agent_team.git_workspace_service import (
    AgentTeamGitWorkspaceService,
    _strip_git_credentials,
)


def test_make_branch_name_for_issue():
    service = AgentTeamGitWorkspaceService(
        github_app=object(), workspace_service=object()
    )

    assert (
        service.make_branch_name(task_id=123, source_issue_number=42)
        == "sakura-agent/task-123-issue-42"
    )


def test_make_branch_name_for_pr():
    service = AgentTeamGitWorkspaceService(
        github_app=object(), workspace_service=object()
    )

    # PR_REVIEW 类型使用 pr- 前缀
    assert (
        service.make_branch_name(
            task_id=62, source_issue_number=384, source_type="pr_review"
        )
        == "sakura-agent/task-62-pr-384"
    )


def test_make_branch_name_for_source():
    service = AgentTeamGitWorkspaceService(
        github_app=object(), workspace_service=object()
    )

    assert (
        service.make_branch_name(task_id=123, source_id=7)
        == "sakura-agent/task-123-source-7"
    )


def test_make_branch_name_uses_task_id_to_avoid_same_second_collision():
    service = AgentTeamGitWorkspaceService(
        github_app=object(), workspace_service=object()
    )

    assert service.make_branch_name(
        task_id=1, source_issue_number=42
    ) != service.make_branch_name(
        task_id=2,
        source_issue_number=42,
    )


def test_quote_escapes_single_quote():
    service = AgentTeamGitWorkspaceService(
        github_app=object(), workspace_service=object()
    )

    assert service._quote("feature/test's") == "'feature/test'\"'\"'s'"


def test_get_installation_token_returns_empty_on_error():
    class BrokenGithubApp:
        @property
        def integration(self):
            raise RuntimeError("not configured")

    service = AgentTeamGitWorkspaceService(
        github_app=BrokenGithubApp(), workspace_service=object()
    )

    assert service._get_installation_token("owner", "repo") == ""


def test_strip_git_credentials_from_remote_url():
    assert (
        _strip_git_credentials(
            "https://x-access-token:secret@github.com/owner/repo.git"
        )
        == "https://github.com/owner/repo.git"
    )
    assert (
        _strip_git_credentials("https://user@github.com/owner/repo.git")
        == "https://github.com/owner/repo.git"
    )
    assert _strip_git_credentials("ssh://git@github.com/owner/repo.git") == (
        "ssh://git@github.com/owner/repo.git"
    )
    with pytest.raises(ValueError, match="query|fragment"):
        _strip_git_credentials("https://user@github.com/owner/repo.git?token=secret")
    with pytest.raises(ValueError, match="query|fragment"):
        _strip_git_credentials("https://github.com/owner/repo.git#fragment")
    assert _strip_git_credentials("user:secret@github.com:owner/repo.git") == (
        "github.com:owner/repo.git"
    )


@pytest.mark.asyncio
async def test_get_repo_info_keeps_token_out_of_clone_url():
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

    service = AgentTeamGitWorkspaceService(
        github_app=GithubApp(), workspace_service=object()
    )
    default_branch, clone_url = await service._get_repo_info(
        "owner", "repo", "owner/repo"
    )

    assert default_branch == "develop"
    assert clone_url == "https://github.com/owner/repo.git"
    assert "token" not in clone_url
