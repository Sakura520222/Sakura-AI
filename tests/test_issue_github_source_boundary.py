"""Real GitHub client auth boundaries preserve Issue cancellation and secrecy."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from github import GithubException
from loguru import logger

from backend.core import github_app
from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.services.issues import reanalysis_admission
from backend.services.issues.issue_budget import IssueBudgetError
from backend.services.issues.issue_source_freshness import (
    IssueSourceReader,
    read_snapshot,
)
from tests import test_issue_reanalysis_admission as admission_tests

analysis_db = admission_tests.analysis_db
SECRET = "private-provider-credential-fixture"
TOKEN = "ghs_private-token-prefix-fixture"


@pytest.fixture
def boundary(monkeypatch):
    # No singleton constructor, state resets or ambient bound-method attributes.
    app = object.__new__(github_app.GitHubAppClient)
    outcome = SimpleNamespace(
        error=None, stage="installation", attempts=0, tokens=[], sleeps=[]
    )
    raw = {
        "number": 7,
        "title": "Issue",
        "body": "Body",
        "state": "open",
        "labels": [],
        "state_reason": None,
        "updated_at": "2026-10-03T00:00:00Z",
    }
    repo = SimpleNamespace(
        full_name="owner/repo", get_issue=lambda number: SimpleNamespace(raw_data=raw)
    )
    api = SimpleNamespace(get_repo=lambda full_name: repo)

    def fail(stage):
        if outcome.error is not None and outcome.stage == stage:
            raise outcome.error

    def installation(**kwargs):
        assert kwargs == {"owner": "owner", "repo": "repo"}
        outcome.attempts += 1
        fail("installation")
        return SimpleNamespace(id=123)

    def access_token(installation_id):
        assert installation_id == 123
        fail("token")
        return SimpleNamespace(token=TOKEN)

    integration = SimpleNamespace(
        get_installation=installation, get_access_token=access_token
    )
    app.integration = integration

    def integration_factory(**kwargs):
        fail("init")
        return integration

    def api_factory(*, login_or_token):
        outcome.tokens.append(login_or_token)
        fail("client")
        return api

    monkeypatch.setattr(github_app, "GithubIntegration", integration_factory)
    monkeypatch.setattr(github_app, "Github", api_factory)
    monkeypatch.setattr(
        github_app,
        "settings",
        SimpleNamespace(
            github_app_id="123",
            github_private_key=f"-----BEGIN PRIVATE KEY-----\n{SECRET}\n-----END PRIVATE KEY-----",
        ),
    )
    monkeypatch.setattr(time, "sleep", outcome.sleeps.append)
    monkeypatch.setattr(reanalysis_admission, "GitHubAppClient", lambda: app)
    # B dynamically imports the core factory; A uses its own bound import.
    monkeypatch.setattr(github_app, "GitHubAppClient", lambda: app)
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), level="DEBUG")
    outcome.app = app
    outcome.api = api
    outcome.messages = messages
    try:
        yield outcome
    finally:
        logger.remove(sink)


def assert_safe_logs(boundary):
    output = "\n".join(boundary.messages)
    assert SECRET not in output
    assert TOKEN not in output
    assert TOKEN[:10] not in output
    assert "Traceback" not in output


@pytest.mark.parametrize("stage", ["installation", "token", "client", "init"])
@pytest.mark.parametrize("kind", [ReviewCancelledError, asyncio.CancelledError])
def test_real_auth_cancellation_propagates_without_retry_or_secret_logs(
    boundary, stage, kind
):
    boundary.stage = stage
    boundary.error = kind(SECRET)
    if stage == "init":
        boundary.app.integration = None

    with pytest.raises(kind):
        boundary.app.get_repo_client("owner", "repo")

    assert boundary.attempts == (0 if stage == "init" else 1)
    assert boundary.sleeps == []
    assert_safe_logs(boundary)


@pytest.mark.parametrize("stage", ["installation", "token", "client"])
def test_real_ordinary_auth_failure_retains_two_attempts_with_safe_diagnostics(
    boundary, stage
):
    boundary.stage = stage
    boundary.error = GithubException(403, {"message": SECRET})

    assert boundary.app.get_repo_client("owner", "repo") is None

    assert boundary.attempts == 2
    assert boundary.sleeps == [2]
    assert_safe_logs(boundary)
    messages = "\n".join(boundary.messages)
    assert "GithubException" in messages
    assert "403" in messages


def test_real_success_uses_token_without_logging_token_prefix(boundary):
    assert boundary.app.get_repo_client("owner", "repo") is boundary.api
    assert boundary.tokens == [TOKEN]
    assert boundary.attempts == 1
    assert boundary.sleeps == []
    assert_safe_logs(boundary)


@pytest.mark.parametrize("configuration", ["constructor", "missing_end"])
def test_real_init_failure_logs_no_provider_error_or_key_fragment(
    boundary, monkeypatch, configuration
):
    boundary.app.integration = None
    if configuration == "constructor":
        boundary.stage = "init"
        boundary.error = RuntimeError(SECRET)
    else:
        github_app.settings.github_private_key = (
            f"-----BEGIN PRIVATE KEY-----\n{SECRET}"
        )

    assert boundary.app.get_repo_client("owner", "repo") is None
    assert boundary.sleeps == [2]
    assert_safe_logs(boundary)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
async def test_admission_real_auth_cancel_propagates_without_enqueue(
    analysis_db, boundary, surface, monkeypatch
):
    from unittest.mock import AsyncMock

    analysis_db.record.issue_state = None
    analysis_db.session.commit()
    boundary.error = ReviewCancelledError(SECRET)
    submit = AsyncMock()
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )

    with pytest.raises(ReviewCancelledError):
        await admission_tests._reanalyze(surface, analysis_db.db, {"sub": "owner"})

    assert boundary.attempts == 1
    assert boundary.sleeps == []
    assert len(analysis_db.statements) == 1
    submit.assert_not_awaited()
    assert_safe_logs(boundary)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
async def test_admission_real_auth_ordinary_failure_returns_safe503(
    analysis_db, boundary, surface, monkeypatch
):
    from unittest.mock import AsyncMock

    analysis_db.record.issue_state = None
    analysis_db.session.commit()
    boundary.error = GithubException(403, {"message": SECRET})
    submit = AsyncMock()
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )

    response = await admission_tests._reanalyze(
        surface, analysis_db.db, {"sub": "owner"}
    )

    assert response.status_code == 503
    assert SECRET not in response.body.decode()
    assert boundary.attempts == 2
    assert len(analysis_db.statements) == 1
    submit.assert_not_awaited()
    assert_safe_logs(boundary)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ReviewCancelledError, asyncio.CancelledError])
async def test_issue_source_reader_real_auth_preserves_cancellation(boundary, kind):
    boundary.error = kind(SECRET)

    with pytest.raises(kind):
        await read_snapshot(
            IssueSourceReader(),
            "owner",
            "repo",
            7,
            include_comments=False,
            max_comments=0,
            max_chars=0,
        )

    assert boundary.attempts == 1
    assert boundary.sleeps == []
    assert_safe_logs(boundary)


@pytest.mark.asyncio
async def test_issue_source_reader_real_auth_failure_is_source_unavailable(boundary):
    boundary.error = GithubException(403, {"message": SECRET})

    with pytest.raises(IssueBudgetError) as error:
        await read_snapshot(
            IssueSourceReader(),
            "owner",
            "repo",
            7,
            include_comments=False,
            max_comments=0,
            max_chars=0,
        )

    assert error.value.failure == "source_unavailable"
    assert SECRET not in str(error.value)
    assert boundary.attempts == 2
    assert_safe_logs(boundary)


def test_real_reinitialization_cancellation_after_ordinary_failure_stops_next_attempt(
    boundary,
):
    boundary.stage = "init"
    boundary.error = ReviewCancelledError(SECRET)

    def ordinary_first_failure(**kwargs):
        boundary.attempts += 1
        raise GithubException(403, {"message": SECRET})

    boundary.app.integration.get_installation = ordinary_first_failure

    with pytest.raises(ReviewCancelledError):
        boundary.app.get_repo_client("owner", "repo")

    assert boundary.attempts == 1
    assert boundary.sleeps == [2]
    assert_safe_logs(boundary)
