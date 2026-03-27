# WebUI P0 实现计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为 Sakura AI Reviewer 添加基于 Jinja2 + HTMX + Tailwind CSS 的 WebUI，实现仪表盘和 PR 审查管理功能。

**Architecture:** 在现有 FastAPI 应用上新增 `/webui` 路由组，使用 Jinja2 渲染 HTML 模板，HTMX 实现动态交互（搜索、过滤、分页），Tailwind CSS（CDN）处理样式，Alpine.js 处理主题切换等轻量客户端逻辑。认证采用简易用户名/密码 + JWT，预置管理员账户。

**Tech Stack:** FastAPI, Jinja2, HTMX 2.0, Tailwind CSS (CDN), Alpine.js, python-jose (JWT), itsdangerous (CSRF)

---

### Task 1: 添加依赖并搭建项目结构

**Files:**
- Modify: `requirements.txt`
- Create: `backend/webui/__init__.py`
- Create: `backend/webui/auth.py`
- Create: `backend/webui/deps.py`
- Create: `backend/webui/routes/__init__.py`
- Create: `backend/webui/templates/base.html`
- Create: `backend/webui/templates/components/`
- Create: `backend/webui/static/`

**Step 1: 添加 Python 依赖**

在 `requirements.txt` 末尾添加：

```
# WebUI
jinja2>=3.1.2
python-jose[cryptography]>=3.3.0
itsdangerous>=2.1.0
```

**Step 2: 创建目录结构**

```bash
mkdir -p backend/webui/routes backend/webui/templates/components backend/webui/static
touch backend/webui/__init__.py backend/webui/routes/__init__.py
```

**Step 3: 验证目录结构**

Run: `ls -la backend/webui/`
Expected: 看到 `__init__.py`, `routes/`, `templates/`, `static/`

**Step 4: Commit**

```bash
git add requirements.txt backend/webui/__init__.py backend/webui/routes/__init__.py
git commit -m "chore(webui): add dependencies and project structure scaffold"
```

---

### Task 2: 添加配置项和数据库模型扩展

**Files:**
- Modify: `backend/core/config.py` (新增 WebUI 相关配置项)
- Modify: `backend/models/database.py` (新增 WebUIConfig 模型)
- Modify: `.env.example` (新增 WebUI 环境变量示例)

**Step 1: 在 `backend/core/config.py` 的 Settings 类中添加 WebUI 配置**

在 `Settings` 类中 `telegram_bot_token` 之前添加：

```python
    # WebUI配置
    webui_secret_key: str = "change-me-in-production"  # JWT 签名密钥
    webui_admin_username: str = "admin"  # WebUI 管理员用户名
    webui_admin_password: str = "admin123"  # WebUI 管理员初始密码（生产环境必须修改）
```

**Step 2: 在 `backend/models/database.py` 末尾添加 WebUIConfig 模型**

在文件末尾 `close_async_db()` 函数之后添加：

```python
class WebUIConfig(Base):
    """用户 WebUI 偏好设置"""

    __tablename__ = "webui_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, unique=True, nullable=False)
    theme = Column(String(10), default="light")  # light / dark
    language = Column(String(10), default="zh-CN")
    items_per_page = Column(Integer, default=20)

    created_at = Column(TIMESTAMP, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return f"<WebUIConfig(user_id={self.user_id}, theme={self.theme})>"
```

**Step 3: 在 `.env.example` 末尾添加 WebUI 环境变量**

```
# WebUI配置
WEBUI_SECRET_KEY=your-random-secret-key-change-in-production
WEBUI_ADMIN_USERNAME=admin
WEBUI_ADMIN_PASSWORD=admin123
```

**Step 4: Commit**

```bash
git add backend/core/config.py backend/models/database.py .env.example
git commit -m "feat(webui): add config settings and WebUIConfig model"
```

---

### Task 3: 实现认证系统

**Files:**
- Create: `backend/webui/auth.py` (JWT 令牌管理 + 密码验证)
- Create: `backend/webui/deps.py` (FastAPI 依赖注入：当前用户、权限检查)

**Step 1: 创建 `backend/webui/auth.py`**

```python
"""WebUI 认证工具"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import jwt, JWTError
from passlib.context import CryptContext
from loguru import logger

from backend.core.config import get_settings

settings = get_settings()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """生成密码哈希"""
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建 JWT 访问令牌"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.webui_secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """解码 JWT 令牌，失败返回 None"""
    try:
        payload = jwt.decode(token, settings.webui_secret_key, algorithms=[ALGORITHM])
        return payload
    except JWTError as e:
        logger.debug(f"JWT 解码失败: {e}")
        return None
```

**Step 2: 创建 `backend/webui/deps.py`**

```python
"""WebUI FastAPI 依赖注入"""

from typing import Optional
from functools import lru_cache

from fastapi import Request, HTTPException, Depends, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from itsdangerous import URLSafeTimedSerializer, BadSignature

from backend.models.database import async_session
from backend.webui.auth import decode_access_token, verify_password
from backend.core.config import get_settings

settings = get_settings()


# ========== 模板引擎 ==========
@lru_cache()
def get_templates() -> Jinja2Templates:
    """获取 Jinja2 模板引擎单例"""
    return Jinja2Templates(directory="backend/webui/templates")


# ========== 数据库会话 ==========
async def get_db() -> AsyncSession:
    """获取异步数据库会话"""
    async with async_session() as session:
        yield session


# ========== CSRF 保护 ==========
_csrf_serializer: Optional[URLSafeTimedSerializer] = None


def get_csrf_serializer() -> URLSafeTimedSerializer:
    """获取 CSRF 序列化器"""
    global _csrf_serializer
    if _csrf_serializer is None:
        _csrf_serializer = URLSafeTimedSerializer(settings.webui_secret_key, salt="webui-csrf")
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
        dict: {"sub": "admin", "role": "super_admin"}
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
        "sub": payload.get("sub"),
        "role": payload.get("role", "user"),
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
```

**Step 3: 添加 passlib 依赖**

在 `requirements.txt` 中 WebUI 部分添加：

```
passlib[bcrypt]>=1.7.4
```

**Step 4: Commit**

```bash
git add requirements.txt backend/webui/auth.py backend/webui/deps.py
git commit -m "feat(webui): implement JWT auth and dependency injection"
```

---

### Task 4: 实现基础布局模板

**Files:**
- Create: `backend/webui/templates/base.html` (基础布局，含导航栏、侧边栏、主题切换)
- Create: `backend/webui/templates/login.html` (登录页)
- Create: `backend/webui/templates/components/navbar.html` (导航栏)
- Create: `backend/webui/templates/components/sidebar.html` (侧边栏)

