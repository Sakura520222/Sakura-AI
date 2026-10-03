"""Accepted candidate source versions must survive until PR publication."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from github import GithubException, UnknownObjectException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.models.database import PRIssueLink
from backend.services.issues.pr_link_sync import PRRelationSyncService
from backend.services.issues.pr_verifier import PRRelationVerifier
from tests import test_pr_issue_budget
from tests.test_pr_issue_budget import Pull, changed_file
from tests.test_pr_issue_relations import CANDIDATE, RELATION, configured_client, linker

budgets = test_pr_issue_budget.budgets


@pytest.fixture
def freshness(budgets):
    engine = create_engine("sqlite:///:memory:")
    PRIssueLink.__table__.create(engine)
    session = Session(engine)
    session.add(
        PRIssueLink(
            repo_name="o/r",
            pr_id=618,
            issue_number=570,
            link_type="semantic",
            inference_reason="old proof",
        )
    )
    session.commit()
    pr = Pull(lambda: [changed_file()])
    original_body = pr.body
    candidate = {**deepcopy(CANDIDATE), "updated_at": "2026-10-02T00:00:00Z"}
    candidates = [candidate]
    sources = {612: {**deepcopy(candidate), "labels": []}}
    actions = SimpleNamespace(model=lambda: None, flush=lambda: None, read=None)
    reads, transactions = [], []
    accepted = [RELATION]

    class DB:
        async def __aenter__(self):
            transactions.append("enter")
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, stmt):
            return session.execute(stmt)

        def add(self, row):
            session.add(row)

        async def delete(self, row):
            session.delete(row)

        async def flush(self):
            session.flush()
            transactions.append("flush")
            actions.flush()

        async def rollback(self):
            transactions.append("rollback")
            session.rollback()

        async def commit(self):
            transactions.append("commit")
            session.commit()

    async def call(**kwargs):
        import json

        actions.model()
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps({"relations": accepted}))
                )
            ]
        )

    def get_issue(number):
        reads.append(number)
        if actions.read:
            actions.read(number)
        return SimpleNamespace(raw_data=deepcopy(sources[number]))

    repo = SimpleNamespace(get_pull=lambda _: pr, get_issue=get_issue)
    retriever = SimpleNamespace(retrieve=AsyncMock(return_value=candidates))
    service = PRRelationSyncService(
        retriever=retriever,
        verifier=PRRelationVerifier(configured_client(call_with_retry=call)),
        session_factory=DB,
        linker=linker(),
    )
    yield SimpleNamespace(
        service=service,
        repo=repo,
        pr=pr,
        session=session,
        original_body=original_body,
        candidates=candidates,
        sources=sources,
        actions=actions,
        reads=reads,
        transactions=transactions,
        accepted=accepted,
        retriever=retriever,
    )
    session.close()
    engine.dispose()


def assert_preserved(harness):
    assert harness.pr.body == harness.original_body
    assert harness.pr.edits == []
    rows = harness.session.scalars(select(PRIssueLink)).all()
    assert [(r.issue_number, r.inference_reason) for r in rows] == [(570, "old proof")]
    assert "commit" not in harness.transactions


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["model", "flush"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "Different requirement"),
        ("body", "Additional unmet requirement"),
        ("state", "closed"),
        ("labels", [{"name": "changed"}]),
        ("state_reason", "completed"),
        ("number", 613),
        # Edit/revert and close/reopen have identical final facts except the version.
        ("updated_at", "2026-10-02T00:01:00Z"),
    ],
)
async def test_candidate_change_preserves_whole_previous_set(
    freshness, phase, field, value
):
    setattr(
        freshness.actions, phase, lambda: freshness.sources[612].update({field: value})
    )
    result = await freshness.service.synchronize(freshness.repo, "o", "r", 618)
    assert not result.succeeded
    assert result.failure == "stale_candidate"
    assert_preserved(freshness)
    if phase == "flush":
        assert (
            "flush" in freshness.transactions and "rollback" in freshness.transactions
        )
    else:
        assert freshness.transactions == []


@pytest.mark.asyncio
async def test_one_changed_of_multiple_accepted_rejects_entire_set(freshness):
    candidate = {**deepcopy(freshness.candidates[0]), "number": 613}
    freshness.candidates.append(candidate)
    freshness.sources[613] = {**deepcopy(candidate), "labels": []}
    freshness.accepted.append({**RELATION, "number": 613, "relation": "related"})
    freshness.actions.flush = lambda: freshness.sources[613].update(body="Changed")
    result = await freshness.service.synchronize(freshness.repo, "o", "r", 618)
    assert result.failure == "stale_candidate"
    assert_preserved(freshness)


@pytest.mark.asyncio
async def test_verifier_mutating_original_candidate_cannot_launder_change(freshness):
    def change():
        freshness.sources[612]["updated_at"] = "2026-10-02T00:01:00Z"
        freshness.candidates[0]["updated_at"] = freshness.sources[612]["updated_at"]

    freshness.actions.model = change
    result = await freshness.service.synchronize(freshness.repo, "o", "r", 618)
    assert result.failure == "stale_candidate"
    assert_preserved(freshness)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["model", "flush"])
@pytest.mark.parametrize(
    "failure", ["404", "403", "network", "missing", "malformed", "version"]
)
async def test_unavailable_candidate_facts_preserve_previous_set(
    freshness, phase, failure
):
    def fail_read(_):
        if failure in {"404", "403"}:
            error = UnknownObjectException if failure == "404" else GithubException
            raise error(int(failure), {"message": "denied"}, {})
        if failure == "network":
            raise ConnectionError("unavailable")
        if failure == "missing":
            del freshness.sources[612]["body"]
        elif failure == "malformed":
            freshness.sources[612]["labels"] = None
        elif failure == "version":
            freshness.sources[612]["updated_at"] = "not-a-version"

    setattr(
        freshness.actions, phase, lambda: setattr(freshness.actions, "read", fail_read)
    )
    result = await freshness.service.synchronize(freshness.repo, "o", "r", 618)
    assert not result.succeeded
    assert result.failure == "candidate_facts_unavailable"
    assert_preserved(freshness)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "", "malformed", "2026-10-02T00:00:00"])
async def test_missing_or_malformed_original_version_fails_closed(freshness, version):
    freshness.candidates[0]["updated_at"] = version
    result = await freshness.service.synchronize(freshness.repo, "o", "r", 618)
    assert result.failure == "candidate_facts_unavailable"
    assert_preserved(freshness)


@pytest.mark.asyncio
async def test_unchanged_accepted_and_changed_unaccepted_candidate_publish(freshness):
    other = {**deepcopy(freshness.candidates[0]), "number": 613}
    freshness.candidates.append(other)
    freshness.sources[613] = {**other, "labels": []}
    freshness.actions.model = lambda: freshness.sources[613].update(body="Changed")
    result = await freshness.service.synchronize(freshness.repo, "o", "r", 618)
    assert result.succeeded
    assert freshness.reads == [612, 612]
    assert "Closes #612" in freshness.pr.body and "Closes #570" not in freshness.pr.body
    rows = freshness.session.scalars(select(PRIssueLink)).all()
    assert [r.issue_number for r in rows] == [612]


@pytest.mark.asyncio
async def test_empty_verification_cleans_up_without_candidate_freshness(freshness):
    freshness.accepted.clear()
    freshness.actions.model = lambda: freshness.sources.clear()
    result = await freshness.service.synchronize(freshness.repo, "o", "r", 618)
    assert result.succeeded
    assert freshness.reads == []
    assert not freshness.session.scalars(select(PRIssueLink)).all()
    assert "Closes #570" not in freshness.pr.body


@pytest.mark.asyncio
async def test_retrieval_soft_deadline_is_optional_failure_and_preserves_set(freshness):
    from backend.services.issues.relation_runtime import RelationDeadlineExceeded

    deadline = SimpleNamespace(is_expired=lambda: False)
    event = asyncio.Event()
    freshness.retriever.retrieve.side_effect = RelationDeadlineExceeded()
    result = await freshness.service.synchronize(
        freshness.repo,
        "o",
        "r",
        618,
        cancel_event=event,
        deadline=deadline,
    )
    assert result.failure == "deadline" and not result.succeeded
    assert freshness.retriever.retrieve.await_args.kwargs["cancel_event"] is event
    assert freshness.retriever.retrieve.await_args.kwargs["deadline"] is deadline
    assert_preserved(freshness)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["model", "flush"])
@pytest.mark.parametrize("control", ["deadline", "domain", "both"])
async def test_candidate_read_boundary_retains_deadline_and_domain_semantics(
    freshness, phase, control
):
    from backend.core.ai_protocol.errors import ReviewCancelledError

    event = asyncio.Event()
    expired = False
    deadline = SimpleNamespace(is_expired=lambda: expired)

    def interrupt(_):
        nonlocal expired
        if control in {"deadline", "both"}:
            expired = True
        if control in {"domain", "both"}:
            event.set()

    setattr(
        freshness.actions, phase, lambda: setattr(freshness.actions, "read", interrupt)
    )
    if control == "deadline":
        result = await freshness.service.synchronize(
            freshness.repo,
            "o",
            "r",
            618,
            cancel_event=event,
            deadline=deadline,
        )
        assert result.failure == "deadline" and not result.succeeded
    else:
        with pytest.raises(ReviewCancelledError):
            await freshness.service.synchronize(
                freshness.repo,
                "o",
                "r",
                618,
                cancel_event=event,
                deadline=deadline,
            )
    assert_preserved(freshness)
    if phase == "flush":
        assert "rollback" in freshness.transactions


@pytest.mark.asyncio
async def test_task_cancellation_drains_candidate_read_before_releasing_ownership(
    freshness,
):
    import threading

    started, release = threading.Event(), threading.Event()

    def read(_):
        started.set()
        assert release.wait(3)

    freshness.actions.read = read
    task = asyncio.create_task(
        freshness.service.synchronize(freshness.repo, "o", "r", 618)
    )
    next_task = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        next_task = asyncio.create_task(
            freshness.service.synchronize(freshness.repo, "o", "r", 618)
        )
        await asyncio.sleep(0.02)
        assert not task.done() and freshness.reads == [612]
        assert freshness.transactions == []
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await next_task).succeeded


@pytest.mark.asyncio
async def test_soft_deadline_waits_for_existing_candidate_read_and_stops_before_next(
    freshness,
):
    import threading

    started, release = threading.Event(), threading.Event()
    expired = False
    deadline = SimpleNamespace(is_expired=lambda: expired)

    def read(_):
        started.set()
        assert release.wait(3)

    freshness.actions.read = read
    task = asyncio.create_task(
        freshness.service.synchronize(freshness.repo, "o", "r", 618, deadline=deadline)
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        expired = True
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        release.set()
    result = await task
    assert result.failure == "deadline" and freshness.reads == [612]
    assert_preserved(freshness)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["domain", "deadline"])
async def test_candidate_read_error_retains_pending_control_semantics(
    freshness, control
):
    event = asyncio.Event()
    expired = False
    deadline = SimpleNamespace(is_expired=lambda: expired)

    def read(_):
        nonlocal expired
        if control == "domain":
            event.set()
        else:
            expired = True
        raise ConnectionError("failed during interruption")

    freshness.actions.read = read
    if control == "domain":
        from backend.core.ai_protocol.errors import ReviewCancelledError

        with pytest.raises(ReviewCancelledError):
            await freshness.service.synchronize(
                freshness.repo, "o", "r", 618, cancel_event=event, deadline=deadline
            )
    else:
        result = await freshness.service.synchronize(
            freshness.repo, "o", "r", 618, cancel_event=event, deadline=deadline
        )
        assert result.failure == "deadline"
    assert_preserved(freshness)


@pytest.mark.asyncio
@pytest.mark.parametrize("round_trip", ["edit_revert", "close_reopen"])
async def test_candidate_round_trip_retains_final_facts_but_rejects_new_version(
    freshness, round_trip
):
    before = deepcopy(freshness.sources[612])

    def change():
        source = freshness.sources[612]
        if round_trip == "edit_revert":
            source.update(
                body="Temporary changed requirement", updated_at="2026-10-02T00:01:00Z"
            )
            source.update(body=before["body"], updated_at="2026-10-02T00:02:00Z")
        else:
            source.update(
                state="closed",
                state_reason="completed",
                updated_at="2026-10-02T00:01:00Z",
            )
            source.update(
                state=before["state"],
                state_reason=before["state_reason"],
                updated_at="2026-10-02T00:02:00Z",
            )

    freshness.actions.model = change
    result = await freshness.service.synchronize(freshness.repo, "o", "r", 618)
    assert {k: v for k, v in freshness.sources[612].items() if k != "updated_at"} == {
        k: v for k, v in before.items() if k != "updated_at"
    }
    assert result.failure == "stale_candidate"
    assert_preserved(freshness)
