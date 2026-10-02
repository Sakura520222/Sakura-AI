"""Regression cases for complete, fresh, fail-closed Issue retrieval."""

import asyncio
import threading
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.issue_embedding_service import IssueEmbeddingService


def issue(number=1, **changes):
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "human problem",
        "state": "open",
        "labels": [{"name": "bug"}],
        "state_reason": None,
        "updated_at": "2026-10-02T00:00:00Z",
    } | changes


class Collection:
    def __init__(self):
        self.metadata = {"repo_full_name": "owner/repo_issues"}
        self.docs = {}
        self.write_error = None

    def count(self):
        return len(self.docs)

    def modify(self, *, metadata):
        self.metadata = deepcopy(metadata)

    def get(self, *, ids, include):
        present = [i for i in ids if i in self.docs]
        return {
            "ids": present,
            "metadatas": [self.docs[i]["metadata"] for i in present],
            "documents": [self.docs[i]["content"] for i in present],
            "embeddings": [self.docs[i]["embedding"] for i in present],
        }

    def upsert(self, *, ids, embeddings, documents, metadatas):
        if self.write_error:
            raise self.write_error
        for i, emb, doc, meta in zip(
            ids, embeddings, documents, metadatas, strict=True
        ):
            self.docs[i] = {"embedding": emb, "content": doc, "metadata": meta}

    def delete(self, *, ids):
        for doc_id in ids:
            self.docs.pop(doc_id, None)

    def query(self, *, query_embeddings, n_results, where, include):
        docs = [
            (i, d)
            for i, d in self.docs.items()
            if not where or d["metadata"]["state"] == where["state"]
        ][:n_results]
        return {
            "ids": [[i for i, _ in docs]],
            "documents": [[d["content"] for _, d in docs]],
            "metadatas": [[d["metadata"] for _, d in docs]],
            "embeddings": [[d["embedding"] for _, d in docs]],
        }


class Repo:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []
        self.fail_page = False

    def get_issues(self, **kwargs):
        self.queries.append(kwargs)

        def generate():
            for row in self.rows:
                if kwargs["state"] != "all" and row["state"] != kwargs["state"]:
                    continue
                yield SimpleNamespace(**({"pull_request": None} | row), raw_data=row)
            if self.fail_page:
                raise RuntimeError("page unavailable")

        return generate()

    def get_issue(self, number):
        from github import UnknownObjectException

        row = next((row for row in self.rows if row["number"] == number), None)
        if row is None:
            raise UnknownObjectException(404, {"message": "Not Found"}, {})
        return SimpleNamespace(raw_data=row)


@pytest.fixture
def foundation(monkeypatch):
    from backend.core import config

    collection = Collection()
    repo = Repo([issue()])
    service = IssueEmbeddingService.__new__(IssueEmbeddingService)
    service.github_app = SimpleNamespace(
        get_repo_client=lambda *a: SimpleNamespace(get_repo=lambda n: repo)
    )

    async def write(key, docs):
        collection.upsert(
            ids=[d["id"] for d in docs],
            embeddings=[d["embedding"] for d in docs],
            documents=[d["content"] for d in docs],
            metadatas=[d["metadata"] for d in docs],
        )
        return len(docs)

    async def search(key, query_embedding, *, top_k, where):
        matches = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "embeddings"],
        )
        return [
            {
                "id": doc_id,
                "content": matches["documents"][0][i],
                "metadata": matches["metadatas"][0][i],
                "distance": 0.0,
            }
            for i, doc_id in enumerate(matches["ids"][0])
        ]

    async def delete(key, ids):
        collection.delete(ids=ids)
        return True

    service._vector_store = SimpleNamespace(
        get_or_create_collection=AsyncMock(return_value=collection),
        get_collection_count=AsyncMock(side_effect=lambda key: collection.count()),
        add_documents=write,
        upsert_documents=write,
        search=search,
        delete_documents=delete,
    )
    service._embedding_service = SimpleNamespace(
        embed_texts=AsyncMock(side_effect=lambda texts: [[1.0, 0.0] for _ in texts]),
        embed_query=AsyncMock(return_value=[1.0, 0.0]),
    )
    service._reranker_service = SimpleNamespace(
        client=None,
        rerank=AsyncMock(side_effect=lambda **kw: kw["docs"][: kw["top_k"]]),
    )
    values = {
        "issue_corpus_freshness_seconds": 0,
        "issue_corpus_batch_size": 1,
        "issue_candidate_pool_multiplier": 3,
        "issue_vector_store_rich_metadata": True,
    }
    config_reader = AsyncMock(side_effect=lambda key, **kw: values[key])
    monkeypatch.setattr(config, "get_dynamic_config", config_reader)
    from backend.services.issues import corpus_service

    monkeypatch.setattr(corpus_service, "get_dynamic_config", config_reader)
    from backend.services.issues import candidate_retriever

    monkeypatch.setattr(candidate_retriever, "get_dynamic_config", config_reader)
    return service, collection, repo, values


