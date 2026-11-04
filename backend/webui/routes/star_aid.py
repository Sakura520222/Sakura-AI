"""仓库互助 GitHub 授权协议回调 / Star Aid GitHub authorization callbacks.

仅保留 auth/start、auth/callback 及其状态存储；成员、仓库和管理员业务动作
由 React SPA 与 backend/api/v1/community.py 承接。

授权流安全要求：

- state 必须绑定当前 WebUI user id，不允许跨用户复用。
- callback 不接受未登录请求。
- 授权成功后立即删除 state。
- 若 GitHub 返回的 login 与当前 WebUI 用户绑定的 github_username 不一致，
  拒绝并清除已写入的凭据。
"""

from __future__ import annotations

import json
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from loguru import logger

from backend.core.config import get_settings
from backend.core.redis import get_async_redis
from backend.core.time_service import monotonic
from backend.models import database as db_module
from backend.services import star_aid_github_service as gh_service
from backend.services import star_aid_service
from backend.webui.deps import (
    get_current_user,
    get_user_preferences,
    require_auth,
    toast_redirect,
)
from backend.webui.i18n import SUPPORTED_LANGUAGES, detect_language

router = APIRouter(prefix="/star-aid", tags=["WebUI Star Aid"])

# GitHub App authorize 端点
_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"

# 授权 state 存储前缀与有效期（Redis，失败回退内存）
_AUTH_STATE_KEY_PREFIX = "star_aid:auth_state:"
_AUTH_STATE_TTL = 600  # 10 分钟
_fallback_states: dict[str, dict] = {}
_MAX_FALLBACK_STATES = 1000


def _cleanup_expired_states() -> None:
    now = monotonic()
    for key in [k for k, v in _fallback_states.items() if v.get("expires", 0) <= now]:
        _fallback_states.pop(key, None)


async def _save_auth_state(state: str, payload: dict) -> None:
    try:
        r = await get_async_redis()
        key = f"{_AUTH_STATE_KEY_PREFIX}{state}"
        await r.setex(key, _AUTH_STATE_TTL, json.dumps(payload))
        return
    except Exception as exc:
        logger.warning("star_aid auth state redis save failed, fallback: {}", exc)
    if len(_fallback_states) > _MAX_FALLBACK_STATES:
        _cleanup_expired_states()
        if len(_fallback_states) >= _MAX_FALLBACK_STATES:
            raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")
    payload = {**payload, "expires": monotonic() + _AUTH_STATE_TTL}
    _fallback_states[state] = payload


async def _get_auth_state(state: str) -> dict | None:
    try:
        r = await get_async_redis()
        key = f"{_AUTH_STATE_KEY_PREFIX}{state}"
        value = await r.get(key)
        if value:
            return json.loads(value)
    except Exception as exc:
        logger.warning("star_aid auth state redis read failed, fallback: {}", exc)
    fallback = _fallback_states.get(state)
    if fallback and fallback.get("expires", 0) > monotonic():
        return {k: v for k, v in fallback.items() if k != "expires"}
    return None


async def _delete_auth_state(state: str) -> None:
    try:
        r = await get_async_redis()
        await r.delete(f"{_AUTH_STATE_KEY_PREFIX}{state}")
    except Exception as exc:
        logger.warning("star_aid auth state redis delete failed: {}", exc)
    _fallback_states.pop(state, None)


def _ensure_app_configured() -> tuple[str, str]:
    """返回 (client_id, callback_url)，未配置时抛 HTTPException。"""
    settings = get_settings()
    client_id = settings.star_aid_github_app_client_id
    callback_url = settings.star_aid_github_app_callback_url
    if not client_id or not callback_url:
        logger.error("star_aid GitHub App user-token flow not configured")
        raise HTTPException(
            status_code=503,
            detail="仓库互助 GitHub App 未配置，请联系管理员",
        )
    return client_id, callback_url


def _safe_return_to(return_to: str | None) -> str:
    """只允许同源绝对路径，防止开放重定向。"""
    if not return_to or not return_to.startswith("/") or return_to.startswith("//"):
        return "/star-aid/"
    if "://" in return_to:
        return "/star-aid/"
    return return_to


