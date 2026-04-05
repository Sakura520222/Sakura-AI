"""Bootstrap 模式管理模块

首次部署时检测配置状态，引导用户完成 Setup Wizard。
使用 config/connection.json 存储数据库连接信息和完成标记。
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

SetupState = Literal["not_configured", "in_progress", "completed"]

# 配置文件路径
CONNECTION_CONFIG_PATH = Path("config/connection.json")

# 进程级缓存（中间件高频调用）
_state_cache: Optional[SetupState] = None
_cache_ts: float = 0
_CACHE_TTL = 5.0  # 秒


def read_connection_config() -> dict:
    """读取 config/connection.json

    Returns:
        连接配置字典，文件不存在时返回空字典
    """
    if not CONNECTION_CONFIG_PATH.exists():
        return {}
    try:
        text = CONNECTION_CONFIG_PATH.read_text(encoding="utf-8")
        return json.loads(text)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"读取连接配置失败: {e}")
        return {}


def write_connection_config(
    database_url: str,
    setup_completed: bool = False,
) -> None:
    """写入 config/connection.json

    Args:
        database_url: 数据库连接字符串
        setup_completed: 配置是否已完成
    """
    config: dict = {
        "database_url": database_url,
        "setup_completed": setup_completed,
    }
    if setup_completed:
        config["completed_at"] = datetime.now(timezone.utc).isoformat()

    # 确保 config 目录存在
    CONNECTION_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONNECTION_CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    clear_bootstrap_cache()
    logger.info(f"连接配置已写入 ({'已完成' if setup_completed else '进行中'})")


def mark_setup_completed(database_url: str) -> None:
    """标记配置完成

    Args:
        database_url: 数据库连接字符串（写入 connection.json 供下次启动使用）
    """
    write_connection_config(database_url, setup_completed=True)
    logger.info("Setup 已完成，标记已写入")


def check_setup_state() -> SetupState:
    """检测 Setup 状态

    通过 connection.json 判断：
    - completed: 文件存在且 setup_completed == True
    - not_configured: 文件不存在
    - in_progress: 文件存在但 setup_completed != True
    """
    config = read_connection_config()

    if not config:
        return "not_configured"

    if config.get("setup_completed"):
        return "completed"

    return "in_progress"


def is_bootstrap_mode() -> bool:
    """当前是否处于 bootstrap 模式（带 TTL 缓存）"""
    global _state_cache, _cache_ts
    now = time.time()
    if _state_cache is not None and (now - _cache_ts) < _CACHE_TTL:
        return _state_cache != "completed"
    _state_cache = check_setup_state()
    _cache_ts = now
    return _state_cache != "completed"


def clear_bootstrap_cache():
    """清除 bootstrap 缓存（配置变更后调用）"""
    global _state_cache, _cache_ts
    _state_cache = None
    _cache_ts = 0


async def get_missing_fields() -> list[str]:
    """返回核心配置中缺失的字段列表（从数据库查询）"""
    core_required = [
        "github_app_id",
        "github_private_key",
        "github_webhook_secret",
        "openai_api_key",
        "telegram_bot_token",
    ]

    try:
        from backend.models.database import async_session, AppConfig
        from sqlalchemy import select

        async with async_session() as session:
            result = await session.execute(
                select(AppConfig.key_name, AppConfig.key_value).where(
                    AppConfig.key_name.in_(core_required)
                )
            )
            db_values = {row[0]: row[1] for row in result.all()}
    except Exception:
        # 数据库不可用时，回退到 Settings 单例
        from backend.core.config import get_settings

        settings = get_settings()
        missing = []
        for field_name in core_required:
            if not getattr(settings, field_name, None):
                missing.append(field_name.upper())
        return missing

    missing = []
    for field_name in core_required:
        value = db_values.get(field_name, "")
        if not value or not str(value).strip():
            missing.append(field_name.upper())
    return missing


async def get_current_step() -> int:
    """根据已有配置推断应从第几步开始（断点续配，从数据库查询）

    步骤映射:
    - 0: 数据库配置
    - 1: GitHub App
    - 2: AI & 通知
    - 3: 管理员
    """
    # Step 0: 数据库配置 — 检查 connection.json
    conn_config = read_connection_config()
    if not conn_config.get("database_url", "").strip():
        return 0

    # Step 1+: 需要数据库连接
    try:
        from backend.models.database import async_session, AppConfig
        from sqlalchemy import select

        # 确保数据库引擎已初始化
        from backend.models import database as db_module
        if db_module.async_engine is None:
            return 0

        async with async_session() as session:
            result = await session.execute(
                select(AppConfig.key_name, AppConfig.key_value).where(
                    AppConfig.key_name.in_([
                        "github_app_id",
                        "github_private_key",
                        "github_webhook_secret",
                        "openai_api_key",
                    ])
                )
            )
            db_values = {row[0]: (row[1] or "") for row in result.all()}
    except Exception:
        return 0

    # Step 1: GitHub App
    github_fields = [
        db_values.get("github_app_id", ""),
        db_values.get("github_private_key", ""),
        db_values.get("github_webhook_secret", ""),
    ]
    if not all(f.strip() for f in github_fields):
        return 1

    # Step 2: AI & 通知
    if not db_values.get("openai_api_key", "").strip():
        return 2

    # Step 3: 管理员
    return 3



class BootstrapMiddleware(BaseHTTPMiddleware):
    """Bootstrap 模式中间件：未完成 Setup 时拦截所有请求"""

    # 始终放行的路径
    ALLOWED_PATHS = ("/setup", "/health", "/docs", "/openapi.json", "/redoc")

    async def dispatch(self, request: Request, call_next):
        if not is_bootstrap_mode():
            return await call_next(request)

        path = request.url.path

        # 放行根路径（重定向到 /setup）
        if path == "/":
            return RedirectResponse(url="/setup", status_code=302)

        # 放行 Setup Wizard 相关路径
        for allowed in self.ALLOWED_PATHS:
            if path.startswith(allowed):
                return await call_next(request)

        # 静态资源放行
        if path.startswith("/static") or path.endswith((".css", ".js", ".ico")):
            return await call_next(request)

        # API 请求返回 503
        if "/api/" in path or path.startswith("/api/"):
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "应用尚未完成初始配置，请访问 /setup 完成设置",
                    "setup_url": "/setup",
                },
            )

        # 页面请求重定向到 Setup
        return RedirectResponse(url="/setup", status_code=302)
