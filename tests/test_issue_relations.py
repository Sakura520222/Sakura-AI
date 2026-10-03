"""Issue relation decisions must be verified before main analysis/publication."""

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.issue_analyzer import IssueAnalyzer
from backend.services.issue_service import IssueService


async def value(v):
    return v


@pytest.fixture
def main_analyzer(monkeypatch):
    module = importlib.import_module("backend.services.issue_analyzer")
    monkeypatch.setattr(module, "get_user_dynamic_config", lambda *a: value("en"))
    monkeypatch.setattr(module, "get_dynamic_config", lambda key, **k: value(False))
    monkeypatch.setattr(
        "backend.services.label_service.label_service.get_repo_labels",
        lambda *a: value({}),
    )
    monkeypatch.setattr(
        "backend.core.github_app.GitHubAppClient",
        lambda: SimpleNamespace(get_repo_collaborators=lambda *a: []),
    )
    monkeypatch.setattr(
        "backend.services.sakura_memory_service.get_sakura_memory_service",
        lambda: SimpleNamespace(get_sakura_context=lambda **k: value({})),
    )
    analyzer = IssueAnalyzer.__new__(IssueAnalyzer)
    analyzer.api_client = SimpleNamespace(
        resolve_role_candidates=AsyncMock(return_value=[]),
        call_with_retry=AsyncMock(
            return_value=SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5),
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="final", tool_calls=None)
                    )
                ],
            )
        ),
    )
    analyzer.tool_manager = SimpleNamespace(
        get_enabled_tools=AsyncMock(return_value=[])
    )
    analyzer._refresh_runtime_config = lambda: None
    analyzer._parse_or_repair_analysis = AsyncMock(
        return_value={"category": "bug", "duplicate_of": 999}
    )
    return analyzer


@pytest.mark.asyncio
async def test_main_model_invented_duplicate_is_discarded(main_analyzer):
    result = await main_analyzer.analyze_issue(
        {
            "issue_number": 1,
            "title": "Crash",
            "body": "A crashes on input x",
            "state": "open",
        },
        "owner",
        "repo",
    )
    assert result["duplicate_of"] is None


def fail_auxiliary_setting_from_live_db(
    monkeypatch, failed_key, error, cancel_event=None
):
    """Exercise the real fresh AppConfig read against a failing active session."""
    from backend.core import config
    from backend.models import database

    class ActiveSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, statement):
            key = statement.compile().params["key_name_1"]
            if key == failed_key:
                if cancel_event is not None:
                    cancel_event.set()
                raise error
            raw = "true" if key == "issue_detect_duplicates" else "false"
            return SimpleNamespace(one_or_none=lambda: (raw, 1, None))

    monkeypatch.setattr(database, "async_session", ActiveSession)
    monkeypatch.setattr(
        "backend.services.issue_analyzer.get_dynamic_config", config.get_dynamic_config
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["issue_include_comments", "issue_detect_duplicates"])
async def test_auxiliary_live_setting_failure_is_sanitized_and_main_continues(
    main_analyzer, monkeypatch, key
):
    secret = "mysql://private-user:private-password@internal-host"
    fail_auxiliary_setting_from_live_db(monkeypatch, key, RuntimeError(secret))
    inference = AsyncMock(
        side_effect=AssertionError("unknown policy must not admit inference")
    )
    main_analyzer._fetch_issue_comments = AsyncMock(
        side_effect=AssertionError("comments policy unknown or disabled")
    )
    monkeypatch.setattr(
        "backend.services.issue_analyzer.IssueRelationAnalyzer",
        lambda **k: SimpleNamespace(analyze=inference),
    )
    result = await main_analyzer.analyze_issue(
        {
            "issue_number": 1,
            "title": "Crash",
            "body": "A crashes on input x",
            "state": "open",
        },
        "owner",
        "repo",
    )
    assert result["category"] == "bug" and result["duplicate_of"] is None
    assert result["issue_relations"]["status"] == "failed"
    assert result["issue_relations"]["failure"] == "RuntimeError"
    assert (
        result["issue_relations"]["primary"] is None
        and result["issue_relations"]["related"] == []
    )
    assert secret not in json.dumps(result)
    assert secret not in json.dumps(
        main_analyzer.api_client.call_with_retry.call_args.kwargs["messages"]
    )
    assert main_analyzer.api_client.call_with_retry.await_count == 1
    assert inference.await_count == main_analyzer._fetch_issue_comments.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["issue_include_comments", "issue_detect_duplicates"])
