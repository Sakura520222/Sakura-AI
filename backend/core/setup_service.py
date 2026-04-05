"""Setup Wizard 业务逻辑

处理连接测试、.env 配置写入、管理员创建和应用重启。
"""

import os
import secrets
import shutil
import signal
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from backend.core.bootstrap import (
    ENV_PATH,
    mark_setup_completed,
)

# 环境变量字段与 Settings 字段的映射
ENV_FIELD_GROUPS = {
    "database": ["DATABASE_URL"],
    "github": ["GITHUB_APP_ID", "GITHUB_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET"],
    "ai": [
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_MODEL",
        "TELEGRAM_BOT_TOKEN",
    ],
    "rag": [
        "EMBEDDING_API_KEY",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "RERANK_API_KEY",
        "RERANK_BASE_URL",
        "RERANK_MODEL",
    ],
    "admin": ["APP_DOMAIN"],
}


def _format_env_value(value: str) -> str:
    """格式化 .env 文件中的值

    多行值（如 PEM 私钥）将实际换行替换为 \\n 并用双引号包裹，
    确保 python-dotenv 能正确解析。
    """
    if "\n" in value:
        escaped = value.replace("\n", "\\n")
        return f'"{escaped}"'
    # 包含 # 或空格的值也用引号包裹
    if "#" in value or (value and value[0] == " "):
        return f'"{value}"'
    return value


