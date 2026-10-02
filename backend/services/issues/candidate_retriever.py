"""Retrieval yields current candidate facts, never relationship decisions."""

import asyncio
import math

from github import UnknownObjectException

from backend.core.config import get_dynamic_config
from backend.services.issues.corpus_service import IssueCorpusService, snapshot_issue


class IssueCandidateRetriever:
    def __init__(self, issue_embedding_service=None):
        if issue_embedding_service is None:
            from backend.services.issue_embedding_service import IssueEmbeddingService

            issue_embedding_service = IssueEmbeddingService()
        self.service = issue_embedding_service
        self.corpus = IssueCorpusService(self.service)

    async def retrieve(
        self,
        repo_owner: str,
        repo_name: str,
        *,
        text: str,
        state: str,
        exclude_numbers: list[int],
        top_k: int,
        similarity_threshold: float,
    ) -> list[dict]:
        if state not in {"open", "closed", "all"}:
            raise ValueError("Invalid Issue candidate state")
        if top_k <= 0 or not math.isfinite(similarity_threshold):
            raise ValueError("Invalid Issue candidate retrieval limits")
        await self.corpus.reconcile(repo_owner, repo_name)
        collection = await self.corpus.collection(repo_owner, repo_name)
        count = await asyncio.to_thread(collection.count)
        if not count:
            return []
        query = await self.service.embedding_service.embed_query(text)
        if not query or any(not math.isfinite(v) for v in query) or not any(query):
            raise ValueError("Invalid Issue query embedding")
        query_norm = math.hypot(*query)
        if not math.isfinite(query_norm):
            raise ValueError("Invalid Issue query embedding norm")
        multiplier = await get_dynamic_config(
            "issue_candidate_pool_multiplier", fresh=True
        )
        if type(multiplier) is not int or multiplier <= 0:
            raise ValueError("Invalid Issue candidate pool multiplier")
        found = await asyncio.to_thread(
            collection.query,
            query_embeddings=[query],
            n_results=min(count, (top_k + len(exclude_numbers)) * multiplier),
            where=None if state == "all" else {"state": state},
            include=["documents", "metadatas", "embeddings"],
        )
        repo = await asyncio.to_thread(self.corpus.get_repo, repo_owner, repo_name)
        excluded = set(exclude_numbers)
        docs = []
        seen = set()
        for i, doc_id in enumerate(found["ids"][0]):
            number = self.service._safe_parse_number(
                found["metadatas"][0][i].get("number")
            )
            if (
                number is None
                or number <= 0
                or number in excluded
                or number in seen
                or doc_id != f"issue_{number}"
            ):
                continue
            embedding = found["embeddings"][0][i]
            if (
                len(embedding) != len(query)
                or any(not math.isfinite(v) for v in embedding)
                or not any(embedding)
            ):
                raise ValueError("Invalid Issue candidate embedding")
            # Legacy collections may use L2. Measure true cosine, not 1-distance.
            embedding_norm = math.hypot(*embedding)
            if not math.isfinite(embedding_norm):
                raise ValueError("Invalid Issue candidate embedding norm")
            similarity = math.fsum(
                (a / query_norm) * (b / embedding_norm)
                for a, b in zip(query, embedding, strict=True)
            )
            if similarity < similarity_threshold:
                continue
            try:
                facts = await asyncio.to_thread(
                    lambda number=number: snapshot_issue(repo.get_issue(number))
                )
            except UnknownObjectException as error:
                if error.status != 404:
                    raise
                # A missed deletion webhook can leave a recalled row behind.
                # Recheck under shared writer ownership: the unlocked 404 must
                # not delete an Issue that reappeared while another writer ran.
                await self.corpus.remove_issue(repo_owner, repo_name, number)
                continue
            if not facts:
                continue
            if facts["number"] != number:
                raise ValueError("Mismatched GitHub Issue candidate")
            if state != "all" and facts["state"] != state:
                continue
            facts.pop("updated_at")
            docs.append(
                {
                    **facts,
                    "content": f"{facts['title']}\n{facts['body']}",
                    "similarity": max(-1.0, min(1.0, similarity)),
                }
            )
            seen.add(number)
        if not docs:
            return []
        docs.sort(key=lambda doc: doc["similarity"], reverse=True)
        return await self.service.reranker_service.rerank(
            query=text, docs=docs, top_k=top_k, strict=True
        )
