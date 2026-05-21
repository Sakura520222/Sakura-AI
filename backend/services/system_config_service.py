"""系统核心配置服务

封装系统核心配置的数据库读写操作，供路由层调用。
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import AppConfig
from backend.core.config import (
    CORE_CONFIG_KEYS,
    mask_sensitive_value,
    update_settings_field,
    get_settings,
    invalidate_dynamic_config_cache,
    get_all_dynamic_config_keys,
)

# 敏感键（在页面显示时脱敏）
SYSTEM_SENSITIVE_KEYS = frozenset(
    {
        "github_private_key",
        "github_webhook_secret",
        "github_oauth_client_secret",
        "telegram_bot_token",
        "webui_secret_key",
    }
)

# 需要重启才能生效的配置键
RESTART_REQUIRED_KEYS = frozenset(
    {
        "database_url",
        "redis_url",
        "github_private_key",
        "webui_secret_key",
    }
)

# 系统核心配置分组定义
SYSTEM_CONFIG_GROUPS = [
    {
        "id": "database",
        "keys": ["database_url", "redis_url"],
    },
    {
        "id": "github_app",
        "keys": [
            "github_app_id",
            "github_private_key",
            "github_webhook_secret",
        ],
    },
    {
        "id": "github_oauth",
        "keys": [
            "github_oauth_client_id",
            "github_oauth_client_secret",
            "github_oauth_redirect_uri",
        ],
    },
    {
        "id": "telegram",
        "keys": ["telegram_bot_token"],
    },
    {
        "id": "application",
        "keys": [
            "app_domain",
            "app_port",
            "log_level",
            "webui_secret_key",
            "bot_username",
        ],
    },
]


class SystemConfigService:
    """系统核心配置服务"""

    async def load_grouped_configs(
        self, db: AsyncSession
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """从数据库加载分组配置数据

        Returns:
            (groups, config_map): 分组展示数据和完整配置映射
        """
        settings = get_settings()

        result = await db.execute(
            select(AppConfig).where(AppConfig.key_name.in_(CORE_CONFIG_KEYS))
        )
        db_configs = result.scalars().all()
        config_map = {c.key_name: c.key_value for c in db_configs}

        groups = []
        for group_def in SYSTEM_CONFIG_GROUPS:
            group_id = group_def["id"]
            items = []
            for key in group_def["keys"]:
                value = config_map.get(key) or str(getattr(settings, key, "") or "")
                is_sensitive = key in SYSTEM_SENSITIVE_KEYS
                display_value = (
                    mask_sensitive_value(value) if (is_sensitive and value) else value
                )
                default_val = str(getattr(settings, key, "") or "")
                items.append(
                    {
                        "key": key,
                        "value": display_value,
                        "default": (
                            mask_sensitive_value(default_val)
                            if (is_sensitive and default_val)
                            else default_val
                        ),
                        "sensitive": is_sensitive,
                        "requires_restart": key in RESTART_REQUIRED_KEYS,
                    }
                )
            groups.append({"id": group_id, "fields": items})

        return groups, config_map

    async def save_configs(
        self,
        db: AsyncSession,
        updates: dict[str, str],
    ) -> tuple[dict[str, dict[str, str]], bool]:
        """批量保存配置到数据库

        Args:
            db: 数据库会话
            updates: {key: value} 待更新的配置

        Returns:
            (changed, needs_restart): 变更日志和是否需要重启
        """
        changed: dict[str, dict[str, str]] = {}
        needs_restart = False

        for key, val in updates.items():
            is_sensitive = key in SYSTEM_SENSITIVE_KEYS

            # Auto-sanitize app_domain: strip protocol prefix and trailing slashes
            if key == "app_domain" and val:
                val = val.strip()
                for prefix in ("https://", "http://"):
                    if val.startswith(prefix):
                        val = val[len(prefix) :]
                        break
                val = val.rstrip("/")

            result = await db.execute(
                select(AppConfig).where(AppConfig.key_name == key)
            )
            cfg = result.scalar_one_or_none()

            if cfg is None:
                cfg = AppConfig(key_name=key, key_value=val, description=key)
                db.add(cfg)
                changed[key] = {
                    "old": "(无)",
                    "new": self._mask(val, is_sensitive),
                    "raw_new": val,
                }
            elif cfg.key_value != val:
                changed[key] = {
                    "old": self._mask(cfg.key_value, is_sensitive),
                    "new": self._mask(val, is_sensitive),
                    "raw_new": val,
                }
                cfg.key_value = val

            if key in RESTART_REQUIRED_KEYS:
                needs_restart = True

        if changed:
            await db.commit()

        return changed, needs_restart

    async def apply_live_settings(self, changed: dict[str, dict[str, str]]) -> None:
        """将变更同步到 Settings 单例"""
        all_dynamic_keys = get_all_dynamic_config_keys()
        invalidate_dynamic_config_cache(all_dynamic_keys)
        for key, change in changed.items():
            if key in all_dynamic_keys or key in CORE_CONFIG_KEYS:
                update_settings_field(key, change.get("raw_new", change["new"]))

    def build_audit_log(
        self, changed: dict[str, dict[str, str]]
    ) -> dict[str, dict[str, str]]:
        """构建审计日志

        changed 中的 old/new 已在 save_configs 中脱敏，此处直接透传。
        """
        return {k: {"old": v["old"], "new": v["new"]} for k, v in changed.items()}

    @staticmethod
    def _mask(value: str, is_sensitive: bool) -> str:
        """脱敏处理"""
        return mask_sensitive_value(value) if (is_sensitive and value) else value


system_config_service = SystemConfigService()
