"""用户信息备份、校验和导入服务。

用户备份与全局配置备份使用独立格式。用户备份只覆盖用户本身、个人配置、
两步验证和 Passkey，不包含仓库订阅、配额使用日志、支付或审计数据。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import (
    DYNAMIC_CONFIG_LABELS,
    USER_DYNAMIC_CONFIG_KEYS,
    get_settings,
    invalidate_user_dynamic_config_cache,
    validate_user_dynamic_config_value,
)
from backend.core.time_service import format_rfc3339, now_utc, parse_rfc3339
from backend.models.database import UserConfig, WebUIConfig
from backend.models.identity_models import NotificationEndpoint, UserIdentity
from backend.models.telegram_models import (
    TelegramUser,
    UserRecoveryCode,
    UserRole,
    UserWebAuthnCredential,
)
from backend.services.identity_service import create_user_and_flush
from backend.services.two_factor_service import (
    TwoFactorNotConfiguredError,
    decrypt_totp_secret,
    encrypt_totp_secret,
)
from backend.services.webauthn_service import credential_id_hash

USER_BACKUP_FORMAT = "sakura-ai-user-backup"
USER_BACKUP_VERSION = 2
LEGACY_USER_BACKUP_VERSION = 1
SUPPORTED_USER_BACKUP_VERSIONS = frozenset({LEGACY_USER_BACKUP_VERSION, USER_BACKUP_VERSION})
USER_BACKUP_SCOPE = "users"
USER_BACKUP_MAX_BYTES = 5 * 1024 * 1024
USER_BACKUP_MAX_USERS = 5000
USER_BACKUP_MAX_RECOVERY_CODES = 100
USER_BACKUP_MAX_PASSKEYS = 100
USER_BACKUP_MAX_CONFIGS = 100

VALID_USER_ROLES = frozenset(role.value for role in UserRole)
VALID_WEBUI_THEMES = frozenset({"light", "dark", "system"})
VALID_ITEMS_PER_PAGE = frozenset({10, 20, 50, 100})
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_RECOVERY_HASH_LENGTHS = frozenset({64, 128})
_BIGINT_MIN = -(2**63)
_BIGINT_MAX = 2**63 - 1
_INT_MAX = 2**31 - 1
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

_PROFILE_INT_FIELDS = (
    "daily_quota",
    "weekly_quota",
    "monthly_quota",
    "daily_used",
    "weekly_used",
    "monthly_used",
    "issue_daily_quota",
    "issue_weekly_quota",
    "issue_monthly_quota",
    "issue_daily_used",
    "issue_weekly_used",
    "issue_monthly_used",
    "agent_daily_quota",
    "agent_weekly_quota",
    "agent_monthly_quota",
    "agent_daily_used",
    "agent_weekly_used",
    "agent_monthly_used",
)
_PROFILE_TIMESTAMP_FIELDS = (
    "last_reset_daily",
    "last_reset_weekly",
    "last_reset_monthly",
    "last_reset_issue_daily",
    "last_reset_issue_weekly",
    "last_reset_issue_monthly",
    "last_reset_agent_daily",
    "last_reset_agent_weekly",
    "last_reset_agent_monthly",
    "created_at",
    "updated_at",
)
_PROFILE_FIELDS = (
    "role",
    *_PROFILE_INT_FIELDS,
    *_PROFILE_TIMESTAMP_FIELDS,
    "is_active",
)

_PROFILE_DEFAULTS: dict[str, Any] = {
    "role": UserRole.USER.value,
    "daily_quota": 10,
    "weekly_quota": 50,
    "monthly_quota": 200,
    "daily_used": 0,
    "weekly_used": 0,
    "monthly_used": 0,
    "issue_daily_quota": 20,
    "issue_weekly_quota": 80,
    "issue_monthly_quota": 300,
    "issue_daily_used": 0,
    "issue_weekly_used": 0,
    "issue_monthly_used": 0,
    "agent_daily_quota": 1,
    "agent_weekly_quota": 2,
    "agent_monthly_quota": 5,
    "agent_daily_used": 0,
    "agent_weekly_used": 0,
    "agent_monthly_used": 0,
    "is_active": True,
}


class UserBackupError(ValueError):
    """用户备份内容无效或无法安全导入。"""


@dataclass(frozen=True)
class UserImportResult:
    """用户备份导入结果。"""

    users_created: int
    users_updated: int
    users_unchanged: int
    user_configs_created: int
    user_configs_updated: int
    user_configs_deleted: int
    webui_configs_created: int
    webui_configs_updated: int
    webui_configs_deleted: int
    recovery_codes_imported: int
    recovery_codes_deleted: int
    passkeys_created: int
    passkeys_updated: int
    recovery_codes_portable: bool
    affected_user_ids: tuple[int, ...]
    recovery_codes_skipped: int = 0

    @property
    def created(self) -> int:
        return (
            self.users_created
            + self.user_configs_created
            + self.webui_configs_created
            + self.passkeys_created
            + self.recovery_codes_imported
        )

    @property
    def updated(self) -> int:
        return (
            self.users_updated
            + self.user_configs_updated
            + self.webui_configs_updated
            + self.passkeys_updated
        )

    @property
    def deleted(self) -> int:
        return (
            self.user_configs_deleted
            + self.webui_configs_deleted
            + self.recovery_codes_deleted
        )

    @property
    def unchanged(self) -> int:
        return self.users_unchanged

    @property
    def total_users(self) -> int:
        return self.users_created + self.users_updated + self.users_unchanged

    @property
    def passkeys_imported(self) -> int:
        return self.passkeys_created + self.passkeys_updated


def _datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise UserBackupError("备份时间必须包含 UTC offset")
    return format_rfc3339(value.astimezone(UTC))


def _parse_datetime(value: Any, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 80:
        raise UserBackupError(f"{label} 时间格式无效")
    try:
        parsed = parse_rfc3339(value)
    except ValueError as exc:
        raise UserBackupError(f"{label} 时间格式无效") from exc
    return parsed


def _validate_string(
    value: Any,
    label: str,
    maximum: int,
    *,
    allow_none: bool = False,
    allow_empty: bool = True,
) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, str):
        raise UserBackupError(f"{label} 必须是字符串")
    if not allow_empty and not value:
        raise UserBackupError(f"{label} 不能为空")
    if len(value) > maximum:
        raise UserBackupError(f"{label} 过长")


def _validate_bool(value: Any, label: str) -> None:
    if not isinstance(value, bool):
        raise UserBackupError(f"{label} 必须是布尔值")


def _validate_int(
    value: Any,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UserBackupError(f"{label} 必须是整数")
    if minimum is not None and value < minimum:
        raise UserBackupError(f"{label} 数值过小")
    if maximum is not None and value > maximum:
        raise UserBackupError(f"{label} 数值过大")


def _recovery_code_hash_key_fingerprint() -> str:
    """Return a non-secret fingerprint for the recovery-code HMAC key."""
    return hashlib.sha256(get_settings().webui_secret_key.encode("utf-8")).hexdigest()


def _profile_from_user(user: TelegramUser) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    for field in _PROFILE_FIELDS:
        value = getattr(user, field, None)
        if field in _PROFILE_TIMESTAMP_FIELDS:
            value = _datetime_to_iso(value)
        profile[field] = value
    return profile


def _metadata_from_row(row: Any) -> Any:
    """Return endpoint/identity metadata without failing an old row export."""
    raw = getattr(row, "metadata_json", None)
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Metadata is informational.  Preserve opaque legacy values rather
            # than making an otherwise valid user backup impossible to export.
            return raw
    return raw


def _normalise_user_record(record: dict[str, Any]) -> dict[str, Any]:
    """Add the v2 identity/endpoint sections while accepting v1-shaped input."""
    normalised = dict(record)
    identity = dict(normalised.get("identity") or {})
    # Some early development backups put these fields at the user level.
    for key in ("telegram_id", "github_username", "email", "email_verified"):
        if key not in identity and key in normalised:
            identity[key] = normalised[key]
    normalised["identity"] = identity
    normalised["identities"] = list(normalised.get("identities") or [])
    normalised["notification_endpoints"] = list(
        normalised.get("notification_endpoints") or []
    )
    return normalised


def _real_telegram_id(value: Any) -> int | None:
    """Hide non-positive legacy compatibility sentinels from backups."""

    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _personal_config_from_rows(
    user_configs: list[UserConfig], webui_config: WebUIConfig | None
) -> dict[str, Any]:
    dynamic_overrides: list[dict[str, Any]] = []
    for row in sorted(user_configs, key=lambda item: item.config_key):
        if row.config_key not in USER_DYNAMIC_CONFIG_KEYS:
            raise UserBackupError(
                f"用户 {row.user_id} 包含不支持导出的配置项 {row.config_key}"
            )
        if row.config_value is not None:
            try:
                value = validate_user_dynamic_config_value(
                    row.config_key, row.config_value
                )
            except ValueError as exc:
                raise UserBackupError(
                    f"用户 {row.user_id} 的配置项 {row.config_key} 无效"
                ) from exc
        else:
            value = None
        dynamic_overrides.append(
            {
                "key": row.config_key,
                "value": value,
                "description": row.description,
            }
        )
    webui = None
    if webui_config is not None:
        webui = {
            "theme": webui_config.theme,
            "language": webui_config.language,
            "items_per_page": webui_config.items_per_page,
        }
    return {"dynamic_overrides": dynamic_overrides, "webui": webui}


def _two_factor_from_user(
    user: TelegramUser, recovery_codes: list[UserRecoveryCode]
) -> dict[str, Any]:
    secret = None
    if user.totp_secret_encrypted:
        try:
            secret = decrypt_totp_secret(user.totp_secret_encrypted)
        except TwoFactorNotConfiguredError as exc:
            raise UserBackupError(
                f"用户 {user.telegram_id} 的 TOTP 密钥无法解密，已停止导出"
            ) from exc
    if user.totp_enabled and not secret:
        raise UserBackupError(
            f"用户 {user.telegram_id} 标记为已启用 TOTP，但缺少可导出的密钥"
        )

    return {
        "mfa_required": bool(user.mfa_required),
        "totp_enabled": bool(user.totp_enabled),
        "totp_secret": secret,
        "totp_enabled_at": _datetime_to_iso(user.totp_enabled_at),
        "totp_last_used_step": user.totp_last_used_step,
        "recovery_codes": [
            {
                "code_hash": row.code_hash,
                "used_at": _datetime_to_iso(row.used_at),
                "created_at": _datetime_to_iso(row.created_at),
            }
            for row in sorted(recovery_codes, key=lambda item: item.id or 0)
        ],
    }


def _passkeys_from_rows(
    passkeys: list[UserWebAuthnCredential],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in sorted(passkeys, key=lambda item: item.id or 0):
        credential_hash = row.credential_id_hash or credential_id_hash(
            row.credential_id
        )
        records.append(
            {
                "credential_id": row.credential_id,
                "credential_id_hash": credential_hash,
                "public_key": row.public_key,
                "sign_count": row.sign_count,
                "transports": row.transports,
                "device_name": row.device_name,
                "backed_up": bool(row.backed_up),
                "created_at": _datetime_to_iso(row.created_at),
                "last_used_at": _datetime_to_iso(row.last_used_at),
            }
        )
    return records


def build_user_backup_document(
    users: list[dict[str, Any]],
    *,
    exported_at: datetime | None = None,
    recovery_code_hash_key_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Build a stable JSON-compatible user backup document from records."""
    timestamp = exported_at or now_utc()
    sorted_users = sorted(
        [_normalise_user_record(user) for user in users],
        key=lambda item: (
            item.get("identity", {}).get("telegram_id") is None,
            item.get("identity", {}).get("telegram_id") or 0,
            item.get("identity", {}).get("github_username") or "",
        ),
    )
    contains_sensitive_values = bool(sorted_users) or any(
        bool(user.get("two_factor", {}).get("totp_secret"))
        or bool(user.get("two_factor", {}).get("recovery_codes"))
        or bool(user.get("passkeys"))
        for user in sorted_users
    )
    return {
        "format": USER_BACKUP_FORMAT,
        "version": USER_BACKUP_VERSION,
        "exported_at": format_rfc3339(timestamp),
        "scope": USER_BACKUP_SCOPE,
        "user_count": len(sorted_users),
        "contains_sensitive_values": contains_sensitive_values,
        "recovery_code_hash_key_fingerprint": (
            recovery_code_hash_key_fingerprint
            if recovery_code_hash_key_fingerprint is not None
            else _recovery_code_hash_key_fingerprint()
        ),
        "users": sorted_users,
    }


