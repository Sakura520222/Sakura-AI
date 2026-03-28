"""WebUI GitHub OAuth 认证路由"""

import json
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Form, HTTPException, Query, Header
from fastapi.responses import RedirectResponse, HTMLResponse
from loguru import logger
from sqlalchemy import select

from backend.models.telegram_models import TelegramUser
from backend.models import database as db_module
from backend.webui.auth import create_access_token, decode_access_token
from backend.webui.deps import get_templates, validate_csrf_token, get_csrf_serializer
from backend.core.config import get_settings
from backend.core.redis import get_redis

router = APIRouter(prefix="/auth", tags=["WebUI Auth"])
templates = get_templates()

APP_VERSION = "2.4.0"

_OAUTH_STATE_TTL = 600  # state 有效期 10 分钟
_OAUTH_STATE_KEY_PREFIX = "oauth:state:"
_oauth_states_fallback: dict[str, dict] = {}  # Redis 故障时的内存回退
_MAX_FALLBACK_STATES = 1000


def _cleanup_expired_states():
    """清理过期的 OAuth state"""
    now = time.time()
    expired = [s for s, d in _oauth_states_fallback.items() if d.get("expires", 0) <= now]
    for s in expired:
        _oauth_states_fallback.pop(s, None)


def _oauth_error(request: Request, error_msg: str, has_oauth: bool = True, status_code: int = 400):
    """统一的 OAuth 错误页面响应"""
    return templates.TemplateResponse("login.html", {
        "request": request,
        "csrf_token": get_csrf_serializer().dumps({}),
        "error": error_msg,
        "app_version": APP_VERSION,
        "has_oauth": has_oauth,
    }, status_code=status_code)


def _save_oauth_state(state: str, redirect: str):
    """将 OAuth state 存储到 Redis，失败时回退到内存"""
    try:
        r = get_redis()
        key = f"{_OAUTH_STATE_KEY_PREFIX}{state}"
        r.setex(key, _OAUTH_STATE_TTL, json.dumps({"redirect": redirect}))
    except Exception as e:
        logger.warning(f"Redis 存储失败，使用内存回退: {e}")
        if len(_oauth_states_fallback) > _MAX_FALLBACK_STATES:
            _cleanup_expired_states()
        if len(_oauth_states_fallback) >= _MAX_FALLBACK_STATES:
            logger.warning("OAuth fallback cache 已满，清理后仍无空间，清空所有状态")
            _oauth_states_fallback.clear()
        _oauth_states_fallback[state] = {"redirect": redirect, "expires": time.time() + _OAUTH_STATE_TTL}


def _get_oauth_state(state: str):
    """读取 OAuth state（不删除，用于验证阶段）"""
    try:
        r = get_redis()
        key = f"{_OAUTH_STATE_KEY_PREFIX}{state}"
        value = r.get(key)
        if value:
            return json.loads(value)
    except Exception as e:
        logger.warning(f"Redis 读取失败，尝试内存回退: {e}")
    # Redis 失败或未命中，尝试内存回退
    fallback = _oauth_states_fallback.get(state)
    if fallback and fallback["expires"] > time.time():
        return {"redirect": fallback["redirect"]}
    return None


def _delete_oauth_state(state: str):
    """删除 OAuth state（登录成功后调用）"""
    try:
        r = get_redis()
        key = f"{_OAUTH_STATE_KEY_PREFIX}{state}"
        r.delete(key)
    except Exception as e:
        logger.warning(f"Redis 删除失败: {e}")
    _oauth_states_fallback.pop(state, None)


@router.get("/login")
async def login_page(request: Request):
    """渲染登录页面（GitHub OAuth 按钮）"""
    # 已登录则跳转仪表盘
    token = request.cookies.get("webui_token")
    if token and decode_access_token(token):
        return RedirectResponse(url="/webui/", status_code=302)

    settings = get_settings()
    has_oauth = bool(settings.github_oauth_client_id)

    return templates.TemplateResponse("login.html", {
        "request": request,
        "csrf_token": get_csrf_serializer().dumps({}),
        "error": None,
        "app_version": APP_VERSION,
        "has_oauth": has_oauth,
    })


@router.get("/github")
async def github_login(request: Request):
    """GitHub OAuth 第一步：重定向到 GitHub 授权页面"""
    settings = get_settings()

    if not settings.github_oauth_client_id:
        logger.error("GitHub OAuth 未配置：缺少 GITHUB_OAUTH_CLIENT_ID")
        return _oauth_error(request, "GitHub OAuth 未配置，请联系管理员设置 Client ID", has_oauth=False, status_code=500)

    if not settings.github_oauth_redirect_uri:
        logger.error("GitHub OAuth 未配置：缺少 GITHUB_OAUTH_REDIRECT_URI")
        return _oauth_error(request, "GitHub OAuth 未配置，请联系管理员设置回调地址", has_oauth=False, status_code=500)

    # 生成 state 防止 CSRF
    state = secrets.token_urlsafe(32)
    _save_oauth_state(state, "/webui/")

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
    state_data = _get_oauth_state(state) if state else None
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
            logger.error(f"GitHub OAuth token 交换失败: status={token_response.status_code}, body={token_response.text}")
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
            logger.error(f"GitHub OAuth 用户信息获取失败: status={user_response.status_code}")
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
                TelegramUser.is_active == True,
            )
        )
        user = result.scalar_one_or_none()

    if not user:
        logger.info(f"GitHub OAuth: 用户 {github_username} 未在系统中注册")
        return _oauth_error(request, f"用户 @{github_username} 未注册。请先通过 Telegram Bot 注册后再登录。", status_code=403)

    # 创建 JWT
    token_data = {
        "sub": github_username,
        "role": user.role,
        "user_id": user.id,
        "github_id": github_id,
        "avatar_url": avatar_url,
    }
    jwt_token = create_access_token(token_data)

    # 登录成功，删除已使用的 state
    _delete_oauth_state(state)

    logger.info(f"GitHub OAuth 登录成功: {github_username} (role={user.role})")

    response = RedirectResponse(url=redirect_target, status_code=302)
    response.set_cookie(
        "webui_token", jwt_token,
        httponly=True,
        secure=settings.webui_cookie_secure,
        max_age=86400,  # 24小时
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    """登出"""
    if not validate_csrf_token(csrf_token):
        raise HTTPException(status_code=403, detail="CSRF 验证失败")

    logger.info("WebUI 用户登出")
    response = RedirectResponse(url="/webui/auth/login", status_code=302)
    response.delete_cookie("webui_token")
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
