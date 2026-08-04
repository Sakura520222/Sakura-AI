"""仓库互助 GitHub 交互服务 / Star-aid GitHub interaction service.

负责 GitHub App user-to-server 的全部外部交互与凭据管理：

- 凭据加密存取（``StarAidCredential``）。
- user access token 自动刷新（过期前用 refresh_token 换新，旧 refresh 即时失效）。
- 列出用户可展示的公开仓库、读取 README / 元数据。
- star / unstar / 检查是否已 star。
- 统一解析 rate-limit / 401 / 403 / 404 / 422，返回结构化 ``GitHubCallResult``。

安全要求：日志与异常中绝不出现 token 前缀或全文；只记录 user_id、状态码、
 sanitized 错误信息。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.core.github_app import (
    exchange_user_access_token,
    refresh_user_access_token,
)
from backend.models.star_aid_models import (
    MEMBER_STATUS_REAUTH_REQUIRED,
    StarAidCredential,
    StarAidMember,
)
from backend.services.secret_crypto_service import (
    SecretCryptoError,
    decrypt_secret,
    encrypt_secret,
)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

# 请求超时（秒）
_REQUEST_TIMEOUT = 15


@dataclass
class GitHubCallResult:
    """GitHub REST 调用的统一结果对象 / Unified GitHub REST call result.

    ``data`` 存放成功时的解析后响应体；错误时 ``error_message`` 仅含
    sanitized 文本，绝不包含 token。
    """

    success: bool = False
    status_code: int | None = None
    rate_limit_reset_at: datetime | None = None
    rate_limit_remaining: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    reauth_required: bool = False
    already_done: bool = False
    data: dict | None = field(default=None)


def _common_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def _parse_rate_limit(headers: httpx.Headers) -> tuple[int | None, datetime | None]:
    """从响应头解析 remaining / reset。"""
    remaining_raw = headers.get("x-ratelimit-remaining")
    reset_raw = headers.get("x-ratelimit-reset")
    remaining = (
        int(remaining_raw) if remaining_raw and remaining_raw.isdigit() else None
    )
    reset_at: datetime | None = None
    if reset_raw and reset_raw.isdigit():
        reset_at = datetime.utcfromtimestamp(int(reset_raw))
    return remaining, reset_at


def _sanitize_error_message(body: dict | str | None) -> str:
    """从 GitHub 错误响应提取并净化 message，绝不包含 token。"""
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error") or ""
    else:
        msg = str(body) if body else ""
    # 防御性剔除任何疑似 token 片段（GitHub 正常 message 不会包含，这里仅兜底）
    for token_prefix in ("gho_", "ghr_", "ghu_", "ghs_", "ghp_"):
        if token_prefix in msg:
            msg = msg.split(token_prefix)[0].rstrip() + " [redacted]"
            break
    return msg.strip()


def _result_from_response(
    resp: httpx.Response,
    *,
    success_statuses: tuple[int, ...] = (200,),
    already_done_statuses: tuple[int, ...] = (),
) -> GitHubCallResult:
    """把 httpx 响应转成 ``GitHubCallResult``。"""
    remaining, reset_at = _parse_rate_limit(resp.headers)
    base = GitHubCallResult(
        status_code=resp.status_code,
        rate_limit_remaining=remaining,
        rate_limit_reset_at=reset_at,
    )

    if resp.status_code in already_done_statuses:
        base.already_done = True
        base.success = True
        return base

    if resp.status_code in success_statuses:
        base.success = True
        try:
            base.data = resp.json()
        except ValueError:
            base.data = None
        return base

    # 错误分支
    try:
        body = resp.json()
    except ValueError:
        body = resp.text

    base.error_message = _sanitize_error_message(body)
    if isinstance(body, dict):
        base.error_code = str(body.get("message") or "") or None

    if resp.status_code == 401:
        base.reauth_required = True
        base.error_code = "bad_credentials"
    elif resp.status_code == 403:
        # rate limit 命中：remaining=0
        if remaining == 0:
            base.error_code = "rate_limited"
        else:
            base.error_code = "forbidden"
    elif resp.status_code == 404:
        base.error_code = "not_found"
    elif resp.status_code == 422:
        base.error_code = "validation_failed"
    return base


# ========== 凭据管理 / Credential management ==========


def _client_credentials() -> tuple[str, str]:
    """从配置取 GitHub App client id / secret。"""
    settings = get_settings()
    return (
        settings.star_aid_github_app_client_id,
        settings.star_aid_github_app_client_secret,
    )


async def save_credential_from_token(
    session: AsyncSession,
    user_id: int,
    github_username: str,
    token_payload: dict,
) -> StarAidCredential:
    """把 token 交换/刷新返回的凭据加密写库（upsert）。"""
    now = datetime.utcnow()
    access_token = token_payload.get("access_token") or ""
    refresh_token = token_payload.get("refresh_token") or ""
    expires_in = token_payload.get("expires_in")
    refresh_expires_in = token_payload.get("refresh_token_expires_in")

    result = await session.execute(
        select(StarAidCredential).where(StarAidCredential.user_id == user_id)
    )
    cred = result.scalar_one_or_none()
    if cred is None:
        cred = StarAidCredential(user_id=user_id, github_username=github_username)
        session.add(cred)

    cred.github_username = github_username
    cred.encrypted_access_token = encrypt_secret(access_token)
    cred.access_token_expires_at = (
        now + timedelta(seconds=int(expires_in)) if expires_in else None
    )
    cred.encrypted_refresh_token = (
        encrypt_secret(refresh_token) if refresh_token else None
    )
    cred.refresh_token_expires_at = (
        now + timedelta(seconds=int(refresh_expires_in)) if refresh_expires_in else None
    )
    cred.token_type = token_payload.get("token_type") or "bearer"
    client_id, _ = _client_credentials()
    cred.github_app_client_id = client_id
    cred.last_authorized_at = now
    cred.last_refresh_at = now
    cred.revoked_at = None

    await session.flush()
    logger.info("star_aid credential saved: user_id={}", user_id)
    return cred


async def get_credential(
    session: AsyncSession, user_id: int
) -> StarAidCredential | None:
    result = await session.execute(
        select(StarAidCredential).where(StarAidCredential.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def mark_reauth_required(session: AsyncSession, user_id: int) -> None:
    """标记用户需要重新授权：吊销凭据并把成员状态置为 reauth_required。"""
    now = datetime.utcnow()
    cred = await get_credential(session, user_id)
    if cred and cred.revoked_at is None:
        cred.revoked_at = now
    member_result = await session.execute(
        select(StarAidMember).where(StarAidMember.user_id == user_id)
    )
    member = member_result.scalar_one_or_none()
    if member and member.status not in (
        MEMBER_STATUS_REAUTH_REQUIRED,
        "left",
        "banned",
    ):
        member.status = MEMBER_STATUS_REAUTH_REQUIRED
    await session.flush()
    logger.warning("star_aid reauth required: user_id={}", user_id)


async def exchange_authorization_code(
    session: AsyncSession,
    user_id: int,
    github_username: str,
    code: str,
    redirect_uri: str | None = None,
) -> StarAidCredential | None:
    """用授权码交换 token 并写库；失败返回 None。"""
    client_id, client_secret = _client_credentials()
    if not client_id or not client_secret:
        logger.error("star_aid GitHub App client id/secret not configured")
        return None
    try:
        token_payload = await exchange_user_access_token(
            client_id, client_secret, code, redirect_uri
        )
    except Exception as exc:
        logger.error(
            "star_aid token exchange failed: user_id={}, error={}", user_id, exc
        )
        return None

    if token_payload.get("error") or not token_payload.get("access_token"):
        logger.warning(
            "star_aid token exchange denied: user_id={}, error={}",
            user_id,
            token_payload.get("error_description") or token_payload.get("error"),
        )
        return None

    return await save_credential_from_token(
        session, user_id, github_username, token_payload
    )


async def get_effective_access_token(
    session: AsyncSession, user_id: int
) -> tuple[str | None, GitHubCallResult]:
    """获取可用的 user access token，必要时自动刷新。

    Returns:
        (token, result)。token 为 None 表示需要重新授权（result.reauth_required）。
    """
    cred = await get_credential(session, user_id)
    if cred is None or cred.revoked_at is not None:
        return None, GitHubCallResult(reauth_required=True, error_code="no_credential")

    try:
        access_token = decrypt_secret(cred.encrypted_access_token)
    except SecretCryptoError:
        logger.error("star_aid access token decrypt failed: user_id={}", user_id)
        await mark_reauth_required(session, user_id)
        return None, GitHubCallResult(reauth_required=True, error_code="decrypt_failed")

    now = datetime.utcnow()
    # 未过期（或长期 token 无 expires_at）直接返回
    expired = (
        cred.access_token_expires_at is not None
        and cred.access_token_expires_at <= now + timedelta(minutes=5)
    )
    if not expired:
        return access_token, GitHubCallResult(success=True)

    # 需要刷新
    refreshed, result = await _refresh_and_persist(session, cred)
    if refreshed is None:
        return None, result
    return refreshed, GitHubCallResult(success=True)


async def _refresh_and_persist(
    session: AsyncSession, cred: StarAidCredential
) -> tuple[str | None, GitHubCallResult]:
    """用 refresh_token 刷新并写库；失败标记 reauth_required。"""
    if not cred.encrypted_refresh_token:
        await mark_reauth_required(session, cred.user_id)
        return None, GitHubCallResult(
            reauth_required=True, error_code="no_refresh_token"
        )

    now = datetime.utcnow()
    if (
        cred.refresh_token_expires_at is not None
        and cred.refresh_token_expires_at <= now
    ):
        await mark_reauth_required(session, cred.user_id)
        return None, GitHubCallResult(
            reauth_required=True, error_code="refresh_expired"
        )

    try:
        refresh_token = decrypt_secret(cred.encrypted_refresh_token)
    except SecretCryptoError:
        await mark_reauth_required(session, cred.user_id)
        return None, GitHubCallResult(reauth_required=True, error_code="decrypt_failed")

    client_id, client_secret = _client_credentials()
    try:
        token_payload = await refresh_user_access_token(
            client_id, client_secret, refresh_token
        )
    except Exception as exc:
        logger.error(
            "star_aid token refresh failed: user_id={}, error={}", cred.user_id, exc
        )
        await mark_reauth_required(session, cred.user_id)
        return None, GitHubCallResult(reauth_required=True, error_code="refresh_failed")

    if token_payload.get("error") or not token_payload.get("access_token"):
        await mark_reauth_required(session, cred.user_id)
        return None, GitHubCallResult(reauth_required=True, error_code="refresh_denied")

    # 刷新会签发新 refresh_token，旧 refresh_token 失效，必须覆盖
    await save_credential_from_token(
        session, cred.user_id, cred.github_username or "", token_payload
    )
    new_access = token_payload.get("access_token") or ""
    logger.info("star_aid token refreshed: user_id={}", cred.user_id)
    return new_access, GitHubCallResult(success=True)


# ========== GitHub REST 操作 / GitHub REST operations ==========


async def fetch_authenticated_user(access_token: str) -> dict | None:
    """GET /user，确认 token 持有者。401/失败返回 None。"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API_BASE}/user",
                headers=_common_headers(access_token),
                timeout=_REQUEST_TIMEOUT,
            )
    except httpx.RequestError:
        return None
    if resp.status_code != 200:
        return None
    return resp.json()