async def export_user_backup(db: AsyncSession) -> dict[str, Any]:
    """Export all users and the explicitly supported related information."""
    users_result = await db.execute(select(TelegramUser).order_by(TelegramUser.id))
    users = list(users_result.scalars().all())
    if len(users) > USER_BACKUP_MAX_USERS:
        raise UserBackupError("用户数量超过备份上限")
    user_ids = [user.id for user in users]

    configs_by_user: dict[int, list[UserConfig]] = defaultdict(list)
    webui_by_user: dict[int, WebUIConfig] = {}
    recovery_by_user: dict[int, list[UserRecoveryCode]] = defaultdict(list)
    passkeys_by_user: dict[int, list[UserWebAuthnCredential]] = defaultdict(list)
    identities_by_user: dict[int, list[UserIdentity]] = defaultdict(list)
    endpoints_by_user: dict[int, list[NotificationEndpoint]] = defaultdict(list)

    if user_ids:
        config_result = await db.execute(
            select(UserConfig)
            .where(UserConfig.user_id.in_(user_ids))
            .order_by(UserConfig.user_id, UserConfig.config_key)
        )
        for row in config_result.scalars().all():
            configs_by_user[row.user_id].append(row)

        webui_result = await db.execute(
            select(WebUIConfig)
            .where(WebUIConfig.user_id.in_(user_ids))
            .order_by(WebUIConfig.user_id)
        )
        for row in webui_result.scalars().all():
            webui_by_user[row.user_id] = row

        recovery_result = await db.execute(
            select(UserRecoveryCode)
            .where(UserRecoveryCode.user_id.in_(user_ids))
            .order_by(UserRecoveryCode.user_id, UserRecoveryCode.id)
        )
        for row in recovery_result.scalars().all():
            recovery_by_user[row.user_id].append(row)

        passkey_result = await db.execute(
            select(UserWebAuthnCredential)
            .where(UserWebAuthnCredential.user_id.in_(user_ids))
            .order_by(UserWebAuthnCredential.user_id, UserWebAuthnCredential.id)
        )
        for row in passkey_result.scalars().all():
            passkeys_by_user[row.user_id].append(row)

        # v2 tables are intentionally optional at this boundary so a backup
        # can still be exported from a process serving a pre-migration schema.
        try:
            identity_result = await db.execute(
                select(UserIdentity)
                .where(UserIdentity.user_id.in_(user_ids))
                .order_by(UserIdentity.user_id, UserIdentity.id)
            )
            for row in identity_result.scalars().all():
                identities_by_user[row.user_id].append(row)
        except (KeyError, AttributeError, SQLAlchemyError):
            logger.debug("身份表不可用，导出将仅包含旧身份字段")
        try:
            endpoint_result = await db.execute(
                select(NotificationEndpoint)
                .where(NotificationEndpoint.user_id.in_(user_ids))
                .order_by(NotificationEndpoint.user_id, NotificationEndpoint.id)
            )
            for row in endpoint_result.scalars().all():
                endpoints_by_user[row.user_id].append(row)
        except (KeyError, AttributeError, SQLAlchemyError):
            logger.debug("通知端点表不可用，导出将仅包含用户资料")

    records = [
        {
            "identity": {
                "telegram_id": _real_telegram_id(user.telegram_id),
                "github_username": user.github_username,
                "email": getattr(user, "email", None),
                "email_verified": bool(getattr(user, "email_verified", False)),
            },
            "identities": [
                {
                    "provider": row.provider,
                    "provider_user_id": row.provider_user_id,
                    "provider_username": row.provider_username,
                    "metadata": _metadata_from_row(row),
                }
                for row in identities_by_user.get(user.id, [])
            ],
            "notification_endpoints": [
                {
                    "provider": row.provider,
                    "address": row.address,
                    "verified": bool(row.verified),
                    "enabled": bool(row.enabled),
                    "metadata": _metadata_from_row(row),
                }
                for row in endpoints_by_user.get(user.id, [])
            ],
            "profile": _profile_from_user(user),
            "personal_config": _personal_config_from_rows(
                configs_by_user.get(user.id, []), webui_by_user.get(user.id)
            ),
            "two_factor": _two_factor_from_user(
                user, recovery_by_user.get(user.id, [])
            ),
            "passkeys": _passkeys_from_rows(passkeys_by_user.get(user.id, [])),
        }
        for user in users
    ]
    return build_user_backup_document(
        records,
        recovery_code_hash_key_fingerprint=_recovery_code_hash_key_fingerprint(),
    )


