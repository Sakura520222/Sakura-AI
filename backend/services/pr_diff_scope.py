"""Select review evidence without attributing base-branch history to a PR."""

from dataclasses import dataclass
from typing import Any

from github import GithubException
from loguru import logger

# GitHub's compare endpoint returns at most 300 files, even with pagination.
_COMPARE_FILE_LIMIT = 300


@dataclass
class PRDiffScope:
    files: list[Any]
    commits: list[Any]
    pr_files: list[Any]
    pr_commits: list[Any]
    base_sha: str
    head_sha: str
    is_incremental: bool = False


def load_pr_diff_scope(
    repo: Any,
    pr_number: int,
    *,
    before_sha: str | None = None,
    head_sha: str | None = None,
) -> PRDiffScope:
    """Load one checked PR snapshot; callers must run this synchronous I/O off-loop.

    Ordinary linear pushes keep their incremental patches. A base merge, rewrite
    or incomplete compare switches the whole review to the PR's net diff. Merely
    intersecting filenames is unsafe: a shared file can contain upstream hunks.
    """
    pr = repo.get_pull(pr_number)
    snapshot_head = pr.head.sha
    snapshot_base = pr.base.sha
    if head_sha and snapshot_head != head_sha:
        raise RuntimeError("PR head changed before loading review evidence")

    pr_files = list(pr.get_files())
    pr_commits = list(pr.get_commits())
    # The PR commits endpoint is capped at 250. A SHA-pinned base/head
    # comparison supplies the paginated PR-only history for longer branches.
    expected_commits = getattr(pr, "commits", len(pr_commits))
    if isinstance(expected_commits, int) and len(pr_commits) < expected_commits:
        history = repo.compare(snapshot_base, snapshot_head)
        pr_commits = list(history.commits)
        if len(pr_commits) != history.total_commits:
            raise RuntimeError("PR commit comparison is incomplete")
    if len(pr_files) != pr.changed_files:
        raise RuntimeError("PR file list is incomplete or changed during loading")

    scope = PRDiffScope(
        files=pr_files,
        commits=pr_commits,
        pr_files=pr_files,
        pr_commits=pr_commits,
        base_sha=snapshot_base,
        head_sha=snapshot_head,
    )
    if before_sha and before_sha != "0" * 40:
        reason = None
        try:
            comparison = repo.compare(before_sha, snapshot_head)
        except GithubException as exc:
            if exc.status != 404:
                raise
            # Force pushes may make the former head unavailable to the API.
            reason = "previous head unavailable"
        else:
            files = list(comparison.files)
            commits = list(comparison.commits)
            pr_commit_shas = {commit.sha for commit in pr_commits}
            pr_paths = {file.filename for file in pr_files}
            if (
                comparison.status not in {"ahead", "identical"}
                or comparison.merge_base_commit.sha != before_sha
            ):
                reason = "rewritten history"
            elif len(commits) != comparison.total_commits:
                reason = "incomplete compare commits"
            elif any(commit.sha not in pr_commit_shas for commit in commits):
                reason = "commits outside the PR"
            elif len(files) >= _COMPARE_FILE_LIMIT:
                reason = "compare file limit"
            elif any(file.filename not in pr_paths for file in files):
                reason = "files removed from the net PR diff"
            else:
                scope.files = files
                scope.commits = commits
                scope.base_sha = before_sha
                scope.is_incremental = True
        if reason:
            logger.info(
                "PR #{} uses full net diff ({}): {} files, {} PR commits",
                pr_number,
                reason,
                len(pr_files),
                len(pr_commits),
            )

    # The paginated files/commits endpoints are live, unlike the SHA-pinned
    # compare. Reject mixed snapshots instead of publishing mismatched evidence.
    pr.update()
    if (pr.base.sha, pr.head.sha) != (snapshot_base, snapshot_head):
        raise RuntimeError("PR base or head changed while loading review evidence")
    return scope
