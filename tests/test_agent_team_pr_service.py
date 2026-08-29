"""Agent 专家团队 PR 服务测试"""

from types import SimpleNamespace

import pytest
from github.GithubException import UnknownObjectException

from backend.services.agent_team.pr_service import AgentTeamPRService, _ApiCommitChange
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


def test_decode_git_path_handles_rename_and_windows_path():
    from backend.services.agent_team.pr_service import _decode_git_path

    assert _decode_git_path(r"old.py -> backend\\new.py") == "backend/new.py"


@pytest.mark.asyncio
async def test_commit_changes_via_api_creates_remote_branch_from_base_sha():
    service = AgentTeamPRService(workspace_service=object())
    repo = FakeRepo(branch_exists=False)

    sha = await service._commit_changes_via_api(
        repo=repo,
        changes=[
            _ApiCommitChange(path="main.py", mode="100644", content=b"print(1)\n")
        ],
        branch_name="sakura-agent/test",
        commit_message="chore: agent update",
        base_sha="base-sha",
    )

    assert sha == "commit-sha"
    assert repo.created_ref == ("refs/heads/sakura-agent/test", "base-sha")
    assert repo.edited_sha == "commit-sha"
    assert repo.created_blobs == [("cHJpbnQoMSkK", "base64")]
    assert repo.created_commits[0]["message"] == "chore: agent update"
    assert repo.created_commits[0]["parents"] == ["base-sha"]
    assert repo.tree_elements[0]._InputGitTreeElement__path == "main.py"
    assert repo.tree_elements[0]._InputGitTreeElement__sha == "blob-sha"


@pytest.mark.asyncio
async def test_commit_changes_via_api_deletes_files_with_null_sha():
    service = AgentTeamPRService(workspace_service=object())
    repo = FakeRepo(branch_exists=True)

    sha = await service._commit_changes_via_api(
        repo=repo,
        changes=[_ApiCommitChange(path="old.py", mode="100644", delete=True)],
        branch_name="sakura-agent/test",
        commit_message="chore: remove file",
        base_sha="base-sha",
    )

    assert sha == "commit-sha"
    assert repo.created_ref is None
    assert repo.tree_elements[0]._InputGitTreeElement__path == "old.py"
    assert repo.tree_elements[0]._InputGitTreeElement__sha is None


@pytest.mark.asyncio
async def test_commit_changes_via_api_continues_original_pr_head():
    service = AgentTeamPRService(workspace_service=object())
    repo = FakeRepo(
        branch_exists=True,
        branch_name="feature/pr",
        remote_sha="pr-head-sha",
    )

    sha = await service._commit_changes_via_api(
        repo=repo,
        changes=[
            _ApiCommitChange(path="main.py", mode="100644", content=b"print(2)\n")
        ],
        branch_name="feature/pr",
        commit_message="fix: apply review feedback",
        base_sha="pr-head-sha",
        expected_head_sha="pr-head-sha",
    )

    assert sha == "commit-sha"
    assert repo.created_ref is None
    assert repo.edited_sha == "commit-sha"
    assert repo.created_commits[0]["parents"] == ["pr-head-sha"]


@pytest.mark.asyncio
async def test_commit_changes_via_api_rejects_changed_original_pr_head():
    service = AgentTeamPRService(workspace_service=object())
    repo = FakeRepo(
        branch_exists=True,
        branch_name="feature/pr",
        remote_sha="new-pr-head-sha",
    )

    with pytest.raises(RuntimeError, match="发生变化"):
        await service._commit_changes_via_api(
            repo=repo,
            changes=[
                _ApiCommitChange(
                    path="main.py", mode="100644", content=b"print(2)\n"
                )
            ],
            branch_name="feature/pr",
            commit_message="fix: apply review feedback",
            base_sha="old-pr-head-sha",
            expected_head_sha="old-pr-head-sha",
        )

    assert repo.created_commits == []
    assert repo.edited_sha is None


class FakeRef:
    def __init__(self, sha: str, repo=None):
        self.object = SimpleNamespace(sha=sha)
        self.repo = repo
        self.edited_sha = None

    def edit(self, sha: str):
        self.edited_sha = sha
        if self.repo is not None:
            self.repo.edited_sha = sha