async def test_cancel_signal_wins_auxiliary_live_setting_error(
    main_analyzer, monkeypatch, key
):
    from backend.core.ai_protocol.errors import ReviewCancelledError

    event = asyncio.Event()
    fail_auxiliary_setting_from_live_db(
        monkeypatch, key, RuntimeError("database interrupted"), event
    )
    with pytest.raises(ReviewCancelledError):
        await main_analyzer.analyze_issue(
            {
                "issue_number": 1,
                "title": "Crash",
                "body": "A crashes on input x",
                "state": "open",
            },
            "owner",
            "repo",
            cancel_event=event,
        )
    assert main_analyzer.api_client.call_with_retry.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["issue_include_comments", "issue_detect_duplicates"])
@pytest.mark.parametrize("kind", ["async", "domain"])
async def test_auxiliary_setting_cancellation_propagates(
    main_analyzer, monkeypatch, key, kind
):
    from backend.core.ai_protocol.errors import ReviewCancelledError

    error = (
        asyncio.CancelledError()
        if kind == "async"
        else ReviewCancelledError("cancelled")
    )
    fail_auxiliary_setting_from_live_db(monkeypatch, key, error)
    with pytest.raises(type(error)):
        await main_analyzer.analyze_issue(
            {
                "issue_number": 1,
                "title": "Crash",
                "body": "A crashes on input x",
                "state": "open",
            },
            "owner",
            "repo",
        )
    assert main_analyzer.api_client.call_with_retry.await_count == 0


@pytest.mark.asyncio
async def test_main_phase_setting_error_remains_outside_auxiliary_boundary(
    main_analyzer, monkeypatch
):
    from backend.services.issues.relation_analyzer import IssueRelationResult

    error = RuntimeError("main vision policy unavailable")
    fail_auxiliary_setting_from_live_db(monkeypatch, "issue_vision_enabled", error)
    inference = AsyncMock(return_value=IssueRelationResult())
    monkeypatch.setattr(
        "backend.services.issue_analyzer.IssueRelationAnalyzer",
        lambda **k: SimpleNamespace(analyze=inference),
    )
    with pytest.raises(RuntimeError) as caught:
        await main_analyzer.analyze_issue(
            {
                "issue_number": 1,
                "title": "Crash",
                "body": "A crashes on input x",
                "state": "open",
            },
            "owner",
            "repo",
        )
    assert caught.value is error
    assert inference.await_count == 1
    assert main_analyzer.api_client.call_with_retry.await_count == 0


@pytest.mark.asyncio
async def test_prephase_reuses_comments_before_main_and_accounts_cost(
    main_analyzer, setup_relation, monkeypatch
):
    analyzer, current, _, client, _ = setup_relation
    comments = [{"author": "maintainer", "body": "A crashes on input x"}]
    main_analyzer._fetch_issue_comments = AsyncMock(return_value=comments)
    monkeypatch.setattr(
        "backend.services.issue_analyzer.get_dynamic_config",
        lambda key, **k: value(
            key in {"issue_include_comments", "issue_detect_duplicates"}
        ),
    )
    monkeypatch.setattr(
        "backend.services.issue_analyzer.IssueRelationAnalyzer", lambda **k: analyzer
    )
    result = await main_analyzer.analyze_issue(current, "owner", "repo")
    assert result["duplicate_of"] == 2
    assert result["issue_relations"]["status"] == "verified"
    assert result["prompt_tokens"] == 6 and result["completion_tokens"] == 10
    assert main_analyzer._fetch_issue_comments.await_count == 1
    relation_data = json.loads(
        client.call_with_retry.call_args.kwargs["messages"][1]["content"]
    )
    assert relation_data["current"]["comments"] == comments
    assert (
        '"duplicate_of": 2'
        in main_analyzer.api_client.call_with_retry.call_args.kwargs["messages"][1][
            "content"
        ]
    )


