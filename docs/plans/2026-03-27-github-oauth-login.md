# GitHub OAuth 登录 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 WebUI 的简易用户名/密码登录替换为 GitHub OAuth 登录，与现有 `telegram_users` 表打通，通过 `github_username` 匹配用户。

**Architecture:** 新增 GitHub OAuth 流程：登录页点击按钮 → 重定向 GitHub 授权 → 回调获取 access_token → 用 access_token 调 GitHub API 获取用户信息 → 通过 `github_username` 查询 `telegram_users` → 匹配成功创建 JWT 写入 Cookie → 跳转仪表盘。现有 `backend/webui/auth.py` 中的 JWT 工具函数保留不变，`backend/webui/routes/auth.py` 重写 OAuth 路由，`backend/webui/deps.py` 的 `get_current_user` 扩展返回完整用户信息。

**Tech Stack:** FastAPI, httpx（OAuth HTTP 请求）, python-jose（JWT）, SQLAlchemy（用户查询）, Jinja2（模板）

---

### Task 1: 添加 GitHub OAuth 配置项

**Files:**
- Modify: `backend/core/config.py:69-74` (替换旧 WebUI 配置，新增 OAuth 配置)
- Modify: `.env.example` (替换旧 WebUI 环境变量)

**Step 1: 修改 `backend/core/config.py` Settings 类**

将现有的 WebUI 配置（第 69-74 行）替换为：

```python
    # WebUI配置
    webui_secret_key: str = "change-me-in-production"  # JWT 签名密钥
    webui_cookie_secure: bool = False  # Cookie Secure 属性，HTTPS 环境设为 True

    # GitHub OAuth 配置
    github_oauth_client_id: str = ""  # GitHub OAuth App Client ID
    github_oauth_client_secret: str = ""  # GitHub OAuth App Client Secret
    github_oauth_redirect_uri: str = ""  # OAuth 回调地址，如 https://example.com/webui/auth/callback
```

删除这两行（不再需要）：
- `webui_admin_username: str = "admin"`
- `webui_admin_password: str = "admin123"`
- `cors_allowed_origins: list[str] = []` (如果存在)

同时添加一个辅助属性，在 `webhook_url` property 之后：

```python
    @property
    def github_oauth_auth_url(self) -> str:
        """GitHub OAuth 授权 URL"""
        return "https://github.com/login/oauth/authorize"

    @property
    def github_oauth_token_url(self) -> str:
        """GitHub OAuth Token URL"""
        return "https://github.com/login/oauth/access_token"

    @property
    def github_oauth_user_url(self) -> str:
        """GitHub OAuth 用户信息 API"""
        return "https://api.github.com/user"
```

**Step 2: 修改 `.env.example`**

将现有的 WebUI 环境变量替换为：

```
# WebUI配置
WEBUI_SECRET_KEY=your-random-secret-key-change-in-production

# GitHub OAuth 配置
# 需要在 GitHub Settings > Developer settings > OAuth Apps 中创建
GITHUB_OAUTH_CLIENT_ID=your-github-oauth-client-id
GITHUB_OAUTH_CLIENT_SECRET=your-github-oauth-client-secret
GITHUB_OAUTH_REDIRECT_URI=https://your-domain.com/webui/auth/callback
```

删除旧的 `WEBUI_ADMIN_USERNAME` 和 `WEBUI_ADMIN_PASSWORD`。

**Step 3: Commit**

```bash
git add backend/core/config.py .env.example
git commit -m "feat(webui): add GitHub OAuth config, remove admin password config"
```

---

### Task 2: 清理旧的密码认证代码

**Files:**
- Modify: `backend/webui/auth.py` (移除密码相关函数，保留 JWT)

**Step 1: 修改 `backend/webui/auth.py`**

删除所有密码相关代码（`verify_password`、`get_password_hash`、`bcrypt` 导入），保留 JWT 工具函数。文件完整内容改为：

```python
"""WebUI 认证工具（JWT 令牌管理）"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import jwt, JWTError
from loguru import logger

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24


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
```

**Step 2: 从 requirements.txt 移除 passlib（如果存在）**

检查 `requirements.txt` 是否有 `passlib[bcrypt]`，有则删除该行。

**Step 3: Commit**

```bash
git add backend/webui/auth.py requirements.txt
git commit -m "refactor(webui): remove password auth, keep only JWT utilities"
```

---

### Task 3: 扩展 deps.py 的用户信息返回

**Files:**
- Modify: `backend/webui/deps.py:57-77`

**Step 1: 修改 `get_current_user`**

