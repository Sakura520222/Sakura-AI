"""Bootstrap 模式管理模块

首次部署时检测配置状态，引导用户完成 Setup Wizard。
使用 config/connection.json 存储数据库连接信息和完成标记。
"""

import hmac
import json
import os
import secrets
import tempfile
import time
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Literal

from loguru import logger
from starlette.responses import JSONResponse, RedirectResponse

SetupState = Literal["not_configured", "in_progress", "completed"]

# 配置文件路径
CONNECTION_CONFIG_PATH = Path("config/connection.json")

# 进程级缓存（中间件高频调用）
_state_cache: SetupState | None = None
_cache_ts: float = 0
_CACHE_TTL = 5.0  # 秒


def get_connection_config_path() -> Path:
    """获取当前使用的连接配置路径，支持本地开发脚本隔离配置。"""
    override = os.getenv("SAKURA_CONNECTION_CONFIG_PATH", "").strip()
    if override:
        return Path(override)
    return CONNECTION_CONFIG_PATH


def read_connection_config() -> dict:
    """读取 config/connection.json

    Returns:
        连接配置字典，文件不存在时返回空字典
    """
    config_path = get_connection_config_path()
    if not config_path.exists():
        return {}
    try:
        text = config_path.read_text(encoding="utf-8")
        return json.loads(text)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"读取连接配置失败: {e}")
        return {}


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync after replacing a config file.

    ``os.replace`` is atomic for readers, while syncing the containing
    directory makes the replacement durable across a sudden POSIX reboot.
    Windows and filesystems without ``O_DIRECTORY`` simply skip this optional
    durability step.
    """

    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not directory_flag:
        return

    directory_fd: int | None = None
    try:
        directory_fd = os.open(directory, os.O_RDONLY | directory_flag)
        os.fsync(directory_fd)
    except (OSError, ValueError):
        logger.debug("无法同步 connection.json 所在目录（当前平台可忽略）")
    finally:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except (OSError, ValueError):
                logger.debug("无法关闭 connection.json 所在目录句柄")


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
        config["completed_at"] = datetime.now(UTC).isoformat()

    # 在同一目录中先写临时文件，再用 os.replace 原子替换目标。这样即使
    # 进程在写入过程中崩溃，启动时也只会看到完整的旧文件或完整的新文件，
    # 不会读到截断 JSON。临时文件与目标同目录也保证 replace 不跨文件系统。
    config_path = get_connection_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            dir=config_path.parent,
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as temp_file:
            temp_file.write(json.dumps(config, indent=2, ensure_ascii=False))
            temp_file.flush()
            os.fsync(temp_file.fileno())

        # 限制临时文件权限后再替换，避免凭证在短暂窗口内继承宽松权限。
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            logger.debug("无法设置临时 connection.json 文件权限（Windows 可忽略）")
        os.replace(temp_path, config_path)
        temp_path = None
        _fsync_directory(config_path.parent)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    # 限制文件权限为仅所有者可读写（包含数据库凭证等敏感信息）
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        logger.debug("无法设置 connection.json 文件权限（Windows 可忽略）")
    clear_bootstrap_cache()
    logger.info(f"连接配置已写入 ({'已完成' if setup_completed else '进行中'})")


def mark_setup_completed(database_url: str) -> None:
    """标记配置完成

    Args:
        database_url: 数据库连接字符串（写入 connection.json 供下次启动使用）
    """
    write_connection_config(database_url, setup_completed=True)
    clear_setup_token()
    logger.info("Setup 已完成，标记已写入")


def check_setup_state() -> SetupState:
    """检测 Setup 状态

    通过 connection.json 判断：
    - not_configured: 文件不存在
    - completed: 文件存在且 setup_completed == True
    - in_progress: 文件存在但 setup_completed != True
    """
    config_path = get_connection_config_path()
    if not config_path.exists():
        return "not_configured"

    config = read_connection_config()

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


# =============================================================================
# Setup Wizard Token 安全防护
# =============================================================================

# 进程级 Token（每次启动重新生成，不持久化）
_setup_token: str | None = None

# Cookie 名称
_COOKIE_NAME = "setup_verified"


def generate_setup_token() -> None:
    """生成 Setup Token 并醒目打印到日志。

    仅在 bootstrap 模式启动时调用。每次启动生成新 Token，
    旧 Token 在进程退出后自然失效。
    """
    global _setup_token
    _setup_token = secrets.token_urlsafe(32)
    logger.info("=" * 60)
    logger.info("Setup Wizard 已启动 — 请使用以下 Token 完成首次部署验证：")
    logger.info(f"  Token: {_setup_token}")
    logger.info("请从日志中复制此 Token，在浏览器 /setup/verify 页面输入。")
    logger.info("=" * 60)


def get_setup_token() -> str | None:
    """获取当前 Setup Token（未生成时返回 None）。"""
    return _setup_token


def validate_setup_token(submitted: str) -> bool:
    """常量时间比较 Token，防止时序攻击。"""
    if _setup_token is None or not submitted:
        return False
    return hmac.compare_digest(submitted, _setup_token)


def clear_setup_token() -> None:
    """清除内存中的 Token。Setup 完成后调用。"""
    global _setup_token
    _setup_token = None


def _has_valid_setup_cookie(scope: dict) -> bool:
    """从 ASGI scope 解析 Cookie，验证 setup_verified 是否有效。

    纯 ASGI 实现，不构造 Request 对象，避免在中间件层引入额外开销。
    """
    token = get_setup_token()
    if token is None:
        return False
    for header_name, header_value in scope.get("headers", []):
        if header_name == b"cookie":
            cookies = SimpleCookie(header_value.decode("latin-1"))
            morsel = cookies.get(_COOKIE_NAME)
            if morsel is not None and hmac.compare_digest(morsel.value, token):
                return True
    return False


async def get_missing_fields() -> list[str]:
    """返回核心配置中缺失的字段列表（从数据库查询）"""
    core_required = [
        "github_app_id",
        "github_private_key",
        "github_webhook_secret",
        "telegram_bot_token",
    ]

    try:
        from sqlalchemy import select

        from backend.models.database import AppConfig, async_session

        async with async_session() as session:
            result = await session.execute(
                select(AppConfig.key_name, AppConfig.key_value).where(
                    AppConfig.key_name.in_(core_required)
                )
            )
            db_values = {row[0]: row[1] for row in result.all()}
    except Exception as exc:
        # 数据库不可用时，回退到 Settings 单例
        from backend.core.config import get_settings

        logger.debug(f"数据库不可用，回退到 Settings 检查缺失字段: {exc}")

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
        from sqlalchemy import select

        # 确保数据库引擎已初始化
        from backend.models import database as db_module
        from backend.models.database import AppConfig, async_session

        if db_module.async_engine is None:
            return 0

        async with async_session() as session:
            result = await session.execute(
                select(AppConfig.key_name, AppConfig.key_value).where(
                    AppConfig.key_name.in_(
                        [
                            "github_app_id",
                            "github_private_key",
                            "github_webhook_secret",
                            "telegram_bot_token",
                        ]
                    )
                )
            )
            db_values = {row[0]: (row[1] or "") for row in result.all()}
    except Exception as exc:
        logger.debug(f"数据库不可用，回退到 Step 0: {exc}")
        return 0

    # Step 1: GitHub App
    github_fields = [
        db_values.get("github_app_id", ""),
        db_values.get("github_private_key", ""),
        db_values.get("github_webhook_secret", ""),
    ]
    if not all(f.strip() for f in github_fields):
        return 1

    # Step 2: AI & 通知。Setup 不强制 AI readiness；账号和角色绑定由配置页管理。
    # Step 3: 管理员
    return 3


class BootstrapMiddleware:
    """Bootstrap 模式中间件：未完成 Setup 时拦截所有请求。

    纯 ASGI 实现（不再继承 ``BaseHTTPMiddleware``）。

    Why: ``starlette.middleware.base.BaseHTTPMiddleware`` 用 anyio TaskGroup
    包裹下游 ``call_next``，响应返回后 teardown 会取消该 coro。若此时
    FastAPI 依赖（如 ``get_db``）正在执行数据库会话清理，cleanup 会被
    ``CancelledError`` 中断，导致连接未归还连接池、最终由 GC 兜底回收，
    触发 ``SAWarning: non-checked-in connection``。纯 ASGI 中间件不经过
    该 TaskGroup 路径，从根上消除连接泄漏。
    """

    # 始终放行的路径（不含 /setup，/setup 由专门逻辑处理）
    ALLOWED_PATHS = ("/health", "/docs", "/openapi.json", "/redoc")

    SETUP_PREFIX = "/setup"
    VERIFY_PREFIX = "/setup/verify"

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        # 仅拦截 HTTP；lifespan / websocket 等透传给下游应用
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not is_bootstrap_mode():
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        # 根路径重定向到 /setup
        if path == "/":
            await RedirectResponse(url="/setup", status_code=302)(scope, receive, send)
            return

        # Setup Wizard 路径分层处理
        if path.startswith(self.SETUP_PREFIX):
            # verify 页面本身不需要 Token（Token 输入入口）
            if path.startswith(self.VERIFY_PREFIX):
                await self.app(scope, receive, send)
                return
            # 其他 /setup 路径需要验证 setup_verified Cookie
            if not _has_valid_setup_cookie(scope):
                await RedirectResponse(
                    url="/setup/verify", status_code=302
                )(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        # 其他始终放行的路径
        for allowed in self.ALLOWED_PATHS:
            if path.startswith(allowed):
                await self.app(scope, receive, send)
                return

        # 静态资源放行
        if path.startswith("/static") or path.endswith((".css", ".js", ".ico")):
            await self.app(scope, receive, send)
            return

        # API 请求返回 503
        if "/api/" in path or path.startswith("/api/"):
            await JSONResponse(
                status_code=503,
                content={
                    "detail": "应用尚未完成初始配置，请访问 /setup 完成设置",
                    "setup_url": "/setup",
                },
            )(scope, receive, send)
            return

        # 页面请求重定向到 Setup
        await RedirectResponse(url="/setup", status_code=302)(scope, receive, send)
