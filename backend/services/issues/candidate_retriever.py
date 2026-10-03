"""Retrieval yields current candidate facts, never relationship decisions."""

import asyncio
import math

from github import UnknownObjectException
from github.GithubObject import GithubObject

from backend.core.config import get_dynamic_config
from backend.services.issues.corpus_service import (
    IssueCorpusService,
    _controlled,
    snapshot_issue,
)
from backend.services.issues.relation_runtime import check_relation_boundary


async def _read_candidate_comments(
    repo, candidate, max_comments, max_chars, cancel_event, deadline
):
    """Read newest REST pages lazily, never traverse an entire discussion."""
    check_relation_boundary(cancel_event, deadline)
    issue = await _controlled(
        lambda: asyncio.to_thread(repo.get_issue, candidate["number"]),
        cancel_event,
        deadline,
    )
    facts = await _controlled(
        lambda: asyncio.to_thread(snapshot_issue, issue),
        cancel_event,
        deadline,
    )
    if facts != {key: candidate[key] for key in facts}:
        raise ValueError("GitHub Issue candidate changed during retrieval")
    raw = await _controlled(
        lambda: asyncio.to_thread(lambda: issue.raw_data), cancel_event, deadline
    )
    total = raw.get("comments")
    if type(total) is not int or total < 0:
        raise ValueError("Incomplete GitHub Issue comment count")
    # PyGithub reversed pagination jumps to Link:last and follows Link:prev;
    # accessing it may fetch a first page to discover that last-page URL.
    iterator = await _controlled(
        lambda: asyncio.to_thread(lambda: iter(reversed(issue.get_comments()))),
        cancel_event,
        deadline,
    )
    comments = []
    for _ in range(max_comments):
        # Drain the one in-flight page read on real Task cancellation, then
        # propagate it before fetching another record or another page.
        comment = await _controlled(
            lambda: asyncio.to_thread(next, iterator, None),
            cancel_event,
            deadline,
        )
        if comment is None:
            break
        # Read the page's loaded source data. CompletableGithubObject.raw_data
        # unconditionally completes lazy objects, adding an individual GET for
        # every already hydrated comment and potentially reading a new version.
        raw_comment = (
            GithubObject.raw_data.fget(comment)
            if isinstance(comment, GithubObject)
            else comment.raw_data
        )
        if not isinstance(raw_comment, dict):
            raise ValueError("Incomplete GitHub Issue comment")
        body = raw_comment.get("body")
        user = raw_comment.get("user")
        if (
            type(raw_comment.get("id")) is not int
            or raw_comment["id"] <= 0
            or not isinstance(body, str)
            or not isinstance(user, dict)
            or not isinstance(user.get("login"), str)
            or not all(
                isinstance(raw_comment.get(key), str) and raw_comment[key]
                for key in ("html_url", "created_at", "updated_at")
            )
        ):
            raise ValueError("Incomplete GitHub Issue comment provenance")
        comments.append(
            {
                "id": raw_comment["id"],
                "body": body[:max_chars],
                "html_url": raw_comment["html_url"],
                "user": {key: user[key] for key in ("login", "type") if key in user},
                "created_at": raw_comment["created_at"],
                "updated_at": raw_comment["updated_at"],
                "body_truncated": len(body) > max_chars,
            }
        )
    check_relation_boundary(cancel_event, deadline)
    if total < len(comments):
        raise ValueError("Inconsistent GitHub Issue comment count")
    return comments, {
        "source": "github_issue_comments",
        "order": "newest_first",
        "total_count": total,
        "included_count": len(comments),
        "max_comments": max_comments,
        "max_chars": max_chars,
        "bounded": True,
        "truncated": total > len(comments)
        or any(comment["body_truncated"] for comment in comments),
    }


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
        cancel_event=None,
        deadline=None,
        include_comments: bool = False,
    ) -> list[dict]:
        check_relation_boundary(cancel_event, deadline)
        if state not in {"open", "closed", "all"}:
            raise ValueError("Invalid Issue candidate state")
        if top_k <= 0 or not math.isfinite(similarity_threshold):
            raise ValueError("Invalid Issue candidate retrieval limits")
        await self.corpus.reconcile(
            repo_owner, repo_name, cancel_event=cancel_event, deadline=deadline
        )
        collection = await self.corpus.collection(
            repo_owner, repo_name, cancel_event=cancel_event, deadline=deadline
        )
        count = await _controlled(
            lambda: asyncio.to_thread(collection.count), cancel_event, deadline
        )
        if not count:
            return []
        query = await _controlled(
            lambda: self.service.embedding_service.embed_query(text),
            cancel_event,
            deadline,
        )
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
        found = await _controlled(
            lambda: asyncio.to_thread(
                collection.query,
                query_embeddings=[query],
                n_results=min(count, (top_k + len(exclude_numbers)) * multiplier),
                where=None if state == "all" else {"state": state},
                include=["documents", "metadatas", "embeddings"],
            ),
            cancel_event,
            deadline,
        )
        repo = await _controlled(
            lambda: asyncio.to_thread(self.corpus.get_repo, repo_owner, repo_name),
            cancel_event,
            deadline,
        )
        excluded = set(exclude_numbers)
        docs = []
        seen = set()
        for i, doc_id in enumerate(found["ids"][0]):
            check_relation_boundary(cancel_event, deadline)
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
                facts = await _controlled(
                    lambda number=number: asyncio.to_thread(
                        lambda: snapshot_issue(repo.get_issue(number))
                    ),
                    cancel_event,
                    deadline,
                )
            except UnknownObjectException as error:
                if error.status != 404:
                    raise
                # A missed deletion webhook can leave a recalled row behind.
                # Recheck under shared writer ownership: the unlocked 404 must
                # not delete an Issue that reappeared while another writer ran.
                await self.corpus.remove_issue(
                    repo_owner,
                    repo_name,
                    number,
                    cancel_event=cancel_event,
                    deadline=deadline,
                )
                continue
            if not facts:
                continue
            if facts["number"] != number:
                raise ValueError("Mismatched GitHub Issue candidate")
            if state != "all" and facts["state"] != state:
                continue
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
        results = await _controlled(
            lambda: self.service.reranker_service.rerank(
                query=text, docs=docs, top_k=top_k, strict=True
            ),
            cancel_event,
            deadline,
        )
        if include_comments and results:
            max_comments = await get_dynamic_config(
                "issue_relation_candidate_max_comments", fresh=True
            )
            max_chars = await get_dynamic_config(
                "issue_relation_candidate_comment_max_chars", fresh=True
            )
            if any(
                type(value) is not int or value <= 0
                for value in (max_comments, max_chars)
            ):
                raise ValueError("Invalid Issue candidate discussion limits")
            for candidate in results:
                comments, context = await _read_candidate_comments(
                    repo,
                    candidate,
                    max_comments,
                    max_chars,
                    cancel_event,
                    deadline,
                )
                candidate["comments"] = comments
                candidate["comments_context"] = context
        check_relation_boundary(cancel_event, deadline)
        return results
