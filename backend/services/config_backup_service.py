"""全局配置、AI 配置与系统配置的版本化备份、校验和恢复服务。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import (
    AI_STRATEGY_CONFIG_KEYS,
    BASIC_CONFIG_KEYS,
    DYNAMIC_CONFIG_RANGES,
    DYNAMIC_CONFIG_SELECT_OPTIONS,
    DYNAMIC_CONFIG_SENSITIVE_KEYS,
    Settings,
    _get_field_type,
    get_all_dynamic_config_keys,
    get_settings,
    invalidate_dynamic_config_cache,
    reload_strategy_config,
    update_settings_field,
)
from backend.core.config_sections import (
    SECTION_REGISTRY,
    clear_section_store,
    deep_merge,
    get_section_defaults,
    update_section_store,
)
from backend.core.time_service import (
    InvalidTimezoneError,
    format_rfc3339,
    now_utc,
    parse_rfc3339,
    resolve_timezone,
)
from backend.models.database import AppConfig
from backend.services.section_config_service import SECTION_VALIDATORS
from backend.services.system_config_service import (
    RESTART_REQUIRED_KEYS,
    SYSTEM_CONFIG_KEYS,
    SYSTEM_SENSITIVE_KEYS,
)

BACKUP_FORMAT = "sakura-ai-config-backup"
BACKUP_VERSION = 3
LEGACY_BACKUP_VERSION = 1
SUPPORTED_BACKUP_VERSIONS = frozenset({LEGACY_BACKUP_VERSION, 2, BACKUP_VERSION})
BACKUP_MAX_BYTES = 5 * 1024 * 1024
BACKUP_MAX_ENTRIES = 5000

GLOBAL_SECTION = "global"
AI_SECTION = "ai"
SYSTEM_SECTION = "system"
ALL_SCOPE = "all"
VALID_SCOPES = frozenset({GLOBAL_SECTION, AI_SECTION, SYSTEM_SECTION, ALL_SCOPE})

GLOBAL_CONFIG_KEYS = frozenset(BASIC_CONFIG_KEYS) | frozenset(
    get_all_dynamic_config_keys()
)
AI_CONFIG_KEYS = frozenset(AI_STRATEGY_CONFIG_KEYS) | {"ai_role_bindings"}
AI_CONFIG_PREFIXES = ("ai_account.", "ai_model_override.")
# strategy/label 节键集合从统一节注册表派生（勿手工维护键清单）；
# 两节均为非敏感的节级 JSON 覆盖，无敏感值。
STRATEGY_SECTION = "strategy"
LABEL_SECTION = "label"
STRATEGY_CONFIG_KEYS = frozenset(
    key for key, spec in SECTION_REGISTRY.items() if spec["target"] == "strategy"
)
LABEL_CONFIG_KEYS = frozenset(
    key for key, spec in SECTION_REGISTRY.items() if spec["target"] == "label"
)
SECTION_BACKUP_SECTIONS = (STRATEGY_SECTION, LABEL_SECTION)

_GLOBAL_SENSITIVE_KEYS = frozenset(DYNAMIC_CONFIG_SENSITIVE_KEYS) | {
    "web_search_api_key"
}
_SYSTEM_BACKUP_SENSITIVE_KEYS = frozenset(SYSTEM_SENSITIVE_KEYS) | {
    "database_url",
    "redis_url",
}
_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_POSITIVE_INTEGER_KEYS = {
    "max_concurrent_reviews",
    "review_timeout_seconds",
    "analysis_min_interval_sec",
}
_AI_STRATEGY_RANGES: dict[str, tuple[float, float]] = {
    "ai_api_timeout_seconds": (1.0, 3600.0),
    "ai_api_max_retries": (0, 20),
    "ai_api_initial_retry_delay_seconds": (0.0, 60.0),
    "ai_api_total_timeout_seconds": (1.0, 7200.0),
    "ai_fallback_max_candidates": (1, 10),
    "context_compression_threshold": (0.1, 1.0),
    "activity_artifact_retention_days": (1, 3650),
}


class ConfigBackupError(ValueError):
    """备份内容无效或无法安全导入。"""


@dataclass(frozen=True)
class BackupRecord:
    """单条 AppConfig 备份记录。"""

    key: str
    value: str | None
    description: str | None


@dataclass(frozen=True)
class ConfigImportResult:
    """配置恢复结果与运行时刷新所需的信息。"""

    sections: tuple[str, ...]
    created: int
    updated: int
    deleted: int
    unchanged: int
    imported_values: dict[str, str | None]
    deleted_keys: frozenset[str]
    requires_restart: bool

    @property
    def total_changes(self) -> int:
        return self.created + self.updated + self.deleted


def config_section_for_key(key: str) -> str | None:
    """返回配置键所属的可备份分类。"""
    if key in GLOBAL_CONFIG_KEYS:
        return GLOBAL_SECTION
    if key in AI_CONFIG_KEYS or key.startswith(AI_CONFIG_PREFIXES):
        return AI_SECTION
    if key in SYSTEM_CONFIG_KEYS:
        return SYSTEM_SECTION
    if key in STRATEGY_CONFIG_KEYS:
        return STRATEGY_SECTION
    if key in LABEL_CONFIG_KEYS:
        return LABEL_SECTION
    return None


def _sections_for_scope(
    scope: str,
    *,
    version: int = BACKUP_VERSION,
) -> tuple[str, ...]:
    """按备份范围与备份版本返回节集合。

    版本演进：v1 无 system 节；v2 增加 system；v3（当前）增加 strategy/label
    两节（仅在 all 范围内整体导出，不提供单独节范围）。
    """
    if scope == ALL_SCOPE:
        if version == LEGACY_BACKUP_VERSION:
            return (GLOBAL_SECTION, AI_SECTION)
        if version == 2:
            return (GLOBAL_SECTION, AI_SECTION, SYSTEM_SECTION)
        return (
            GLOBAL_SECTION,
            AI_SECTION,
            SYSTEM_SECTION,
            STRATEGY_SECTION,
            LABEL_SECTION,
        )
    if scope in (GLOBAL_SECTION, AI_SECTION):
        return (scope,)
    if scope == SYSTEM_SECTION and version >= 2:
        return (scope,)
    raise ConfigBackupError("不支持的备份范围")


def _is_sensitive_record(section: str, record: BackupRecord) -> bool:
    if not record.value:
        return False
    if section == AI_SECTION and record.key.startswith("ai_account."):
        return True
    if section == GLOBAL_SECTION:
        return record.key in _GLOBAL_SENSITIVE_KEYS
    return section == SYSTEM_SECTION and record.key in _SYSTEM_BACKUP_SENSITIVE_KEYS


def build_backup_document(
    records: list[BackupRecord],
    scope: str,
    *,
    exported_at: datetime | None = None,
) -> dict[str, Any]:
    """从已读取的配置记录构建稳定、可测试的备份文档。"""
    selected_sections = _sections_for_scope(scope)
    grouped: dict[str, list[BackupRecord]] = {
        section: [] for section in selected_sections
    }

    for record in records:
        section = config_section_for_key(record.key)
        if section in grouped:
            grouped[section].append(record)

    contains_sensitive_values = False
    sections: dict[str, dict[str, Any]] = {}
    for section in selected_sections:
        section_records = sorted(grouped[section], key=lambda item: item.key)
        contains_sensitive_values = contains_sensitive_values or any(
            _is_sensitive_record(section, record) for record in section_records
        )
        sections[section] = {
            "count": len(section_records),
            "configs": [
                {
                    "key": record.key,
                    "value": record.value,
                    "description": record.description,
                }
                for record in section_records
            ],
        }

    timestamp = exported_at or now_utc()
    return {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "exported_at": format_rfc3339(timestamp),
        "scope": scope,
        "contains_sensitive_values": contains_sensitive_values,
        "sections": sections,
    }


async def export_config_backup(
    db: AsyncSession,
    scope: str,
) -> dict[str, Any]:
    """从 AppConfig 导出指定范围的配置。"""
    selected_sections = _sections_for_scope(scope)
    conditions = []
    if GLOBAL_SECTION in selected_sections:
        conditions.append(AppConfig.key_name.in_(GLOBAL_CONFIG_KEYS))
    if AI_SECTION in selected_sections:
        conditions.extend(
            [
                AppConfig.key_name.in_(AI_CONFIG_KEYS),
                *(
                    AppConfig.key_name.like(f"{prefix}%")
                    for prefix in AI_CONFIG_PREFIXES
                ),
            ]
        )
    if SYSTEM_SECTION in selected_sections:
        conditions.append(AppConfig.key_name.in_(SYSTEM_CONFIG_KEYS))
    if STRATEGY_SECTION in selected_sections:
        conditions.append(AppConfig.key_name.in_(STRATEGY_CONFIG_KEYS))
    if LABEL_SECTION in selected_sections:
        conditions.append(AppConfig.key_name.in_(LABEL_CONFIG_KEYS))

    result = await db.execute(
        select(AppConfig).where(or_(*conditions)).order_by(AppConfig.key_name)
    )
    rows = result.scalars().all()
    records = [
        BackupRecord(
            key=row.key_name,
            value=row.key_value,
            description=row.description,
        )
        for row in rows
    ]
    return build_backup_document(records, scope)


def serialize_config_backup(document: dict[str, Any]) -> bytes:
    """将备份文档序列化为 UTF-8 JSON。"""
    return json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")


def _validate_typed_config_value(key: str, value: str | None) -> None:
    if value is None:
        return

    expected_type = _get_field_type(key)
    normalized = value.strip()
    try:
        if expected_type is bool:
            if normalized.lower() not in {"true", "false", "1", "0", "yes", "no"}:
                raise ValueError
        elif expected_type is int:
            int(normalized)
        elif expected_type is float:
            float(normalized)
    except ValueError as exc:
        raise ConfigBackupError(f"配置项 {key} 的值类型无效") from exc


def _validate_ranged_value(
    key: str,
    value: str | None,
    ranges: dict[str, tuple[float, float | None]],
) -> None:
    if value is None or key not in ranges:
        return
    lo, hi = ranges[key]
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigBackupError(f"配置项 {key} 必须是数字") from exc
    if numeric < lo or (hi is not None and numeric > hi):
        raise ConfigBackupError(f"配置项 {key} 必须在 {lo} 到 {hi} 之间")


def _validate_global_record(record: BackupRecord) -> None:
    _validate_typed_config_value(record.key, record.value)
    _validate_ranged_value(record.key, record.value, DYNAMIC_CONFIG_RANGES)

    if record.value is not None and record.key in _POSITIVE_INTEGER_KEYS:
        try:
            if int(record.value) < 1:
                raise ValueError
        except ValueError as exc:
            raise ConfigBackupError(f"配置项 {record.key} 必须是正整数") from exc

    if record.value is not None and record.key in DYNAMIC_CONFIG_SELECT_OPTIONS:
        valid_values = {
            str(option["value"]) for option in DYNAMIC_CONFIG_SELECT_OPTIONS[record.key]
        }
        if record.value not in valid_values:
            raise ConfigBackupError(f"配置项 {record.key} 包含不支持的选项")

    if (
        record.key == "web_search_provider"
        and record.value is not None
        and record.value not in {"duckduckgo", "tavily"}
    ):
        raise ConfigBackupError("配置项 web_search_provider 包含不支持的选项")


def _validate_system_record(record: BackupRecord) -> None:
    _validate_typed_config_value(record.key, record.value)
    if record.key == "app_timezone":
        if (
            record.value is None
            or not record.value
            or record.value != record.value.strip()
        ):
            raise ConfigBackupError("系统配置 app_timezone 不得为空或包含首尾空格")
        try:
            resolve_timezone(record.value)
        except InvalidTimezoneError as exc:
            raise ConfigBackupError(
                "系统配置 app_timezone 必须是 system 或有效 IANA 时区"
            ) from exc
        return
    if record.value is None or not record.value.strip():
        return

    value = record.value.strip()
    if record.key == "database_url" and not value.startswith(
        (
            "mysql+aiomysql://",
            "mysql+asyncmy://",
            "mysql://",
            "postgresql+asyncpg://",
            "postgresql://",
        )
    ):
        raise ConfigBackupError("系统配置 database_url 格式无效")
    if record.key == "app_port":
        try:
            port = int(value)
        except ValueError as exc:
            raise ConfigBackupError("系统配置 app_port 必须是整数") from exc
        if not 1 <= port <= 65535:
            raise ConfigBackupError("系统配置 app_port 必须在 1 到 65535 之间")
    if record.key == "log_level" and value.upper() not in {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }:
        raise ConfigBackupError("系统配置 log_level 无效")


def _validate_ai_account(record: BackupRecord) -> None:
    account_id = record.key.removeprefix("ai_account.")
    if not _ACCOUNT_ID_RE.fullmatch(account_id):
        raise ConfigBackupError(f"AI 账号键无效: {record.key}")
    if not record.value:
        raise ConfigBackupError(f"AI 账号 {account_id} 缺少配置内容")

    try:
        account = json.loads(record.value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ConfigBackupError(f"AI 账号 {account_id} 不是有效 JSON") from exc
    if not isinstance(account, dict):
        raise ConfigBackupError(f"AI 账号 {account_id} 必须是对象")

    embedded_id = str(account.get("id") or "")
    if embedded_id and embedded_id != account_id:
        raise ConfigBackupError(f"AI 账号 {account_id} 的内部 ID 不匹配")
    if not str(account.get("name") or "").strip():
        raise ConfigBackupError(f"AI 账号 {account_id} 缺少名称")

    provider_id = str(account.get("provider_id") or account.get("provider") or "")
    protocol = str(account.get("protocol") or account.get("family") or "")
    if not provider_id or not protocol:
        raise ConfigBackupError(f"AI 账号 {account_id} 缺少厂商或协议")

    from backend.core.ai_protocol.endpoint_security import validate_provider_base_url
    from backend.core.ai_protocol.models import ProtocolFamily

    try:
        ProtocolFamily(protocol)
    except ValueError as exc:
        raise ConfigBackupError(f"AI 账号 {account_id} 的协议无效") from exc

    api_base = str(account.get("api_base") or account.get("base_url") or "")
    try:
        ok, message = validate_provider_base_url(
            provider_id,
            api_base,
            protocol=protocol,
        )
    except (KeyError, ValueError) as exc:
        raise ConfigBackupError(
            f"AI 账号 {account_id} 的厂商 {provider_id} 无效"
        ) from exc
    if not ok:
        raise ConfigBackupError(f"AI 账号 {account_id}: {message}")

    models = account.get("models") or []
    if not isinstance(models, list) or any(
        not isinstance(model, str) for model in models
    ):
        raise ConfigBackupError(f"AI 账号 {account_id} 的模型列表无效")


def _load_json_object(record: BackupRecord, label: str) -> dict[str, Any]:
    if not record.value:
        return {}
    try:
        payload = json.loads(record.value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ConfigBackupError(f"{label}不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise ConfigBackupError(f"{label}必须是对象")
    return payload


def _validate_assignment(assignment: dict[str, Any], label: str) -> None:
    account = assignment.get("account")
    model = assignment.get("model")
    if not isinstance(account, str) or not isinstance(model, str):
        raise ConfigBackupError(f"{label}的账号或模型无效")


def _validate_model_override(record: BackupRecord) -> None:
    payload = _load_json_object(record, f"模型覆盖 {record.key}")
    for key in ("context_window_tokens", "max_output_tokens"):
        value = payload.get(key)
        if value is not None and (not isinstance(value, int) or value < 0):
            raise ConfigBackupError(f"模型覆盖 {record.key} 的 {key} 无效")

    capabilities = payload.get("capabilities", {})
    reasoning_params = payload.get("reasoning_params", {})
    if not isinstance(capabilities, dict) or not isinstance(reasoning_params, dict):
        raise ConfigBackupError(f"模型覆盖 {record.key} 的能力配置无效")
    if any(not isinstance(value, bool) for value in capabilities.values()):
        raise ConfigBackupError(f"模型覆盖 {record.key} 的能力值必须是布尔值")

    effort = reasoning_params.get("effort")
    if effort is not None and effort not in {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }:
        raise ConfigBackupError(f"模型覆盖 {record.key} 的思考等级无效")
    thinking = reasoning_params.get("thinking")
    if thinking is not None and (
        not isinstance(thinking, dict)
        or thinking.get("type") not in {"adaptive", "disabled"}
    ):
        raise ConfigBackupError(f"模型覆盖 {record.key} 的思考模式无效")


def _validate_ai_record(record: BackupRecord) -> None:
    if record.key in AI_STRATEGY_CONFIG_KEYS:
        _validate_typed_config_value(record.key, record.value)
        _validate_ranged_value(record.key, record.value, _AI_STRATEGY_RANGES)
        return
    if record.key == "ai_role_bindings":
        bindings = _load_json_object(record, "AI 角色绑定")
        for role, binding in bindings.items():
            if not isinstance(role, str) or not isinstance(binding, dict):
                raise ConfigBackupError("AI 角色绑定结构无效")
            primary = binding.get("primary")
            fallback = binding.get("fallback", [])
            if not isinstance(primary, dict) or not isinstance(fallback, list):
                raise ConfigBackupError(f"AI 角色 {role} 的绑定结构无效")
            if any(not isinstance(item, dict) for item in fallback):
                raise ConfigBackupError(f"AI 角色 {role} 的回退链结构无效")
            _validate_assignment(primary, f"AI 角色 {role} 主绑定")
            for index, assignment in enumerate(fallback, start=1):
                _validate_assignment(
                    assignment,
                    f"AI 角色 {role} 第 {index} 个回退绑定",
                )
        return
    if record.key.startswith("ai_account."):
        _validate_ai_account(record)
        return
    if record.key.startswith("ai_model_override."):
        suffix = record.key.removeprefix("ai_model_override.")
        if "." not in suffix or suffix.startswith(".") or suffix.endswith("."):
            raise ConfigBackupError(f"模型覆盖键无效: {record.key}")
        _validate_model_override(record)
        return
    raise ConfigBackupError(f"不允许导入 AI 配置项 {record.key}")


def _validate_section_config_record(record: BackupRecord) -> None:
    """校验 strategy/label 节键记录。

    值为 null 表示清除该节覆盖（回退内置默认），直接放行；否则必须是
    合法 JSON 对象，且与内置默认合并后的有效值能通过节注册校验器，
    防止损坏或手改的备份把非法结构写入节键。
    """
    if record.value is None:
        return
    try:
        data = json.loads(record.value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ConfigBackupError(f"配置节 {record.key} 不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise ConfigBackupError(f"配置节 {record.key} 必须是 JSON 对象")
    validator = SECTION_VALIDATORS.get(record.key)
    if validator is None:
        raise ConfigBackupError(f"不允许导入配置节 {record.key}")
    try:
        validator(deep_merge(get_section_defaults(record.key), data))
    except ValueError as exc:
        raise ConfigBackupError(f"配置节 {record.key} 结构无效: {exc}") from exc


def parse_config_backup(content: bytes) -> dict[str, list[BackupRecord]]:
    """解析并严格校验上传的备份文件。"""
    if not content:
        raise ConfigBackupError("备份文件为空")
    if len(content) > BACKUP_MAX_BYTES:
        raise ConfigBackupError("备份文件超过 5 MiB 限制")

    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigBackupError("备份文件不是有效的 UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ConfigBackupError("备份文件顶层必须是对象")
    if payload.get("format") != BACKUP_FORMAT:
        raise ConfigBackupError("备份文件格式标识不匹配")
    version = payload.get("version")
    if version not in SUPPORTED_BACKUP_VERSIONS:
        raise ConfigBackupError("不支持此备份版本")
    if version >= 2:
        exported_at = payload.get("exported_at")
        if not isinstance(exported_at, str):
            raise ConfigBackupError("v2 备份缺少有效 exported_at")
        try:
            parse_rfc3339(exported_at)
        except ValueError as exc:
            raise ConfigBackupError("v2 备份 exported_at 必须是 RFC3339 时间") from exc

    scope = payload.get("scope")
    selected_sections = _sections_for_scope(scope, version=version)
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, dict):
        raise ConfigBackupError("备份文件缺少配置分类")
    if set(raw_sections) != set(selected_sections):
        raise ConfigBackupError("备份范围与配置分类不一致")

    parsed: dict[str, list[BackupRecord]] = {}
    seen_keys: set[str] = set()
    total_entries = 0
    for section in selected_sections:
        raw_section = raw_sections.get(section)
        if not isinstance(raw_section, dict):
            raise ConfigBackupError(f"配置分类 {section} 结构无效")
        raw_configs = raw_section.get("configs")
        if not isinstance(raw_configs, list):
            raise ConfigBackupError(f"配置分类 {section} 缺少 configs 列表")
        if raw_section.get("count", len(raw_configs)) != len(raw_configs):
            raise ConfigBackupError(f"配置分类 {section} 的数量校验失败")

        section_records: list[BackupRecord] = []
        for raw_record in raw_configs:
            if not isinstance(raw_record, dict):
                raise ConfigBackupError(f"配置分类 {section} 包含无效记录")
            key = raw_record.get("key")
            value = raw_record.get("value")
            description = raw_record.get("description")
            if not isinstance(key, str) or not key or len(key) > 100:
                raise ConfigBackupError("备份包含无效配置键")
            if value is not None and not isinstance(value, str):
                raise ConfigBackupError(f"配置项 {key} 的值必须是字符串或 null")
            if value is not None and len(value) > 1024 * 1024:
                raise ConfigBackupError(f"配置项 {key} 的值过大")
            if description is not None and (
                not isinstance(description, str) or len(description) > 255
            ):
                raise ConfigBackupError(f"配置项 {key} 的描述无效")
            if key in seen_keys:
                raise ConfigBackupError(f"配置项 {key} 重复")
            # 宽容恢复：未知键（历史版本备份中已移除的配置）跳过并告警，
            # 不阻断整个备份导入；键已知但节归属不符仍视为数据损坏，报错。
            if config_section_for_key(key) is None:
                logger.warning("备份包含未知配置键（已移除的历史配置），跳过: {}", key)
                continue
            if config_section_for_key(key) != section:
                raise ConfigBackupError(f"配置项 {key} 不属于 {section} 分类")

            record = BackupRecord(
                key=key,
                value=value,
                description=description,
            )
            if section == GLOBAL_SECTION:
                _validate_global_record(record)
            elif section == AI_SECTION:
                _validate_ai_record(record)
            elif section in SECTION_BACKUP_SECTIONS:
                _validate_section_config_record(record)
            else:
                _validate_system_record(record)
            section_records.append(record)
            seen_keys.add(key)

        total_entries += len(section_records)
        if total_entries > BACKUP_MAX_ENTRIES:
            raise ConfigBackupError("备份配置项数量过多")
        parsed[section] = section_records

    return parsed


async def restore_config_backup(
    db: AsyncSession,
    sections: dict[str, list[BackupRecord]],
    *,
    allow_database_url: bool = True,
) -> ConfigImportResult:
    """事务式精确恢复所选分类。

    ``database_url`` is allowed for Setup restores, where the final
    ``mark_setup_completed`` call writes the same value to ``connection.json``.
    A running deployment must opt out: a database transaction and a filesystem
    replacement cannot be made one atomic operation, so silently importing a
    new URL would leave the next restart pointed at the wrong database.
    """
    if not sections or not set(sections).issubset(
        {
            GLOBAL_SECTION,
            AI_SECTION,
            SYSTEM_SECTION,
            STRATEGY_SECTION,
            LABEL_SECTION,
        }
    ):
        raise ConfigBackupError("没有可导入的配置分类")

    selected_sections = tuple(
        section
        for section in (
            GLOBAL_SECTION,
            AI_SECTION,
            SYSTEM_SECTION,
            STRATEGY_SECTION,
            LABEL_SECTION,
        )
        if section in sections
    )
    conditions = []
    if GLOBAL_SECTION in sections:
        conditions.append(AppConfig.key_name.in_(GLOBAL_CONFIG_KEYS))
    if AI_SECTION in sections:
        conditions.extend(
            [
                AppConfig.key_name.in_(AI_CONFIG_KEYS),
                *(
                    AppConfig.key_name.like(f"{prefix}%")
                    for prefix in AI_CONFIG_PREFIXES
                ),
            ]
        )
    if SYSTEM_SECTION in sections:
        conditions.append(AppConfig.key_name.in_(SYSTEM_CONFIG_KEYS))
    if STRATEGY_SECTION in sections:
        conditions.append(AppConfig.key_name.in_(STRATEGY_CONFIG_KEYS))
    if LABEL_SECTION in sections:
        conditions.append(AppConfig.key_name.in_(LABEL_CONFIG_KEYS))

    try:
        result = await db.execute(select(AppConfig).where(or_(*conditions)))
        existing_rows = result.scalars().all()
        existing = {row.key_name: row for row in existing_rows}
        imported = {
            record.key: record
            for section_records in sections.values()
            for record in section_records
        }
        if not allow_database_url and "database_url" in imported:
            raise ConfigBackupError(
                "database_url 只能通过 Setup 恢复，以同步 connection.json；"
                " restore database_url through Setup so connection.json stays in sync"
            )

        # Exact section restore must not delete the live connection anchor when
        # a normal runtime backup intentionally omits database_url.
        protected_keys = set() if allow_database_url else {"database_url"}

        created = 0
        updated = 0
        deleted = 0
        unchanged = 0
        deleted_keys: set[str] = set()

        for key, row in existing.items():
            if key not in imported and key not in protected_keys:
                await db.delete(row)
                deleted += 1
                deleted_keys.add(key)

        for key, record in imported.items():
            row = existing.get(key)
            if row is None:
                db.add(
                    AppConfig(
                        key_name=key,
                        key_value=record.value,
                        description=record.description,
                    )
                )
                created += 1
            elif row.key_value != record.value or row.description != record.description:
                row.key_value = record.value
                row.description = record.description
                updated += 1
            else:
                unchanged += 1

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return ConfigImportResult(
        sections=selected_sections,
        created=created,
        updated=updated,
        deleted=deleted,
        unchanged=unchanged,
        imported_values={key: record.value for key, record in imported.items()},
        deleted_keys=frozenset(deleted_keys),
        requires_restart=bool(
            (set(imported) | deleted_keys) & set(RESTART_REQUIRED_KEYS)
        ),
    )


def refresh_imported_runtime_config(result: ConfigImportResult) -> None:
    """将恢复后的 DB 覆盖同步到当前进程的 Settings 与配置缓存。"""
    affected_keys = set(result.imported_values) | set(result.deleted_keys)

    # 统一节配置键不走动态配置缓存，单独同步 _section_store 并刷新 facade。
    section_keys = affected_keys & set(SECTION_REGISTRY)
    if section_keys:
        for key in section_keys:
            value = result.imported_values.get(key)
            data = None
            if value is not None:
                try:
                    parsed = json.loads(value)
                except TypeError, ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    data = parsed
            if data is None:
                clear_section_store(key)
            else:
                update_section_store(key, data)

        strategy_section_keys = section_keys & STRATEGY_CONFIG_KEYS
        label_section_keys = section_keys & LABEL_CONFIG_KEYS
        if strategy_section_keys:
            reload_strategy_config()
        if label_section_keys:
            # LabelService owns both the repository-label cache and the
            # conflict-rule snapshot.  Reload through its existing singleton
            # lifecycle so a backup import cannot leave either stale.
            from backend.services.label_service import label_service

            label_service.reload_labels()

    runtime_keys = affected_keys & (
        set(GLOBAL_CONFIG_KEYS) | set(AI_STRATEGY_CONFIG_KEYS) | set(SYSTEM_CONFIG_KEYS)
    )
    invalidate_dynamic_config_cache(list(runtime_keys))

    reset_keys = {
        key
        for key in runtime_keys
        if key not in RESTART_REQUIRED_KEYS
        and (key in result.deleted_keys or result.imported_values.get(key) is None)
    }
    defaults = Settings() if reset_keys else None
    settings = get_settings()
    for key in reset_keys:
        if key in Settings.model_fields:
            setattr(settings, key, getattr(defaults, key))

    for key, value in result.imported_values.items():
        # Restart-required settings (notably app_timezone) are persisted and
        # audited but never hot-applied to this frozen process.
        if (
            key in runtime_keys
            and key not in RESTART_REQUIRED_KEYS
            and value is not None
        ):
            update_settings_field(key, value)

    if "max_concurrent_issues" in affected_keys:
        from backend.workers.issue_worker import reset_issue_semaphore

        reset_issue_semaphore()
    if "max_concurrent_reviews" in affected_keys:
        from backend.workers.review_worker import reset_review_semaphore

        reset_review_semaphore()