JWT payload 中将存储 `sub`（github_username）、`role`、`user_id`、`github_id`。修改 `get_current_user` 返回更多信息：

```python
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

    return {
        "sub": payload.get("sub"),          # github_username
        "role": payload.get("role", "user"),
        "user_id": payload.get("user_id"),  # telegram_users.id
        "github_id": payload.get("github_id"),  # GitHub numeric ID
    }
```

**Step 2: Commit**

```bash
git add backend/webui/deps.py
git commit -m "refactor(webui): extend get_current_user to return user_id and github_id"
```

---

### Task 4: 重写 auth.py 路由为 GitHub OAuth

**Files:**
- Rewrite: `backend/webui/routes/auth.py`

**Step 1: 重写 `backend/webui/routes/auth.py`**

完整替换为：

```python
"""WebUI GitHub OAuth 认证路由"""

import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Form, HTTPException, Query
from fastapi.responses import RedirectResponse
from loguru import logger
from sqlalchemy import select

from backend.models.telegram_models import TelegramUser
from backend.models.database import async_session
from backend.webui.auth import create_access_token, decode_access_token
from backend.webui.deps import get_templates, validate_csrf_token, get_csrf_serializer
from backend.core.config import get_settings

router = APIRouter(prefix="/auth", tags=["WebUI Auth"])
templates = get_templates()

APP_VERSION = "2.4.0"

# OAuth state 存储（生产环境应使用 Redis，此处用内存字典简化）
_oauth_states: dict[str, str] = {}


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

    # 生成 state 防止 CSRF
    state = secrets.token_urlsafe(32)
    # 存储到内存（可以带上 referer 以便回调后跳回原页面）
    _oauth_states[state] = "/webui/"

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
    if not state or state not in _oauth_states:
        logger.warning(f"GitHub OAuth state 验证失败: state={state}")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "csrf_token": get_csrf_serializer().dumps({}),
            "error": "无效的授权请求，请重新登录",
            "app_version": APP_VERSION,
            "has_oauth": True,
        }, status_code=400)

    # 清理已使用的 state
    redirect_target = _oauth_states.pop(state)

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
            token_data = token_response.json()

        if "error" in token_data:
            logger.error(f"GitHub OAuth token 交换失败: {token_data}")
            return templates.TemplateResponse("login.html", {
                "request": request,
                "csrf_token": get_csrf_serializer().dumps({}),
                "error": "获取访问令牌失败，请重试",
                "app_version": APP_VERSION,
                "has_oauth": True,
            }, status_code=400)

        access_token = token_data["access_token"]

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
    async with async_session() as session:
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
async def set_theme(request: Request, theme: str = Form(...)):
    """HTMX 调用的主题切换接口"""
    if theme not in ("light", "dark"):
        from fastapi.responses import HTMLResponse
        return HTMLResponse(status_code=400)
    from fastapi.responses import HTMLResponse
    return HTMLResponse(status_code=204)
```

**Step 2: Commit**

```bash
git add backend/webui/routes/auth.py
git commit -m "feat(webui): implement GitHub OAuth login flow with telegram_users matching"
```

---

### Task 5: 重写登录页模板为 GitHub OAuth 按钮

**Files:**
- Rewrite: `backend/webui/templates/login.html`

**Step 1: 重写 `backend/webui/templates/login.html`**

完整替换为：

