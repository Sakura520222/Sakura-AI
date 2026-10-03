"""Replace only the machine-owned semantic relation set in one transaction."""

import asyncio
import json
import weakref
from contextlib import asynccontextmanager

from sqlalchemy import select

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.models.database import PRIssueLink
from backend.services.issues.pr_budget import PRBudgetError, check_boundary, read_source
from backend.services.issues.pr_candidate_freshness import (
    capture_candidate_versions,
    revalidate_candidates,
)
from backend.services.issues.relation_runtime import RelationDeadlineExceeded
from backend.services.issues.unified_diff import parse_unified_diff

_LOCKS = weakref.WeakKeyDictionary()
# PRIssueLink.inference_reason is MySQL TEXT: capacity is bytes, not characters.
_INFERENCE_REASON_MAX_BYTES = 65535


class SemanticLinkEvidenceTooLargeError(ValueError):
    """The complete decision/proof cannot fit its persisted TEXT column."""


def _serialize_decision(relation: dict) -> str:
    decision = {
        key: relation[key]
        for key in ("number", "relation", "confidence", "reason", "evidence")
    }
    serialized = json.dumps(decision, ensure_ascii=False, allow_nan=False)
    if len(serialized.encode("utf-8")) > _INFERENCE_REASON_MAX_BYTES:
        # Never truncate proof or drop evidence to fit storage.
        raise SemanticLinkEvidenceTooLargeError()
    return serialized


@asynccontextmanager
async def pr_relation_ownership(repo_name: str, pr_number: int):
    locks = _LOCKS.setdefault(asyncio.get_running_loop(), {})
    lock = locks.setdefault((repo_name.casefold(), pr_number), asyncio.Lock())
    async with lock:
        yield


async def replace_semantic_links(
    db, repo_name: str, pr_number: int, relations: list[dict]
):
    # Validate the entire projected set before deleting or modifying any rows.
    wanted = {
        relation["number"]: (relation, _serialize_decision(relation))
        for relation in relations
    }
    result = await db.execute(
        select(PRIssueLink).where(
            PRIssueLink.repo_name == repo_name,
            PRIssueLink.pr_id == pr_number,
            PRIssueLink.link_type == "semantic",
        )
    )
    existing = {row.issue_number: row for row in result.scalars().all()}
    for number, row in existing.items():
        if number not in wanted:
            # AsyncSession.delete is async; sync test facade can expose it too.
            await db.delete(row)
    for number, (relation, serialized) in wanted.items():
        row = existing.get(number)
        if row is None:
            row = PRIssueLink(
                repo_name=repo_name,
                pr_id=pr_number,
                issue_number=number,
                link_type="semantic",
            )
            db.add(row)
        prefix = "Closes" if relation["relation"] == "closes" else "Related to"
        row.reference_text = f"{prefix} #{number}"
        row.inference_reason = serialized
    await db.flush()


