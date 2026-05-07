"""WebUI FastAPI 依赖注入"""

import time
from collections import OrderedDict
from typing import Any, Optional
from functools import lru_cache

from fastapi import Request, HTTPException, Depends, Form, Header
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession
from itsdangerous import URLSafeTimedSerializer, BadSignature

from sqlalchemy import select, or_

from backend.core.config import get_settings
from backend.models import database as db_module
from backend.models.database import PRReview
from backend.models.database import WebUIConfig
from backend.webui.auth import decode_access_token
from backend.services.payment_service import is_payment_enabled
from backend.webui.i18n import make_translation_func, SUPPORTED_LANGUAGES


# ========== 模板引擎 ==========
@lru_cache()
def get_templates() -> Jinja2Templates:
    """获取 Jinja2 模板引擎单例"""
    templates = Jinja2Templates(directory="backend/webui/templates", autoescape=True)
    templates.env.globals["percentage"] = _percentage_filter
    # get_settings() returns the cached singleton updated in place by dynamic config.
    templates.env.globals["settings"] = get_settings()
    templates.env.filters["format_duration"] = _format_duration_filter
    # i18n: 注入默认翻译函数（实际语言由模板上下文中的 _ 覆盖）
    templates.env.globals["_"] = make_translation_func("zh-CN")
    return templates


def _percentage_filter(used, quota) -> int:
    """计算配额使用百分比（0-100）"""
    if quota and quota > 0:
        return min(int((used / quota) * 100), 100)
    return 0


def _format_duration_filter(seconds) -> str:
    """将秒数格式化为可读字符串（Jinja2 过滤器）"""
    if not seconds:
        return "-"
    total_seconds = int(seconds)
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes}m"


def render_template(
    template_name: str,
    request: Request,
    user_prefs: Optional[dict] = None,
    **context: Any,
):
    """渲染模板并自动注入 i18n 翻译函数

    所有路由应使用此函数代替直接调用 templates.TemplateResponse，
    以确保模板中的 _() 函数绑定到正确的语言。

    Args:
        template_name: 模板文件名
        request: FastAPI Request 对象
        user_prefs: 用户偏好（包含 language 字段）
        **context: 传递给模板的额外上下文变量
    """
    from backend.webui.i18n import detect_language

    lang = detect_language(user_prefs)
    context["_"] = make_translation_func(lang)
    context["lang"] = lang
    context["supported_languages"] = SUPPORTED_LANGUAGES
    context["request"] = request
    if user_prefs:
        context["user_prefs"] = user_prefs

    return get_templates().TemplateResponse(template_name, context)


def build_review_search_filter(search: str):
    """构建 PRReview 多字段搜索过滤条件（title/repo_name/repo_owner/author）

    Args:
        search: 搜索关键词，为空时返回 None
    Returns:
        or_() 过滤表达式或 None
    """
    if not search:
        return None
    escaped = search.replace("%", r"\%").replace("_", r"\_")
    return or_(
        PRReview.title.ilike(f"%{escaped}%", escape="\\"),
        PRReview.repo_name.ilike(f"%{escaped}%", escape="\\"),
        PRReview.repo_owner.ilike(f"%{escaped}%", escape="\\"),
        PRReview.author.ilike(f"%{escaped}%", escape="\\"),
    )


def build_user_scope_filter(user: dict, model: type) -> Optional[Any]:
    """构建用户数据范围过滤条件

    普通用户只能看到 repo_owner 或 author 与自己 GitHub 用户名匹配的记录；
    admin/super_admin 可看全部。

    Args:
        user: 当前登录用户信息（含 sub=github_username, role）
        model: ORM 模型类（需有 repo_owner 和 author 属性）
    Returns:
        过滤表达式或 None（管理员时不过滤）
    """
    if user.get("role") in ("admin", "super_admin"):
        return None
    return or_(model.repo_owner == user["sub"], model.author == user["sub"])


async def paginate(
    db: AsyncSession,
    query: Select,
    count_query: Select,
    page: int,
    per_page: int,
) -> tuple[list, int, int, int]:
    """执行分页查询，返回 (items, total, total_pages, page)"""
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    result = await db.execute(query.offset((page - 1) * per_page).limit(per_page))
    return result.scalars().all(), total, total_pages, page


async def require_payment_enabled():
    if not await is_payment_enabled():
        raise HTTPException(status_code=404, detail="付费配额系统未启用")


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
        _csrf_serializer = URLSafeTimedSerializer(
            _settings.webui_secret_key, salt="webui-csrf"
        )
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


async def require_csrf(csrf_token: str = Form(...)) -> str:
    """FastAPI 依赖：验证 CSRF Token，失败时抛出 403"""
    if not validate_csrf_token(csrf_token):
        raise HTTPException(status_code=403, detail="CSRF 验证失败")
    return csrf_token


async def require_csrf_header(
    x_csrf_token: str = Header(..., alias="X-CSRF-Token"),
) -> str:
    """FastAPI 依赖：从 Header 验证 CSRF Token（用于 JSON API）"""
    if not validate_csrf_token(x_csrf_token):
        raise HTTPException(status_code=403, detail="CSRF 验证失败")
    return x_csrf_token


