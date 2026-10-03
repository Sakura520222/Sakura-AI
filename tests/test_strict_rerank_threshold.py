"""Invalid rerank policy cannot become a successful empty relation replacement."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.core.config import Settings
from backend.models.database import PRIssueLink
from backend.services import ai_usage_service, embedding_service
from backend.services.issues.candidate_retriever import IssueCandidateRetriever
from backend.services.issues.issue_source_freshness import IssueSourceReader
from backend.services.issues.pr_link_sync import PRRelationSyncService
from backend.services.issues.pr_verifier import PRRelationVerifier
from backend.services.issues.relation_analyzer import IssueRelationAnalyzer
from tests import test_issue_candidate_foundation, test_issue_relations
from tests.test_pr_issue_budget import Pull, changed_file, summary_candidate
from tests.test_pr_issue_relations import linker
from tests.test_relation_configuration_guards import config_db as _config_db
from tests.test_strict_rerank_contract import install_retrieval_provider

config_db = _config_db
foundation = test_issue_candidate_foundation.foundation
main_analyzer = test_issue_relations.main_analyzer


@pytest_asyncio.fixture
async def threshold_provider(monkeypatch):
    settings = Settings(
        _env_file=None,
        rerank_provider="siliconflow",
        rerank_base_url="https://rerank.invalid",
        rerank_api_key="test-only",
        rerank_model="model",
        rerank_top_k=2,
        rerank_score_threshold=0.6,
    )
    response = {
        "results": [
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.8},
        ],
        "usage": {"total_tokens": 11},
    }
    requests, usage = [], []
    state = SimpleNamespace(error=None)

    def send(request):
        requests.append(json.loads(request.content))
        if state.error is not None:
            raise state.error
        return httpx.Response(200, json=response)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(embedding_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        embedding_service.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(send)),
    )

    async def record(**kwargs):
        usage.append(kwargs)

    monkeypatch.setattr(ai_usage_service, "record_ai_usage_best_effort", record)
    service = embedding_service.RerankerService()
    try:
        yield SimpleNamespace(
            service=service,
            settings=settings,
            response=response,
            requests=requests,
            usage=usage,
            state=state,
        )
    finally:
        await service.close()


INVALID_THRESHOLDS = [
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="positive-infinity"),
    pytest.param(-float("inf"), id="negative-infinity"),
    pytest.param(-0.1, id="negative"),
    pytest.param(1.1, id="above-one"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param("0.6", id="numeric-string"),
    pytest.param("bad", id="text"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", INVALID_THRESHOLDS)
@pytest.mark.parametrize("source", ["settings_assignment", "explicit", "private"])
async def test_strict_invalid_threshold_fails_before_http_or_usage(
    threshold_provider, source, bad
):
    case = threshold_provider
    docs = [{"content": "first"}, {"content": "second"}]
    if source == "settings_assignment":
        case.settings.rerank_score_threshold = bad
        call = case.service.rerank("query", docs, strict=True)
    elif source == "explicit":
        call = case.service.rerank("query", docs, score_threshold=bad, strict=True)
    else:
        call = case.service._rerank_via_siliconflow("query", docs, 2, bad, strict=True)

    with pytest.raises(ValueError, match="score_threshold"):
        await call
    assert case.requests == []
    assert case.usage == []


@pytest.mark.asyncio
async def test_strict_invalid_default_none_is_not_an_empty_success(threshold_provider):
    case = threshold_provider
    case.settings.rerank_score_threshold = None
    with pytest.raises(ValueError, match="score_threshold"):
        await case.service.rerank(
            "query", [{"content": "first"}, {"content": "second"}], strict=True
        )
    assert case.requests == [] and case.usage == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("threshold", "scores", "expected"),
    [
        (0, [0.0, 1.0], ["first", "second"]),
        (0.6, [0.6, 0.59], ["first"]),
        (1, [1.0, 0.99], ["first"]),
        (1.0, [0.9, 0.8], []),
    ],
)
@pytest.mark.parametrize("source", ["settings_assignment", "explicit"])
async def test_valid_threshold_has_inclusive_filtering_and_usage(
    threshold_provider, source, threshold, scores, expected
):
    case = threshold_provider
    case.response["results"] = [
        {"index": index, "relevance_score": score} for index, score in enumerate(scores)
    ]
    kwargs = {}
    if source == "settings_assignment":
        case.settings.rerank_score_threshold = threshold
    else:
        # A valid explicit override is independent of an unused invalid default.
        case.settings.rerank_score_threshold = float("nan")
        kwargs["score_threshold"] = threshold
    result = await case.service.rerank(
        "query", [{"content": "first"}, {"content": "second"}], strict=True, **kwargs
    )
    assert [doc["content"] for doc in result] == expected
    assert case.requests == [
        {
            "model": "model",
            "query": "query",
            "documents": ["first", "second"],
            "top_n": 2,
        }
    ]
    assert len(case.usage) == 1
    assert case.usage[0]["usage"] == case.response
    assert case.usage[0]["input_only"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_empty_input_does_not_consume_unused_invalid_threshold(
    threshold_provider, strict
):
    case = threshold_provider
    case.settings.rerank_score_threshold = float("nan")
    assert await case.service.rerank("query", [], strict=strict) == []
    assert case.requests == [] and case.usage == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("top_k", "expected"), [(0, []), (1, ["first"])])
async def test_disabled_strict_reranker_ignores_unused_invalid_threshold(
    threshold_provider, top_k, expected
):
    case = threshold_provider
    case.settings.rerank_provider = "none"
    case.settings.rerank_score_threshold = float("nan")
    result = await case.service.rerank(
        "query", [{"content": "first"}, {"content": "second"}], top_k=top_k, strict=True
    )
    assert [doc["content"] for doc in result] == expected
    assert case.requests == [] and case.usage == []


@pytest.mark.asyncio
async def test_non_strict_rag_retains_provider_failure_fallback(threshold_provider):
    case = threshold_provider
    case.state.error = httpx.ConnectError("test provider unavailable")
    docs = [{"content": "first"}, {"content": "second"}]
    assert await case.service.rerank("query", docs, top_k=1) == docs[:1]
    assert len(case.requests) == 1 and case.usage == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strict", "error_type"),
    [
        (False, asyncio.CancelledError),
        (True, asyncio.CancelledError),
        (True, ReviewCancelledError),
    ],
)
async def test_threshold_guard_preserves_provider_cancellation(
    threshold_provider, strict, error_type
):
    case = threshold_provider
    case.state.error = error_type()
    with pytest.raises(error_type):
        await case.service.rerank("query", [{"content": "first"}], strict=strict)
    assert len(case.requests) == 1 and case.usage == []


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [float("nan"), 1.1, 1.0])
async def test_rerank_threshold_controls_real_pr_replacement(
    config_db, foundation, threshold_provider, monkeypatch, threshold
):
    case = threshold_provider
    service = install_retrieval_provider(
        foundation, (case.service, case.response, case.usage), monkeypatch, 2
    )
    case.settings.rerank_score_threshold = threshold
    case.response["results"] = [
        {"index": 0, "relevance_score": 0.9},
        {"index": 1, "relevance_score": 0.8},
    ]
    config_db.session.add(
        PRIssueLink(
            repo_name="owner/repo",
            pr_id=99,
            issue_number=570,
            link_type="semantic",
            reference_text="Closes #570",
            inference_reason="existing proof",
        )
    )
    config_db.session.commit()
    pr = Pull(lambda: [changed_file()])
    old_body = pr.body

    async def unexpected_inference(**kwargs):
        pytest.fail("Invalid or all-low rerank must not call verification AI")

    client = SimpleNamespace(
        resolve_role_candidates=AsyncMock(return_value=[summary_candidate()]),
        call_with_retry=unexpected_inference,
    )
    result = await PRRelationSyncService(
        retriever=IssueCandidateRetriever(service),
        verifier=PRRelationVerifier(client),
        session_factory=config_db.factory,
        linker=linker(),
    ).synchronize(SimpleNamespace(get_pull=lambda _: pr), "owner", "repo", 99)
    rows = config_db.session.scalars(select(PRIssueLink)).all()
    if threshold == 1.0:
        assert result.succeeded and result.relations == []
        assert rows == [] and "#570" not in pr.body and pr.edits
        assert len(case.requests) == len(case.usage) == 1
    else:
        assert not result.succeeded and result.failure == "ValueError"
        assert pr.body == old_body and pr.edits == []
        assert [
            (row.issue_number, row.reference_text, row.inference_reason) for row in rows
        ] == [(570, "Closes #570", "existing proof")]
        assert config_db.transactions == []
        assert case.requests == [] and case.usage == []


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [float("nan"), 1.1])
async def test_invalid_rerank_threshold_fails_issue_prephase_but_main_continues(
    main_analyzer, foundation, threshold_provider, monkeypatch, threshold
):
    case = threshold_provider
    service = install_retrieval_provider(
        foundation, (case.service, case.response, case.usage), monkeypatch, 2
    )
    case.settings.rerank_score_threshold = threshold
    repo = foundation[2]
    repo.rows.append(
        test_issue_candidate_foundation.issue(
            99, title="Current issue", body="Human problem"
        )
    )
    monkeypatch.setattr(
        "backend.services.issue_analyzer.get_dynamic_config",
        lambda key, **kwargs: test_issue_relations.value(
            key == "issue_detect_duplicates"
        ),
    )
    monkeypatch.setattr(
        "backend.services.issue_analyzer.IssueRelationAnalyzer",
        lambda **kwargs: IssueRelationAnalyzer(
            retriever=IssueCandidateRetriever(service),
            source_reader=IssueSourceReader(repo=repo),
            **kwargs,
        ),
    )
    main_analyzer.api_client.resolve_role_candidates.return_value = [
        summary_candidate()
    ]
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
    assert result["category"] == "bug" and result["duplicate_of"] is None
    assert result["issue_relations"]["status"] == "failed"
    assert result["issue_relations"]["failure"] == "ValueError"
    assert result["issue_relations"]["related"] == []
    assert main_analyzer.api_client.call_with_retry.await_count == 1
    assert case.requests == [] and case.usage == []