def test_strip_only_generated_sections_preserves_human_references():
    from backend.services.pr_body import strip_sakura_generated_sections

    body = "Human fixes #9\n<!-- sakura-ai-summary-start -->Fixes #1<!-- sakura-ai-summary-end -->\n<!-- sakura-ai-depgraph-start -->graph<!-- sakura-ai-depgraph-end -->\n<!-- sakura-ai-issue-links-start -->Closes #3<!-- sakura-ai-issue-links-end -->\nMore human"
    assert strip_sakura_generated_sections(body) == "Human fixes #9\n\n\n\nMore human"
    assert strip_sakura_generated_sections(None) == ""


@pytest.mark.asyncio
async def test_nonempty_legacy_collection_bootstraps_closed_and_new_issues(foundation):
    service, collection, repo, _ = foundation
    collection.docs["issue_99"] = {
        "metadata": {"number": "99", "state": "open"},
        "content": "old",
        "embedding": [1, 0],
    }
    repo.rows.append(issue(2, state="closed", state_reason="completed"))
    await service.index_repo_issues("owner", "repo")
    assert collection.docs["issue_2"]["content"] == "Issue 2\nhuman problem"
    assert collection.docs["issue_1"]["metadata"]["state"] == "open"
    assert repo.queries[0]["state"] == "all"
    assert "since" not in repo.queries[0]
    assert collection.metadata["issue_corpus_cursor"]


@pytest.mark.asyncio
async def test_incremental_reconciliation_refreshes_edit_close_and_reopen(foundation):
    service, collection, repo, _ = foundation
    await service.index_repo_issues("owner", "repo")
    cursor = collection.metadata["issue_corpus_cursor"]
    repo.rows = [
        issue(1, body="edited root cause", state="closed", state_reason="completed")
    ]
    await service.index_repo_issues("owner", "repo")
    assert collection.docs["issue_1"]["content"] == "Issue 1\nedited root cause"
    assert collection.docs["issue_1"]["metadata"]["state"] == "closed"
    assert repo.queries[-1]["since"] <= datetime.fromisoformat(cursor)
    repo.rows = [issue()]
    await service.index_repo_issues("owner", "repo")
    assert collection.docs["issue_1"]["metadata"]["state"] == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["page", "embed", "write"])
async def test_checkpoint_retained_after_partial_synchronization_failure(
    foundation, failure
):
    service, collection, repo, _ = foundation
    await service.index_repo_issues("owner", "repo")
    old = deepcopy(collection.metadata)
    repo.rows.append(issue(2))
    if failure == "page":
        repo.fail_page = True
    elif failure == "embed":
        service._embedding_service.embed_texts.side_effect = RuntimeError(
            "embed unavailable"
        )
    else:
        collection.write_error = RuntimeError("write unavailable")
    with pytest.raises(RuntimeError):
        await service.index_repo_issues("owner", "repo")
    assert collection.metadata == old


@pytest.mark.asyncio
async def test_retriever_hydrates_current_facts_excludes_pr_and_current_issue(
    foundation,
):
    from backend.services.issues.candidate_retriever import IssueCandidateRetriever

    service, _collection, repo, values = foundation
    repo.rows += [issue(2), issue(3), issue(4)]
    await service.index_repo_issues("owner", "repo")
    values["issue_corpus_freshness_seconds"] = 3600
    repo.rows[1] = issue(
        2,
        title="current title",
        body="current body",
        state="closed",
        labels=[{"name": "done"}],
        state_reason="completed",
    )
    repo.rows[2]["pull_request"] = {"url": "pr"}
    results = await IssueCandidateRetriever(service).retrieve(
        "owner",
        "repo",
        text="query",
        state="all",
        exclude_numbers=[1],
        top_k=5,
        similarity_threshold=0.8,
    )
    assert [r["number"] for r in results] == [2, 4]
    assert results[0] == {
        "number": 2,
        "title": "current title",
        "body": "current body",
        "state": "closed",
        "labels": ["done"],
        "state_reason": "completed",
        "similarity": 1.0,
        "content": "current title\ncurrent body",
    }


