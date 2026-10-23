"""PR recall configuration and zero-line source snapshots stay transactional."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.core import config
from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.models.database import PRIssueLink
from backend.services.issues.pr_link_sync import PRRelationSyncService
from backend.services.issues.pr_verifier import PRRelationVerifier
from backend.services.issues.unified_diff import parse_unified_diff
from tests.test_pr_issue_budget import Pull, changed_file
from tests.test_pr_issue_relations import (
    CANDIDATE,
    RELATION,
    configured_client,
    linker,
    repo_with_candidate,
)
from tests.test_relation_configuration_guards import config_db as _config_db
from tests.test_relation_configuration_guards import model_response

config_db = _config_db
THRESHOLD = "semantic_issue_similarity_threshold"
RENAME = "diff --git a/old.py b/new.py\nsimilarity index 100%\nrename from old.py\nrename to new.py"
MODE = "diff --git a/tool.sh b/tool.sh\nold mode 100644\nnew mode 100755"


def zero_file(patch=None, *, status="renamed", additions=0, deletions=0):
    return SimpleNamespace(
        filename="new.py",
        previous_filename="old.py",
        status=status,
        patch=patch,
        additions=additions,
        deletions=deletions,
    )


@pytest.fixture
def sync_case(config_db):
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

    def make(files, *, candidates=None, relations=None):
        pr = Pull(lambda: iter(files), count=len(files))
        client = configured_client(
            call_with_retry=AsyncMock(
                return_value=model_response(
                    [RELATION] if relations is None else relations
                )
            )
        )
        retriever = SimpleNamespace(
            retrieve=AsyncMock(
                return_value=[CANDIDATE] if candidates is None else candidates
            )
        )
        service = PRRelationSyncService(
            retriever=retriever,
            verifier=PRRelationVerifier(client),
            session_factory=config_db.factory,
            linker=linker(),
        )
        return SimpleNamespace(
            service=service,
            pr=pr,
            body=pr.body,
            repo=repo_with_candidate(pr),
            client=client,
            retriever=retriever,
        )

    return make


def assert_preserved(config_db, case):
    assert case.pr.body == case.body and case.pr.edits == []
    row = config_db.session.scalar(select(PRIssueLink))
    assert (row.issue_number, row.reference_text, row.inference_reason) == (
        570,
        "Closes #570",
        "existing proof",
    )
    assert config_db.transactions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["stored", "settings_assignment"])
@pytest.mark.parametrize(
    "bad",
    [1.1, -0.1, float("nan"), float("inf"), -float("inf"), True, False, None, "bad"],
)
async def test_invalid_recall_threshold_cannot_retract_links(
    config_db, sync_case, monkeypatch, source, bad
):
    if source == "stored":
        config_db.store(THRESHOLD, str(bad))
        monkeypatch.setitem(
            config._dynamic_config_cache, THRESHOLD, ("0.8", float("inf"))
        )
    else:
        setattr(config_db.settings, THRESHOLD, bad)
    # An out-of-range threshold would make the real recall return no candidates.
    case = sync_case([changed_file()], candidates=[])
    result = await case.service.synchronize(case.repo, "o", "r", 618)

    assert not result.succeeded and result.failure == "ValueError"
    assert_preserved(config_db, case)
    case.retriever.retrieve.assert_not_awaited()
    case.client.call_with_retry.assert_not_awaited()


@pytest.mark.parametrize("bad", [1.1, -0.1, float("nan"), float("inf"), True, False])
def test_settings_reject_invalid_recall_threshold_before_coercion(bad):
    with pytest.raises(ValidationError):
        config.Settings(**{THRESHOLD: bad})


def test_environment_rejects_out_of_range_recall_threshold(monkeypatch):
    monkeypatch.setenv(THRESHOLD.upper(), "1.1")
    with pytest.raises(ValidationError):
        config.Settings(_env_file=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["stored", "settings"])
@pytest.mark.parametrize("value", [0, 0.8, 1])
async def test_valid_recall_threshold_allows_empty_replacement(
    config_db, sync_case, source, value
):
    if source == "stored":
        config_db.store(THRESHOLD, str(value))
    else:
        parsed = config.Settings(**{THRESHOLD: str(value)})
        setattr(config_db.settings, THRESHOLD, getattr(parsed, THRESHOLD))
    case = sync_case([changed_file()], candidates=[])
    result = await case.service.synchronize(case.repo, "o", "r", 618)

    assert result.succeeded and not result.relations
    assert case.retriever.retrieve.await_args.kwargs["similarity_threshold"] == value
    assert config_db.session.scalar(select(PRIssueLink)) is None
    assert "#570" not in case.pr.body and case.pr.edits
    case.client.call_with_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["cancel", "deadline", "both"])
@pytest.mark.parametrize("raises", [False, True])
async def test_recall_config_controls_win_before_retrieval(
    config_db, sync_case, monkeypatch, control, raises
):
    event = asyncio.Event()
    state = {"expired": False}
    deadline = SimpleNamespace(is_expired=lambda: state["expired"])
    original = config.get_dynamic_config

    async def read(key, **kwargs):
        if key != THRESHOLD:
            return await original(key, **kwargs)
        if control in {"cancel", "both"}:
            event.set()
        state["expired"] = control in {"deadline", "both"}
        if raises:
            raise RuntimeError("private provider detail")
        return float("nan")

    monkeypatch.setattr(config, "get_dynamic_config", read)
    case = sync_case([changed_file()], candidates=[])
    call = case.service.synchronize(
        case.repo, "o", "r", 618, cancel_event=event, deadline=deadline
    )
    if control == "deadline":
        result = await call
        assert not result.succeeded and result.failure == "deadline"
    else:
        with pytest.raises(ReviewCancelledError):
            await call
    assert_preserved(config_db, case)
    case.retriever.retrieve.assert_not_awaited()
    case.client.call_with_retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [None, "", RENAME, MODE])
@pytest.mark.parametrize("status", ["renamed", "modified"])
async def test_complete_zero_line_entry_does_not_block_other_code_evidence(
    config_db, sync_case, patch, status
):
    case = sync_case([changed_file(), zero_file(patch, status=status)])
    result = await case.service.synchronize(case.repo, "o", "r", 618)

    assert result.succeeded and result.relations[0]["relation"] == "closes"
    row = config_db.session.scalar(select(PRIssueLink))
    assert row.issue_number == 612 and row.reference_text == "Closes #612"
    assert "#570" not in case.pr.body and "#612" in case.pr.body
    packet = json.loads(
        case.client.call_with_retry.await_args.kwargs["messages"][1]["content"]
    )
    assert len(packet["files"]) == 2
    assert all(source["complete"] for source in packet["files"])
    zero = packet["files"][1]
    assert zero["additions"] == zero["deletions"] == 0
    assert zero["patch"] == (patch or "")
    assert case.retriever.retrieve.await_args.kwargs["similarity_threshold"] == 0.65


@pytest.mark.asyncio
@pytest.mark.parametrize("candidates", [[], [CANDIDATE]])
async def test_zero_line_only_pr_can_retract_stale_links(
    config_db, sync_case, candidates
):
    case = sync_case(
        [zero_file(RENAME), zero_file(None)], candidates=candidates, relations=[]
    )
    result = await case.service.synchronize(case.repo, "o", "r", 618)
    assert result.succeeded and result.relations == []
    assert config_db.session.scalar(select(PRIssueLink)) is None
    assert "#570" not in case.pr.body


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [None, "", RENAME])
@pytest.mark.parametrize("counts", [(1, 0), (0, 1)])
async def test_changed_lines_still_require_hunks(config_db, sync_case, patch, counts):
    case = sync_case(
        [changed_file(), zero_file(patch, additions=counts[0], deletions=counts[1])]
    )
    result = await case.service.synchronize(case.repo, "o", "r", 618)
    assert not result.succeeded and result.failure == "snapshot_incomplete"
    assert_preserved(config_db, case)
    case.retriever.retrieve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        "unrecognized metadata",
        "+unframed code",
        "@@ -0,0 +1 @@\n+hidden_code()",
        "@@ -0,0 +1,2 @@\n+truncated",
    ],
)
async def test_zero_counts_do_not_hide_malformed_or_changed_patch(
    config_db, sync_case, patch
):
    case = sync_case([changed_file(), zero_file(patch)])
    result = await case.service.synchronize(case.repo, "o", "r", 618)
    assert not result.succeeded and result.failure == "snapshot_incomplete"
    assert_preserved(config_db, case)
    case.retriever.retrieve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [False, None, -1, 0.5, "0"])
@pytest.mark.parametrize("field", ["additions", "deletions"])
async def test_zero_line_entry_requires_authoritative_integer_counts(
    config_db, sync_case, bad, field
):
    zero = zero_file(RENAME)
    setattr(zero, field, bad)
    case = sync_case([changed_file(), zero])
    result = await case.service.synchronize(case.repo, "o", "r", 618)
    assert not result.succeeded and result.failure == "snapshot_incomplete"
    assert_preserved(config_db, case)


@pytest.mark.asyncio
async def test_metadata_cannot_ground_a_changed_code_quote(config_db, sync_case):
    relation = {
        **RELATION,
        "evidence": [
            {
                **RELATION["evidence"][0],
                "path": "new.py",
                "code_quote": "rename to new.py",
            }
        ],
    }
    case = sync_case([changed_file(), zero_file(RENAME)], relations=[relation])
    result = await case.service.synchronize(case.repo, "o", "r", 618)
    assert not result.succeeded and result.failure == "ValueError"
    assert_preserved(config_db, case)
    case.client.call_with_retry.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["files", "input"])
async def test_zero_line_entries_still_consume_snapshot_budget(
    config_db, sync_case, budget
):
    if budget == "files":
        config_db.settings.pr_issue_max_files = 1
        files = [changed_file(), zero_file(RENAME)]
        failure = "snapshot_incomplete"
    else:
        config_db.settings.pr_issue_max_input_tokens = 1500
        files = [zero_file("rename to " + "x" * 10000)]
        failure = "input_budget"
    case = sync_case(files)
    result = await case.service.synchronize(case.repo, "o", "r", 618)
    assert not result.succeeded and result.failure == failure
    assert_preserved(config_db, case)
    case.retriever.retrieve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "counts", [{}, {"additions": 0}, {"additions": 1, "deletions": 0}]
)
async def test_direct_verifier_does_not_trust_missing_zero_line_provenance(
    config_db, counts
):
    client = configured_client(
        call_with_retry=AsyncMock(return_value=model_response([]))
    )
    result = await PRRelationVerifier(client).verify(
        pr_title="Title",
        pr_body="Body",
        candidates=[CANDIDATE],
        files=[{"path": "new.py", "patch": RENAME, "complete": True, **counts}],
    )
    assert not result.succeeded and result.failure == "ValueError"


@pytest.mark.parametrize("patch", ["", RENAME, MODE])
def test_unified_diff_without_count_provenance_remains_strict(patch):
    with pytest.raises(ValueError):
        parse_unified_diff(patch)
