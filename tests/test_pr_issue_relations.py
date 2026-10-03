"""Evidence and replacement regressions for PR relations (#619)."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.services.pr_issue_linker import PRIssueLinker


@pytest.fixture(autouse=True)
def configured_pr_budget(monkeypatch):
    from backend.services.ai_reviewer.api_client import AIApiClient
    from backend.services.issues import pr_budget
    from tests.test_pr_issue_budget import summary_candidate

    monkeypatch.setattr(
        AIApiClient,
        "resolve_role_candidates",
        AsyncMock(return_value=[summary_candidate()]),
    )
    monkeypatch.setattr(
        pr_budget,
        "get_dynamic_config",
        AsyncMock(
            side_effect=lambda key, **_: {
                "pr_issue_max_files": 128,
                "pr_issue_max_input_tokens": 64000,
            }[key]
        ),
    )


def configured_client(**kwargs):
    from tests.test_pr_issue_budget import summary_candidate

    return SimpleNamespace(
        resolve_role_candidates=AsyncMock(return_value=[summary_candidate()]), **kwargs
    )


def linker():
    obj = PRIssueLinker.__new__(PRIssueLinker)
    import re

    obj._reference_pattern = re.compile(
        r"(?:Fixes|Closes|Resolves)\s+#(\d+)", re.IGNORECASE
    )
    return obj


@pytest.mark.asyncio
async def test_generated_sections_do_not_supply_explicit_references():
    body = "Human Fixes #123\n<!-- sakura-ai-summary-start -->\nFixes #570\n<!-- sakura-ai-summary-end -->\n<!-- sakura-ai-issue-links-start -->\nResolves #570\n<!-- sakura-ai-issue-links-end -->"
    assert await linker().parse_issue_references(body) == [123]


def test_replace_set_preserves_human_references_and_related_is_not_closing():
    obj = linker()
    body = (
        "Human Fixes #123\n\n"
        + obj.ISSUE_LINKS_START
        + "\nResolves #570\n"
        + obj.ISSUE_LINKS_END
    )
    result = obj.build_updated_pr_body(body, [{"number": 612, "relation": "related"}])
    assert "Fixes #123" in result
    assert "#570" not in result
    assert "Related to #612" in result
    assert "Resolves #612" not in result
    assert obj.ISSUE_LINKS_START not in obj.build_updated_pr_body(result, [])


CANDIDATE = {
    "number": 612,
    "title": "Bootstrap retries",
    "body": "Retry transient TLS EOF",
    "state": "open",
    "labels": [],
    "state_reason": None,
}
FILES = [
    {
        "path": "dependency.py",
        "patch": "@@ -1 +1 @@\n-old\n+retry_tls_eof()",
        "complete": True,
    }
]
RELATION = {
    "number": 612,
    "relation": "closes",
    "confidence": 0.99,
    "reason": "Adds retry",
    "evidence": [
        {
            "path": "dependency.py",
            "change": "added",
            "code_quote": "retry_tls_eof()",
            "issue_quote": "Retry transient TLS EOF",
        }
    ],
}


async def verify(monkeypatch, payload=None, error=None, **kwargs):
    from backend.services.issues import pr_verifier

    async def call(**call_kwargs):
        if error:
            raise error
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))
            ]
        )

    monkeypatch.setattr(
        pr_verifier,
        "get_dynamic_config",
        AsyncMock(
            side_effect=lambda key, **_: {
                "pr_issue_related_confidence_threshold": 0.85,
                "pr_issue_closing_confidence_threshold": 0.95,
            }[key]
        ),
    )
    client = configured_client(call_with_retry=AsyncMock(side_effect=call))
    result = await pr_verifier.PRRelationVerifier(client).verify(
        pr_title="TLS retry PR",
        pr_body="Human\n<!-- sakura-ai-summary-start -->Fixes #570<!-- sakura-ai-summary-end -->",
        candidates=kwargs.pop("candidates", [CANDIDATE]),
        files=kwargs.pop("files", FILES),
        **kwargs,
    )
    return result, client


@pytest.mark.asyncio
async def test_grounded_tls_retry_protocol_closes_and_summary_is_not_evidence(
    monkeypatch,
):
    result, client = await verify(monkeypatch, {"relations": [RELATION]})
    assert result.succeeded
    assert result.relations[0]["relation"] == "closes"
    text = client.call_with_retry.call_args.kwargs["messages"][1]["content"]
    assert "#570" not in text
    assert "retry_tls_eof" in text
    assert client.call_with_retry.call_args.kwargs["role"] == "summary"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"number": 570},
        {"confidence": float("nan")},
        {"confidence": True},
        {"evidence": []},
        {"relation": "duplicate"},
        {
            "evidence": [
                {
                    "path": "dependency.py",
                    "change": "added",
                    "code_quote": "invented()",
                    "issue_quote": "Retry transient TLS EOF",
                }
            ]
        },
    ],
)
async def test_invalid_model_output_fails_closed(monkeypatch, change):
    result, _ = await verify(monkeypatch, {"relations": [{**RELATION, **change}]})
    assert not result.succeeded
    assert result.relations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,error",
    [({"verified": [612]}, None), (None, TimeoutError()), (None, RuntimeError("500"))],
)
async def test_protocol_and_provider_failures_are_not_empty_success(
    monkeypatch, payload, error
):
    result, _ = await verify(monkeypatch, payload, error)
    assert not result.succeeded
    assert result.relations == []


@pytest.mark.asyncio
async def test_verifier_empty_success_and_cancellation(monkeypatch):
    result, _ = await verify(monkeypatch, {"relations": []})
    assert result.succeeded and result.relations == []
    with pytest.raises(asyncio.CancelledError):
        await verify(monkeypatch, error=asyncio.CancelledError())


@pytest.mark.asyncio
async def test_semantic_sync_repeat_and_empty_preserve_other_owners():
    from backend.models.database import PRIssueLink
    from backend.services.issues.pr_link_sync import replace_semantic_links

    engine = create_engine("sqlite:///:memory:")
    PRIssueLink.__table__.create(engine)

    class Adapter:
        def __init__(self, session):
            self.session = session

        async def execute(self, stmt):
            return self.session.execute(stmt)

        def add(self, obj):
            self.session.add(obj)

        async def flush(self):
            self.session.flush()

        async def delete(self, row):
            self.session.delete(row)

    with Session(engine) as session:
        session.add_all(
            [
                PRIssueLink(repo_name=r, pr_id=p, issue_number=570, link_type=t)
                for r, p, t in [
                    ("owner/repo", 618, "explicit"),
                    ("other/repo", 618, "semantic"),
                    ("owner/repo", 574, "semantic"),
                ]
            ]
        )
        session.commit()
        for _ in range(5):
            await replace_semantic_links(
                Adapter(session), "owner/repo", 618, [RELATION]
            )
            session.commit()
        rows = session.scalars(select(PRIssueLink)).all()
        assert len(rows) == 4
        semantic = next(r for r in rows if r.issue_number == 612)
        assert json.loads(semantic.inference_reason)["evidence"] == RELATION["evidence"]
        await replace_semantic_links(Adapter(session), "owner/repo", 618, [])
        session.commit()
        assert len(session.scalars(select(PRIssueLink)).all()) == 3


@pytest.mark.asyncio
async def test_agent_source_closes_survives_custom_empty_files_fallback():
    from backend.services.agent_team.pr_service import AgentTeamPRService

    obj = AgentTeamPRService.__new__(AgentTeamPRService)
    kwargs = {
        "task_title": "task",
        "task_summary": "summary",
        "fullstack_analysis": "",
        "review_summary": "",
        "review_verdict": "",
        "review_score": 0,
        "review_findings": [],
        "modified_files": [],
        "iteration_count": 1,
        "source_type": "issue_analysis",
        "source_issue_number": 612,
        "fallback_body": "Custom fallback",
    }
    assert "Closes #612" in await obj.generate_pr_body(**kwargs)
    kwargs["source_type"] = "pr_review"
    assert "Closes #612" not in await obj.generate_pr_body(**kwargs)


@pytest.mark.asyncio
async def test_exact_key_migration_deduplicates_and_enforces_uniqueness():
    import logging

    from sqlalchemy import inspect, text
    from sqlalchemy.exc import IntegrityError

    from backend.models.database import _ensure_pr_issue_link_unique_index

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE pr_issue_links (id INTEGER PRIMARY KEY, repo_name TEXT NOT NULL, pr_id INTEGER NOT NULL, issue_number INTEGER NOT NULL, link_type TEXT NOT NULL, inference_reason TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO pr_issue_links VALUES (1,'o/r',618,612,'semantic','old'),(2,'o/r',618,612,'semantic','new'),(3,'o/r',618,612,'explicit','human'),(4,'x/r',618,612,'semantic','other')"
            )
        )

        class Adapter:
            async def run_sync(self, f):
                return f(conn)

        assert await _ensure_pr_issue_link_unique_index(
            Adapter(), logging.getLogger(__name__)
        )
        assert not await _ensure_pr_issue_link_unique_index(
            Adapter(), logging.getLogger(__name__)
        )
        rows = (
            conn.execute(text("SELECT id FROM pr_issue_links ORDER BY id"))
            .scalars()
            .all()
        )
        assert rows == [2, 3, 4]
        assert any(i["unique"] for i in inspect(conn).get_indexes("pr_issue_links"))
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO pr_issue_links VALUES (5,'o/r',618,612,'semantic','duplicate')"
                )
            )


class PR:
    def __init__(self, body="Human", sha="head"):
        self.body, self.title = body, "PR title"
        self.head = SimpleNamespace(sha=sha)
        self.base = SimpleNamespace(sha="base")
        self.changed_files = 1
        self.edits = []

    def get_files(self):
        return [
            SimpleNamespace(
                filename="dependency.py",
                status="modified",
                patch=FILES[0]["patch"],
                additions=1,
                deletions=1,
            )
        ]

    def edit(self, **kwargs):
        self.edits.append(kwargs)
        self.body = kwargs["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed,stale", [(True, False), (False, True), (False, False)])
async def test_sync_status_controls_real_body_and_persistence(
    monkeypatch, failed, stale
):
    from backend.models.database import PRIssueLink
    from backend.services.issues.pr_link_sync import PRRelationSyncService
    from backend.services.issues.pr_verifier import PRVerificationResult

    engine = create_engine("sqlite:///:memory:")
    PRIssueLink.__table__.create(engine)
    session = Session(engine)
    session.add(
        PRIssueLink(repo_name="o/r", pr_id=618, issue_number=570, link_type="semantic")
    )
    session.commit()

    class DB:
        async def __aenter__(self):
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

        async def commit(self):
            session.commit()

        async def rollback(self):
            session.rollback()

    first = PR(
        "Human\n\n<!-- sakura-ai-issue-links-start -->\nResolves #570\n<!-- sakura-ai-issue-links-end -->"
    )
    latest = PR(first.body, "newhead") if stale else first
    repo = SimpleNamespace(get_pull=lambda _: pulls.pop(0) if pulls else latest)
    pulls = [first, latest]
    retriever = SimpleNamespace(retrieve=AsyncMock(return_value=[]))
    verifier = SimpleNamespace(
        verify=AsyncMock(
            return_value=PRVerificationResult(
                not failed, [], "provider" if failed else None
            )
        )
    )
    service = PRRelationSyncService(
        retriever=retriever, verifier=verifier, session_factory=DB, linker=linker()
    )
    result = await service.synchronize(repo, "o", "r", 618)
    assert result.succeeded == (not failed and not stale)
    if failed or stale:
        assert session.scalar(select(PRIssueLink.issue_number)) == 570
        assert not latest.edits
    else:
        assert session.scalar(select(PRIssueLink.issue_number)) is None
        assert "sakura-ai-issue-links-start" not in first.body
    assert retriever.retrieve.call_args.kwargs["state"] == "open"
    session.close()


@pytest.mark.asyncio
async def test_incomplete_diff_cannot_close(monkeypatch):
    from backend.services.issues import pr_verifier

    client = configured_client(
        call_with_retry=AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps({"relations": [RELATION]})
                        )
                    )
                ]
            )
        )
    )
    monkeypatch.setattr(pr_verifier, "get_dynamic_config", AsyncMock(return_value=0.85))
    result = await pr_verifier.PRRelationVerifier(client).verify(
        pr_title="PR",
        pr_body="Human",
        candidates=[CANDIDATE],
        files=[{**FILES[0], "complete": False}],
    )
    assert result.succeeded and not result.relations


def test_sync_uses_initialized_database_session_factory(monkeypatch):
    from backend.models import database
    from backend.services.issues.pr_link_sync import PRRelationSyncService

    factory = object()
    monkeypatch.setattr(database, "async_session", factory)
    service = PRRelationSyncService(
        retriever=object(), verifier=object(), linker=linker()
    )
    assert service.session_factory is factory


@pytest.mark.asyncio
async def test_cancelled_publish_drains_before_releasing_pr_ownership():
    from backend.services.issues.pr_link_sync import pr_relation_ownership

    async with pr_relation_ownership("o/r", 1):
        acquired = asyncio.Event()

        async def waiter():
            async with pr_relation_ownership("O/R", 1):
                acquired.set()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        assert not acquired.is_set()
    await task
    assert acquired.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["base", "human", "title"])
async def test_changed_source_fields_reject_stale_inference(mutation):
    from backend.services.issues.pr_link_sync import PRRelationSyncService
    from backend.services.issues.pr_verifier import PRVerificationResult

    first, latest = PR(), PR()
    first.base = SimpleNamespace(sha="base")
    latest.base = SimpleNamespace(sha="base")
    if mutation == "base":
        latest.base.sha = "newbase"
    elif mutation == "human":
        latest.body = "edited human description"
    else:
        latest.title = "new title"
    pulls = [first, latest]
    service = PRRelationSyncService(
        retriever=SimpleNamespace(retrieve=AsyncMock(return_value=[])),
        verifier=SimpleNamespace(
            verify=AsyncMock(return_value=PRVerificationResult(True))
        ),
        session_factory=lambda: None,
        linker=linker(),
    )
    result = await service.synchronize(
        SimpleNamespace(get_pull=lambda _: pulls.pop(0) if pulls else latest),
        "o",
        "r",
        618,
    )
    assert result.failure == "stale_source"
    assert not latest.edits


@pytest.mark.asyncio
async def test_review_worker_records_failed_status_without_injecting_relations(
    monkeypatch,
):
    from backend.services.issues import pr_link_sync
    from backend.services.issues.pr_verifier import PRVerificationResult
    from backend.workers.review_worker import ReviewWorker

    worker = ReviewWorker.__new__(ReviewWorker)
    worker._cancel_events = {"key": asyncio.Event()}
    sync = SimpleNamespace(
        synchronize=AsyncMock(
            return_value=PRVerificationResult(False, failure="provider")
        )
    )
    monkeypatch.setattr(pr_link_sync, "PRRelationSyncService", lambda: sync)
    context = {"main_analysis": "continue"}
    execution = SimpleNamespace(invocation_context=object(), observer=object())
    await worker._sync_pr_issue_relations(
        object(),
        {"repo_owner": "o", "repo_name": "r", "pr_number": 618},
        context,
        "task",
        "key",
        None,
        execution,
    )
    assert context == {
        "main_analysis": "continue",
        "pr_issue_relation_status": "provider",
    }
    assert sync.synchronize.call_args.kwargs["context"] is execution.invocation_context


@pytest.mark.asyncio
async def test_review_worker_successful_empty_cleanup_uses_real_sync(monkeypatch):
    from backend.models.database import PRIssueLink
    from backend.services.issues import pr_link_sync
    from backend.services.issues.pr_verifier import PRVerificationResult
    from backend.workers.review_worker import ReviewWorker

    engine = create_engine("sqlite:///:memory:")
    PRIssueLink.__table__.create(engine)
    session = Session(engine)
    session.add(
        PRIssueLink(repo_name="o/r", pr_id=618, issue_number=570, link_type="semantic")
    )
    session.commit()

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, stmt):
            return session.execute(stmt)

        async def delete(self, row):
            session.delete(row)

        async def flush(self):
            session.flush()

        async def commit(self):
            session.commit()

        async def rollback(self):
            session.rollback()

    pr = PR(
        "Human\n<!-- sakura-ai-issue-links-start -->Resolves #570<!-- sakura-ai-issue-links-end -->"
    )
    service = pr_link_sync.PRRelationSyncService(
        retriever=SimpleNamespace(retrieve=AsyncMock(return_value=[])),
        verifier=SimpleNamespace(
            verify=AsyncMock(return_value=PRVerificationResult(True))
        ),
        session_factory=DB,
        linker=linker(),
    )
    monkeypatch.setattr(pr_link_sync, "PRRelationSyncService", lambda: service)
    worker = ReviewWorker.__new__(ReviewWorker)
    worker._cancel_events = {}
    context = {}
    await worker._sync_pr_issue_relations(
        SimpleNamespace(get_pull=lambda _: pr),
        {"repo_owner": "o", "repo_name": "r", "pr_number": 618},
        context,
        "task",
        "key",
        None,
        SimpleNamespace(),
    )
    assert context == {
        "pr_issue_relation_status": "verified",
        "semantically_linked_issues": [],
    }
    assert session.scalar(select(PRIssueLink.issue_number)) is None
    assert "Resolves #570" not in pr.body
    session.close()


@pytest.mark.asyncio
async def test_agent_without_custom_fallback_keeps_template_and_source():
    from backend.services.agent_team.pr_service import AgentTeamPRService

    obj = AgentTeamPRService.__new__(AgentTeamPRService)
    body = await obj.generate_pr_body(
        task_title="Important task",
        task_summary="details",
        fullstack_analysis="",
        review_summary="",
        review_verdict="",
        review_score=0,
        review_findings=[],
        modified_files=[],
        iteration_count=1,
        source_type="issue_analysis",
        source_issue_number=612,
    )
    assert "Important task" in body
    assert "Closes #612" in body


@pytest.mark.asyncio
async def test_cancellation_drains_real_thread_edit_before_next_sync():
    import threading

    from backend.models.database import PRIssueLink
    from backend.services.issues.pr_link_sync import PRRelationSyncService
    from backend.services.issues.pr_verifier import PRVerificationResult

    engine = create_engine("sqlite:///:memory:")
    PRIssueLink.__table__.create(engine)
    session = Session(engine)

    class DB:
        async def __aenter__(self):
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

        async def commit(self):
            session.commit()

        async def rollback(self):
            session.rollback()

    started, release = threading.Event(), threading.Event()
    pr = PR(
        "Human\n<!-- sakura-ai-issue-links-start -->Resolves #570<!-- sakura-ai-issue-links-end -->"
    )
    original_edit = pr.edit

    def gated_edit(**kwargs):
        if not started.is_set():
            started.set()
            assert release.wait(3)
        original_edit(**kwargs)

    pr.edit = gated_edit
    reads = []

    def get_pull(_):
        reads.append(1)
        return pr

    verifier = SimpleNamespace(
        verify=AsyncMock(
            side_effect=[
                PRVerificationResult(True),
                PRVerificationResult(True, [RELATION]),
            ]
        )
    )
    service = PRRelationSyncService(
        retriever=SimpleNamespace(retrieve=AsyncMock(return_value=[])),
        verifier=verifier,
        session_factory=DB,
        linker=linker(),
    )
    repo = SimpleNamespace(get_pull=get_pull)
    first = asyncio.create_task(service.synchronize(repo, "o", "r", 618))
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        first.cancel()
        await asyncio.sleep(0)
        second = asyncio.create_task(service.synchronize(repo, "o", "r", 618))
        await asyncio.sleep(0.02)
        assert not first.done()
        assert len(reads) == 3
        first.cancel()  # repeated cancellation must not cancel the mutation task
        await asyncio.sleep(0)
        assert not first.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await second).succeeded
    assert "Closes #612" in pr.body
    assert session.scalar(select(PRIssueLink.issue_number)) == 612
    session.close()


@pytest.mark.asyncio
async def test_provider_review_cancellation_propagates(monkeypatch):
    from backend.core.ai_protocol.errors import ReviewCancelledError

    with pytest.raises(ReviewCancelledError):
        await verify(monkeypatch, error=ReviewCancelledError("cancelled"))


@pytest.mark.asyncio
async def test_migration_same_name_nonunique_index_fails_before_deleting_rows():
    import logging

    from sqlalchemy import text

    from backend.models.database import _ensure_pr_issue_link_unique_index

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE pr_issue_links (id INTEGER PRIMARY KEY, repo_name TEXT, pr_id INTEGER, issue_number INTEGER, link_type TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO pr_issue_links VALUES (1,'o/r',618,612,'semantic'),(2,'o/r',618,612,'semantic')"
            )
        )
        conn.execute(
            text("CREATE INDEX uq_pr_issue_link_key ON pr_issue_links(repo_name)")
        )

        class Adapter:
            async def run_sync(self, f):
                return f(conn)

        with pytest.raises(RuntimeError, match="not unique"):
            await _ensure_pr_issue_link_unique_index(
                Adapter(), logging.getLogger(__name__)
            )
        assert conn.scalar(text("SELECT COUNT(*) FROM pr_issue_links")) == 2


@pytest.mark.asyncio
async def test_source_edit_during_database_flush_prevents_body_publication():
    from backend.services.issues.pr_link_sync import PRRelationSyncService
    from backend.services.issues.pr_verifier import PRVerificationResult

    pr = PR(
        "Human\n<!-- sakura-ai-issue-links-start -->Resolves #570<!-- sakura-ai-issue-links-end -->"
    )

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=list))

        async def flush(self):
            pr.body = "New human description"

        async def rollback(self):
            pass

        async def commit(self):
            pytest.fail("stale source must not commit")

    service = PRRelationSyncService(
        retriever=SimpleNamespace(retrieve=AsyncMock(return_value=[])),
        verifier=SimpleNamespace(
            verify=AsyncMock(return_value=PRVerificationResult(True))
        ),
        session_factory=DB,
        linker=linker(),
    )
    result = await service.synchronize(
        SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618
    )
    assert result.failure == "stale_source"
    assert pr.body == "New human description"
    assert not pr.edits


@pytest.mark.parametrize("following", ["summary", "depgraph"])
@pytest.mark.parametrize("relations", [[], [{"number": 612, "relation": "related"}]])
def test_truncated_issue_links_preserves_following_generated_section(
    following, relations
):
    block = f"<!-- sakura-ai-{following}-start -->\nFresh {following}\n<!-- sakura-ai-{following}-end -->"
    body = (
        "Human Fixes #123\n<!-- sakura-ai-issue-links-start -->\nResolves #570\n"
        + block
    )
    updated = linker().build_updated_pr_body(body, relations)
    assert block in updated
    assert "Fixes #123" in updated
    assert "Resolves #570" not in updated
    from backend.services.pr_body import strip_sakura_generated_sections

    assert strip_sakura_generated_sections(updated).startswith("Human Fixes #123")


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["summary", "depgraph"])
async def test_body_writer_replaces_only_its_own_generated_section(writer):
    from backend.services.ai_reviewer.pr_dependency_graph import (
        PRDependencyGraphService,
    )
    from backend.services.ai_reviewer.pr_summary import PRSummaryService

    blocks = {
        kind: f"<!-- sakura-ai-{kind}-start -->\nOld {kind}\n<!-- sakura-ai-{kind}-end -->"
        for kind in ("summary", "depgraph", "issue-links")
    }
    pr = PR("Human Fixes #123\n" + "\n".join(blocks.values()))
    if writer == "summary":
        await PRSummaryService(None).update_pr_body(pr, "Fresh summary")
    else:
        await PRDependencyGraphService(None).update_pr_body_with_graph(
            pr, "graph TD; A-->B;"
        )
    for kind, block in blocks.items():
        if kind != writer:
            assert block in pr.body
        else:
            assert block not in pr.body
    assert "Human Fixes #123" in pr.body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "existing",
    [
        "<!-- sakura-ai-issue-links-start -->\nCloses #612\n<!-- sakura-ai-issue-links-end -->",
        "```text\nCloses #612\n```",
        "<!-- sakura-ai-summary-start -->\nCloses #612\n<!-- sakura-ai-summary-end -->",
        "<!-- sakura-ai-issue-links-start -->\nCloses #612",
        "```text\nCloses #612",
    ],
)
async def test_agent_trusted_source_survives_generated_or_fenced_prose(existing):
    from backend.services.agent_team.pr_service import AgentTeamPRService
    from backend.services.pr_body import strip_sakura_generated_sections

    obj = AgentTeamPRService.__new__(AgentTeamPRService)
    body = await obj.generate_pr_body(
        task_title="task",
        task_summary="",
        fullstack_analysis="",
        review_summary="",
        review_verdict="",
        review_score=0,
        review_findings=[],
        modified_files=[],
        iteration_count=1,
        source_type="issue_analysis",
        source_issue_number=612,
        fallback_body=existing,
    )
    assert body.startswith("Closes #612\n")
    assert strip_sakura_generated_sections(body).splitlines()[0] == "Closes #612"
    assert linker().build_updated_pr_body(body, []).splitlines()[0] == "Closes #612"
    assert obj._ensure_source_closes(body, "issue_analysis", 612) == body


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["model", "empty", "short", "failure"])
async def test_agent_model_and_fallback_paths_keep_stable_source_metadata(
    monkeypatch, response_kind
):
    from backend.services.agent_team import ai_client
    from backend.services.agent_team.pr_service import AgentTeamPRService
    from backend.services.pr_body import strip_sakura_generated_sections

    generated = (
        "<!-- sakura-ai-issue-links-start -->\nCloses #612\n<!-- sakura-ai-issue-links-end -->\n"
        + "Detailed summary " * 10
    )

    async def call(**kwargs):
        if response_kind == "failure":
            raise RuntimeError("provider failure")
        if response_kind == "empty":
            return SimpleNamespace(choices=[])
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=generated if response_kind == "model" else "short"
                    )
                )
            ]
        )

    monkeypatch.setattr(
        ai_client,
        "create_agent_team_summary_client",
        AsyncMock(
            return_value=(configured_client(call_with_retry=call), "summary", {})
        ),
    )
    obj = AgentTeamPRService.__new__(AgentTeamPRService)
    body = await obj.generate_pr_body(
        task_title="task",
        task_summary="",
        fullstack_analysis="",
        review_summary="",
        review_verdict="",
        review_score=0,
        review_findings=[],
        modified_files=["changed.py"],
        iteration_count=1,
        source_type="issue_analysis",
        source_issue_number=612,
        fallback_body="```text\nCloses #612\n```",
    )
    assert strip_sakura_generated_sections(body).splitlines()[0] == "Closes #612"
    assert linker().build_updated_pr_body(body, []).splitlines()[0] == "Closes #612"


@pytest.mark.parametrize("body", ["Closes #612\nHuman", "    Closes #612\n\nHuman"])
def test_agent_deduplicates_only_effective_source_prose(body):
    from backend.services.agent_team.pr_service import AgentTeamPRService

    result = AgentTeamPRService._ensure_source_closes(body, "issue_analysis", 612)
    if body.startswith("Closes"):
        assert result == body
    else:
        assert result.startswith("Closes #612\n") and body in result


def nested_generated_body(outer, inner):
    return (
        f"Human Fixes #123\n<!-- sakura-ai-{outer}-start -->\nGenerated introduction\n"
        f"<!-- sakura-ai-{inner}-start -->\nRelated to #100\n<!-- sakura-ai-{inner}-end -->\n"
        f"Closes #612\n<!-- sakura-ai-{outer}-end -->\nHuman Fixes #321"
    )


@pytest.mark.parametrize("outer", ["summary", "depgraph", "issue-links"])
@pytest.mark.parametrize("inner", ["summary", "depgraph", "issue-links"])
@pytest.mark.asyncio
async def test_complete_outer_owns_nested_sections_for_all_human_consumers(
    outer, inner
):
    from backend.services.agent_team.pr_service import AgentTeamPRService
    from backend.services.pr_body import strip_sakura_generated_sections

    body = nested_generated_body(outer, inner)
    human = strip_sakura_generated_sections(body)
    assert "Generated introduction" not in human
    assert "Closes #612" not in human
    assert "sakura-ai-" not in human
    assert sorted(await linker().parse_issue_references(body)) == [123, 321]
    trusted = AgentTeamPRService._ensure_source_closes(body, "issue_analysis", 612)
    assert trusted.startswith("Closes #612\n")
    assert strip_sakura_generated_sections(trusted).splitlines()[0] == "Closes #612"


@pytest.mark.parametrize(
    "outer,inner",
    [("summary", "issue-links"), ("depgraph", "summary"), ("issue-links", "depgraph")],
)
def test_selective_writer_preserves_nested_markers_owned_by_intact_other_section(
    outer, inner
):
    from backend.services.pr_body import (
        remove_sakura_generated_sections,
        replace_sakura_generated_section,
    )

    body = nested_generated_body(outer, inner)
    assert remove_sakura_generated_sections(body, sections={inner}) == body
    updated = replace_sakura_generated_section(body, inner, "replacement")
    assert updated.startswith(body)


@pytest.mark.asyncio
async def test_cancelled_publication_error_preserves_task_cancellation(monkeypatch):
    import threading

    from backend.services.issues.pr_link_sync import PRRelationSyncService
    from backend.services.issues.pr_verifier import PRVerificationResult

    started, release = threading.Event(), threading.Event()
    rollback = AsyncMock()

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=list))

        flush = AsyncMock()
        commit = AsyncMock()

    DB.rollback = rollback
    pr = PR(
        "Human\n<!-- sakura-ai-issue-links-start -->Closes #570<!-- sakura-ai-issue-links-end -->"
    )

    def failing_edit(**kwargs):
        started.set()
        assert release.wait(3)
        raise RuntimeError("GitHub edit failed")

    pr.edit = failing_edit
    service = PRRelationSyncService(
        retriever=SimpleNamespace(retrieve=AsyncMock(return_value=[])),
        verifier=SimpleNamespace(
            verify=AsyncMock(return_value=PRVerificationResult(True))
        ),
        session_factory=DB,
        linker=linker(),
    )
    task = asyncio.create_task(
        service.synchronize(SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618)
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert isinstance(caught.value.__cause__, RuntimeError)
    finally:
        release.set()
        if not task.done():
            await task
    rollback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["entry", "provider"])
async def test_verifier_ordinary_event_uses_domain_cancellation(monkeypatch, phase):
    from backend.core.ai_protocol.errors import ReviewCancelledError
    from backend.services.issues import pr_verifier

    event = asyncio.Event()

    async def call(**kwargs):
        event.set()
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content='{"relations":[]}'))
            ]
        )

    if phase == "entry":
        event.set()
    monkeypatch.setattr(pr_verifier, "get_dynamic_config", AsyncMock(return_value=0.95))
    with pytest.raises(ReviewCancelledError):
        await pr_verifier.PRRelationVerifier(
            configured_client(call_with_retry=call)
        ).verify(
            pr_title="PR",
            pr_body="human",
            candidates=[CANDIDATE],
            files=FILES,
            cancel_event=event,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch,change,quote,accepted",
    [
        (
            "@@ -1 +1 @@\n-retry_tls_eof()\n+fail_fast()",
            "added",
            "retry_tls_eof()",
            False,
        ),
        ("@@ -1 +1 @@\n-old\n+retry_tls_eof()", None, "retry_tls_eof()", False),
        ("@@ -1 +1 @@\n-retry_tls_eof()\n+fail_fast()", None, "retry_tls_eof()", False),
        ("@@ -1 +1 @@\n-old\n+retry_tls_eof()", "removed", "retry_tls_eof()", False),
        (
            "@@ -1,2 +1,2 @@\n+first()\n-removed()\n+second()",
            "added",
            "first()\nsecond()",
            False,
        ),
        (
            "@@ -1,2 +1,3 @@\n+first()\n context\n+second()",
            "added",
            "first()\nsecond()",
            False,
        ),
        (
            "@@ -1 +1 @@\n+first()\n@@ -4 +4 @@\n+second()",
            "added",
            "first()\nsecond()",
            False,
        ),
        ("@@ -1 +1,2 @@\n+first()\n+second()", "added", "first()\nsecond()", True),
        ("@@ -1,2 +1 @@\n-first()\n-second()", "removed", "first()\nsecond()", True),
        (
            "@@ -1 +1 @@\n-    fail_on_transient_tls()",
            "removed",
            "fail_on_transient_tls()",
            True,
        ),
    ],
)
async def test_evidence_direction_and_contiguous_hunk_runs(
    monkeypatch, patch, change, quote, accepted
):
    evidence = {
        "path": "dependency.py",
        "code_quote": quote,
        "issue_quote": "Retry transient TLS EOF",
    }
    if change is not None:
        evidence["change"] = change
    relation = {
        **RELATION,
        "reason": "Fix transient failures using the stated change",
        "evidence": [evidence],
    }
    result, _ = await verify(
        monkeypatch,
        {"relations": [relation]},
        files=[{"path": "dependency.py", "patch": patch, "complete": True}],
    )
    assert result.succeeded is accepted
    if accepted:
        assert result.relations[0]["evidence"] == [evidence]
    else:
        assert result.relations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["small_proof", "oversized_proof", "removed_as_added", "valid_removed"]
)
async def test_sync_persists_only_bounded_decision_and_preserves_large_source_context(
    monkeypatch, scenario
):
    from sqlalchemy import text

    from backend.models.database import PRIssueLink
    from backend.services.issues import pr_link_sync, pr_verifier

    engine = create_engine("sqlite:///:memory:")
    PRIssueLink.__table__.create(engine)
    # SQLite otherwise accepts oversized TEXT; enforce the deployed MySQL byte
    # invariant with a real SQL trigger while exercising the real ORM writes.
    with engine.begin() as conn:
        conn.execute(
            text("""CREATE TRIGGER inference_reason_text_limit BEFORE INSERT ON pr_issue_links
            WHEN length(CAST(NEW.inference_reason AS BLOB)) > 65535
            BEGIN SELECT RAISE(ABORT, 'TEXT byte capacity exceeded'); END""")
        )
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

    class DB:
        async def __aenter__(self):
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

        async def commit(self):
            session.commit()

        async def rollback(self):
            session.rollback()

    candidate = {**CANDIDATE, "body": CANDIDATE["body"] + "多字节正文" * 15000}
    candidate["content"] = candidate["title"] + "\n" + candidate["body"]
    relation = {**RELATION}
    if scenario == "oversized_proof":
        relation["evidence"] = [
            {**RELATION["evidence"][0], "issue_quote": candidate["body"][:30000]}
        ]
        # Character-count guards would admit this proof; TEXT capacity is bytes.
        proof_json = json.dumps(relation, ensure_ascii=False)
        assert len(proof_json) < 65535 < len(proof_json.encode("utf-8"))
    elif scenario == "valid_removed":
        relation["reason"] = (
            "Removes the faulty immediate failure on transient TLS errors"
        )
        relation["evidence"] = [
            {
                **RELATION["evidence"][0],
                "change": "removed",
                "code_quote": "fail_on_transient_tls()",
            }
        ]

    async def call(**kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"relations": [relation]})
                    )
                )
            ]
        )

    monkeypatch.setattr(pr_verifier, "get_dynamic_config", AsyncMock(return_value=0.85))
    pr = PR(
        "Human\n<!-- sakura-ai-issue-links-start -->Closes #570<!-- sakura-ai-issue-links-end -->"
    )
    if scenario in {"removed_as_added", "valid_removed"}:
        removed = (
            "retry_tls_eof()"
            if scenario == "removed_as_added"
            else "fail_on_transient_tls()"
        )
        pr.get_files = lambda: [
            SimpleNamespace(
                filename="dependency.py",
                status="modified",
                patch=f"@@ -1 +0,0 @@\n-{removed}",
                additions=0,
                deletions=1,
            )
        ]
    original_body = pr.body
    result = await pr_link_sync.PRRelationSyncService(
        retriever=SimpleNamespace(retrieve=AsyncMock(return_value=[candidate])),
        verifier=pr_verifier.PRRelationVerifier(
            configured_client(call_with_retry=call)
        ),
        session_factory=DB,
        linker=linker(),
    ).synchronize(SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618)
    rows = session.scalars(select(PRIssueLink)).all()
    if scenario in {"oversized_proof", "removed_as_added"}:
        assert not result.succeeded
        assert result.failure == (
            "SemanticLinkEvidenceTooLargeError"
            if scenario == "oversized_proof"
            else "ValueError"
        )
        assert result.relations == []
        assert pr.body == original_body and not pr.edits
        assert len(rows) == 1 and rows[0].issue_number == 570
        assert rows[0].inference_reason == "old proof"
    else:
        assert result.succeeded
        assert result.relations[0]["body"] == candidate["body"]
        assert result.relations[0]["content"] == candidate["content"]
        assert "Closes #612" in pr.body and "Closes #570" not in pr.body
        assert len(rows) == 1 and rows[0].issue_number == 612
        stored = json.loads(rows[0].inference_reason)
        assert stored == relation
        assert "body" not in stored and "content" not in stored
        assert len(rows[0].inference_reason.encode("utf-8")) <= 65535
    session.close()
