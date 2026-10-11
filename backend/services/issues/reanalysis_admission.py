"""Authorize reanalysis state without guessing legacy NULL rows are open."""

import asyncio
import re

from loguru import logger

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.core.github_app import GitHubAppClient


class IssueReanalysisAdmissionError(Exception):
    """A safe, explicit refusal shared by API and WebUI admission."""

    def __init__(self, *, closed=False, code=None):
        self.code = code
        if code is not None:
            self.status_code, self.translation_key = {
                "insufficient_credits": (402, "billing.insufficient_credits"),
                "concurrency_limit": (429, "billing.rate_limit_exceeded"),
                "rate_limit_exceeded": (429, "billing.rate_limit_exceeded"),
                "legacy_migration_required": (
                    409,
                    "issue.reanalyze_entitlements_pending",
                ),
                "billing_owner_unavailable": (403, "issue.reanalyze_owner_unavailable"),
                "reanalysis_submit_failed": (503, "issue.reanalyze_submit_failed"),
                "reanalysis_handoff_unconfirmed": (
                    503,
                    "issue.reanalyze_handoff_unconfirmed",
                ),
            }.get(code, (503, "issue.reanalyze_admission_unavailable"))
            super().__init__(self.translation_key)
            return
        self.status_code = 409 if closed else 503
        self.translation_key = (
            "issue.reanalyze_closed" if closed else "issue.reanalyze_source_unavailable"
        )
        super().__init__(self.translation_key)


def _repository_identity(owner, name):
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", owner):
        raise ValueError("Invalid repository owner")
    if not isinstance(name, str):
        raise ValueError("Invalid repository name")
    parts = name.split("/")
    if len(parts) == 2:
        if parts[0].casefold() != owner.casefold():
            raise ValueError("Repository owner mismatch")
        name = parts[1]
    elif len(parts) != 1:
        raise ValueError("Invalid repository name")
    if name in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("Invalid repository name")
    return owner, name


def _read_live_state(owner, name, number):
    """PyGithub authentication, hydration and lazy attributes are blocking."""
    client = GitHubAppClient().get_repo_client(owner, name)
    if client is None:
        raise ValueError("GitHub client unavailable")
    repo = client.get_repo(f"{owner}/{name}")
    if repo.full_name.casefold() != f"{owner}/{name}".casefold():
        raise ValueError("GitHub repository identity mismatch")
    issue = repo.get_issue(number)
    raw = issue.raw_data
    if (
        not isinstance(raw, dict)
        or type(raw.get("number")) is not int
        or raw["number"] != number
        or raw.get("state") not in {"open", "closed"}
        or raw.get("pull_request") is not None
    ):
        raise ValueError("Invalid GitHub Issue state")
    return raw["state"]


async def prepare_issue_reanalysis(analysis):
    """Call only after the route's database ownership check has succeeded.

    Admission resolves only legacy state. Historical title/body/author remain
    the main analysis inputs; relation inference hydrates its own current source.
    No database state changes happen before worker admission.
    """
    if analysis.issue_state == "closed":
        raise IssueReanalysisAdmissionError(closed=True)
    try:
        owner, name = _repository_identity(analysis.repo_owner, analysis.repo_name)
        number = analysis.issue_number
        if type(number) is not int or number <= 0:
            raise ValueError("Invalid Issue number")
        state = analysis.issue_state
        if state is None:
            state = await asyncio.to_thread(_read_live_state, owner, name, number)
        elif state != "open":
            raise ValueError("Invalid persisted Issue state")
    except ReviewCancelledError:
        raise
    except Exception:
        # Never render or log provider error text: it may contain credentials.
        logger.warning("Issue reanalysis source state unavailable")
        raise IssueReanalysisAdmissionError() from None
    if state == "closed":
        raise IssueReanalysisAdmissionError(closed=True)
    issue_info = {
        "issue_number": number,
        "repo_name": name,
        "repo_owner": owner,
        "author": analysis.author,
        "title": analysis.title,
        "body": analysis.body,
        "state": state,
    }
    if name != analysis.repo_name:
        issue_info["repo_full_name"] = f"{owner}/{name}"
    return issue_info


async def admit_reanalysis_operation(db, context):
    """Commit financial admission before a caller acknowledges or starts I/O."""
    from backend.services.billing_service import BillingService

    async with db.begin_nested():
        await BillingService(db).register_operation(
            context.user_id,
            context.operation_id,
            context.feature,
            source=dict(context.source),
            platform_reason=context.platform_reason,
        )
    await db.commit()


async def submit_issue_reanalysis(db, issue_info, user):
    """Shared API/WebUI handoff; clients never supply the payer or execution ID."""
    from backend.services.billing_context import context_for_payload
    from backend.services.billing_service import BillingError, BillingService
    from backend.services.database_reset_runtime_service import (
        DatabaseResetRuntimeAdmissionClosed,
    )
    from backend.services.webhook_execution_service import compensate_unstarted_review
    from backend.workers.issue_worker import submit_issue_analysis_task

    user_id = user.get("user_id")
    administrator = user.get("role") in {"admin", "super_admin"}
    if not administrator and (type(user_id) is not int or user_id <= 0):
        raise IssueReanalysisAdmissionError(code="billing_owner_unavailable")
    issue_info.update(
        repo_full_name=f"{issue_info['repo_owner']}/{issue_info['repo_name']}",
        user_id=user_id,
        billing_user_id=None if administrator else user_id,
    )
    if user_id is not None:
        issue_info["trigger_user_id"] = user_id
    if administrator:
        issue_info["billing_platform_reason"] = "administrator_issue_reanalysis"
    context = context_for_payload(issue_info, "issue_analysis")
    try:
        await admit_reanalysis_operation(db, context)
    except BillingError as exc:
        await db.rollback()
        raise IssueReanalysisAdmissionError(code=exc.code) from None
    except Exception as exc:
        await db.rollback()
        logger.warning("Issue reanalysis admission unavailable: {}", type(exc).__name__)
        raise IssueReanalysisAdmissionError(code="billing_unavailable") from None
    try:
        return await submit_issue_analysis_task(issue_info)
    except DatabaseResetRuntimeAdmissionClosed:
        # The runtime supervisor proves this handoff was not registered.
        await compensate_unstarted_review(context.operation_id, None, db=db)
        await db.commit()
        raise IssueReanalysisAdmissionError(code="reanalysis_submit_failed") from None
    except Exception as exc:
        logger.warning("Issue reanalysis handoff unconfirmed: {}", type(exc).__name__)
        await db.rollback()
        operation = await BillingService(db)._operation(context.operation_id)
        if operation.outcome is None:
            operation.status = "pending_reconciliation"
            # A lost submission acknowledgement is business uncertainty, not
            # unknown Provider Usage. Do not poison the financial request gate
            # if the legitimately queued Worker later starts this operation.
            operation.source = {**operation.source, "submission_acknowledged": False}
            await db.commit()
        raise IssueReanalysisAdmissionError(
            code="reanalysis_handoff_unconfirmed"
        ) from None
