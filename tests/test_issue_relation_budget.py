"""The optional Issue prephase admits only complete bounded summary requests."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.core.ai_protocol.models import UnifiedMessage
from backend.core.ai_protocol.request_policy import estimate_unified_messages
from backend.core.config import DYNAMIC_CONFIG_GROUPS, Settings
from backend.services.issues import relation_analyzer
from tests.test_issue_candidate_foundation import NewestComments, issue
from tests.test_issue_candidate_foundation import foundation as _foundation

foundation = _foundation
from tests.test_issue_relations import candidate, decision, response
from tests.test_pr_issue_budget import summary_candidate


@pytest.fixture
def bounded_relation(monkeypatch):
    values = {
        "issue_relation_max_candidates": 5,
        "issue_relation_similarity_threshold": 0.75,
        "issue_relation_confidence_threshold": 0.85,
        "issue_duplicate_confidence_threshold": 0.95,
        "issue_include_comments": True,
        "issue_relation_max_input_tokens": 5000,
    }
    read = AsyncMock(side_effect=lambda key, **_: values[key])
    monkeypatch.setattr(relation_analyzer, "get_dynamic_config", read)
    monkeypatch.setattr("backend.core.config.get_dynamic_config", read)
    # A previously imported helper must use the same live reader.
    import sys

    helper = sys.modules.get("backend.services.issues.issue_budget")
    if helper:
        monkeypatch.setattr(helper, "get_dynamic_config", read)
    retriever = SimpleNamespace(retrieve=AsyncMock(side_effect=[[candidate()], []]))
    client = SimpleNamespace(
        resolve_role_candidates=AsyncMock(return_value=[summary_candidate()]),
        call_with_retry=AsyncMock(return_value=response([decision()])),
    )
    analyzer = relation_analyzer.IssueRelationAnalyzer(retriever, client)
    current = {
        "issue_number": 1,
        "title": "Crash",
        "body": "A crashes on input x",
        "state": "open",
    }
    return analyzer, current, values, read


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "current",
        "candidate",
        "current_comment",
        "candidate_comment",
        "language",
        "system",
        "labels",
    ],
)
async def test_full_summary_packet_is_bounded_before_provider(
    bounded_relation, monkeypatch, source
):
    analyzer, current, _, _ = bounded_relation
    huge = '"\\\n' * 10000
    fact = candidate()
    comments = None
    kwargs = {}
    if source == "current":
        current["body"] = huge
    elif source == "candidate":
        fact["body"] = huge
    elif source == "current_comment":
        comments = [{"body": huge}]
    elif source == "candidate_comment":
        fact["comments"] = [{"body": huge}]
    elif source == "labels":
        fact["labels"] = [huge]
    elif source == "language":
        kwargs["output_language"] = huge
    else:
        monkeypatch.setattr(relation_analyzer, "ISSUE_RELATION_PROMPT", huge)
    analyzer.retriever.retrieve.side_effect = [[fact], []]
    result = await analyzer.analyze(
        "owner", "repo", current, comments=comments, **kwargs
    )
    assert result.status == "failed" and result.failure == "input_budget"
    assert (
        result.duplicate_of is None and result.primary is None and result.related == []
    )
    assert analyzer.client.call_with_retry.await_count == 0


@pytest.mark.asyncio
async def test_small_fallback_context_includes_output_and_reserve(bounded_relation):
    analyzer, current, _, _ = bounded_relation
    analyzer.client.resolve_role_candidates.return_value = [
        summary_candidate(),
        summary_candidate(context=1200, output=800),
    ]
    result = await analyzer.analyze("owner", "repo", current)
    assert result.failure == "input_budget"
    assert analyzer.client.call_with_retry.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [[], [summary_candidate(context=0)]])
async def test_unknown_metadata_never_calls_provider(bounded_relation, metadata):
    analyzer, current, _, _ = bounded_relation
    analyzer.client.resolve_role_candidates.return_value = metadata
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "failed" and result.failure == "budget_unavailable"
    assert analyzer.client.call_with_retry.await_count == 0


@pytest.mark.asyncio
async def test_closed_phase_refreshes_budget_and_retains_open_usage(bounded_relation):
    analyzer, current, values, read = bounded_relation
    analyzer.retriever.retrieve.side_effect = [
        [candidate()],
        [candidate(state="closed")],
    ]

    async def first(**kwargs):
        values["issue_relation_max_input_tokens"] = 1
        return response([decision("related")])

    analyzer.client.call_with_retry.side_effect = first
    result = await analyzer.analyze("owner", "repo", current)
    assert result.phase == "closed" and result.failure == "input_budget"
    assert (
        result.primary is None and result.related == [] and result.duplicate_of is None
    )
    assert (result.prompt_tokens, result.completion_tokens) == (3, 5)
    assert analyzer.client.call_with_retry.await_count == 1
    assert (
        sum(c.args == ("issue_relation_max_input_tokens",) for c in read.call_args_list)
        == 2
    )
    assert all(c.kwargs["fresh"] is True for c in read.call_args_list)


@pytest.mark.asyncio
async def test_closed_phase_refreshes_model_metadata(bounded_relation):
    analyzer, current, _, _ = bounded_relation
    analyzer.retriever.retrieve.side_effect = [
        [candidate()],
        [candidate(state="closed")],
    ]
    analyzer.client.call_with_retry.return_value = response([decision("related")])
    analyzer.client.resolve_role_candidates.side_effect = [
        [summary_candidate()],
        [summary_candidate(context=1200, output=800)],
    ]
    result = await analyzer.analyze("owner", "repo", current)
    assert result.phase == "closed" and result.failure == "input_budget"
    assert (result.prompt_tokens, result.completion_tokens) == (3, 5)
    assert analyzer.client.call_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_valid_request_uses_full_uncropped_packet_and_controls(bounded_relation):
    analyzer, current, values, _ = bounded_relation
    fact = candidate()
    fact["comments"] = [{"body": "fixed by commit abc", "body_truncated": True}]
    fact["comments_context"] = {"bounded": True, "truncated": True, "max_comments": 20}
    analyzer.retriever.retrieve.side_effect = [[fact], []]
    comments = [{"body": "A crashes on input x", "author": "reporter"}]
    event = asyncio.Event()
    deadline = SimpleNamespace(is_expired=lambda: False)
    result = await analyzer.analyze(
        "owner",
        "repo",
        current,
        comments=comments,
        cancel_event=event,
        deadline=deadline,
        output_language="en",
    )
    assert result.duplicate_of == 2
    messages = analyzer.client.call_with_retry.call_args.kwargs["messages"]
    assert (
        estimate_unified_messages([UnifiedMessage(**m) for m in messages])
        <= values["issue_relation_max_input_tokens"]
    )
    packet = json.loads(messages[1]["content"])
    assert packet["current"]["comments"] == comments
    assert packet["candidates"][0]["comments"] == fact["comments"]
    assert packet["candidates"][0]["comments_context"] == fact["comments_context"]
    call = analyzer.retriever.retrieve.call_args.kwargs
    assert (
        call["cancel_event"] is event
        and call["deadline"] is deadline
        and call["include_comments"] is True
    )


@pytest.mark.asyncio
async def test_disabled_discussion_policy_ignores_supplied_comments(bounded_relation):
    analyzer, current, values, _ = bounded_relation
    values["issue_include_comments"] = False
    fact = candidate()
    fact["comments"] = [{"body": "A" * 1000000}]
    analyzer.retriever.retrieve.side_effect = [[fact], []]
    result = await analyzer.analyze(
        "owner", "repo", current, comments=[{"body": "A" * 1000000}]
    )
    assert result.duplicate_of == 2
    packet = json.loads(
        analyzer.client.call_with_retry.call_args.kwargs["messages"][1]["content"]
    )
    assert packet["current"]["comments"] == packet["candidates"][0]["comments"] == []
    assert analyzer.retriever.retrieve.call_args.kwargs["include_comments"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), ReviewCancelledError()])
async def test_budget_resolution_preserves_cancellation(bounded_relation, error):
    analyzer, current, _, _ = bounded_relation
    analyzer.client.resolve_role_candidates.side_effect = error
    with pytest.raises(type(error)):
        await analyzer.analyze("owner", "repo", current)


def test_issue_budget_settings_have_registered_defaults():
    settings = Settings()
    keys = {
        "issue_relation_max_input_tokens": 64000,
        "issue_relation_candidate_max_comments": 20,
        "issue_relation_candidate_comment_max_chars": 4000,
    }
    group = DYNAMIC_CONFIG_GROUPS["issue_analysis"]
    for key, value in keys.items():
        assert getattr(settings, key) == value
        assert key in group["keys"] and key in group["descriptions"]


@pytest.mark.asyncio
async def test_retrieval_deadline_is_skipped_with_open_usage(bounded_relation):
    from backend.services.issues.relation_runtime import RelationDeadlineExceeded

    analyzer, current, _, _ = bounded_relation
    analyzer.retriever.retrieve.side_effect = [
        [candidate()],
        RelationDeadlineExceeded(),
    ]
    analyzer.client.call_with_retry.return_value = response([decision("related")])
    result = await analyzer.analyze("owner", "repo", current)
    assert (
        result.status == "skipped"
        and result.failure == "deadline"
        and result.phase == "closed"
    )
    assert (result.prompt_tokens, result.completion_tokens) == (3, 5)
    assert (
        result.primary is None and result.related == [] and result.duplicate_of is None
    )


@pytest.mark.asyncio
async def test_budget_read_cancel_signal_wins_operational_failure(bounded_relation):
    analyzer, current, _, _ = bounded_relation
    event = asyncio.Event()

    async def interrupted(role):
        event.set()
        raise RuntimeError("private credentials")

    analyzer.client.resolve_role_candidates.side_effect = interrupted
    with pytest.raises(ReviewCancelledError):
        await analyzer.analyze("owner", "repo", current, cancel_event=event)
    assert analyzer.client.call_with_retry.await_count == 0


@pytest.mark.asyncio
async def test_closed_comment_fix_quotes_are_grounded_in_actual_bounded_source(
    foundation, bounded_relation
):
    from backend.services.issues.candidate_retriever import IssueCandidateRetriever

    service, _, repo, values = foundation
    analyzer, current, _, _ = bounded_relation
    values.update(
        issue_relation_candidate_max_comments=1,
        issue_relation_candidate_comment_max_chars=40,
    )
    repo.rows = [
        issue(
            2,
            state="closed",
            state_reason="completed",
            title="Prior crash",
            body="A crashes on input x",
        )
    ]
    rows = [
        {
            "id": i,
            "body": "Fixed by commit abc: import handles input x"
            if i == 9
            else "Older discussion",
            "html_url": f"https://github.com/owner/repo/issues/2#issuecomment-{i}",
            "user": {"login": "maintainer"},
            "created_at": "2026-10-02T00:00:00Z",
            "updated_at": "2026-10-02T00:00:00Z",
        }
        for i in range(10)
    ]
    comments = NewestComments(rows)
    original_get = repo.get_issue

    def get_issue(number):
        obj = original_get(number)
        obj.raw_data = {**obj.raw_data, "comments": 10}
        obj.get_comments = lambda: comments
        return obj

    repo.get_issue = get_issue
    analyzer.retriever = IssueCandidateRetriever(service)
    quote = "Fixed by commit abc: import handles inpu"
    analyzer.client.call_with_retry.return_value = response(
        [
            decision(
                "previously_resolved",
                evidence=[
                    {"current_quote": "A crashes on input x", "candidate_quote": quote}
                ],
            )
        ]
    )
    result = await analyzer.analyze("owner", "repo", current)
    assert (
        result.status == "verified"
        and result.primary["relation"] == "previously_resolved"
    )
    assert result.primary["evidence"][0]["candidate_quote"] == quote
    packet = json.loads(
        analyzer.client.call_with_retry.call_args.kwargs["messages"][1]["content"]
    )
    fact = packet["candidates"][0]
    assert fact["comments"][0]["body"] == quote
    assert fact["comments"][0]["body_truncated"] is True
    assert fact["updated_at"] == "2026-10-02T00:00:00Z"
    assert fact["comments_context"]["included_count"] == 1
    assert fact["comments_context"]["total_count"] == 10
    assert fact["comments_context"]["truncated"] is True
    assert comments.reads <= 2


@pytest.mark.asyncio
async def test_json_escaping_exceeds_budget_even_when_raw_body_fits(bounded_relation):
    analyzer, current, values, _ = bounded_relation
    current["body"] = '"' * 11000
    assert len(current["body"]) < values["issue_relation_max_input_tokens"] * 4
    result = await analyzer.analyze("owner", "repo", current)
    assert result.failure == "input_budget"
    assert analyzer.client.call_with_retry.await_count == 0


@pytest.mark.asyncio
async def test_two_individual_inputs_fit_but_combined_packet_fails(bounded_relation):
    analyzer, current, _, _ = bounded_relation
    current["body"] = "a" * 10000
    fact = candidate()
    fact["body"] = "b" * 10000
    analyzer.retriever.retrieve.side_effect = [[fact], []]
    result = await analyzer.analyze("owner", "repo", current)
    assert result.failure == "input_budget"
    assert analyzer.client.call_with_retry.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_limit", [False, None, 0, -1, "5000"])
async def test_invalid_budget_config_has_no_provider_fallback(
    bounded_relation, bad_limit
):
    analyzer, current, values, _ = bounded_relation
    values["issue_relation_max_input_tokens"] = bad_limit
    result = await analyzer.analyze("owner", "repo", current)
    assert result.failure == "budget_unavailable" and result.duplicate_of is None
    assert analyzer.client.call_with_retry.await_count == 0


@pytest.mark.asyncio
async def test_closed_comment_invented_omitted_quote_fails_with_usage(bounded_relation):
    analyzer, current, _, _ = bounded_relation
    fact = candidate(state="closed")
    fact["comments"] = [{"body": "Fixed by commit", "body_truncated": True}]
    fact["comments_context"] = {"bounded": True, "truncated": True}
    analyzer.retriever.retrieve.side_effect = [[], [fact]]
    analyzer.client.call_with_retry.return_value = response(
        [
            decision(
                "previously_resolved",
                evidence=[
                    {
                        "current_quote": "A crashes on input x",
                        "candidate_quote": "Fixed by commit abc",
                    }
                ],
            )
        ]
    )
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "failed" and result.failure == "ValueError"
    assert result.primary is None and result.duplicate_of is None
    assert (result.prompt_tokens, result.completion_tokens) == (3, 5)
