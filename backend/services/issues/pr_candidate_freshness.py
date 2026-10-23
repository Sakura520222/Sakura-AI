"""Immutable source versions for accepted PR relation candidates."""

from dataclasses import dataclass
from datetime import UTC, datetime

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.services.issues.corpus_service import snapshot_issue
from backend.services.issues.pr_budget import PRBudgetError, read_source
from backend.services.issues.relation_runtime import (
    RelationDeadlineExceeded,
    check_relation_boundary,
)


@dataclass(frozen=True)
class CandidateVersion:
    number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    state_reason: str | None
    updated_at: datetime


def _version(facts: dict) -> CandidateVersion:
    required = {
        "number",
        "title",
        "body",
        "state",
        "labels",
        "state_reason",
        "updated_at",
    }
    if not isinstance(facts, dict) or not required <= facts.keys():
        raise ValueError("Incomplete candidate source")
    if (
        type(facts["number"]) is not int
        or facts["number"] <= 0
        or not isinstance(facts["title"], str)
        or not facts["title"].strip()
        or not isinstance(facts["body"], str)
        or facts["state"] not in {"open", "closed"}
        or not isinstance(facts["labels"], list)
        or any(not isinstance(label, str) for label in facts["labels"])
        or facts["state_reason"] is not None
        and not isinstance(facts["state_reason"], str)
        or not isinstance(facts["updated_at"], str)
    ):
        raise ValueError("Malformed candidate source")
    updated = datetime.fromisoformat(facts["updated_at"])
    if updated.tzinfo is None or updated.utcoffset() is None:
        raise ValueError("Missing candidate source version timezone")
    return CandidateVersion(
        number=facts["number"],
        title=facts["title"],
        body=facts["body"],
        state=facts["state"],
        labels=tuple(sorted(facts["labels"])),
        state_reason=facts["state_reason"],
        updated_at=updated.astimezone(UTC),
    )


def capture_candidate_versions(
    candidates: list[dict],
) -> dict[int, CandidateVersion | None]:
    """Capture before verifier invocation; malformed unaccepted facts allow cleanup."""
    versions = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or type(candidate.get("number")) is not int:
            continue
        number = candidate["number"]
        if number in versions:
            versions[number] = None
            continue
        try:
            versions[number] = _version(candidate)
        except ValueError, TypeError:
            versions[number] = None
    return versions


async def revalidate_candidates(
    repo, versions, relations, *, cancel_event=None, deadline=None
):
    """Read only accepted candidates through the supplied repository source.

    Repeating this after SQL flush narrows the publication window. GitHub offers
    no atomic compare-and-swap spanning these source reads and the PR body edit.
    """
    for relation in relations:
        check_relation_boundary(cancel_event, deadline)
        number = relation.get("number")
        expected = versions.get(number) if type(number) is int else None
        if expected is None:
            raise PRBudgetError("candidate_facts_unavailable")
        try:
            current = await read_source(
                lambda number=number: _version(snapshot_issue(repo.get_issue(number)))
            )
        except Exception as exc:
            # Control exceptions retain their domain semantics, even if a source
            # double raises them within the blocking read.
            if isinstance(exc, (ReviewCancelledError, RelationDeadlineExceeded)):
                raise
            check_relation_boundary(cancel_event, deadline)
            raise PRBudgetError("candidate_facts_unavailable") from exc
        check_relation_boundary(cancel_event, deadline)
        if current != expected:
            raise PRBudgetError("stale_candidate")
    check_relation_boundary(cancel_event, deadline)