@pytest.mark.asyncio
async def test_relation_failure_keeps_main_analysis_running(
    main_analyzer, setup_relation, monkeypatch
):
    analyzer, current, retriever, _, _ = setup_relation
    retriever.retrieve.side_effect = TimeoutError()
    monkeypatch.setattr(
        "backend.services.issue_analyzer.get_dynamic_config",
        lambda key, **k: value(key == "issue_detect_duplicates"),
    )
    monkeypatch.setattr(
        "backend.services.issue_analyzer.IssueRelationAnalyzer", lambda **k: analyzer
    )
    result = await main_analyzer.analyze_issue(current, "owner", "repo")
    assert result["category"] == "bug" and result["duplicate_of"] is None
    assert result["issue_relations"]["failure"] == "TimeoutError"


@pytest.mark.asyncio
async def test_comment_fetch_failure_prevents_duplicate_but_main_continues(
    main_analyzer, setup_relation, monkeypatch
):
    analyzer, current, _, client, _ = setup_relation
    main_analyzer._fetch_issue_comments = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "backend.services.issue_analyzer.get_dynamic_config",
        lambda key, **k: value(
            key in {"issue_include_comments", "issue_detect_duplicates"}
        ),
    )
    monkeypatch.setattr(
        "backend.services.issue_analyzer.IssueRelationAnalyzer", lambda **k: analyzer
    )
    result = await main_analyzer.analyze_issue(current, "owner", "repo")
    assert result["category"] == "bug" and result["duplicate_of"] is None
    assert result["issue_relations"]["failure"] == "comments_unavailable"
    assert client.call_with_retry.call_count == 0


@pytest.mark.asyncio
async def test_real_comment_fetch_distinguishes_error_from_empty(monkeypatch):
    from backend.core.github_app import GitHubAppClient

    app = GitHubAppClient()
    issue = SimpleNamespace(get_comments=list)
    repo = SimpleNamespace(get_issue=lambda *a: issue)
    monkeypatch.setattr(
        app, "get_repo_client", lambda *a: SimpleNamespace(get_repo=lambda *a: repo)
    )
    analyzer = IssueAnalyzer.__new__(IssueAnalyzer)
    assert await analyzer._fetch_issue_comments(app, "owner", "repo", 1) == []

    def failed():
        raise RuntimeError("unavailable")

    issue.get_comments = failed
    assert await analyzer._fetch_issue_comments(app, "owner", "repo", 1) is None


@pytest.mark.asyncio
async def test_strict_comment_fetch_propagates_domain_cancellation(monkeypatch):
    from backend.core.ai_protocol.errors import ReviewCancelledError
    from backend.core.github_app import GitHubAppClient

    app = GitHubAppClient()

    def cancelled():
        raise ReviewCancelledError("cancelled")

    issue = SimpleNamespace(get_comments=cancelled)
    repo = SimpleNamespace(get_issue=lambda *a: issue)
    monkeypatch.setattr(
        app, "get_repo_client", lambda *a: SimpleNamespace(get_repo=lambda *a: repo)
    )
    with pytest.raises(ReviewCancelledError):
        await IssueAnalyzer.__new__(IssueAnalyzer)._fetch_issue_comments(
            app, "owner", "repo", 1
        )


@pytest.mark.asyncio
async def test_disabled_comments_are_not_fetched_or_injected(
    main_analyzer, setup_relation, monkeypatch
):
    analyzer, current, _, client, _ = setup_relation
    main_analyzer._fetch_issue_comments = AsyncMock(
        side_effect=AssertionError("comments disabled")
    )
    monkeypatch.setattr(
        "backend.services.issue_analyzer.get_dynamic_config",
        lambda key, **k: value(key == "issue_detect_duplicates"),
    )
    monkeypatch.setattr(
        "backend.services.issue_analyzer.IssueRelationAnalyzer", lambda **k: analyzer
    )
    result = await main_analyzer.analyze_issue(current, "owner", "repo")
    assert result["duplicate_of"] == 2
    assert main_analyzer._fetch_issue_comments.await_count == 0
    sent = json.loads(client.call_with_retry.call_args.kwargs["messages"][1]["content"])
    assert sent["current"]["comments"] == []


