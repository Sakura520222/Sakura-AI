"""GitHub title-only Issues retain strict relation source validation."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.core.github_app import extract_issue_info_from_webhook
from backend.services.issues.relation_analyzer import IssueRelationAnalyzer

TITLE = "Version 1 crashes on input x during import"


@pytest.fixture
def title_only_relation(monkeypatch):
    values = {
        "issue_relation_max_candidates": 5,
        "issue_relation_similarity_threshold": 0.75,
        "issue_relation_confidence_threshold": 0.85,
        "issue_duplicate_confidence_threshold": 0.95,
    }

    async def config(key, *, fresh):
        assert fresh is True
        return values[key]

    monkeypatch.setattr(
        "backend.services.issues.relation_analyzer.get_dynamic_config", config
    )
    info = extract_issue_info_from_webhook(
        {
            "action": "opened",
            "issue": {
                "number": 1,
                "title": TITLE,
                "body": None,
                "state": "open",
                "labels": [],
                "user": {"login": "reporter"},
                "html_url": "https://github.com/owner/repo/issues/1",
            },
            "repository": {
                "name": "repo",
                "full_name": "owner/repo",
                "owner": {"login": "owner"},
            },
        }
    )
    assert info["body"] is None
    candidate = {
        "number": 2,
        "title": TITLE,
        "body": "",
        "state": "open",
        "state_reason": None,
        "labels": ["bug"],
    }
    relation = {
        "number": 2,
        "relation": "duplicate",
        "confidence": 0.99,
        "reason": "Same affected version, input, import failure and outcome",
        "similarities": ["Same import crash on input x in version 1"],
        "differences": [],
        "evidence": [{"current_quote": TITLE, "candidate_quote": TITLE}],
    }
    retriever = SimpleNamespace(retrieve=AsyncMock(side_effect=[[candidate], []]))
    client = SimpleNamespace(
        call_with_retry=AsyncMock(
            return_value=SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5),
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps({"relations": [relation]})
                        )
                    )
                ],
            )
        )
    )
    return IssueRelationAnalyzer(retriever, client), info, candidate, relation, values


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "kind", "duplicate_of"),
    [("open", "duplicate", 2), ("closed", "duplicate_closed", None)],
)
async def test_webhook_null_body_uses_title_evidence(
    title_only_relation, phase, kind, duplicate_of
):
    analyzer, info, candidate, relation, _ = title_only_relation
    original = dict(info)
    if phase == "closed":
        candidate.update(state="closed", state_reason="completed")
        analyzer.retriever.retrieve.side_effect = [[], [candidate]]
    relation["relation"] = kind
    analyzer.client.call_with_retry.return_value.choices[
        0
    ].message.content = json.dumps({"relations": [relation]})
    result = await analyzer.analyze("owner", "repo", info)
    assert result.status == "verified"
    assert result.phase == phase
    assert result.duplicate_of == duplicate_of
    assert result.primary["relation"] == kind
    assert result.primary["evidence"] == [
        {"current_quote": TITLE, "candidate_quote": TITLE}
    ]
    sent = json.loads(
        analyzer.client.call_with_retry.call_args.kwargs["messages"][1]["content"]
    )
    assert sent["current"]["title"] == TITLE
    assert sent["current"]["body"] == ""
    assert analyzer.retriever.retrieve.call_args.kwargs["text"] == TITLE + "\n"
    assert result.prompt_tokens == 3 and result.completion_tokens == 5
    assert info == original and info["body"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [False, 0, [], {}, [TITLE]])
async def test_webhook_malformed_body_remains_failed(title_only_relation, body):
    analyzer, info, _, _, _ = title_only_relation
    info["body"] = body
    result = await analyzer.analyze("owner", "repo", info)
    assert result.status == "failed" and result.failure == "ValueError"
    assert result.primary is None and result.duplicate_of is None
    assert analyzer.retriever.retrieve.await_count == 0
    assert analyzer.client.call_with_retry.await_count == 0
    assert info["body"] == body


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["body", "title", "issue_number", "state"])
async def test_null_body_does_not_admit_missing_current_facts(
    title_only_relation, missing
):
    analyzer, info, _, _, _ = title_only_relation
    del info[missing]
    result = await analyzer.analyze("owner", "repo", info)
    assert result.status == "failed" and result.failure == "ValueError"
    assert result.primary is None and result.related == []
    assert analyzer.retriever.retrieve.await_count == 0
    assert analyzer.client.call_with_retry.await_count == 0
    assert missing not in info


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", ["invented fix from absent body", ""])
async def test_null_body_still_rejects_ungrounded_evidence(title_only_relation, quote):
    analyzer, info, _, relation, _ = title_only_relation
    relation["evidence"][0]["current_quote"] = quote
    analyzer.client.call_with_retry.return_value.choices[
        0
    ].message.content = json.dumps({"relations": [relation]})
    result = await analyzer.analyze("owner", "repo", info)
    assert result.status == "failed" and result.failure == "ValueError"
    assert result.primary is None and result.duplicate_of is None
    assert analyzer.client.call_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_null_body_preserves_invalid_config_failure(title_only_relation):
    analyzer, info, _, _, values = title_only_relation
    values["issue_relation_similarity_threshold"] = False
    result = await analyzer.analyze("owner", "repo", info)
    assert result.status == "failed" and result.failure == "ValueError"
    assert analyzer.retriever.retrieve.await_count == 0
    assert info["body"] is None


@pytest.mark.asyncio
async def test_null_body_preserves_provider_failure(title_only_relation):
    analyzer, info, _, _, _ = title_only_relation
    analyzer.client.call_with_retry.side_effect = RuntimeError("private provider token")
    result = await analyzer.analyze("owner", "repo", info)
    assert result.status == "failed" and result.failure == "RuntimeError"
    assert result.primary is None and result.related == []
    assert "private provider token" not in json.dumps(result.to_dict())
    assert info["body"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [asyncio.CancelledError(), ReviewCancelledError("stop")]
)
async def test_null_body_preserves_cancellation(title_only_relation, error):
    analyzer, info, _, _, _ = title_only_relation
    analyzer.retriever.retrieve.side_effect = error
    with pytest.raises(type(error)):
        await analyzer.analyze("owner", "repo", info)
    assert analyzer.client.call_with_retry.await_count == 0
    assert info["body"] is None
