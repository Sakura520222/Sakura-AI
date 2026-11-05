"""PR evidence must exclude changes inherited by synchronizing the base branch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from github import GithubException

from backend.services.ai_reviewer.pr_summary import PRSummaryService
from backend.services.pr_analyzer import PRAnalyzer


def file_change(path, patch="@@ -1 +1 @@\n-old\n+new", **kwargs):
    return SimpleNamespace(
        filename=path,
        status=kwargs.get("status", "modified"),
        additions=kwargs.get("additions", 1),
        deletions=kwargs.get("deletions", 1),
        changes=kwargs.get("additions", 1) + kwargs.get("deletions", 1),
        patch=patch,
    )


def commit(sha, message):
    return SimpleNamespace(
        sha=sha,
        commit=SimpleNamespace(message=message, author=SimpleNamespace(name="Dev")),
    )


@pytest.fixture
def case(monkeypatch):
    pr_files = [file_change("docker/Dockerfile"), file_change("webui.yml")]
    old_commit = commit("old", "Upgrade Node")
    new_commit = commit("new", "Align WebUI CI")
    pr = MagicMock()
    pr.head.sha = "new"
    pr.base.sha = "base"
    pr.changed_files = len(pr_files)
    pr.get_files.return_value = pr_files
    pr.get_commits.return_value = [old_commit, new_commit]
    comparison = SimpleNamespace(
        status="ahead",
        merge_base_commit=SimpleNamespace(sha="old"),
        total_commits=1,
        files=[pr_files[1]],
        commits=[new_commit],
    )
    repo = MagicMock()
    repo.get_pull.return_value = pr
    repo.compare.return_value = comparison
    analyzer = PRAnalyzer()
    analyzer.github_app = MagicMock()
    analyzer.github_app.get_repo_client.return_value.get_repo.return_value = repo
    config = SimpleNamespace(
        is_code_file=lambda _path: True,
        should_skip_file=lambda _path: False,
        determine_strategy=lambda *_args: "standard",
        config={
            "pr_summary": {
                "user_template": (
                    "{file_count} files +{additions}/-{deletions}\n"
                    "{file_list}\n{commits}"
                )
            }
        },
    )
    monkeypatch.setattr(
        "backend.services.pr_analyzer.get_strategy_config", lambda: config
    )
    monkeypatch.setattr(
        "backend.services.ai_reviewer.pr_summary.get_strategy_config", lambda: config
    )
    monkeypatch.setattr(
        "backend.services.ai_reviewer.pr_summary.get_settings",
        lambda: SimpleNamespace(output_language="en"),
    )
    info = {
        "pr_id": 1,
        "pr_number": 718,
        "repo_owner": "owner",
        "repo_name": "repo",
        "repo_full_name": "owner/repo",
        "title": "Upgrade Node",
        "body": "",
        "action": "synchronize",
        "before": "old",
        "after": "new",
        "head_sha": "new",
    }
    return SimpleNamespace(
        analyzer=analyzer,
        repo=repo,
        pr=pr,
        comparison=comparison,
        info=info,
        config=config,
        pr_files=pr_files,
    )


@pytest.mark.parametrize("pr_number,unrelated_count", [(718, 34), (722, 38)])
def test_base_merge_uses_net_pr_patch_even_in_shared_file(
    case, pr_number, unrelated_count
):
    case.info["pr_number"] = pr_number
    upstream = commit("upstream", "Release version 3.2.5")
    case.comparison.commits.insert(0, upstream)
    case.comparison.total_commits = 2
    # Filename intersection alone would retain this unrelated hunk.
    case.comparison.files = [
        file_change("webui.yml", "@@ -100 +100 @@\n-old CI\n+unrelated base CI"),
        *[file_change(f"upstream/{i}.py") for i in range(unrelated_count)],
    ]

    result = case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)

    assert result.total_files == 2
    assert [f.path for f in result.code_files] == ["docker/Dockerfile", "webui.yml"]
    assert result.code_files[1].patch == case.pr_files[1].patch
    assert 100 not in result.changed_lines_map["webui.yml"]
    assert not result.is_incremental


def test_normal_linear_push_keeps_incremental_patch(case):
    result = case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)
    assert result.is_incremental
    assert [f.path for f in result.code_files] == ["webui.yml"]
    assert result.new_commits[0]["title"] == "Align WebUI CI"


def test_force_push_uses_full_pr_instead_of_merge_base_diff(case):
    case.comparison.status = "diverged"
    case.comparison.merge_base_commit.sha = "ancestor"
    case.comparison.files = [file_change("unrelated.py")]
    result = case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)
    assert not result.is_incremental
    assert [f.path for f in result.code_files] == ["docker/Dockerfile", "webui.yml"]


def test_compare_file_limit_uses_paginated_pr_files(case):
    case.comparison.files = [file_change(f"src/{i}.py") for i in range(300)]
    case.pr.get_files.return_value = [
        *case.comparison.files,
        file_change("src/last.py"),
    ]
    case.pr.changed_files = 301
    result = case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)
    assert result.total_files == 301
    assert result.code_files[-1].path == "src/last.py"


@pytest.mark.parametrize("field", ["head", "base"])
def test_moving_pr_snapshot_is_rejected(case, field):
    def move_ref():
        getattr(case.pr, field).sha = "moved"

    case.pr.update.side_effect = move_ref
    with pytest.raises(RuntimeError, match="changed"):
        case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)


def test_stale_event_head_is_rejected(case):
    case.pr.head.sha = "newer"
    with pytest.raises(RuntimeError, match="head"):
        case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)


def test_incomplete_pr_file_list_is_rejected(case):
    case.pr.changed_files = 3
    with pytest.raises(RuntimeError, match="file"):
        case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)


def test_missing_previous_head_uses_full_pr(case):
    case.repo.compare.side_effect = GithubException(404, {"message": "Not Found"})
    result = case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)
    assert not result.is_incremental
    assert result.total_files == 2


@pytest.mark.parametrize("status", [403, 503])
def test_compare_infrastructure_failure_propagates(case, status):
    error = GithubException(status, {"message": "Unavailable"})
    case.repo.compare.side_effect = error
    with pytest.raises(GithubException) as raised:
        case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)
    assert raised.value is error


def test_reverted_file_is_not_reintroduced_from_incremental_diff(case):
    case.comparison.files = [file_change("reverted.py", status="removed")]
    result = case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)
    assert not result.is_incremental
    assert all(file.path != "reverted.py" for file in result.code_files)


def test_full_review_uses_net_diff_without_compare(case):
    case.info["action"] = "opened"
    result = case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)
    case.repo.compare.assert_not_called()
    assert result.total_files == 2
    assert not result.is_incremental


def test_incremental_deletion_and_rename_keep_current_status_and_patch(case):
    case.pr_files[:] = [
        file_change(
            "deleted.py", "@@ -1 +0,0 @@\n-removed", status="removed", additions=0
        ),
        file_change("renamed.py", status="renamed"),
    ]
    case.comparison.files = case.pr_files[:]
    result = case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)
    assert result.is_incremental
    assert [file.status for file in result.code_files] == ["removed", "renamed"]
    assert [file.patch for file in result.code_files] == [
        file.patch for file in case.pr_files
    ]


@pytest.mark.asyncio
async def test_summary_rebuilds_full_pr_and_drops_old_generated_claims(case):
    analysis = case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)
    client = SimpleNamespace(
        call_with_retry=AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content="Fresh summary"))
                ]
            )
        )
    )
    service = PRSummaryService(client)
    case.info["body"] = service._build_summary_block("STALE: v3.2.5 changed 40 files")
    # Live counters may have moved since analysis; they must not mix into the snapshot.
    case.pr.changed_files = 99
    case.pr.additions = 999
    case.pr.deletions = 999

    await service.generate_summary(analysis, case.info, case.pr)

    messages = client.call_with_retry.await_args.kwargs["messages"]
    evidence = messages[1]["content"]
    assert evidence.startswith("2 files +2/-2")
    assert "docker/Dockerfile" in evidence
    assert "webui.yml" in evidence
    assert "Upgrade Node" in evidence
    assert "Align WebUI CI" in evidence
    assert "STALE" not in evidence
    assert "999" not in evidence


@pytest.mark.asyncio
async def test_summary_counts_non_code_and_filtered_files_consistently(case):
    case.pr_files.append(file_change("uv.lock", additions=5, deletions=3))
    case.pr.changed_files = 3
    case.config.should_skip_file = lambda path: path == "uv.lock"
    case.config.is_code_file = lambda path: path != "uv.lock"
    analysis = case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)
    service = PRSummaryService(None)
    _, evidence = service._build_prompts(analysis, case.info)
    assert evidence.startswith("3 files +7/-5")
    assert "uv.lock (+5/-3)" in evidence


def test_cumulative_summary_keeps_recent_commit_semantics(case):
    analysis = case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)
    analysis.pr_commits = [
        {"sha": f"{i:040x}", "title": f"Older change {i}"} for i in range(25)
    ] + [{"sha": "newest", "title": "Fix the latest retry behavior in webui.yml"}]

    _, evidence = PRSummaryService(None)._build_prompts(analysis, case.info)

    assert "Fix the latest retry behavior in webui.yml" in evidence
    assert "Older change 24" in evidence
    assert "6 earlier commits omitted" in evidence
    assert evidence.count("  - ") == 20


@pytest.mark.parametrize("incomplete", [False, True])
def test_long_pr_uses_paginated_pinned_commit_history(case, incomplete):
    commits = [commit(f"{i:040x}", f"Earlier change {i}") for i in range(250)]
    commits.append(commit("new", "Latest fix after 250 commits"))
    case.pr.commits = len(commits)
    case.pr.get_commits.return_value = commits[:250]

    def compare(base, head):
        assert head == "new"
        if base == "base":
            return SimpleNamespace(commits=commits, total_commits=251 + int(incomplete))
        assert base == "old"
        return case.comparison

    case.repo.compare.side_effect = compare
    if incomplete:
        with pytest.raises(RuntimeError, match="commit comparison is incomplete"):
            case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)
    else:
        analysis = case.analyzer._analyze_pr_sync(case.info, allow_incremental=True)
        _, evidence = PRSummaryService(None)._build_prompts(analysis, case.info)
        assert "Latest fix after 250 commits" in evidence
        assert "231 earlier commits omitted" in evidence


@pytest.mark.asyncio
async def test_baseline_database_failure_propagates_before_github(case, monkeypatch):
    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("backend.models.database.async_session", unavailable)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await case.analyzer.analyze_pr(case.info)
    case.repo.get_pull.assert_not_called()
