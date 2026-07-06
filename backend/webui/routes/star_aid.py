"""仓库互助 WebUI 路由 / Star-aid WebUI routes.

本文件随功能迭代逐步扩展：

- Task 3：GitHub App user-to-server 授权流（auth/start、auth/callback）。
- Task 4+：页面主入口、加入/退出、仓库选择、手动 star、管理员操作等。

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
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.core.redis import get_async_redis
from backend.models import database as db_module
from backend.services import star_aid_github_service as gh_service
from backend.services import star_aid_service
from backend.services import star_aid_summary_service
from backend.webui.deps import (
    get_csrf_serializer,
    get_current_user,
    get_db,
    get_user_preferences,
    render_template,
    require_admin,
    require_auth,
    require_csrf,
    require_super_admin,
    toast_redirect,
)
from backend.webui.i18n import detect_language

router = APIRouter(prefix="/star-aid", tags=["WebUI Star Aid"])

# GitHub App authorize 端点
_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"

# 授权 state 存储前缀与有效期（Redis，失败回退内存）
_AUTH_STATE_KEY_PREFIX = "star_aid:auth_state:"
_AUTH_STATE_TTL = 600  # 10 分钟
_fallback_states: dict[str, dict] = {}
_MAX_FALLBACK_STATES = 1000


def _cleanup_expired_states() -> None:
    now = time.time()
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
    payload = {**payload, "expires": time.time() + _AUTH_STATE_TTL}
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
    if fallback and fallback.get("expires", 0) > time.time():
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
):
    """发起 GitHub App user-to-server 授权。

    生成绑定当前用户的 state，存 Redis，重定向到 GitHub 授权页。
    """
    if intent not in ("join", "manual_star"):
        intent = "join"

    client_id, callback_url = _ensure_app_configured()

    state = secrets.token_urlsafe(32)
    payload = {
        "user_id": int(user["user_id"]),
        "github_username": user.get("sub") or "",
        "intent": intent,
        "repo_id": repo_id,
        "return_to": _safe_return_to(return_to),
    }
    await _save_auth_state(state, payload)

    params = {
        "client_id": client_id,
        "redirect_uri": callback_url,
        "state": state,
    }
    auth_url = f"{_GITHUB_AUTHORIZE_URL}?{urlencode(params)}"
    logger.info(
        "star_aid auth start: user_id={}, intent={}", user["user_id"], intent
    )
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
    回调（带 ``installation_id`` / ``setup_action``，无 ``code`` / ``state``），
    这不是授权请求，需静默回到页面，不要当成 state 不匹配报错。
    """
    lang = detect_language()

    # GitHub App setup callback（安装/更新权限）：不是授权请求，静默回页面
    if setup_action is not None and not code and not error:
        return RedirectResponse("/star-aid/", status_code=302)

    # 用户拒绝了授权
    if error:
        logger.warning("star_aid auth denied: {} - {}", error, error_description or "")
        return toast_redirect(
            "/star-aid/",
            "star_aid.auth_denied",
            toast_type="error",
            lang=lang,
        )

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
    logger.info(
        "star_aid auth success: user_id={}, intent={}", user["user_id"], intent
    )

    if intent == "manual_star" and repo_id:
        key = {
            "success": "star_aid.manual_star_success",
            "already_done": "star_aid.manual_star_already",
        }.get(star_status, "star_aid.manual_star_failed")
        toast_type = "success" if star_status in ("success", "already_done") else "error"
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


# ========== 页面与业务路由（Task 4+）==========


@router.get("/")
async def index(
    request: Request,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    user_prefs: dict = Depends(get_user_preferences),
):
    """仓库互助页面主入口（所有已登录用户可访问）。"""
    state = await star_aid_service.get_page_state(db, user)
    return render_template(
        "star_aid/index.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        active_page="star_aid",
        csrf_token=get_csrf_serializer().dumps({}),
        state=state,
    )


