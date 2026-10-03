"""PR relation workload bounds preserve optional review and existing evidence."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.core.ai_protocol.models import (
    AuthScheme,
    ModelCapabilitySet,
    ModelMetadata,
    ProtocolFamily,
    ProviderDeclaration,
    ReasoningParams,
    ResolvedModel,
)
from backend.core.ai_protocol.registry import resolve_endpoint
from backend.models.database import PRIssueLink
from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.issues import pr_link_sync, pr_verifier
from tests.test_pr_issue_relations import CANDIDATE, FILES, RELATION, linker


def summary_candidate(context=128000, output=4096):
    provider = ProviderDeclaration(
        id="configured",
        label="Configured",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://configured.test/v1",
        auth_scheme=AuthScheme.BEARER,
    )
    return ResolvedModel(
        provider=provider,
        model=ModelMetadata(
            model_id="configured-summary",
            provider_id=provider.id,
            display_name="Configured summary",
            context_window_tokens=context,
            max_output_tokens=output,
            capabilities=ModelCapabilitySet(),
            reasoning_params=ReasoningParams(max_output_tokens=output),
        ),
        endpoint=resolve_endpoint(provider, None),
        credential="test-only",
    )


@pytest.fixture
def budgets(monkeypatch):
    from backend.core import config

    values = {
        "pr_issue_max_files": 128,
        "pr_issue_max_input_tokens": 64000,
        "semantic_issue_max_links": 5,
        "semantic_issue_similarity_threshold": 0.8,
        "pr_issue_related_confidence_threshold": 0.85,
        "pr_issue_closing_confidence_threshold": 0.95,
    }
    read = AsyncMock(side_effect=lambda key, **_: values[key])
    monkeypatch.setattr(config, "get_dynamic_config", read)
    monkeypatch.setattr(pr_verifier, "get_dynamic_config", read)
    # The helper may already be imported by another focused regression module.
    import sys

    helper = sys.modules.get("backend.services.issues.pr_budget")
    if helper:
        monkeypatch.setattr(helper, "get_dynamic_config", read)
    resolve = AsyncMock(return_value=[summary_candidate()])
    monkeypatch.setattr(AIApiClient, "resolve_role_candidates", resolve)
    return values, resolve, read


def changed_file(patch=None):
    return SimpleNamespace(
        filename="dependency.py",
        status="modified",
        patch=FILES[0]["patch"] if patch is None else patch,
        additions=1,
        deletions=1,
    )


class Pull:
    head = SimpleNamespace(sha="head")
    base = SimpleNamespace(sha="base")
    title = "TLS retry"

    def __init__(self, files, count=1):
        self.body = "Human\n<!-- sakura-ai-issue-links-start -->Closes #570<!-- sakura-ai-issue-links-end -->"
        self.changed_files = count
        self.get_files = files
        self.edits = []

    def edit(self, *, body):
        self.edits.append(body)
        self.body = body


@pytest.fixture
def sync_harness(budgets):
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

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, statement):
            return session.execute(statement)

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

    call = AsyncMock(
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
    client = AIApiClient()
    client.call_with_retry = call
    retriever = SimpleNamespace(retrieve=AsyncMock(return_value=[CANDIDATE]))
    service = pr_link_sync.PRRelationSyncService(
        retriever=retriever,
        verifier=pr_verifier.PRRelationVerifier(client),
        session_factory=DB,
        linker=linker(),
    )
    yield service, session, call, retriever
    session.close()
    engine.dispose()


def assert_old_state(pr, session):
    assert not pr.edits and "Closes #570" in pr.body
    rows = session.scalars(select(PRIssueLink)).all()
    assert len(rows) == 1 and rows[0].inference_reason == "old proof"


@pytest.mark.asyncio
async def test_lazy_snapshot_does_not_request_file_after_configured_limit(
    sync_harness, budgets
):
    service, session, call, retriever = sync_harness
    budgets[0]["pr_issue_max_files"] = 1
    consumed = []

    def files():
        consumed.append(1)
        yield changed_file()
        raise AssertionError("requested another GitHub page beyond workload cap")

    pr = Pull(files, count=2)
    result = await service.synchronize(
        SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618
    )
    assert not result.succeeded and result.failure == "snapshot_incomplete"
    assert consumed == [1]
    call.assert_not_awaited()
    retriever.retrieve.assert_not_awaited()
    assert_old_state(pr, session)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["huge_patch", "missing_patch", "missing_file"])
async def test_incomplete_snapshot_preserves_links_and_cannot_close(
    sync_harness, budgets, mode
):
    service, session, call, _ = sync_harness
    budgets[0]["pr_issue_max_input_tokens"] = 2000
    patch = "@@ -1 +1 @@\n-old\n+" + "x" * 1000000 if mode == "huge_patch" else ""
    pr = Pull(lambda: [changed_file(patch)], count=2 if mode == "missing_file" else 1)
    result = await service.synchronize(
        SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618
    )
    assert not result.succeeded
    assert result.failure in {"snapshot_incomplete", "input_budget"}
    call.assert_not_awaited()
    assert_old_state(pr, session)


@pytest.mark.asyncio
async def test_snapshot_and_final_request_read_dynamic_limits_each_run(
    sync_harness, budgets
):
    service, session, call, _ = sync_harness
    pr = Pull(lambda: [changed_file()])
    budgets[0]["pr_issue_max_input_tokens"] = 1
    first = await service.synchronize(
        SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618
    )
    assert not first.succeeded and first.failure == "input_budget"
    assert_old_state(pr, session)
    budgets[0]["pr_issue_max_input_tokens"] = 64000
    second = await service.synchronize(
        SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618
    )
    assert second.succeeded and second.relations[0]["relation"] == "closes"
    assert "Closes #612" in pr.body
    call.assert_awaited_once()
    assert all(c.kwargs.get("fresh") for c in budgets[2].await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["patch", "description", "issue", "labels"])
async def test_direct_verifier_rejects_final_serialized_over_budget_input(
    budgets, part
):
    budgets[0]["pr_issue_max_input_tokens"] = 1000
    call = AsyncMock()
    client = AIApiClient()
    client.call_with_retry = call
    candidate = {**CANDIDATE}
    files = [dict(FILES[0])]
    body = "Human"
    enormous = "a" * 10000
    if part == "patch":
        files[0]["patch"] = enormous
    elif part == "description":
        body = enormous
    else:
        candidate["body" if part == "issue" else "labels"] = (
            enormous if part == "issue" else [enormous]
        )
    result = await pr_verifier.PRRelationVerifier(client).verify(
        pr_title="PR",
        pr_body=body,
        candidates=[candidate],
        files=files,
    )
    assert not result.succeeded and result.failure == "input_budget"
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_summary_fallback_small_context_and_output_reserve_bound_request(budgets):
    # Primary fits, fallback would overflow including configured output + system.
    budgets[1].return_value = [
        summary_candidate(),
        summary_candidate(context=1200, output=800),
    ]
    call = AsyncMock()
    client = AIApiClient()
    client.call_with_retry = call
    result = await pr_verifier.PRRelationVerifier(client).verify(
        pr_title="PR",
        pr_body="Human",
        candidates=[CANDIDATE],
        files=FILES,
    )
    assert not result.succeeded and result.failure == "input_budget"
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_summary_metadata_preserves_links(sync_harness, budgets):
    service, session, call, _ = sync_harness
    budgets[1].return_value = []
    pr = Pull(lambda: (_ for _ in ()))
    result = await service.synchronize(
        SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618
    )
    assert not result.succeeded and result.failure == "budget_unavailable"
    call.assert_not_awaited()
    assert_old_state(pr, session)


@pytest.mark.asyncio
async def test_complete_under_budget_empty_verification_clears_old_links(sync_harness):
    service, session, call, _ = sync_harness
    call.return_value.choices[0].message.content = '{"relations":[]}'
    pr = Pull(lambda: [changed_file()])
    result = await service.synchronize(
        SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618
    )
    assert result.succeeded and result.relations == []
    assert not session.scalars(select(PRIssueLink)).all()
    assert "Closes #570" not in pr.body


@pytest.mark.asyncio
async def test_soft_deadline_between_file_reads_stops_pagination(sync_harness):
    service, session, call, _ = sync_harness
    expired = False

    def files():
        nonlocal expired
        yield changed_file()
        expired = True
        yield changed_file()
        raise AssertionError("continued after soft deadline")

    pr = Pull(files, count=3)
    deadline = SimpleNamespace(is_expired=lambda: expired)
    result = await service.synchronize(
        SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618, deadline=deadline
    )
    assert not result.succeeded and result.failure == "deadline"
    call.assert_not_awaited()
    assert_old_state(pr, session)


@pytest.mark.asyncio
async def test_task_cancellation_drains_active_read_and_never_continues_iterator(
    sync_harness,
):
    service, session, call, _ = sync_harness
    started, release = threading.Event(), threading.Event()
    consumed = []

    def files():
        started.set()
        assert release.wait(3)
        consumed.append(1)
        yield changed_file()
        consumed.append(2)
        raise AssertionError("background enumeration after caller cancellation")

    pr = Pull(files, count=2)
    task = asyncio.create_task(
        service.synchronize(SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618)
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert consumed == [1]
        call.assert_not_awaited()
        assert_old_state(pr, session)
    finally:
        release.set()


@pytest.mark.asyncio
async def test_final_budget_includes_json_escaping_and_system_overhead(budgets):
    from backend.core.ai_protocol.models import UnifiedMessage
    from backend.core.ai_protocol.request_policy import estimate_unified_messages
    from backend.services.issues.pr_budget import PRInputBudget

    client = AIApiClient()
    client.call_with_retry = AsyncMock()
    # Raw source alone is below the allowance. JSON escaping plus the system
    # instructions makes the actual serialized request exceed it.
    files = [{**FILES[0], "patch": '"' * 1500}]
    budget = PRInputBudget(128, 64000)
    messages = budget.messages(
        pr_title="PR", pr_body="Human", files=files, candidates=[CANDIDATE]
    )
    estimate = estimate_unified_messages([UnifiedMessage(**m) for m in messages])
    assert estimate > 1500 // 4
    budgets[0]["pr_issue_max_input_tokens"] = estimate - 1
    result = await pr_verifier.PRRelationVerifier(client).verify(
        pr_title="PR",
        pr_body="Human",
        candidates=[CANDIDATE],
        files=files,
    )
    assert not result.succeeded and result.failure == "input_budget"
    client.call_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_verifier_rechecks_changed_summary_metadata_after_snapshot(
    sync_harness, budgets
):
    service, session, call, retriever = sync_harness

    async def retrieve(*args, **kwargs):
        budgets[1].return_value = [summary_candidate(context=1000, output=800)]
        return [CANDIDATE]

    retriever.retrieve.side_effect = retrieve
    pr = Pull(lambda: [changed_file()])
    result = await service.synchronize(
        SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618
    )
    assert not result.succeeded and result.failure == "input_budget"
    assert budgets[1].await_count == 2
    call.assert_not_awaited()
    assert_old_state(pr, session)


@pytest.mark.asyncio
async def test_domain_cancel_between_reads_does_not_read_next_file(sync_harness):
    from backend.core.ai_protocol.errors import ReviewCancelledError

    service, session, call, _ = sync_harness
    event = asyncio.Event()

    def files():
        event.set()
        yield changed_file()
        raise AssertionError("continued after domain cancellation")

    pr = Pull(files, count=2)
    with pytest.raises(ReviewCancelledError):
        await service.synchronize(
            SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618, cancel_event=event
        )
    call.assert_not_awaited()
    assert_old_state(pr, session)


# Real Worker fixture keeps the pipeline through its main review boundary.
@pytest.fixture
def worker_for_budget(monkeypatch):
    from tests.test_review_worker_timeout import pr_relation_runtime_worker

    return pr_relation_runtime_worker.__wrapped__(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["snapshot_incomplete", "budget_unavailable", "input_budget"]
)
async def test_actual_worker_continues_main_review_after_optional_budget_failure(
    monkeypatch,
    sync_harness,
    budgets,
    worker_for_budget,
    failure,
):
    from backend.workers import review_worker

    service, session, call, _ = sync_harness
    worker, info, context, _ = worker_for_budget
    if failure == "snapshot_incomplete":
        budgets[0]["pr_issue_max_files"] = 1

        def files():
            yield changed_file()
            raise AssertionError("Worker exhausted optional file pages")

        pr = Pull(files, count=2)
    elif failure == "budget_unavailable":
        budgets[1].return_value = []
        pr = Pull(lambda: [changed_file()])
    else:
        budgets[0]["pr_issue_max_input_tokens"] = 1
        pr = Pull(lambda: [changed_file()])
    worker.github_app = SimpleNamespace(
        get_repo_client=lambda *_: SimpleNamespace(
            get_repo=lambda _: SimpleNamespace(get_pull=lambda _: pr)
        )
    )
    monkeypatch.setattr(pr_link_sync, "PRRelationSyncService", lambda: service)
    monkeypatch.setattr(
        review_worker, "get_dynamic_config", AsyncMock(return_value=True)
    )
    await worker.process_review_task(info)
    assert context["pr_issue_relation_status"] == failure
    worker.ai_reviewer.review_pr.assert_awaited_once()
    call.assert_not_awaited()
    assert_old_state(pr, session)


@pytest.mark.asyncio
async def test_snapshot_stops_before_next_page_when_input_budget_is_exhausted(
    sync_harness, budgets
):
    from backend.core.ai_protocol.models import UnifiedMessage
    from backend.core.ai_protocol.request_policy import estimate_unified_messages
    from backend.services.issues.pr_budget import PRInputBudget

    service, session, call, _ = sync_harness
    first = {**FILES[0], "status": "modified", "complete": False}
    messages = PRInputBudget(128, 64000).messages(
        pr_title="TLS retry",
        pr_body="Human",
        files=[first],
        candidates=[],
    )
    budgets[0]["pr_issue_max_input_tokens"] = estimate_unified_messages(
        [UnifiedMessage(**m) for m in messages]
    )
    consumed = []

    def files():
        consumed.append(1)
        yield changed_file()
        consumed.append(2)
        raise AssertionError("requested another page after input budget reached")

    pr = Pull(files, count=2)
    result = await service.synchronize(
        SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618
    )
    assert not result.succeeded and result.failure == "input_budget"
    assert consumed == [1]
    call.assert_not_awaited()
    assert_old_state(pr, session)