async def list_user_public_repositories(access_token: str) -> list[dict]:
    """列出 user token 可访问的全部公开仓库（自动翻页）。"""
    repos: list[dict] = []
    page = 1
    try:
        async with httpx.AsyncClient() as client:
            while True:
                resp = await client.get(
                    f"{GITHUB_API_BASE}/user/repos",
                    headers=_common_headers(access_token),
                    params={
                        "visibility": "public",
                        "affiliation": "owner,collaborator,organization_member",
                        "sort": "updated",
                        "direction": "desc",
                        "per_page": 100,
                        "page": page,
                    },
                    timeout=_REQUEST_TIMEOUT,
                )
                if resp.status_code != 200:
                    logger.warning(
                        "star_aid list repos failed: status={}", resp.status_code
                    )
                    return repos
                data = resp.json()
                if not isinstance(data, list) or not data:
                    return repos
                repos.extend(data)
                page += 1
    except httpx.RequestError as exc:
        logger.warning("star_aid list repos network error: {}", exc)
        return repos


async def get_repository_metadata(
    access_token: str, owner: str, repo: str
) -> GitHubCallResult:
    """GET /repos/{owner}/{repo}。"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}",
                headers=_common_headers(access_token),
                timeout=_REQUEST_TIMEOUT,
            )
    except httpx.RequestError as exc:
        return GitHubCallResult(error_message=str(exc), error_code="network_error")
    return _result_from_response(resp)


async def get_readme(
    access_token: str, owner: str, repo: str
) -> tuple[str | None, str | None]:
    """读取仓库 README，返回 (sha, decoded_text)。

    GitHub 返回 base64 编码的 content。失败返回 (None, None)。
    不对原文做截断——完整内容交由调用方（摘要服务）按需处理。
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/readme",
                headers={
                    **_common_headers(access_token),
                    "Accept": "application/vnd.github+json",
                },
                timeout=_REQUEST_TIMEOUT,
            )
    except httpx.RequestError:
        return None, None
    if resp.status_code != 200:
        return None, None
    data = resp.json()
    sha = data.get("sha")
    content_b64 = data.get("content") or ""
    encoding = data.get("encoding") or "base64"
    try:
        if encoding == "base64":
            content = base64.b64decode(content_b64).decode("utf-8", errors="replace")
        else:
            content = content_b64
    except Exception:
        return sha, None
    return sha, content


