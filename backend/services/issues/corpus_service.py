"""Durable, bounded synchronization of the existing repository Issue index."""

import asyncio
import json
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from itertools import islice
from weakref import WeakKeyDictionary

from backend.core.config import get_dynamic_config
from backend.core.time_service import now_utc

_locks = WeakKeyDictionary()
_CURSOR = "issue_corpus_cursor"


async def _await_owned(operation):
    """Drain an in-flight operation before relinquishing writer ownership.

    Cancelling to_thread only cancels its awaiter, not the running thread.
    Shield the operation and observe its completion while the caller's lock is
    still held, including repeated cancellation, then propagate cancellation.
    """
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception as error:
                raise cancelled from error
        try:
            task.result()
        except Exception as error:
            raise cancelled from error
        raise


async def _mutate(callback, **kwargs):
    return await _await_owned(asyncio.to_thread(callback, **kwargs))


def snapshot_issue(issue) -> dict:
    """Read loaded GitHub facts in a worker thread; reject missing facts."""
    raw = issue if isinstance(issue, dict) else issue.raw_data
    if not isinstance(raw, dict):
        raise ValueError("Incomplete GitHub Issue facts")
    if raw.get("pull_request") is not None:
        return {}
    required = {"number", "title", "body", "state", "labels", "state_reason"}
    if not required <= raw.keys():
        raise ValueError("Incomplete GitHub Issue facts")
    number = raw["number"]
    if (
        type(number) is not int
        or number <= 0
        or not isinstance(raw["title"], str)
        or not raw["title"].strip()
        or raw["body"] is not None
        and not isinstance(raw["body"], str)
        or raw["state"] not in {"open", "closed"}
        or not isinstance(raw["labels"], list)
        or raw["state_reason"] is not None
        and not isinstance(raw["state_reason"], str)
    ):
        raise ValueError("Incomplete or malformed GitHub Issue facts")
    labels = []
    for label in raw["labels"]:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise ValueError("Incomplete GitHub Issue labels")
        labels.append(label["name"])
    return {
        "number": number,
        "title": raw["title"],
        "body": raw["body"] or "",
        "state": raw["state"],
        "labels": labels,
        "state_reason": raw["state_reason"],
        "updated_at": raw.get("updated_at", ""),
    }


