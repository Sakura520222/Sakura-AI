"""Untrusted references cannot expand recall; complete hunks preserve source signs."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.models.database import PRIssueLink
from backend.services.issues.candidate_retriever import IssueCandidateRetriever
from tests import test_issue_candidate_foundation as foundation_tests
from tests import test_pr_issue_budget as budget_tests
from tests.test_pr_issue_relations import CANDIDATE, RELATION, repo_with_candidate

foundation = foundation_tests.foundation
budgets = budget_tests.budgets
sync_harness = budget_tests.sync_harness


@pytest.mark.asyncio
@pytest.mark.parametrize("references", ["many", "duplicate", "small"])
async def test_pr_references_never_expand_query_or_rest_hydration(
    foundation, monkeypatch, references
):
    service, collection, repo, values = foundation
    repo.rows = [foundation_tests.issue(i) for i in range(1, 201)]
    await service.index_repo_issues("owner", "repo")
    values["issue_corpus_freshness_seconds"] = 3600
    values["issue_candidate_pool_multiplier"] = 3
    exclusions = {
        "many": [1, *range(1000, 3000)],
        "duplicate": [1] * 2000,
        "small": [1],
    }[references]
    queries, reads = [], []
    query, read = collection.query, repo.get_issue

    def observed_query(**kwargs):
        queries.append(kwargs["n_results"])
        return query(**kwargs)

    def observed_read(number):
        reads.append(number)
        return read(number)

    monkeypatch.setattr(collection, "query", observed_query)
    monkeypatch.setattr(repo, "get_issue", observed_read)
    # Exercise the real PR compatibility caller and strict retrieval pipeline.
    result = await service.search_related_issues(
        "owner", "repo", "PR", "Fixes #1", exclusions, top_k=2
    )
    assert queries == [6]
    assert reads == [2, 3, 4, 5, 6]
    assert [row["number"] for row in result] == [2, 3]
    assert service.reranker_service.rerank.call_args.kwargs["strict"] is True


@pytest.mark.asyncio
async def test_exhausted_bounded_pool_does_not_page_to_refill(foundation, monkeypatch):
    service, collection, repo, values = foundation
    repo.rows = [foundation_tests.issue(i) for i in range(1, 21)]
    await service.index_repo_issues("owner", "repo")
    values["issue_corpus_freshness_seconds"] = 3600
    query = collection.query
    queries = []

    def observed_query(**kwargs):
        queries.append(kwargs["n_results"])
        return query(**kwargs)

    monkeypatch.setattr(collection, "query", observed_query)
    result = await IssueCandidateRetriever(service).retrieve(
        "owner",
        "repo",
        text="PR",
        state="open",
        exclude_numbers=[1, 2, 3],
        top_k=1,
        similarity_threshold=0.8,
    )
    assert result == []
    assert queries == [3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch,additions,deletions,change,quote",
    [
        ("@@ -1 +1 @@\n---old\n+++new", 1, 1, "added", "++new"),
        ("@@ -1 +1 @@\n---old\n+++new", 1, 1, "removed", "--old"),
        (
            "--- a/dependency.py\n+++ b/dependency.py\n@@ -1 +1 @@\n---old\n+++new",
            1,
            1,
            "added",
            "++new",
        ),
        (
            "@@ -1,2 +1,2 @@ section\n context\n---old\n+++new\n\\ No newline at end of file",
            1,
            1,
            "removed",
            "--old",
        ),
        ("@@ -0,0 +1 @@\n+++new\n@@ -10 +11,0 @@\n---old", 1, 1, "added", "++new"),
    ],
)
async def test_sync_and_verifier_keep_literal_signs_inside_complete_hunks(
    sync_harness, patch, additions, deletions, change, quote
):
    service, session, call, _retriever = sync_harness
    file = budget_tests.changed_file(patch)
    file.additions, file.deletions = additions, deletions
    pr = budget_tests.Pull(lambda: iter([file]))
    decision = {
        **RELATION,
        "evidence": [
            {
                "path": "dependency.py",
                "change": change,
                "code_quote": quote,
                "issue_quote": CANDIDATE["body"],
            }
        ],
    }
    call.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=json.dumps({"relations": [decision]}))
            )
        ]
    )
    result = await service.synchronize(repo_with_candidate(pr), "o", "r", 618)
    assert result.succeeded and result.relations[0]["evidence"] == decision["evidence"]
    assert "Closes #612" in pr.body
    assert session.scalar(select(PRIssueLink.issue_number)) == 612


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        "+retry_tls_eof()\n-old",  # Changed text outside a hunk.
        "@@ -1,2 +1,2 @@\n-old\n+retry_tls_eof()",  # Missing context.
        "@@ -1 +1 @@\n context\n-old\n+retry_tls_eof()",  # Counts exhausted.
        "@@ -1 +1 @@ broken\n-old\n+retry_tls_eof()\n@@ malformed",
        "@@ -1 +1 @@\n-old\n+retry_tls_eof()\n+extra",
        "@@ -1,2 +1,2 @@\n-old\n+retry_tls_eof()\n@@ -8,0 +8,0 @@",
        "@@ -1 +1 @@\n-old\n+retry_tls_eof()\n\\ invalid marker",
    ],
)
async def test_malformed_or_truncated_hunks_cannot_replace_verified_links(
    sync_harness, patch
):
    service, session, call, retriever = sync_harness
    pr = budget_tests.Pull(lambda: iter([budget_tests.changed_file(patch)]))
    result = await service.synchronize(repo_with_candidate(pr), "o", "r", 618)
    assert not result.succeeded and result.failure == "snapshot_incomplete"
    budget_tests.assert_old_state(pr, session)
    call.assert_not_awaited()
    retriever.retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_overlapping_hunks_with_matching_metadata_cannot_close(sync_harness):
    service, session, call, _retriever = sync_harness
    file = budget_tests.changed_file(
        "@@ -1 +1 @@\n-old\n+retry_tls_eof()\n@@ -1 +1 @@\n-old\n+retry_tls_eof()"
    )
    file.additions = file.deletions = 2
    pr = budget_tests.Pull(lambda: iter([file]))
    result = await service.synchronize(repo_with_candidate(pr), "o", "r", 618)
    assert not result.succeeded and result.failure == "snapshot_incomplete"
    budget_tests.assert_old_state(pr, session)
    call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch,quote",
    [
        ("@@ -1,2 +1,2 @@\n-old\n+retry_tls_eof()", "retry_tls_eof()"),
        (
            "--- a/dependency.py\n+++ b/dependency.py\n@@ -1 +1 @@\n-old\n+retry_tls_eof()",
            "b/dependency.py",
        ),
    ],
)
async def test_direct_verifier_rejects_incomplete_hunks_and_header_evidence(
    sync_harness, patch, quote
):
    service, _session, call, _retriever = sync_harness
    decision = {
        **RELATION,
        "evidence": [
            {
                **RELATION["evidence"][0],
                "code_quote": quote,
            }
        ],
    }
    call.return_value.choices[0].message.content = json.dumps({"relations": [decision]})
    result = await service.verifier.verify(
        pr_title="PR",
        pr_body="human",
        candidates=[CANDIDATE],
        files=[{"path": "dependency.py", "patch": patch, "complete": True}],
    )
    assert not result.succeeded and result.relations == []
