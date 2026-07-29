"""Credential-safe, structure-preserving request/response artifact projection."""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any

_SECRET_KEY = re.compile(
    r"(?:authorization|cookie|api[_-]?key|access[_-]?token|password|passwd|"
    r"secret|credential|private[_-]?key|proxy[_-]?(?:url|auth)|bearer)",
    re.IGNORECASE,
)
_ENDPOINT_KEY = re.compile(
    r"(?:endpoint|base[_-]?url|api[_-]?base|request[_-]?url|url)$",
    re.IGNORECASE,
)


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, dict):
        return {key: item for key, item in raw.items() if not str(key).startswith("_")}
    return str(value)


def sanitize_projection(value: Any) -> Any:
    """Recursively remove credentials while retaining request/response shape."""
    value = _plain(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if _SECRET_KEY.search(key):
                result[key] = "[redacted]"
            elif _ENDPOINT_KEY.search(key):
                result[key] = "[endpoint-redacted]"
            else:
                result[key] = sanitize_projection(item)
        return result
    if isinstance(value, list):
        return [sanitize_projection(item) for item in value]
    return value


def projection_json(value: Any) -> str:
    return json.dumps(
        sanitize_projection(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = ["projection_json", "sanitize_projection"]
