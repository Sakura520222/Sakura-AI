"""Validated dynamic runtime limits, with Settings as the sole default source."""

from typing import Annotated

from loguru import logger
from pydantic import TypeAdapter, ValidationError

from backend.core.config import Settings, get_dynamic_config, get_settings


async def get_runtime_limits() -> dict[str, int]:
    settings = get_settings()
    limits = {}
    for name in (
        "max_model_rounds",
        "max_tool_calls",
        "max_parallel_reads",
        "max_no_progress_rounds",
    ):
        key = f"agent_team_{name}"
        field = Settings.model_fields[key]
        value = await get_dynamic_config(key, fresh=True)
        if value is None:
            value = getattr(settings, key)
        try:
            if isinstance(value, bool):
                raise ValueError("boolean limit")
            limits[name] = TypeAdapter(
                Annotated[field.annotation, *field.metadata]
            ).validate_python(value)
        except TypeError, ValueError, ValidationError:
            logger.warning(
                "Invalid Agent runtime limit {}; using Settings default", key
            )
            limits[name] = field.default
    return limits
