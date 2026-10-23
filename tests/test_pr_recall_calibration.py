"""Calibrated recall admits observed cosine without deciding the relation."""

import json
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger
from sqlalchemy import select

from backend.core import config
from backend.models import database
from backend.models.database import AppConfig, PRIssueLink
from backend.services.issues import candidate_retriever, pr_budget, pr_verifier
from backend.services.issues.candidate_retriever import IssueCandidateRetriever
from backend.services.issues.pr_link_sync import PRRelationSyncService
from backend.services.issues.pr_verifier import PRRelationVerifier
from tests import test_issue_candidate_foundation
from tests.test_pr_issue_budget import Pull, changed_file
from tests.test_pr_issue_relations import RELATION, configured_client, linker
from tests.test_relation_configuration_guards import config_db as _config_db
from tests.test_relation_configuration_guards import model_response

foundation = test_issue_candidate_foundation.foundation
config_db = _config_db
_read_config = config.get_dynamic_config
KEY = "semantic_issue_similarity_threshold"


@pytest.fixture
def recall_case(foundation, config_db, monkeypatch):
    service, collection, repo, values = foundation
    repo.rows = [
        test_issue_candidate_foundation.issue(
            645,
            title="CI retry failure",
            body="Retry transient TLS EOF\nUNLOGGED_ISSUE_BODY",
        ),
        test_issue_candidate_foundation.issue(569, body="Different problem"),
    ]
    monkeypatch.setattr(config, "get_dynamic_config", _read_config)
    monkeypatch.setattr(candidate_retriever, "get_dynamic_config", _read_config)
    monkeypatch.setattr(pr_budget, "get_dynamic_config", _read_config)
    monkeypatch.setattr(pr_verifier, "get_dynamic_config", _read_config)
    config_db.settings.semantic_issue_similarity_threshold = (
        config.Settings.model_fields[KEY].default
    )
    records = []
    sink = logger.add(records.append, level="INFO", format="{message}")
    case = SimpleNamespace(
        service=service,
        collection=collection,
        repo=repo,
        values=values,
        records=records,
    )
    yield case
    logger.remove(sink)


async def seed(case):
    await case.service.index_repo_issues("o", "r")
    case.values["issue_corpus_freshness_seconds"] = 3600
    for number, cosine in ((645, 0.7148), (569, 0.61)):
        case.collection.docs[f"issue_{number}"]["embedding"] = [
            cosine,
            math.sqrt(1 - cosine**2),
        ]


def recall_records(case):
    return [
        entry.record
        for entry in case.records
        if entry.record["message"].startswith("Issue candidate recall")
    ]


async def synchronize(case, config_db, *, relations):
    pr = Pull(lambda: [changed_file()])
    pr.title = "UNLOGGED_HUMAN_QUERY"
    case.repo.get_pull = lambda number: pr
    client = configured_client(
        call_with_retry=AsyncMock(return_value=model_response(relations))
    )
    service = PRRelationSyncService(
        retriever=IssueCandidateRetriever(case.service),
        verifier=PRRelationVerifier(client),
        session_factory=config_db.factory,
        linker=linker(),
    )
    result = await service.synchronize(case.repo, "o", "r", 649)
    return result, client, pr


def test_default_and_new_seed_use_calibrated_pr_threshold(monkeypatch):
    monkeypatch.delenv(KEY.upper(), raising=False)
    settings = config.Settings(_env_file=None)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    assert config.Settings.model_fields[KEY].default == 0.65
    assert settings.semantic_issue_similarity_threshold == 0.65
    rows = []
    database._append_dynamic_config_defaults(rows)
    assert next(row.key_value for row in rows if row.key_name == KEY) == "0.65"


