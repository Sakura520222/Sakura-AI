"""Bootstrap 模式管理模块

首次部署时检测配置状态，引导用户完成 Setup Wizard。
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

# 必填的环境变量字段（对应 Settings 中无默认值的字段）
REQUIRED_ENV_FIELDS = [
    "GITHUB_APP_ID",
    "GITHUB_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "OPENAI_API_KEY",
    "DATABASE_URL",
    "TELEGRAM_BOT_TOKEN",
]

# 文件路径
SETUP_MARKER = Path("config/.setup_complete")
ENV_PATH = Path(".env")

# 进程级缓存（中间件高频调用）
_state_cache: Optional[SetupState] = None
_cache_ts: float = 0
_CACHE_TTL = 5.0  # 秒


def check_setup_state() -> SetupState:
    """检测 Setup 状态

    仅通过标记文件判断（不检测 .env 内容，避免 Setup 过程中误判）
    - completed: .setup_complete 标记文件存在
    - not_configured: .env 文件不存在且标记文件不存在
    - in_progress: .env 存在但标记文件不存在
    """
    if SETUP_MARKER.is_file():
        return "completed"

    if not ENV_PATH.is_file():
        return "not_configured"

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


def mark_setup_completed() -> None:
    """写入 .setup_complete 标记文件"""
    # 如果路径存在但是目录（Docker volume mount 可能创建目录），先删除
    if SETUP_MARKER.exists() and SETUP_MARKER.is_dir():
        import shutil
        shutil.rmtree(SETUP_MARKER)
        logger.info("已删除 .setup_complete 目录（将由文件替代）")

    marker_data = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "version": "2.6.1",
    }
    SETUP_MARKER.write_text(json.dumps(marker_data, indent=2), encoding="utf-8")
    clear_bootstrap_cache()
    logger.info("Setup 已完成，标记文件已写入")


def parse_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    """解析 .env 文件为 key=value 字典

    支持双引号包裹的值和 \\n 转义（用于多行 PEM 私钥等）
    """
    result = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            value = value.strip()
            # 去除双引号包裹
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            # 还原 \n 转义为真实换行
            value = value.replace("\\n", "\n")
            result[key.strip()] = value
    return result


def get_missing_fields() -> list[str]:
    """返回 .env 中缺失或为空的必填字段列表"""
    env_values = parse_env_file()
    missing = []
    for field in REQUIRED_ENV_FIELDS:
        if not env_values.get(field, "").strip():
            missing.append(field)
    return missing


def get_current_step() -> int:
    """根据已有配置推断应从第几步开始（断点续配）

    步骤映射:
    - 0: 欢迎（始终从这开始或没有配置时）
    - 1: 数据库（DATABASE_URL 已配则跳过）
    - 2: GitHub App（三个字段已配则跳过）
    - 3: AI & 通知（OPENAI_API_KEY 已配则跳过）
    - 4: 管理员（最后一步）
    """
    env_values = parse_env_file()

    # Step 1: 数据库
    if not env_values.get("DATABASE_URL", "").strip():
        return 0

    # Step 2: GitHub App
    github_fields = [
        env_values.get("GITHUB_APP_ID", ""),
        env_values.get("GITHUB_PRIVATE_KEY", ""),
        env_values.get("GITHUB_WEBHOOK_SECRET", ""),
    ]
    if not all(f.strip() for f in github_fields):
        return 1

    # Step 3: AI & 通知
    if not env_values.get("OPENAI_API_KEY", "").strip():
        return 2

    # Step 4: 管理员（可配项）
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