def candidate(number=2, state="open", **overrides):
    return {
        "number": number,
        "title": "Crash",
        "body": "A crashes on input x",
        "state": state,
        "state_reason": "completed" if state == "closed" else None,
        "labels": ["bug"],
        "similarity": 0.99,
        **overrides,
    }


def decision(kind="duplicate", number=2, **overrides):
    return {
        "number": number,
        "relation": kind,
        "confidence": 0.99,
        "reason": "Same input and failure",
        "similarities": ["Same crash"],
        "differences": [],
        "evidence": [
            {
                "current_quote": "A crashes on input x",
                "candidate_quote": "A crashes on input x",
            }
        ],
        **overrides,
    }


def response(relations):
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=json.dumps({"relations": relations}))
            )
        ],
    )


@pytest.fixture
def setup_relation(monkeypatch):
    module = importlib.import_module("backend.services.issues.relation_analyzer")
    values = {
        "issue_relation_max_candidates": 5,
        "issue_include_comments": True,
        "issue_relation_max_input_tokens": 64000,
        "issue_relation_similarity_threshold": 0.75,
        "issue_relation_confidence_threshold": 0.85,
        "issue_duplicate_confidence_threshold": 0.95,
    }

    async def config(key, **kwargs):
        assert kwargs.get("fresh") is True
        return values[key]

    monkeypatch.setattr(module, "get_dynamic_config", config)
    monkeypatch.setattr(
        "backend.services.issues.issue_budget.get_dynamic_config", config
    )
    retriever = SimpleNamespace(retrieve=AsyncMock(side_effect=[[candidate()], []]))
    from tests.test_pr_issue_budget import summary_candidate

    client = SimpleNamespace(
        resolve_role_candidates=AsyncMock(return_value=[summary_candidate()]),
        call_with_retry=AsyncMock(return_value=response([decision()])),
    )
    analyzer = module.IssueRelationAnalyzer(retriever=retriever, client=client)
    current = {
        "issue_number": 1,
        "title": "Crash",
        "body": "A crashes on input x",
        "state": "open",
    }
    return analyzer, current, retriever, client, values


@pytest.mark.asyncio
async def test_compatibility_helper_rejects_semantic_only_hit(monkeypatch):
    service = IssueService.__new__(IssueService)
    monkeypatch.setattr(
        service,
        "_issue_embedding_service",
        SimpleNamespace(search_related_issues=AsyncMock(return_value=[candidate()])),
    )
    monkeypatch.setattr(
        service, "github_app", SimpleNamespace(search_issues=lambda *a: [])
    )
    # High cosine alone is not proof of duplication.
    assert (
        await service.detect_duplicates(
            "owner", "repo", "Crash", "different occurrence", 1
        )
        == []
    )


@pytest.mark.asyncio
async def test_open_duplicate_is_primary_and_skips_closed(setup_relation):
    analyzer, current, retriever, client, _ = setup_relation
    result = await analyzer.analyze("owner", "repo", current)
    assert result.duplicate_of == 2
    assert result.status == "verified"
    assert result.primary["relation"] == "duplicate"
    assert retriever.retrieve.call_count == 1
    assert client.call_with_retry.call_args.kwargs["role"] == "summary"
    assert result.prompt_tokens == 3 and result.completion_tokens == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "previously_resolved",
        "regression",
        "duplicate_closed",
        "previously_rejected",
        "related",
    ],
)
async def test_closed_relation_is_history_never_duplicate(setup_relation, kind):
    analyzer, current, retriever, client, _ = setup_relation
    retriever.retrieve.side_effect = [[], [candidate(state="closed")]]
    client.call_with_retry.return_value = response([decision(kind)])
    result = await analyzer.analyze("owner", "repo", current)
    assert result.duplicate_of is None
    assert result.primary["relation"] == kind
    assert result.primary["state_reason"] == "completed"
    assert [c.kwargs["state"] for c in retriever.retrieve.call_args_list] == [
        "open",
        "closed",
    ]