class PRRelationSyncService:
    """Serialize inference/publication for a PR in the supported single process.

    Fresh source checks reject stale heads/descriptions. SQL uniqueness remains
    the backstop for independent writers; GitHub edits and DB commits are not a
    distributed transaction and a retry reconciles any interrupted publication.
    """

    def __init__(
        self, *, retriever=None, verifier=None, session_factory=None, linker=None
    ):
        from backend.models import database

        self.retriever = retriever
        self.verifier = verifier
        self.session_factory = session_factory or database.async_session
        self.linker = linker

    async def synchronize(
        self,
        repo,
        owner,
        name,
        number,
        *,
        cancel_event=None,
        deadline=None,
        context=None,
        observer=None,
    ):
        from backend.core.config import get_dynamic_config
        from backend.services.issues.pr_verifier import PRVerificationResult
        from backend.services.pr_body import strip_sakura_generated_sections

        def check_cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise ReviewCancelledError()

        async with pr_relation_ownership(f"{owner}/{name}", number):
            check_cancelled()
            if deadline is not None and deadline.is_expired():
                return PRVerificationResult(False, failure="deadline")
            try:
                from backend.services.issues.candidate_retriever import (
                    IssueCandidateRetriever,
                )
                from backend.services.issues.pr_verifier import PRRelationVerifier
                from backend.services.pr_issue_linker import PRIssueLinker

                self.retriever = self.retriever or IssueCandidateRetriever()
                self.verifier = self.verifier or PRRelationVerifier()
                self.linker = self.linker or PRIssueLinker()

                budget_verifier = (
                    self.verifier
                    if callable(getattr(self.verifier, "resolve_budget", None))
                    else PRRelationVerifier()
                )
                budget = await budget_verifier.resolve_budget(
                    cancel_event=cancel_event, deadline=deadline
                )
                # Only one lazy iterator read is allowed between boundary checks.
                # Do not materialize PaginatedList: that requests every GitHub page.
                pr = await read_source(repo.get_pull, number)
                check_boundary(cancel_event, deadline)
                sha, base_sha, title, body = (
                    pr.head.sha,
                    pr.base.sha,
                    pr.title,
                    pr.body or "",
                )
                human = strip_sakura_generated_sections(body)
                budget.messages(pr_title=title, pr_body=human, files=[], candidates=[])
                expected = pr.changed_files
                if type(expected) is not int or expected < 0:
                    raise PRBudgetError("snapshot_incomplete")
                iterator = await read_source(lambda: iter(pr.get_files()))
                files, end = [], object()
                while len(files) < expected:
                    check_boundary(cancel_event, deadline)
                    if len(files) >= budget.max_files:
                        raise PRBudgetError("snapshot_incomplete")
                    # If even the smallest file envelope cannot fit, do not
                    # trigger another network page merely to discover overflow.
                    budget.messages(
                        pr_title=title,
                        pr_body=human,
                        files=[
                            *files,
                            {"path": "", "status": "", "patch": "", "complete": False},
                        ],
                        candidates=[],
                    )
                    f = await read_source(next, iterator, end)
                    check_boundary(cancel_event, deadline)
                    if f is end:
                        raise PRBudgetError("snapshot_incomplete")
                    patch = f.patch or ""
                    source = {
                        "path": f.filename,
                        "status": f.status,
                        "patch": patch,
                        "complete": False,
                    }
                    # Bound before splitting/retaining a potentially enormous patch.
                    budget.messages(
                        pr_title=title,
                        pr_body=human,
                        files=[*files, source],
                        candidates=[],
                    )
                    try:
                        parsed = parse_unified_diff(patch)
                    except ValueError as exc:
                        raise PRBudgetError("snapshot_incomplete") from exc
                    source["complete"] = (
                        bool(patch)
                        and parsed.additions == f.additions
                        and parsed.deletions == f.deletions
                    )
                    if not source["complete"]:
                        # Partial evidence must never replace a previously verified set.
                        raise PRBudgetError("snapshot_incomplete")
                    files.append(source)
                check_boundary(cancel_event, deadline)
                explicit = await self.linker.parse_issue_references(human)
                check_cancelled()
                if deadline is not None and deadline.is_expired():
                    return PRVerificationResult(False, failure="deadline")
                top_k = await get_dynamic_config("semantic_issue_max_links", fresh=True)
                threshold = await get_dynamic_config(
                    "semantic_issue_similarity_threshold", fresh=True
                )
                candidates = await self.retriever.retrieve(
                    owner,
                    name,
                    text=f"{title}\n{human}",
                    state="open",
                    exclude_numbers=[number, *explicit],
                    top_k=top_k,
                    similarity_threshold=threshold,
                    cancel_event=cancel_event,
                    deadline=deadline,
                )
                check_boundary(cancel_event, deadline)
                candidate_versions = capture_candidate_versions(candidates)
                result = await self.verifier.verify(
                    pr_title=title,
                    pr_body=human,
                    candidates=candidates,
                    files=files,
                    cancel_event=cancel_event,
                    deadline=deadline,
                    context=context,
                    observer=observer,
                )
                if not result.succeeded:
                    return result
                check_cancelled()
                if deadline is not None and deadline.is_expired():
                    return PRVerificationResult(False, failure="deadline")
                latest = await read_source(repo.get_pull, number)
                check_boundary(cancel_event, deadline)
                if (
                    latest.head.sha != sha
                    or latest.base.sha != base_sha
                    or latest.title != title
                    or strip_sakura_generated_sections(latest.body) != human
                ):
                    return PRVerificationResult(False, failure="stale_source")
                await revalidate_candidates(
                    repo,
                    candidate_versions,
                    result.relations,
                    cancel_event=cancel_event,
                    deadline=deadline,
                )

                async def publish():
                    check_boundary(cancel_event, deadline)
                    async with self.session_factory() as db:
                        try:
                            await replace_semantic_links(
                                db, f"{owner}/{name}", number, result.relations
                            )
                            check_boundary(cancel_event, deadline)
                            await revalidate_candidates(
                                repo,
                                candidate_versions,
                                result.relations,
                                cancel_event=cancel_event,
                                deadline=deadline,
                            )
                            fresh = await read_source(repo.get_pull, number)
                            check_boundary(cancel_event, deadline)
                            if (
                                fresh.head.sha != sha
                                or fresh.base.sha != base_sha
                                or fresh.title != title
                                or strip_sakura_generated_sections(fresh.body) != human
                            ):
                                await db.rollback()
                                return False
                            new_body = self.linker.build_updated_pr_body(
                                fresh.body or "", result.relations
                            )
                            if new_body != (fresh.body or ""):
                                await asyncio.to_thread(fresh.edit, body=new_body)
                            await db.commit()
                            return True
                        except BaseException:
                            await db.rollback()
                            raise

                # A to_thread edit cannot be cancelled. Drain the whole commit
                # before releasing ownership, then propagate cancellation.
                task = asyncio.create_task(publish())
                cancellation = None
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError as exc:
                        cancellation = exc
                    except BaseException:
                        if cancellation is None:
                            raise
                        # The shielded task has failed after cancellation was
                        # recorded. Observe/chains its exception below instead
                        # of converting pending cancellation into failure data.
                        break
                if cancellation is not None:
                    try:
                        task.result()
                    except BaseException as exc:
                        raise cancellation from exc
                    raise cancellation
                published = task.result()
                check_cancelled()
                if not published:
                    return PRVerificationResult(False, failure="stale_source")
                return result
            except asyncio.CancelledError, ReviewCancelledError:
                raise
            except Exception as exc:
                from loguru import logger

                failure = (
                    exc.failure
                    if isinstance(exc, PRBudgetError)
                    else "deadline"
                    if isinstance(exc, RelationDeadlineExceeded)
                    else type(exc).__name__
                )
                logger.warning("PR relation synchronization failed: {}", failure)
                return PRVerificationResult(False, failure=failure)
