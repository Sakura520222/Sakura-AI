"""系统核心配置服务

封装系统核心配置的数据库读写操作，供路由层调用。
"""

import math
import re
from collections.abc import Mapping
from typing import Any, get_args

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import (
    CORE_CONFIG_KEYS,
    Settings,
    _get_field_type,
    get_all_dynamic_config_keys,
    get_settings,
    invalidate_dynamic_config_cache,
    mask_sensitive_value,
    sanitize_domain,
    update_settings_field,
)
from backend.core.time_service import InvalidTimezoneError, resolve_timezone
from backend.models.database import AppConfig

# 敏感键（在页面显示时脱敏）
SYSTEM_SENSITIVE_KEYS = frozenset(
    {
        "github_private_key",
        "github_webhook_secret",
        "github_oauth_client_secret",
        "telegram_bot_token",
        "smtp_password",
        "webui_secret_key",
        "activity_cursor_signing_secret",
        "star_aid_github_app_client_secret",
    }
)

# 需要重启才能生效的配置键
RESTART_REQUIRED_KEYS = frozenset(
    {
        "database_url",
        "redis_url",
        "github_private_key",
        "telegram_enabled",
        "telegram_bot_token",
        "webui_secret_key",
        "activity_cursor_signing_secret",
        "app_timezone",
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
            "mobile_oauth_allowed_redirect_uris",
        ],
    },
    {
        "id": "star_aid_app",
        "keys": [
            "star_aid_github_app_client_id",
            "star_aid_github_app_client_secret",
            "star_aid_github_app_callback_url",
        ],
    },
    {
        "id": "telegram",
        "keys": [
            "telegram_enabled",
            "telegram_bot_token",
            "telegram_bind_token_expire_seconds",
        ],
    },
    {
        "id": "email",
        "keys": [
            "email_enabled",
            "smtp_host",
            "smtp_port",
            "smtp_username",
            "smtp_password",
            "smtp_from",
            "smtp_from_name",
            "smtp_security",
        ],
    },
    {
        "id": "notifications",
        "keys": [
            "notification_max_concurrency",
            "notification_retry_max_attempts",
            "notification_retry_initial_delay_seconds",
            "notification_retry_backoff_factor",
            "notification_rate_limit_seconds",
        ],
    },
    {
        "id": "application",
        "keys": [
            "app_domain",
            "app_port",
            "app_timezone",
            "log_level",
            "webui_secret_key",
            "activity_cursor_signing_secret",
            "bot_username",
        ],
    },
]

# 「系统配置」页面实际管理的完整键集合。
SYSTEM_CONFIG_KEYS = frozenset(
    key for group in SYSTEM_CONFIG_GROUPS for key in group["keys"]
)

# ``save_configs`` is also used by the Star Aid feature toggle.  Keep that
# existing internal caller in the allowlist while rejecting arbitrary keys
# passed by future callers.
SYSTEM_CONFIG_UPDATE_KEYS = SYSTEM_CONFIG_KEYS | frozenset({"star_aid_enabled"})

_INTEGER_RE = re.compile(r"^[+-]?[0-9]+$")


class SystemConfigValidationError(ValueError):
    """系统配置预校验失败，并携带可供 WebUI 使用的 toast 上下文。"""

    def __init__(
        self,
        key: str,
        reason: str,
        *,
        toast_key: str = "toast.config_validation_failed",
        **context: object,
    ) -> None:
        self.key = key
        self.reason = reason
        self.toast_key = toast_key
        self.context = context
        super().__init__(f"{key}: {reason}")