def serialize_user_backup(document: dict[str, Any]) -> bytes:
    """Serialize a user backup as UTF-8 JSON."""
    return json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")


def _validate_identity(
    raw: Any, index: int, *, allow_external_only: bool = False
) -> dict[str, Any]:
    label = f"用户 {index + 1} 的身份"
    if not isinstance(raw, dict):
        raise UserBackupError(f"{label}结构无效")
    telegram_id = raw.get("telegram_id")
    if telegram_id is not None:
        _validate_int(
            telegram_id,
            f"{label} telegram_id",
            minimum=_BIGINT_MIN,
            maximum=_BIGINT_MAX,
        )
    github_username = raw.get("github_username")
    _validate_string(
        github_username,
        f"{label} github_username",
        100,
        allow_none=True,
    )
    email = raw.get("email")
    _validate_string(email, f"{label} email", 320, allow_none=True, allow_empty=False)
    if email is not None:
        email = email.strip().lower()
        if not _EMAIL_RE.fullmatch(email):
            raise UserBackupError(f"{label} email 格式无效")
    email_verified = raw.get("email_verified", False)
    _validate_bool(email_verified, f"{label} email_verified")
    if telegram_id is None and not github_username and not allow_external_only:
        raise UserBackupError(f"{label} 至少需要 telegram_id 或 github_username")
    if telegram_id is not None and telegram_id <= 0:
        # Old SQLite rows created before nullable migration may contain 0 or
        # a negative placeholder for a GitHub-only account.  Treat it as an
        # absent Telegram identity during import instead of binding it.
        telegram_id = None
    return {
        "telegram_id": telegram_id,
        "github_username": github_username,
        "email": email,
        "email_verified": email_verified,
    }


def _validate_external_identities(raw: Any, index: int) -> list[dict[str, Any]]:
    label = f"用户 {index + 1} 的外部身份"
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > USER_BACKUP_MAX_CONFIGS:
        raise UserBackupError(f"{label}列表无效")
    identities: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise UserBackupError(f"{label}包含无效记录")
        provider = item.get("provider")
        provider_user_id = item.get("provider_user_id")
        _validate_string(provider, f"{label} provider", 50, allow_empty=False)
        _validate_string(
            provider_user_id,
            f"{label} provider_user_id",
            255,
            allow_empty=False,
        )
        key = (provider.lower(), provider_user_id)
        if key in seen:
            raise UserBackupError(f"{label}身份重复")
        seen.add(key)
        provider_username = item.get("provider_username")
        _validate_string(
            provider_username,
            f"{label} provider_username",
            255,
            allow_none=True,
        )
        metadata = item.get("metadata")
        if metadata is not None and not isinstance(metadata, (dict, list, str, int, float, bool)):
            raise UserBackupError(f"{label} metadata 格式无效")
        identities.append(
            {
                "provider": provider.lower(),
                "provider_user_id": provider_user_id,
                "provider_username": provider_username,
                "metadata": metadata,
            }
        )
    return identities


def _validate_notification_endpoints(raw: Any, index: int) -> list[dict[str, Any]]:
    label = f"用户 {index + 1} 的通知端点"
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > USER_BACKUP_MAX_CONFIGS:
        raise UserBackupError(f"{label}列表无效")
    endpoints: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise UserBackupError(f"{label}包含无效记录")
        provider = item.get("provider")
        address = item.get("address")
        _validate_string(provider, f"{label} provider", 50, allow_empty=False)
        _validate_string(address, f"{label} address", 320, allow_empty=False)
        provider = provider.lower()
        address = address.strip()
        if provider == "telegram":
            try:
                telegram_id = int(address)
            except (TypeError, ValueError) as exc:
                raise UserBackupError(f"{label} Telegram 地址格式无效") from exc
            if telegram_id <= 0:
                # Old SQLite compatibility exports may contain 0/-1/-2 in
                # the legacy mirror.  They are not real chats and must not
                # become active endpoints when a backup is restored.
                continue
            address = str(telegram_id)
        key = (provider, address.lower() if provider == "email" else address)
        if key in seen:
            raise UserBackupError(f"{label}端点重复")
        seen.add(key)
        if provider == "email" and not _EMAIL_RE.fullmatch(address.lower()):
            raise UserBackupError(f"{label} email 地址格式无效")
        verified = item.get("verified", False)
        enabled = item.get("enabled", True)
        _validate_bool(verified, f"{label} verified")
        _validate_bool(enabled, f"{label} enabled")
        metadata = item.get("metadata")
        if metadata is not None and not isinstance(metadata, (dict, list, str, int, float, bool)):
            raise UserBackupError(f"{label} metadata 格式无效")
        endpoints.append(
            {
                "provider": provider,
                "address": address.lower() if provider == "email" else address,
                "verified": verified,
                # Normalize legacy unsafe combinations as early as parsing;
                # restore applies the same rule again for programmatic callers
                # that bypass this parser.
                "enabled": (
                    False
                    if provider == "email" and not bool(verified)
                    else bool(enabled)
                ),
                "metadata": metadata,
            }
        )
    return endpoints