class SetupService:
    """Setup Wizard 服务"""

    async def test_database_connection(self, database_url: str) -> dict[str, Any]:
        """测试数据库连接"""
        if not database_url:
            return {"success": False, "message": "数据库连接字符串不能为空"}

        # 确保使用异步驱动
        if not database_url.startswith(("mysql+aiomysql://", "postgresql+asyncpg://")):
            return {
                "success": False,
                "message": "连接字符串必须以 mysql+aiomysql:// 或 postgresql+asyncpg:// 开头",
            }

        try:
            engine = create_async_engine(database_url, pool_pre_ping=True)
            async with engine.connect() as conn:
                await conn.execute(select(1))
            await engine.dispose()
            return {"success": True, "message": "数据库连接成功"}
        except Exception as e:
            error_msg = str(e)
            # 脱敏：不暴露完整连接字符串
            if database_url in error_msg:
                error_msg = error_msg.replace(database_url, "***")
            return {"success": False, "message": f"连接失败: {error_msg}"}

    async def test_redis_connection(self, redis_url: str) -> dict[str, Any]:
        """测试 Redis 连接"""
        if not redis_url:
            return {"success": False, "message": "Redis 连接地址不能为空"}

        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(redis_url, socket_connect_timeout=5)
            await client.ping()
            await client.aclose()
            return {"success": True, "message": "Redis 连接成功"}
        except ImportError:
            return {"success": False, "message": "缺少 redis 依赖，无法测试"}
        except Exception as e:
            error_msg = str(e)
            if redis_url in error_msg:
                error_msg = error_msg.replace(redis_url, "***")
            return {"success": False, "message": f"连接失败: {error_msg}"}

    async def test_github_app(self, app_id: str, private_key: str) -> dict[str, Any]:
        """测试 GitHub App 凭证"""
        if not app_id or not private_key:
            return {"success": False, "message": "App ID 和 Private Key 不能为空"}

        try:
            import time
            import jwt

            now = int(time.time())
            payload = {
                "iat": now - 60,
                "exp": now + (10 * 60),
                "iss": app_id,
            }
            token = jwt.encode(payload, private_key, algorithm="RS256")

            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.github.com/app",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                    },
                    timeout=10,
                )

                if resp.status_code == 200:
                    app_data = resp.json()
                    app_name = app_data.get("name", "Unknown")
                    app_slug = app_data.get("slug", "")
                    bot_username = f"{app_slug}[bot]" if app_slug else ""
                    return {
                        "success": True,
                        "message": f"GitHub App 验证成功: {app_name}",
                        "bot_username": bot_username,
                    }
                elif resp.status_code == 401:
                    return {
                        "success": False,
                        "message": "凭证无效，请检查 App ID 和 Private Key",
                    }
                else:
                    return {
                        "success": False,
                        "message": f"验证失败 (HTTP {resp.status_code})",
                    }
        except ImportError:
            return {"success": False, "message": "缺少 PyJWT 依赖，无法验证"}
        except Exception as e:
            error_msg = str(e)
            if private_key in error_msg:
                error_msg = error_msg.replace(private_key, "***")
            return {"success": False, "message": f"验证异常: {error_msg}"}

    async def test_openai_api(self, api_key: str, api_base: str) -> dict[str, Any]:
        """测试 OpenAI API Key"""
        if not api_key:
            return {"success": False, "message": "API Key 不能为空"}

        base_url = api_base or "https://api.openai.com/v1"
        if not base_url.endswith("/"):
            base_url += "/"

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{base_url}models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    raw_models = data.get("data", [])
                    model_count = len(raw_models)
                    # 提取模型 ID 列表（用于前端选择）
                    model_ids = sorted(
                        [m.get("id", "") for m in raw_models if m.get("id")]
                    )
                    return {
                        "success": True,
                        "message": f"API Key 有效，可用模型: {model_count} 个",
                        "models": model_ids,
                    }
                elif resp.status_code == 401:
                    return {"success": False, "message": "API Key 无效"}
                else:
                    return {
                        "success": False,
                        "message": f"验证失败 (HTTP {resp.status_code})",
                    }
        except httpx.ConnectError:
            return {
                "success": False,
                "message": f"无法连接到 {base_url}，请检查 API Base URL",
            }
        except Exception as e:
            return {"success": False, "message": f"验证异常: {e}"}

    async def test_telegram_bot(self, bot_token: str) -> dict[str, Any]:
        """测试 Telegram Bot Token"""
        if not bot_token:
            return {"success": False, "message": "Bot Token 不能为空"}

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.telegram.org/bot{bot_token}/getMe",
                    timeout=10,
                )
                data = resp.json()
                if data.get("ok"):
                    bot_info = data.get("result", {})
                    bot_name = bot_info.get("username", "Unknown")
                    return {
                        "success": True,
                        "message": f"Bot 验证成功: @{bot_name}",
                    }
                else:
                    error_desc = data.get("description", "未知错误")
                    return {"success": False, "message": f"验证失败: {error_desc}"}
        except Exception as e:
            return {"success": False, "message": f"验证异常: {e}"}

    def write_env_config(self, values: dict[str, str]) -> None:
        """写入 .env 配置（合并模式，原子写入）

        Args:
            values: 环境变量键值对
        """
        # 1. 读取已有 .env 内容
        existing = {}
        if ENV_PATH.exists():
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    value = value.strip()
                    # 去除双引号包裹
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    # 还原 \n 转义为真实换行
                    value = value.replace("\\n", "\n")
                    existing[key.strip()] = value

        # 2. 合并新值
        existing.update(values)

        # 3. 原子写入
        tmp_path = ENV_PATH.with_suffix(".env.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write("# Sakura AI Reviewer 配置文件\n")
                f.write("# 由 Setup Wizard 生成，也可手动编辑\n\n")

                # 按分组写入
                sections = [
                    (
                        "GitHub App 配置",
                        [
                            "GITHUB_APP_ID",
                            "GITHUB_PRIVATE_KEY",
                            "GITHUB_WEBHOOK_SECRET",
                        ],
                    ),
                    ("数据库配置", ["DATABASE_URL"]),
                    ("Redis 配置", ["REDIS_URL"]),
                    ("应用配置", ["APP_DOMAIN", "APP_PORT", "LOG_LEVEL"]),
                    (
                        "OpenAI 配置",
                        ["OPENAI_API_BASE", "OPENAI_API_KEY", "OPENAI_MODEL"],
                    ),
                    (
                        "RAG 嵌入与重排序配置",
                        [
                            "ENABLE_RAG",
                            "EMBEDDING_API_KEY",
                            "EMBEDDING_BASE_URL",
                            "EMBEDDING_MODEL",
                            "RERANK_API_KEY",
                            "RERANK_BASE_URL",
                            "RERANK_MODEL",
                        ],
                    ),
                    ("WebUI 配置", ["WEBUI_SECRET_KEY"]),
                    (
                        "Telegram Bot 配置",
                        ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ADMIN_USER_IDS"],
                    ),
                    (
                        "GitHub OAuth 配置",
                        [
                            "GITHUB_OAUTH_CLIENT_ID",
                            "GITHUB_OAUTH_CLIENT_SECRET",
                            "GITHUB_OAUTH_REDIRECT_URI",
                        ],
                    ),
                ]

                written_keys = set()
                for section_title, section_keys in sections:
                    section_items = {
                        k: v for k, v in existing.items() if k in section_keys
                    }
                    if section_items:
                        f.write(f"# {section_title}\n")
                        for k, v in section_items.items():
                            f.write(f"{k}={_format_env_value(v)}\n")
                            written_keys.add(k)
                        f.write("\n")

                # 写入未分组的配置
                remaining = {k: v for k, v in existing.items() if k not in written_keys}
                if remaining:
                    f.write("# 其他配置\n")
                    for k, v in remaining.items():
                        f.write(f"{k}={_format_env_value(v)}\n")

            shutil.move(str(tmp_path), str(ENV_PATH))
            logger.info(f".env 配置已写入 ({len(values)} 项)")
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    async def create_admin_user(
        self, github_username: str, telegram_id: int, database_url: str
    ) -> None:
        """创建初始超级管理员

        Args:
            github_username: 管理员的 GitHub 用户名
            telegram_id: 管理员的 Telegram 用户 ID
            database_url: 数据库连接字符串
        """
        from backend.models.database import (
            init_async_db,
            create_tables_async,
            insert_default_configs_async,
        )
        from backend.models.telegram_models import TelegramUser

        # 初始化数据库引擎（可能已经初始化过）
        from backend.models import database as db_module

        if db_module.async_engine is None:
            init_async_db(database_url)
            await create_tables_async()
            await insert_default_configs_async()

        # 创建管理员记录
        from backend.models.database import async_session

        async with async_session() as session:
            # 检查是否已存在（按 github_username、telegram_id 或 telegram_id=0 的占位记录）
            result = await session.execute(
                select(TelegramUser).where(
                    (TelegramUser.github_username == github_username)
                    | (TelegramUser.telegram_id == telegram_id)
                    | (TelegramUser.telegram_id == 0)
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.role = "super_admin"
                existing.github_username = github_username
                existing.telegram_id = telegram_id
                existing.is_active = True
                logger.info(f"已将用户 {github_username} 提升为超级管理员")
            else:
                admin = TelegramUser(
                    telegram_id=telegram_id,
                    github_username=github_username,
                    role="super_admin",
                    is_active=True,
                    daily_quota=999,
                    weekly_quota=9999,
                    monthly_quota=99999,
                )
                session.add(admin)
                logger.info(f"已创建超级管理员: {github_username}")
            await session.commit()

    async def complete_setup(self, all_config: dict[str, str]) -> dict[str, Any]:
        """完成 Setup 全流程

        Args:
            all_config: 所有配置项的环境变量键值对

        Returns:
            结果字典
        """
        try:
            # 1. 自动生成 WEBUI_SECRET_KEY
            all_config.setdefault("WEBUI_SECRET_KEY", secrets.token_hex(32))

            # 2. 未配置嵌入 API Key 时自动禁用 RAG，避免空 Key 调用报错
            if not all_config.get("EMBEDDING_API_KEY", "").strip():
                all_config["ENABLE_RAG"] = "false"
                logger.info("未配置嵌入 API Key，自动禁用 RAG 功能")

            # 3. 写入所有配置到 .env
            self.write_env_config(all_config)

            database_url = all_config.get("DATABASE_URL", "")
            admin_github = all_config.get("ADMIN_GITHUB_USERNAME", "")
            admin_telegram_id = all_config.get("ADMIN_TELEGRAM_ID", "")

            # 3. 初始化数据库并创建管理员
            if database_url and admin_github and admin_telegram_id:
                await self.create_admin_user(
                    admin_github, int(admin_telegram_id), database_url
                )

            # 4. 写入完成标记
            mark_setup_completed()

            # 5. 返回成功（前端开始轮询 /health）
            return {"success": True, "message": "配置完成，正在重启应用..."}
        except Exception as e:
            logger.error(f"Setup 完成失败: {e}")
            return {"success": False, "message": f"配置失败: {e}"}

    def trigger_restart(self) -> None:
        """触发应用重启（通过 SIGTERM 信号）"""
        logger.info("Setup 完成，正在触发应用重启...")
        os.kill(os.getpid(), signal.SIGTERM)


# 全局单例
setup_service = SetupService()
