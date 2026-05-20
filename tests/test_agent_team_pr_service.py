"""Agent 专家团队 PR 服务测试"""

from types import SimpleNamespace

import pytest
from github.GithubException import UnknownObjectException

from backend.services.agent_team.pr_service import AgentTeamPRService, _ApiCommitChange


def test_decode_git_path_handles_rename_and_windows_path():
    from backend.services.agent_team.pr_service import _decode_git_path

    assert _decode_git_path(r"old.py -> backend\\new.py") == "backend/new.py"


@pytest.mark.asyncio
async def test_commit_changes_via_api_creates_remote_branch_from_base_sha():
    service = AgentTeamPRService(workspace_service=object())
    repo = FakeRepo(branch_exists=False)

    sha = await service._commit_changes_via_api(
        repo=repo,
        changes=[_ApiCommitChange(path="main.py", mode="100644", content=b"print(1)\n")],
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
    def __init__(self, branch_exists: bool):
        self.branch_exists = branch_exists
        self.ref = FakeRef("remote-head-sha" if branch_exists else "base-sha", self)
        self.created_ref = None
        self.created_blobs = []
        self.created_commits = []
        self.tree_elements = []
        self.edited_sha = None

    def get_git_ref(self, ref: str):
        assert ref == "heads/sakura-agent/test"
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
        assert base_tree.sha in {"tree-base-sha", "tree-remote-head-sha"}
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
