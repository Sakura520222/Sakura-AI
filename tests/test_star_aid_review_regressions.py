"""Regressions for retryable star failures and worker-wide pacing."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models.star_aid_models import StarAidMember, StarAidRepository
from backend.services import star_aid_github_service as gh
from backend.services import star_aid_service
from backend.workers import star_aid_worker


@pytest.mark.asyncio
async def test_transient_token_refresh_failure_does_not_require_reauth(monkeypatch):
    repo = StarAidRepository(
        id=1, owner_user_id=2, repo_id=3, full_name="owner/repo",
        is_displayed=True, is_public=True, is_archived=False,
        disabled_by_admin=False,
    )
    session = AsyncMock()
    session.execute.return_value = MagicMock(scalar_one_or_none=lambda: repo)
    monkeypatch.setattr(
        star_aid_service.gh, "get_effective_access_token",
        AsyncMock(return_value=(
            None, gh.GitHubCallResult(error_code="refresh_network_error"),
        )),
    )
    log = AsyncMock()
    monkeypatch.setattr(star_aid_service, "_upsert_action_log", log)

    outcome = await star_aid_service.perform_star(
        session, actor_user_id=1, repository_id=1, trigger="manual",
    )

    assert outcome["status"] == "failed"
    assert outcome["reauth_required"] is False
    assert log.await_args.kwargs["status"] == "failed"
    assert log.await_args.kwargs["error_code"] == "refresh_network_error"


@pytest.mark.asyncio
async def test_star_commits_rotated_token_before_github_request(monkeypatch):
    repo = StarAidRepository(
        id=1, owner_user_id=2, repo_id=3, full_name="owner/repo",
        is_displayed=True, is_public=True, is_archived=False,
        disabled_by_admin=False,
    )
    session = AsyncMock()
    session.execute.return_value = MagicMock(scalar_one_or_none=lambda: repo)
    events = []

    async def committed():
        events.append("commit")

    async def checked(*_args):
        events.append("github")
        return gh.GitHubCallResult(success=True)

    session.commit.side_effect = committed
    monkeypatch.setattr(
        star_aid_service.gh, "get_effective_access_token",
        AsyncMock(return_value=("rotated-token", gh.GitHubCallResult(success=True))),
    )
    monkeypatch.setattr(star_aid_service.gh, "is_starred", checked)
    monkeypatch.setattr(star_aid_service, "_upsert_action_log", AsyncMock())

    outcome = await star_aid_service.perform_star(
        session, actor_user_id=1, repository_id=1, trigger="manual",
    )
    assert outcome["status"] == "already_done"
    assert events == ["commit", "github"]


@pytest.mark.asyncio
async def test_pacing_applies_to_first_target_of_next_member(monkeypatch):
    worker = star_aid_worker.StarAidWorker()
    members = {
        1: StarAidMember(id=1, user_id=11, status="active"),
        2: StarAidMember(id=2, user_id=22, status="active"),
    }
    for member in members.values():
        member.last_daily_reset_at = datetime.now(UTC)
    session = AsyncMock()
    session.get.side_effect = lambda _model, member_id: members[member_id]
    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=session)
    session_factory.return_value.__aexit__ = AsyncMock()
    monkeypatch.setattr(star_aid_worker, "async_session", session_factory)
    monkeypatch.setattr(worker, "_needs_daily_reset", lambda _member: False)
    monkeypatch.setattr(worker, "_select_targets", AsyncMock(return_value=[100]))
    monkeypatch.setattr(star_aid_worker.star_aid_service, "perform_star",
                        AsyncMock(return_value={"status": "success"}))
    monkeypatch.setattr(star_aid_worker, "get_dynamic_config",
                        AsyncMock(return_value=15))
    sleep = AsyncMock()
    monkeypatch.setattr(star_aid_worker.asyncio, "sleep", sleep)

    await worker._process_member(1)
    await worker._process_member(2)

    assert star_aid_worker.star_aid_service.perform_star.await_count == 2
    sleep.assert_awaited_once()
    assert 1 <= sleep.await_args.args[0] <= 2
    assert session.commit.await_count == 2
