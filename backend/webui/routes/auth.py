"""WebUI GitHub OAuth 认证路由"""

import json
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import (
    APIRouter,
    Request,
    Depends,
    Form,
    HTTPException,
    Query,
    Header,
    Body,
)
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from loguru import logger
from sqlalchemy import select

from backend import __version__
from backend.models.telegram_models import TelegramUser
from backend.models import database as db_module
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
from backend.services.security_admin_service import user_has_any_mfa_method
from backend.services.mfa_lockout_service import (
    AccountLockedError,
    check_mfa_lockout,
    record_mfa_failure,
    reset_mfa_failures,
)
from backend.webui.auth import (
    create_access_token,
    create_mfa_pending_token,
    decode_access_token,
    is_mfa_pending_payload,
)
from backend.webui.deps import (
    get_templates,
    validate_csrf_token,
    get_csrf_serializer,
    require_csrf_header,
    request_origin,
    toast_redirect,
    render_template,
)
from backend.webui.i18n import detect_language, i18n as _i18n
from backend.core.config import get_settings
from backend.core.redis import get_async_redis
from backend.core.rate_limit import limiter

router = APIRouter(prefix="/auth", tags=["WebUI Auth"])
templates = get_templates()


def _get_telegram_deep_link() -> str | None:
    """构建 Telegram Bot 深链接（用于注册引导）"""
    settings = get_settings()
    if settings.telegram_bot_username:
        return f"https://t.me/{settings.telegram_bot_username}?start=sign"
    return None


APP_VERSION = __version__

_OAUTH_STATE_TTL = 600  # state 有效期 10 分钟
_OAUTH_STATE_KEY_PREFIX = "oauth:state:"
_oauth_states_fallback: dict[str, dict] = {}  # Redis 故障时的内存回退
_MAX_FALLBACK_STATES = 1000
MFA_PENDING_COOKIE_NAME = "webui_mfa_token"


def _cleanup_expired_states():
    """清理过期的 OAuth state"""
    now = time.time()
    expired = [
        s for s, d in _oauth_states_fallback.items() if d.get("expires", 0) <= now
    ]
    for s in expired:
        _oauth_states_fallback.pop(s, None)


def _oauth_error(
    request: Request,
    error_msg: str,
    has_oauth: bool = True,
    status_code: int = 400,
    telegram_deep_link: str | None = None,
):
    """统一的 OAuth 错误页面响应"""
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "csrf_token": get_csrf_serializer().dumps({}),
            "error": error_msg,
            "app_version": APP_VERSION,
            "has_oauth": has_oauth,
            "telegram_deep_link": telegram_deep_link,
        },
        status_code=status_code,
    )


def _build_login_token_payload(
    user: TelegramUser,
    github_username: str,
    github_id: int | None,
    avatar_url: str,
) -> dict:
    """构建 WebUI/API 登录 JWT payload。"""
    return {
        "sub": github_username,
        "role": user.role,
        "user_id": user.id,
        "github_id": github_id,
        "avatar_url": avatar_url,
    }


def _set_webui_token_cookie(response: RedirectResponse | JSONResponse, token: str):
    """写入正式 WebUI 登录 Cookie。"""
    settings = get_settings()
    response.set_cookie(
        "webui_token",
        token,
        httponly=True,
        secure=settings.webui_cookie_secure,
        max_age=86400,
        samesite="lax",
    )


def _set_mfa_pending_cookie(response: RedirectResponse, token: str):
    """写入等待二次验证的临时 Cookie。"""
    settings = get_settings()
    response.set_cookie(
        MFA_PENDING_COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.webui_cookie_secure,
        max_age=settings.two_factor_pending_token_expire_minutes * 60,
        samesite="lax",
    )


async def _save_oauth_state(state: str, redirect: str):
    """将 OAuth state 存储到 Redis，失败时回退到内存"""
    try:
        r = await get_async_redis()
        key = f"{_OAUTH_STATE_KEY_PREFIX}{state}"
        await r.setex(key, _OAUTH_STATE_TTL, json.dumps({"redirect": redirect}))
    except Exception as e:
        logger.warning(f"Redis 存储失败，使用内存回退: {e}")
        if len(_oauth_states_fallback) > _MAX_FALLBACK_STATES:
            _cleanup_expired_states()
        if len(_oauth_states_fallback) >= _MAX_FALLBACK_STATES:
            logger.warning("OAuth fallback cache 已满，拒绝新请求")
            raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")
        _oauth_states_fallback[state] = {
            "redirect": redirect,
            "expires": time.time() + _OAUTH_STATE_TTL,
        }


