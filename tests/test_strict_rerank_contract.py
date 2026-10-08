"""Strict rerank completeness distinguishes provider failure from low scores."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.models.database import PRIssueLink
from backend.services import ai_usage_service, embedding_service
from backend.services.issues.candidate_retriever import IssueCandidateRetriever
from backend.services.issues.pr_link_sync import PRRelationSyncService
from backend.services.issues.pr_verifier import PRRelationVerifier
from backend.services.issues.relation_analyzer import IssueRelationAnalyzer
from tests import test_issue_candidate_foundation, test_issue_relations
from tests.test_ai_effective_request_policy import _candidate
from tests.test_pr_issue_relations import linker

foundation = test_issue_candidate_foundation.foundation
main_analyzer = test_issue_relations.main_analyzer


@pytest.fixture
def provider(monkeypatch):
    service = embedding_service.RerankerService.__new__(
        embedding_service.RerankerService
    )
    service.provider = "siliconflow"
    response = {"results": [], "usage": {"total_tokens": 11}}
    service.client = SimpleNamespace(
        post=AsyncMock(
            return_value=SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: response
            )
        )
    )
    service._refresh_client = lambda: None
    monkeypatch.setattr(
        embedding_service,
        "get_settings",
        lambda: SimpleNamespace(
            rerank_top_k=5, rerank_score_threshold=0.6, rerank_model="model"
        ),
    )
    usage = []
    service.billing_attempt_results = []

    async def record(**kwargs):
        service.billing_attempt_results.append(kwargs)
        if kwargs["usage"] is not None:
            usage.append(kwargs)

    # Capture the actual response boundary introduced by durable billing;
    # these tests still assert successful API usage survives invalid ranking.
    monkeypatch.setattr(
        ai_usage_service, "begin_ai_call", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(ai_usage_service, "finish_ai_call", record)
    return service, response, usage


@pytest.mark.asyncio
@pytest.mark.parametrize(("top_k", "result_count"), [(2, 0), (2, 1), (2, 3), (5, 2)])
async def test_strict_rerank_rejects_wrong_count_before_filtering(
    provider, top_k, result_count
):
    service, response, usage = provider
    docs = [{"content": title} for title in ("first", "second", "third")]
    response["results"] = [
        {"index": index, "relevance_score": 0.1} for index in range(result_count)
    ]
    with pytest.raises(ValueError, match="Malformed"):
        await service.rerank("query", docs, top_k=top_k, strict=True)
    # Successful API usage is accounted even when its result is inadmissible.
    assert len(usage) == 1
    assert usage[0]["usage"] == response
    assert usage[0]["input_only"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("top_k", [2, 5, None])
async def test_complete_strict_rerank_can_filter_every_score(provider, top_k):
    service, response, _ = provider
    response["results"] = [
        {"index": 0, "relevance_score": 0.1},
        {"index": 1, "relevance_score": 0.2},
    ]
    assert (
        await service.rerank(
            "query",
            [{"content": "first"}, {"content": "second"}],
            top_k=top_k,
            strict=True,
        )
        == []
    )


@pytest.mark.asyncio
async def test_strict_rerank_accepts_requested_subset_of_more_documents(provider):
    service, response, _ = provider
    response["results"] = [
        {"index": 2, "relevance_score": 0.9},
        {"index": 0, "relevance_score": 0.8},
    ]
    docs = [{"content": title} for title in ("first", "second", "third")]
    assert await service.rerank("query", docs, top_k=2, strict=True) == [
        docs[2],
        docs[0],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(("top_k", "expected_count"), [(1, 1), (5, 2), (None, 2)])
async def test_siliconflow_request_maps_public_limit_to_top_n(
    provider, strict, top_k, expected_count
):
    service, response, _ = provider
    response["results"] = [
        {"index": index, "relevance_score": 0.9} for index in range(expected_count)
    ]
    await service.rerank(
        "query",
        [{"content": "first"}, {"content": "second"}],
        top_k=top_k,
        strict=strict,
    )
    assert service.client.post.call_args.kwargs["json"] == {
        "model": "model",
        "query": "query",
        "documents": ["first", "second"],
        "top_n": expected_count,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("top_k", [0, -1, True, 1.5])
async def test_configured_strict_reranker_requires_positive_integer_limit(
    provider, top_k
):
    service, _, usage = provider
    with pytest.raises(ValueError):
        await service.rerank("query", [{"content": "first"}], top_k=top_k, strict=True)
    assert usage == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("top_k", "expected"), [(0, []), (-1, ["first"])])
async def test_non_strict_reranker_keeps_nonpositive_limit_behavior(
    provider, top_k, expected
):
    service, response, _ = provider
    response["results"] = [
        {"index": 0, "relevance_score": 0.9},
        {"index": 1, "relevance_score": 0.8},
    ]
    results = await service.rerank(
        "query", [{"content": "first"}, {"content": "second"}], top_k=top_k
    )
    assert [doc["content"] for doc in results] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second",
    [
        {"index": 0, "relevance_score": 0.7},
        {"index": 5, "relevance_score": 0.7},
        {"index": 1, "relevance_score": True},
        {"index": 1, "relevance_score": float("inf")},
    ],
)
async def test_complete_count_does_not_bypass_strict_item_validation(provider, second):
    service, response, _ = provider
    response["results"] = [{"index": 0, "relevance_score": 0.9}, second]
    with pytest.raises(ValueError, match="Malformed"):
        await service.rerank(
            "query", [{"content": "first"}, {"content": "second"}], strict=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("results", "expected"),
    [([], []), ([{"index": 1, "relevance_score": 0.9}], ["second"])],
)
async def test_non_strict_rag_keeps_partial_provider_results(
    provider, results, expected
):
    service, response, _ = provider
    response["results"] = results
    docs = [{"content": "first"}, {"content": "second"}]
    assert [doc["content"] for doc in await service.rerank("query", docs)] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("top_k", "expected"), [(1, ["first"]), (0, [])])
async def test_disabled_reranker_keeps_strict_passthrough(provider, top_k, expected):
    service, _, usage = provider
    service.provider = "none"
    service.client = None
    docs = [{"content": "first"}, {"content": "second"}]
    results = await service.rerank("query", docs, top_k=top_k, strict=True)
    assert [doc["content"] for doc in results] == expected
    assert usage == []


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_rerank_provider_cancellation_propagates(provider, strict):
    service, _, usage = provider
    service.client.post.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.rerank("query", [{"content": "first"}], strict=strict)
    assert usage == []
    assert len(service.billing_attempt_results) == 1
    assert service.billing_attempt_results[0]["usage"] is None
    assert service.billing_attempt_results[0]["outcome"] == "cancelled"


def install_retrieval_provider(foundation, provider, monkeypatch, result_count):
    service, _, repo, settings = foundation
    service._reranker_service = provider[0]
    repo.rows = [test_issue_candidate_foundation.issue(i) for i in (1, 2)]
    provider[1]["results"] = [
        {"index": index, "relevance_score": 0.1} for index in range(result_count)
    ]
    settings.update(
        semantic_issue_max_links=2,
        semantic_issue_similarity_threshold=0.8,
        issue_include_comments=False,
        issue_relation_max_input_tokens=64_000,
        issue_relation_max_candidates=2,
        issue_relation_similarity_threshold=0.8,
        issue_relation_confidence_threshold=0.85,
        issue_duplicate_confidence_threshold=0.95,
        pr_issue_max_files=128,
        pr_issue_max_input_tokens=64_000,
    )
    from backend.services.issues import issue_budget, pr_budget, relation_analyzer

    monkeypatch.setattr(
        relation_analyzer,
        "get_dynamic_config",
        AsyncMock(side_effect=lambda key, **_: settings[key]),
    )
    monkeypatch.setattr(
        issue_budget,
        "get_dynamic_config",
        AsyncMock(side_effect=lambda key, **_: settings[key]),
    )
    monkeypatch.setattr(
        pr_budget,
        "get_dynamic_config",
        AsyncMock(side_effect=lambda key, **_: settings[key]),
    )
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("result_count", [0, 1, 2])
async def test_strict_provider_completeness_controls_real_pr_replacement(
    foundation, provider, monkeypatch, result_count
):
    service = install_retrieval_provider(
        foundation, provider, monkeypatch, result_count
    )
    issue_linker = linker()
    old_body = (
        "Human request\n"
        "<!-- sakura-ai-issue-links-start -->\n"
        "Resolves #7\n<!-- sakura-ai-issue-links-end -->"
    )

    class PR:
        head = SimpleNamespace(sha="head")
        base = SimpleNamespace(sha="base")
        title = "Current PR"
        body = old_body
        changed_files = 1

        def get_files(self):
            return [
                SimpleNamespace(
                    filename="fix.py",
                    status="modified",
                    patch="@@ -1 +1 @@\n-old\n+new",
                    additions=1,
                    deletions=1,
                )
            ]

        def edit(self, *, body):
            self.body = body

    engine = create_engine("sqlite:///:memory:")
    PRIssueLink.__table__.create(engine)
    with Session(engine) as session:
        session.add(
            PRIssueLink(
                repo_name="owner/repo",
                pr_id=99,
                issue_number=7,
                link_type="semantic",
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

        def unexpected_model_call(**kwargs):
            pytest.fail("Incomplete or all-low rerank must not call verification AI")

        client = SimpleNamespace(
            resolve_role_candidates=AsyncMock(
                return_value=[
                    _candidate(context_window_tokens=128_000, max_output_tokens=8_000)
                ]
            ),
            call_with_retry=AsyncMock(side_effect=unexpected_model_call),
        )
        pr = PR()
        result = await PRRelationSyncService(
            retriever=IssueCandidateRetriever(service),
            verifier=PRRelationVerifier(client),
            session_factory=DB,
            linker=issue_linker,
        ).synchronize(SimpleNamespace(get_pull=lambda _: pr), "owner", "repo", 99)
        rows = session.scalars(select(PRIssueLink)).all()
        if result_count == 2:
            assert result.succeeded
            assert rows == []
            assert "#7" not in pr.body
            assert "Human request" in pr.body
        else:
            assert not result.succeeded
            assert result.failure == "ValueError"
            assert pr.body == old_body
            assert [(row.issue_number, row.link_type) for row in rows] == [
                (7, "semantic")
            ]
        assert len(provider[2]) == 1
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("result_count", [0, 1, 2])
async def test_real_issue_prephase_reports_incomplete_rerank_without_stopping_main(
    main_analyzer, foundation, provider, monkeypatch, result_count
):
    service = install_retrieval_provider(
        foundation, provider, monkeypatch, result_count
    )
    from backend.services.issues.issue_source_freshness import IssueSourceReader

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
    from tests.test_pr_issue_budget import summary_candidate

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
    assert result["category"] == "bug"
    assert result["duplicate_of"] is None
    relations = result["issue_relations"]
    if result_count == 2:
        assert relations["status"] == "verified"
        assert relations["failure"] is None
    else:
        assert relations["status"] == "failed"
        assert relations["failure"] == "ValueError"
    assert relations["related"] == []
    assert len(provider[2]) == 1
