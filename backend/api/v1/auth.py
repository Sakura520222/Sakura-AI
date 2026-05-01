"""API v1 认证端点（含移动端 OAuth）"""

import secrets
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, Query, Request
from loguru import logger
from sqlalchemy import select

from backend.models.telegram_models import TelegramUser
from backend.models import database as db_module
from backend.webui.auth import create_access_token
from backend.core.config import get_settings

from backend.api.v1.deps import require_api_auth, get_api_current_user
from backend.api.v1.schemas import (
    OAuthAuthorizeResponse,
    OAuthCallbackRequest,
    TokenResponse,
    UserInfoResponse,
)
from backend.api.v1.responses import success_response, error_response
from backend.api.v1.deps import limiter

# 复用 WebUI OAuth state 管理
from backend.webui.routes.auth import (
    _save_oauth_state,
    _get_oauth_state,
    _delete_oauth_state,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


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
    await _save_oauth_state(state, "/webui/")

    params = {
        "client_id": settings.github_oauth_client_id,
        "redirect_uri": settings.github_oauth_redirect_uri,
        "scope": "read:user",
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
            for u in settings.mobile_oauth_allowed_redirect_uris.split(",")
            if u.strip()
        ]
        normalized_uri = urlparse(redirect_uri).geturl()
        if normalized_uri not in allowed:
            logger.warning(f"OAuth 白名单拒绝: {redirect_uri[:200]}")
            return error_response("不支持的回调地址", status_code=400)

    state = secrets.token_urlsafe(32)
    # 将 redirect_uri 绑定到 state 中，回调时验证一致性
    await _save_oauth_state(state, uri)

    params = {
        "client_id": settings.github_oauth_client_id,
        "redirect_uri": uri,
        "scope": "read:user",
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

    # 用授权码换取 access_token
    try:
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                settings.github_oauth_token_url,
                data={
                    "client_id": settings.github_oauth_client_id,
                    "client_secret": settings.github_oauth_client_secret,
                    "code": body.code,
                },
                headers={"Accept": "application/json"},
                timeout=10,
            )

        if token_resp.status_code != 200:
            logger.error(f"API OAuth token 交换失败: status={token_resp.status_code}")
            logger.debug(f"OAuth 响应体: {token_resp.text[:500]}")
            return error_response("获取访问令牌失败", status_code=502)

        token_data = token_resp.json()
        github_access_token = token_data.get("access_token")
        if not github_access_token:
            return error_response("获取访问令牌失败", status_code=502)

        # 获取 GitHub 用户信息
        async with httpx.AsyncClient() as client:
            user_resp = await client.get(
                settings.github_oauth_user_url,
                headers={
                    "Authorization": f"Bearer {github_access_token}",
                    "Accept": "application/json",
                },
                timeout=10,
            )

        if user_resp.status_code != 200:
            return error_response("获取用户信息失败", status_code=502)

        gh_user = user_resp.json()

    except httpx.TimeoutException:
        return error_response("连接 GitHub 超时", status_code=504)
    except httpx.RequestError:
        return error_response("网络连接失败", status_code=504)
    except Exception:
        logger.exception("API OAuth 未预期错误")
        return error_response("登录过程中发生错误", status_code=500)

    github_username = gh_user.get("login")
    github_id = gh_user.get("id")
    avatar_url = gh_user.get("avatar_url", "")

    if not github_username:
        return error_response("无法获取 GitHub 用户信息", status_code=502)

    # 匹配系统用户
    async with db_module.async_session() as session:
        result = await session.execute(
            select(TelegramUser).where(
                TelegramUser.github_username == github_username,
                TelegramUser.is_active,
            )
        )
        user = result.scalar_one_or_none()

    if not user:
        await _delete_oauth_state(body.state)
        return error_response(
            f"用户 {github_username} 未在系统中注册，请先通过 Telegram Bot 注册",
            status_code=403,
        )

    # 创建 JWT
    token_payload = {
        "sub": github_username,
        "role": user.role,
        "user_id": user.id,
        "github_id": github_id,
        "avatar_url": avatar_url,
    }
    jwt_token = create_access_token(token_payload)

    # 登录成功，删除已使用的 state
    await _delete_oauth_state(body.state)
    logger.info(f"API OAuth 登录成功: {github_username} (role={user.role})")

    return success_response(
        data=TokenResponse(
            access_token=jwt_token,
            user=UserInfoResponse(
                sub=github_username,
                role=user.role,
                user_id=user.id,
                github_id=github_id,
                avatar_url=avatar_url,
            ),
        ).model_dump(mode="json")
    )


@router.post("/logout")
@limiter.limit("10/minute")
async def logout(request: Request, user: dict = Depends(get_api_current_user)):
    """登出（API 模式下客户端自行删除 token）"""
    logger.info(f"API 用户登出: {user.get('sub')}")
    return success_response(message="已退出登录")


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
        ).model_dump(mode="json")
    )