@router.get("/auth/start")
async def auth_start(
    request: Request,
    intent: str = Query("join"),
    repo_id: int | None = Query(None),
    return_to: str | None = Query(None),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """发起 GitHub App user-to-server 授权。

    生成绑定当前用户的 state，存 Redis，重定向到 GitHub 授权页。
    """
    if intent not in ("join", "manual_star", "github_app"):
        intent = "join"

    client_id, callback_url = _ensure_app_configured()

    state = secrets.token_urlsafe(32)
    payload = {
        "user_id": int(user["user_id"]),
        "github_username": user.get("sub") or "",
        "intent": intent,
        "repo_id": repo_id,
        "return_to": _safe_return_to(return_to),
        "language": (
            user_prefs.get("language")
            if user_prefs.get("language") in SUPPORTED_LANGUAGES
            else "zh-CN"
        ),
    }
    await _save_auth_state(state, payload)

    params = {
        "client_id": client_id,
        "redirect_uri": callback_url,
        "state": state,
    }
    auth_url = f"{_GITHUB_AUTHORIZE_URL}?{urlencode(params)}"
    logger.info("star_aid auth start: user_id={}, intent={}", user["user_id"], intent)
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
    setup_action: str | None = Query(None),
):
    """GitHub App user-to-server 授权回调。

    校验 state 绑定当前登录用户，用 code 换 token，确认 GitHub login 与
    当前用户一致后保存加密凭据，再按 intent 重定向。

    注意：GitHub App 安装/更新权限时会用同一 Callback URL 触发 setup
    回调（带 ``installation_id`` / ``setup_action``；若该 App 启用了
    Request user authorization，还会带 ``code``，但始终无 ``state``）。
    这是 GitHub 发起的安装回调，不是用户授权请求，需直接回仪表盘，
    不要当成 state 不匹配报错，也不要消费该 code。
    """
    lang = detect_language()

    # GitHub App setup callback（安装/更新权限）：
    # 启用 Request user authorization 时会带 code 但一定无 state。
    # 这是 GitHub 发起的安装回调，不是用户授权请求——回仪表盘，
    # 不进仓库互助流程、不消费 code、也不报错。
    if setup_action is not None and not error:
        return RedirectResponse("/", status_code=302)

    # 必须已登录（cookie）
    try:
        user = await get_current_user(request)
    except HTTPException:
        return toast_redirect(
            "/auth/login",
            "toast.login_required",
            toast_type="error",
            lang=lang,
        )

    # 校验 state
    state_data = await _get_auth_state(state) if state else None
    if not state_data or int(state_data.get("user_id", 0)) != int(user["user_id"]):
        logger.warning(
            "star_aid auth state mismatch: state_user={}, cookie_user={}",
            state_data.get("user_id") if state_data else None,
            user["user_id"],
        )
        return toast_redirect(
            "/star-aid/",
            "star_aid.auth_state_invalid",
            toast_type="error",
            lang=lang,
        )

    state_language = state_data.get("language")
    if state_language in SUPPORTED_LANGUAGES:
        lang = state_language

    # 用户拒绝了授权。先完成同一个 state/user 校验，才能使用其中保存的
    # return_to，否则取消授权会把授权中心用户带回仓库互助页。
    if error:
        logger.warning("star_aid auth denied: {} - {}", error, error_description or "")
        if state:
            await _delete_auth_state(state)
        return toast_redirect(
            _safe_return_to(state_data.get("return_to")),
            "star_aid.auth_denied",
            toast_type="error",
            lang=lang,
        )

    if not code:
        return toast_redirect(
            "/star-aid/",
            "star_aid.auth_no_code",
            toast_type="error",
            lang=lang,
        )

    intent = state_data.get("intent", "join")
    repo_id = state_data.get("repo_id")
    return_to = _safe_return_to(state_data.get("return_to"))
    github_username = state_data.get("github_username") or user.get("sub") or ""

    settings = get_settings()
    callback_url = settings.star_aid_github_app_callback_url

    async with db_module.async_session() as session:
        # 用 code 换 token 并写库
        cred = await gh_service.exchange_authorization_code(
            session,
            int(user["user_id"]),
            github_username,
            code,
            redirect_uri=callback_url,
        )
        if cred is None:
            await session.rollback()
            return toast_redirect(
                return_to,
                "star_aid.auth_exchange_failed",
                toast_type="error",
                lang=lang,
            )

        # 用 access token 确认 GitHub 身份与当前用户一致
        access_token = await _decrypt_for_verification(cred)
        gh_user = None
        if access_token:
            gh_user = await gh_service.fetch_authenticated_user(access_token)
        gh_login = (gh_user or {}).get("login", "")

        if not gh_login or gh_login.lower() != (github_username or "").lower():
            # 身份不一致：吊销刚写入的凭据并拒绝
            logger.warning(
                "star_aid auth identity mismatch: expected={}, got={}",
                github_username,
                gh_login or "<none>",
            )
            await gh_service.mark_reauth_required(session, int(user["user_id"]))
            await session.commit()
            return toast_redirect(
                return_to,
                "star_aid.auth_identity_mismatch",
                toast_type="error",
                lang=lang,
            )

        # manual_star 意图：身份确认后立即执行本次 star（功能关闭时不执行）
        star_status = None
        if intent == "manual_star" and repo_id:
            if await star_aid_service.is_feature_enabled():
                star_res = await star_aid_service.perform_star(
                    session,
                    actor_user_id=int(user["user_id"]),
                    repository_id=int(repo_id),
                    trigger="manual",
                    enforce_daily_limit=False,
                )
                star_status = star_res.get("status")
            else:
                star_status = "feature_disabled"
        await session.commit()

    # 授权成功，删除 state
    await _delete_auth_state(state)
    logger.info("star_aid auth success: user_id={}, intent={}", user["user_id"], intent)

    if intent == "manual_star" and repo_id:
        key = {
            "success": "star_aid.manual_star_success",
            "already_done": "star_aid.manual_star_already",
        }.get(star_status, "star_aid.manual_star_failed")
        toast_type = (
            "success" if star_status in ("success", "already_done") else "error"
        )
        return toast_redirect(return_to, key, toast_type=toast_type, lang=lang)

    return toast_redirect(return_to, "star_aid.auth_success", lang=lang)


async def _decrypt_for_verification(cred) -> str | None:
    """仅用于 callback 身份校验阶段解密 access token，失败返回 None。"""
    from backend.services.secret_crypto_service import (
        SecretCryptoError,
        decrypt_secret,
    )

    try:
        return decrypt_secret(cred.encrypted_access_token)
    except SecretCryptoError:
        return None
