"""WebUI 认证工具（JWT 令牌管理）"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from jose import JWTError, jwt
from loguru import logger

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_MFA_PENDING = "mfa_pending"


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """创建 JWT 访问令牌"""
    return _create_token(data, TOKEN_TYPE_ACCESS, expires_delta)


def create_mfa_pending_token(
    data: dict, expires_delta: timedelta | None = None
) -> str:
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
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    )
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