@pytest.mark.asyncio
async def test_incomplete_hydration_cannot_return_candidates(foundation):
    from backend.services.issues.candidate_retriever import IssueCandidateRetriever

    service, _collection, repo, values = foundation
    await service.index_repo_issues("owner", "repo")
    values["issue_corpus_freshness_seconds"] = 3600
    del repo.rows[0]["labels"]
    with pytest.raises(ValueError, match="Incomplete"):
        await IssueCandidateRetriever(service).retrieve(
            "owner",
            "repo",
            text="query",
            state="open",
            exclude_numbers=[],
            top_k=5,
            similarity_threshold=0.8,
        )


@pytest.mark.asyncio
async def test_low_actual_cosine_filtered_before_rerank(foundation):
    from backend.services.issues.candidate_retriever import IssueCandidateRetriever

    service, collection, repo, values = foundation
    repo.rows.append(issue(2))
    await service.index_repo_issues("owner", "repo")
    collection.docs["issue_2"]["embedding"] = [0.0, 1.0]
    values["issue_corpus_freshness_seconds"] = 3600

    async def rerank(**kwargs):
        assert [d["number"] for d in kwargs["docs"]] == [1]
        assert kwargs["strict"] is True
        return kwargs["docs"]

    service._reranker_service.rerank.side_effect = rerank
    results = await IssueCandidateRetriever(service).retrieve(
        "owner",
        "repo",
        text="query",
        state="open",
        exclude_numbers=[],
        top_k=5,
        similarity_threshold=0.8,
    )
    assert [r["number"] for r in results] == [1]
    assert results[0]["similarity"] == 1.0


@pytest.mark.asyncio
async def test_pr_retrieval_uses_only_human_body_and_defaults_open(foundation):
    service, _collection, repo, _ = foundation
    repo.rows.append(issue(2, state="closed"))
    results = await service.search_related_issues(
        "owner",
        "repo",
        "Title",
        "Human\n<!-- sakura-ai-summary-start -->generated<!-- sakura-ai-summary-end -->",
        [],
    )
    assert [r["number"] for r in results] == [1]
    assert service._embedding_service.embed_query.await_args.args == ("Title\nHuman",)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "malformed", "unavailable"])
async def test_strict_configured_reranker_failure_raises_preserving_rag_fallback(
    monkeypatch, failure
):
    from backend.services import embedding_service as module

    service = module.RerankerService.__new__(module.RerankerService)
    service.provider = "siliconflow"
    service.client = (
        SimpleNamespace(post=AsyncMock()) if failure != "unavailable" else None
    )
    service._refresh_client = lambda: None
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(
            rerank_top_k=5, rerank_score_threshold=0.6, rerank_model="model"
        ),
    )
    if failure == "http":
        service.client.post.side_effect = RuntimeError("provider unavailable")
    elif failure == "malformed":
        service.client.post.return_value = SimpleNamespace(
            raise_for_status=lambda: None, json=dict
        )
    docs = [{"content": "problem", "metadata": {"number": "1"}}]
    with pytest.raises((RuntimeError, ValueError)):
        await service.rerank("query", docs, strict=True)
    assert await service.rerank("query", docs) == docs


@pytest.mark.asyncio
async def test_explicitly_disabled_reranking_is_success(monkeypatch):
    from backend.services import embedding_service as module

    service = module.RerankerService.__new__(module.RerankerService)
    service.provider = "none"
    service.client = None
    service._refresh_client = lambda: None
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(rerank_top_k=5, rerank_score_threshold=0.6),
    )
    docs = [{"content": "problem", "metadata": {"number": "1"}}]
    assert await service.rerank("query", docs, strict=True) == docs


@pytest.mark.asyncio
async def test_retriever_cancellation_propagates(foundation):
    from backend.services.issues.candidate_retriever import IssueCandidateRetriever

    service, _, _, _ = foundation
    service._embedding_service.embed_texts.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await IssueCandidateRetriever(service).retrieve(
            "owner",
            "repo",
            text="query",
            state="open",
            exclude_numbers=[],
            top_k=5,
            similarity_threshold=0.8,
        )


@pytest.mark.asyncio
async def test_force_reindex_repairs_old_rows_even_before_incremental_cursor(
    foundation,
):
    service, collection, repo, _ = foundation
    await service.index_repo_issues("owner", "repo")
    repo.rows[0]["body"] = "server body after repair"
    await service.index_repo_issues("owner", "repo", force=True)
    assert "since" not in repo.queries[-1]
    assert collection.docs["issue_1"]["content"] == "Issue 1\nserver body after repair"


@pytest.mark.asyncio
async def test_configured_candidate_multiplier_recalls_past_low_similarity_rows(
    foundation,
):
    from backend.services.issues.candidate_retriever import IssueCandidateRetriever

    service, collection, repo, values = foundation
    repo.rows = [issue(i) for i in range(1, 6)]
    await service.index_repo_issues("owner", "repo")
    values["issue_corpus_freshness_seconds"] = 3600
    values["issue_candidate_pool_multiplier"] = 5
    for i in range(1, 5):
        collection.docs[f"issue_{i}"]["embedding"] = [0.0, 1.0]
    results = await IssueCandidateRetriever(service).retrieve(
        "owner",
        "repo",
        text="query",
        state="all",
        exclude_numbers=[],
        top_k=1,
        similarity_threshold=0.8,
    )
    assert [row["number"] for row in results] == [5]


