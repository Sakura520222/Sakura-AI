"""Concurrent token refresh tests for Star Aid."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models.star_aid_models import StarAidCredential, StarAidMember
from backend.services import star_aid_github_service as gh
from backend.services.secret_crypto_service import encrypt_secret


@pytest.mark.asyncio
async def test_concurrent_refresh_calls_github_only_once(monkeypatch):
    """同一 user 的并发请求遇到 access token 过期时，只应调用 1 次 GitHub refresh，
    后续请求复用刷新后的新 token。"""
    now = datetime.now(UTC)
    user_id = 999

    # 初始已过期的凭据
    cred = StarAidCredential(
        user_id=user_id,
        github_username="test_user",
        encrypted_access_token=encrypt_secret("old_access_token"),
        access_token_expires_at=now - timedelta(minutes=10),
        encrypted_refresh_token=encrypt_secret("old_refresh_token"),
        refresh_token_expires_at=now + timedelta(days=10),
        revoked_at=None,
    )

    refresh_calls = 0

    async def fake_refresh_user_access_token(client_id, client_secret, refresh_token):
        nonlocal refresh_calls
        refresh_calls += 1
        # 模拟少许耗时触发并发
        await asyncio.sleep(0.05)
        return {
            "access_token": "brand_new_access_token",
            "refresh_token": "brand_new_refresh_token",
            "expires_in": 3600,
            "refresh_token_expires_in": 86400,
        }

    monkeypatch.setattr(gh, "refresh_user_access_token", fake_refresh_user_access_token)
    monkeypatch.setattr(gh, "_client_credentials", lambda: ("fake_client_id", "fake_client_secret"))

    async def fake_save_credential(session, uid, gh_user, payload):
        cred.encrypted_access_token = encrypt_secret(payload["access_token"])
        cred.access_token_expires_at = datetime.now(UTC) + timedelta(seconds=payload["expires_in"])
        cred.encrypted_refresh_token = encrypt_secret(payload["refresh_token"])
        cred.refresh_token_expires_at = datetime.now(UTC) + timedelta(seconds=payload["refresh_token_expires_in"])
        return cred

    monkeypatch.setattr(gh, "save_credential_from_token", fake_save_credential)

    async def fake_get_cred(session, uid):
        return cred

    monkeypatch.setattr(gh, "get_credential", fake_get_cred)

    # Model caller transactions serialized through the credential row lock.
    row_lock = asyncio.Lock()
    sessions = []

    async def call_with_caller_session():
        session = AsyncMock()
        sessions.append(session)

        async def locked_read(_query):
            if _query.column_descriptions[0]["entity"] is StarAidCredential:
                await row_lock.acquire()
                return MagicMock(scalar_one_or_none=lambda: cred)
            return MagicMock(scalar_one_or_none=lambda: None)

        session.execute.side_effect = locked_read
        try:
            return await gh.get_effective_access_token(session, user_id)
        finally:
            await session.commit()
            if row_lock.locked() and session.execute.await_count == 2:
                row_lock.release()

    # 5 个并发协程同时调用 get_effective_access_token
    results = await asyncio.gather(
        *(call_with_caller_session() for _ in range(5))
    )

    # 验证 GitHub 仅被请求刷新 1 次
    assert refresh_calls == 1
    assert sum(session.execute.await_count for session in sessions) == 10
    assert all(session.commit.await_count == 1 for session in sessions)
    from sqlalchemy.dialects import mysql

    locked_query = sessions[0].execute.call_args_list[1].args[0]
    assert "FOR UPDATE" in str(locked_query.compile(dialect=mysql.dialect()))
    assert locked_query.get_execution_options()["populate_existing"] is True

    # 所有 5 个请求都成功拿到了新 token
    for token, call_res in results:
        assert token == "brand_new_access_token"
        assert call_res.success is True
        assert call_res.reauth_required is False


@pytest.mark.asyncio
async def test_race_refresh_failure_does_not_revoke_if_already_refreshed(monkeypatch):
    """如果两个请求发生竞态，一个请求已成功刷新导致旧 refresh token 换新，
    旧请求失败时不应将用户标记为 reauth_required。"""
    now = datetime.now(UTC)
    user_id = 888

    # 模拟数据库中已有最新凭据
    cred = StarAidCredential(
        user_id=user_id,
        github_username="test_user",
        encrypted_access_token=encrypt_secret("already_refreshed_access_token"),
        access_token_expires_at=now + timedelta(minutes=50),
        encrypted_refresh_token=encrypt_secret("new_refresh_token"),
        refresh_token_expires_at=now + timedelta(days=10),
        revoked_at=None,
    )

    # 被传入的旧 cred
    old_cred = StarAidCredential(
        user_id=user_id,
        github_username="test_user",
        encrypted_access_token=encrypt_secret("old_access_token"),
        access_token_expires_at=now - timedelta(minutes=10),
        encrypted_refresh_token=encrypt_secret("stale_refresh_token"),
        refresh_token_expires_at=now + timedelta(days=10),
        revoked_at=None,
    )

    fake_session = AsyncMock()

    # 模拟 GitHub 返回 invalid_grant（因为 stale refresh token）
    async def fake_refresh_user_access_token(client_id, client_secret, refresh_token):
        return {"error": "bad_refresh_token", "error_description": "The refresh token is invalid"}

    monkeypatch.setattr(gh, "refresh_user_access_token", fake_refresh_user_access_token)
    monkeypatch.setattr(gh, "_client_credentials", lambda: ("fake_client_id", "fake_client_secret"))

    # get_credential 返回数据库中的最新 cred
    async def fake_get_cred(session, uid):
        return cred

    monkeypatch.setattr(gh, "get_credential", fake_get_cred)

    reauth_called = False

    async def fake_mark_reauth(session, uid):
        nonlocal reauth_called
        reauth_called = True

    monkeypatch.setattr(gh, "mark_reauth_required", fake_mark_reauth)

    token, res = await gh._refresh_and_persist(fake_session, old_cred)

    # 不应该被 revoke，而是应当复用数据库中已更新的有效 token
    assert reauth_called is False
    assert token == "already_refreshed_access_token"
    assert res.success is True


@pytest.mark.asyncio
async def test_refresh_uses_caller_transaction_and_reuses_locked_credential(monkeypatch):
    """The token refresh must not open another session while the caller owns a connection."""
    now = datetime.now(UTC)
    stale = StarAidCredential(
        user_id=123, github_username="test",
        encrypted_access_token=encrypt_secret("old_access"),
        access_token_expires_at=now - timedelta(minutes=1),
        encrypted_refresh_token=encrypt_secret("old_refresh"),
        refresh_token_expires_at=now + timedelta(days=1),
    )
    latest = StarAidCredential(
        user_id=123, github_username="test",
        encrypted_access_token=stale.encrypted_access_token,
        access_token_expires_at=stale.access_token_expires_at,
        encrypted_refresh_token=stale.encrypted_refresh_token,
        refresh_token_expires_at=stale.refresh_token_expires_at,
    )
    caller = AsyncMock()
    monkeypatch.setattr(gh, "get_credential", AsyncMock(return_value=stale))
    monkeypatch.setattr(gh, "_client_credentials", lambda: ("id", "secret"))
    refresh_calls = 0

    async def fake_refresh(*_args):
        nonlocal refresh_calls
        refresh_calls += 1
        return {"access_token": "new_access", "refresh_token": "new_refresh",
                "expires_in": 3600, "refresh_token_expires_in": 86400}

    async def fake_save(_session, _user_id, _username, payload):
        latest.encrypted_access_token = encrypt_secret(payload["access_token"])
        latest.encrypted_refresh_token = encrypt_secret(payload["refresh_token"])
        latest.access_token_expires_at = now + timedelta(hours=1)
        return latest

    monkeypatch.setattr(gh, "refresh_user_access_token", fake_refresh)
    monkeypatch.setattr(gh, "save_credential_from_token", fake_save)
    caller.execute.return_value = MagicMock(scalar_one_or_none=lambda: latest)
    for _ in range(2):
        token, result = await gh.get_effective_access_token(caller, 123)
        assert result.success and token == "new_access"
    assert refresh_calls == 1
    assert caller.execute.await_count == 4
    caller.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_denied_refresh_locks_member_before_credential(monkeypatch):
    """The worker and a concurrent manual/summary request use the same row order."""
    now = datetime.now(UTC)
    member = StarAidMember(user_id=123, status="active")
    cred = StarAidCredential(
        user_id=123,
        encrypted_access_token=encrypt_secret("expired"),
        access_token_expires_at=now - timedelta(minutes=1),
        encrypted_refresh_token=None,
    )
    monkeypatch.setattr(gh, "get_credential", AsyncMock(return_value=cred))
    session = AsyncMock()
    order = []

    def queried(stmt):
        entity = stmt.column_descriptions[0]["entity"]
        order.append(entity)
        return MagicMock(
            scalar_one_or_none=lambda: member if entity is StarAidMember else cred
        )

    session.execute.side_effect = queried
    token, result = await gh.get_effective_access_token(session, 123)

    assert token is None and result.reauth_required
    assert order == [
        StarAidMember, StarAidCredential, StarAidMember, StarAidCredential,
    ]
    from sqlalchemy.dialects import mysql

    for call in session.execute.call_args_list:
        assert "FOR UPDATE" in str(call.args[0].compile(dialect=mysql.dialect()))
    assert member.status == "reauth_required"
    assert cred.revoked_at is not None


@pytest.mark.asyncio
async def test_authorization_callback_locks_member_before_saving_credential(monkeypatch):
    """A later identity-mismatch reauth cannot invert the worker lock order."""
    monkeypatch.setattr(gh, "_client_credentials", lambda: ("id", "secret"))
    monkeypatch.setattr(
        gh, "exchange_user_access_token",
        AsyncMock(return_value={"access_token": "new-token"}),
    )
    session = AsyncMock()
    events = []

    async def execute(stmt):
        events.append(("lock", stmt.column_descriptions[0]["entity"]))
        return MagicMock()

    async def save(*_args):
        events.append(("save", StarAidCredential))
        return MagicMock()

    session.execute.side_effect = execute
    monkeypatch.setattr(gh, "save_credential_from_token", save)

    await gh.exchange_authorization_code(session, 123, "name", "code")

    assert events == [
        ("lock", StarAidMember),
        ("save", StarAidCredential),
    ]
