"""Authorize reanalysis state without guessing legacy NULL rows are open."""

import asyncio
import re

from loguru import logger

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.core.github_app import GitHubAppClient


class IssueReanalysisAdmissionError(Exception):
    """A safe, explicit refusal shared by API and WebUI admission."""

    def __init__(self, *, closed=False):
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