@pytest.mark.asyncio
async def test_actual_gitflow_occurrences_remain_related(setup_relation):
    analyzer, _, retriever, client, _ = setup_relation
    prior, current = json.loads(
        (Path(__file__).parent / "fixtures/issue_relation_gitflow.json").read_text()
    )
    prior = {
        **prior,
        "number": prior["issue_number"],
        "labels": [x["name"] for x in prior["labels"]],
    }
    retriever.retrieve.side_effect = [[], [prior]]
    current_run = current["body"].split("Workflow run: ")[1]
    prior_run = prior["body"].split("Workflow run: ")[1]
    assert current_run != prior_run
    client.call_with_retry.return_value = response(
        [
            decision(
                "related",
                581,
                reason="Same workflow, distinct executions and handling",
                differences=["Different workflow runs"],
                evidence=[{"current_quote": current_run, "candidate_quote": prior_run}],
            )
        ]
    )
    result = await analyzer.analyze(
        "owner", "repo", current, comments=current["comments"]
    )
    assert result.duplicate_of is None and result.primary["relation"] == "related"
    sent = json.loads(client.call_with_retry.call_args.kwargs["messages"][1]["content"])
    assert sent["current"]["comments"] == current["comments"]
    assert sent["candidates"][0]["state_reason"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"number": 1},
        {"number": 999},
        {"confidence": True},
        {"confidence": float("nan")},
        {"evidence": []},
        {"reason": ""},
        {"differences": None},
        {"evidence": [{"current_quote": "invented", "candidate_quote": "Crash"}]},
        {"relation": "duplicate_closed"},
    ],
)
async def test_invalid_protocol_fails_closed(setup_relation, override):
    analyzer, current, _, client, _ = setup_relation
    client.call_with_retry.return_value = response([decision(**override)])
    result = await analyzer.analyze("owner", "repo", current)
    assert result.duplicate_of is None and result.status == "failed" and result.failure
    assert result.primary is None and result.related == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError("private token")])
async def test_provider_failure_fails_closed(setup_relation, failure):
    analyzer, current, _, client, _ = setup_relation
    client.call_with_retry.side_effect = failure
    result = await analyzer.analyze("owner", "repo", current)
    assert result.duplicate_of is None and result.failure == type(failure).__name__
    assert "private token" not in json.dumps(result.to_dict())


@pytest.mark.asyncio
async def test_retrieval_failure_and_incomplete_facts_fail_closed(setup_relation):
    analyzer, current, retriever, _, _ = setup_relation
    retriever.retrieve.side_effect = RuntimeError("reranker unavailable")
    assert (await analyzer.analyze("owner", "repo", current)).status == "failed"
    retriever.retrieve.side_effect = [[{"number": 2}], []]
    assert (await analyzer.analyze("owner", "repo", current)).duplicate_of is None


@pytest.mark.asyncio
async def test_cancellation_propagates(setup_relation):
    analyzer, current, retriever, _, _ = setup_relation
    retriever.retrieve.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await analyzer.analyze("owner", "repo", current)


@pytest.mark.asyncio
async def test_cancel_signal_wins_provider_error(setup_relation):
    from backend.core.ai_protocol.errors import ReviewCancelledError

    analyzer, current, _, client, _ = setup_relation
    event = asyncio.Event()

    async def failed(**kwargs):
        event.set()
        raise RuntimeError("provider interrupted")

    client.call_with_retry.side_effect = failed
    with pytest.raises(ReviewCancelledError):
        await analyzer.analyze("owner", "repo", current, cancel_event=event)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ["garbage", '{"relations": []}', '{"relations": null}', '{"relations": [{}]}'],
)
async def test_malformed_or_incomplete_decisions_fail_closed(setup_relation, content):
    analyzer, current, _, client, _ = setup_relation
    client.call_with_retry.return_value.choices[0].message.content = content
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "failed" and result.duplicate_of is None
    assert result.prompt_tokens == 3


