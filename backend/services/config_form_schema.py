"""Presentation schemas for existing atomic configuration values; no new write path."""

from backend.core.config_sections import SECTION_REGISTRY
from backend.services.config_metadata import ConfigField, value_type


def value_schema(value):
    kind = value_type(value)
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {key: value_schema(child) for key, child in value.items()},
            "required": list(value),
            "additionalProperties": True,
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "items": value_schema(value[0]) if value else {"type": "string"},
        }
    result = {"type": kind}
    if isinstance(value, str) and ("\n" in value or len(value) > 160):
        result["format"] = "multiline"
    return result


def form_schema(item: ConfigField):
    if item.type not in {"object", "array"} or item.storage == "binding":
        return None
    if item.storage == "model" and item.path == ("reasoning_params", "thinking"):
        return {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["adaptive", "disabled"]}
                    },
                    "required": ["type"],
                    "additionalProperties": False,
                },
            ]
        }
    schema = value_schema(item.default)
    if not item.path and item.storage == "section":
        schema = {
            "type": "object",
            "additionalProperties": value_schema(next(iter(item.default.values()), {})),
            "x-fixedKeys": list(item.default),
            "x-keyLabel": "editor_label_name"
            if item.key.startswith("label.")
            else "editor_strategy_id",
        }
    if item.key == "strategy.review_policy" and item.path == ("repo_overrides",):
        defaults = SECTION_REGISTRY[item.key]["defaults"]
        schema = {
            "type": "object",
            "x-keyLabel": "editor_repository",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    key: value_schema(value)
                    for key, value in defaults.items()
                    if key != "repo_overrides"
                },
                "additionalProperties": True,
            },
        }
    if item.path == ("categories",) and item.key == "strategy.issue_analysis":
        schema["minItems"] = 1
        schema["items"]["properties"]["name"]["minLength"] = 1
    if item.key == "label.definitions" and not item.path:
        schema["additionalProperties"]["properties"]["color"].update(
            minLength=1, pattern="^#?[0-9a-fA-F]{6}$"
        )
    if item.key == "strategy.strategies" and not item.path:
        properties = schema["additionalProperties"]["properties"]
        for key in ("name", "prompt"):
            properties[key]["minLength"] = 1
        for key in ("max_files", "max_lines"):
            properties["conditions"]["properties"][key]["minimum"] = 1
    return schema
