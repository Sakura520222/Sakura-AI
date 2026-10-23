"""Invalid relation configuration never authorizes replacement or unbounded recall."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.core import config
from backend.core.ai_protocol.errors import (
    AllCandidatesFailedError,
    ReviewCancelledError,
)
from backend.models import database
from backend.models.database import AppConfig, PRIssueLink
from backend.services.issues import pr_verifier, relation_analyzer
from backend.services.issues.pr_link_sync import PRRelationSyncService
from tests.issue_source_fixtures import complete_source
from tests.test_issue_relations import main_analyzer as _main_analyzer
from tests.test_pr_issue_budget import Pull, changed_file
from tests.test_pr_issue_relations import (
    CANDIDATE,
    FILES,
    RELATION,
    configured_client,
    linker,
    repo_with_candidate,
)

main_analyzer = _main_analyzer
THRESHOLDS = (
    "pr_issue_related_confidence_threshold",
    "pr_issue_closing_confidence_threshold",
)


def model_response(relations):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=json.dumps({"relations": relations}))
            )
        ]
    )


@pytest.fixture
def config_db(monkeypatch):
    """Exercise real fresh config reads while keeping SQL and settings local."""
    engine = create_engine("sqlite:///:memory:")
    AppConfig.__table__.create(engine)
    PRIssueLink.__table__.create(engine)
    session = Session(engine)
    settings = config.Settings()
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    transactions = []

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
            transactions.append("flush")
            session.flush()

        async def commit(self):
            transactions.append("commit")
            session.commit()

        async def rollback(self):
            transactions.append("rollback")
            session.rollback()

    def store(key, value):
        session.add(AppConfig(key_name=key, key_value=value))
        session.commit()

    monkeypatch.setattr(database, "async_session", DB)
    yield SimpleNamespace(
        session=session,
        factory=DB,
        settings=settings,
        store=store,
        transactions=transactions,
    )
    session.close()
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", THRESHOLDS)
@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), -float("inf"), -0.1, 1.1, True, False, None, "bad"],
)
@pytest.mark.parametrize("candidates", [[], [CANDIDATE]])
async def test_invalid_pr_threshold_fails_before_provider_or_empty_success(
    config_db, monkeypatch, key, bad, candidates
):
    values = {THRESHOLDS[0]: 0.85, THRESHOLDS[1]: 0.95, key: bad}
    monkeypatch.setattr(
        pr_verifier,
        "get_dynamic_config",
        AsyncMock(side_effect=lambda name, **_: values[name]),
    )
    client = configured_client(
        call_with_retry=AsyncMock(return_value=model_response([]))
    )
    result = await pr_verifier.PRRelationVerifier(client).verify(
        pr_title="TLS retry", pr_body="Human", candidates=candidates, files=FILES
    )
    assert not result.succeeded and result.failure == "ValueError"
    assert result.relations == []
    client.resolve_role_candidates.assert_not_awaited()
    client.call_with_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["related", "closes"])
@pytest.mark.parametrize("source", ["stored", "settings"])
@pytest.mark.parametrize(
    "threshold,confidence,accepted",
    [(0, 0.0, True), (1, 1.0, True), (1.0, 0.99, False), (0.85, 0.85, True)],
)
async def test_valid_pr_threshold_inclusive_boundaries(
    config_db, kind, source, threshold, confidence, accepted
):
    key = THRESHOLDS[kind == "closes"]
    if source == "stored":
        config_db.store(key, str(threshold))
    else:
        settings = config.Settings(**{key: str(threshold)})
        setattr(config_db.settings, key, getattr(settings, key))
    relation = {**RELATION, "relation": kind, "confidence": confidence}
    client = configured_client(
        call_with_retry=AsyncMock(return_value=model_response([relation]))
    )
    result = await pr_verifier.PRRelationVerifier(client).verify(
        pr_title="TLS retry", pr_body="Human", candidates=[CANDIDATE], files=FILES
    )
    assert result.succeeded and bool(result.relations) is accepted
    client.call_with_retry.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", THRESHOLDS)
@pytest.mark.parametrize("bad", [True, False, float("nan")])
@pytest.mark.parametrize("candidates", [[], [CANDIDATE]])
async def test_direct_settings_assignment_cannot_bypass_pr_threshold_guard(
    config_db, key, bad, candidates
):
    setattr(config_db.settings, key, bad)
    client = configured_client(
        call_with_retry=AsyncMock(
            return_value=model_response([{**RELATION, "confidence": 0.0}])
        )
    )
    result = await pr_verifier.PRRelationVerifier(client).verify(
        pr_title="TLS retry", pr_body="Human", candidates=candidates, files=FILES
    )
    assert not result.succeeded and result.failure == "ValueError"
    assert result.relations == []
    client.call_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_empty_pr_candidates_require_no_provider(config_db):
    client = configured_client(call_with_retry=AsyncMock())
    result = await pr_verifier.PRRelationVerifier(client).verify(
        pr_title="TLS retry", pr_body="Human", candidates=[], files=FILES
    )
    assert result.succeeded and result.relations == []
    client.resolve_role_candidates.assert_not_awaited()
    client.call_with_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["cancel", "deadline", "both"])
@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.parametrize("candidates", [[], [CANDIDATE]])
async def test_pr_control_signal_wins_coincident_config_failure(
    config_db, monkeypatch, control, raises, candidates
):
    event = asyncio.Event()
    state = {"expired": False}
    deadline = SimpleNamespace(is_expired=lambda: state["expired"])

    async def read(*args, **kwargs):
        if control in {"cancel", "both"}:
            event.set()
        state["expired"] = control in {"deadline", "both"}
        if raises:
            raise RuntimeError("private config failure")
        return float("nan")

    monkeypatch.setattr(pr_verifier, "get_dynamic_config", read)
    client = configured_client(call_with_retry=AsyncMock())
    call = pr_verifier.PRRelationVerifier(client).verify(
        pr_title="TLS retry",
        pr_body="Human",
        candidates=candidates,
        files=FILES,
        cancel_event=event,
        deadline=deadline,
        raise_configuration_error=True,
    )
    if control == "deadline":
        result = await call
        assert not result.succeeded and result.failure == "deadline"
    else:
        with pytest.raises(ReviewCancelledError):
            await call
    client.call_with_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_valid_thresholds_preserve_missing_model_configuration_boundary(
    config_db, legacy
):
    client = SimpleNamespace(
        resolve_role_candidates=AsyncMock(return_value=[]), call_with_retry=AsyncMock()
    )
    call = pr_verifier.PRRelationVerifier(client).verify(
        pr_title="TLS retry",
        pr_body="Human",
        candidates=[CANDIDATE],
        files=FILES,
        raise_configuration_error=legacy,
    )
    if legacy:
        with pytest.raises(AllCandidatesFailedError):
            await call
    else:
        result = await call
        assert not result.succeeded and result.failure == "budget_unavailable"
    client.call_with_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", THRESHOLDS)
@pytest.mark.parametrize("stored", ["nan", "inf", "-0.1", "1.1", "true", "bad"])
@pytest.mark.parametrize("candidates", [[], [CANDIDATE]])
async def test_stored_invalid_pr_threshold_preserves_real_sync_body_and_links(
    config_db, key, stored, candidates
):
    config_db.store(key, stored)
    config_db.session.add(
        PRIssueLink(
            repo_name="o/r",
            pr_id=618,
            issue_number=570,
            link_type="semantic",
            reference_text="Closes #570",
            inference_reason="existing proof",
        )
    )
    config_db.session.commit()
    pr = Pull(lambda: [changed_file()])
    body_before = pr.body
    client = configured_client(
        call_with_retry=AsyncMock(return_value=model_response([]))
    )
    service = PRRelationSyncService(
        retriever=SimpleNamespace(retrieve=AsyncMock(return_value=candidates)),
        verifier=pr_verifier.PRRelationVerifier(client),
        session_factory=config_db.factory,
        linker=linker(),
    )
    result = await service.synchronize(repo_with_candidate(pr), "o", "r", 618)
    assert not result.succeeded and result.failure == "ValueError"
    assert pr.body == body_before and pr.edits == []
    row = config_db.session.scalar(select(PRIssueLink))
    assert (row.issue_number, row.reference_text, row.inference_reason) == (
        570,
        "Closes #570",
        "existing proof",
    )
    assert config_db.transactions == []
    client.call_with_retry.assert_not_awaited()


@pytest.mark.parametrize("bad", [0, -1, 201, 1000000000, "201"])
def test_settings_reject_candidate_limit_outside_operational_bounds(bad):
    with pytest.raises(ValidationError):
        config.Settings(issue_relation_max_candidates=bad)


@pytest.mark.parametrize("key", [*THRESHOLDS, "issue_relation_max_candidates"])
@pytest.mark.parametrize("bad", [True, False])
def test_settings_do_not_coerce_boolean_relation_configuration(key, bad):
    with pytest.raises(ValidationError):
        config.Settings(**{key: bad})


@pytest.mark.parametrize("key", THRESHOLDS)
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.1])
def test_settings_reject_invalid_pr_confidence(key, bad):
    with pytest.raises(ValidationError):
        config.Settings(**{key: bad})


@pytest.fixture
def issue_guard(config_db):
    config_db.store("issue_include_comments", "false")
    current = {
        "issue_number": 1,
        "title": "Crash",
        "body": "A crashes on input x",
        "state": "open",
    }
    source = SimpleNamespace(read=AsyncMock(return_value=complete_source(current)))
    retriever = SimpleNamespace(retrieve=AsyncMock(return_value=[]))
    client = configured_client(call_with_retry=AsyncMock())
    analyzer = relation_analyzer.IssueRelationAnalyzer(retriever, client, source)
    return analyzer, current


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["stored", "settings_assignment"])
@pytest.mark.parametrize("bad", [0, -1, 201, 1000000000, True, 1.5, float("nan")])
async def test_invalid_candidate_limit_fails_before_source_or_recall(
    config_db, issue_guard, monkeypatch, source, bad
):
    if source == "stored":
        config_db.store("issue_relation_max_candidates", str(bad))
        # A previously cached good value must not hide a newly invalid DB override.
        monkeypatch.setitem(
            config._dynamic_config_cache,
            "issue_relation_max_candidates",
            ("5", float("inf")),
        )
    else:
        # Startup loading and old code can assign directly, bypassing Pydantic.
        config_db.settings.issue_relation_max_candidates = bad
    analyzer, current = issue_guard
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "failed" and result.failure == "ValueError"
    assert (
        result.duplicate_of is None and result.primary is None and result.related == []
    )
    analyzer.source_reader.read.assert_not_awaited()
    analyzer.retriever.retrieve.assert_not_awaited()
    analyzer.client.call_with_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 5, 200])
@pytest.mark.parametrize("source", ["stored", "settings"])
async def test_valid_candidate_limits_reach_both_phases_unchanged(
    config_db, issue_guard, source, limit
):
    if source == "stored":
        config_db.store("issue_relation_max_candidates", str(limit))
    else:
        config_db.settings.issue_relation_max_candidates = config.Settings(
            issue_relation_max_candidates=limit
        ).issue_relation_max_candidates
    analyzer, current = issue_guard
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "verified" and result.failure is None
    assert [
        call.kwargs["top_k"] for call in analyzer.retriever.retrieve.await_args_list
    ] == [
        limit,
        limit,
    ]
    assert [
        call.kwargs["state"] for call in analyzer.retriever.retrieve.await_args_list
    ] == [
        "open",
        "closed",
    ]


@pytest.mark.asyncio
async def test_absent_candidate_override_uses_default_five(issue_guard):
    analyzer, current = issue_guard
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "verified"
    assert [
        call.kwargs["top_k"] for call in analyzer.retriever.retrieve.await_args_list
    ] == [
        5,
        5,
    ]


@pytest.mark.asyncio
async def test_stored_oversized_limit_keeps_main_analysis_running(
    config_db, issue_guard, main_analyzer, monkeypatch
):
    config_db.store("issue_relation_max_candidates", "1000000000")
    analyzer, current = issue_guard
    monkeypatch.setattr(
        "backend.services.issue_analyzer.IssueRelationAnalyzer", lambda **_: analyzer
    )
    monkeypatch.setattr(
        "backend.services.issue_analyzer.get_dynamic_config",
        AsyncMock(side_effect=lambda key, **_: key == "issue_detect_duplicates"),
    )
    result = await main_analyzer.analyze_issue(current, "owner", "repo")
    assert result["category"] == "bug" and result["duplicate_of"] is None
    assert result["issue_relations"]["status"] == "failed"
    assert result["issue_relations"]["failure"] == "ValueError"
    assert result["issue_relations"]["primary"] is None
    assert result["issue_relations"]["related"] == []
    analyzer.source_reader.read.assert_not_awaited()
    analyzer.retriever.retrieve.assert_not_awaited()
    main_analyzer.api_client.call_with_retry.assert_awaited_once()