async def is_starred(access_token: str, owner: str, repo: str) -> GitHubCallResult:
    """GET /user/starred/{owner}/{repo}：204=已 star，404=未 star。"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GITHUB_API_BASE}/user/starred/{owner}/{repo}",
                headers=_common_headers(access_token),
                timeout=_REQUEST_TIMEOUT,
            )
    except httpx.RequestError as exc:
        return GitHubCallResult(error_message=str(exc), error_code="network_error")
    return _result_from_response(
        resp, success_statuses=(204,), already_done_statuses=()
    )


async def star_repository(access_token: str, owner: str, repo: str) -> GitHubCallResult:
    """PUT /user/starred/{owner}/{repo}。"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"{GITHUB_API_BASE}/user/starred/{owner}/{repo}",
                headers=_common_headers(access_token),
                timeout=_REQUEST_TIMEOUT,
            )
    except httpx.RequestError as exc:
        return GitHubCallResult(error_message=str(exc), error_code="network_error")
    return _result_from_response(resp, success_statuses=(204,))


async def unstar_repository(
    access_token: str, owner: str, repo: str
) -> GitHubCallResult:
    """DELETE /user/starred/{owner}/{repo}。"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{GITHUB_API_BASE}/user/starred/{owner}/{repo}",
                headers=_common_headers(access_token),
                timeout=_REQUEST_TIMEOUT,
            )
    except httpx.RequestError as exc:
        return GitHubCallResult(error_message=str(exc), error_code="network_error")
    return _result_from_response(resp, success_statuses=(204,))
