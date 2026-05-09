"""API v1 依赖注入（双模认证）

公共函数（get_db, paginate 等）由各路由文件直接从 backend.webui.deps 导入。
"""

from fastapi import Request, HTTPException, Header
from slowapi import Limiter
from slowapi.util import get_remote_address

from backend.webui.auth import decode_access_token, is_access_token_payload
from backend.webui.deps import user_requires_mfa_enrollment
from backend.models import database as db_module

# 限流器：按客户端 IP 限流（config_filename="" 跳过 .env 读取，避免 Windows GBK 编码问题）
limiter = Limiter(key_func=get_remote_address, config_filename="")


async def get_api_current_user(
    request: Request,
    authorization: str = Header(None, alias="Authorization"),
) -> dict:
    """API 三模认证：优先 Bearer Token，回退 Cookie / 查询参数

    查询参数模式适用于 SSE 等无法设置 Header 的场景（?token=xxx）。

    Returns:
        dict: {"sub": github_username, "role": role, "user_id": id, ...}
    """
    token = None

    # 当通过 Depends() 直接注入时 authorization 是字符串；
    # 当被 require_api_auth 等函数手动调用时它是 Header FieldInfo 对象，
    # 需要从 request.headers 回退提取。
    if authorization is None or not isinstance(authorization, str):
        authorization = request.headers.get("authorization")

    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]

    # 模式 2：Cookie（WebUI 前端 fetch 到 API 的兼容场景）
    if not token:
        token = request.cookies.get("webui_token")

    # 模式 3：查询参数（SSE 等无法设置 Header 的场景）
    # 安全警告：URL 中的 token 会被记录到访问日志、浏览器历史、Referer 头中。
    # 仅限 SSE 等无法设置 Header 的场景使用，建议配置日志中间件过滤 token 参数。
    if not token:
        token = request.query_params.get("token")

    if not token:
        raise HTTPException(status_code=401, detail="未提供认证凭证")

    payload = decode_access_token(token)
    if not is_access_token_payload(payload):
        raise HTTPException(status_code=401, detail="凭证无效或已过期")

    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="无效的登录凭证")

    return {
        "sub": payload.get("sub") or "",
        "role": payload.get("role", "user"),
        "user_id": user_id,
        "github_id": payload.get("github_id"),
        "avatar_url": payload.get("avatar_url"),
    }


async def require_api_auth(request: Request) -> dict:
    """需要登录的 API 路由依赖"""
    user = await get_api_current_user(request)
    async with db_module.async_session() as session:
        if await user_requires_mfa_enrollment(int(user["user_id"]), session):
            raise HTTPException(status_code=428, detail="MFA enrollment required")
    return user


async def require_api_admin(request: Request) -> dict:
    """需要管理员权限的 API 路由依赖"""
    user = await get_api_current_user(request)
    if user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="权限不足")
    return user


async def require_api_super_admin(request: Request) -> dict:
    """需要超级管理员权限的 API 路由依赖"""
    user = await get_api_current_user(request)
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="权限不足")
    return user