class IssueCorpusService:
    """One all-state corpus, shared with webhook and legacy embedding APIs.

    The checkpoint is collection metadata, so a service/process restart still
    performs incremental reconciliation. Missing checkpoints bootstrap legacy
    collections. Partial batch writes are idempotent and never advance it.
    """

    def __init__(self, issue_embedding_service):
        self.service = issue_embedding_service

    async def collection(self, owner: str, name: str):
        # Store construction can initialize persistent Chroma and must also run
        # outside the event loop. get_or_create returns fresh server metadata.
        store = await asyncio.to_thread(lambda: self.service.vector_store)
        return await _await_owned(
            store.get_or_create_collection(self.service._collection_key(owner, name))
        )

    def get_repo(self, owner: str, name: str):
        client = self.service.github_app.get_repo_client(owner, name)
        if client is None:
            raise RuntimeError("GitHub Issue corpus client unavailable")
        return client.get_repo(f"{owner}/{name}")

    @asynccontextmanager
    async def ownership(self, owner: str, name: str):
        loop_locks = _locks.setdefault(asyncio.get_running_loop(), {})
        lock = loop_locks.setdefault(
            (owner.casefold(), name.casefold()), asyncio.Lock()
        )
        async with lock:
            yield

    async def reconcile(self, owner: str, name: str, *, force: bool = False) -> dict:
        async with self.ownership(owner, name):
            return await self._reconcile(owner, name, force=force)

    async def update_issue(
        self, owner: str, name: str, number: int, *, enrichment: dict | None = None
    ) -> None:
        async with self.ownership(owner, name):
            repo = await asyncio.to_thread(self.get_repo, owner, name)
            facts = await asyncio.to_thread(
                lambda: snapshot_issue(repo.get_issue(number))
            )
            if not facts or facts["number"] != number:
                raise ValueError("Invalid GitHub Issue update target")
            collection = await self.collection(owner, name)
            await self._write_facts(
                collection, [facts], enrichment=enrichment, single=True
            )

    async def remove_issue(self, owner: str, name: str, number: int) -> bool:
        from github import UnknownObjectException

        async with self.ownership(owner, name):
            repo = await asyncio.to_thread(self.get_repo, owner, name)
            collection = await self.collection(owner, name)
            try:
                facts = await asyncio.to_thread(
                    lambda: snapshot_issue(repo.get_issue(number))
                )
            except UnknownObjectException as error:
                if error.status != 404:
                    raise
                await _mutate(collection.delete, ids=[f"issue_{number}"])
                return True
            if not facts or facts["number"] != number:
                raise ValueError("Invalid GitHub Issue deletion target")
            # A deferred deletion event must not erase a currently present Issue.
            await self._write_facts(collection, [facts], single=True)
            return False

    async def _write_facts(
        self,
        collection,
        facts_batch: list[dict],
        *,
        enrichment: dict | None = None,
        single: bool = False,
    ) -> tuple[int, int]:
        ids = [f"issue_{facts['number']}" for facts in facts_batch]
        old = await asyncio.to_thread(collection.get, ids=ids, include=["metadatas"])
        existing = dict(zip(old["ids"], old["metadatas"], strict=True))
        texts = [f"{facts['title']}\n{facts['body']}" for facts in facts_batch]
        embeddings = (
            [await self.service.embedding_service.embed_query(texts[0])]
            if single
            else await self.service.embedding_service.embed_texts(texts)
        )
        if len(embeddings) != len(texts) or any(
            not emb
            or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in emb)
            or not any(emb)
            for emb in embeddings
        ):
            raise ValueError("Incomplete Issue corpus embeddings")
        metadatas = []
        for doc_id, facts in zip(ids, facts_batch, strict=True):
            previous = existing.get(doc_id) or {}
            # Enrichment remains valid only while the source text matches.
            meta = (
                dict(previous)
                if previous.get("body") == facts["body"]
                and previous.get("title") == facts["title"]
                else {}
            )
            meta.update(
                number=str(facts["number"]),
                title=facts["title"],
                body=facts["body"],
                state=facts["state"],
                labels=json.dumps(facts["labels"]),
                state_reason=facts["state_reason"] or "",
                source_updated_at=facts["updated_at"],
            )
            if enrichment and await get_dynamic_config(
                "issue_vector_store_rich_metadata", fresh=True
            ):
                meta.update(
                    {key: str(value) for key, value in enrichment.items() if value}
                )
            metadatas.append(meta)
        await _mutate(
            collection.upsert,
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas,
        )
        return sum(doc_id not in existing for doc_id in ids), sum(
            doc_id in existing for doc_id in ids
        )

    async def _reconcile(self, owner: str, name: str, *, force: bool) -> dict:
        collection = await self.collection(owner, name)
        metadata = dict(collection.metadata or {})
        cursor = None
        if metadata.get(_CURSOR):
            try:
                parsed = datetime.fromisoformat(metadata[_CURSOR])
                if parsed.tzinfo is not None:
                    cursor = parsed
            except TypeError, ValueError:
                pass  # A malformed legacy checkpoint requires a full bootstrap.
        started = now_utc()
        freshness = await get_dynamic_config(
            "issue_corpus_freshness_seconds", fresh=True
        )
        batch_size = await get_dynamic_config("issue_corpus_batch_size", fresh=True)
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("Invalid Issue corpus batch size")
        if (
            not isinstance(freshness, (float, int))
            or not math.isfinite(freshness)
            or freshness < 0
        ):
            raise ValueError("Invalid Issue corpus freshness")
        count = await asyncio.to_thread(collection.count)
        if not force and cursor and 0 <= (started - cursor).total_seconds() < freshness:
            return {"status": "cached" if count else "no_issues", "count": count}
        repo = await asyncio.to_thread(self.get_repo, owner, name)
        kwargs = {"state": "all", "sort": "updated", "direction": "asc"}
        if cursor and not force:
            # GitHub timestamps have second precision; overlap the boundary.
            kwargs["since"] = cursor - timedelta(seconds=1)

        def source():
            for raw_issue in repo.get_issues(**kwargs):
                facts = snapshot_issue(raw_issue)
                if facts:
                    yield facts

        iterator = source()
        added = updated = 0
        while facts_batch := await asyncio.to_thread(
            lambda: list(islice(iterator, batch_size))
        ):
            batch_added, batch_updated = await self._write_facts(
                collection, facts_batch
            )
            added += batch_added
            updated += batch_updated
        total = await asyncio.to_thread(collection.count)
        # Fetch and all embedding/writes completed. Commit is the final await so
        # cancellation cannot arrive during a later read after advancing it.
        try:
            await _mutate(
                collection.modify, metadata={**metadata, _CURSOR: started.isoformat()}
            )
        except asyncio.CancelledError:
            # The commit thread was drained under ownership. Restore the prior
            # cursor before a retry may acquire the repository and read it.
            await _mutate(collection.modify, metadata=metadata)
            raise
        return {
            "status": ("reindexed" if count else "indexed") if total else "no_issues",
            "count": total,
            "added": added,
            "updated": updated,
        }