**Step 1: 创建 `backend/webui/templates/base.html`**

```html
<!DOCTYPE html>
<html lang="zh-CN" class="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}Sakura AI Reviewer{% endblock %}</title>
    <!-- Tailwind CSS CDN -->
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
        }
    </script>
    <!-- HTMX -->
    <script src="https://unpkg.com/htmx.org@2.0.4"></script>
    <!-- Alpine.js -->
    <script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js"></script>
    <style>
        [x-cloak] { display: none !important; }
        /* 自定义滚动条 */
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }
        .dark ::-webkit-scrollbar-thumb { background: #475569; }
    </style>
    {% block extra_head %}{% endblock %}
</head>
<body class="bg-gray-50 dark:bg-gray-900 text-gray-900 dark:text-gray-100 min-h-screen"
      x-data="{ theme: localStorage.getItem('theme') || 'light' }"
      x-init="$watch('theme', val => {
          document.documentElement.classList.toggle('dark', val === 'dark');
          localStorage.setItem('theme', val);
          fetch('/api/webui/theme', {
              method: 'POST',
              headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRF-Token': '{{ csrf_token }}' },
              body: 'theme=' + val
          }).catch(() => {});
      }); document.documentElement.classList.toggle('dark', theme === 'dark');">

    <!-- 导航栏 -->
    {% include "components/navbar.html" %}

    <!-- 主内容区域 -->
    <div class="flex">
        <!-- 侧边栏 -->
        {% include "components/sidebar.html" %}

        <!-- 页面内容 -->
        <main class="flex-1 ml-0 lg:ml-64 p-6 min-h-[calc(100vh-4rem)]">
            {% block content %}{% endblock %}
        </main>
    </div>

    {% block extra_scripts %}{% endblock %}
</body>
</html>
```

**Step 2: 创建 `backend/webui/templates/components/navbar.html`**

```html
<!-- 顶部导航栏 -->
<nav class="fixed top-0 left-0 right-0 z-50 h-16 bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 px-4 lg:px-6 flex items-center justify-between shadow-sm">
    <!-- 左侧：Logo + 标题 -->
    <div class="flex items-center gap-3">
        <!-- 移动端菜单按钮 -->
        <button id="sidebar-toggle" class="lg:hidden p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"/>
            </svg>
        </button>
        <a href="/webui/" class="flex items-center gap-2">
            <span class="text-xl">🌸</span>
            <span class="font-semibold text-lg hidden sm:inline">Sakura AI Reviewer</span>
        </a>
    </div>

    <!-- 右侧：主题切换 + 用户信息 -->
    <div class="flex items-center gap-2">
        <!-- 主题切换按钮 -->
        <button @click="theme = theme === 'light' ? 'dark' : 'light'"
                class="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                :title="theme === 'light' ? '切换到暗色模式' : '切换到亮色模式'">
            <!-- 太阳图标（亮色模式显示） -->
            <svg x-show="theme === 'light'" class="w-5 h-5 text-yellow-500" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 2a1 1 0 011 1v1a1 1 0 11-2 0V3a1 1 0 011-1zm4 8a4 4 0 11-8 0 4 4 0 018 0zm-.464 4.95l.707.707a1 1 0 001.414-1.414l-.707-.707a1 1 0 00-1.414 1.414zm2.12-10.607a1 1 0 010 1.414l-.706.707a1 1 0 11-1.414-1.414l.707-.707a1 1 0 011.414 0zM17 11a1 1 0 100-2h-1a1 1 0 100 2h1zm-7 4a1 1 0 011 1v1a1 1 0 11-2 0v-1a1 1 0 011-1zM5.05 6.464A1 1 0 106.465 5.05l-.708-.707a1 1 0 00-1.414 1.414l.707.707zm1.414 8.486l-.707.707a1 1 0 01-1.414-1.414l.707-.707a1 1 0 011.414 1.414zM4 11a1 1 0 100-2H3a1 1 0 000 2h1z" clip-rule="evenodd"/>
            </svg>
            <!-- 月亮图标（暗色模式显示） -->
            <svg x-show="theme === 'dark'" x-cloak class="w-5 h-5 text-blue-400" fill="currentColor" viewBox="0 0 20 20">
                <path d="M17.293 13.293A8 8 0 016.707 2.707a8.001 8.001 0 1010.586 10.586z"/>
            </svg>
        </button>

        <!-- 用户信息 -->
        <div class="flex items-center gap-2 ml-2">
            <div class="w-8 h-8 bg-gradient-to-br from-pink-400 to-purple-500 rounded-full flex items-center justify-center text-white text-sm font-medium">
                {{ current_user.sub[0]|upper }}
            </div>
            <span class="hidden sm:inline text-sm">{{ current_user.sub }}</span>
        </div>

        <!-- 登出 -->
        <form method="POST" action="/webui/auth/logout" class="ml-1">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <button type="submit" class="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors" title="退出登录">
                <svg class="w-5 h-5 text-gray-500 dark:text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/>
                </svg>
            </button>
        </form>
    </div>
</nav>
```

**Step 3: 创建 `backend/webui/templates/components/sidebar.html`**

```html
<!-- 侧边栏 -->
<aside id="sidebar"
       class="fixed top-16 left-0 bottom-0 w-64 bg-white dark:bg-gray-800 border-r border-gray-200 dark:border-gray-700 z-40 transform -translate-x-full lg:translate-x-0 transition-transform duration-200">
    <nav class="p-4 space-y-1">
        <a href="/webui/"
           class="sidebar-link flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors
                  {% if active_page == 'dashboard' %}bg-pink-50 dark:bg-pink-900/20 text-pink-700 dark:text-pink-400{% else %}text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700{% endif %}">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6"/>
            </svg>
            仪表盘
        </a>

        <a href="/webui/pr/"
           class="sidebar-link flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors
                  {% if active_page == 'pr' %}bg-pink-50 dark:bg-pink-900/20 text-pink-700 dark:text-pink-400{% else %}text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700{% endif %}">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z"/>
            </svg>
            PR 审查
        </a>

        <!-- 分隔线 -->
        <div class="border-t border-gray-200 dark:border-gray-700 my-3"></div>

        <!-- 状态指示 -->
        <div class="px-3 py-2">
            <div class="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                <span class="w-2 h-2 bg-green-500 rounded-full animate-pulse"></span>
                服务运行中
            </div>
        </div>
    </nav>
</aside>

<!-- 移动端遮罩 -->
<div id="sidebar-overlay" class="fixed inset-0 bg-black/50 z-30 hidden lg:hidden" onclick="document.getElementById('sidebar').classList.toggle('-translate-x-full'); this.classList.add('hidden');"></div>

<script>
// 移动端侧边栏切换
document.getElementById('sidebar-toggle')?.addEventListener('click', () => {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    sidebar.classList.toggle('-translate-x-full');
    overlay.classList.toggle('hidden');
});
</script>
```