class FakeRepo:
    def __init__(
        self,
        branch_exists: bool,
        branch_name: str = "sakura-agent/test",
        remote_sha: str | None = None,
    ):
        self.branch_exists = branch_exists
        self.branch_name = branch_name
        self.remote_sha = remote_sha or ("remote-head-sha" if branch_exists else "base-sha")
        self.ref = FakeRef(self.remote_sha, self)
        self.created_ref = None
        self.created_blobs = []
        self.created_commits = []
        self.tree_elements = []
        self.edited_sha = None

    def get_git_ref(self, ref: str):
        assert ref == f"heads/{self.branch_name}"
        if not self.branch_exists:
            raise UnknownObjectException(404, {"message": "Not Found"})
        return self.ref

    def create_git_ref(self, ref: str, sha: str):
        self.created_ref = (ref, sha)
        self.ref = FakeRef(sha, self)
        return self.ref

    def get_git_commit(self, sha: str):
        return SimpleNamespace(sha=sha, tree=SimpleNamespace(sha=f"tree-{sha}"))

    def create_git_blob(self, content: str, encoding: str):
        self.created_blobs.append((content, encoding))
        return SimpleNamespace(sha="blob-sha")

    def create_git_tree(self, tree, base_tree):
        self.tree_elements = tree
        assert base_tree.sha in {
            "tree-base-sha",
            "tree-remote-head-sha",
            "tree-pr-head-sha",
            "tree-new-pr-head-sha",
        }
        return SimpleNamespace(sha="tree-sha")

    def create_git_commit(self, message: str, tree, parents):
        self.created_commits.append(
            {
                "message": message,
                "tree": tree.sha,
                "parents": [parent.sha for parent in parents],
            }
        )
        return SimpleNamespace(sha="commit-sha")


@pytest.mark.asyncio
async def test_commit_and_push_noop_validates_original_pr_remote_head(
    monkeypatch, tmp_path
):
    workspace = str(tmp_path / "workplace" / "alice" / "repo-fork")
    head_sha = "a" * 40
    calls = []

    class FakeRunner:
        def __init__(self, workspace, workspace_service):
            self.workspace = workspace

        async def run_args(self, args):
            stdout = head_sha if args[1:] == ["rev-parse", "HEAD"] else ""
            return SimpleNamespace(stdout=stdout, returncode=0, stderr="")

    class Repo:
        def get_git_ref(self, ref):
            calls.append(ref)
            return SimpleNamespace(object=SimpleNamespace(sha=head_sha))

    class Client:
        def get_repo(self, full_name):
            assert full_name == "alice/repo-fork"
            return Repo()

    class GithubApp:
        def get_repo_client(self, owner, name):
            assert (owner, name) == ("alice", "repo-fork")
            return Client()

    monkeypatch.setattr(
        "backend.services.agent_team.pr_service.GitHubAppClient", GithubApp
    )
    monkeypatch.setattr(
        "backend.services.agent_team.pr_service.TrustedGitRunner", FakeRunner
    )
    service = AgentTeamPRService(workspace_service=AgentTeamWorkspaceService(tmp_path))

    async def no_op_gitignore(executor):
        return None

    monkeypatch.setattr(service, "_ensure_gitignore", no_op_gitignore)

    result = await service.commit_and_push(
        workspace=str(workspace),
        branch_name="sakura-agent/local-task-1",
        target_branch_name="feature/original",
        commit_message="fix: apply review feedback",
        repo_owner="owner",
        repo_name="repo",
        target_repo_owner="alice",
        target_repo_name="repo-fork",
        expected_head_sha=head_sha,
    )

    assert result == head_sha
    assert calls == ["heads/feature/original"]


@pytest.mark.asyncio
async def test_commit_and_push_noop_rejects_advanced_original_pr_remote_head(
    monkeypatch, tmp_path
):
    workspace = str(tmp_path / "workplace" / "alice" / "repo-fork")
    head_sha = "a" * 40
    remote_sha = "f" * 40

    class FakeRunner:
        def __init__(self, workspace, workspace_service):
            self.workspace = workspace

        async def run_args(self, args):
            stdout = head_sha if args[1:] == ["rev-parse", "HEAD"] else ""
            return SimpleNamespace(stdout=stdout, returncode=0, stderr="")

    class Repo:
        def get_git_ref(self, ref):
            assert ref == "heads/feature/original"
            return SimpleNamespace(object=SimpleNamespace(sha=remote_sha))

    class Client:
        def get_repo(self, full_name):
            assert full_name == "alice/repo-fork"
            return Repo()

    class GithubApp:
        def get_repo_client(self, owner, name):
            assert (owner, name) == ("alice", "repo-fork")
            return Client()

    monkeypatch.setattr(
        "backend.services.agent_team.pr_service.GitHubAppClient", GithubApp
    )
    monkeypatch.setattr(
        "backend.services.agent_team.pr_service.TrustedGitRunner", FakeRunner
    )
    service = AgentTeamPRService(workspace_service=AgentTeamWorkspaceService(tmp_path))

    async def no_op_gitignore(executor):
        return None

    monkeypatch.setattr(service, "_ensure_gitignore", no_op_gitignore)

    with pytest.raises(RuntimeError, match="发生变化"):
        await service.commit_and_push(
            workspace=str(workspace),
            branch_name="sakura-agent/local-task-1",
            target_branch_name="feature/original",
            commit_message="fix: apply review feedback",
            repo_owner="owner",
            repo_name="repo",
            target_repo_owner="alice",
            target_repo_name="repo-fork",
            expected_head_sha=head_sha,
        )