@pytest.mark.asyncio
async def test_none_then_historical_relation_and_observer_context(setup_relation):
    analyzer, current, retriever, client, _ = setup_relation
    retriever.retrieve.side_effect = [[candidate()], [candidate(3, state="closed")]]
    client.call_with_retry.side_effect = [
        response([decision("none")]),
        response([decision("regression", 3)]),
    ]
    context, observer = object(), object()
    result = await analyzer.analyze(
        "owner", "repo", current, context=context, observer=observer
    )
    assert result.duplicate_of is None and result.primary["number"] == 3
    assert result.prompt_tokens == 6 and result.completion_tokens == 10
    assert all(
        c.kwargs["context"] is context and c.kwargs["observer"] is observer
        for c in client.call_with_retry.call_args_list
    )


@pytest.mark.asyncio
async def test_soft_deadline_skips_auxiliary_calls_without_hard_timeout(setup_relation):
    analyzer, current, retriever, client, _ = setup_relation
    deadline = SimpleNamespace(is_expired=lambda: True)
    result = await analyzer.analyze("owner", "repo", current, deadline=deadline)
    assert result.status == "skipped" and result.failure == "deadline"
    assert retriever.retrieve.call_count == client.call_with_retry.call_count == 0


@pytest.mark.asyncio
async def test_deadline_crossed_in_open_phase_skips_closed_recall(setup_relation):
    analyzer, current, retriever, client, _ = setup_relation
    expired = False
    deadline = SimpleNamespace(is_expired=lambda: expired)

    async def complete(**kwargs):
        nonlocal expired
        expired = True
        return response([decision("related")])

    client.call_with_retry.side_effect = complete
    result = await analyzer.analyze("owner", "repo", current, deadline=deadline)
    assert result.duplicate_of is None and result.status == "skipped"
    assert retriever.retrieve.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {"number": 1},
        {"pull_request": {}},
        {"state": "closed"},
        {"labels": None},
        {"state_reason": False},
    ],
)
async def test_invalid_candidate_facts_are_not_sent_to_model(setup_relation, invalid):
    analyzer, current, retriever, client, _ = setup_relation
    retriever.retrieve.side_effect = [[candidate(**invalid)], []]
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "failed" and result.duplicate_of is None
    assert client.call_with_retry.call_count == 0


@pytest.mark.asyncio
async def test_source_state_reason_is_preserved_without_new_enum_assumption(
    setup_relation,
):
    analyzer, current, retriever, client, _ = setup_relation
    retriever.retrieve.side_effect = [
        [],
        [candidate(state="closed", state_reason="source_new_reason")],
    ]
    client.call_with_retry.return_value = response([decision("related")])
    result = await analyzer.analyze("owner", "repo", current)
    assert result.primary["state_reason"] == "source_new_reason"


@pytest.mark.asyncio
async def test_low_confidence_does_not_create_duplicate(setup_relation):
    analyzer, current, _, client, values = setup_relation
    values["issue_duplicate_confidence_threshold"] = 1.0
    result = await analyzer.analyze("owner", "repo", current)
    assert result.duplicate_of is None
    assert client.call_with_retry.call_count == 1


def test_additive_relation_column_migration_keeps_old_duplicate():
    from sqlalchemy import create_engine, text

    from backend.models.database import IssueAnalysis, _build_add_column_sql

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE issue_analyses (id INTEGER PRIMARY KEY, duplicate_of BIGINT)"
            )
        )
        conn.execute(text("INSERT INTO issue_analyses VALUES (1, 42)"))
        column = IssueAnalysis.__table__.c.get("issue_relations")
        assert column is not None
        conn.execute(
            text(_build_add_column_sql(conn.dialect, "issue_analyses", column))
        )
        assert conn.execute(
            text("SELECT duplicate_of, issue_relations FROM issue_analyses")
        ).one() == (42, None)
    engine.dispose()


