"""WebUI 认证工具"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import jwt, JWTError
from loguru import logger
import bcrypt

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8"),
    )


def get_password_hash(password: str) -> str:
    """生成密码哈希"""
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建 JWT 访问令牌"""
    from backend.core.config import get_settings
    _settings = get_settings()

    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, _settings.webui_secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """解码 JWT 令牌，失败返回 None"""
    from backend.core.config import get_settings
    _settings = get_settings()

    try:
        payload = jwt.decode(token, _settings.webui_secret_key, algorithms=[ALGORITHM])
        return payload
    except JWTError as e:
        logger.debug(f"JWT 解码失败: {e}")
        return None
