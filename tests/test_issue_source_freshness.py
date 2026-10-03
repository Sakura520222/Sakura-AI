"""Relations never accept source changes across inference or later phases."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.issues import relation_analyzer
from tests.test_issue_relations import candidate, decision, response
from tests.test_issue_relations import main_analyzer as _main_analyzer

main_analyzer = _main_analyzer
from tests.test_pr_issue_budget import summary_candidate

VERSION = "2026-10-02T00:00:00Z"


class MemorySource:
    def __init__(self, rows):
        self.rows = rows
        self.reads = []
        self.error = None

    async def read(self, owner, name, number, **controls):
        self.reads.append((number, controls))
        if self.error:
            raise self.error
        return deepcopy(self.rows[number])


@pytest.fixture
def fresh_relation(monkeypatch):
    values = {
        "issue_relation_max_candidates": 5,
        "issue_include_comments": False,
        "issue_relation_max_input_tokens": 64000,
        "issue_relation_similarity_threshold": 0.75,
        "issue_relation_confidence_threshold": 0.85,
        "issue_duplicate_confidence_threshold": 0.95,
        "issue_relation_candidate_max_comments": 20,
        "issue_relation_candidate_comment_max_chars": 2000,
    }

    async def config(key, **kwargs):
        return values[key]

    monkeypatch.setattr(relation_analyzer, "get_dynamic_config", config)
    monkeypatch.setattr(
        "backend.services.issues.issue_budget.get_dynamic_config", config
    )
    current = candidate(number=1, updated_at=VERSION)
    other = candidate(updated_at=VERSION)
    source = MemorySource({1: current, 2: other})
    retriever = SimpleNamespace(retrieve=AsyncMock(side_effect=[[deepcopy(other)], []]))
    client = SimpleNamespace(
        resolve_role_candidates=AsyncMock(return_value=[summary_candidate()]),
        call_with_retry=AsyncMock(return_value=response([decision()])),
    )
    analyzer = relation_analyzer.IssueRelationAnalyzer(retriever, client)
    analyzer.source_reader = source
    return analyzer, deepcopy(current), source, values


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [1, 2])
async def test_inference_edit_revert_version_never_verifies(fresh_relation, number):
    analyzer, current, source, _ = fresh_relation

    async def infer(**kwargs):
        source.rows[number]["updated_at"] = "2026-10-02T00:01:00Z"
        return response([decision()])

    analyzer.client.call_with_retry.side_effect = infer
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "failed" and result.failure == "stale_source"
    assert result.duplicate_of is None and result.primary is None
    assert (result.prompt_tokens, result.completion_tokens) == (3, 5)


class LiveRepo:
    """Loaded REST source objects; each GET owns an independent raw snapshot."""

    def __init__(self, rows):
        self.rows = rows
        self.discussions = {number: [] for number in rows}
        self.reads = []
        self.error = None
        self.comment_reads = []

    def get_issue(self, number):
        from tests.test_issue_candidate_foundation import NewestComments

        self.reads.append(number)
        if self.error:
            raise self.error
        row = deepcopy(self.rows[number])
        row["labels"] = [{"name": label} for label in row["labels"]]
        row["comments"] = len(self.discussions[number])
        obj = SimpleNamespace(raw_data=row)

        def comments():
            self.comment_reads.append(number)
            return NewestComments(deepcopy(self.discussions[number]))

        obj.get_comments = comments
        return obj


async def live_analyzer(fresh_relation, *, include_comments=False):
    from backend.services.issues.issue_source_freshness import IssueSourceReader

    analyzer, current, source, values = fresh_relation
    values["issue_include_comments"] = include_comments
    values["issue_relation_candidate_max_comments"] = 2
    values["issue_relation_candidate_comment_max_chars"] = 100
    repo = LiveRepo(source.rows)
    reader = IssueSourceReader(repo=repo)
    analyzer.source_reader = reader
    return analyzer, current, repo, values


async def recall_live(analyzer, repo, values):
    return await analyzer.source_reader.read(
        "owner",
        "repo",
        2,
        include_comments=values["issue_include_comments"],
        max_comments=values["issue_relation_candidate_max_comments"],
        max_chars=values["issue_relation_candidate_comment_max_chars"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [1, 2])
@pytest.mark.parametrize(
    "change",
    [
        "body",
        "title",
        "labels",
        "state",
        "state_reason",
        "version",
        "delete",
        "unavailable",
        "malformed",
    ],
)
async def test_real_source_changes_never_verify(fresh_relation, number, change):
    analyzer, current, repo, values = await live_analyzer(fresh_relation)
    baseline = await recall_live(analyzer, repo, values)
    analyzer.retriever.retrieve.side_effect = [[baseline], []]

    async def infer(**kwargs):
        row = repo.rows[number]
        if change == "body":
            row["body"] += " changed"
        elif change == "title":
            row["title"] += " changed"
        elif change == "labels":
            row["labels"].append("new label")
        elif change == "state":
            row["state"] = "closed"
        elif change == "state_reason":
            row["state_reason"] = "not_planned"
        elif change == "version":
            # Close/reopen and edit/revert preserve final content but change version.
            row["updated_at"] = "2026-10-02T01:00:00Z"
        elif change == "delete":
            del repo.rows[number]
        elif change == "unavailable":
            repo.error = RuntimeError("private token source error")
        else:
            row["updated_at"] = "invalid"
        return response([decision()])

    analyzer.client.call_with_retry.side_effect = infer
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "failed"
    assert result.failure == (
        "source_unavailable"
        if change in {"delete", "unavailable", "malformed"}
        else "stale_source"
    )
    assert (
        result.primary is None and result.related == [] and result.duplicate_of is None
    )
    assert (result.prompt_tokens, result.completion_tokens) == (3, 5)
    assert "private token" not in str(result.to_dict())


def comment(number, body="A crashes on input x"):
    return {
        "id": number,
        "body": body,
        "user": {"login": "maintainer"},
        "html_url": f"https://github.com/owner/repo/issues/1#issuecomment-{number}",
        "created_at": VERSION,
        "updated_at": VERSION,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [1, 2])
@pytest.mark.parametrize(
    "change", ["add", "delete", "edit", "edit_revert", "identity", "readfail"]
)
async def test_real_bounded_discussion_changes_never_verify(
    fresh_relation, number, change
):
    analyzer, current, repo, values = await live_analyzer(
        fresh_relation, include_comments=True
    )
    repo.discussions = {1: [comment(1)], 2: [comment(2)]}
    baseline = await recall_live(analyzer, repo, values)
    analyzer.retriever.retrieve.side_effect = [[baseline], []]

    async def infer(**kwargs):
        rows = repo.discussions[number]
        if change == "add":
            rows.append(comment(3))
        elif change == "delete":
            rows.clear()
        elif change == "edit":
            rows[0]["body"] += " edited"
        elif change == "edit_revert":
            rows[0]["updated_at"] = "2026-10-02T01:00:00Z"
        elif change == "identity":
            rows[0]["id"] = 99
        else:
            rows[0].pop("updated_at")
        return response([decision()])

    analyzer.client.call_with_retry.side_effect = infer
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "failed"
    assert result.failure == (
        "source_unavailable" if change == "readfail" else "stale_source"
    )
    assert result.duplicate_of is None
    assert (result.prompt_tokens, result.completion_tokens) == (3, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("include_comments", [False, True])
async def test_fresh_input_uses_live_current_and_unchanged_source_verifies(
    fresh_relation, include_comments
):
    import json

    analyzer, current, repo, values = await live_analyzer(
        fresh_relation, include_comments=include_comments
    )
    current["body"] = "obsolete webhook payload"
    original = deepcopy(current)
    baseline = await recall_live(analyzer, repo, values)
    analyzer.retriever.retrieve.side_effect = [[baseline], []]
    result = await analyzer.analyze(
        "owner", "repo", current, comments=[{"body": "obsolete legacy comment"}]
    )
    assert result.status == "verified" and result.duplicate_of == 2
    packet = json.loads(
        analyzer.client.call_with_retry.call_args.kwargs["messages"][1]["content"]
    )
    assert packet["current"]["body"] == repo.rows[1]["body"]
    assert packet["current"]["comments"] == []
    assert current == original
    assert bool(repo.comment_reads) is include_comments


@pytest.mark.asyncio
@pytest.mark.parametrize("candidates", [False, True])
async def test_none_or_empty_still_checks_current(fresh_relation, candidates):
    analyzer, current, repo, values = await live_analyzer(fresh_relation)
    if candidates:
        analyzer.retriever.retrieve.side_effect = [
            [await recall_live(analyzer, repo, values)],
            [],
        ]

        async def infer(**kwargs):
            repo.rows[1]["updated_at"] = "2026-10-02T01:00:00Z"
            return response([decision("none")])

        analyzer.client.call_with_retry.side_effect = infer
    else:

        async def retrieve(*args, **kwargs):
            repo.rows[1]["updated_at"] = "2026-10-02T01:00:00Z"
            return []

        analyzer.retriever.retrieve.side_effect = retrieve
    result = await analyzer.analyze("owner", "repo", current)
    assert result.failure == "stale_source" and result.status == "failed"
    assert result.duplicate_of is None


@pytest.mark.asyncio
async def test_unaccepted_candidate_change_does_not_invalidate_relation(fresh_relation):
    analyzer, current, repo, values = await live_analyzer(fresh_relation)
    repo.rows[3] = candidate(number=3, updated_at=VERSION)
    baseline = await recall_live(analyzer, repo, values)
    analyzer.retriever.retrieve.side_effect = [[baseline, deepcopy(repo.rows[3])], []]

    async def infer(**kwargs):
        del repo.rows[3]
        return response([decision(), decision("none", number=3)])

    analyzer.client.call_with_retry.side_effect = infer
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "verified" and result.duplicate_of == 2
    assert 3 not in repo.reads


@pytest.mark.asyncio
@pytest.mark.parametrize("closed_inference", [False, True])
@pytest.mark.parametrize("number", [1, 2])
async def test_later_closed_phase_rechecks_current_and_earlier_open(
    fresh_relation, closed_inference, number
):
    analyzer, current, repo, values = await live_analyzer(fresh_relation)
    repo.rows[3] = candidate(number=3, state="closed", updated_at=VERSION)
    baseline = await recall_live(analyzer, repo, values)

    async def retrieve(*args, **kwargs):
        if kwargs["state"] == "open":
            return [baseline]
        if not closed_inference:
            repo.rows[number]["updated_at"] = "2026-10-02T01:00:00Z"
            return []
        return [deepcopy(repo.rows[3])]

    async def infer(**kwargs):
        import json

        if json.loads(kwargs["messages"][1]["content"])["phase"] == "open":
            return response([decision("related")])
        repo.rows[number]["updated_at"] = "2026-10-02T01:00:00Z"
        return response([decision("related", number=3)])

    analyzer.retriever.retrieve.side_effect = retrieve
    analyzer.client.call_with_retry.side_effect = infer
    result = await analyzer.analyze("owner", "repo", current)
    assert result.failure == "stale_source" and result.status == "failed"
    assert result.primary is None and result.related == []
    assert result.prompt_tokens == (6 if closed_inference else 3)


@pytest.mark.asyncio
async def test_main_analysis_continues_and_retains_stale_source_usage(
    fresh_relation, main_analyzer, monkeypatch
):
    analyzer, current, repo, values = await live_analyzer(fresh_relation)
    analyzer.retriever.retrieve.side_effect = [
        [await recall_live(analyzer, repo, values)],
        [],
    ]

    async def infer(**kwargs):
        repo.rows[2]["updated_at"] = "2026-10-02T01:00:00Z"
        return response([decision()])

    analyzer.client.call_with_retry.side_effect = infer
    monkeypatch.setattr(
        "backend.services.issue_analyzer.IssueRelationAnalyzer",
        lambda **kwargs: analyzer,
    )

    async def config(key, **kwargs):
        return key == "issue_detect_duplicates"

    monkeypatch.setattr("backend.services.issue_analyzer.get_dynamic_config", config)
    current["issue_number"] = 1
    result = await main_analyzer.analyze_issue(current, "owner", "repo")
    assert result["category"] == "bug"
    assert result["issue_relations"]["failure"] == "stale_source"
    assert result["duplicate_of"] is None
    assert (result["prompt_tokens"], result["completion_tokens"]) == (6, 10)
    assert main_analyzer.api_client.call_with_retry.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["task", "domain", "signal", "deadline"])
async def test_post_inference_controls_preserve_semantics(fresh_relation, kind):
    import asyncio

    from backend.core.ai_protocol.errors import ReviewCancelledError

    analyzer, current, source, _ = fresh_relation
    original_read = source.read
    event = asyncio.Event()
    state = {"expired": False, "inferred": False}
    deadline = SimpleNamespace(is_expired=lambda: state["expired"])

    async def infer(**kwargs):
        state["inferred"] = True
        return response([decision()])

    async def read(*args, **kwargs):
        if state["inferred"]:
            if kind == "task":
                raise asyncio.CancelledError()
            if kind == "domain":
                raise ReviewCancelledError()
            if kind == "signal":
                event.set()
                raise RuntimeError("private source")
            state["expired"] = True
            raise RuntimeError("private source")
        return await original_read(*args, **kwargs)

    source.read = read
    analyzer.client.call_with_retry.side_effect = infer
    if kind == "deadline":
        result = await analyzer.analyze(
            "owner", "repo", current, cancel_event=event, deadline=deadline
        )
        assert result.status == "skipped" and result.failure == "deadline"
        assert (result.prompt_tokens, result.completion_tokens) == (3, 5)
    else:
        with pytest.raises(
            asyncio.CancelledError if kind == "task" else ReviewCancelledError
        ):
            await analyzer.analyze(
                "owner", "repo", current, cancel_event=event, deadline=deadline
            )


@pytest.mark.asyncio
async def test_newest_discussion_sample_is_bounded_and_stable(fresh_relation):
    analyzer, current, repo, values = await live_analyzer(
        fresh_relation, include_comments=True
    )
    repo.discussions = {
        1: [comment(i + 1) for i in range(10)],
        2: [comment(i + 20) for i in range(10)],
    }
    analyzer.retriever.retrieve.side_effect = [
        [await recall_live(analyzer, repo, values)],
        [],
    ]
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "verified" and result.duplicate_of == 2
    import json

    packet = json.loads(
        analyzer.client.call_with_retry.call_args.kwargs["messages"][1]["content"]
    )
    assert [c["id"] for c in packet["current"]["comments"]] == [10, 9]
    assert [c["id"] for c in packet["candidates"][0]["comments"]] == [29, 28]
    assert packet["current"]["comments_context"]["total_count"] == 10
    assert packet["current"]["comments_context"]["truncated"] is True


@pytest.mark.asyncio
async def test_closed_phase_cannot_replace_accepted_open_baseline(fresh_relation):
    analyzer, current, repo, values = await live_analyzer(fresh_relation)
    baseline = await recall_live(analyzer, repo, values)

    async def retrieve(*args, **kwargs):
        if kwargs["state"] == "open":
            return [baseline]
        repo.rows[2]["state"] = "closed"
        return [deepcopy(repo.rows[2])]

    analyzer.retriever.retrieve.side_effect = retrieve
    analyzer.client.call_with_retry.side_effect = [
        response([decision("related")]),
        response([decision("related")]),
    ]
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "failed" and result.failure == "stale_source"
    assert result.primary is None and result.duplicate_of is None


@pytest.mark.asyncio
async def test_disabled_discussion_does_not_read_or_compare_comment_changes(
    fresh_relation,
):
    analyzer, current, repo, values = await live_analyzer(fresh_relation)
    analyzer.retriever.retrieve.side_effect = [
        [await recall_live(analyzer, repo, values)],
        [],
    ]

    async def infer(**kwargs):
        repo.discussions[1] = [{"malformed": True}]
        repo.discussions[2] = [{"malformed": True}]
        return response([decision()])

    analyzer.client.call_with_retry.side_effect = infer
    result = await analyzer.analyze("owner", "repo", current)
    assert result.status == "verified" and result.duplicate_of == 2
    assert repo.comment_reads == []


@pytest.mark.asyncio
async def test_task_cancellation_drains_real_inflight_postcheck(fresh_relation):
    import asyncio
    import threading

    analyzer, current, repo, values = await live_analyzer(fresh_relation)
    analyzer.retriever.retrieve.side_effect = [
        [await recall_live(analyzer, repo, values)],
        [],
    ]
    inferred = False
    entered = threading.Event()
    release = threading.Event()
    original = repo.get_issue

    def get_issue(number):
        if inferred:
            entered.set()
            assert release.wait(5), "postcheck read was not released"
        return original(number)

    async def infer(**kwargs):
        nonlocal inferred
        inferred = True
        return response([decision()])

    repo.get_issue = get_issue
    analyzer.client.call_with_retry.side_effect = infer
    task = asyncio.create_task(analyzer.analyze("owner", "repo", current))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repo.reads == [2, 1, 1]
