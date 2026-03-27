"""WebUI 认证路由"""

import asyncio

from fastapi import APIRouter, Request, Form, Depends, HTTPException, Header
from fastapi.responses import RedirectResponse, HTMLResponse
from loguru import logger

from backend.webui.auth import verify_password, get_password_hash, create_access_token
from backend.webui.deps import get_templates, validate_csrf_token, get_csrf_serializer

router = APIRouter(prefix="/auth", tags=["WebUI Auth"])
templates = get_templates()

# 简易管理员账户（后续可从数据库读取）
_ADMIN_CREDENTIALS = {
    "username": None,  # 启动时从配置加载
    "hashed_password": None,
}


async def _get_admin_credentials():
    """惰性加载管理员凭据"""
    if _ADMIN_CREDENTIALS["username"] is None:
        from backend.core.config import get_settings
        settings = get_settings()
        _ADMIN_CREDENTIALS["username"] = settings.webui_admin_username
        # 使用 bcrypt 哈希，在线程中运行避免阻塞事件循环
        _ADMIN_CREDENTIALS["hashed_password"] = await asyncio.to_thread(
            get_password_hash, settings.webui_admin_password
        )
    return _ADMIN_CREDENTIALS


APP_VERSION = "2.4.0"


@router.get("/login")
async def login_page(request: Request):
    """渲染登录页面"""
    # 已登录则跳转仪表盘
    token = request.cookies.get("webui_token")
    if token:
        from backend.webui.auth import decode_access_token
        if decode_access_token(token):
            return RedirectResponse(url="/webui/", status_code=302)

    return templates.TemplateResponse("login.html", {
        "request": request,
        "csrf_token": get_csrf_serializer().dumps({}),
        "error": None,
        "app_version": APP_VERSION,
    })


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    """处理登录"""
    # 验证 CSRF
    if not validate_csrf_token(csrf_token):
        logger.warning("登录 CSRF 验证失败")
        raise HTTPException(status_code=403, detail="CSRF 验证失败")

    creds = await _get_admin_credentials()
    if username != creds["username"] or not await asyncio.to_thread(verify_password, password, creds["hashed_password"]):
        logger.info(f"WebUI 登录失败: username={username}")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "csrf_token": get_csrf_serializer().dumps({}),
            "error": "用户名或密码错误",
            "username": username,
            "app_version": APP_VERSION,
        }, status_code=401)

    # 创建 JWT
    token_data = {"sub": username, "role": "super_admin"}
    access_token = create_access_token(token_data)

    logger.info(f"WebUI 登录成功: {username}")

    from backend.core.config import get_settings
    _settings = get_settings()

    response = RedirectResponse(url="/webui/", status_code=302)
    response.set_cookie(
        "webui_token", access_token,
        httponly=True,
        secure=_settings.webui_cookie_secure,
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
    # 验证 CSRF（前端通过 X-CSRF-Token header 发送）
    if not validate_csrf_token(x_csrf_token):
        raise HTTPException(status_code=403, detail="CSRF 验证失败")

    if theme not in ("light", "dark"):
        return HTMLResponse(status_code=400)
    return HTMLResponse(status_code=204)