@router.post("/sync", dependencies=[Depends(require_csrf)])
async def sync_repositories(
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """从 GitHub App user token 刷新可展示仓库候选。"""
    result = await star_aid_service.refresh_available_repositories(
        db, int(user["user_id"])
    )
    await db.commit()
    lang = detect_language()
    if result["success"]:
        await _trigger_displayed_summaries(db, int(user["user_id"]))
        return toast_redirect("/star-aid/", "star_aid.sync_success", lang=lang)
    return toast_redirect(
        "/star-aid/", "star_aid.sync_failed", toast_type="error", lang=lang
    )


async def _trigger_displayed_summaries(db: AsyncSession, user_id: int) -> None:
    """对当前用户已展示的仓库异步触发 AI 摘要刷新。"""
    from backend.models.star_aid_models import StarAidRepository

    res = await db.execute(
        select(StarAidRepository.id).where(
            StarAidRepository.owner_user_id == user_id,
            StarAidRepository.is_displayed.is_(True),
        )
    )
    for (repo_id,) in res.all():
        star_aid_summary_service.trigger_summary_refresh(int(repo_id))


@router.post("/repositories/{repo_id}/summary/refresh", dependencies=[Depends(require_csrf)])
async def refresh_summary_route(
    repo_id: int,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """手动刷新单个仓库的 AI 摘要。"""
    result = await star_aid_summary_service.refresh_repository_summary(
        db, int(repo_id), force=True
    )
    await db.commit()
    lang = detect_language()
    status = result.get("status")
    if status == "ready":
        return toast_redirect(
            "/star-aid/", "star_aid.summary_refresh_success", lang=lang
        )
    # 失败：记录并把具体原因带到 toast，便于诊断
    reason = str(result.get("error") or status or "unknown")
    logger.warning(
        "star_aid summary refresh not ready: repo_id={}, status={}, error={}",
        repo_id,
        status,
        result.get("error"),
    )
    return toast_redirect(
        "/star-aid/", f"摘要刷新失败: {reason}", toast_type="error"
    )


@router.post("/join", dependencies=[Depends(require_csrf)])
async def join_plan_route(
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    repo_full_names: list[str] | None = Form(default=None),
):
    """加入互助计划并设置展示仓库。"""
    github_username = user.get("sub") or ""
    result = await star_aid_service.join_plan(
        db, int(user["user_id"]), github_username, repo_full_names or []
    )
    await db.commit()
    lang = detect_language()
    if result["success"]:
        await _trigger_displayed_summaries(db, int(user["user_id"]))
        return toast_redirect("/star-aid/", "star_aid.join_success", lang=lang)
    msg_key = {
        "feature_disabled": "star_aid.feature_disabled",
        "banned": "star_aid.banned",
    }.get(result.get("message"), "star_aid.join_failed")
    return toast_redirect("/star-aid/", msg_key, toast_type="error", lang=lang)


@router.post("/leave", dependencies=[Depends(require_csrf)])
async def leave_plan_route(
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    unstar_created: bool = Form(default=False),
):
    """退出互助计划，可选同时取消本功能此前创建的 star。"""
    result = await star_aid_service.leave_plan(
        db, int(user["user_id"]), unstar_created=unstar_created
    )
    await db.commit()
    lang = detect_language()
    if result.get("success") and unstar_created and result.get("unstar"):
        unstar = result["unstar"]
        if unstar.get("failed", 0) > 0:
            return toast_redirect(
                "/star-aid/", "star_aid.exit_unstar_failed", toast_type="error", lang=lang
            )
        return toast_redirect("/star-aid/", "star_aid.exit_unstar_done", lang=lang)
    return toast_redirect("/star-aid/", "star_aid.leave_success", lang=lang)


@router.post("/repositories", dependencies=[Depends(require_csrf)])
async def update_repositories_route(
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
    repo_full_names: list[str] | None = Form(default=None),
):
    """更新当前用户的展示仓库选择。"""
    await star_aid_service.select_repositories(
        db, int(user["user_id"]), repo_full_names or []
    )
    await db.commit()
    await _trigger_displayed_summaries(db, int(user["user_id"]))
    lang = detect_language()
    return toast_redirect("/star-aid/", "star_aid.selection_updated", lang=lang)


@router.post("/repositories/{repo_id}/star", dependencies=[Depends(require_csrf)])
async def manual_star_route(
    repo_id: int,
    user: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """手动 star 单个仓库（不改变成员状态；未授权时跳转授权）。"""
    lang = detect_language()
    if not await star_aid_service.is_feature_enabled():
        return toast_redirect(
            "/star-aid/", "star_aid.feature_disabled", toast_type="error", lang=lang
        )

    result = await star_aid_service.perform_star(
        db,
        actor_user_id=int(user["user_id"]),
        repository_id=int(repo_id),
        trigger="manual",
        enforce_daily_limit=False,
    )
    await db.commit()

    # 凭据失效：跳转授权，回调后会自动完成本次 star
    if result.get("reauth_required"):
        return RedirectResponse(
            f"/star-aid/auth/start?intent=manual_star&repo_id={repo_id}&return_to=/star-aid/",
            status_code=302,
        )

    status = result.get("status")
    msg_map = {
        "success": ("star_aid.manual_star_success", "success"),
        "already_done": ("star_aid.manual_star_already", "success"),
    }
    key, toast_type = msg_map.get(status, ("star_aid.manual_star_failed", "error"))
    return toast_redirect("/star-aid/", key, toast_type=toast_type, lang=lang)


# ========== 管理员操作（Task 9）==========


@router.post("/admin/members/{member_user_id}/ban", dependencies=[Depends(require_csrf)])
async def ban_member_route(
    member_user_id: int,
    user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    reason: str = Form(default=""),
):
    """管理员封禁成员。"""
    result = await star_aid_service.ban_member(
        db, int(user["user_id"]), int(member_user_id), reason
    )
    await db.commit()
    lang = detect_language()
    if result.get("success"):
        return toast_redirect("/star-aid/", "star_aid.ban_success", lang=lang)
    return toast_redirect(
        "/star-aid/", "star_aid.ban_failed", toast_type="error", lang=lang
    )


@router.post(
    "/admin/members/{member_user_id}/unban",
    dependencies=[Depends(require_csrf), Depends(require_admin)],
)
async def unban_member_route(
    member_user_id: int,
    db: AsyncSession = Depends(get_db),
):
    """管理员解除封禁。"""
    result = await star_aid_service.unban_member(db, int(member_user_id))
    await db.commit()
    lang = detect_language()
    if result.get("success"):
        return toast_redirect("/star-aid/", "star_aid.unban_success", lang=lang)
    return toast_redirect(
        "/star-aid/", "star_aid.unban_failed", toast_type="error", lang=lang
    )


@router.post(
    "/admin/repositories/{repo_id}/disable",
    dependencies=[Depends(require_csrf), Depends(require_admin)],
)
async def disable_repository_route(
    repo_id: int,
    db: AsyncSession = Depends(get_db),
    reason: str = Form(default=""),
):
    """管理员禁用展示仓库。"""
    result = await star_aid_service.set_repository_disabled(
        db, int(repo_id), disabled=True, reason=reason
    )
    await db.commit()
    lang = detect_language()
    if result.get("success"):
        return toast_redirect("/star-aid/", "star_aid.repo_disable_success", lang=lang)
    return toast_redirect(
        "/star-aid/", "star_aid.repo_disable_failed", toast_type="error", lang=lang
    )


@router.post(
    "/admin/repositories/{repo_id}/enable",
    dependencies=[Depends(require_csrf), Depends(require_admin)],
)
async def enable_repository_route(
    repo_id: int,
    db: AsyncSession = Depends(get_db),
):
    """管理员解除仓库禁用。"""
    result = await star_aid_service.set_repository_disabled(
        db, int(repo_id), disabled=False
    )
    await db.commit()
    lang = detect_language()
    if result.get("success"):
        return toast_redirect("/star-aid/", "star_aid.repo_enable_success", lang=lang)
    return toast_redirect(
        "/star-aid/", "star_aid.repo_enable_failed", toast_type="error", lang=lang
    )


@router.post(
    "/admin/feature",
    dependencies=[Depends(require_csrf), Depends(require_super_admin)],
)
async def toggle_feature_route(
    db: AsyncSession = Depends(get_db),
    enabled: bool = Form(default=False),
):
    """超级管理员切换仓库互助全局开关（持久化到 app_config）。"""
    from backend.services.system_config_service import system_config_service

    val = "true" if enabled else "false"
    changed, _ = await system_config_service.save_configs(db, {"star_aid_enabled": val})
    await system_config_service.apply_live_settings(changed)
    lang = detect_language()
    key = "star_aid.feature_now_on" if enabled else "star_aid.feature_now_off"
    return toast_redirect("/star-aid/", key, lang=lang)
