"""API v1 认证端点（含移动端 OAuth）"""

import secrets
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, Query, Request
from loguru import logger
from sqlalchemy import select

from backend.api.v1.deps import get_api_current_user, limiter, require_api_auth
from backend.api.v1.responses import error_response, success_response
from backend.api.v1.schemas import (
    MfaRequiredResponse,
    MfaVerifyRequest,
    OAuthAuthorizeResponse,
    OAuthCallbackRequest,
    TokenResponse,
    UserInfoResponse,
)
from backend.core.config import get_settings
from backend.models import database as db_module
from backend.models.telegram_models import TelegramUser
from backend.services.auth_service import (
    AuthProviderError,
    GitHubOAuthProvider,
    auth_service,
)
from backend.services.mfa_lockout_service import (
    AccountLockedError,
    check_mfa_lockout,
    record_mfa_failure,
    reset_mfa_failures,
)
from backend.services.security_admin_service import user_has_any_mfa_method
from backend.services.two_factor_service import (
    TwoFactorError,
    TwoFactorReplayError,
    consume_recovery_code,
    verify_user_totp,
)
from backend.services.webauthn_service import (
    WebAuthnError,
    begin_authentication,
    finish_authentication,
)
from backend.webui.auth import (
    create_access_token,
    create_mfa_pending_token,
    decode_access_token,
    is_mfa_pending_payload,
)
from backend.webui.deps import request_origin
from backend.webui.i18n import i18n as _i18n