@pytest.mark.asyncio
async def test_persist_relations_with_task_bound_id_and_cancelled_guard():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from backend.models.database import IssueAnalysis

    engine = create_engine("sqlite:///:memory:")
    IssueAnalysis.__table__.create(engine)
    session = Session(engine)
    record = IssueAnalysis(
        issue_number=1, repo_owner="owner", repo_name="repo", status="analyzing"
    )
    session.add(record)
    session.commit()

    class AsyncFacade:
        async def execute(self, statement):
            return session.execute(statement)

        async def commit(self):
            session.commit()

        async def refresh(self, item):
            session.refresh(item)

    relations = {
        "status": "verified",
        "primary": decision("regression"),
        "related": [],
        "duplicate_of": None,
    }
    result = await IssueService().save_analysis_result(
        {"summary": "Current", "issue_relations": relations},
        {"issue_number": 1, "repo_owner": "owner", "repo_name": "repo"},
        AsyncFacade(),
        analysis_id=record.id,
    )
    assert result is not None
    assert json.loads(record.issue_relations) == relations
    assert record.duplicate_of is None
    record.status = "cancelled"
    session.commit()
    assert (
        await IssueService().save_analysis_result(
            {"issue_relations": {}},
            {"issue_number": 1, "repo_owner": "owner", "repo_name": "repo"},
            AsyncFacade(),
            analysis_id=record.id,
        )
        is None
    )
    assert json.loads(record.issue_relations) == relations
    session.close()
    engine.dispose()


def test_api_serializes_relation_object_and_tolerates_legacy_null():
    from backend.api.v1.schemas import IssueAnalysisResponse

    data = {
        "id": 1,
        "issue_number": 1,
        "issue_relations": json.dumps({"status": "verified", "duplicate_of": None}),
    }
    assert IssueAnalysisResponse.model_validate(data).model_dump()[
        "issue_relations"
    ] == {"status": "verified", "duplicate_of": None}
    assert (
        IssueAnalysisResponse.model_validate({"id": 1, "issue_number": 1}).model_dump()[
            "issue_relations"
        ]
        is None
    )


@pytest.mark.asyncio
async def test_api_list_and_detail_return_history_and_keep_owner_scope(monkeypatch):
    from backend.api.v1 import issues as routes
    from backend.models.database import IssueAnalysis

    record = IssueAnalysis(
        id=1,
        issue_number=1,
        repo_owner="owner",
        repo_name="repo",
        issue_relations=json.dumps(
            {"status": "verified", "primary": decision("regression")}
        ),
    )
    statements = []
    scope = IssueAnalysis.repo_owner == "owner"
    monkeypatch.setattr(routes, "build_user_scope_filter", lambda *a: scope)

    class Session:
        async def execute(self, stmt):
            statements.append(stmt)
            return SimpleNamespace(scalar_one_or_none=lambda: record)

    async def paginate(db, query, count_query, page, per_page):
        statements.extend([query, count_query])
        return [record], 1, 1, page

    monkeypatch.setattr(routes, "paginate", paginate)
    detail = await routes.get_issue(1, db=Session(), user={})
    listing = await routes.list_issues(
        db=Session(),
        user={},
        search="",
        repo_name="",
        category="",
        priority="",
        status="",
        page=1,
        per_page=20,
    )
    assert (
        json.loads(detail.body)["data"]["issue_relations"]["primary"]["relation"]
        == "regression"
    )
    assert (
        json.loads(listing.body)["data"]["items"][0]["issue_relations"]["primary"][
            "relation"
        ]
        == "regression"
    )
    assert all("repo_owner" in str(s.whereclause) for s in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
async def test_reanalysis_retains_known_issue_state_for_relation_prephase(
    monkeypatch, surface
):
    from backend.api.v1 import issues as api_routes
    from backend.models.database import IssueAnalysis
    from backend.webui.routes import issues as webui_routes

    record = IssueAnalysis(
        id=1,
        issue_number=1,
        repo_owner="owner",
        repo_name="repo",
        title="Current",
        body="Raw body",
        issue_state="open",
    )

    class Session:
        async def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: record, scalar=lambda: 1)

    submit = AsyncMock(return_value="task")
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )
    module = api_routes if surface == "api" else webui_routes
    monkeypatch.setattr(module, "build_user_scope_filter", lambda *a: None)
    if surface == "api":
        await module.reanalyze_issue(1, db=Session(), user={})
    else:
        await module.reanalyze_issue(SimpleNamespace(), 1, db=Session(), user={})
    assert submit.call_args.args[0]["state"] == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "language,label", [("en", "Regression"), ("zh-CN", "回归问题")]
)
async def test_detail_page_renders_historical_evidence_and_escapes_quotes(
    monkeypatch, language, label
):
    from starlette.requests import Request

    from backend.models.database import IssueAnalysis
    from backend.webui.routes import issues as routes

    record = IssueAnalysis(
        id=1,
        issue_number=1,
        repo_owner="owner",
        repo_name="repo",
        title="Current",
        issue_relations=json.dumps(
            {
                "status": "verified",
                "primary": decision(
                    "regression",
                    evidence=[
                        {
                            "current_quote": "<script>current</script>",
                            "candidate_quote": "prior fix",
                        }
                    ],
                ),
                "related": [],
            }
        ),
    )

    class Session:
        async def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: record)

    monkeypatch.setattr(routes, "build_user_scope_filter", lambda *a: None)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/issues/1",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )
    result = await routes.issue_detail_page(
        request,
        1,
        db=Session(),
        user={"role": "super_admin", "sub": "1", "username": "tester"},
        user_prefs={"language": language},
    )
    html = result.body.decode()
    assert label in html and "prior fix" in html and "#2" in html
    assert "&lt;script&gt;current&lt;/script&gt;" in html