```html
<!DOCTYPE html>
<html lang="zh-CN" class="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登录 - Sakura AI Reviewer</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>tailwind.config = { darkMode: 'class' }</script>
    <script>
        if (localStorage.getItem('theme') === 'dark') document.documentElement.classList.add('dark');
    </script>
</head>
<body class="bg-gray-50 dark:bg-gray-900 flex items-center justify-center min-h-screen">
    <div class="w-full max-w-md px-4">
        <!-- Logo -->
        <div class="text-center mb-8">
            <span class="text-5xl">🌸</span>
            <h1 class="mt-4 text-2xl font-bold text-gray-900 dark:text-white">Sakura AI Reviewer</h1>
            <p class="mt-2 text-gray-500 dark:text-gray-400">GitHub PR 智能审查平台</p>
        </div>

        <!-- 登录卡片 -->
        <div class="bg-white dark:bg-gray-800 rounded-2xl shadow-lg p-8 border border-gray-200 dark:border-gray-700">
            {% if error %}
            <div class="mb-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg text-red-700 dark:text-red-400 text-sm">
                {{ error }}
            </div>
            {% endif %}

            {% if has_oauth %}
            <!-- GitHub OAuth 登录按钮 -->
            <a href="/webui/auth/github"
               class="flex items-center justify-center gap-3 w-full py-3 px-4 bg-gray-900 dark:bg-white hover:bg-gray-800 dark:hover:bg-gray-100 text-white dark:text-gray-900 font-medium rounded-lg transition-all">
                <!-- GitHub Logo SVG -->
                <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24">
                    <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
                </svg>
                使用 GitHub 登录
            </a>

            <div class="mt-6 text-center">
                <p class="text-xs text-gray-400 dark:text-gray-500 leading-relaxed">
                    首次使用需先通过 Telegram Bot 注册，<br>
                    注册时绑定 GitHub 用户名即可登录。
                </p>
            </div>
            {% else %}
            <!-- OAuth 未配置提示 -->
            <div class="text-center py-4">
                <svg class="w-12 h-12 mx-auto text-gray-300 dark:text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z"/>
                </svg>
                <p class="mt-4 text-gray-500 dark:text-gray-400 text-sm">GitHub OAuth 尚未配置</p>
                <p class="mt-2 text-xs text-gray-400 dark:text-gray-500">请联系管理员设置 GITHUB_OAUTH_CLIENT_ID 和 GITHUB_OAUTH_CLIENT_SECRET</p>
            </div>
            {% endif %}
        </div>

        <p class="mt-6 text-center text-xs text-gray-400 dark:text-gray-500">
            Sakura AI Reviewer v{{ app_version }}
        </p>
    </div>
</body>
</html>
```

**Step 2: Commit**

```bash
git add backend/webui/templates/login.html
git commit -m "feat(webui): replace login form with GitHub OAuth button"
```

---

### Task 6: 更新导航栏用户信息显示

**Files:**
- Modify: `backend/webui/templates/components/navbar.html`

**Step 1: 修改 navbar 中的用户显示区域**

在 `navbar.html` 中找到用户信息区域（约第 51-60 行），将用户名显示和头像更新为使用 GitHub 信息。替换整个用户信息 + 登出区域：

```html
        <!-- 用户信息 -->
        <div class="flex items-center gap-2 ml-2">
            {% if current_user.get('avatar_url') %}
            <img src="{{ current_user.avatar_url }}"
                 alt="{{ current_user.sub }}"
                 class="w-8 h-8 rounded-full border-2 border-gray-200 dark:border-gray-600"
                 onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
            <div class="w-8 h-8 bg-gradient-to-br from-pink-400 to-purple-500 rounded-full items-center justify-center text-white text-sm font-medium" style="display:none;">
                {{ current_user.sub[0]|upper }}
            </div>
            {% else %}
            <div class="w-8 h-8 bg-gradient-to-br from-pink-400 to-purple-500 rounded-full flex items-center justify-center text-white text-sm font-medium">
                {{ current_user.sub[0]|upper }}
            </div>
            {% endif %}
            <span class="hidden sm:inline text-sm">{{ current_user.sub }}</span>
        </div>
```

**Step 2: Commit**

```bash
git add backend/webui/templates/components/navbar.html
git commit -m "feat(webui): show GitHub avatar and username in navbar"
```

---

### Task 7: 确保依赖就绪并验证

**Step 1: 确认 httpx 已在 requirements.txt 中**

`httpx==0.27.0` 已存在于 requirements.txt，无需额外安装。

**Step 2: 验证流程**

部署前需要：

1. **在 GitHub 创建 OAuth App**：
   - 进入 GitHub → Settings → Developer settings → OAuth Apps → New OAuth App
   - Application name: `Sakura AI Reviewer`
   - Homepage URL: `https://your-domain.com`
   - Authorization callback URL: `https://your-domain.com/webui/auth/callback`
   - 获取 Client ID 和 Client Secret

2. **配置环境变量**（添加到 `.env`）：
   ```
   GITHUB_OAUTH_CLIENT_ID=your-client-id
   GITHUB_OAUTH_CLIENT_SECRET=your-client-secret
   GITHUB_OAUTH_REDIRECT_URI=https://your-domain.com/webui/auth/callback
   ```

3. **测试流程**：
   - 访问 `/webui/auth/login` → 应看到 GitHub 登录按钮
   - 点击按钮 → 应重定向到 GitHub 授权页面
   - 授权后 → 应回调并检查 `telegram_users` 中是否有匹配的 `github_username`
   - 已注册用户 → 登录成功，跳转仪表盘
   - 未注册用户 → 显示错误提示"请先通过 Telegram Bot 注册"
   - 导航栏 → 应显示 GitHub 头像和用户名

**Step 3: Final commit**

```bash
git add -A
git commit -m "feat(webui): complete GitHub OAuth login implementation"
```