**Step 4: 创建 `backend/webui/templates/login.html`**

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

        <!-- 登录表单 -->
        <div class="bg-white dark:bg-gray-800 rounded-2xl shadow-lg p-8 border border-gray-200 dark:border-gray-700">
            {% if error %}
            <div class="mb-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg text-red-700 dark:text-red-400 text-sm">
                {{ error }}
            </div>
            {% endif %}

            <form method="POST" action="/webui/auth/login">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">

                <div class="mb-5">
                    <label for="username" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">用户名</label>
                    <input type="text" id="username" name="username" required
                           class="w-full px-4 py-2.5 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-2 focus:ring-pink-500 focus:border-transparent outline-none transition-all"
                           placeholder="请输入用户名"
                           value="{{ username | default('') }}">
                </div>

                <div class="mb-6">
                    <label for="password" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">密码</label>
                    <input type="password" id="password" name="password" required
                           class="w-full px-4 py-2.5 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-2 focus:ring-pink-500 focus:border-transparent outline-none transition-all"
                           placeholder="请输入密码">
                </div>

                <button type="submit"
                        class="w-full py-2.5 bg-gradient-to-r from-pink-500 to-purple-500 text-white font-medium rounded-lg hover:from-pink-600 hover:to-purple-600 focus:ring-4 focus:ring-pink-200 dark:focus:ring-pink-800 transition-all">
                    登 录
                </button>
            </form>
        </div>

        <p class="mt-6 text-center text-xs text-gray-400 dark:text-gray-500">
            Sakura AI Reviewer v2.4.0
        </p>
    </div>
</body>
</html>
```

**Step 5: Commit**

```bash
git add backend/webui/templates/
git commit -m "feat(webui): add base layout, navbar, sidebar, and login page templates"
```

---

### Task 5: 实现认证路由

**Files:**
- Create: `backend/webui/routes/auth.py` (登录/登出/主题切换)

**Step 1: 创建 `backend/webui/routes/auth.py`**

```python
"""WebUI 认证路由"""

from fastapi import APIRouter, Request, Form, Depends, HTTPException
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


def _get_admin_credentials():
    """惰性加载管理员凭据"""
    from backend.core.config import get_settings
    if _ADMIN_CREDENTIALS["username"] is None:
        settings = get_settings()
        _ADMIN_CREDENTIALS["username"] = settings.webui_admin_username
        # 使用 bcrypt 哈希，确保固定输入产生固定输出
        _ADMIN_CREDENTIALS["hashed_password"] = get_password_hash(settings.webui_admin_password)
    return _ADMIN_CREDENTIALS


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

    creds = _get_admin_credentials()
    if username != creds["username"] or not verify_password(password, creds["hashed_password"]):
        logger.info(f"WebUI 登录失败: username={username}")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "csrf_token": get_csrf_serializer().dumps({}),
            "error": "用户名或密码错误",
            "username": username,
        }, status_code=401)

    # 创建 JWT
    token_data = {"sub": username, "role": "super_admin"}
    access_token = create_access_token(token_data)

    logger.info(f"WebUI 登录成功: {username}")

    response = RedirectResponse(url="/webui/", status_code=302)
    response.set_cookie(
        "webui_token", access_token,
        httponly=True,
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


@router.post("/api/webui/theme")
async def set_theme(request: Request, theme: str = Form(...), csrf_token: str = Form("")):
    """HTMX 调用的主题切换接口（无需返回内容）"""
    # 简单验证
    if theme not in ("light", "dark"):
        return HTMLResponse(status_code=400)
    return HTMLResponse(status_code=204)
```

**Step 2: Commit**

```bash
git add backend/webui/routes/auth.py
git commit -m "feat(webui): implement login/logout routes with JWT auth"
```

---

### Task 6: 实现仪表盘 API 和页面

**Files:**
- Create: `backend/webui/routes/dashboard.py` (仪表盘页面 + 统计 API)
- Create: `backend/webui/templates/dashboard.html` (仪表盘模板)
- Create: `backend/webui/templates/components/stats_cards.html` (统计卡片)
- Create: `backend/webui/templates/components/recent_reviews.html` (最近审查列表)

**Step 1: 创建 `backend/webui/routes/dashboard.py`**

```python
"""WebUI 仪表盘路由"""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import PRReview, ReviewComment
from backend.webui.deps import require_auth, get_db, get_templates, get_csrf_serializer

router = APIRouter(tags=["WebUI Dashboard"])
templates = get_templates()


@router.get("/")
async def dashboard_page(
    request: Request,
    user: dict = Depends(require_auth),
):
    """渲染仪表盘页面"""
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "dashboard",
    })


