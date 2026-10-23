"""Reject unsafe recall budgets before starting candidate work."""

import asyncio
from collections import OrderedDict
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.core import config
from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.models import database
from backend.services.issues import candidate_retriever, relation_analyzer
from backend.services.issues.candidate_retriever import IssueCandidateRetriever
from backend.services.issues.issue_source_freshness import IssueSourceReader
from backend.services.issues.relation_runtime import RelationDeadlineExceeded
from tests import (
    test_issue_candidate_foundation,
    test_issue_relations,
    test_pr_candidate_freshness,
    test_pr_issue_budget,
)

foundation = test_issue_candidate_foundation.foundation
freshness = test_pr_candidate_freshness.freshness
budgets = test_pr_issue_budget.budgets
main_analyzer = test_issue_relations.main_analyzer
_read_dynamic_config = config.get_dynamic_config
_KEY = "issue_candidate_pool_multiplier"


@pytest.fixture
def dynamic_config(foundation, monkeypatch):
    """Keep parsing/cache/Settings real, with AppConfig stored in SQLite."""
    engine = create_engine("sqlite:///:memory:")
    database.AppConfig.__table__.create(engine)
    session = Session(engine)
    settings = config.Settings()
    controls = SimpleNamespace(before_read=None)

    class AsyncConfigSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, statement):
            if controls.before_read is not None:
                controls.before_read()
            return session.execute(statement)

    monkeypatch.setattr(database, "async_session", AsyncConfigSession)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(config, "_dynamic_config_cache", OrderedDict())
    monkeypatch.setattr(candidate_retriever, "get_dynamic_config", _read_dynamic_config)
    yield SimpleNamespace(settings=settings, session=session, controls=controls)
    session.close()
    engine.dispose()


def _store_multiplier(dynamic_config, value):
    dynamic_config.session.add(database.AppConfig(key_name=_KEY, key_value=value))
    dynamic_config.session.commit()


async def _retrieve(service, **overrides):
    return await IssueCandidateRetriever(service).retrieve(
        "owner",
        "repo",
        **{
            "text": "query",
            "state": "all",
            "exclude_numbers": [],
            "top_k": 2,
            "similarity_threshold": 0.8,
            **overrides,
        },
    )


def _assert_no_candidate_work(foundation):
    service, collection, repo, _ = foundation
    assert repo.queries == []
    assert collection.docs == {}
    assert collection.metadata == {"repo_full_name": "owner/repo_issues"}
    service._vector_store.get_or_create_collection.assert_not_awaited()
    service._embedding_service.embed_texts.assert_not_awaited()
    service._embedding_service.embed_query.assert_not_awaited()
    service._reranker_service.rerank.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize(
    "value", [-1, 0, 11, 10**30, True, False, 3.0, "3", None, float("nan")]
)
async def test_invalid_assigned_multiplier_rejects_before_candidate_work(
    foundation, dynamic_config, value, empty
):
    service, _, repo, _ = foundation
    if empty:
        repo.rows.clear()
    dynamic_config.settings.issue_candidate_pool_multiplier = value
    with pytest.raises(ValueError, match="candidate pool multiplier"):
        await _retrieve(service)
    _assert_no_candidate_work(foundation)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("stored", ["-1", "0", "11", "1000000000", "1.5", "true", ""])
async def test_invalid_stored_multiplier_bypasses_stale_cache_before_work(
    foundation, dynamic_config, stored, empty
):
    service, _, repo, _ = foundation
    if empty:
        repo.rows.clear()
    _store_multiplier(dynamic_config, stored)
    config._dynamic_config_cache[_KEY] = ("3", config.monotonic() + 3600)
    assert await _read_dynamic_config(_KEY) == 3
    with pytest.raises(ValueError, match="candidate pool multiplier"):
        await _retrieve(service)
    _assert_no_candidate_work(foundation)


@pytest.mark.asyncio
@pytest.mark.parametrize("multiplier,expected_pool", [(1, 2), (3, 6), (10, 20)])
@pytest.mark.parametrize("overreturned", [False, True])
async def test_allowed_multipliers_bound_actual_recall_and_hydration(
    foundation, dynamic_config, monkeypatch, multiplier, expected_pool, overreturned
):
    service, collection, repo, values = foundation
    repo.rows = [test_issue_candidate_foundation.issue(i) for i in range(1, 32)]
    await service.index_repo_issues("owner", "repo")
    values["issue_corpus_freshness_seconds"] = 3600
    _store_multiplier(dynamic_config, str(multiplier))
    config._dynamic_config_cache[_KEY] = ("1000000000", config.monotonic() + 3600)
    hydrated = Mock(wraps=repo.get_issue)
    monkeypatch.setattr(repo, "get_issue", hydrated)
    original_query = collection.query
    requests = []

    def query(**kwargs):
        requests.append(deepcopy(kwargs))
        return original_query(
            **(kwargs | {"n_results": 31}) if overreturned else kwargs
        )

    monkeypatch.setattr(collection, "query", query)
    results = await _retrieve(service, exclude_numbers=list(range(1000, 2000)))
    assert [row["number"] for row in results] == [1, 2]
    assert [request["n_results"] for request in requests] == [expected_pool]
    assert [call.args[0] for call in hydrated.call_args_list] == list(
        range(1, expected_pool + 1)
    )
    rerank_request = service._reranker_service.rerank.await_args.kwargs
    assert [row["number"] for row in rerank_request["docs"]] == list(
        range(1, expected_pool + 1)
    )
    assert rerank_request["strict"] is True
    assert rerank_request["top_k"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["domain", "deadline", "both"])