async def _get_oauth_state(state: str):
    """读取 OAuth state（不删除，用于验证阶段）"""
    try:
        r = await get_async_redis()
        key = f"{_OAUTH_STATE_KEY_PREFIX}{state}"
        value = await r.get(key)
        if value:
            return json.loads(value)
    except Exception as e:
        logger.warning(f"Redis 读取失败，尝试内存回退: {e}")
    # Redis 失败或未命中，尝试内存回退
    fallback = _oauth_states_fallback.get(state)
    if fallback and fallback["expires"] > time.time():
        return {"redirect": fallback["redirect"]}
    return None


async def _delete_oauth_state(state: str):
    """删除 OAuth state（登录成功后调用）"""
    try:
        r = await get_async_redis()
        key = f"{_OAUTH_STATE_KEY_PREFIX}{state}"
        await r.delete(key)
    except Exception as e:
        logger.warning(f"Redis 删除失败: {e}")
    _oauth_states_fallback.pop(state, None)


@router.get("/login")
async def login_page(request: Request):
    """渲染登录页面（GitHub OAuth 按钮）"""
    # 已登录则跳转仪表盘
    token = request.cookies.get("webui_token")
    if token and decode_access_token(token):
        return toast_redirect("/", "toast.auto_logged_in", lang=detect_language())

    settings = get_settings()
    has_oauth = bool(settings.github_oauth_client_id)
    telegram_deep_link = _get_telegram_deep_link()

    return render_template(
        "login.html",
        request,
        csrf_token=get_csrf_serializer().dumps({}),
        error=None,
        app_version=APP_VERSION,
        has_oauth=has_oauth,
        telegram_deep_link=telegram_deep_link,
    )


@router.get("/github")
async def github_login(request: Request):
    """GitHub OAuth 第一步：重定向到 GitHub 授权页面"""
    settings = get_settings()

    if not settings.github_oauth_client_id:
        logger.error("GitHub OAuth 未配置：缺少 GITHUB_OAUTH_CLIENT_ID")
        return _oauth_error(
            request,
            "GitHub OAuth 未配置，请联系管理员设置 Client ID",
            has_oauth=False,
            status_code=500,
        )

    if not settings.github_oauth_redirect_uri:
        logger.error("GitHub OAuth 未配置：缺少 GITHUB_OAUTH_REDIRECT_URI")
        return _oauth_error(
            request,
            "GitHub OAuth 未配置，请联系管理员设置回调地址",
            has_oauth=False,
            status_code=500,
        )

    # 生成 state 防止 CSRF
    state = secrets.token_urlsafe(32)
    await _save_oauth_state(state, "/")

    params = {
        "client_id": settings.github_oauth_client_id,
        "redirect_uri": settings.github_oauth_redirect_uri,
        "scope": "read:user",
        "state": state,
    }

    auth_url = f"{settings.github_oauth_auth_url}?{urlencode(params)}"
    logger.info(f"GitHub OAuth: 重定向用户到授权页面, state={state[:8]}...")
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/callback")
async def github_callback(
    request: Request,
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    error_description: str = Query(None),
):
    """GitHub OAuth 第二步：处理授权回调"""

    # 用户拒绝了授权
    if error:
        logger.warning(f"GitHub OAuth 授权被拒绝: {error} - {error_description}")
        return _oauth_error(request, f"授权被拒绝: {error_description or error}")

    # 验证 state（惰性读取，不立即删除 — 登录成功后再删除）
    state_data = await _get_oauth_state(state) if state else None
    if not state_data:
        logger.warning(f"GitHub OAuth state 验证失败: state={state}")
        return _oauth_error(request, "无效的授权请求，请重新登录")

    redirect_target = state_data["redirect"]

    if not code:
        return _oauth_error(request, "未收到授权码")

    settings = get_settings()

    # 用授权码换取 access_token
    try:
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                settings.github_oauth_token_url,
                data={
                    "client_id": settings.github_oauth_client_id,
                    "client_secret": settings.github_oauth_client_secret,
                    "code": code,
                },
                headers={"Accept": "application/json"},
                timeout=10,
            )

        if token_response.status_code != 200:
            logger.error(
                f"GitHub OAuth token 交换失败: status={token_response.status_code}, body={token_response.text}"
            )
            return _oauth_error(request, "获取访问令牌失败，请重试")

        token_data = token_response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            logger.error(f"GitHub OAuth token 响应缺少 access_token: {token_data}")
            return _oauth_error(request, "获取访问令牌失败，请重试")

        # 用 access_token 获取 GitHub 用户信息
        async with httpx.AsyncClient() as client:
            user_response = await client.get(
                settings.github_oauth_user_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                timeout=10,
            )

        if user_response.status_code != 200:
            logger.error(
                f"GitHub OAuth 用户信息获取失败: status={user_response.status_code}"
            )
            return _oauth_error(request, "获取用户信息失败，请重试", status_code=502)

        gh_user = user_response.json()

    except httpx.TimeoutException:
        logger.error("GitHub OAuth 请求超时")
        return _oauth_error(request, "连接 GitHub 超时，请重试", status_code=502)
    except httpx.RequestError as e:
        logger.error(f"GitHub OAuth 网络请求失败: {type(e).__name__}: {e}")
        return _oauth_error(request, "网络连接失败，请重试", status_code=502)
    except Exception:
        logger.exception("GitHub OAuth 未预期的错误")
        return _oauth_error(request, "登录过程中发生错误，请重试", status_code=502)

    github_username = gh_user.get("login")
    github_id = gh_user.get("id")
    avatar_url = gh_user.get("avatar_url", "")

    if not github_username:
        logger.error(f"GitHub OAuth 返回的用户信息缺少 login 字段: {gh_user}")
        return _oauth_error(request, "无法获取 GitHub 用户信息")

    # 通过 github_username 匹配 telegram_users
    async with db_module.async_session() as session:
        result = await session.execute(
            select(TelegramUser).where(
                TelegramUser.github_username == github_username,
                TelegramUser.is_active,
            )
        )
        user = result.scalar_one_or_none()
        has_mfa_method = await user_has_any_mfa_method(session, user) if user else False

    if not user:
        logger.info(f"GitHub OAuth: 用户 {github_username} 未在系统中注册")
        deep_link = _get_telegram_deep_link()
        return render_template(
            "register.html",
            request,
            github_username=github_username,
            deep_link=deep_link,
            app_version=APP_VERSION,
            status_code=403,
        )

    token_data = _build_login_token_payload(
        user, github_username, github_id, avatar_url
    )

    # 登录成功，删除已使用的 state
    await _delete_oauth_state(state)

    if has_mfa_method:
        mfa_token = create_mfa_pending_token(token_data)
        logger.info(f"GitHub OAuth 需要二次验证: {github_username} (role={user.role})")
        response = RedirectResponse(url="/auth/2fa", status_code=302)
        _set_mfa_pending_cookie(response, mfa_token)
        response.delete_cookie("webui_token")
        return response

    jwt_token = create_access_token(token_data)

    logger.info(f"GitHub OAuth 登录成功: {github_username} (role={user.role})")

    response = RedirectResponse(url=redirect_target, status_code=302)
    _set_webui_token_cookie(response, jwt_token)
    return response