@pytest.mark.asyncio
async def test_failed_initial_sync_never_creates_cursor(foundation):
    service, collection, repo, _ = foundation
    repo.fail_page = True
    with pytest.raises(RuntimeError, match="page unavailable"):
        await service.index_repo_issues("owner", "repo")
    assert "issue_corpus_cursor" not in collection.metadata
    repo.fail_page = False
    await service.index_repo_issues("owner", "repo")
    assert "since" not in repo.queries[-1]


@pytest.mark.asyncio
async def test_checkpoint_survives_new_service_and_dynamic_freshness_change(foundation):
    service, collection, repo, values = foundation
    await service.index_repo_issues("owner", "repo")
    restarted = IssueEmbeddingService.__new__(IssueEmbeddingService)
    restarted.__dict__.update(service.__dict__)
    values["issue_corpus_freshness_seconds"] = 3600
    assert (await restarted.index_repo_issues("owner", "repo"))["status"] == "cached"
    assert len(repo.queries) == 1
    values["issue_corpus_freshness_seconds"] = 0
    repo.rows.append(issue(2))
    await restarted.index_repo_issues("owner", "repo")
    assert "since" in repo.queries[-1]
    assert "issue_2" in collection.docs


@pytest.mark.asyncio
async def test_valid_enrichment_preserved_but_edited_source_drops_stale_enrichment(
    foundation,
):
    service, collection, repo, _ = foundation
    await service.index_repo_issues("owner", "repo")
    collection.docs["issue_1"]["metadata"]["category"] = "bug"
    await service.index_repo_issues("owner", "repo")
    assert collection.docs["issue_1"]["metadata"]["category"] == "bug"
    repo.rows[0]["body"] = "new problem"
    await service.index_repo_issues("owner", "repo")
    assert "category" not in collection.docs["issue_1"]["metadata"]


@pytest.mark.asyncio
async def test_incomplete_embedding_batch_does_not_write_or_checkpoint(foundation):
    service, collection, _, _ = foundation
    service._embedding_service.embed_texts.side_effect = lambda texts: []
    with pytest.raises(ValueError, match="Incomplete"):
        await service.index_repo_issues("owner", "repo")
    assert not collection.docs
    assert "issue_corpus_cursor" not in collection.metadata


@pytest.mark.asyncio
async def test_github_client_failure_does_not_checkpoint_empty_success(foundation):
    service, collection, _, _ = foundation
    service.github_app.get_repo_client = lambda *args: None
    with pytest.raises(RuntimeError, match="unavailable"):
        await service.index_repo_issues("owner", "repo")
    assert "issue_corpus_cursor" not in collection.metadata


@pytest.mark.asyncio
async def test_chroma_collection_creation_runs_off_event_loop():
    from backend.services.vector_store import VectorStore

    main_thread = threading.get_ident()
    store = VectorStore.__new__(VectorStore)
    expected = object()

    def create(**kwargs):
        assert kwargs["metadata"]["repo_full_name"] == "owner/repo_issues"
        assert threading.get_ident() != main_thread
        return expected

    store.client = SimpleNamespace(get_or_create_collection=create)
    assert await store.get_or_create_collection("owner/repo_issues") is expected


@pytest.mark.asyncio
async def test_ai_enrichment_cannot_replace_authoritative_github_body(foundation):
    service, collection, repo, _ = foundation
    repo.rows[0] = issue(body="authoritative problem", title="current title")
    success = await service.upsert_issue(
        "owner",
        "repo",
        1,
        title="old title",
        body="AI summary",
        state="open",
        analysis_metadata={"category": "bug"},
    )
    assert success is True
    stored = collection.docs["issue_1"]
    assert stored["content"] == "current title\nauthoritative problem"
    assert stored["metadata"]["summary"] == "AI summary"
    assert stored["metadata"]["category"] == "bug"
    await service.index_repo_issues("owner", "repo")
    assert collection.docs["issue_1"]["metadata"]["summary"] == "AI summary"