# 复用 WebUI OAuth state 管理
from backend.webui.routes.auth import (
    _delete_oauth_state,
    _get_oauth_state,
    _save_oauth_state,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


def _build_user_info_response(
    github_username: str,
    role: str,
    user_id: int,
    github_id: int | None,
    avatar_url: str | None,
    email: str | None = None,
    email_verified: bool = False,
) -> UserInfoResponse:
    return UserInfoResponse(
        sub=github_username,
        role=role,
        user_id=user_id,
        github_id=github_id,
        avatar_url=avatar_url,
        email=email,
        email_verified=email_verified,
    )


@router.get("/github")
@limiter.limit("10/minute")
async def github_authorize(request: Request):
    """获取 GitHub OAuth 授权 URL（JSON 响应，不重定向）"""
    settings = get_settings()

    if not settings.github_oauth_client_id:
        return error_response("GitHub OAuth 未配置", status_code=500)

    if not settings.github_oauth_redirect_uri:
        return error_response("GitHub OAuth 回调地址未配置", status_code=500)

    state = secrets.token_urlsafe(32)
    await _save_oauth_state(state, "/")

    params = {
        "client_id": settings.github_oauth_client_id,
        "redirect_uri": settings.github_oauth_redirect_uri,
        "scope": "read:user user:email",
        "state": state,
    }
    authorization_url = f"{settings.github_oauth_auth_url}?{urlencode(params)}"

    return success_response(
        data=OAuthAuthorizeResponse(
            authorization_url=authorization_url,
            state=state,
        ).model_dump(mode="json")
    )


@router.get("/github/mobile")
@limiter.limit("10/minute")
async def github_mobile_authorize(
    request: Request,
    redirect_uri: str = Query(None, description="移动端回调 URI"),
):
    """获取移动端 GitHub OAuth 授权 URL（支持自定义 redirect_uri）"""
    settings = get_settings()

    if not settings.github_oauth_client_id:
        return error_response("GitHub OAuth 未配置", status_code=500)

    uri = redirect_uri or settings.github_oauth_redirect_uri

    # 白名单校验：自定义 redirect_uri 必须在允许列表中
    if redirect_uri and redirect_uri != settings.github_oauth_redirect_uri:
        allowed = [
            urlparse(u.strip()).geturl()
            for u in (settings.mobile_oauth_allowed_redirect_uris or "").split(",")
            if u.strip()
        ]
        normalized_uri = urlparse(redirect_uri).geturl()
        if normalized_uri not in allowed:
            logger.warning("OAuth redirect URI rejected by allowlist")
            return error_response("不支持的回调地址", status_code=400)

    state = secrets.token_urlsafe(32)
    # 将 redirect_uri 绑定到 state 中，回调时验证一致性
    await _save_oauth_state(state, uri)

    params = {
        "client_id": settings.github_oauth_client_id,
        "redirect_uri": uri,
        "scope": "read:user user:email",
        "state": state,
    }
    authorization_url = f"{settings.github_oauth_auth_url}?{urlencode(params)}"

    return success_response(
        data=OAuthAuthorizeResponse(
            authorization_url=authorization_url,
            state=state,
        ).model_dump(mode="json")
    )


@router.post("/callback")
@limiter.limit("5/minute")
async def github_callback(request: Request, body: OAuthCallbackRequest):
    """移动端 OAuth 回调：用授权码换取 access_token"""
    # 验证 state
    state_data = await _get_oauth_state(body.state) if body.state else None
    if not state_data:
        return error_response("无效的授权请求，请重新登录", status_code=400)

    settings = get_settings()

    # 与 WebUI callback 共用 provider transport、邮箱选择及账户 upsert 逻辑。
    try:
        oauth_result = await GitHubOAuthProvider(settings).exchange_code(body.code)
    except AuthProviderError as exc:
        if exc.status_code is not None and exc.status_code != 200:
            if "token" in str(exc).lower():
                logger.error("API OAuth token exchange failed: status={}", exc.status_code)
                logger.debug("API OAuth token response received with failure status")
                return error_response("获取访问令牌失败", status_code=502)
            return error_response("获取用户信息失败", status_code=502)
        if "timed out" in str(exc).lower():
            return error_response("连接 GitHub 超时", status_code=504)
        if "missing login" in str(exc).lower():
            return error_response("无法获取 GitHub 用户信息", status_code=502)
        if "missing access token" in str(exc).lower():
            return error_response("获取访问令牌失败", status_code=502)
        return error_response("网络连接失败", status_code=504)
    except httpx.TimeoutException:
        return error_response("连接 GitHub 超时", status_code=504)
    except httpx.RequestError:
        return error_response("网络连接失败", status_code=504)
    except Exception:
        logger.error("API OAuth unexpected error")
        return error_response("登录过程中发生错误", status_code=500)

    account = oauth_result.account
    github_username = account.username
    try:
        github_id = int(account.provider_user_id)
    except (TypeError, ValueError):
        github_id = None
    avatar_url = account.avatar_url or ""

    async with db_module.async_session() as session:
        user = await auth_service.authenticate_github(
            session,
            account,
            create_if_missing=True,
        )
        has_mfa_method = await user_has_any_mfa_method(session, user) if user else False

    if not user:
        await _delete_oauth_state(body.state)
        return error_response("用户已停用，请联系管理员", status_code=403)

    token_payload = {
        "sub": github_username,
        "role": user.role,
        "user_id": user.id,
        "github_id": github_id,
        "avatar_url": avatar_url,
        "email": getattr(user, "email", None),
        "email_verified": bool(getattr(user, "email_verified", False)),
    }

    user_info = _build_user_info_response(
        github_username,
        user.role,
        user.id,
        github_id,
        avatar_url,
        user.email,
        bool(user.email_verified),
    )

    if has_mfa_method:
        mfa_token = create_mfa_pending_token(token_payload)
        await _delete_oauth_state(body.state)
        logger.info(f"API OAuth 需要二次验证: {github_username} (role={user.role})")
        return success_response(
            data=MfaRequiredResponse(
                mfa_token=mfa_token,
                user=user_info,
            ).model_dump(mode="json"),
            message="mfa_required",
        )

    jwt_token = create_access_token(token_payload)

    # 登录成功，删除已使用的 state
    await _delete_oauth_state(body.state)
    logger.info(f"API OAuth 登录成功: {github_username} (role={user.role})")

    return success_response(
        data=TokenResponse(
            access_token=jwt_token,
            user=user_info,
        ).model_dump(mode="json")
    )


@router.post("/2fa/verify")
@limiter.limit("5/minute")
async def verify_two_factor(request: Request, body: MfaVerifyRequest):
    """验证移动端/API 二次验证码或恢复码，并签发正式 Token。"""
    payload = decode_access_token(body.mfa_token)
    if not is_mfa_pending_payload(payload):
        return error_response("无效或已过期的二次验证凭证", status_code=401)

    user_id = payload.get("user_id")
    async with db_module.async_session() as session:
        result = await session.execute(
            select(TelegramUser).where(
                TelegramUser.id == user_id,
                TelegramUser.is_active,
            )
        )
        user = result.scalar_one_or_none()
        if not user or not await user_has_any_mfa_method(session, user):
            return error_response("用户未启用二次验证", status_code=400)

        # Check lockout before attempting
        try:
            await check_mfa_lockout(int(user_id))
        except AccountLockedError as exc:
            return error_response(
                _i18n.t("toast.account_locked", seconds=exc.remaining_seconds),
                status_code=429,
            )

        if not user.totp_enabled:
            return error_response("请使用通行密钥完成验证", status_code=400)

        verified = False
        try:
            used_step = verify_user_totp(user, body.code)
            if used_step is not None:
                user.totp_last_used_step = used_step
                verified = True
        except TwoFactorReplayError:
            logger.warning("API TOTP 重放尝试: user_id={}", user_id)
        except TwoFactorError as exc:
            logger.warning("API TOTP 验证失败: user_id={}, error={}", user_id, exc)

        if not verified:
            verified = await consume_recovery_code(session, int(user_id), body.code)

        if not verified:
            await record_mfa_failure(int(user_id))
            await session.rollback()
            return error_response("验证码或恢复码无效", status_code=400)

        await reset_mfa_failures(int(user_id))
        await session.commit()

    token_payload = {
        "sub": payload.get("sub"),
        "role": payload.get("role"),
        "user_id": payload.get("user_id"),
        "github_id": payload.get("github_id"),
        "avatar_url": payload.get("avatar_url"),
        "email": payload.get("email"),
        "email_verified": payload.get("email_verified", False),
    }
    jwt_token = create_access_token(token_payload)
    logger.info("API 二次验证成功: user={}", payload.get("sub"))

    return success_response(
        data=TokenResponse(
            access_token=jwt_token,
            user=_build_user_info_response(
                payload.get("sub") or "",
                payload.get("role") or "user",
                int(payload.get("user_id")),
                payload.get("github_id"),
                payload.get("avatar_url"),
                payload.get("email"),
                bool(payload.get("email_verified", False)),
            ),
        ).model_dump(mode="json")
    )


@router.post("/logout")
@limiter.limit("10/minute")
async def logout(request: Request, user: dict = Depends(get_api_current_user)):
    """登出（API 模式下客户端自行删除 token）"""
    logger.info(f"API 用户登出: {user.get('sub')}")
    return success_response(message="已退出登录")


@router.post("/2fa/passkey/options")
@limiter.limit(lambda: get_settings().passkeys_authentication_rate_limit)
async def api_passkey_options(request: Request, body: dict):
    """创建 API Passkey 认证 options。"""
    mfa_token = body.get("mfa_token")
    payload = decode_access_token(mfa_token) if mfa_token else None
    if not is_mfa_pending_payload(payload):
        return error_response(_i18n.t("api.invalid_mfa_token"), status_code=401)

    user_id = int(payload["user_id"])
    try:
        await check_mfa_lockout(user_id)
    except AccountLockedError as exc:
        return error_response(
            _i18n.t("toast.account_locked", seconds=exc.remaining_seconds),
            status_code=429,
        )

    async with db_module.async_session() as session:
        try:
            data = await begin_authentication(session, user_id, request_origin(request))
        except WebAuthnError as exc:
            logger.warning(
                "Passkey 开始认证失败 / begin authentication failed: {}", exc
            )
            return error_response("Passkey 认证失败，请重试", status_code=400)
    return success_response(data=data)


@router.post("/2fa/passkey/verify")
@limiter.limit(lambda: get_settings().passkeys_authentication_rate_limit)
async def api_passkey_verify(request: Request, body: dict):
    """验证 API Passkey 认证结果并签发正式 Token。"""
    mfa_token = body.get("mfa_token")
    payload = decode_access_token(mfa_token) if mfa_token else None
    if not is_mfa_pending_payload(payload):
        return error_response(_i18n.t("api.invalid_mfa_token"), status_code=401)

    user_id = int(payload["user_id"])
    try:
        await check_mfa_lockout(user_id)
    except AccountLockedError as exc:
        return error_response(
            _i18n.t("toast.account_locked", seconds=exc.remaining_seconds),
            status_code=429,
        )

    try:
        async with db_module.async_session() as session:
            await finish_authentication(
                session,
                body.get("challenge_id", ""),
                body.get("credential", {}),
                user_id,
            )
            await session.commit()
    except AccountLockedError as exc:
        return error_response(
            _i18n.t("toast.account_locked", seconds=exc.remaining_seconds),
            status_code=429,
        )
    except Exception as exc:
        await record_mfa_failure(user_id)
        logger.warning("API Passkey 验证失败: user_id={}, error={}", user_id, exc)
        return error_response(_i18n.t("toast.passkey_login_failed"), status_code=400)

    await reset_mfa_failures(user_id)
    token_payload = {
        "sub": payload.get("sub"),
        "role": payload.get("role"),
        "user_id": user_id,
        "github_id": payload.get("github_id"),
        "avatar_url": payload.get("avatar_url"),
        "email": payload.get("email"),
        "email_verified": payload.get("email_verified", False),
    }
    jwt_token = create_access_token(token_payload)
    logger.info("API Passkey 二次验证成功: user={}", payload.get("sub"))

    user_info = _build_user_info_response(
        payload.get("sub") or "",
        payload.get("role") or "user",
        user_id,
        payload.get("github_id"),
        payload.get("avatar_url"),
        payload.get("email"),
        bool(payload.get("email_verified", False)),
    )
    return success_response(
        data=TokenResponse(
            access_token=jwt_token,
            user=user_info,
        ).model_dump(mode="json")
    )


@router.get("/me")
async def get_current_user_info(user: dict = Depends(require_api_auth)):
    """获取当前认证用户信息"""
    return success_response(
        data=UserInfoResponse(
            sub=user["sub"],
            role=user["role"],
            user_id=user["user_id"],
            github_id=user.get("github_id"),
            avatar_url=user.get("avatar_url"),
            email=user.get("email"),
            email_verified=bool(user.get("email_verified", False)),
        ).model_dump(mode="json")
    )