@router.get("/2fa")
async def two_factor_page(request: Request):
    """渲染登录二次验证页面。"""
    token = request.cookies.get(MFA_PENDING_COOKIE_NAME)
    payload = decode_access_token(token) if token else None
    if not is_mfa_pending_payload(payload):
        return toast_redirect("/auth/login", "toast.login_required", "error")

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
            return toast_redirect("/auth/login", "toast.login_required", "error")
        totp_enabled = bool(user.totp_enabled)

    return render_template(
        "two_factor_verify.html",
        request,
        csrf_token=get_csrf_serializer().dumps({}),
        error=None,
        app_version=APP_VERSION,
        github_username=payload.get("sub", ""),
        totp_enabled=totp_enabled,
    )


@router.post("/2fa")
@limiter.limit(lambda: get_settings().two_factor_verify_rate_limit)
async def verify_two_factor(
    request: Request,
    code: str = Form(...),
    csrf_token: str = Form(...),
):
    """验证 TOTP 或恢复码并签发正式登录 Cookie。"""
    if not validate_csrf_token(csrf_token):
        raise HTTPException(status_code=403, detail="CSRF 验证失败")

    token = request.cookies.get(MFA_PENDING_COOKIE_NAME)
    payload = decode_access_token(token) if token else None
    if not is_mfa_pending_payload(payload):
        return toast_redirect("/auth/login", "toast.login_required", "error")

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
            return toast_redirect("/auth/login", "toast.login_required", "error")

        # Check lockout before attempting verification
        try:
            await check_mfa_lockout(int(user_id))
        except AccountLockedError as exc:
            lang = detect_language()
            locked_msg = _i18n.t(
                "toast.account_locked", lang=lang, seconds=exc.remaining_seconds
            )
            return render_template(
                "two_factor_verify.html",
                request,
                csrf_token=get_csrf_serializer().dumps({}),
                error=locked_msg,
                app_version=APP_VERSION,
                github_username=payload.get("sub", ""),
                totp_enabled=bool(user.totp_enabled),
                status_code=429,
            )

        if not user.totp_enabled:
            return render_template(
                "two_factor_verify.html",
                request,
                csrf_token=get_csrf_serializer().dumps({}),
                error="请使用通行密钥完成验证",
                app_version=APP_VERSION,
                github_username=payload.get("sub", ""),
                totp_enabled=False,
                status_code=400,
            )

        verified = False
        try:
            used_step = verify_user_totp(user, code)
            if used_step is not None:
                user.totp_last_used_step = used_step
                verified = True
        except TwoFactorReplayError:
            logger.warning("TOTP 重放尝试: user_id={}", user_id)
        except TwoFactorError as exc:
            logger.warning("TOTP 验证失败: user_id={}, error={}", user_id, exc)

        if not verified:
            verified = await consume_recovery_code(session, int(user_id), code)

        if not verified:
            await record_mfa_failure(int(user_id))
            await session.rollback()
            return render_template(
                "two_factor_verify.html",
                request,
                csrf_token=get_csrf_serializer().dumps({}),
                error="验证码或恢复码无效",
                app_version=APP_VERSION,
                github_username=payload.get("sub", ""),
                totp_enabled=True,
                status_code=400,
            )

        await reset_mfa_failures(int(user_id))
        await session.commit()

    jwt_token = create_access_token(
        {
            "sub": payload.get("sub"),
            "role": payload.get("role"),
            "user_id": payload.get("user_id"),
            "github_id": payload.get("github_id"),
            "avatar_url": payload.get("avatar_url"),
        }
    )
    logger.info("WebUI 二次验证成功: user={}", payload.get("sub"))
    response = RedirectResponse(url="/", status_code=302)
    _set_webui_token_cookie(response, jwt_token)
    response.delete_cookie(MFA_PENDING_COOKIE_NAME)
    return response