def error_page(
    request: Request,
    status_code: int = 404,
    title: str = "页面未找到",
    message: str = "请求的资源不存在",
    user: dict | None = None,
    user_prefs: dict | None = None,
) -> HTMLResponse:
    """渲染统一的错误页面"""
    from backend.webui.i18n import detect_language

    lang = detect_language(user_prefs)
    return get_templates().TemplateResponse(
        "error.html",
        {
            "request": request,
            "status_code": status_code,
            "title": title,
            "message": message,
            "current_user": user,
            "csrf_token": get_csrf_serializer().dumps({}),
            "user_prefs": user_prefs or {"language": "zh-CN", "items_per_page": 20},
            "_": make_translation_func(lang),
            "lang": lang,
            "supported_languages": SUPPORTED_LANGUAGES,
        },
        status_code=status_code,
    )


def toast_redirect(
    url: str,
    message: str = "toast.success",
    toast_type: str = "success",
    status_code: int = 302,
    lang: str = "",
    **fmt_kwargs: Any,
) -> RedirectResponse:
    """创建带 toast 通知的 redirect 响应

    通过 query params 传递 toast 信息，供前端 JS 拾取并显示。

    Args:
        url: 重定向目标 URL
        message: toast 消息文本，当 lang 非空时视为翻译键
        toast_type: toast 类型（success/error）
        status_code: HTTP 状态码
        lang: 语言代码，非空时将 message 作为翻译键处理
        **fmt_kwargs: 翻译键的格式化参数（如 order_no="123"）
    """
    from urllib.parse import urlencode

    display_message = message
    if lang:
        from backend.webui.i18n import i18n as _i18n

        display_message = _i18n.t(message, lang=lang, **fmt_kwargs)
    elif "." in message and " " not in message:
        # Looks like a translation key but no lang given — auto-translate
        from backend.webui.i18n import i18n as _i18n

        display_message = _i18n.t(message, **fmt_kwargs)

    params = {"_toast": display_message, "_toast_type": toast_type}
    separator = "&" if "?" in url else "?"
    return RedirectResponse(
        url=f"{url}{separator}{urlencode(params)}",
        status_code=status_code,
    )


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
        "sub": payload.get("sub") or "",  # github_username
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
_USER_PREFS_CACHE: OrderedDict[int, tuple[dict, float]] = OrderedDict()
_USER_PREFS_TTL = 300  # 缓存 5 分钟
_MAX_USER_PREFS_CACHE = 1000


async def get_user_preferences(request: Request, db: AsyncSession = Depends(get_db)):
    """获取当前用户的 WebUI 偏好设置，未配置时返回默认值（带内存缓存）"""
    token = request.cookies.get("webui_token")
    if not token:
        return {"language": "zh-CN", "items_per_page": 20}

    payload = decode_access_token(token)
    user_id = payload.get("user_id") if payload else None
    if not user_id:
        return {"language": "zh-CN", "items_per_page": 20}

    # 检查缓存
    cached = _USER_PREFS_CACHE.get(user_id)
    if cached:
        prefs, ts = cached
        if time.time() - ts < _USER_PREFS_TTL:
            _USER_PREFS_CACHE.move_to_end(user_id)
            return prefs

    result = await db.execute(select(WebUIConfig).where(WebUIConfig.user_id == user_id))
    config = result.scalar_one_or_none()
    prefs = (
        {
            "language": config.language or "zh-CN",
            "items_per_page": config.items_per_page or 20,
        }
        if config
        else {"language": "zh-CN", "items_per_page": 20}
    )

    # LRU 淘汰
    if len(_USER_PREFS_CACHE) >= _MAX_USER_PREFS_CACHE:
        _USER_PREFS_CACHE.popitem(last=False)

    _USER_PREFS_CACHE[user_id] = (prefs, time.time())
    return prefs


def invalidate_user_prefs_cache(user_id: int):
    """失效指定用户的偏好设置缓存"""
    _USER_PREFS_CACHE.pop(user_id, None)


# ========== 活跃仓库缓存 ==========
_ACTIVE_REPOS_CACHE: tuple[list[str], float] | None = None
_ACTIVE_REPOS_TTL = 300  # 缓存 5 分钟


async def get_active_repos(db: AsyncSession) -> list[str]:
    """获取活跃仓库名称列表（带内存缓存）"""
    global _ACTIVE_REPOS_CACHE
    if _ACTIVE_REPOS_CACHE:
        repos, ts = _ACTIVE_REPOS_CACHE
        if time.time() - ts < _ACTIVE_REPOS_TTL:
            return repos

    from backend.models.telegram_models import RepoSubscription

    result = await db.execute(
        select(RepoSubscription.repo_name)
        .where(RepoSubscription.is_active)
        .order_by(RepoSubscription.repo_name)
    )
    repos = [r[0] for r in result.all()]
    _ACTIVE_REPOS_CACHE = (repos, time.time())
    return repos