@pytest.mark.parametrize("outcome", ["valid", "invalid", "error"])
async def test_configuration_read_preserves_pending_control_priority(
    foundation, dynamic_config, signal, outcome
):
    event = asyncio.Event()
    deadline = test_issue_candidate_foundation.MutableDeadline()
    failure = RuntimeError("configuration unavailable")
    dynamic_config.settings.issue_candidate_pool_multiplier = (
        3 if outcome == "valid" else 0
    )

    def signal_during_read():
        if signal in {"domain", "both"}:
            event.set()
        if signal in {"deadline", "both"}:
            deadline.expired = True
        if outcome == "error":
            raise failure

    dynamic_config.controls.before_read = signal_during_read
    expected = (
        RelationDeadlineExceeded if signal == "deadline" else ReviewCancelledError
    )
    with pytest.raises(expected) as captured:
        await _retrieve(foundation[0], cancel_event=event, deadline=deadline)
    if outcome == "error":
        assert captured.value.__cause__ is failure
    _assert_no_candidate_work(foundation)


@pytest.mark.asyncio
async def test_configuration_read_failure_is_not_empty_corpus_success(
    foundation, dynamic_config
):
    foundation[2].rows.clear()
    failure = RuntimeError("configuration unavailable")

    def fail_read():
        raise failure

    dynamic_config.controls.before_read = fail_read
    with pytest.raises(RuntimeError) as captured:
        await _retrieve(foundation[0])
    assert captured.value is failure
    _assert_no_candidate_work(foundation)


@pytest.mark.asyncio
@pytest.mark.parametrize("read_fails", [False, True])
async def test_real_task_cancellation_drains_config_read_before_propagating(
    foundation, monkeypatch, read_fails
):
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = RuntimeError("configuration unavailable")

    async def read_config(*args, **kwargs):
        started.set()
        await release.wait()
        finished.set()
        if read_fails:
            raise failure
        return 11

    monkeypatch.setattr(candidate_retriever, "get_dynamic_config", read_config)
    task = asyncio.create_task(_retrieve(foundation[0]))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as captured:
            await task
    assert finished.is_set()
    if read_fails:
        assert captured.value.__cause__ is failure
    _assert_no_candidate_work(foundation)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_real_pr_sync_preserves_previous_links_when_pool_config_is_invalid(
    freshness, foundation, dynamic_config, monkeypatch, empty
):
    service, _, repo, _ = foundation
    if empty:
        repo.rows.clear()
    _store_multiplier(dynamic_config, "1000000000")
    monkeypatch.setattr(config, "get_dynamic_config", _read_dynamic_config)
    freshness.service.retriever = IssueCandidateRetriever(service)
    result = await freshness.service.synchronize(freshness.repo, "o", "r", 618)
    assert not result.succeeded
    assert result.failure == "ValueError"
    test_pr_candidate_freshness.assert_preserved(freshness)
    assert freshness.transactions == []
    _assert_no_candidate_work(foundation)


@pytest.mark.asyncio
async def test_real_issue_prephase_reports_invalid_pool_and_main_analysis_continues(
    main_analyzer, foundation, dynamic_config, monkeypatch
):
    service, _, repo, _ = foundation
    repo.rows = [
        test_issue_candidate_foundation.issue(
            99, title="Current issue", body="Human problem"
        )
    ]
    _store_multiplier(dynamic_config, "1000000000")
    dynamic_config.settings.issue_include_comments = False
    monkeypatch.setattr(relation_analyzer, "get_dynamic_config", _read_dynamic_config)
    monkeypatch.setattr(
        "backend.services.issue_analyzer.get_dynamic_config",
        lambda key, **kwargs: test_issue_relations.value(
            key == "issue_detect_duplicates"
        ),
    )
    monkeypatch.setattr(
        "backend.services.issue_analyzer.IssueRelationAnalyzer",
        lambda **kwargs: relation_analyzer.IssueRelationAnalyzer(
            retriever=IssueCandidateRetriever(service),
            source_reader=IssueSourceReader(repo=repo),
            **kwargs,
        ),
    )
    result = await main_analyzer.analyze_issue(
        {
            "issue_number": 99,
            "title": "Current issue",
            "body": "Human problem",
            "state": "open",
        },
        "owner",
        "repo",
    )
    assert result["category"] == "bug"
    assert result["duplicate_of"] is None
    assert result["issue_relations"]["status"] == "failed"
    assert result["issue_relations"]["failure"] == "ValueError"
    assert result["issue_relations"]["primary"] is None
    assert result["issue_relations"]["related"] == []
    assert main_analyzer.api_client.call_with_retry.await_count == 1
    _assert_no_candidate_work(foundation)
