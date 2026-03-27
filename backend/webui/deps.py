"""WebUI FastAPI 依赖注入"""

from typing import Optional
from functools import lru_cache

from fastapi import Request, HTTPException, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from itsdangerous import URLSafeTimedSerializer, BadSignature

from sqlalchemy import select

from backend.models import database as db_module
from backend.models.database import WebUIConfig
from backend.webui.auth import decode_access_token


# ========== 模板引擎 ==========
@lru_cache()
def get_templates() -> Jinja2Templates:
    """获取 Jinja2 模板引擎单例"""
    return Jinja2Templates(directory="backend/webui/templates")


# ========== 数据库会话 ==========
async def get_db() -> AsyncSession:
    """获取异步数据库会话"""
    async with db_module.async_session() as session:
        yield session


# ========== CSRF 保护 ==========
_csrf_serializer: Optional[URLSafeTimedSerializer] = None


def get_csrf_serializer() -> URLSafeTimedSerializer:
    """获取 CSRF 序列化器"""
    global _csrf_serializer
    if _csrf_serializer is None:
        from backend.core.config import get_settings
        _settings = get_settings()
        _csrf_serializer = URLSafeTimedSerializer(_settings.webui_secret_key, salt="webui-csrf")
    return _csrf_serializer


def generate_csrf_token() -> str:
    """生成 CSRF Token"""
    return get_csrf_serializer().dumps({})


def validate_csrf_token(token: str) -> bool:
    """验证 CSRF Token（有效期 1 小时）"""
    try:
        get_csrf_serializer().loads(token, max_age=3600)
        return True
    except BadSignature:
        return False


# ========== 认证 ==========
async def get_current_user(request: Request) -> dict:
    """从 Cookie 获取当前登录用户信息

    Returns:
        dict: {"sub": github_username, "role": role, "user_id": id}
    Raises:
        HTTPException: 401 未登录
    """
    token = request.cookies.get("webui_token")
    if not token:
        raise HTTPException(status_code=401, detail="未登录")

    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="登录已过期")

    # 校验必要字段
    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="无效的登录凭证")

    return {
        "sub": payload.get("sub") or "",     # github_username
        "role": payload.get("role", "user"),
        "user_id": user_id,
        "github_id": payload.get("github_id"),
        "avatar_url": payload.get("avatar_url"),
    }


async def require_auth(request: Request) -> dict:
    """需要登录的页面路由依赖"""
    return await get_current_user(request)


async def require_admin(request: Request) -> dict:
    """需要管理员权限的路由依赖"""
    user = await get_current_user(request)
    if user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="权限不足")
    return user


async def require_super_admin(request: Request) -> dict:
    """需要超级管理员权限的路由依赖"""
    user = await get_current_user(request)
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="权限不足")
    return user


# ========== 用户偏好 ==========
async def get_user_preferences(request: Request, db: AsyncSession = Depends(get_db)):
    """获取当前用户的 WebUI 偏好设置，未配置时返回默认值"""
    token = request.cookies.get("webui_token")
    if not token:
        return {"language": "zh-CN", "items_per_page": 20}

    payload = decode_access_token(token)
    user_id = payload.get("user_id") if payload else None
    if not user_id:
        return {"language": "zh-CN", "items_per_page": 20}

    result = await db.execute(
        select(WebUIConfig).where(WebUIConfig.user_id == user_id)
    )
    config = result.scalar_one_or_none()
    if config:
        return {
            "language": config.language or "zh-CN",
            "items_per_page": config.items_per_page or 20,
        }
    return {"language": "zh-CN", "items_per_page": 20}
