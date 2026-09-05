"""Setup Wizard 业务逻辑

处理连接测试、配置写入数据库、管理员创建和应用重启。
"""

import os
import secrets
import signal
from collections.abc import Collection, Mapping
from inspect import isawaitable
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.core.ai_providers import (
    get_ai_provider,
    list_ai_providers,
)
from backend.core.bootstrap import (
    mark_setup_completed,
)
from backend.core.time_service import now_utc

# 环境变量字段（大写） → Settings 字段名（小写）
# 注意：此映射的 values 集合应与 config.py 中 CORE_CONFIG_KEYS 保持同步。
# AI 账号、角色绑定和模型覆盖仅通过 ai_account.* 等新结构管理，Setup
# 不再把旧的 provider/key/model 字段写入 AppConfig。
_ENV_TO_SETTINGS_KEY: dict[str, str] = {
    "GITHUB_APP_ID": "github_app_id",
    "GITHUB_PRIVATE_KEY": "github_private_key",
    "GITHUB_WEBHOOK_SECRET": "github_webhook_secret",
    "TELEGRAM_ENABLED": "telegram_enabled",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_BIND_TOKEN_EXPIRE_SECONDS": "telegram_bind_token_expire_seconds",
    "EMAIL_ENABLED": "email_enabled",
    "SMTP_HOST": "smtp_host",
    "SMTP_PORT": "smtp_port",
    "SMTP_USERNAME": "smtp_username",
    "SMTP_PASSWORD": "smtp_password",
    "SMTP_FROM": "smtp_from",
    "SMTP_FROM_NAME": "smtp_from_name",
    "SMTP_SECURITY": "smtp_security",
    "NOTIFICATION_MAX_CONCURRENCY": "notification_max_concurrency",
    "NOTIFICATION_RETRY_MAX_ATTEMPTS": "notification_retry_max_attempts",
    "NOTIFICATION_RETRY_INITIAL_DELAY_SECONDS": "notification_retry_initial_delay_seconds",
    "NOTIFICATION_RETRY_BACKOFF_FACTOR": "notification_retry_backoff_factor",
    "NOTIFICATION_RATE_LIMIT_SECONDS": "notification_rate_limit_seconds",
    "WEBUI_SECRET_KEY": "webui_secret_key",
    "ACTIVITY_CURSOR_SIGNING_SECRET": "activity_cursor_signing_secret",
    "APP_DOMAIN": "app_domain",
    "APP_PORT": "app_port",
    "APP_TIMEZONE": "app_timezone",
    "LOG_LEVEL": "log_level",
    "BOT_USERNAME": "bot_username",
    "DATABASE_URL": "database_url",
    "REDIS_URL": "redis_url",
    "ENABLE_WEBUI": "enable_webui",
    "ENABLE_RAG": "enable_rag",
    "GITHUB_OAUTH_CLIENT_ID": "github_oauth_client_id",
    "GITHUB_OAUTH_CLIENT_SECRET": "github_oauth_client_secret",
    "GITHUB_OAUTH_REDIRECT_URI": "github_oauth_redirect_uri",
    "MOBILE_OAUTH_ALLOWED_REDIRECT_URIS": "mobile_oauth_allowed_redirect_uris",
    "PASSKEYS_ALLOWED_ORIGINS": "passkeys_allowed_origins",
    # 嵌入 & 重排序
    "EMBEDDING_API_KEY": "embedding_api_key",
    "EMBEDDING_BASE_URL": "embedding_base_url",
    "EMBEDDING_MODEL": "embedding_model",
    "EMBEDDING_PROVIDER": "embedding_provider",
    "EMBEDDING_DIMENSION": "embedding_dimension",
    "RERANK_API_KEY": "rerank_api_key",
    "RERANK_BASE_URL": "rerank_base_url",
    "RERANK_MODEL": "rerank_model",
    "RERANK_PROVIDER": "rerank_provider",
}

_LEGACY_CONFIG_KEYS = frozenset(
    {
        "ai_provider",
        "openai_api_key",
        "openai_api_base",
        "openai_model",
        "summary_provider",
        "summary_api_key",
        "summary_api_base",
        "summary_model",
    }
)

# GitHub App JWT 的有效期上限是 10 分钟。留出 1 分钟裕量，避免本机时钟
# 略快于 GitHub 时把 exp 判定为“too far in the future”。
_GITHUB_APP_JWT_LIFETIME_SECONDS = 9 * 60