@pytest.mark.parametrize(
    "language,label", [("en", "Regression"), ("zh-CN", "回归问题")]
)
def test_comment_exposes_history_without_duplicate_warning(
    monkeypatch, language, label
):
    from backend.models.database import IssueAnalysis
    from backend.services import issue_service as module

    monkeypatch.setattr(
        module, "get_settings", lambda: SimpleNamespace(output_language=language)
    )
    monkeypatch.setattr(
        module,
        "get_strategy_config",
        lambda: SimpleNamespace(
            get_issue_analysis_config=lambda: {"comment_template": "{summary}"}
        ),
    )
    record = IssueAnalysis(
        summary="Current",
        issue_relations=json.dumps(
            {"status": "verified", "primary": decision("regression"), "related": []}
        ),
    )
    comment = IssueService().build_analysis_comment(record)
    assert label in comment and "#2" in comment
    assert "Same input and failure" in comment
    assert "Duplicate" not in comment and "重复" not in comment


@pytest.mark.asyncio
async def test_real_budget_failure_preserves_usage_and_main_analysis(
    setup_relation, main_analyzer, monkeypatch
):
    analyzer, current, retriever, client, values = setup_relation
    retriever.retrieve.side_effect = [[candidate()], [candidate(state="closed")]]

    async def summary(**kwargs):
        values["issue_relation_max_input_tokens"] = 1
        return response([decision("related")])

    client.call_with_retry.side_effect = summary
    monkeypatch.setattr(
        "backend.services.issue_analyzer.IssueRelationAnalyzer", lambda **k: analyzer
    )
    monkeypatch.setattr(
        "backend.services.issue_analyzer.get_dynamic_config",
        lambda key, **_: value(key == "issue_detect_duplicates"),
    )
    result = await main_analyzer.analyze_issue(current, "owner", "repo")
    assert result["category"] == "bug" and result["duplicate_of"] is None
    assert result["issue_relations"]["failure"] == "input_budget"
    assert result["issue_relations"]["primary"] is None
    assert result["issue_relations"]["related"] == []
    assert result["prompt_tokens"] == 6 and result["completion_tokens"] == 10
    assert main_analyzer.api_client.call_with_retry.await_count == 1
