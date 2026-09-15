"""WebUI 认证工具（JWT 令牌管理）"""

from datetime import timedelta
from typing import Literal

from fastapi import Request, Response
from jose import JWTError, jwt
from loguru import logger
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp

from backend.core.time_service import now_utc

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7
ACCESS_TOKEN_EXPIRE_HOURS = ACCESS_TOKEN_EXPIRE_DAYS * 24
WEBUI_TOKEN_COOKIE_NAME = "webui_token"
WEBUI_TOKEN_COOKIE_MAX_AGE = ACCESS_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
WEBUI_TOKEN_RENEWAL_STATE_KEY = "webui_token_renewal"
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_MFA_PENDING = "mfa_pending"


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """创建 JWT 访问令牌"""
    return _create_token(data, TOKEN_TYPE_ACCESS, expires_delta)


def set_webui_token_cookie(response: Response, token: str) -> None:
    """写入正式 WebUI 登录 Cookie。"""
    response.set_cookie(
        WEBUI_TOKEN_COOKIE_NAME,
        token,
        httponly=True,
        secure=True,
        max_age=WEBUI_TOKEN_COOKIE_MAX_AGE,
        samesite="lax",
    )


def queue_webui_token_renewal(request: Request, payload: dict) -> None:
    """将重新签发的 WebUI Cookie 排队到当前请求状态。"""
    setattr(
        request.state,
        WEBUI_TOKEN_RENEWAL_STATE_KEY,
        create_access_token(payload),
    )


class WebUITokenRenewalMiddleware:
    """将认证依赖排队的 WebUI Cookie 添加到最终响应。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_renewal(message):
            if message["type"] == "http.response.start":
                state = scope.get("state", {})
                renewal_token = state.get(WEBUI_TOKEN_RENEWAL_STATE_KEY)
                has_webui_cookie = any(
                    name.lower() == b"set-cookie"
                    and value.lower().startswith(b"webui_token=")
                    for name, value in message.get("headers", [])
                )
                if renewal_token and not has_webui_cookie:
                    renewal_response = Response()
                    set_webui_token_cookie(renewal_response, renewal_token)
                    set_cookie = renewal_response.headers.get("set-cookie")
                    if set_cookie:
                        MutableHeaders(scope=message).append(
                            "set-cookie", set_cookie
                        )
            await send(message)

        await self.app(scope, receive, send_with_renewal)


def create_mfa_pending_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """创建等待二次验证的短期 JWT 令牌"""
    from backend.core.config import get_settings

    settings = get_settings()
    return _create_token(
        data,
        TOKEN_TYPE_MFA_PENDING,
        expires_delta
        or timedelta(minutes=settings.two_factor_pending_token_expire_minutes),
    )


def _create_token(
    data: dict,
    token_type: Literal["access", "mfa_pending"],
    expires_delta: timedelta | None = None,
) -> str:
    """创建指定类型的 JWT 令牌"""
    from backend.core.config import get_settings

    _settings = get_settings()

    to_encode = data.copy()
    expire = now_utc() + (expires_delta or timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS))
    to_encode.update({"exp": expire, "token_type": token_type})
    return jwt.encode(to_encode, _settings.webui_secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """解码 JWT 令牌，失败返回 None"""
    from backend.core.config import get_settings

    _settings = get_settings()

    try:
        payload = jwt.decode(token, _settings.webui_secret_key, algorithms=[ALGORITHM])
        return payload
    except JWTError as e:
        logger.debug(f"JWT 解码失败: {e}")
        return None


def is_access_token_payload(payload: dict | None) -> bool:
    """判断 payload 是否为正式访问令牌。"""
    return (
        bool(payload)
        and payload.get("token_type", TOKEN_TYPE_ACCESS) == TOKEN_TYPE_ACCESS
    )


def is_mfa_pending_payload(payload: dict | None) -> bool:
    """判断 payload 是否为等待二次验证的临时令牌。"""
    return bool(payload) and payload.get("token_type") == TOKEN_TYPE_MFA_PENDING