# 环境变量字段与 Settings 字段的分组（前端步骤用）
ENV_FIELD_GROUPS = {
    "database": ["DATABASE_URL", "REDIS_URL"],
    "github": ["GITHUB_APP_ID", "GITHUB_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET"],
    # Telegram/Email are optional notification providers.  Keeping the group
    # allows old Setup payloads to import their values without making a token a
    # prerequisite for GitHub OAuth or Passkey authentication.
    "ai": ["TELEGRAM_BOT_TOKEN"],
    # RAG 配置是可选项，不参与 Setup readiness 判定。
    "rag": [],
    "admin": ["APP_DOMAIN"],
}

# Setup 页面可以从备份中预填的字段。未列出的配置仍会在完成 Setup 时恢复，
# 但不会把不需要展示的密钥（例如 WebUI 会话密钥）回传给浏览器。
# database_url 与 redis_url 会返回给前端，但前端默认不覆盖当前部署已预填的值
# （见 setup_wizard.html inspectBackup）：彻底清空数据库后重新 Setup 时保留当前
# 部署的连接地址；部署者可通过页面选项主动用备份值覆盖，以支持跨环境迁移。
SETUP_BACKUP_PREFILL_KEYS = frozenset(
    {
        "database_url",
        "redis_url",
        "github_app_id",
        "github_private_key",
        "github_webhook_secret",
        "telegram_bot_token",
        "telegram_enabled",
        "telegram_bind_token_expire_seconds",
        "email_enabled",
        "smtp_host",
        "smtp_port",
        "smtp_username",
        "smtp_from",
        "smtp_from_name",
        "smtp_security",
        "notification_max_concurrency",
        "notification_retry_max_attempts",
        "notification_retry_initial_delay_seconds",
        "notification_retry_backoff_factor",
        "notification_rate_limit_seconds",
        "app_domain",
        "bot_username",
        "github_oauth_client_id",
        "github_oauth_client_secret",
        "github_oauth_redirect_uri",
        "mobile_oauth_allowed_redirect_uris",
        "embedding_api_key",
        "embedding_base_url",
        "embedding_model",
        "rerank_api_key",
        "rerank_base_url",
        "rerank_model",
    }
)