@router.post("/2fa/passkey/options")
async def two_factor_passkey_options(
    request: Request,
    csrf_token: str = Depends(require_csrf_header),
):
    """创建登录二次验证 Passkey options。"""
    token = request.cookies.get(MFA_PENDING_COOKIE_NAME)
    payload = decode_access_token(token) if token else None
    if not is_mfa_pending_payload(payload):
        return JSONResponse(
            status_code=401,
            content={"success": False, "message": "登录已过期", "data": None},
        )

    async with db_module.async_session() as session:
        try:
            data = await begin_authentication(
                session, int(payload["user_id"]), request_origin(request)
            )
        except WebAuthnError as exc:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": str(exc), "data": None},
            )
    return {"success": True, "message": "ok", "data": data}


@router.post("/2fa/passkey/verify")
@limiter.limit(lambda: get_settings().passkeys_authentication_rate_limit)
async def two_factor_passkey_verify(
    request: Request,
    csrf_token: str = Depends(require_csrf_header),
    body: dict = Body(...),
):
    """验证登录二次验证 Passkey 并签发正式 Cookie。"""
    token = request.cookies.get(MFA_PENDING_COOKIE_NAME)
    payload = decode_access_token(token) if token else None
    if not is_mfa_pending_payload(payload):
        return JSONResponse(
            status_code=401,
            content={"success": False, "message": "登录已过期", "data": None},
        )

    user_id = int(payload["user_id"])

    # Check lockout before attempting
    try:
        await check_mfa_lockout(user_id)
    except AccountLockedError as exc:
        locked_msg = _i18n.t("toast.account_locked", seconds=exc.remaining_seconds)
        return JSONResponse(
            status_code=429,
            content={
                "success": False,
                "message": locked_msg,
                "data": None,
            },
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
    except Exception as exc:
        await record_mfa_failure(user_id)
        logger.warning("Passkey 登录验证失败: user_id={}, error={}", user_id, exc)
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Passkey 验证失败", "data": None},
        )

    await reset_mfa_failures(user_id)

    jwt_token = create_access_token(
        {
            "sub": payload.get("sub"),
            "role": payload.get("role"),
            "user_id": payload.get("user_id"),
            "github_id": payload.get("github_id"),
            "avatar_url": payload.get("avatar_url"),
        }
    )
    response = JSONResponse(
        content={"success": True, "message": "ok", "data": {"redirect": "/"}}
    )
    _set_webui_token_cookie(response, jwt_token)
    response.delete_cookie(MFA_PENDING_COOKIE_NAME)
    return response


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    """登出"""
    if not validate_csrf_token(csrf_token):
        raise HTTPException(status_code=403, detail="CSRF 验证失败")

    logger.info("WebUI 用户登出")
    response = toast_redirect("/auth/login", "toast.logged_out", lang=detect_language())
    response.delete_cookie("webui_token")
    response.delete_cookie(MFA_PENDING_COOKIE_NAME)
    return response


