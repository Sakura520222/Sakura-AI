"""Concurrent token refresh tests for Star Aid."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from backend.models.star_aid_models import StarAidCredential
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

    fake_session = AsyncMock()

    async def fake_get_cred(session, uid):
        return cred

    monkeypatch.setattr(gh, "get_credential", fake_get_cred)

    # 5 个并发协程同时调用 get_effective_access_token
    results = await asyncio.gather(
        gh.get_effective_access_token(fake_session, user_id),
        gh.get_effective_access_token(fake_session, user_id),
        gh.get_effective_access_token(fake_session, user_id),
        gh.get_effective_access_token(fake_session, user_id),
        gh.get_effective_access_token(fake_session, user_id),
    )

    # 验证 GitHub 仅被请求刷新 1 次
    assert refresh_calls == 1

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
