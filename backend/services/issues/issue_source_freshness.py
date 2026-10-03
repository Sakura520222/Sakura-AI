"""Bounded authoritative Issue input snapshots and cooperative freshness reads."""

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.services.issues.candidate_retriever import _read_candidate_comments
from backend.services.issues.corpus_service import _controlled, snapshot_issue
from backend.services.issues.issue_budget import IssueBudgetError
from backend.services.issues.pr_candidate_freshness import (
    CandidateVersion,
    capture_candidate_versions,
)
from backend.services.issues.relation_runtime import (
    RelationDeadlineExceeded,
    check_relation_boundary,
)
from backend.services.pr_body import strip_sakura_generated_sections


class IssueSourceReader:
    """Use the same newest-comment bounds for current and accepted Issues.

    No full-history traversal is used here. Issue update versions detect content
    round trips; discussion identities, versions and total count cover the exact
    bounded sample given to inference. GitHub supplies no cross-read atomic CAS.
    """

    def __init__(self, repo=None):
        self.repo = repo

    def _repo(self, owner, name):
        from backend.core.github_app import GitHubAppClient

        client = GitHubAppClient().get_repo_client(owner, name)
        if client is None:
            raise ValueError("Issue source unavailable")
        return client.get_repo(f"{owner}/{name}")

    async def read(
        self,
        owner,
        name,
        number,
        *,
        include_comments,
        max_comments,
        max_chars,
        cancel_event=None,
        deadline=None,
    ):
        if self.repo is None:
            self.repo = await _controlled(
                lambda: asyncio.to_thread(self._repo, owner, name),
                cancel_event,
                deadline,
            )
        facts = await _controlled(
            lambda: asyncio.to_thread(
                lambda: snapshot_issue(self.repo.get_issue(number))
            ),
            cancel_event,
            deadline,
        )
        if not facts or facts["number"] != number:
            raise ValueError("Invalid Issue source identity")
        if include_comments:
            (
                facts["comments"],
                facts["comments_context"],
            ) = await _read_candidate_comments(
                self.repo, facts, max_comments, max_chars, cancel_event, deadline
            )
        return facts


def model_source(facts, include_comments):
    """Own the exact facts sent to the model, independently of mutable providers."""
    source = deepcopy(
        {
            key: facts[key]
            for key in (
                "number",
                "title",
                "body",
                "state",
                "labels",
                "state_reason",
                "updated_at",
            )
        }
    )
    source["body"] = strip_sakura_generated_sections(source["body"])
    source["comments"] = deepcopy(facts.get("comments", [])) if include_comments else []
    if include_comments:
        source["comments_context"] = deepcopy(facts["comments_context"])
    return source


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing discussion version")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Missing discussion version timezone")


@dataclass(frozen=True)
class IssueSourceSnapshot:
    version: CandidateVersion
    discussion: str | None

    @classmethod
    def capture(cls, facts, *, include_comments, max_comments, max_chars):
        version = capture_candidate_versions([facts]).get(facts.get("number"))
        if version is None:
            raise ValueError("Missing Issue source version")
        discussion = None
        if include_comments:
            comments = facts.get("comments")
            context = facts.get("comments_context")
            if not isinstance(comments, list) or not isinstance(context, dict):
                raise ValueError("Missing discussion provenance")
            total = context.get("total_count")
            if (
                context.get("source") != "github_issue_comments"
                or context.get("order") != "newest_first"
                or context.get("bounded") is not True
                or type(total) is not int
                or total < 0
                or context.get("included_count") != len(comments)
                or len(comments) != min(total, max_comments)
                or context.get("max_comments") != max_comments
                or context.get("max_chars") != max_chars
            ):
                raise ValueError("Inconsistent discussion bounds")
            ids = set()
            for comment in comments:
                if (
                    not isinstance(comment, dict)
                    or type(comment.get("id")) is not int
                    or comment["id"] <= 0
                    or comment["id"] in ids
                    or not isinstance(comment.get("body"), str)
                    or len(comment["body"]) > max_chars
                    or type(comment.get("body_truncated")) is not bool
                    or not isinstance(comment.get("html_url"), str)
                    or not isinstance(comment.get("user"), dict)
                    or not isinstance(comment["user"].get("login"), str)
                ):
                    raise ValueError("Malformed discussion source")
                ids.add(comment["id"])
                _timestamp(comment.get("created_at"))
                _timestamp(comment.get("updated_at"))
            truncated = total > len(comments) or any(
                c["body_truncated"] for c in comments
            )
            if context.get("truncated") is not truncated:
                raise ValueError("Inconsistent discussion truncation")
            discussion = json.dumps(
                [comments, context], sort_keys=True, allow_nan=False
            )
        return cls(version, discussion)


async def read_snapshot(reader, owner, name, number, **controls):
    check_relation_boundary(controls.get("cancel_event"), controls.get("deadline"))
    try:
        facts = await reader.read(owner, name, number, **controls)
        snapshot = IssueSourceSnapshot.capture(
            facts,
            **{
                key: controls[key]
                for key in ("include_comments", "max_comments", "max_chars")
            },
        )
        if snapshot.version.number != number:
            raise ValueError("Wrong Issue source identity")
    except ReviewCancelledError, RelationDeadlineExceeded:
        raise
    except Exception as exc:
        check_relation_boundary(controls.get("cancel_event"), controls.get("deadline"))
        raise IssueBudgetError("source_unavailable") from exc
    check_relation_boundary(controls.get("cancel_event"), controls.get("deadline"))
    return facts, snapshot


async def revalidate_sources(reader, owner, name, snapshots, numbers, **controls):
    for number in dict.fromkeys(numbers):
        _, actual = await read_snapshot(reader, owner, name, number, **controls)
        if actual != snapshots[number]:
            raise IssueBudgetError("stale_source")
