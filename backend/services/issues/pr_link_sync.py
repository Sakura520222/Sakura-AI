"""Replace only the machine-owned semantic relation set in one transaction."""

import asyncio
import json
import weakref
from contextlib import asynccontextmanager

from sqlalchemy import select

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.models.database import PRIssueLink

_LOCKS = weakref.WeakKeyDictionary()


@asynccontextmanager
async def pr_relation_ownership(repo_name: str, pr_number: int):
    locks = _LOCKS.setdefault(asyncio.get_running_loop(), {})
    lock = locks.setdefault((repo_name.casefold(), pr_number), asyncio.Lock())
    async with lock:
        yield


async def replace_semantic_links(
    db, repo_name: str, pr_number: int, relations: list[dict]
):
    result = await db.execute(
        select(PRIssueLink).where(
            PRIssueLink.repo_name == repo_name,
            PRIssueLink.pr_id == pr_number,
            PRIssueLink.link_type == "semantic",
        )
    )
    existing = {row.issue_number: row for row in result.scalars().all()}
    wanted = {relation["number"]: relation for relation in relations}
    for number, row in existing.items():
        if number not in wanted:
            # AsyncSession.delete is async; sync test facade can expose it too.
            await db.delete(row)
    for number, relation in wanted.items():
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
        row.inference_reason = json.dumps(relation, ensure_ascii=False, allow_nan=False)
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

                # Fetch the source used by this inference, independently of the
                # possibly outdated webhook or AI analyzer subset of files.
                def snapshot():
                    pr = repo.get_pull(number)
                    raw_files = list(pr.get_files())
                    files = []
                    count_complete = len(raw_files) == pr.changed_files
                    for f in raw_files:
                        patch = f.patch or ""
                        additions = sum(
                            line.startswith("+") and not line.startswith("+++")
                            for line in patch.splitlines()
                        )
                        deletions = sum(
                            line.startswith("-") and not line.startswith("---")
                            for line in patch.splitlines()
                        )
                        files.append(
                            {
                                "path": f.filename,
                                "status": f.status,
                                "patch": patch,
                                "complete": count_complete
                                and bool(patch)
                                and additions == f.additions
                                and deletions == f.deletions,
                            }
                        )
                    return pr.head.sha, pr.base.sha, pr.title, pr.body or "", files

                sha, base_sha, title, body, files = await asyncio.to_thread(snapshot)
                human = strip_sakura_generated_sections(body)
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
                )
                check_cancelled()
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
                latest = await asyncio.to_thread(repo.get_pull, number)
                if (
                    latest.head.sha != sha
                    or latest.base.sha != base_sha
                    or latest.title != title
                    or strip_sakura_generated_sections(latest.body) != human
                ):
                    return PRVerificationResult(False, failure="stale_source")
                check_cancelled()

                async def publish():
                    async with self.session_factory() as db:
                        try:
                            await replace_semantic_links(
                                db, f"{owner}/{name}", number, result.relations
                            )
                            fresh = await asyncio.to_thread(repo.get_pull, number)
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

                failure = type(exc).__name__
                logger.warning("PR relation synchronization failed: {}", failure)
                return PRVerificationResult(False, failure=failure)