class SystemConfigService:
    """系统核心配置服务"""

    @staticmethod
    def _field_allows_none(key: str) -> bool:
        """返回 Settings 字段是否显式允许 ``None``。"""
        field_info = Settings.model_fields.get(key)
        if field_info is None:
            return False
        return type(None) in get_args(field_info.annotation)

    @staticmethod
    def _numeric_constraints(
        key: str,
    ) -> tuple[tuple[str, float | int], ...]:
        """读取 Settings Field 的数值约束，并补充 app_port 的历史约束。"""
        constraints: dict[str, float | int] = {}
        if key == "app_port":
            # app_port predates the Field(ge=..., le=...) declarations used by
            # the notification settings, but the same bounds are part of its
            # WebUI contract.
            constraints.update({"ge": 1, "le": 65535})

        field_info = Settings.model_fields.get(key)
        for metadata in getattr(field_info, "metadata", ()):
            for name in ("gt", "ge", "lt", "le"):
                value = getattr(metadata, name, None)
                if value is not None:
                    constraints[name] = value

        return tuple(constraints.items())

    @classmethod
    def _validate_value(cls, key: str, raw: Any) -> str | None:
        """纯校验并标准化单个配置值，不访问 DB、缓存或 Settings 实例。"""
        if key not in SYSTEM_CONFIG_UPDATE_KEYS:
            raise SystemConfigValidationError(
                key,
                "配置项不允许在此处修改",
                toast_key="toast.value_invalid",
                field_key=key,
            )

        if raw is None:
            if cls._field_allows_none(key):
                return None
            raise SystemConfigValidationError(
                key,
                "值不能为空",
                toast_key="toast.value_invalid",
                field_key=key,
            )

        expected_type = _get_field_type(key)
        value = str(raw).strip()

        if expected_type is bool:
            bool_values = {
                "true": "true",
                "false": "false",
                "1": "true",
                "0": "false",
                "yes": "true",
                "no": "false",
            }
            normalized = bool_values.get(value.lower())
            if normalized is None:
                raise SystemConfigValidationError(
                    key,
                    "值必须是 true 或 false",
                    toast_key="toast.value_invalid",
                    field_key=key,
                )
            return normalized

        numeric_value: int | float | None = None
        if expected_type is int:
            # ``int('1.0')`` already fails, but explicitly reject Python
            # floats too: accepting 1.0 would make the persisted contract
            # depend on the caller's input representation.
            if isinstance(raw, (bool, float)):
                raise SystemConfigValidationError(
                    key,
                    "必须是整数",
                    toast_key="toast.numeric_required",
                    field_key=key,
                )
            if isinstance(raw, int):
                numeric_value = raw
            elif not _INTEGER_RE.fullmatch(value):
                raise SystemConfigValidationError(
                    key,
                    "必须是整数",
                    toast_key="toast.numeric_required",
                    field_key=key,
                )
            else:
                numeric_value = int(value)
        elif expected_type is float:
            if isinstance(raw, bool):
                raise SystemConfigValidationError(
                    key,
                    "必须是有限数值",
                    toast_key="toast.numeric_required",
                    field_key=key,
                )
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                raise SystemConfigValidationError(
                    key,
                    "必须是有限数值",
                    toast_key="toast.numeric_required",
                    field_key=key,
                ) from None
            if not math.isfinite(numeric_value):
                raise SystemConfigValidationError(
                    key,
                    "必须是有限数值",
                    toast_key="toast.numeric_required",
                    field_key=key,
                )

        if numeric_value is not None:
            constraints = dict(cls._numeric_constraints(key))
            lower = constraints.get("ge", constraints.get("gt"))
            upper = constraints.get("le", constraints.get("lt"))
            lower_invalid = lower is not None and (
                numeric_value < lower
                if "ge" in constraints
                else numeric_value <= lower
            )
            upper_invalid = upper is not None and (
                numeric_value > upper
                if "le" in constraints
                else numeric_value >= upper
            )
            if lower_invalid or upper_invalid:
                if lower is not None and upper is not None:
                    toast_key = "toast.value_range"
                    context = {
                        "field_key": key,
                        "min_v": lower,
                        "max_v": upper,
                    }
                elif lower is not None:
                    toast_key = (
                        "toast.value_min_required"
                        if "ge" in constraints
                        else "toast.config_validation_failed"
                    )
                    context = {"field_key": key, "min_v": lower}
                else:
                    toast_key = "toast.config_validation_failed"
                    context = {"field_key": key, "max_v": upper}
                if lower_invalid:
                    relation = "大于等于" if "ge" in constraints else "大于"
                    reason = f"必须{relation} {lower}"
                else:
                    relation = "小于等于" if "le" in constraints else "小于"
                    reason = f"必须{relation} {upper}"
                raise SystemConfigValidationError(
                    key,
                    reason,
                    toast_key=toast_key,
                    **context,
                )

        # These choices are also checked by the form route.  Keeping them in
        # the service protects direct callers (Star Aid, imports and tests).
        if key == "smtp_security":
            value = value.lower()
            if value not in {"ssl", "starttls", "none"}:
                raise SystemConfigValidationError(
                    key,
                    "安全模式无效",
                    toast_key="system_config.invalid_smtp_security",
                )
        elif key == "log_level":
            value = value.upper()
            if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                raise SystemConfigValidationError(
                    key,
                    "日志级别无效",
                    toast_key="system_config.invalid_log_level",
                )
        elif key == "app_port":
            # Store the canonical integer representation, as the old route did.
            value = str(numeric_value)
        elif key == "database_url" and value:
            if not value.startswith(
                (
                    "mysql+aiomysql://",
                    "mysql+asyncmy://",
                    "mysql://",
                    "postgresql+asyncpg://",
                    "postgresql://",
                )
            ):
                raise SystemConfigValidationError(
                    key,
                    "数据库连接字符串格式无效",
                    toast_key="system_config.invalid_db_url",
                )
        elif key == "app_timezone":
            try:
                # Validate with the exact startup resolver, but persist the
                # user's original IANA/system spelling for audit/UI.
                resolve_timezone(value)
            except InvalidTimezoneError as exc:
                raise SystemConfigValidationError(
                    key,
                    "无效应用时区",
                    toast_key="system_config.invalid_timezone",
                ) from exc

        if key == "app_domain" and value:
            value = sanitize_domain(value)

        return value

    @classmethod
    def validate_updates(cls, updates: Mapping[str, Any]) -> dict[str, str | None]:
        """预校验整批更新，保证失败时不触碰数据库或运行时单例。"""
        if not isinstance(updates, Mapping):
            raise ValueError("配置更新必须是键值映射")
        return {key: cls._validate_value(key, raw) for key, raw in updates.items()}

    async def load_grouped_configs(
        self, db: AsyncSession
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """从数据库加载分组配置数据

        Returns:
            (groups, config_map): 分组展示数据和完整配置映射
        """
        settings = get_settings()

        result = await db.execute(
            select(AppConfig).where(AppConfig.key_name.in_(SYSTEM_CONFIG_KEYS))
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
        updates: Mapping[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """批量保存配置到数据库

        Args:
            db: 数据库会话
            updates: {key: value} 待更新的配置

        Returns:
            (changed, needs_restart): 变更日志和是否需要重启
        """
        # This must stay a pure phase.  In particular, do not query or mutate
        # ORM rows, commit, invalidate caches, or update the Settings singleton
        # until every item in the batch has passed type and bound validation.
        validated_updates = self.validate_updates(updates)
        if not validated_updates:
            return {}, False

        # Read all current rows before mutating any of them.  A later query
        # failure therefore cannot leave an earlier item partially changed in
        # the current transaction/session.
        current_rows: dict[str, AppConfig | None] = {}
        for key in validated_updates:
            result = await db.execute(
                select(AppConfig).where(AppConfig.key_name == key)
            )
            current_rows[key] = result.scalar_one_or_none()

        changed: dict[str, dict[str, Any]] = {}
        for key, val in validated_updates.items():
            is_sensitive = key in SYSTEM_SENSITIVE_KEYS
            cfg = current_rows[key]

            if cfg is None:
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

        # Apply the already validated batch as one mutation phase.
        for key, val in validated_updates.items():
            cfg = current_rows[key]
            if cfg is None:
                db.add(AppConfig(key_name=key, key_value=val, description=key))
            elif key in changed:
                cfg.key_value = val

        if changed:
            await db.commit()

        return changed, bool(set(changed) & RESTART_REQUIRED_KEYS)

    async def apply_live_settings(self, changed: dict[str, dict[str, str]]) -> None:
        """将变更同步到 Settings 单例"""
        all_dynamic_keys = get_all_dynamic_config_keys()
        invalidate_dynamic_config_cache(all_dynamic_keys)
        for key, change in changed.items():
            # Restart-required settings are persisted and audited but must not
            # mutate this process's frozen runtime context.
            if key in RESTART_REQUIRED_KEYS:
                continue
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