def _restored_endpoint_enabled(endpoint: dict[str, Any]) -> bool:
    """Apply notification safety rules at the backup restore boundary.

    Historical backups may contain an enabled email row without proving that
    GitHub verified the address.  Keep that row for compatibility/audit, but
    never restore it as an active delivery target.  Other providers retain
    their original enabled state, and a verified email still honors an
    explicit user-disabled state from the backup.
    """

    if (
        str(endpoint.get("provider", "")).casefold() == "email"
        and not bool(endpoint.get("verified", False))
    ):
        return False
    return bool(endpoint.get("enabled", True))


def _validate_profile(raw: Any, index: int) -> dict[str, Any]:
    label = f"用户 {index + 1} 的 profile"
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise UserBackupError(f"{label}结构无效")
    profile = dict(raw)
    role = profile.get("role", _PROFILE_DEFAULTS["role"])
    if role not in VALID_USER_ROLES:
        raise UserBackupError(f"{label} role 无效")
    profile["role"] = role
    for field in _PROFILE_INT_FIELDS:
        if field not in profile:
            continue
        _validate_int(profile[field], f"{label} {field}", minimum=0, maximum=_INT_MAX)
    for field in _PROFILE_TIMESTAMP_FIELDS:
        if field in profile:
            _parse_datetime(profile[field], f"{label} {field}")
    is_active = profile.get("is_active", _PROFILE_DEFAULTS["is_active"])
    _validate_bool(is_active, f"{label} is_active")
    profile["is_active"] = is_active
    return profile


def _validate_personal_config(raw: Any, index: int) -> dict[str, Any]:
    label = f"用户 {index + 1} 的个人配置"
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise UserBackupError(f"{label}结构无效")

    raw_overrides = raw.get("dynamic_overrides", [])
    if (
        not isinstance(raw_overrides, list)
        or len(raw_overrides) > USER_BACKUP_MAX_CONFIGS
    ):
        raise UserBackupError(f"{label} dynamic_overrides 无效")
    overrides: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in raw_overrides:
        if not isinstance(item, dict):
            raise UserBackupError(f"{label}包含无效动态配置")
        key = item.get("key")
        _validate_string(key, f"{label}配置键", 100, allow_empty=False)
        if key in seen_keys:
            raise UserBackupError(f"{label}配置键 {key} 重复")
        seen_keys.add(key)
        if key not in USER_DYNAMIC_CONFIG_KEYS:
            raise UserBackupError(f"不允许导入用户配置项 {key}")
        value = item.get("value")
        if value is not None:
            _validate_string(value, f"{label}配置项 {key} 的值", 1024)
            try:
                value = validate_user_dynamic_config_value(key, value)
            except ValueError as exc:
                raise UserBackupError(str(exc)) from exc
        description = item.get("description")
        _validate_string(
            description,
            f"{label}配置项 {key} 的描述",
            255,
            allow_none=True,
        )
        overrides.append({"key": key, "value": value, "description": description})

    webui = raw.get("webui")
    if webui is not None:
        if not isinstance(webui, dict):
            raise UserBackupError(f"{label} webui 结构无效")
        theme = webui.get("theme", "system")
        if theme not in VALID_WEBUI_THEMES:
            raise UserBackupError(f"{label} theme 无效")
        language = webui.get("language", "zh-CN")
        _validate_string(language, f"{label} language", 10, allow_empty=False)
        items_per_page = webui.get("items_per_page", 20)
        _validate_int(items_per_page, f"{label} items_per_page")
        if items_per_page not in VALID_ITEMS_PER_PAGE:
            raise UserBackupError(f"{label} items_per_page 无效")
        webui = {
            "theme": theme,
            "language": language,
            "items_per_page": items_per_page,
        }
    return {"dynamic_overrides": overrides, "webui": webui}


def _validate_two_factor(raw: Any, index: int) -> dict[str, Any]:
    label = f"用户 {index + 1} 的两步验证"
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise UserBackupError(f"{label}结构无效")
    mfa_required = raw.get("mfa_required", False)
    totp_enabled = raw.get("totp_enabled", False)
    _validate_bool(mfa_required, f"{label} mfa_required")
    _validate_bool(totp_enabled, f"{label} totp_enabled")
    secret = raw.get("totp_secret")
    _validate_string(
        secret, f"{label} TOTP 密钥", 256, allow_none=True, allow_empty=False
    )
    if totp_enabled and not secret:
        raise UserBackupError(f"{label}已启用 TOTP 但缺少密钥")
    totp_enabled_at = raw.get("totp_enabled_at")
    _parse_datetime(totp_enabled_at, f"{label} totp_enabled_at")
    last_step = raw.get("totp_last_used_step")
    if last_step is not None:
        _validate_int(
            last_step,
            f"{label} totp_last_used_step",
            minimum=0,
            maximum=_BIGINT_MAX,
        )

    raw_codes = raw.get("recovery_codes", [])
    if (
        not isinstance(raw_codes, list)
        or len(raw_codes) > USER_BACKUP_MAX_RECOVERY_CODES
    ):
        raise UserBackupError(f"{label} recovery_codes 无效")
    codes: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for code in raw_codes:
        if not isinstance(code, dict):
            raise UserBackupError(f"{label}包含无效恢复码")
        code_hash = code.get("code_hash")
        _validate_string(code_hash, f"{label}恢复码哈希", 128, allow_empty=False)
        if len(code_hash) not in _RECOVERY_HASH_LENGTHS or not _HEX_RE.fullmatch(
            code_hash
        ):
            raise UserBackupError(f"{label}恢复码哈希格式无效")
        normalized_hash = code_hash.lower()
        if normalized_hash in seen_hashes:
            raise UserBackupError(f"{label}恢复码哈希重复")
        seen_hashes.add(normalized_hash)
        used_at = code.get("used_at")
        created_at = code.get("created_at")
        _parse_datetime(used_at, f"{label}恢复码 used_at")
        _parse_datetime(created_at, f"{label}恢复码 created_at")
        codes.append(
            {
                "code_hash": normalized_hash,
                "used_at": used_at,
                "created_at": created_at,
            }
        )
    return {
        "mfa_required": mfa_required,
        "totp_enabled": totp_enabled,
        "totp_secret": secret,
        "totp_enabled_at": totp_enabled_at,
        "totp_last_used_step": last_step,
        "recovery_codes": codes,
    }


def _validate_passkeys(raw: Any, index: int) -> list[dict[str, Any]]:
    label = f"用户 {index + 1} 的通行密钥"
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > USER_BACKUP_MAX_PASSKEYS:
        raise UserBackupError(f"{label}列表无效")
    passkeys: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise UserBackupError(f"{label}包含无效记录")
        credential_id = item.get("credential_id")
        public_key = item.get("public_key")
        _validate_string(
            credential_id, f"{label} credential_id", 1024, allow_empty=False
        )
        _validate_string(
            public_key, f"{label} public_key", 1024 * 1024, allow_empty=False
        )
        derived_hash = credential_id_hash(credential_id)
        supplied_hash = item.get("credential_id_hash")
        if supplied_hash is not None:
            _validate_string(
                supplied_hash,
                f"{label} credential_id_hash",
                64,
                allow_empty=False,
            )
            if supplied_hash.lower() != derived_hash:
                raise UserBackupError(f"{label} credential_id_hash 与凭据不匹配")
        credential_hash = derived_hash
        if credential_hash in seen_hashes:
            raise UserBackupError(f"{label} credential_id_hash 重复")
        seen_hashes.add(credential_hash)
        sign_count = item.get("sign_count", 0)
        _validate_int(sign_count, f"{label} sign_count", minimum=0, maximum=_BIGINT_MAX)
        transports = item.get("transports")
        _validate_string(transports, f"{label} transports", 255, allow_none=True)
        device_name = item.get("device_name")
        _validate_string(device_name, f"{label} device_name", 100, allow_none=True)
        backed_up = item.get("backed_up", False)
        _validate_bool(backed_up, f"{label} backed_up")
        created_at = item.get("created_at")
        last_used_at = item.get("last_used_at")
        _parse_datetime(created_at, f"{label} created_at")
        _parse_datetime(last_used_at, f"{label} last_used_at")
        passkeys.append(
            {
                "credential_id": credential_id,
                "credential_id_hash": credential_hash,
                "public_key": public_key,
                "sign_count": sign_count,
                "transports": transports,
                "device_name": device_name,
                "backed_up": backed_up,
                "created_at": created_at,
                "last_used_at": last_used_at,
            }
        )
    return passkeys