@pytest.mark.asyncio
async def test_issue_callers_can_select_closed_and_bootstrap_excludes_prs(foundation):
    service, collection, repo, _ = foundation
    repo.rows += [issue(2, state="closed"), issue(3, pull_request={"url": "pr"})]
    rows = await service.search_related_issues(
        "owner", "repo", "Issue query", "body", [], state_filter="closed"
    )
    assert [row["number"] for row in rows] == [2]
    assert "issue_3" not in collection.docs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"results": [{"index": 0, "relevance_score": True}]},
        {"results": [{"index": 0, "relevance_score": float("nan")}]},
        {"results": [{"index": 20, "relevance_score": 1.0}]},
        {
            "results": [
                {"index": 0, "relevance_score": 1.0},
                {"index": 0, "relevance_score": 1.0},
            ]
        },
    ],
)
async def test_strict_reranker_rejects_malformed_provider_results(
    monkeypatch, response
):
    from backend.services import ai_usage_service
    from backend.services import embedding_service as module

    service = module.RerankerService.__new__(module.RerankerService)
    service.provider = "siliconflow"
    service.client = SimpleNamespace(
        post=AsyncMock(
            return_value=SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: response
            )
        )
    )
    service._refresh_client = lambda: None
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(
            rerank_top_k=5, rerank_score_threshold=0.6, rerank_model="model"
        ),
    )
    monkeypatch.setattr(ai_usage_service, "record_ai_usage_best_effort", AsyncMock())
    with pytest.raises(ValueError, match="Malformed"):
        await service.rerank("query", [{"content": "problem"}], strict=True)


@pytest.mark.asyncio
async def test_overflow_in_embedding_norm_cannot_admit_opposite_candidate(foundation):
    from backend.services.issues.candidate_retriever import IssueCandidateRetriever

    service, collection, _, values = foundation
    await service.index_repo_issues("owner", "repo")
    values["issue_corpus_freshness_seconds"] = 3600
    collection.docs["issue_1"]["embedding"] = [-1e308, 0.0]
    service._embedding_service.embed_query.return_value = [1e308, 0.0]
    assert (
        await IssueCandidateRetriever(service).retrieve(
            "owner",
            "repo",
            text="query",
            state="all",
            exclude_numbers=[],
            top_k=5,
            similarity_threshold=0.8,
        )
        == []
    )