@router.get("/api/webui/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """获取仪表盘统计数据"""
    # 总审查数
    total = await db.execute(select(func.count(PRReview.id)))
    total_count = total.scalar() or 0

    # 已完成数
    completed = await db.execute(
        select(func.count(PRReview.id)).where(PRReview.status == "completed")
    )
    completed_count = completed.scalar() or 0

    # 审查中
    reviewing = await db.execute(
        select(func.count(PRReview.id)).where(PRReview.status == "reviewing")
    )
    reviewing_count = reviewing.scalar() or 0

    # 失败
    failed = await db.execute(
        select(func.count(PRReview.id)).where(PRReview.status == "failed")
    )
    failed_count = failed.scalar() or 0

    # 通过（decision = approve）
    approved = await db.execute(
        select(func.count(PRReview.id)).where(
            PRReview.status == "completed",
            PRReview.decision == "approve",
        )
    )
    approved_count = approved.scalar() or 0

    # 需修改（decision = request_changes）
    changes_requested = await db.execute(
        select(func.count(PRReview.id)).where(
            PRReview.status == "completed",
            PRReview.decision == "request_changes",
        )
    )
    changes_count = changes_requested.scalar() or 0

    # 平均评分
    avg_score_result = await db.execute(
        select(func.avg(PRReview.overall_score)).where(PRReview.status == "completed")
    )
    avg_score = avg_score_result.scalar()
    avg_score = round(avg_score, 1) if avg_score else 0

    # 评论总数
    comment_count_result = await db.execute(select(func.count(ReviewComment.id)))
    comment_count = comment_count_result.scalar() or 0

    return {
        "total": total_count,
        "completed": completed_count,
        "reviewing": reviewing_count,
        "failed": failed_count,
        "approved": approved_count,
        "changes_requested": changes_count,
        "avg_score": avg_score,
        "comment_count": comment_count,
    }


@router.get("/api/webui/recent-reviews")
async def get_recent_reviews(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """获取最近审查列表（最近 10 条）"""
    result = await db.execute(
        select(PRReview)
        .order_by(desc(PRReview.created_at))
        .limit(10)
    )
    reviews = result.scalars().all()
    return [
        {
            "id": r.id,
            "pr_id": r.pr_id,
            "repo_name": r.repo_name,
            "repo_owner": r.repo_owner,
            "title": r.title,
            "author": r.author,
            "status": r.status,
            "overall_score": r.overall_score,
            "decision": r.decision,
            "strategy": r.strategy,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in reviews
    ]
```

**Step 2: 创建 `backend/webui/templates/components/stats_cards.html`**

```html
<!-- 统计卡片 -->
<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4" id="stats-cards">
    <!-- 总审查数 -->
    <div class="bg-white dark:bg-gray-800 rounded-xl p-5 border border-gray-200 dark:border-gray-700 shadow-sm">
        <div class="flex items-center justify-between">
            <div>
                <p class="text-sm text-gray-500 dark:text-gray-400">总审查数</p>
                <p class="text-2xl font-bold mt-1" id="stat-total">-</p>
            </div>
            <div class="w-10 h-10 bg-blue-100 dark:bg-blue-900/30 rounded-lg flex items-center justify-center">
                <svg class="w-5 h-5 text-blue-600 dark:text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                </svg>
            </div>
        </div>
    </div>

    <!-- 已通过 -->
    <div class="bg-white dark:bg-gray-800 rounded-xl p-5 border border-gray-200 dark:border-gray-700 shadow-sm">
        <div class="flex items-center justify-between">
            <div>
                <p class="text-sm text-gray-500 dark:text-gray-400">已通过</p>
                <p class="text-2xl font-bold mt-1 text-green-600 dark:text-green-400" id="stat-approved">-</p>
            </div>
            <div class="w-10 h-10 bg-green-100 dark:bg-green-900/30 rounded-lg flex items-center justify-center">
                <svg class="w-5 h-5 text-green-600 dark:text-green-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>
                </svg>
            </div>
        </div>
    </div>

    <!-- 需修改 -->
    <div class="bg-white dark:bg-gray-800 rounded-xl p-5 border border-gray-200 dark:border-gray-700 shadow-sm">
        <div class="flex items-center justify-between">
            <div>
                <p class="text-sm text-gray-500 dark:text-gray-400">需修改</p>
                <p class="text-2xl font-bold mt-1 text-yellow-600 dark:text-yellow-400" id="stat-changes">-</p>
            </div>
            <div class="w-10 h-10 bg-yellow-100 dark:bg-yellow-900/30 rounded-lg flex items-center justify-center">
                <svg class="w-5 h-5 text-yellow-600 dark:text-yellow-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z"/>
                </svg>
            </div>
        </div>
    </div>

    <!-- 平均评分 -->
    <div class="bg-white dark:bg-gray-800 rounded-xl p-5 border border-gray-200 dark:border-gray-700 shadow-sm">
        <div class="flex items-center justify-between">
            <div>
                <p class="text-sm text-gray-500 dark:text-gray-400">平均评分</p>
                <p class="text-2xl font-bold mt-1" id="stat-score">-</p>
            </div>
            <div class="w-10 h-10 bg-purple-100 dark:bg-purple-900/30 rounded-lg flex items-center justify-center">
                <svg class="w-5 h-5 text-purple-600 dark:text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z"/>
                </svg>
            </div>
        </div>
    </div>
</div>
```

**Step 3: 创建 `backend/webui/templates/components/recent_reviews.html`**

```html
<!-- 最近审查列表（HTMX 片段） -->
<div id="recent-reviews">
    {% if reviews %}
    <div class="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm overflow-hidden">
        <div class="px-5 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
            <h2 class="text-lg font-semibold">最近审查</h2>
            <a href="/webui/pr/" class="text-sm text-pink-600 dark:text-pink-400 hover:underline">查看全部</a>
        </div>
        <div class="divide-y divide-gray-200 dark:divide-gray-700">
            {% for r in reviews %}
            <a href="/webui/pr/{{ r.id }}" class="flex items-center gap-4 px-5 py-3.5 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors">
                <!-- 状态图标 -->
                <div class="flex-shrink-0">
                    {% if r.status == "completed" and r.decision == "approve" %}
                    <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400">
                        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
                        通过
                    </span>
                    {% elif r.status == "completed" and r.decision == "request_changes" %}
                    <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-400">
                        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01"/></svg>
                        需修改
                    </span>
                    {% elif r.status == "reviewing" %}
                    <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400">
                        <svg class="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
                        审查中
                    </span>
                    {% elif r.status == "failed" %}
                    <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400">
                        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                        失败
                    </span>
                    {% else %}
                    <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400">
                        待处理
                    </span>
                    {% endif %}
                </div>

                <!-- PR 信息 -->
                <div class="flex-1 min-w-0">
                    <p class="text-sm font-medium truncate">{{ r.repo_owner }}/{{ r.repo_name }} <span class="text-gray-400">#{{ r.pr_id }}</span></p>
                    <p class="text-xs text-gray-500 dark:text-gray-400 truncate mt-0.5">{{ r.title or '无标题' }}</p>
                </div>

                <!-- 评分 -->
                {% if r.overall_score %}
                <div class="flex-shrink-0 text-right">
                    <span class="text-lg font-bold {% if r.overall_score >= 8 %}text-green-600 dark:text-green-400{% elif r.overall_score >= 6 %}text-yellow-600 dark:text-yellow-400{% else %}text-red-600 dark:text-red-400{% endif %}">
                        {{ r.overall_score }}
                    </span>
                    <span class="text-xs text-gray-400">/10</span>
                </div>
                {% endif %}

                <!-- 时间 -->
                <div class="flex-shrink-0 text-xs text-gray-400 dark:text-gray-500 hidden sm:block">
                    {{ r.created_at[:16] if r.created_at else '-' }}
                </div>
            </a>
            {% endfor %}
        </div>
    </div>
    {% else %}
    <div class="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-12 text-center">
        <svg class="w-12 h-12 mx-auto text-gray-300 dark:text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
        </svg>
        <p class="mt-4 text-gray-500 dark:text-gray-400">暂无审查记录</p>
    </div>
    {% endif %}
</div>
```

**Step 4: 创建 `backend/webui/templates/dashboard.html`**

```html
{% extends "base.html" %}

{% block title %}仪表盘 - Sakura AI Reviewer{% endblock %}

{% block content %}
<div class="space-y-6" x-data="dashboardInit()">

    <!-- 页面标题 -->
    <div>
        <h1 class="text-2xl font-bold">仪表盘</h1>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">PR 审查概览</p>
    </div>

    <!-- 统计卡片 -->
    {% include "components/stats_cards.html" %}

    <!-- 最近审查 -->
    <div>
        <h2 class="text-lg font-semibold mb-3">最近审查</h2>
        <div id="recent-reviews-container" hx-get="/api/webui/recent-reviews-html" hx-trigger="load" hx-swap="innerHTML">
            <div class="text-center py-8 text-gray-400">
                <svg class="w-8 h-8 mx-auto animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
                </svg>
                <p class="mt-2">加载中...</p>
            </div>
        </div>
    </div>
</div>
{% endblock %}

{% block extra_scripts %}
<script>
function dashboardInit() {
    // 加载统计数据
    fetch('/api/webui/stats')
        .then(r => r.json())
        .then(data => {
            document.getElementById('stat-total').textContent = data.total;
            document.getElementById('stat-approved').textContent = data.approved;
            document.getElementById('stat-changes').textContent = data.changes_requested;
            document.getElementById('stat-score').textContent = data.avg_score;
        })
        .catch(err => console.error('加载统计数据失败:', err));
}
</script>
{% endblock %}
```

**Step 5: Commit**

```bash
git add backend/webui/routes/dashboard.py backend/webui/templates/dashboard.html backend/webui/templates/components/stats_cards.html backend/webui/templates/components/recent_reviews.html
git commit -m "feat(webui): implement dashboard page with stats and recent reviews"
```

---

### Task 7: 实现 PR 列表页面（搜索、过滤、分页）

**Files:**
- Create: `backend/webui/routes/pr.py` (PR 列表 + 详情路由和 API)
- Create: `backend/webui/templates/pr_list.html` (PR 列表模板)
- Create: `backend/webui/templates/components/pr_filters.html` (搜索过滤组件)
- Create: `backend/webui/templates/components/pagination.html` (分页组件)

**Step 1: 创建 `backend/webui/routes/pr.py`**

```python
"""WebUI PR 审查管理路由"""

from fastapi import APIRouter, Request, Depends, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import PRReview, ReviewComment
from backend.webui.deps import require_auth, get_db, get_templates, get_csrf_serializer

router = APIRouter(prefix="/pr", tags=["WebUI PR"])
templates = get_templates()


@router.get("/")
async def pr_list_page(
    request: Request,
    user: dict = Depends(require_auth),
):
    """渲染 PR 列表页面"""
    return templates.TemplateResponse("pr_list.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "pr",
    })


@router.get("/list-fragment")
async def pr_list_fragment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    search: str = Query("", description="搜索关键词（PR标题/仓库名/作者）"),
    status: str = Query("", description="按状态过滤"),
    decision: str = Query("", description="按决策过滤"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """PR 列表 HTMX 片段（支持搜索、过滤、分页）"""
    # 构建查询
    query = select(PRReview)
    count_query = select(func.count(PRReview.id))

    # 搜索
    if search:
        search_filter = or_(
            PRReview.title.ilike(f"%{search}%"),
            PRReview.repo_name.ilike(f"%{search}%"),
            PRReview.repo_owner.ilike(f"%{search}%"),
            PRReview.author.ilike(f"%{search}%"),
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    # 状态过滤
    if status:
        query = query.where(PRReview.status == status)
        count_query = count_query.where(PRReview.status == status)

    # 决策过滤
    if decision:
        query = query.where(PRReview.decision == decision)
        count_query = count_query.where(PRReview.decision == decision)

    # 排序
    query = query.order_by(desc(PRReview.created_at))

    # 总数
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    total_pages = max(1, (total + per_page - 1) // per_page)

    # 分页
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    result = await db.execute(query)
    reviews = result.scalars().all()

    # 渲染 HTMX 片段
    return templates.TemplateResponse("components/pr_list_fragment.html", {
        "request": request,
        "reviews": reviews,
        "search": search,
        "status": status,
        "decision": decision,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "per_page": per_page,
    })


@router.get("/{review_id}")
async def pr_detail_page(
    request: Request,
    review_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """PR 详情页面"""
    # 查询 PR 审查记录
    review = await db.execute(
        select(PRReview).where(PRReview.id == review_id)
    )
    review = review.scalar_one_or_none()
    if not review:
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<h1>审查记录不存在</h1>", status_code=404)

    # 查询关联评论
    comments_result = await db.execute(
        select(ReviewComment)
        .where(ReviewComment.review_id == review_id)
        .order_by(
            ReviewComment.file_path.asc().nullslast(),
            ReviewComment.line_number.asc().nullslast(),
            ReviewComment.created_at.asc(),
        )
    )
    comments = comments_result.scalars().all()

    return templates.TemplateResponse("pr_detail.html", {
        "request": request,
        "current_user": user,
        "csrf_token": get_csrf_serializer().dumps({}),
        "active_page": "pr",
        "review": review,
        "comments": comments,
    })
```

**Step 2: 创建 `backend/webui/templates/components/pr_filters.html`**

```html
<!-- PR 搜索和过滤 -->
<div class="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-4 mb-4">
    <form id="pr-filter-form" class="flex flex-col sm:flex-row gap-3">
        <!-- 搜索框 -->
        <div class="flex-1 relative">
            <svg class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>
            </svg>
            <input type="text" name="search" value="{{ search }}" placeholder="搜索 PR 标题、仓库或作者..."
                   class="w-full pl-10 pr-4 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-sm focus:ring-2 focus:ring-pink-500 focus:border-transparent outline-none">
        </div>

        <!-- 状态过滤 -->
        <select name="status"
                class="px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-sm focus:ring-2 focus:ring-pink-500 focus:border-transparent outline-none">
            <option value="" {% if not status %}selected{% endif %}>全部状态</option>
            <option value="pending" {% if status == 'pending' %}selected{% endif %}>待处理</option>
            <option value="reviewing" {% if status == 'reviewing' %}selected{% endif %}>审查中</option>
            <option value="completed" {% if status == 'completed' %}selected{% endif %}>已完成</option>
            <option value="failed" {% if status == 'failed' %}selected{% endif %}>失败</option>
        </select>

        <!-- 决策过滤 -->
        <select name="decision"
                class="px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-sm focus:ring-2 focus:ring-pink-500 focus:border-transparent outline-none">
            <option value="" {% if not decision %}selected{% endif %}>全部决策</option>
            <option value="approve" {% if decision == 'approve' %}selected{% endif %}>通过</option>
            <option value="request_changes" {% if decision == 'request_changes' %}selected{% endif %}>需修改</option>
            <option value="comment" {% if decision == 'comment' %}selected{% endif %}>评论</option>
        </select>

        <!-- 搜索按钮 -->
        <button type="submit"
                class="px-4 py-2 bg-pink-500 text-white rounded-lg text-sm font-medium hover:bg-pink-600 transition-colors">
            搜索
        </button>
    </form>
</div>
```

**Step 3: 创建 `backend/webui/templates/components/pr_list_fragment.html`**

```html
<!-- PR 列表 HTMX 片段 -->
{% include "components/pr_filters.html" %}

<!-- 列表 -->
<div class="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm overflow-hidden">
    {% if reviews %}
    <div class="divide-y divide-gray-200 dark:divide-gray-700">
        {% for r in reviews %}
        <a href="/webui/pr/{{ r.id }}" class="flex items-center gap-4 px-5 py-3.5 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors">
            <!-- 状态 -->
            <div class="flex-shrink-0">
                {% if r.status == "completed" and r.decision == "approve" %}
                <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400">通过</span>
                {% elif r.status == "completed" and r.decision == "request_changes" %}
                <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-400">需修改</span>
                {% elif r.status == "completed" and r.decision == "comment" %}
                <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400">评论</span>
                {% elif r.status == "reviewing" %}
                <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400">审查中</span>
                {% elif r.status == "failed" %}
                <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400">失败</span>
                {% else %}
                <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400">待处理</span>
                {% endif %}
            </div>

            <!-- PR 信息 -->
            <div class="flex-1 min-w-0">
                <p class="text-sm font-medium truncate">
                    <span class="text-gray-500 dark:text-gray-400">{{ r.repo_owner }}/{{ r.repo_name }}</span>
                    <span class="text-gray-400 mx-1">#{{ r.pr_id }}</span>
                    {{ r.title or '无标题' }}
                </p>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                    {% if r.author %}{{ r.author }} · {% endif %}{{ r.strategy }} 策略 · {{ r.file_count or 0 }} 文件 · {{ r.line_count or 0 }} 行
                </p>
            </div>

            <!-- 评分 -->
            {% if r.overall_score %}
            <div class="flex-shrink-0 text-right">
                <span class="text-lg font-bold {% if r.overall_score >= 8 %}text-green-600 dark:text-green-400{% elif r.overall_score >= 6 %}text-yellow-600 dark:text-yellow-400{% else %}text-red-600 dark:text-red-400{% endif %}">
                    {{ r.overall_score }}
                </span>
                <span class="text-xs text-gray-400">/10</span>
            </div>
            {% endif %}

            <!-- 时间 -->
            <div class="flex-shrink-0 text-xs text-gray-400 dark:text-gray-500 hidden md:block">
                {{ r.created_at[:16] if r.created_at else '-' }}
            </div>
        </a>
        {% endfor %}
    </div>

    <!-- 分页 -->
    {% if total_pages > 1 %}
    <div class="px-5 py-3 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between">
        <p class="text-sm text-gray-500 dark:text-gray-400">
            共 {{ total }} 条，第 {{ page }}/{{ total_pages }} 页
        </p>
        <div class="flex gap-1">
            {% if page > 1 %}
            <a href="#" onclick="loadPage({{ page - 1 }}); return false;"
               class="px-3 py-1.5 rounded-lg text-sm border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors">
                上一页
            </a>
            {% endif %}

            {% for p in range(1, total_pages + 1) %}
                {% if p == page %}
                <span class="px-3 py-1.5 rounded-lg text-sm bg-pink-500 text-white">{{ p }}</span>
                {% elif p <= 3 or p >= total_pages - 2 or (p >= page - 1 and p <= page + 1) %}
                <a href="#" onclick="loadPage({{ p }}); return false;"
                   class="px-3 py-1.5 rounded-lg text-sm border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors">
                    {{ p }}
                </a>
                {% elif p == 4 or p == total_pages - 3 %}
                <span class="px-2 py-1.5 text-gray-400">...</span>
                {% endif %}
            {% endfor %}

            {% if page < total_pages %}
            <a href="#" onclick="loadPage({{ page + 1 }}); return false;"
               class="px-3 py-1.5 rounded-lg text-sm border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors">
                下一页
            </a>
            {% endif %}
        </div>
    </div>
    {% endif %}

    {% else %}
    <div class="p-12 text-center">
        <svg class="w-12 h-12 mx-auto text-gray-300 dark:text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>
        </svg>
        <p class="mt-4 text-gray-500 dark:text-gray-400">没有找到匹配的审查记录</p>
    </div>
    {% endif %}
</div>
```

**Step 4: 创建 `backend/webui/templates/pr_list.html`**

```html
{% extends "base.html" %}

{% block title %}PR 审查 - Sakura AI Reviewer{% endblock %}

{% block content %}
<div class="space-y-4">
    <!-- 页面标题 -->
    <div>
        <h1 class="text-2xl font-bold">PR 审查</h1>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">管理和查看所有 PR 审查记录</p>
    </div>

    <!-- PR 列表（通过 HTMX 加载） -->
    <div id="pr-list-container" hx-get="/webui/pr/list-fragment" hx-trigger="load" hx-swap="innerHTML">
        <div class="text-center py-8 text-gray-400">
            <svg class="w-8 h-8 mx-auto animate-spin" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
            </svg>
            <p class="mt-2">加载中...</p>
        </div>
    </div>
</div>
{% endblock %}

{% block extra_scripts %}
<script>
function loadPage(pageNum) {
    // 收集当前过滤条件
    const container = document.getElementById('pr-list-container');
    const params = new URLSearchParams();

    // 从当前 URL 获取查询参数
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('search')) params.set('search', urlParams.get('search'));
    if (urlParams.get('status')) params.set('status', urlParams.get('status'));
    if (urlParams.get('decision')) params.set('decision', urlParams.get('decision'));
    params.set('page', pageNum);

    container.innerHTML = '<div class="text-center py-8 text-gray-400"><svg class="w-8 h-8 mx-auto animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg><p class="mt-2">加载中...</p></div>';

    fetch('/webui/pr/list-fragment?' + params.toString(), {
        headers: { 'HX-Request': 'true' }
    })
    .then(r => r.text())
    .then(html => { container.innerHTML = html; })
    .catch(err => console.error('加载失败:', err));
}

// 拦截过滤表单提交
document.addEventListener('submit', function(e) {
    if (e.target.id === 'pr-filter-form') {
        e.preventDefault();
        const formData = new FormData(e.target);
        const params = new URLSearchParams();
        for (const [key, value] of formData.entries()) {
            if (value) params.set(key, value);
        }
        params.set('page', '1');

        const container = document.getElementById('pr-list-container');
        container.innerHTML = '<div class="text-center py-8 text-gray-400"><svg class="w-8 h-8 mx-auto animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg><p class="mt-2">加载中...</p></div>';

        fetch('/webui/pr/list-fragment?' + params.toString(), {
            headers: { 'HX-Request': 'true' }
        })
        .then(r => r.text())
        .then(html => { container.innerHTML = html; })
        .catch(err => console.error('加载失败:', err));
    }
});
</script>
{% endblock %}
```

**Step 5: Commit**

```bash
git add backend/webui/routes/pr.py backend/webui/templates/pr_list.html backend/webui/templates/components/pr_filters.html backend/webui/templates/components/pr_list_fragment.html
git commit -m "feat(webui): implement PR list page with search, filter and pagination"
```

---

### Task 8: 实现 PR 详情页面

**Files:**
- Create: `backend/webui/templates/pr_detail.html` (PR 详情模板)
- Create: `backend/webui/templates/components/pr_header.html` (PR 信息头部)
- Create: `backend/webui/templates/components/comment_list.html` (评论列表)
- Modify: `backend/webui/routes/dashboard.py` (添加 recent-reviews HTML 端点)

**Step 1: 创建 `backend/webui/templates/pr_detail.html`**

```html
{% extends "base.html" %}

{% block title %}PR #{{ review.pr_id }} - Sakura AI Reviewer{% endblock %}

{% block content %}
<div class="space-y-6">
    <!-- 面包屑导航 -->
    <nav class="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
        <a href="/webui/" class="hover:text-pink-600 dark:hover:text-pink-400">仪表盘</a>
        <span>/</span>
        <a href="/webui/pr/" class="hover:text-pink-600 dark:hover:text-pink-400">PR 审查</a>
        <span>/</span>
        <span class="text-gray-900 dark:text-white">{{ review.repo_owner }}/{{ review.repo_name }} #{{ review.pr_id }}</span>
    </nav>

    <!-- PR 信息卡片 -->
    <div class="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
        <div class="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
            <div class="flex-1">
                <h1 class="text-xl font-bold">
                    {{ review.repo_owner }}/{{ review.repo_name }}
                    <span class="text-gray-400 font-normal">#{{ review.pr_id }}</span>
                </h1>
                <p class="mt-2 text-gray-700 dark:text-gray-300">{{ review.title or '无标题' }}</p>
                <div class="mt-3 flex flex-wrap gap-2 text-sm text-gray-500 dark:text-gray-400">
                    {% if review.author %}
                    <span class="flex items-center gap-1">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/></svg>
                        {{ review.author }}
                    </span>
                    {% endif %}
                    <span class="flex items-center gap-1">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
                        {{ review.strategy }} 策略
                    </span>
                    {% if review.file_count %}
                    <span>{{ review.file_count }} 文件</span>
                    {% endif %}
                    {% if review.line_count %}
                    <span>{{ review.line_count }} 行</span>
                    {% endif %}
                </div>
            </div>

            <!-- 状态和评分 -->
            <div class="flex items-center gap-3">
                <!-- 状态 -->
                {% if review.status == "completed" and review.decision == "approve" %}
                <span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
                    通过
                </span>
                {% elif review.status == "completed" and review.decision == "request_changes" %}
                <span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-400">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01"/></svg>
                    需修改
                </span>
                {% elif review.status == "completed" and review.decision == "comment" %}
                <span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01"/></svg>
                    评论
                </span>
                {% elif review.status == "reviewing" %}
                <span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400">
                    <svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
                    审查中
                </span>
                {% elif review.status == "failed" %}
                <span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                    失败
                </span>
                {% else %}
                <span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400">
                    待处理
                </span>
                {% endif %}

                <!-- 评分 -->
                {% if review.overall_score %}
                <div class="text-center px-4">
                    <div class="text-3xl font-bold {% if review.overall_score >= 8 %}text-green-600 dark:text-green-400{% elif review.overall_score >= 6 %}text-yellow-600 dark:text-yellow-400{% else %}text-red-600 dark:text-red-400{% endif %}">
                        {{ review.overall_score }}
                    </div>
                    <div class="text-xs text-gray-400">/ 10</div>
                </div>
                {% endif %}
            </div>
        </div>

        <!-- 时间信息 -->
        <div class="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700 flex flex-wrap gap-4 text-xs text-gray-500 dark:text-gray-400">
            <span>创建时间: {{ review.created_at[:19] if review.created_at else '-' }}</span>
            {% if review.completed_at %}
            <span>完成时间: {{ review.completed_at[:19] }}</span>
            {% endif %}
            {% if review.branch %}
            <span>分支: {{ review.branch }}</span>
            {% endif %}
        </div>
    </div>

    <!-- 审查摘要 -->
    {% if review.review_summary %}
    <div class="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
        <h2 class="text-lg font-semibold mb-3 flex items-center gap-2">
            <svg class="w-5 h-5 text-pink-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
            </svg>
            审查摘要
        </h2>
        <div class="prose dark:prose-invert max-w-none text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap leading-relaxed">
            {{ review.review_summary }}
        </div>
    </div>
    {% endif %}

    <!-- 决策理由 -->
    {% if review.decision_reason %}
    <div class="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
        <h2 class="text-lg font-semibold mb-3 flex items-center gap-2">
            <svg class="w-5 h-5 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
            </svg>
            决策理由
        </h2>
        <div class="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap leading-relaxed">
            {{ review.decision_reason }}
        </div>
    </div>
    {% endif %}

    <!-- 错误信息 -->
    {% if review.error_message %}
    <div class="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-xl p-6">
        <h2 class="text-lg font-semibold mb-2 text-red-700 dark:text-red-400">错误信息</h2>
        <pre class="text-sm text-red-600 dark:text-red-300 whitespace-pre-wrap">{{ review.error_message }}</pre>
    </div>
    {% endif %}

    <!-- 审查评论 -->
    <div class="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm overflow-hidden">
        <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
            <h2 class="text-lg font-semibold flex items-center gap-2">
                <svg class="w-5 h-5 text-pink-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z"/>
                </svg>
                审查评论
                <span class="text-sm font-normal text-gray-400">({{ comments | length }})</span>
            </h2>
        </div>

        {% if comments %}
        <div class="divide-y divide-gray-200 dark:divide-gray-700">
            {% for c in comments %}
            <div class="px-6 py-4">
                <div class="flex items-start gap-3">
                    <!-- 严重程度图标 -->
                    <div class="flex-shrink-0 mt-0.5">
                        {% if c.severity == "critical" %}
                        <span class="inline-block w-6 h-6 rounded-full bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400 text-center leading-6 text-xs font-bold">!</span>
                        {% elif c.severity == "major" %}
                        <span class="inline-block w-6 h-6 rounded-full bg-orange-100 dark:bg-orange-900/30 text-orange-600 dark:text-orange-400 text-center leading-6 text-xs font-bold">W</span>
                        {% elif c.severity == "minor" %}
                        <span class="inline-block w-6 h-6 rounded-full bg-yellow-100 dark:bg-yellow-900/30 text-yellow-600 dark:text-yellow-400 text-center leading-6 text-xs font-bold">I</span>
                        {% else %}
                        <span class="inline-block w-6 h-6 rounded-full bg-blue-100 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 text-center leading-6 text-xs font-bold">S</span>
                        {% endif %}
                    </div>

                    <div class="flex-1 min-w-0">
                        <!-- 文件和行号 -->
                        {% if c.file_path %}
                        <div class="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 mb-1">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
                            <code class="bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded text-xs">{{ c.file_path }}</code>
                            {% if c.line_number %}
                            <span>:L{{ c.line_number }}</span>
                            {% endif %}
                            <!-- 评论类型标签 -->
                            {% if c.comment_type == "overall" %}
                            <span class="px-1.5 py-0.5 rounded bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400 text-xs">总评</span>
                            {% elif c.comment_type == "file" %}
                            <span class="px-1.5 py-0.5 rounded bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400 text-xs">文件级</span>
                            {% elif c.comment_type == "line" %}
                            <span class="px-1.5 py-0.5 rounded bg-cyan-100 dark:bg-cyan-900/30 text-cyan-700 dark:text-cyan-400 text-xs">行级</span>
                            {% endif %}
                        </div>
                        {% endif %}

                        <!-- 评论内容 -->
                        <div class="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap leading-relaxed">
                            {{ c.content }}
                        </div>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div class="p-12 text-center text-gray-400 dark:text-gray-500">
            <svg class="w-12 h-12 mx-auto" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z"/>
            </svg>
            <p class="mt-4">暂无评论</p>
        </div>
        {% endif %}
    </div>

    <!-- 返回按钮 -->
    <div>
        <a href="/webui/pr/" class="inline-flex items-center gap-1 text-sm text-gray-500 dark:text-gray-400 hover:text-pink-600 dark:hover:text-pink-400">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7"/>
            </svg>
            返回 PR 列表
        </a>
    </div>
</div>
{% endblock %}
```

**Step 2: 在 `backend/webui/routes/dashboard.py` 添加 `recent-reviews-html` 端点**

在文件中 `get_recent_reviews` 函数之后添加：

```python
@router.get("/api/webui/recent-reviews-html")
async def get_recent_reviews_html(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """返回最近审查的 HTML 片段（供仪表盘 HTMX 加载）"""
    result = await db.execute(
        select(PRReview)
        .order_by(desc(PRReview.created_at))
        .limit(10)
    )
    reviews = result.scalars().all()

    review_data = [
        {
            "id": r.id,
            "pr_id": r.pr_id,
            "repo_name": r.repo_name,
            "repo_owner": r.repo_owner,
            "title": r.title,
            "author": r.author,
            "status": r.status,
            "overall_score": r.overall_score,
            "decision": r.decision,
            "strategy": r.strategy,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in reviews
    ]

    return templates.TemplateResponse("components/recent_reviews.html", {
        "request": request,
        "reviews": review_data,
    })
```

**Step 3: Commit**

```bash
git add backend/webui/routes/dashboard.py backend/webui/routes/pr.py backend/webui/templates/pr_detail.html
git commit -m "feat(webui): implement PR detail page with review summary and comments"
```

---

### Task 9: 创建 WebUI 路由总入口并注册到 FastAPI

**Files:**
- Create: `backend/webui/routes/__init__.py` (路由总入口)
- Modify: `backend/main.py` (注册 WebUI 路由)

**Step 1: 更新 `backend/webui/routes/__init__.py`**

```python
"""WebUI 路由"""

from fastapi import APIRouter
from backend.webui.routes import auth, dashboard, pr

webui_router = APIRouter(prefix="/webui")

webui_router.include_router(auth.router)
webui_router.include_router(dashboard.router)
webui_router.include_router(pr.router)
```

**Step 2: 修改 `backend/main.py` 注册 WebUI 路由**

在 `backend/main.py` 中添加导入和路由注册。在现有 `from backend.api import webhook` 之后添加：

```python
from backend.webui.routes import webui_router
```

在 `app.include_router(webhook.router, ...)` 之后添加：

```python
# 注册 WebUI 路由
app.include_router(webui_router)
```

**Step 3: Commit**

```bash
git add backend/webui/routes/__init__.py backend/main.py
git commit -m "feat(webui): register WebUI routes in FastAPI app"
```

---

### Task 10: 端到端验证

**Step 1: 安装新依赖**

```bash
pip install jinja2 python-jose[cryptography] itsdangerous passlib[bcrypt]
```

**Step 2: 启动应用并验证**

```bash
cd /path/to/project && python -m uvicorn backend.main:app --reload --port 8000
```

验证清单：
1. 访问 `http://localhost:8000/webui/auth/login` — 应看到登录页面
2. 使用 admin/admin123 登录 — 应跳转到仪表盘
3. 仪表盘应显示统计数据和最近审查列表
4. 点击"PR 审查" — 应看到 PR 列表页
5. 搜索框输入关键词 — 应过滤结果
6. 状态/决策下拉框 — 应过滤结果
7. 点击一条 PR — 应看到详情页（摘要、评分、评论列表）
8. 切换明暗主题 — 页面风格应切换
9. 退出登录 — 应跳转回登录页
10. 未登录访问 `/webui/` — 应重定向到登录页

**Step 3: 修复发现的问题**

**Step 4: Final commit**

```bash
git add -A
git commit -m "feat(webui): complete P0 WebUI with dashboard and PR management"
```