@pytest.mark.asyncio
async def test_observed_cosine_enters_real_verifier_with_new_default(
    recall_case, config_db
):
    await seed(recall_case)
    result, client, pr = await synchronize(
        recall_case, config_db, relations=[{**RELATION, "number": 645}]
    )
    client.call_with_retry.assert_awaited_once()
    packet = json.loads(
        client.call_with_retry.await_args.kwargs["messages"][1]["content"]
    )
    assert [item["number"] for item in packet["issues"]] == [645]
    assert result.succeeded and result.relations[0]["number"] == 645
    row = config_db.session.scalar(select(PRIssueLink))
    assert row.issue_number == 645 and row.reference_text == "Closes #645"
    assert "#645" in pr.body
    (record,) = recall_records(recall_case)
    message = record["message"]
    assert record["level"].name == "INFO"
    for metric in (
        "threshold=0.6500",
        "recalled=2",
        "eligible=2",
        "cosine_matches=1",
        "hydrated=1",
        "candidates=1",
        "max_cosine=0.7148",
    ):
        assert metric in message
    assert "UNLOGGED_HUMAN_QUERY" not in message
    assert "UNLOGGED_ISSUE_BODY" not in message


@pytest.mark.asyncio
async def test_recalled_candidate_does_not_establish_unverified_relation(
    recall_case, config_db
):
    await seed(recall_case)
    result, client, pr = await synchronize(recall_case, config_db, relations=[])
    client.call_with_retry.assert_awaited_once()
    assert result.succeeded and result.relations == []
    assert config_db.session.scalar(select(PRIssueLink)) is None
    assert "#645" not in pr.body


@pytest.mark.asyncio
async def test_saved_point_eight_override_is_preserved_and_logged(
    recall_case, config_db
):
    config_db.store(KEY, "0.8")
    await database.insert_default_configs_async()
    assert await _read_config(KEY, fresh=True) == 0.8
    await seed(recall_case)
    result, client, _pr = await synchronize(recall_case, config_db, relations=[])
    assert result.succeeded and result.relations == []
    client.call_with_retry.assert_not_awaited()
    row = config_db.session.scalar(select(AppConfig).where(AppConfig.key_name == KEY))
    assert row.key_value == "0.8"
    (record,) = recall_records(recall_case)
    for metric in (
        "threshold=0.8000",
        "recalled=2",
        "cosine_matches=0",
        "candidates=0",
        "max_cosine=0.7148",
    ):
        assert metric in record["message"]


@pytest.mark.asyncio
async def test_empty_corpus_has_an_info_summary_without_an_embedding(recall_case):
    recall_case.repo.rows.clear()
    result = await IssueCandidateRetriever(recall_case.service).retrieve(
        "o",
        "r",
        text="UNLOGGED_QUERY",
        state="open",
        exclude_numbers=[],
        top_k=5,
        similarity_threshold=0.65,
    )
    assert result == []
    recall_case.service._embedding_service.embed_query.assert_not_awaited()
    (record,) = recall_records(recall_case)
    for metric in ("recalled=0", "eligible=0", "candidates=0", "max_cosine=none"):
        assert metric in record["message"]
    assert "UNLOGGED_QUERY" not in record["message"]


@pytest.mark.asyncio
async def test_rerank_empty_is_distinct_from_cosine_empty_in_info(recall_case):
    await seed(recall_case)
    rerank = recall_case.service._reranker_service.rerank
    rerank.side_effect = None
    rerank.return_value = []
    result = await IssueCandidateRetriever(recall_case.service).retrieve(
        "o",
        "r",
        text="UNLOGGED_QUERY",
        state="open",
        exclude_numbers=[],
        top_k=5,
        similarity_threshold=0.65,
    )
    assert result == []
    rerank.assert_awaited_once()
    (record,) = recall_records(recall_case)
    for metric in (
        "cosine_matches=1",
        "hydrated=1",
        "candidates=0",
        "max_cosine=0.7148",
    ):
        assert metric in record["message"]


def test_explicit_environment_threshold_still_overrides_default(monkeypatch):
    monkeypatch.setenv(KEY.upper(), "0.8")
    assert config.Settings(_env_file=None).semantic_issue_similarity_threshold == 0.8
