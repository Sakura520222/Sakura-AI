"""Shared configuration resolution utilities.

Consolidates dynamic config parsing patterns used across Agent Team services.
"""

from __future__ import annotations

from loguru import logger

from backend.core.config import DYNAMIC_CONFIG_RANGES, get_dynamic_config, get_settings


async def resolve_bool_config(key: str, fallback: bool) -> bool:
    """Resolve a boolean dynamic config value with fallback."""
    value = await get_dynamic_config(key)
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "启用", "是"}


async def resolve_int_config(key: str, fallback: int) -> int:
    """Resolve an integer dynamic config value with range validation."""
    value = await get_dynamic_config(key)
    if value is None:
        return fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "配置 {} 值 {} 无法转为整数，使用默认值 {}: {}", key, value, fallback, exc
        )
        return fallback
    min_value, max_value = DYNAMIC_CONFIG_RANGES.get(key, (parsed, parsed))
    if min_value <= parsed <= max_value:
        return parsed
    logger.warning(
        "配置 {} 值 {} 超出范围 {}-{}，使用默认值 {}",
        key,
        parsed,
        min_value,
        max_value,
        fallback,
    )
    return fallback


async def resolve_float_config(key: str, fallback: float) -> float:
    """Resolve a float dynamic config value with range validation."""
    value = await get_dynamic_config(key)
    if value is None:
        return fallback
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "配置 {} 值 {} 无法转为浮点数，使用默认值 {}: {}", key, value, fallback, exc
        )
        return fallback
    min_value, max_value = DYNAMIC_CONFIG_RANGES.get(key, (parsed, parsed))
    if min_value <= parsed <= max_value:
        return parsed
    logger.warning(
        "配置 {} 值 {} 超出范围 {}-{}，使用默认值 {}",
        key,
        parsed,
        min_value,
        max_value,
        fallback,
    )
    return fallback


async def resolve_clamped_int_config(
    config_key: str,
    settings_attr: str = "",
    log_label: str = "",
) -> int:
    """Read an integer config from dynamic settings with range clamping.

    Consolidates the pattern used by resolve_agent_team_max_tool_rounds
    and resolve_reviewer_max_tool_rounds.
    """
    attr = settings_attr or config_key
    settings = get_settings()
    fallback = getattr(settings, attr)
    label = log_label or config_key
    try:
        raw = await get_dynamic_config(config_key)
        if raw is None:
            return fallback
        value = int(raw)
        min_value, max_value = DYNAMIC_CONFIG_RANGES.get(config_key, (value, value))
        if min_value <= value <= max_value:
            return value
        raise ValueError(f"value {value} outside range {min_value}-{max_value}")
    except (TypeError, ValueError) as exc:
        logger.warning(
            "读取 {} 配置失败，使用默认值 {}: {}",
            label,
            fallback,
            exc,
        )
        return fallback
    except Exception as exc:
        logger.warning(
            "读取 {} 配置异常，使用默认值 {}: {}",
            label,
            fallback,
            exc,
        )
        return fallback