def timestamped_source(monkeypatch, repo):
    """GitHub updates older than a cursor must not magically repair stale writes."""
    from backend.services.issues import corpus_service

    origin = datetime(2026, 10, 2, tzinfo=UTC)
    clock = [origin]
    monkeypatch.setattr(corpus_service, "now_utc", lambda: clock[0])

    def updated_since(**kwargs):
        for row in repo.rows:
            updated = datetime.fromisoformat(row["updated_at"])
            if "since" not in kwargs or updated >= kwargs["since"]:
                yield SimpleNamespace(raw_data=deepcopy(row))

    repo.get_issues = updated_since
    return origin, clock


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["upsert", "checkpoint"])
async def test_cancelled_corpus_mutation_drains_before_retry_owns_repository(
    foundation, monkeypatch, mutation
):
    service, collection, repo, _ = foundation
    origin, clock = timestamped_source(monkeypatch, repo)
    repo.rows[0] = issue(body="old", updated_at=origin.isoformat())
    started, release = threading.Event(), threading.Event()
    original = getattr(collection, "upsert" if mutation == "upsert" else "modify")

    def blocked(**kwargs):
        old_write = (
            kwargs.get("documents") == ["Issue 1\nold"]
            if mutation == "upsert"
            else kwargs["metadata"].get("issue_corpus_cursor") == origin.isoformat()
        )
        if old_write:
            started.set()
            assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(
        collection, "upsert" if mutation == "upsert" else "modify", blocked
    )
    initial = asyncio.create_task(service.index_repo_issues("owner", "repo"))
    retry = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        initial.cancel()
        # Dispatch cancellation without finishing the still-running write thread.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not initial.done(), (
            "Cancellation released ownership before mutation drained"
        )
        repo.rows[0] = issue(
            body="new", updated_at=(origin + timedelta(seconds=2)).isoformat()
        )
        clock[0] = origin + timedelta(seconds=5)
        retry = asyncio.create_task(service.index_repo_issues("owner", "repo"))
        await asyncio.sleep(0)
        assert not retry.done()
        # Repeated cancellation must not interrupt the drain or release ownership.
        initial.cancel()
        await asyncio.sleep(0)
        assert not initial.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await initial
        await retry
        assert collection.metadata["issue_corpus_cursor"] == clock[0].isoformat()
        clock[0] = origin + timedelta(seconds=10)
        await service.index_repo_issues("owner", "repo")
        assert collection.docs["issue_1"]["metadata"]["body"] == "new"
    finally:
        release.set()
        await asyncio.gather(
            initial, *([retry] if retry else []), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_delayed_plain_upsert_cannot_regress_facts_after_checkpoint(
    foundation, monkeypatch
):
    from backend.services.issues.candidate_retriever import IssueCandidateRetriever

    service, collection, repo, _ = foundation
    origin, clock = timestamped_source(monkeypatch, repo)
    repo.rows[0] = issue(
        body="current",
        state="closed",
        state_reason="completed",
        updated_at=(origin + timedelta(seconds=2)).isoformat(),
    )
    clock[0] = origin + timedelta(seconds=5)
    await service.index_repo_issues("owner", "repo")
    assert await service.upsert_issue(
        "owner", "repo", 1, title="old", body="old reopened", state="open"
    )
    clock[0] = origin + timedelta(seconds=10)
    rows = await IssueCandidateRetriever(service).retrieve(
        "owner",
        "repo",
        text="query",
        state="closed",
        exclude_numbers=[],
        top_k=5,
        similarity_threshold=0.8,
    )
    assert [row["number"] for row in rows] == [1]
    assert collection.docs["issue_1"]["metadata"]["body"] == "current"
    assert collection.docs["issue_1"]["metadata"]["state"] == "closed"


@pytest.mark.asyncio
async def test_enrichment_snapshot_and_reconciliation_share_writer_ownership(
    foundation,
):
    service, collection, repo, _ = foundation
    await service.index_repo_issues("owner", "repo")
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked_embedding(text):
        started.set()
        await release.wait()
        return [1.0, 0.0]

    service._embedding_service.embed_query.side_effect = blocked_embedding
    enrichment = asyncio.create_task(
        service.upsert_issue(
            "owner",
            "repo",
            1,
            title="Issue 1",
            body="summary",
            state="open",
            analysis_metadata={"category": "bug"},
        )
    )
    sync = None
    try:
        await started.wait()
        repo.rows[0] = issue(body="new body", state="closed", state_reason="completed")
        sync = asyncio.create_task(service.index_repo_issues("owner", "repo"))
        # The second writer must remain blocked while the first embedding is
        # deliberately gated. Shield keeps the blocked sync available afterward.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(sync), timeout=0.1)
        release.set()
        assert await enrichment
        await sync
        assert collection.docs["issue_1"]["metadata"]["body"] == "new body"
        assert collection.docs["issue_1"]["metadata"]["state"] == "closed"
    finally:
        release.set()
        await asyncio.gather(
            enrichment, *([sync] if sync else []), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_delayed_close_hydrates_reopened_issue(foundation):
    service, collection, _, _ = foundation
    await service.index_repo_issues("owner", "repo")
    assert await service.close_issue("owner", "repo", 1)
    assert collection.docs["issue_1"]["metadata"]["state"] == "open"


@pytest.mark.asyncio
async def test_delayed_delete_does_not_remove_current_issue(foundation):
    service, collection, _, _ = foundation
    await service.index_repo_issues("owner", "repo")
    assert await service.remove_issue("owner", "repo", 1) is False
    assert "issue_1" in collection.docs


@pytest.mark.asyncio
async def test_confirmed_github_deletion_removes_shared_corpus_row(foundation):
    service, collection, repo, _ = foundation
    await service.index_repo_issues("owner", "repo")
    repo.rows.clear()
    assert await service.remove_issue("owner", "repo", 1)
    assert "issue_1" not in collection.docs


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["upsert", "close", "remove"])
async def test_public_writer_cancellation_retains_ownership_until_mutation_drains(
    foundation, monkeypatch, writer
):
    service, collection, repo, _ = foundation
    origin, clock = timestamped_source(monkeypatch, repo)
    repo.rows[0] = issue(body="old", updated_at=origin.isoformat())
    await service.index_repo_issues("owner", "repo")
    started, release = threading.Event(), threading.Event()
    mutation = "delete" if writer == "remove" else "upsert"
    original = getattr(collection, mutation)

    def blocked(**kwargs):
        started.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(collection, mutation, blocked)
    if writer == "remove":
        repo.rows.clear()
        operation = service.remove_issue("owner", "repo", 1)
    elif writer == "close":
        operation = service.close_issue("owner", "repo", 1)
    else:
        operation = service.upsert_issue(
            "owner", "repo", 1, title="stale title", body="stale body", state="open"
        )
    pending = asyncio.create_task(operation)
    retry = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not pending.done()
        repo.rows = [
            issue(
                body="new",
                state="closed",
                state_reason="completed",
                updated_at=(origin + timedelta(seconds=2)).isoformat(),
            )
        ]
        clock[0] = origin + timedelta(seconds=5)
        retry = asyncio.create_task(service.index_repo_issues("owner", "repo"))
        await asyncio.sleep(0)
        assert not retry.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await retry
        assert collection.docs["issue_1"]["metadata"]["body"] == "new"
        assert collection.docs["issue_1"]["metadata"]["state"] == "closed"
        assert collection.metadata["issue_corpus_cursor"] == clock[0].isoformat()
    finally:
        release.set()
        await asyncio.gather(
            pending, *([retry] if retry else []), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["upsert", "close", "remove"])
async def test_writer_source_permission_failure_preserves_corpus_and_bool_boundary(
    foundation, monkeypatch, writer
):
    from github import GithubException

    service, collection, repo, _ = foundation
    await service.index_repo_issues("owner", "repo")
    previous = deepcopy(collection.docs)
    checkpoint = deepcopy(collection.metadata)

    def denied(number):
        raise GithubException(403, {"message": "Forbidden"}, {})

    monkeypatch.setattr(repo, "get_issue", denied)
    if writer == "upsert":
        result = await service.upsert_issue(
            "owner", "repo", 1, title="stale", body="stale", state="closed"
        )
    elif writer == "close":
        result = await service.close_issue("owner", "repo", 1)
    else:
        result = await service.remove_issue("owner", "repo", 1)
    assert result is False
    assert collection.docs == previous
    assert collection.metadata == checkpoint


@pytest.mark.asyncio
async def test_cancellation_during_final_count_cannot_leave_committed_checkpoint(
    foundation, monkeypatch
):
    service, collection, _, _ = foundation
    started, release = threading.Event(), threading.Event()
    original = collection.count

    def blocked_final_count():
        if collection.docs:
            started.set()
            assert release.wait(5)
        return original()

    monkeypatch.setattr(collection, "count", blocked_final_count)
    pending = asyncio.create_task(service.index_repo_issues("owner", "repo"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert "issue_corpus_cursor" not in collection.metadata
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def retrieve_candidates(service, *, caller="issue"):
    if caller == "pr":
        return await service.search_related_issues(
            "owner", "repo", "query", "human body", [], top_k=1
        )
    from backend.services.issues.candidate_retriever import IssueCandidateRetriever

    return await IssueCandidateRetriever(service).retrieve(
        "owner",
        "repo",
        text="query",
        state="all",
        exclude_numbers=[],
        top_k=1,
        similarity_threshold=0.8,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["pr", "issue"])
@pytest.mark.parametrize("force", [False, True])
async def test_deleted_recalled_candidate_is_pruned_without_losing_valid_candidate(
    foundation, monkeypatch, caller, force
):
    service, collection, repo, values = foundation
    origin, clock = timestamped_source(monkeypatch, repo)
    repo.rows.append(issue(2))
    clock[0] = origin + timedelta(seconds=5)
    await service.index_repo_issues("owner", "repo")
    # The deleted row ranks ahead of a still-valid result. Its missed webhook
    # cannot be repaired by either an incremental fetch or all-state upserts.
    collection.docs["issue_2"]["embedding"] = [0.9, 0.1]
    repo.rows = [issue(2)]
    clock[0] = origin + timedelta(seconds=10)
    await service.index_repo_issues("owner", "repo", force=force)
    assert "issue_1" in collection.docs
    collection.docs["issue_2"]["embedding"] = [0.9, 0.1]
    checkpoint = deepcopy(collection.metadata)
    values["issue_corpus_freshness_seconds"] = 3600
    for _ in range(2):
        rows = await retrieve_candidates(service, caller=caller)
        assert [row["number"] for row in rows] == [2]
        assert "issue_1" not in collection.docs
        assert collection.metadata == checkpoint


@pytest.mark.asyncio
async def test_recalled_404_rechecks_after_newer_writer_and_preserves_reappeared_issue(
    foundation, monkeypatch
):
    from github import UnknownObjectException

    from backend.services.issues.corpus_service import IssueCorpusService

    service, collection, repo, values = foundation
    origin, clock = timestamped_source(monkeypatch, repo)
    repo.rows.append(issue(2))
    await service.index_repo_issues("owner", "repo")
    values["issue_corpus_freshness_seconds"] = 3600
    initial_read, release_read = threading.Event(), threading.Event()
    writer_started, release_writer = asyncio.Event(), asyncio.Event()
    writer_finished = threading.Event()
    original_get = repo.get_issue
    first = True

    def read(number):
        nonlocal first
        if number == 1 and first:
            first = False
            initial_read.set()
            assert release_read.wait(5)
            raise UnknownObjectException(404, {"message": "Not Found"}, {})
        if number == 1 and initial_read.is_set() and release_read.is_set():
            assert writer_finished.is_set(), "Recheck ran outside writer ownership"
        return original_get(number)

    async def blocked_embedding(text):
        writer_started.set()
        await release_writer.wait()
        return [1.0, 0.0]

    monkeypatch.setattr(repo, "get_issue", read)
    pending = asyncio.create_task(retrieve_candidates(service))
    writer = None
    try:
        assert await asyncio.to_thread(initial_read.wait, 5)
        repo.rows[0] = issue(
            body="newer reappeared source",
            updated_at=(origin + timedelta(seconds=2)).isoformat(),
        )
        service._embedding_service.embed_query.side_effect = blocked_embedding
        writer = asyncio.create_task(
            IssueCorpusService(service).update_issue("owner", "repo", 1)
        )
        await asyncio.wait_for(writer_started.wait(), 5)
        release_read.set()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending), 0.1)
        writer_finished.set()
        release_writer.set()
        await writer
        rows = await pending
        assert [row["number"] for row in rows] == [2]
        assert (
            collection.docs["issue_1"]["metadata"]["body"] == "newer reappeared source"
        )
        clock[0] = origin + timedelta(seconds=10)
        values["issue_corpus_freshness_seconds"] = 0
        await service.index_repo_issues("owner", "repo")
        assert (
            collection.docs["issue_1"]["metadata"]["body"] == "newer reappeared source"
        )
        service._embedding_service.embed_query.side_effect = None
        current = await retrieve_candidates(service)
        assert [row["number"] for row in current] == [1]
        assert current[0]["body"] == "newer reappeared source"
    finally:
        release_read.set()
        release_writer.set()
        await asyncio.gather(
            pending, *([writer] if writer else []), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["hydration", "recheck"])
@pytest.mark.parametrize("failure", ["permission", "server", "network", "unavailable"])
async def test_candidate_failure_is_observable_and_never_prunes_rows(
    foundation, monkeypatch, phase, failure
):
    from github import GithubException, UnknownObjectException

    service, collection, repo, values = foundation
    await service.index_repo_issues("owner", "repo")
    values["issue_corpus_freshness_seconds"] = 3600
    previous = deepcopy(collection.docs)
    checkpoint = deepcopy(collection.metadata)
    calls = 0

    def fail(number):
        nonlocal calls
        calls += 1
        if phase == "recheck" and calls == 1:
            if failure == "unavailable":
                service.github_app.get_repo_client = lambda *args: None
            raise UnknownObjectException(404, {"message": "Not Found"}, {})
        if failure == "permission":
            raise GithubException(403, {"message": "Forbidden"}, {})
        if failure == "server":
            raise UnknownObjectException(500, {"message": "Server failure"}, {})
        raise RuntimeError("network unavailable")

    if failure == "unavailable" and phase == "hydration":
        service.github_app.get_repo_client = lambda *args: None
    monkeypatch.setattr(repo, "get_issue", fail)
    expected = GithubException if failure in {"permission", "server"} else RuntimeError
    with pytest.raises(expected) as raised:
        await retrieve_candidates(service)
    if failure in {"permission", "server"}:
        assert raised.value.status == (403 if failure == "permission" else 500)
    assert collection.docs == previous
    assert collection.metadata == checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_fails", [False, True])
async def test_cancelled_candidate_pruning_drains_delete_before_next_writer(
    foundation, monkeypatch, delete_fails
):
    service, collection, repo, values = foundation
    origin, clock = timestamped_source(monkeypatch, repo)
    repo.rows.append(issue(2))
    await service.index_repo_issues("owner", "repo")
    repo.rows = [issue(2)]
    values["issue_corpus_freshness_seconds"] = 3600
    started, release = threading.Event(), threading.Event()
    original = collection.delete

    def blocked_delete(**kwargs):
        started.set()
        assert release.wait(5)
        if delete_fails:
            raise RuntimeError("delete unavailable")
        original(**kwargs)

    monkeypatch.setattr(collection, "delete", blocked_delete)
    pending = asyncio.create_task(retrieve_candidates(service))
    retry = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not pending.done(), (
            "Cancellation released ownership before deletion drained"
        )
        repo.rows.insert(
            0,
            issue(
                body="new after deletion",
                updated_at=(origin + timedelta(seconds=2)).isoformat(),
            ),
        )
        clock[0] = origin + timedelta(seconds=5)
        values["issue_corpus_freshness_seconds"] = 0
        retry = asyncio.create_task(service.index_repo_issues("owner", "repo"))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(retry), 0.1)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await retry
        assert collection.docs["issue_1"]["metadata"]["body"] == "new after deletion"
        clock[0] = origin + timedelta(seconds=10)
        await service.index_repo_issues("owner", "repo")
        assert collection.docs["issue_1"]["metadata"]["body"] == "new after deletion"
    finally:
        release.set()
        await asyncio.gather(
            pending, *([retry] if retry else []), return_exceptions=True
        )
