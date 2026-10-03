"""Shared retrieval controls against real corpus and PR consumers."""

import asyncio

import pytest

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.services.issues.candidate_retriever import IssueCandidateRetriever
from backend.services.issues.relation_runtime import RelationDeadlineExceeded
from tests import test_issue_candidate_foundation, test_pr_candidate_freshness

foundation = test_issue_candidate_foundation.foundation
freshness = test_pr_candidate_freshness.freshness
budgets = test_pr_candidate_freshness.budgets


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["source_init", "source_next", "embedding", "store"])
@pytest.mark.parametrize("signal", ["domain", "deadline", "none"])
async def test_real_retriever_failure_prioritizes_pending_control(
    foundation, stage, signal
):
    service, collection, repo, _ = foundation
    event = asyncio.Event()
    deadline = test_issue_candidate_foundation.MutableDeadline()
    failure = RuntimeError("external operation failed")
    old_metadata = dict(collection.metadata)

    def fail(*args, **kwargs):
        if signal == "domain":
            event.set()
        elif signal == "deadline":
            deadline.expired = True
        raise failure

    if stage == "source_init":
        repo.get_issues = fail
    elif stage == "source_next":

        def source(**kwargs):
            yield from ()
            fail()

        repo.get_issues = source
    elif stage == "embedding":
        service._embedding_service.embed_texts.side_effect = fail
    else:
        collection.upsert = fail
    expected = {
        "domain": ReviewCancelledError,
        "deadline": RelationDeadlineExceeded,
        "none": RuntimeError,
    }[signal]
    with pytest.raises(expected) as captured:
        await IssueCandidateRetriever(service).retrieve(
            "owner",
            "repo",
            text="query",
            state="all",
            exclude_numbers=[],
            top_k=1,
            similarity_threshold=0.8,
            cancel_event=event,
            deadline=deadline,
        )
    if signal == "none":
        assert captured.value is failure
    else:
        assert captured.value.__cause__ is failure
    assert collection.metadata == old_metadata
    assert not collection.docs


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["domain", "deadline", "none"])
async def test_real_pr_consumer_retains_retrieval_error_control(
    foundation, freshness, signal
):
    service, _, repo, _ = foundation
    event = asyncio.Event()
    deadline = test_issue_candidate_foundation.MutableDeadline()

    def source(**kwargs):
        if signal == "domain":
            event.set()
        elif signal == "deadline":
            deadline.expired = True
        raise RuntimeError("source unavailable")

    repo.get_issues = source
    freshness.service.retriever = IssueCandidateRetriever(service)
    if signal == "domain":
        with pytest.raises(ReviewCancelledError):
            await freshness.service.synchronize(
                freshness.repo, "o", "r", 618, cancel_event=event, deadline=deadline
            )
    else:
        result = await freshness.service.synchronize(
            freshness.repo, "o", "r", 618, cancel_event=event, deadline=deadline
        )
        assert not result.succeeded
        assert result.failure == (
            "deadline" if signal == "deadline" else "RuntimeError"
        )
    test_pr_candidate_freshness.assert_preserved(freshness)


@pytest.mark.asyncio
async def test_task_cancel_stops_before_next_comment_page(foundation):
    import threading

    from github.IssueComment import IssueComment
    from github.PaginatedList import PaginatedList

    service, _, repo, values = foundation
    values.update(
        issue_relation_candidate_max_comments=20,
        issue_relation_candidate_comment_max_chars=4000,
    )
    started, release, completed = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    first = "https://api.github.com/repos/owner/repo/issues/1/comments"
    last = first + "?page=1000"
    previous = first + "?page=999"
    urls = []

    def row(number):
        return {
            "id": number,
            "url": f"https://api.github.com/repos/owner/repo/issues/comments/{number}",
            "body": "source decision",
            "html_url": f"https://github.com/owner/repo/issues/1#issuecomment-{number}",
            "user": {"login": "maintainer"},
            "created_at": "2026-10-02T00:00:00Z",
            "updated_at": "2026-10-02T00:00:00Z",
        }

    class Requester:
        is_not_lazy = False
        per_page = 30

        def requestJsonAndCheck(self, method, url, **kwargs):
            urls.append(url)
            if url == first:
                return {"link": f'<{last}>; rel="last"'}, [row(1)]
            if url == last:
                started.set()
                assert release.wait(3)
                completed.set()
                return {"link": f'<{previous}>; rel="prev"'}, [row(30000)]
            assert url == previous
            return {}, [row(i) for i in range(29970, 30000)]

    original_get = repo.get_issue

    def get_issue(number):
        obj = original_get(number)
        obj.raw_data = {**obj.raw_data, "comments": 30000}
        obj.get_comments = lambda: PaginatedList(IssueComment, Requester(), first, {})
        return obj

    repo.get_issue = get_issue
    task = asyncio.create_task(
        IssueCandidateRetriever(service).retrieve(
            "owner",
            "repo",
            text="query",
            state="all",
            exclude_numbers=[],
            top_k=1,
            similarity_threshold=0.8,
            include_comments=True,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()
    assert urls == [first, last]
