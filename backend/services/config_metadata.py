"""Configuration centre contract, independent of its HTML/React consumer.

Registries and model definitions own defaults. Runtime Settings never do.
Arrays and extensible maps are atomic; fixed dictionaries expose their leaves.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, get_args, get_origin

from backend.core.config import (
    AI_STRATEGY_CONFIG_KEYS,
    DYNAMIC_CONFIG_GROUPS,
    DYNAMIC_CONFIG_RANGES,
    DYNAMIC_CONFIG_SELECT_OPTIONS,
    DYNAMIC_CONFIG_SENSITIVE_KEYS,
    Settings,
    _get_field_type,
)
from backend.core.config_sections import SECTION_REGISTRY
from backend.services.system_config_service import (
    RESTART_REQUIRED_KEYS,
    SYSTEM_CONFIG_GROUPS,
    SYSTEM_SENSITIVE_KEYS,
)

CATEGORIES = (
    "personal",
    "ai",
    "review",
    "issue",
    "agent",
    "labels",
    "system",
    "backup",
)
PERSONAL_DEFAULTS = {"language": "zh-CN", "items_per_page": 20, "theme": "system"}
PERSONAL_OPTIONS = {
    "language": ["zh-CN", "en"],
    "items_per_page": [10, 20, 50, 100],
    "theme": ["system", "light", "dark"],
    "output_language": ["", "zh-CN", "en"],
}
AI_STRATEGY_RANGES = {
    "ai_api_timeout_seconds": (1.0, 3600.0),
    "ai_api_max_retries": (0, 20),
    "ai_api_initial_retry_delay_seconds": (0.0, 60.0),
    "ai_api_total_timeout_seconds": (1.0, 7200.0),
    "ai_fallback_max_candidates": (1, 10),
    "context_compression_threshold": (0.1, 1.0),
    "activity_artifact_retention_days": (1, 3650),
}
SENSITIVE_KEYS = (
    DYNAMIC_CONFIG_SENSITIVE_KEYS
    | SYSTEM_SENSITIVE_KEYS
    | {
        "database_url",
        "redis_url",
        "embedding_base_url",
        "rerank_base_url",
    }
)
# These objects have a domain-specific editor and must never enter generic writes.
DEDICATED_KEYS = {
    "agent_team_harness_plugins",
    "agent_team_permission_profile",
    "agent_team_mcp_io_timeout_seconds",
}
DEPLOYMENT_KEYS = frozenset({"database_url", "redis_url"})
RESTART_KEYS = RESTART_REQUIRED_KEYS
GROUP_CATEGORY = {
    "review_basic": "review",
    "review_strategy": "review",
    "pr_summary": "review",
    "pr_dependency_graph": "review",
    "semantic_issue_linking": "review",
    "issue_analysis": "issue",
    "repo_scan": "labels",
    "agent_team": "agent",
    "web_search": "agent",
    "fetch_url": "agent",
    "embedding": "ai",
    "rerank": "ai",
    "rag": "ai",
    "code_index": "review",
}
SECTION_CATEGORY = {
    "strategy.issue_analysis": "issue",
    "strategy.scan": "labels",
    "label.definitions": "labels",
    "label.recommendation": "labels",
    "label.conflict_rules": "labels",
}
ATOMIC_MAPS = {("strategy.review_policy", ("repo_overrides",))}


def builtin_value(key: str) -> Any:
    return deepcopy(Settings.model_fields[key].get_default(call_default_factory=True))


def value_type(value: Any) -> str:
    return {
        bool: "boolean",
        int: "integer",
        float: "number",
        list: "array",
        dict: "object",
    }.get(type(value), "string")


def leaf_items(node: dict, path: tuple[str, ...] = ()):
    for key, value in node.items():
        child = (*path, key)
        if isinstance(value, dict) and value:
            yield from leaf_items(value, child)
        else:
            yield child, value


@dataclass(frozen=True)
class ConfigField:
    id: str
    key: str
    path: tuple[str, ...]
    category: str
    group: str
    type: str
    default: Any
    scope: str = "global"
    storage: str = "app"
    sensitive: bool = False
    requires_restart: bool = False
    resettable: bool = True
    advanced: bool = False
    constraints: dict = field(default_factory=dict)

    @property
    def permission(self) -> str:
        return "authenticated" if self.scope == "user" else "super_admin"


def _setting_field(key: str, category: str, group: str) -> ConfigField:
    definition = Settings.model_fields[key]
    constraints = {}
    for metadata in definition.metadata:
        for name in ("ge", "gt", "le", "lt", "min_length", "max_length"):
            limit = getattr(metadata, name, None)
            if limit is not None:
                constraints[name] = limit
    ranges = {**DYNAMIC_CONFIG_RANGES, **AI_STRATEGY_RANGES}
    if key in ranges:
        constraints["ge"], upper = ranges[key]
        if upper is not None:
            constraints["le"] = upper
    options = DYNAMIC_CONFIG_SELECT_OPTIONS.get(key)
    if options:
        constraints["enum"] = [option["value"] for option in options]
    if get_origin(definition.annotation) is Literal:
        constraints["enum"] = list(get_args(definition.annotation))
    if key == "smtp_security":
        constraints["enum"] = ["ssl", "starttls", "none"]
    if key == "log_level":
        constraints["enum"] = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    if key == "app_port":
        constraints.update(ge=1, le=65535)
    default = builtin_value(key)
    sensitive = key in SENSITIVE_KEYS
    kind = {bool: "boolean", int: "integer", float: "number"}.get(
        _get_field_type(key), "string"
    )
    return ConfigField(
        id=f"global/{key}",
        key=key,
        path=(),
        category=category,
        group=group,
        type=kind,
        default=default,
        sensitive=sensitive,
        resettable=not sensitive,
        requires_restart=key in RESTART_KEYS,
        advanced=group
        not in {"review_basic", "issue_analysis", "ai_strategy", "application"},
        constraints=constraints,
    )


def field_catalog() -> dict[str, ConfigField]:
    fields = {}
    for group, spec in DYNAMIC_CONFIG_GROUPS.items():
        for key in spec["keys"]:
            if key not in DEDICATED_KEYS:
                item = _setting_field(key, GROUP_CATEGORY.get(group, "system"), group)
                fields[item.id] = item
    for key in AI_STRATEGY_CONFIG_KEYS:
        item = _setting_field(key, "ai", "ai_strategy")
        fields[item.id] = item
    for group in SYSTEM_CONFIG_GROUPS:
        for key in group["keys"]:
            item = _setting_field(key, "system", group["id"])
            fields[item.id] = item
    for key, section in SECTION_REGISTRY.items():
        for path, default in leaf_items(section["defaults"]):
            item = ConfigField(
                id="section/" + key + "/" + "/".join(path),
                key=key,
                path=path,
                category=SECTION_CATEGORY.get(key, "review"),
                group=key,
                type=value_type(default),
                default=deepcopy(default),
                storage="section",
                advanced=len(path) > 1
                or "prompt" in path[-1]
                or "template" in path[-1],
            )
            fields[item.id] = item
        # Extensible maps retain a whole-map editor for adding/removing entries.
        if key in {"label.definitions", "label.conflict_rules", "strategy.strategies"}:
            item = ConfigField(
                id=f"section/{key}",
                key=key,
                path=(),
                category=SECTION_CATEGORY.get(key, "review"),
                group=key,
                type="object",
                default=deepcopy(section["defaults"]),
                storage="section",
                advanced=True,
            )
            fields[item.id] = item
    # mode is historically resolved from a flat setting when the section omits it.
    item = _setting_field("pr_dependency_graph_mode", "review", "pr_dependency_graph")
    fields[item.id] = item
    for key in (*PERSONAL_DEFAULTS, "output_language"):
        item = ConfigField(
            id=f"personal/{key}",
            key=key,
            path=(),
            category="personal",
            group="preferences",
            type="integer" if key == "items_per_page" else "string",
            default=PERSONAL_DEFAULTS.get(key, builtin_value("output_language")),
            scope="user",
            storage="preference",
            constraints={"enum": PERSONAL_OPTIONS[key]},
        )
        fields[item.id] = item
    for role in ("main", "summary", "agent_team"):
        item = ConfigField(
            id=f"binding/{role}",
            key="ai_role_bindings",
            path=(role,),
            category="ai",
            group="bindings",
            type="object",
            default={},
            storage="binding",
            resettable=False,  # No safe unconfigured binding; never detach accounts implicitly.
        )
        fields[item.id] = item
    return fields


def model_fields(provider: str, model: str) -> dict[str, ConfigField]:
    from dataclasses import asdict
    from urllib.parse import quote

    from backend.core.ai_protocol.role_config import _build_metadata
    from backend.core.ai_providers import BUILTIN_PROVIDERS

    key = f"ai_model_override.{provider}.{model}"
    if provider not in BUILTIN_PROVIDERS or not model.strip() or len(key) > 100:
        raise ValueError("Invalid provider or model")
    metadata = asdict(_build_metadata(provider, model))
    defaults = {
        name: metadata[name]
        for name in (
            "context_window_tokens",
            "max_output_tokens",
            "capabilities",
            "reasoning_params",
        )
    }
    result = {}
    for path, value in leaf_items(defaults):
        if path == ("reasoning_params", "max_output_tokens"):
            continue  # Runtime derives this from the top-level output limit.
        kind = value_type(value)
        constraints = {}
        if path[0] != "capabilities" and path[-1] in {
            "context_window_tokens",
            "max_output_tokens",
            "top_k",
        }:
            kind, constraints = "integer", {"ge": 1}
        elif path[0] != "capabilities" and path[-1] in {"temperature", "top_p"}:
            kind, constraints = "number", {"ge": 0}
        if path == ("reasoning_params", "thinking"):
            kind = "object"
        if path[-1] == "effort" and path[0] == "reasoning_params":
            constraints = {
                "enum": ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
            }
        item = ConfigField(
            id=f"model/{provider}/{quote(model, safe='')}/" + "/".join(path),
            key=key,
            path=path,
            category="ai",
            group="model",
            type=kind,
            default=value,
            storage="model",
            constraints=constraints,
        )
        result[item.id] = item
    return result


def lookup_path(node: Any, path: tuple[str, ...], fallback=None):
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return fallback
        node = node[part]
    return deepcopy(node)


def replace_path(
    node: dict, path: tuple[str, ...], value: Any, *, remove=False
) -> dict:
    result = deepcopy(node)
    if not path:
        return {} if remove else deepcopy(value)
    current = result
    parents = []
    for part in path[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        parents.append((current, part))
        current = child
    if remove:
        current.pop(path[-1], None)
        for parent, part in reversed(parents):
            if not parent[part]:
                parent.pop(part)
    else:
        current[path[-1]] = deepcopy(value)
    return result