class SetupService:
    """Setup Wizard 服务"""

    @staticmethod
    def _collect_config_items(values: Mapping[str, Any]) -> dict[str, str]:
        """Normalize Setup payload keys without touching persistence.

        Setup accepts the historical environment-variable spelling as well as
        lower-case ``Settings`` names.  Keep this phase side-effect free so it
        can be run before database initialization and before any Settings
        singleton update.
        """
        items: dict[str, str] = {}
        for env_key, env_value in values.items():
            settings_key = _ENV_TO_SETTINGS_KEY.get(env_key)
            if settings_key is None:
                settings_key = env_key if isinstance(env_key, str) and env_key.islower() else None
            if settings_key in _LEGACY_CONFIG_KEYS:
                continue
            if settings_key is None or env_value is None:
                continue
            value = str(env_value).strip()
            if value:
                items[settings_key] = value
        return items

    @classmethod
    def _validate_config_items(
        cls, values: Mapping[str, Any]
    ) -> dict[str, str]:
        """Validate Setup's system/notification values as one pure batch.

        Setup also carries non-system fields (for example embedding settings)
        which are intentionally not part of the system-config page allowlist.
        Only the overlapping system keys are sent to
        ``SystemConfigService.validate_updates``; passing the complete Setup
        payload there would reject valid legacy/non-system Setup fields.
        """
        items = cls._collect_config_items(values)
        if not items:
            return items

        from backend.services.system_config_service import (
            SYSTEM_CONFIG_UPDATE_KEYS,
            SystemConfigService,
        )

        system_items = {
            key: value
            for key, value in items.items()
            if key in SYSTEM_CONFIG_UPDATE_KEYS
        }
        validated = SystemConfigService.validate_updates(system_items)
        # Keep the canonical forms returned by the shared validator (e.g.
        # ``smtp_security`` and boolean values) for the later DB write.
        items.update(
            {
                key: value
                for key, value in validated.items()
                if value is not None
            }
        )
        return items

    def validate_config_values(self, values: Mapping[str, Any]) -> dict[str, str]:
        """Validate a Setup batch before route-level side effects.

        The save-step routes need the same preflight as
        :meth:`save_configs_to_db`, but Step 1 initializes the database before
        reaching that method.  Expose the pure validation phase so routes can
        reject malformed notification values before connection tests or DB
        initialization run.
        """
        return self._validate_config_items(values)

    async def test_database_connection(self, database_url: str) -> dict[str, Any]:
        """测试数据库连接"""
        if not database_url:
            return {"success": False, "message": "数据库连接字符串不能为空"}

        # 与 init_async_db 一致：接受所有可规范化的异步驱动连接串
        if not database_url.startswith(
            (
                "mysql+aiomysql://",
                "mysql+asyncmy://",
                "mysql://",
                "postgresql+asyncpg://",
                "postgresql://",
            )
        ):
            return {
                "success": False,
                "message": "连接字符串必须以 mysql+aiomysql://、mysql+asyncmy://、mysql://、postgresql+asyncpg:// 或 postgresql:// 开头",
            }

        # 规范化到实际使用的异步驱动（aiomysql → asyncmy），否则 SQLAlchemy 会尝试
        # 加载未安装的 aiomysql 而报 ModuleNotFoundError
        from backend.models.database import normalize_database_url

        normalized_url = normalize_database_url(database_url)

        try:
            engine = create_async_engine(normalized_url)
            async with engine.connect() as conn:
                await conn.execute(select(1))
            await engine.dispose()
            return {"success": True, "message": "数据库连接成功"}
        except Exception as e:
            error_msg = str(e)
            # 脱敏：不暴露完整连接字符串（原始与规范化后的都需脱敏）
            for secret in (database_url, normalized_url):
                if secret and secret in error_msg:
                    error_msg = error_msg.replace(secret, "***")
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
        app_id = (app_id or "").strip()
        private_key = (private_key or "").replace("\\n", "\n").strip()
        if not app_id or not private_key:
            return {"success": False, "message": "App ID 和 Private Key 不能为空"}

        try:
            import jwt

            now = int(now_utc().timestamp())
            payload = {
                "iat": now - 60,
                "exp": now + _GITHUB_APP_JWT_LIFETIME_SECONDS,
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
                    github_message = None
                    try:
                        response_body = resp.json()
                    except (AttributeError, TypeError, ValueError):
                        response_body = None
                    if isinstance(response_body, dict):
                        raw_message = response_body.get("message")
                        if isinstance(raw_message, str):
                            github_message = raw_message.strip()

                    if github_message:
                        return {
                            "success": False,
                            "message": f"GitHub App 验证失败: {github_message}",
                        }
                    return {
                        "success": False,
                        "message": (
                            "GitHub App 验证失败 (HTTP 401)，请检查 App ID、"
                            "Private Key 和服务器时间"
                        ),
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

    def list_ai_providers(self) -> list[dict[str, Any]]:
        """获取内置 AI 厂商列表。"""
        return list_ai_providers()

    async def test_ai_api(
        self,
        api_key: str,
        api_base: str = "",
        provider: str = "custom",
        model: str = "",
    ) -> dict[str, Any]:
        """测试 AI API Key 并返回可用模型（按协议族适配）.

        支持 OpenAI 兼容、Anthropic 原生、Gemini 原生三类协议族。返回结构
        与旧版一致以兼容现有前端：{success, message, models, provider,
        default_model, context_window_k}。
        """
        # 去除首尾空白或换行
        api_key = api_key.strip()
        if not api_key:
            return {"success": False, "message": "API Key 不能为空"}

        from backend.core.ai_protocol.registry import get_adapter, resolve_endpoint
        from backend.core.ai_providers import get_builtin_provider

        decl = get_builtin_provider(provider)
        endpoint = resolve_endpoint(decl, api_base)
        adapter = get_adapter(decl.family)
        provider_meta = get_ai_provider(provider)

        try:
            async with httpx.AsyncClient() as client:
                discovered = await adapter.list_models(client, endpoint, api_key)
            model_ids = [d.model_id for d in discovered]
            model_count = len(model_ids)
            selected_model = model or provider_meta.default_model
            context_window_k: int | None = None
            if selected_model:
                detail = None
                # 先从发现结果中查找 / look up in discovery results first
                for d in discovered:
                    if d.model_id == selected_model:
                        detail = d
                        break
                if detail is None:
                    try:
                        detail = await adapter.fetch_model_metadata(
                            client, endpoint, api_key, selected_model
                        )
                    except Exception as exc:
                        logger.debug(
                            "模型详情获取失败 / model detail fetch failed: {}", exc
                        )
                if detail and detail.context_window_tokens:
                    ctx_tokens = detail.context_window_tokens
                    # tokens → K tokens（>2000 视为绝对值，否则视为 K）/ to K
                    context_window_k = (
                        max(1, round(ctx_tokens / 1000))
                        if ctx_tokens > 2000
                        else ctx_tokens
                    )
            return {
                "success": True,
                "message": f"API Key 有效，可用模型: {model_count} 个",
                "models": sorted(set(model_ids)),
                "provider": provider_meta.to_public_dict(),
                "default_model": provider_meta.default_model,
                "context_window_k": context_window_k,
            }
        except Exception as e:
            from backend.core.ai_protocol.errors import (
                AIError,
                classify_context_overflow,
            )

            if isinstance(e, AIError):
                if e.category.value == "auth_invalid":
                    return {
                        "success": False,
                        "message": (
                            "API 鉴权失败：上游拒绝了当前凭证，请检查 API Key 是否过期、"
                            "令牌权限/渠道是否可用，以及 API Base URL 是否正确"
                        ),
                    }
                if e.category.value == "network":
                    return {
                        "success": False,
                        "message": "无法连接到 API 服务，请检查 API Base URL",
                    }
                return {"success": False, "message": f"验证失败: {e}"}
            logger.debug(f"AI API 测试异常: {e}")
            msg_lower = str(e).lower()
            if classify_context_overflow(msg_lower):
                return {"success": False, "message": "验证失败：上下文超限"}
            return {"success": False, "message": "验证异常，请稍后重试"}

    async def fetch_provider_models(
        self, provider: str, api_key: str, api_base: str = ""
    ) -> dict[str, Any]:
        """按厂商获取模型列表。

        内部委托 :meth:`test_ai_api` 并传入空 model，返回值结构与其一致：
        ``{success, message, provider, ...}``，成功时 ``models`` 字段包含模型 ID 列表。
        """
        return await self.test_ai_api(api_key, api_base, provider=provider)

    async def fetch_model_context_window(
        self, model: str, api_key: str, api_base: str = "", provider: str = "custom"
    ) -> int | None:
        """尝试从模型详情端点获取上下文窗口大小（K tokens，按协议族适配）."""
        if not model or not api_key:
            return None
        provider_meta = get_ai_provider(provider)
        if not provider_meta.supports_context_window:
            return None
        from backend.core.ai_protocol.registry import get_adapter, resolve_endpoint
        from backend.core.ai_providers import get_builtin_provider

        decl = get_builtin_provider(provider)
        endpoint = resolve_endpoint(decl, api_base)
        adapter = get_adapter(decl.family)
        try:
            async with httpx.AsyncClient() as client:
                detail = await adapter.fetch_model_metadata(
                    client, endpoint, api_key, model
                )
        except Exception as e:
            logger.debug(
                f"获取模型上下文窗口失败: provider={provider}, model={model}, err={e}"
            )
            return None
        if not detail or not detail.context_window_tokens:
            return None
        ctx_tokens = detail.context_window_tokens
        return max(1, round(ctx_tokens / 1000)) if ctx_tokens > 2000 else ctx_tokens

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

    async def save_configs_to_db(self, values: Mapping[str, Any]) -> int:
        """将配置项保存到数据库 AppConfig 表

        Args:
            values: 配置键值对（环境变量名大写形式 或 Settings 字段名小写形式）

        Returns:
            写入/更新的配置项数量
        """
        from backend.models.database import AppConfig, async_session

        # Validate the complete batch before opening a DB session.  In
        # particular this prevents invalid notification/SMTP values such as
        # ``inf`` or an out-of-range retry count from reaching AppConfig.
        items = self._validate_config_items(values)

        if not items:
            return 0

        saved = 0
        settings_updates: dict[str, str] = {}
        async with async_session() as session:
            # 批量查询已存在的配置项
            result = await session.execute(
                select(AppConfig).where(AppConfig.key_name.in_(list(items.keys())))
            )
            existing_map = {c.key_name: c for c in result.scalars().all()}

            for settings_key, env_value in items.items():
                existing = existing_map.get(settings_key)
                if existing:
                    if existing.key_value != env_value:
                        existing.key_value = env_value
                        saved += 1
                        settings_updates[settings_key] = env_value
                else:
                    session.add(
                        AppConfig(
                            key_name=settings_key,
                            key_value=env_value,
                        )
                    )
                    saved += 1
                    settings_updates[settings_key] = env_value

            await session.commit()

        # Apply runtime values only after the transaction succeeds.  A failed
        # commit must not leave the Settings singleton ahead of the database.
        if settings_updates:
            from backend.core.config import update_settings_field

            for settings_key, env_value in settings_updates.items():
                update_settings_field(settings_key, env_value)

        if saved:
            logger.info(f"已保存 {saved} 项配置到数据库")
        return saved

    async def init_database(self, database_url: str) -> None:
        """初始化数据库引擎并创建表

        Args:
            database_url: 数据库连接字符串
        """
        from backend.models import database as db_module
        from backend.models.database import (
            create_tables_async,
            init_async_db,
            insert_default_configs_async,
            migrate_schema_async,
        )

        if db_module.async_engine is None:
            init_async_db(database_url)
            await create_tables_async()
        await migrate_schema_async()
        await insert_default_configs_async()

    async def create_admin_user(
        self,
        github_username: str,
        telegram_id: int | None = None,
        database_url: str | None = None,
    ) -> None:
        """创建初始超级管理员

        Args:
            github_username: 管理员的 GitHub 用户名
            telegram_id: 可选的管理员 Telegram 用户 ID（旧 Setup 兼容字段）
            database_url: 数据库连接字符串
        """
        # Keep the old two-argument convenience form usable by deployments that
        # already called ``create_admin_user(username, database_url)`` while the
        # new setup flow no longer requires a Telegram ID.
        if database_url is None and isinstance(telegram_id, str):
            database_url, telegram_id = telegram_id, None
        if not database_url:
            raise ValueError("数据库连接字符串为必填项")

        # 初始化数据库引擎（可能已经初始化过）
        from backend.models import database as db_module
        from backend.models.database import (
            create_tables_async,
            init_async_db,
            insert_default_configs_async,
            migrate_schema_async,
        )
        from backend.models.identity_models import AuthProvider, UserIdentity
        from backend.models.telegram_models import TelegramUser
        from backend.services.identity_service import create_user_and_flush

        if db_module.async_engine is None:
            init_async_db(database_url)
            await create_tables_async()
        await migrate_schema_async()
        await insert_default_configs_async()

        # 创建管理员记录
        from backend.models.database import async_session

        async with async_session() as session:
            # 身份匹配优先使用显式 GitHub 用户名；Telegram ID 只在没有
            # 该用户名时作为旧 Setup 的显式兼容匹配。不能把两个独立
            # 条件拼成 AND，否则用户名已存在但 Telegram ID 变化时会
            # 创建重复账号。
            result = await session.execute(
                select(TelegramUser).where(
                    func.lower(TelegramUser.github_username)
                    == github_username.lower()
                )
            )
            existing = result.scalars().first()
            if existing is None and telegram_id is not None:
                result = await session.execute(
                    select(TelegramUser).where(
                        (TelegramUser.telegram_id == telegram_id)
                        | (
                            (TelegramUser.telegram_id == 0)
                            & (TelegramUser.github_username.is_(None))
                        )
                    )
                )
                existing = result.scalars().first()
            if existing:
                existing.role = "super_admin"
                existing.github_username = github_username
                if telegram_id is not None:
                    existing.telegram_id = telegram_id
                existing.is_active = True
                admin = existing
                logger.info(f"已将用户 {github_username} 提升为超级管理员")
            else:
                from backend.core.config import get_settings  # 延迟导入避免循环引用

                settings = get_settings()
                admin = await create_user_and_flush(
                    session,
                    lambda resolved_telegram_id: TelegramUser(
                        telegram_id=resolved_telegram_id,
                        github_username=github_username,
                        role="super_admin",
                        is_active=True,
                        daily_quota=settings.init_admin_daily_quota,
                        weekly_quota=settings.init_admin_weekly_quota,
                        monthly_quota=settings.init_admin_monthly_quota,
                        # 管理员 Issue 配额复用管理员 PR 初始配额
                        issue_daily_quota=settings.init_admin_daily_quota,
                        issue_weekly_quota=settings.init_admin_weekly_quota,
                        issue_monthly_quota=settings.init_admin_daily_quota,
                        agent_daily_quota=settings.init_admin_agent_daily_quota,
                        agent_weekly_quota=settings.init_admin_agent_weekly_quota,
                        agent_monthly_quota=settings.init_admin_agent_monthly_quota,
                    ),
                    telegram_id=telegram_id,
                )
                logger.info(f"已创建超级管理员: {github_username}")
            # AsyncSession.flush is awaitable.  Keep maintenance/test session
            # facades that expose a synchronous no-op flush compatible too.
            flush_result = session.flush()
            if isawaitable(flush_result):
                await flush_result
            # Setup only knows the configured username, so retain a synthetic
            # legacy identity until the first OAuth callback supplies the stable
            # GitHub provider_user_id.  This prevents duplicate accounts while
            # avoiding unsafe username-based merges with Telegram-only users.
            identity_result = await session.execute(
                select(UserIdentity).where(
                    UserIdentity.user_id == admin.id,
                    UserIdentity.provider == AuthProvider.GITHUB,
                )
            )
            if identity_result.scalars().first() is None:
                session.add(
                    UserIdentity(
                        user_id=admin.id,
                        provider=AuthProvider.GITHUB,
                        provider_user_id=f"legacy:{github_username.lower()}",
                        provider_username=github_username,
                    )
                )
            await session.commit()

    @staticmethod
    def _flatten_backup_values(
        backup_sections: dict[str, list[Any]] | None,
    ) -> dict[str, str | None]:
        """把已校验的备份分类展开为配置键值映射。"""
        if not backup_sections:
            return {}
        return {
            record.key: record.value
            for records in backup_sections.values()
            for record in records
        }

    def get_backup_setup_values(
        self,
        backup_sections: dict[str, list[Any]],
    ) -> dict[str, str]:
        """提取可安全回填到 Setup 表单的备份字段。"""
        values = self._flatten_backup_values(backup_sections)
        return {
            key: value
            for key, value in values.items()
            if key in SETUP_BACKUP_PREFILL_KEYS and value is not None
        }

    async def restore_backup_for_setup(
        self,
        backup_sections: dict[str, list[Any]],
        protected_keys: Collection[str] | None = None,
    ) -> Any:
        """在已初始化的数据库中恢复备份，并尽力刷新当前进程配置。

        ``protected_keys`` contains deployment-owned connection settings that
        must not be overwritten by a backup.  It is optional to keep the
        historical one-argument call compatible with maintenance scripts and
        tests.
        """
        from backend.models.database import async_session
        from backend.services.config_backup_service import (
            refresh_imported_runtime_config,
            restore_config_backup,
        )

        async with async_session() as session:
            if protected_keys:
                result = await restore_config_backup(
                    session,
                    backup_sections,
                    protected_keys=protected_keys,
                )
            else:
                result = await restore_config_backup(session, backup_sections)

        try:
            refresh_imported_runtime_config(result)
        except Exception as exc:
            # Setup 成功后必定重启，运行时刷新失败不影响已提交的备份数据。
            logger.warning("Setup 备份已恢复，但运行时配置刷新失败: {}", exc)
        return result

    async def complete_setup(
        self,
        all_config: dict[str, str],
        backup_sections: dict[str, list[Any]] | None = None,
    ) -> dict[str, Any]:
        """完成 Setup 全流程

        Args:
            all_config: 所有配置项的环境变量键值对
            backup_sections: 已严格校验的配置备份分类；为空时执行普通 Setup

        Returns:
            结果字典
        """
        all_config = dict(all_config)
        backup_values = self._flatten_backup_values(backup_sections)
        database_url = ""
        protected_connection_keys: set[str] = set()
        try:
            # Resolve deployment-owned connection values before validating the
            # batch.  The browser normally sends these values, but keeping the
            # server-side fallback prevents a crafted/old Setup client from
            # letting a backup replace the connection used by this deployment.
            explicit_database_url = str(
                all_config.get("DATABASE_URL", "") or ""
            ).strip()
            explicit_redis_url = str(all_config.get("REDIS_URL", "") or "").strip()
            from backend.core.config import Settings, get_settings

            runtime_settings = get_settings()
            deployment_database_url = str(
                getattr(runtime_settings, "database_url", "") or ""
            ).strip()
            deployment_redis_url = str(
                getattr(runtime_settings, "redis_url", "") or ""
            ).strip()
            # Settings.redis_url has a local default.  It is not a deployment
            # override unless REDIS_URL was actually provided (or a caller
            # explicitly supplied a non-default runtime value).
            default_redis_url = getattr(
                Settings.model_fields.get("redis_url"), "default", None
            )
            if (
                not os.environ.get("REDIS_URL")
                and deployment_redis_url == str(default_redis_url or "")
            ):
                deployment_redis_url = ""

            backup_database_url = str(
                backup_values.get("database_url") or ""
            ).strip()
            backup_redis_url = str(backup_values.get("redis_url") or "").strip()
            if explicit_database_url or deployment_database_url:
                protected_connection_keys.add("database_url")
            if explicit_redis_url or deployment_redis_url:
                protected_connection_keys.add("redis_url")

            database_url = (
                explicit_database_url
                or deployment_database_url
                or backup_database_url
            )
            if database_url:
                all_config["DATABASE_URL"] = database_url
            effective_redis_url = (
                explicit_redis_url or deployment_redis_url or backup_redis_url
            )
            if effective_redis_url:
                all_config["REDIS_URL"] = effective_redis_url

            # ``complete_setup`` may be called directly (without the route's
            # backup parser), so revalidate both explicit Setup values and
            # imported system values before any DB initialization or backup
            # restore side effect.
            self._validate_config_items(all_config)
            if backup_values:
                self._validate_config_items(backup_values)

            # 1. 先校验管理员和数据库信息，避免无效请求产生部分写入。
            admin_github = str(
                all_config.get("ADMIN_GITHUB_USERNAME", "") or ""
            ).strip()
            admin_telegram_id = str(
                all_config.get("ADMIN_TELEGRAM_ID", "") or ""
            ).strip()
            if not admin_github:
                return {
                    "success": False,
                    "message": "管理员 GitHub 用户名为必填项",
                }
            telegram_id_int: int | None = None
            if admin_telegram_id:
                try:
                    telegram_id_int = int(admin_telegram_id)
                except (ValueError, TypeError):
                    return {
                        "success": False,
                        "message": f"管理员 Telegram ID 格式无效: {admin_telegram_id}",
                    }

            database_url = str(all_config.get("DATABASE_URL", "") or "").strip()
            if not database_url:
                database_url = str(backup_values.get("database_url") or "").strip()
            if not database_url:
                return {"success": False, "message": "数据库连接字符串为必填项"}
            # 显式填写的数据库地址优先；从备份取得时也写回完成配置。
            all_config["DATABASE_URL"] = database_url

            # 2. 优先沿用备份中的安全密钥，仅在新部署和备份都未提供时生成。
            if not str(all_config.get("WEBUI_SECRET_KEY", "") or "").strip():
                all_config["WEBUI_SECRET_KEY"] = str(
                    backup_values.get("webui_secret_key") or secrets.token_hex(32)
                )
            # 活动可观测性 cursor HMAC 密钥：留空则新版 dispatcher 跳过，故自动生成
            if not str(
                all_config.get("ACTIVITY_CURSOR_SIGNING_SECRET", "") or ""
            ).strip():
                all_config["ACTIVITY_CURSOR_SIGNING_SECRET"] = str(
                    backup_values.get("activity_cursor_signing_secret")
                    or secrets.token_hex(32)
                )

            # 3. 表单或备份均未配置嵌入 API Key 时自动禁用 RAG。
            embedding_api_key = str(
                all_config.get("EMBEDDING_API_KEY", "")
                or backup_values.get("embedding_api_key")
                or ""
            ).strip()
            if not embedding_api_key:
                all_config["ENABLE_RAG"] = "false"
                logger.info("未配置嵌入 API Key，自动禁用 RAG 功能")

            # 4. 初始化目标数据库；旧版备份不含系统分类时使用表单中的地址。
            await self.init_database(database_url)

            # 5. 先精确恢复备份，再写入本次 Setup 表单值，使部署时的显式修改优先。
            import_result = None
            if backup_sections is not None:
                backup_protected_keys = protected_connection_keys & set(backup_values)
                if backup_protected_keys:
                    import_result = await self.restore_backup_for_setup(
                        backup_sections,
                        backup_protected_keys,
                    )
                else:
                    import_result = await self.restore_backup_for_setup(backup_sections)
                logger.info(
                    "Setup 配置备份已恢复, sections={}, created={}, updated={}, deleted={}",
                    import_result.sections,
                    import_result.created,
                    import_result.updated,
                    import_result.deleted,
                )

            # 6. 将 Setup 表单配置写入数据库。
            await self.save_configs_to_db(all_config)

            # 7. 备份不包含用户，始终由当前部署者创建/确认超级管理员。
            await self.create_admin_user(admin_github, telegram_id_int, database_url)

            # 8. 所有步骤成功后才写入 connection.json 标记完成。
            mark_setup_completed(database_url)

            # 9. 返回成功（前端开始轮询 /health）。
            response: dict[str, Any] = {
                "success": True,
                "message": "配置完成，正在重启应用...",
            }
            if import_result is not None:
                response["backup_import"] = {
                    "sections": list(import_result.sections),
                    "created": import_result.created,
                    "updated": import_result.updated,
                    "deleted": import_result.deleted,
                    "unchanged": import_result.unchanged,
                }
            return response
        except Exception as e:
            logger.error(f"Setup 完成失败: {e}")
            error_message = str(e)
            if database_url and database_url in error_message:
                error_message = error_message.replace(database_url, "***")
            return {"success": False, "message": f"配置失败: {error_message}"}

    def trigger_restart(self) -> None:
        """请求应用优雅停机并重启。

        进程由 ``python -m backend.main`` 监督循环或容器重启策略管理时，
        通过登记的 uvicorn Server 优雅停机（含 lifespan shutdown），由
        监督者重新拉起；未登记 Server（如 uvicorn CLI 直启）时退回
        SIGTERM 自行退出，交给外部环境重启。
        """
        logger.info("正在请求应用重启...")
        try:
            # 优雅停机会等待 HTTP 长连接结束；先唤醒所有 SSE 生成器，
            # 避免 EventSource 让停机无限等待。
            from backend.webui.sse import sse_manager

            sse_manager.close_all()
        except Exception as exc:
            # SSE 清理失败不能阻止重启；停机超时会继续兜底。
            logger.warning(
                "重启前关闭 SSE 长连接失败: error_type={}",
                type(exc).__name__,
            )
        from backend.core import server_runtime

        if server_runtime.request_restart():
            return
        os.kill(os.getpid(), signal.SIGTERM)


# 全局单例
setup_service = SetupService()


async def ensure_activity_cursor_signing_secret() -> str:
    """启动时自愈：``activity_cursor_signing_secret`` 为空则生成并幂等落库。

    覆盖 Setup 之前未生成该密钥的已部署实例。幂等写兼容 web/worker 容器同时
    启动的竞态：SELECT → 不存在则 INSERT → 捕获唯一约束冲突再 SELECT，
    最终以 DB 中权威值为准回填 Settings 单例。
    """
    from backend.core.config import get_settings
    from backend.models.database import AppConfig, async_session

    settings = get_settings()
    current = settings.activity_cursor_signing_secret
    if current:
        return current

    new_secret = secrets.token_hex(32)
    value: str
    generated = False
    async with async_session() as session:
        existing = (
            await session.execute(
                select(AppConfig).where(
                    AppConfig.key_name == "activity_cursor_signing_secret"
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            value = existing.key_value
        else:
            try:
                session.add(
                    AppConfig(
                        key_name="activity_cursor_signing_secret",
                        key_value=new_secret,
                    )
                )
                await session.commit()
                value = new_secret
                generated = True
            except IntegrityError:
                # 并发对手刚刚写入：回退后重新读取权威值
                await session.rollback()
                existing = (
                    await session.execute(
                        select(AppConfig).where(
                            AppConfig.key_name == "activity_cursor_signing_secret"
                        )
                    )
                ).scalar_one()
                value = existing.key_value
    settings.activity_cursor_signing_secret = value
    logger.info(
        "活动 cursor signing secret 已就绪（{}）",
        "自动生成并写入 DB" if generated else "从 DB 加载",
    )
    return value