def parse_user_backup(content: bytes) -> dict[str, Any]:
    """Parse and strictly validate an uploaded user backup."""
    if not content:
        raise UserBackupError("用户备份文件为空")
    if len(content) > USER_BACKUP_MAX_BYTES:
        raise UserBackupError("用户备份文件超过 5 MiB 限制")
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UserBackupError("用户备份文件不是有效的 UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise UserBackupError("用户备份文件顶层必须是对象")
    if payload.get("format") != USER_BACKUP_FORMAT:
        raise UserBackupError("用户备份文件格式标识不匹配")
    version = payload.get("version")
    if version not in SUPPORTED_USER_BACKUP_VERSIONS:
        raise UserBackupError("不支持此用户备份版本")
    if payload.get("scope") != USER_BACKUP_SCOPE:
        raise UserBackupError("用户备份范围无效")
    exported_at = payload.get("exported_at")
    if exported_at is not None:
        _parse_datetime(exported_at, "exported_at")
    if "contains_sensitive_values" in payload:
        _validate_bool(
            payload["contains_sensitive_values"], "contains_sensitive_values"
        )

    fingerprint = payload.get("recovery_code_hash_key_fingerprint")
    _validate_string(
        fingerprint,
        "recovery_code_hash_key_fingerprint",
        64,
        allow_none=True,
        allow_empty=False,
    )
    if fingerprint is not None and (
        len(fingerprint) != 64 or not _HEX_RE.fullmatch(fingerprint)
    ):
        raise UserBackupError("恢复码哈希密钥指纹格式无效")

    raw_users = payload.get("users")
    if not isinstance(raw_users, list) or len(raw_users) > USER_BACKUP_MAX_USERS:
        raise UserBackupError("用户列表无效或数量过多")
    user_count = payload.get("user_count", len(raw_users))
    _validate_int(user_count, "user_count", minimum=0, maximum=USER_BACKUP_MAX_USERS)
    if user_count != len(raw_users):
        raise UserBackupError("用户数量校验失败")

    users: list[dict[str, Any]] = []
    seen_telegram_ids: set[int] = set()
    seen_github_usernames: set[str] = set()
    seen_passkey_hashes: set[str] = set()
    seen_external_identities: set[tuple[str, str]] = set()
    seen_notification_endpoints: set[tuple[str, str]] = set()
    for index, raw_user in enumerate(raw_users):
        if not isinstance(raw_user, dict):
            raise UserBackupError(f"用户 {index + 1} 记录结构无效")
        # v1 exports used a nested identity object.  Accept early migration
        # snapshots that put those fields directly on each user record.
        raw_identity = raw_user.get("identity")
        if raw_identity is None:
            raw_identity = {
                key: raw_user.get(key)
                for key in ("telegram_id", "github_username", "email", "email_verified")
                if key in raw_user
            }
        # v2 users may be represented solely by a stable external identity
        # (for example a Passkey subject) while the legacy Telegram facade is
        # empty.  Validate the external records before deciding whether that
        # legacy identity may be empty.
        identities = _validate_external_identities(raw_user.get("identities"), index)
        endpoints = _validate_notification_endpoints(
            raw_user.get("notification_endpoints"), index
        )
        identity = _validate_identity(
            raw_identity,
            index,
            allow_external_only=bool(identities or endpoints),
        )
        telegram_id = identity["telegram_id"]
        github_username = identity["github_username"]
        if telegram_id is not None:
            if telegram_id in seen_telegram_ids:
                raise UserBackupError(f"telegram_id {telegram_id} 重复")
            seen_telegram_ids.add(telegram_id)
        if github_username:
            github_key = github_username.casefold()
            if github_key in seen_github_usernames:
                raise UserBackupError(f"github_username {github_username} 重复")
            seen_github_usernames.add(github_key)

        for external in identities:
            key = (external["provider"], external["provider_user_id"])
            if key in seen_external_identities:
                raise UserBackupError(
                    f"外部身份 {external['provider']}:{external['provider_user_id']} 重复"
                )
            seen_external_identities.add(key)
        for endpoint in endpoints:
            key = (endpoint["provider"], endpoint["address"])
            if key in seen_notification_endpoints:
                raise UserBackupError(
                    f"通知端点 {endpoint['provider']}:{endpoint['address']} 重复"
                )
            seen_notification_endpoints.add(key)

        profile = _validate_profile(raw_user.get("profile"), index)
        personal_config = _validate_personal_config(
            raw_user.get("personal_config"), index
        )
        two_factor = _validate_two_factor(raw_user.get("two_factor"), index)
        passkeys = _validate_passkeys(raw_user.get("passkeys"), index)
        for passkey in passkeys:
            key = passkey["credential_id_hash"]
            if key in seen_passkey_hashes:
                raise UserBackupError(f"通行密钥 credential_id_hash {key} 重复")
            seen_passkey_hashes.add(key)
        users.append(
            {
                "identity": identity,
                "identities": identities,
                "notification_endpoints": endpoints,
                "profile": profile,
                "personal_config": personal_config,
                "two_factor": two_factor,
                "passkeys": passkeys,
            }
        )

    return {
        "format": USER_BACKUP_FORMAT,
        "version": USER_BACKUP_VERSION,
        "exported_at": exported_at,
        "scope": USER_BACKUP_SCOPE,
        "user_count": len(users),
        "contains_sensitive_values": bool(
            payload.get("contains_sensitive_values", True)
        ),
        "recovery_code_hash_key_fingerprint": fingerprint,
        "users": users,
    }


def _apply_value(target: Any, field: str, value: Any) -> bool:
    if getattr(target, field, None) == value:
        return False
    setattr(target, field, value)
    return True


def _profile_value(profile: dict[str, Any], field: str) -> Any:
    value = profile.get(field, _PROFILE_DEFAULTS.get(field))
    if field in _PROFILE_TIMESTAMP_FIELDS:
        return _parse_datetime(value, f"profile {field}")
    return value


def _count_query_rows(result: Any) -> list[Any]:
    """Read scalar rows from both AsyncResult and small test doubles."""
    return list(result.scalars().all())


async def _optional_model_rows(db: AsyncSession, model: Any) -> tuple[list[Any], bool]:
    """Load a v2 model while remaining compatible with tiny v1 test doubles.

    Real startup migration always creates these tables.  The narrow exception
    handling is only for legacy callers that provide a session facade without
    the new model in its dispatch map.
    """
    try:
        return _count_query_rows(await db.execute(select(model))), True
    except (KeyError, AttributeError):
        return [], False
    except SQLAlchemyError:
        # PostgreSQL marks the transaction failed when an optional legacy
        # table is absent; clear that state before the caller continues with
        # v1-compatible user rows.
        await db.rollback()
        return [], False


async def restore_user_backup(
    db: AsyncSession,
    document: dict[str, Any],
) -> UserImportResult:
    """Merge users and restore their supported related information transactionally."""
    if not isinstance(document, dict) or document.get("format") != USER_BACKUP_FORMAT:
        raise UserBackupError("没有可导入的用户备份内容")
    users_payload = document.get("users")
    if not isinstance(users_payload, list):
        raise UserBackupError("用户备份缺少 users 列表")

    recovery_count = sum(
        len(raw_user.get("two_factor", {}).get("recovery_codes", []))
        for raw_user in users_payload
    )
    source_fingerprint = document.get("recovery_code_hash_key_fingerprint")
    current_fingerprint = _recovery_code_hash_key_fingerprint()
    recovery_codes_portable = recovery_count == 0 or (
        isinstance(source_fingerprint, str)
        and source_fingerprint.lower() == current_fingerprint
    )
    recovery_codes_skipped = 0 if recovery_codes_portable else recovery_count
    if recovery_codes_skipped:
        logger.warning(
            "恢复码哈希密钥指纹不匹配，保留现有恢复码并跳过导入: skipped={}",
            recovery_codes_skipped,
        )

    try:
        existing_users = _count_query_rows(
            await db.execute(select(TelegramUser).order_by(TelegramUser.id))
        )
        by_telegram_id = {user.telegram_id: user for user in existing_users}
        by_github_username = {
            user.github_username.casefold(): user
            for user in existing_users
            if user.github_username
        }
        by_email = {
            user.email.casefold(): user
            for user in existing_users
            if getattr(user, "email", None)
        }
        existing_identity_rows, identity_tables_available = await _optional_model_rows(
            db, UserIdentity
        )
        existing_endpoint_rows, endpoint_tables_available = await _optional_model_rows(
            db, NotificationEndpoint
        )
        by_external_identity = {
            (str(row.provider).casefold(), row.provider_user_id): row
            for row in existing_identity_rows
        }
        by_notification_endpoint = {
            (
                str(row.provider).casefold(),
                row.address.casefold()
                if str(row.provider).casefold() == "email"
                else row.address,
            ): row
            for row in existing_endpoint_rows
        }
        existing_telegram_endpoints_by_user: dict[int, list[NotificationEndpoint]] = (
            defaultdict(list)
        )
        for row in existing_endpoint_rows:
            if str(row.provider).casefold() == "telegram":
                existing_telegram_endpoints_by_user[int(row.user_id)].append(row)

        matches: list[tuple[dict[str, Any], TelegramUser | None, str | None]] = []
        seen_existing_ids: set[int] = set()
        for raw_user in users_payload:
            identity = raw_user["identity"]
            telegram_id = identity.get("telegram_id")
            github_username = identity.get("github_username")
            external_identities = raw_user.get("identities", [])
            by_telegram = (
                by_telegram_id.get(telegram_id) if telegram_id is not None else None
            )
            by_github = (
                by_github_username.get(github_username.casefold())
                if github_username
                else None
            )
            by_external = None
            for external in external_identities:
                identity_row = by_external_identity.get(
                    (
                        external["provider"].casefold(),
                        external["provider_user_id"],
                    )
                )
                if identity_row is None:
                    continue
                external_user = next(
                    (user for user in existing_users if user.id == identity_row.user_id),
                    None,
                )
                if external_user is None:
                    raise UserBackupError("外部身份关联的用户不存在")
                if by_external is not None and by_external.id != external_user.id:
                    raise UserBackupError("备份中的外部身份指向不同用户")
                by_external = external_user
            targets_by_identity = [candidate for candidate in (by_telegram, by_github, by_external) if candidate]
            if targets_by_identity and any(
                candidate.id != targets_by_identity[0].id
                for candidate in targets_by_identity[1:]
            ):
                raise UserBackupError(
                    f"用户身份冲突：{github_username or telegram_id or 'external identity'} 指向不同用户"
                )
            if (
                by_telegram is not None
                and by_github is not None
                and by_telegram.id != by_github.id
            ):
                raise UserBackupError(
                    f"用户身份冲突：telegram_id {telegram_id} 与 github_username {github_username} 指向不同用户"
                )
            target = by_external or by_telegram or by_github
            match_field = (
                "provider_user_id"
                if by_external is not None
                else (
                    "telegram_id"
                    if by_telegram is not None
                    else ("github_username" if by_github is not None else None)
                )
            )
            if target is not None:
                if target.id in seen_existing_ids:
                    raise UserBackupError(
                        f"备份中的多个用户匹配到同一目标用户 {target.id}"
                    )
                seen_existing_ids.add(target.id)
            email = identity.get("email")
            if email:
                email_owner = by_email.get(email.casefold())
                if email_owner is not None and (
                    target is None or email_owner.id != target.id
                ):
                    raise UserBackupError(
                        f"用户 email {email} 已属于其他用户，拒绝自动合并"
                    )
            for endpoint in raw_user.get("notification_endpoints", []):
                endpoint_key = (
                    endpoint["provider"].casefold(),
                    endpoint["address"].casefold()
                    if endpoint["provider"].casefold() == "email"
                    else endpoint["address"],
                )
                existing_endpoint = by_notification_endpoint.get(endpoint_key)
                if existing_endpoint is not None and (
                    target is None or existing_endpoint.user_id != target.id
                ):
                    raise UserBackupError(
                        f"通知端点 {endpoint['provider']}:{endpoint['address']} 已属于其他用户"
                    )
            if (
                target is None
                and telegram_id is None
                and not github_username
                and not external_identities
            ):
                raise UserBackupError(
                    f"新用户 {github_username or '(unknown)'} 缺少可用身份，无法导入"
                )
            matches.append((raw_user, target, match_field))

        existing_passkeys = _count_query_rows(
            await db.execute(select(UserWebAuthnCredential))
        )
        passkeys_by_hash: dict[str, UserWebAuthnCredential] = {}
        for row in existing_passkeys:
            derived_key = credential_id_hash(row.credential_id)
            if row.credential_id_hash and row.credential_id_hash != derived_key:
                raise UserBackupError(f"数据库中通行密钥哈希与凭据不匹配：{row.id}")
            key = derived_key
            previous = passkeys_by_hash.get(key)
            if previous is not None and previous.id != row.id:
                raise UserBackupError(f"数据库中通行密钥哈希重复：{key}")
            passkeys_by_hash[key] = row
        target_ids = {target.id for _, target, _ in matches if target is not None}
        target_id_by_payload = {
            id(raw): target.id for raw, target, _ in matches if target
        }
        for raw_user, target, _ in matches:
            target_id = (
                target.id
                if target is not None
                else target_id_by_payload.get(id(raw_user))
            )
            for passkey in raw_user.get("passkeys", []):
                existing_passkey = passkeys_by_hash.get(passkey["credential_id_hash"])
                if existing_passkey is not None and (
                    target_id is None or existing_passkey.user_id != target_id
                ):
                    raise UserBackupError(
                        f"通行密钥 {passkey['credential_id_hash']} 已属于其他用户"
                    )

        # Only now mutate the session. All identity and credential conflicts above are preflighted.
        users_created = users_updated = users_unchanged = 0
        targets: list[tuple[dict[str, Any], TelegramUser, str | None]] = []
        affected_ids: list[int] = []
        for raw_user, target, match_field in matches:
            identity = raw_user["identity"]
            profile = raw_user.get("profile", {})
            two_factor = raw_user.get("two_factor", {})
            changed = False
            existing_target = target is not None
            if target is None:
                telegram_id = identity.get("telegram_id")
                target = await create_user_and_flush(
                    db,
                    lambda resolved_telegram_id, github_username=identity.get(
                        "github_username"
                    ): TelegramUser(
                        telegram_id=resolved_telegram_id,
                        github_username=github_username,
                    ),
                    telegram_id=telegram_id,
                )
                users_created += 1
                changed = True
            else:
                if (
                    match_field in {"telegram_id", "provider_user_id"}
                    and identity.get("github_username")
                    and identity.get("github_username") != target.github_username
                ):
                    changed = (
                        _apply_value(
                            target, "github_username", identity.get("github_username")
                        )
                        or changed
                    )
                for field in _PROFILE_FIELDS:
                    if field not in profile:
                        continue
                    changed = (
                        _apply_value(target, field, _profile_value(profile, field))
                        or changed
                    )
                # ``telegram_users.telegram_id`` is a legacy mirror that is
                # also referenced by user_repo_subscriptions.  A backup may
                # match an existing user by a stable provider identity while
                # carrying a newer Telegram address; updating this populated
                # mirror would violate that foreign key (there is no ON
                # UPDATE CASCADE).  Keep it intact and restore the new
                # address through NotificationEndpoint below.  A NULL mirror
                # is safe to fill for old GitHub-only rows.
                if (
                    target.telegram_id is None
                    and identity.get("telegram_id") is not None
                ):
                    changed = (
                        _apply_value(target, "telegram_id", identity["telegram_id"])
                        or changed
                    )

            if identity.get("email"):
                changed = (
                    _apply_value(target, "email", identity["email"]) or changed
                )
                changed = (
                    _apply_value(
                        target,
                        "email_verified",
                        bool(identity.get("email_verified", False)),
                    )
                    or changed
                )

            # For new rows SQLAlchemy defaults cover omitted profile fields; explicit backup values win.
            if target.id is None:
                await db.flush()
            if target.id is None:
                raise UserBackupError("无法为导入用户分配数据库 ID")
            if not existing_target:
                for field in _PROFILE_FIELDS:
                    if field in profile or field in _PROFILE_DEFAULTS:
                        changed = (
                            _apply_value(target, field, _profile_value(profile, field))
                            or changed
                        )

            changed = (
                _apply_value(
                    target, "mfa_required", bool(two_factor.get("mfa_required", False))
                )
                or changed
            )
            changed = (
                _apply_value(
                    target, "totp_enabled", bool(two_factor.get("totp_enabled", False))
                )
                or changed
            )
            raw_secret = two_factor.get("totp_secret")
            encrypted_secret = encrypt_totp_secret(raw_secret) if raw_secret else None
            changed = (
                _apply_value(target, "totp_secret_encrypted", encrypted_secret)
                or changed
            )
            changed = (
                _apply_value(
                    target,
                    "totp_enabled_at",
                    _parse_datetime(
                        two_factor.get("totp_enabled_at"), "totp_enabled_at"
                    ),
                )
                or changed
            )
            changed = (
                _apply_value(
                    target,
                    "totp_last_used_step",
                    two_factor.get("totp_last_used_step"),
                )
                or changed
            )
            if not existing_target:
                # New rows were counted above; no separate update count is needed.
                pass
            elif changed:
                users_updated += 1
            else:
                users_unchanged += 1
            affected_ids.append(int(target.id))
            targets.append((raw_user, target, match_field))

        target_ids.update(affected_ids)
        if target_ids:
            config_rows = _count_query_rows(
                await db.execute(
                    select(UserConfig).where(UserConfig.user_id.in_(target_ids))
                )
            )
            webui_rows = _count_query_rows(
                await db.execute(
                    select(WebUIConfig).where(WebUIConfig.user_id.in_(target_ids))
                )
            )
            recovery_rows = _count_query_rows(
                await db.execute(
                    select(UserRecoveryCode).where(
                        UserRecoveryCode.user_id.in_(target_ids)
                    )
                )
            )
        else:
            config_rows = []
            webui_rows = []
            recovery_rows = []
        config_by_key = {(row.user_id, row.config_key): row for row in config_rows}
        webui_by_user = {row.user_id: row for row in webui_rows}
        recovery_by_user: dict[int, list[UserRecoveryCode]] = defaultdict(list)
        for row in recovery_rows:
            recovery_by_user[row.user_id].append(row)

        user_configs_created = user_configs_updated = user_configs_deleted = 0
        webui_configs_created = webui_configs_updated = webui_configs_deleted = 0
        recovery_codes_imported = recovery_codes_deleted = 0
        passkeys_created = passkeys_updated = 0

        for raw_user, target, _ in targets:
            user_id = int(target.id)
            personal_config = raw_user.get("personal_config", {})
            for override in personal_config.get("dynamic_overrides", []):
                key = override["key"]
                value = override.get("value")
                existing = config_by_key.get((user_id, key))
                if value is None:
                    if existing is not None:
                        await db.delete(existing)
                        user_configs_deleted += 1
                    continue
                description = override.get("description") or DYNAMIC_CONFIG_LABELS.get(
                    key, key
                )
                if existing is None:
                    db.add(
                        UserConfig(
                            user_id=user_id,
                            config_key=key,
                            config_value=value,
                            description=description,
                        )
                    )
                    user_configs_created += 1
                elif (
                    existing.config_value != value
                    or existing.description != description
                ):
                    existing.config_value = value
                    existing.description = description
                    user_configs_updated += 1
                invalidate_user_dynamic_config_cache(user_id, [key])

            webui = personal_config.get("webui")
            existing_webui = webui_by_user.get(user_id)
            if webui is None:
                if existing_webui is not None:
                    await db.delete(existing_webui)
                    webui_configs_deleted += 1
            elif existing_webui is None:
                db.add(
                    WebUIConfig(
                        user_id=user_id,
                        theme=webui["theme"],
                        language=webui["language"],
                        items_per_page=webui["items_per_page"],
                    )
                )
                webui_configs_created += 1
            elif any(
                getattr(existing_webui, field) != webui[field]
                for field in ("theme", "language", "items_per_page")
            ):
                existing_webui.theme = webui["theme"]
                existing_webui.language = webui["language"]
                existing_webui.items_per_page = webui["items_per_page"]
                webui_configs_updated += 1

            if recovery_codes_portable:
                for row in recovery_by_user.get(user_id, []):
                    await db.delete(row)
                    recovery_codes_deleted += 1
                recovery_codes = raw_user.get("two_factor", {}).get(
                    "recovery_codes", []
                )
                for code in recovery_codes:
                    db.add(
                        UserRecoveryCode(
                            user_id=user_id,
                            code_hash=code["code_hash"],
                            used_at=_parse_datetime(
                                code.get("used_at"), "recovery used_at"
                            ),
                            created_at=_parse_datetime(
                                code.get("created_at"), "recovery created_at"
                            )
                            or now_utc(),
                        )
                    )
                    recovery_codes_imported += 1

            for passkey in raw_user.get("passkeys", []):
                key = passkey["credential_id_hash"]
                existing_passkey = passkeys_by_hash.get(key)
                if existing_passkey is None:
                    db.add(
                        UserWebAuthnCredential(
                            user_id=user_id,
                            credential_id=passkey["credential_id"],
                            credential_id_hash=key,
                            public_key=passkey["public_key"],
                            sign_count=passkey["sign_count"],
                            transports=passkey.get("transports"),
                            device_name=passkey.get("device_name"),
                            backed_up=passkey.get("backed_up", False),
                            created_at=_parse_datetime(
                                passkey.get("created_at"), "passkey created_at"
                            )
                            or now_utc(),
                            last_used_at=_parse_datetime(
                                passkey.get("last_used_at"), "passkey last_used_at"
                            ),
                        )
                    )
                    passkeys_created += 1
                else:
                    existing_passkey.credential_id_hash = key
                    existing_passkey.credential_id = passkey["credential_id"]
                    existing_passkey.public_key = passkey["public_key"]
                    existing_passkey.sign_count = max(
                        int(existing_passkey.sign_count or 0),
                        passkey["sign_count"],
                    )
                    existing_passkey.transports = passkey.get("transports")
                    existing_passkey.device_name = passkey.get("device_name")
                    existing_passkey.backed_up = passkey.get("backed_up", False)
                    created_at = _parse_datetime(
                        passkey.get("created_at"), "passkey created_at"
                    )
                    if created_at is not None:
                        existing_passkey.created_at = created_at
                    existing_passkey.last_used_at = _parse_datetime(
                        passkey.get("last_used_at"), "passkey last_used_at"
                    )
                    passkeys_updated += 1

            # Restore stable external identities first.  Existing provider IDs
            # are matched above and never cause a second internal user.
            if identity_tables_available:
                external_identities = list(raw_user.get("identities", []))
                github_username = raw_user["identity"].get("github_username")
                if github_username and not external_identities:
                    external_identities.append(
                        {
                            "provider": "github",
                            "provider_user_id": f"legacy:{github_username.casefold()}",
                            "provider_username": github_username,
                            "metadata": None,
                        }
                    )
                for external in external_identities:
                    key = (
                        external["provider"].casefold(),
                        external["provider_user_id"],
                    )
                    existing_identity = by_external_identity.get(key)
                    metadata = external.get("metadata")
                    metadata_value = (
                        json.dumps(metadata, ensure_ascii=False)
                        if metadata is not None and not isinstance(metadata, str)
                        else metadata
                    )
                    if existing_identity is None:
                        db.add(
                            UserIdentity(
                                user_id=user_id,
                                provider=external["provider"].casefold(),
                                provider_user_id=external["provider_user_id"],
                                provider_username=external.get("provider_username"),
                                metadata_json=metadata_value,
                            )
                        )
                    elif existing_identity.user_id != user_id:
                        raise UserBackupError(
                            f"外部身份 {external['provider']}:{external['provider_user_id']} 已属于其他用户"
                        )
                    else:
                        existing_identity.provider_username = external.get(
                            "provider_username"
                        )
                        existing_identity.metadata_json = metadata_value

            if endpoint_tables_available:
                endpoints = list(raw_user.get("notification_endpoints", []))
                explicit_telegram_endpoints = [
                    item
                    for item in endpoints
                    if item["provider"].casefold() == "telegram"
                ]
                # Legacy Telegram IDs and verified OAuth emails become endpoint
                # records during restore as well, so notification code can use
                # one abstraction immediately after importing a v1 backup.
                telegram_id = raw_user["identity"].get("telegram_id")
                # A legacy identity-only backup has no endpoint state, so
                # derive one compatibility endpoint from its Telegram mirror.
                # Once the backup contains any explicit Telegram endpoint,
                # that list is authoritative (including an explicit disabled
                # entry) and must not be overridden by this fallback.
                if (
                    telegram_id is not None
                    and not explicit_telegram_endpoints
                ):
                    endpoints.append(
                        {
                            "provider": "telegram",
                            "address": str(telegram_id),
                            "verified": True,
                            "enabled": True,
                            "metadata": {"legacy": True},
                        }
                    )
                email = raw_user["identity"].get("email")
                if email and not any(
                    item["provider"] == "email"
                    and item["address"].casefold() == email.casefold()
                    for item in endpoints
                ):
                    endpoints.append(
                        {
                            "provider": "email",
                            "address": email,
                            "verified": bool(
                                raw_user["identity"].get("email_verified", False)
                            ),
                            # The restore boundary below normalizes this to
                            # disabled when the legacy mirror was unverified.
                            "enabled": True,
                            "metadata": {"oauth": True},
                        }
                    )

                # A supplied Telegram endpoint list is authoritative for the
                # target user's active addresses.  Keep stale endpoint rows for
                # audit/rollback, but disable an old address that is absent or
                # explicitly disabled in the backup.  This lets compatibility
                # callers which still pass ``TelegramUser.telegram_id`` resolve
                # to the newly restored endpoint without sending to both old
                # and new chats.  An explicitly enabled old address remains
                # enabled, so a backup can intentionally retain multiple chats.
                telegram_endpoints = [
                    item
                    for item in endpoints
                    if item["provider"].casefold() == "telegram"
                ]
                if telegram_endpoints:
                    enabled_addresses = {
                        item["address"]
                        for item in telegram_endpoints
                        if bool(item.get("enabled", True))
                    }
                    for existing_endpoint in existing_telegram_endpoints_by_user.get(
                        int(target.id), []
                    ):
                        if existing_endpoint.address not in enabled_addresses:
                            existing_endpoint.enabled = False
                for endpoint in endpoints:
                    key = (
                        endpoint["provider"].casefold(),
                        endpoint["address"].casefold()
                        if endpoint["provider"].casefold() == "email"
                        else endpoint["address"],
                    )
                    existing_endpoint = by_notification_endpoint.get(key)
                    metadata = endpoint.get("metadata")
                    metadata_value = (
                        json.dumps(metadata, ensure_ascii=False)
                        if metadata is not None and not isinstance(metadata, str)
                        else metadata
                    )
                    if existing_endpoint is None:
                        db.add(
                            NotificationEndpoint(
                                user_id=user_id,
                                provider=endpoint["provider"].casefold(),
                                address=endpoint["address"],
                                verified=bool(endpoint.get("verified", False)),
                                enabled=_restored_endpoint_enabled(endpoint),
                                metadata_json=metadata_value,
                            )
                        )
                    elif existing_endpoint.user_id != user_id:
                        raise UserBackupError(
                            f"通知端点 {endpoint['provider']}:{endpoint['address']} 已属于其他用户"
                        )
                    else:
                        existing_endpoint.verified = bool(endpoint.get("verified", False))
                        existing_endpoint.enabled = _restored_endpoint_enabled(endpoint)
                        existing_endpoint.metadata_json = metadata_value

        await db.commit()
    except UserBackupError:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise UserBackupError("用户备份导入失败，当前数据已回滚") from exc

    return UserImportResult(
        users_created=users_created,
        users_updated=users_updated,
        users_unchanged=users_unchanged,
        user_configs_created=user_configs_created,
        user_configs_updated=user_configs_updated,
        user_configs_deleted=user_configs_deleted,
        webui_configs_created=webui_configs_created,
        webui_configs_updated=webui_configs_updated,
        webui_configs_deleted=webui_configs_deleted,
        recovery_codes_imported=recovery_codes_imported,
        recovery_codes_deleted=recovery_codes_deleted,
        recovery_codes_skipped=recovery_codes_skipped,
        passkeys_created=passkeys_created,
        passkeys_updated=passkeys_updated,
        recovery_codes_portable=recovery_codes_portable,
        affected_user_ids=tuple(dict.fromkeys(affected_ids)),
    )


# Descriptive aliases for callers that use the longer feature name.
export_user_info_backup = export_user_backup
parse_user_info_backup = parse_user_backup
serialize_user_info_backup = serialize_user_backup
restore_user_info_backup = restore_user_backup