# ========== Passkey 直接登录（无需先 GitHub OAuth）==========


@router.post("/passkey/discover")
@limiter.limit(lambda: get_settings().passkeys_authentication_rate_limit)
async def passkey_discover_options(request: Request):
    """Generate discoverable-credential authentication options.

    This endpoint does NOT require a prior login. The browser will
    present all passkeys matching the RP ID so the user can pick one.

    Security note — CSRF: Like the GitHub OAuth callback and other login
    entry-points, this endpoint operates in an unauthenticated context
    (no session yet).  The issued token is bound to the cookie set in
    ``/passkey/verify-discover``, so a cross-site request cannot steal
    the credential.
    """
    async with db_module.async_session() as session:
        try:
            # user_id=None → no allow_credentials → discoverable flow
            data = await begin_authentication(
                session, user_id=None, request_origin=request_origin(request)
            )
        except WebAuthnError as exc:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": str(exc), "data": None},
            )
    return {"success": True, "message": "ok", "data": data}


@router.post("/passkey/verify-discover")
@limiter.limit(lambda: get_settings().passkeys_authentication_rate_limit)
async def passkey_verify_discover(request: Request, body: dict = Body(...)):
    """Verify a discoverable passkey assertion and issue a login token.

    Security note — CSRF: This is an unauthenticated login endpoint
    (consistent with the GitHub OAuth callback).  It does not rely on a
    CSRF token because there is no established session to protect.
    """
    user_id = 0
    try:
        async with db_module.async_session() as session:
            # expected_user_id=None → accept any user's credential
            db_credential = await finish_authentication(
                session,
                body.get("challenge_id", ""),
                body.get("credential", {}),
                expected_user_id=None,
            )
            # Look up the user who owns this credential
            user_id = db_credential.user_id
            await check_mfa_lockout(user_id)
            result = await session.execute(
                select(TelegramUser).where(
                    TelegramUser.id == user_id,
                    TelegramUser.is_active,
                )
            )
            user = result.scalar_one_or_none()
            if not user:
                return JSONResponse(
                    status_code=403,
                    content={
                        "success": False,
                        "message": "toast.user_not_found",
                        "data": None,
                    },
                )
            # Build the full token payload
            # Note: github_id is not available for passkey-only login; the user can
            # refresh it via a subsequent GitHub OAuth if needed.
            # avatar_url is derived from github_username via GitHub's public avatar
            # endpoint, so the WebUI can display the user's avatar without an extra
            # API call.
            github_username = user.github_username or ""
            avatar_url = (
                f"https://avatars.githubusercontent.com/{github_username}"
                if github_username
                else None
            )
            token_payload = {
                "sub": github_username,
                "role": user.role,
                "user_id": user.id,
                "github_id": None,
                "avatar_url": avatar_url,
            }
            await reset_mfa_failures(user_id)
            await session.commit()
    except AccountLockedError as exc:
        return JSONResponse(
            status_code=429,
            content={
                "success": False,
                "message": _i18n.t(
                    "toast.account_locked", seconds=exc.remaining_seconds
                ),
                "data": None,
            },
        )
    except Exception as exc:
        if user_id:
            await record_mfa_failure(user_id)
        logger.warning("Passkey 直接登录失败: error={}", exc)
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "message": "toast.passkey_login_failed",
                "data": None,
            },
        )

    jwt_token = create_access_token(token_payload)
    logger.info("Passkey 直接登录成功: user={}", token_payload["sub"])
    response = JSONResponse(
        content={"success": True, "message": "ok", "data": {"redirect": "/"}}
    )
    _set_webui_token_cookie(response, jwt_token)
    response.delete_cookie(MFA_PENDING_COOKIE_NAME)
    return response


@router.post("/api/theme")
async def set_theme(
    request: Request,
    theme: str = Form(...),
    x_csrf_token: str = Header("", alias="X-CSRF-Token"),
):
    """HTMX 调用的主题切换接口"""
    if not validate_csrf_token(x_csrf_token):
        raise HTTPException(status_code=403, detail="CSRF 验证失败")

    if theme not in ("light", "dark"):
        return HTMLResponse(status_code=400)
    return HTMLResponse(status_code=204)
