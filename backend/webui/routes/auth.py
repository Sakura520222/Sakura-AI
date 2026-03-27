"""WebUI GitHub OAuth 认证路由"""

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

router = APIRouter(prefix="/auth", tags=["WebUI Auth"])
templates = get_templates()

APP_VERSION = "2.4.0"

# OAuth state 存储（生产环境应使用 Redis，此处用内存字典简化）
# 格式: {state: {"redirect": str, "expires": float}}
_oauth_states: dict[str, dict] = {}
_OAUTH_STATE_TTL = 600  # state 有效期 10 分钟


def _cleanup_expired_states():
    """清理过期的 OAuth state"""
    now = time.time()
    expired = [k for k, v in _oauth_states.items() if now > v["expires"]]
    for k in expired:
        del _oauth_states[k]
    if expired:
        logger.debug(f"OAuth state: 清理了 {len(expired)} 个过期 state")


@router.get("/login")
async def login_page(request: Request):
    """渲染登录页面（GitHub OAuth 按钮）"""
    # 已登录则跳转仪表盘
    token = request.cookies.get("webui_token")
    if token:
        if decode_access_token(token):
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
        return templates.TemplateResponse("login.html", {
            "request": request,
            "csrf_token": get_csrf_serializer().dumps({}),
            "error": "GitHub OAuth 未配置，请联系管理员设置 Client ID",
            "app_version": APP_VERSION,
            "has_oauth": False,
        }, status_code=500)

    if not settings.github_oauth_redirect_uri:
        logger.error("GitHub OAuth 未配置：缺少 GITHUB_OAUTH_REDIRECT_URI")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "csrf_token": get_csrf_serializer().dumps({}),
            "error": "GitHub OAuth 未配置，请联系管理员设置回调地址",
            "app_version": APP_VERSION,
            "has_oauth": False,
        }, status_code=500)

    # 生成 state 防止 CSRF
    state = secrets.token_urlsafe(32)
    # 存储到内存（可以带上 referer 以便回调后跳回原页面）
    _cleanup_expired_states()
    _oauth_states[state] = {"redirect": "/webui/", "expires": time.time() + _OAUTH_STATE_TTL}

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
        return templates.TemplateResponse("login.html", {
            "request": request,
            "csrf_token": get_csrf_serializer().dumps({}),
            "error": f"授权被拒绝: {error_description or error}",
            "app_version": APP_VERSION,
            "has_oauth": True,
        })

    # 验证 state
    _cleanup_expired_states()
    state_data = _oauth_states.pop(state, None) if state else None
    if not state_data:
        logger.warning(f"GitHub OAuth state 验证失败: state={state}")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "csrf_token": get_csrf_serializer().dumps({}),
            "error": "无效的授权请求，请重新登录",
            "app_version": APP_VERSION,
            "has_oauth": True,
        }, status_code=400)

    redirect_target = state_data["redirect"]

    if not code:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "csrf_token": get_csrf_serializer().dumps({}),
            "error": "未收到授权码",
            "app_version": APP_VERSION,
            "has_oauth": True,
        }, status_code=400)

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
            return templates.TemplateResponse("login.html", {
                "request": request,
                "csrf_token": get_csrf_serializer().dumps({}),
                "error": "获取访问令牌失败，请重试",
                "app_version": APP_VERSION,
                "has_oauth": True,
            }, status_code=400)

        token_data = token_response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            logger.error(f"GitHub OAuth token 响应缺少 access_token: {token_data}")
            return templates.TemplateResponse("login.html", {
                "request": request,
                "csrf_token": get_csrf_serializer().dumps({}),
                "error": "获取访问令牌失败，请重试",
                "app_version": APP_VERSION,
                "has_oauth": True,
            }, status_code=400)

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
            return templates.TemplateResponse("login.html", {
                "request": request,
                "csrf_token": get_csrf_serializer().dumps({}),
                "error": "获取用户信息失败，请重试",
                "app_version": APP_VERSION,
                "has_oauth": True,
            }, status_code=502)

        gh_user = user_response.json()

    except httpx.TimeoutException:
        logger.error("GitHub OAuth 请求超时")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "csrf_token": get_csrf_serializer().dumps({}),
            "error": "连接 GitHub 超时，请重试",
            "app_version": APP_VERSION,
            "has_oauth": True,
        }, status_code=502)
    except Exception as e:
        logger.error(f"GitHub OAuth 请求失败: {e}")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "csrf_token": get_csrf_serializer().dumps({}),
            "error": "登录过程中发生错误，请重试",
            "app_version": APP_VERSION,
            "has_oauth": True,
        }, status_code=502)

    github_username = gh_user.get("login")
    github_id = gh_user.get("id")
    avatar_url = gh_user.get("avatar_url", "")

    if not github_username:
        logger.error(f"GitHub OAuth 返回的用户信息缺少 login 字段: {gh_user}")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "csrf_token": get_csrf_serializer().dumps({}),
            "error": "无法获取 GitHub 用户信息",
            "app_version": APP_VERSION,
            "has_oauth": True,
        }, status_code=400)

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
        return templates.TemplateResponse("login.html", {
            "request": request,
            "csrf_token": get_csrf_serializer().dumps({}),
            "error": f"用户 @{github_username} 未注册。请先通过 Telegram Bot 注册后再登录。",
            "app_version": APP_VERSION,
            "has_oauth": True,
        }, status_code=403)

    # 创建 JWT
    token_data = {
        "sub": github_username,
        "role": user.role,
        "user_id": user.id,
        "github_id": github_id,
        "avatar_url": avatar_url,
    }
    jwt_token = create_access_token(token_data)

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
