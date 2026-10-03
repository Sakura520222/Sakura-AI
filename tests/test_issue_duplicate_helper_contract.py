"""The compatibility API needs a concrete Issue identity before any work."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.services.issue_service import IssueService
from backend.services.issues import candidate_retriever, relation_analyzer
from backend.services.issues.relation_analyzer import IssueRelationResult


@pytest.fixture
def helper(monkeypatch):
    # Keep the global service singleton and all external providers untouched.
    service = object.__new__(IssueService)
    service._issue_embedding_service = object()
    retriever = Mock()
    analyzer = Mock(analyze=AsyncMock(return_value=IssueRelationResult()))
    retriever_factory = Mock(return_value=retriever)
    analyzer_factory = Mock(return_value=analyzer)
    monkeypatch.setattr(
        candidate_retriever, "IssueCandidateRetriever", retriever_factory
    )
    monkeypatch.setattr(relation_analyzer, "IssueRelationAnalyzer", analyzer_factory)
    return service, retriever_factory, analyzer_factory, analyzer


@pytest.mark.asyncio
async def test_missing_identity_is_required_before_dependency_work(helper):
    service, retriever_factory, analyzer_factory, analyzer = helper

    with pytest.raises(TypeError, match="current_issue_number"):
        await service.detect_duplicates("owner", "repo", "Title", "Body")

    retriever_factory.assert_not_called()
    analyzer_factory.assert_not_called()
    analyzer.analyze.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [None, False, True, 0, -1, 1.5, "1", [], {}])
async def test_invalid_identity_is_rejected_before_dependency_work(helper, number):
    service, retriever_factory, analyzer_factory, analyzer = helper

    with pytest.raises(ValueError, match="current_issue_number.*positive integer"):
        await service.detect_duplicates("owner", "repo", "Title", "Body", number)

    retriever_factory.assert_not_called()
    analyzer_factory.assert_not_called()
    analyzer.analyze.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword", [False, True])
async def test_explicit_number_preserves_verified_result_and_call_semantics(
    helper, keyword
):
    service, retriever_factory, analyzer_factory, analyzer = helper
    primary = {"number": 8, "relation": "duplicate", "confidence": 0.98}
    analyzer.analyze.return_value = IssueRelationResult(primary=primary, duplicate_of=8)

    if keyword:
        result = await service.detect_duplicates(
            "owner", "repo", "Title", "Body", current_issue_number=7
        )
    else:
        result = await service.detect_duplicates("owner", "repo", "Title", "Body", 7)

    assert result == [{**primary, "issue_number": 8}]
    assert "issue_number" not in primary
    retriever_factory.assert_called_once_with(service._issue_embedding_service)
    analyzer_factory.assert_called_once_with(retriever=retriever_factory.return_value)
    analyzer.analyze.assert_awaited_once_with(
        "owner",
        "repo",
        {"issue_number": 7, "title": "Title", "body": "Body", "state": "open"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", [None, "related", "previously_resolved"])
async def test_verified_nonduplicate_still_returns_empty(helper, relation):
    service, _, _, analyzer = helper
    analyzer.analyze.return_value = IssueRelationResult(
        primary={"number": 8, "relation": relation} if relation else None,
        phase="closed" if relation == "previously_resolved" else "open",
    )

    assert await service.detect_duplicates("owner", "repo", "Title", None, 7) == []
    assert analyzer.analyze.await_args.args[2]["body"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), ReviewCancelledError()])
async def test_explicit_number_preserves_cancellation(helper, error):
    service, _, _, analyzer = helper
    analyzer.analyze.side_effect = error

    with pytest.raises(type(error)):
        await service.detect_duplicates("owner", "repo", "Title", "Body", 7)
