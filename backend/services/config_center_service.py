"""Typed configuration projections and transactional field PATCH/RESET.

The service owns authorization as well as validation. Neither an HTML form nor
a JSON client may bypass the same storage, concurrency and runtime contract.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.core.config import (
    Settings,
    _cast_config_type,
    _get_field_type,
    effective_log_level,
    get_settings,
    invalidate_user_dynamic_config_cache,
)
from backend.core.config_sections import (
    _PRUNED,
    SECTION_REGISTRY,
    _prune_default_equal_leaves,
    deep_merge,
    get_section_config,
)
from backend.core.log_sanitizer import sensitive_log_scope
from backend.models.admin_action_log import AdminActionLog
from backend.models.database import AppConfig, ConfigRevision, UserConfig, WebUIConfig
from backend.services.config_metadata import (
    CATEGORIES,
    DEPLOYMENT_KEYS,
    PERSONAL_DEFAULTS,
    ConfigField,
    field_catalog,
    lookup_path,
    model_fields,
    replace_path,
    value_type,
)
from backend.services.config_runtime import apply_config_values, config_runtime_sync
from backend.services.config_store import config_version, lock_config
from backend.services.section_config_service import validate_section_config
from backend.services.system_config_service import (
    SYSTEM_CONFIG_UPDATE_KEYS,
    system_config_service,
)

FIELD_TYPES = {
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
    "string": (str,),
    "array": (list,),
    "object": (dict,),
}


class ConfigError(ValueError):
    def __init__(
        self, code: str, *, status: int = 422, field_id: str = "", detail: str = ""
    ):
        self.code, self.status, self.field_id, self.detail = (
            code,
            status,
            field_id,
            detail,
        )
        super().__init__(code)


class ConfigChanges(dict):
    """Legacy change mapping with an explicit post-commit outcome."""

    effect = "live"
    pending_fields: tuple[str, ...] = ()

    def status(self):
        return {
            "persisted": True,
            "effect": self.effect,
            "pending_fields": list(self.pending_fields),
        }


def serialize_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def validate_field(item: ConfigField, value: Any, *, strict=True) -> Any:
    """Reject coercion at the JSON boundary; legacy forms explicitly opt in."""
    if item.key in DEPLOYMENT_KEYS:
        raise ConfigError("deployment_owned", field_id=item.id)
    if not strict and item.type in {"boolean", "integer", "number"}:
        if isinstance(value, bool) and item.type != "boolean":
            raise ConfigError("invalid_type", field_id=item.id)
        if item.type == "boolean":
            if str(value).lower() not in {"true", "false", "1", "0", "yes", "no"}:
                raise ConfigError("invalid_type", field_id=item.id)
        try:
            value = _cast_config_type(
                value, {"boolean": bool, "integer": int, "number": float}[item.type]
            )
        except ValueError, TypeError:
            raise ConfigError("invalid_type", field_id=item.id) from None
    if type(value) not in FIELD_TYPES[item.type]:
        raise ConfigError("invalid_type", field_id=item.id)
    if item.type == "number" and not math.isfinite(value):
        raise ConfigError("invalid_type", field_id=item.id)
    constraints = item.constraints
    if item.key.endswith("_currency") and isinstance(value, str):
        from backend.core.config import validate_currency_config_value

        try:
            value = validate_currency_config_value(item.key, value)
        except ValueError:
            raise ConfigError("invalid_choice", field_id=item.id) from None
    if "enum" in constraints and value not in constraints["enum"]:
        raise ConfigError("invalid_choice", field_id=item.id)
    for name, valid in (
        ("ge", lambda limit: value >= limit),
        ("gt", lambda limit: value > limit),
        ("le", lambda limit: value <= limit),
        ("lt", lambda limit: value < limit),
        ("min_length", lambda limit: len(value) >= limit),
        ("max_length", lambda limit: len(value) <= limit),
    ):
        if name in constraints and not valid(constraints[name]):
            raise ConfigError(
                "out_of_range", field_id=item.id, detail=f"{name} {constraints[name]}"
            )
    if item.sensitive and (
        not value.strip() or "****" in value or value == "[REDACTED]"
    ):
        raise ConfigError("credential_required", field_id=item.id)
    if item.storage == "app" and item.key in SYSTEM_CONFIG_UPDATE_KEYS:
        try:
            normalized = system_config_service.validate_updates({item.key: value})[
                item.key
            ]
            value = _cast_config_type(normalized, _get_field_type(item.key))
        except ValueError:
            raise ConfigError("invalid_value", field_id=item.id) from None
    return value


def _parse_override(raw: str | None) -> dict:
    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except ValueError, TypeError:
        return {}


def _strict_tree(default: Any, value: Any) -> None:
    """Check known leaves; extensible map validation belongs to its domain."""
    if default is None and value is None:
        return
    if isinstance(default, dict) and isinstance(value, dict):
        for key in default.keys() & value.keys():
            _strict_tree(default[key], value[key])
    elif isinstance(default, float):
        if type(value) not in {float, int} or not math.isfinite(value):
            raise ConfigError("invalid_type")
    # Nullable leaves use the same non-null type as their field descriptor.
    elif type(value) not in FIELD_TYPES[value_type(default)]:
        raise ConfigError("invalid_type")


class ConfigCenterService:
    async def _apply_committed(self, values, revisions):
        """Persistence has succeeded; a failed effect is retryable, not a failed write."""
        try:
            await apply_config_values(values)
        except BaseException as exc:
            config_runtime_sync.pending.update(revisions)
            if not isinstance(exc, Exception):
                raise
            logger.exception("Configuration committed; runtime application pending")
            return "pending"
        config_runtime_sync.record_applied(revisions)
        return "live"

    async def _clear_depgraph_mode(self, db, revisions):
        """The historical section mode wins over the flat key at runtime.

        Move only that leaf back to the canonical scalar path, retaining prompts.
        All callers already hold validation/global.
        """
        key = "strategy.pr_dependency_graph"
        revision = await lock_config(db, "app/" + key)
        row = (
            await db.execute(
                select(AppConfig).where(AppConfig.key_name == key).with_for_update()
            )
        ).scalar_one_or_none()
        raw = _parse_override(row.key_value if row else None)
        if "mode" in raw:
            raw.pop("mode")
            value = json.dumps(raw, ensure_ascii=False) if raw else None
            await self._write_app(db, key, value)
            revision.revision += 1
            revisions["app/" + key] = revision.revision
            return {key: value}
        return {}

    async def save_model_overrides(self, db, provider, model, payload, *, reset=False):
        """Compatibility model editor: validate submitted leaves and preserve siblings."""
        fields = model_fields(provider, model)
        by_path = {item.path: item for item in fields.values()}
        from backend.services.config_metadata import leaf_items

        for path, value in leaf_items(payload):
            if path == ("reasoning_params", "thinking", "type"):
                if value not in {"adaptive", "disabled"}:
                    raise ConfigError("invalid_choice")
            elif path not in by_path:
                raise ConfigError("unknown_field")
            elif value is not None:
                validate_field(by_path[path], value)
        key = next(iter(fields.values())).key
        try:
            await lock_config(db, "validation/global")
            revision = await lock_config(db, "app/" + key)
            row = (
                await db.execute(
                    select(AppConfig).where(AppConfig.key_name == key).with_for_update()
                )
            ).scalar_one_or_none()
            value = (
                {}
                if reset
                else deep_merge(
                    _parse_override(row.key_value if row else None), payload
                )
            )
            # Null explicitly removes an override; it is never sent as a capability value.
            for path, leaf in list(leaf_items(value)):
                if leaf is None:
                    value = replace_path(value, path, None, remove=True)
            await self._write_app(
                db, key, json.dumps(value, ensure_ascii=False) if value else None
            )
            revision.revision += 1
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return value

    async def save_app_updates(self, db, updates, *, strict=False):
        """Compatibility entry point for legacy forms/APIs, with one commit.

        Absence never means false. The caller supplies only submitted fields.
        """
        catalog = {
            item.key: item for item in field_catalog().values() if item.storage == "app"
        }
        if set(updates) - catalog.keys():
            raise ConfigError("unknown_field")
        normalized = {
            key: serialize_value(validate_field(catalog[key], value, strict=strict))
            for key, value in updates.items()
        }
        secrets = [value for key, value in normalized.items() if catalog[key].sensitive]
        async with config_runtime_sync.lock:
            with sensitive_log_scope(*secrets):
                try:
                    await lock_config(db, "validation/global")
                    locks = {
                        key: await lock_config(db, "app/" + key)
                        for key in sorted(normalized)
                    }
                    await self.validate_app_combination(db, normalized)
                    result = await db.execute(
                        select(AppConfig)
                        .where(AppConfig.key_name.in_(normalized))
                        .execution_options(populate_existing=True)
                    )
                    old = {
                        row.key_name: row.key_value for row in result.scalars().all()
                    }
                    changed = ConfigChanges()
                    committed_revisions = {}
                    aliases = {}
                    for key, value in normalized.items():
                        if key == "pr_dependency_graph_mode":
                            aliases.update(
                                await self._clear_depgraph_mode(db, committed_revisions)
                            )
                        if old.get(key) == value:
                            continue
                        await self._write_app(db, key, value)
                        locks[key].revision += 1
                        changed[key] = {
                            "old": "****" if catalog[key].sensitive else old.get(key),
                            "new": "****" if catalog[key].sensitive else value,
                            "raw_new": value,
                        }
                    # Retry a committed/pending write even when its value is unchanged.
                    values = dict(aliases)
                    for key, value in normalized.items():
                        unit = "app/" + key
                        revision = locks[key].revision
                        if (
                            key in changed
                            or unit in config_runtime_sync.pending
                            or (
                                revision
                                and config_runtime_sync.seen.get(unit, 0) != revision
                            )
                        ):
                            values[key] = value
                            committed_revisions[unit] = revision
                    if changed or aliases:
                        await db.commit()
                    else:
                        await db.rollback()
                except BaseException:
                    await db.rollback()
                    raise
                changed.effect = await self._apply_committed(
                    values, committed_revisions
                )
                if changed.effect == "pending":
                    changed.pending_fields = tuple(sorted(values))
                elif any(catalog[key].requires_restart for key in changed):
                    changed.effect = "restart_required"
        return changed

    async def save_preferences(self, db, user, updates):
        items = {key: self.resolve("personal/" + key, user) for key in updates}
        normalized = {
            key: validate_field(items[key], value) if value is not None else None
            for key, value in updates.items()
        }
        try:
            await lock_config(db, f"validation/user/{int(user['user_id'])}")
            for key in sorted(normalized):
                revision = await lock_config(db, self.unit(items[key], user))
                await self._write_preference(
                    db, items[key], user, normalized[key], normalized[key] is None
                )
                revision.revision += 1
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        from backend.webui.deps import invalidate_user_prefs_cache

        invalidate_user_prefs_cache(int(user["user_id"]))
        invalidate_user_dynamic_config_cache(int(user["user_id"]))

    async def save_section(self, db, section_key, data, *, mode="replace", reset=False):
        """Shared full-section compatibility path; locks match leaf mutations."""
        from backend.services.section_config_service import (
            _PROMPT_HEAVY_SECTIONS,
            _leaf_diff,
        )

        if section_key not in SECTION_REGISTRY:
            raise KeyError(section_key)
        if not isinstance(data, dict) or mode not in {"replace", "patch"}:
            raise ValueError("Invalid section payload")
        async with config_runtime_sync.lock:
            try:
                await lock_config(db, "validation/global")
                revision = await lock_config(db, "app/" + section_key)
                row = (
                    await db.execute(
                        select(AppConfig)
                        .where(AppConfig.key_name == section_key)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                old = _parse_override(row.key_value if row else None)
                defaults = SECTION_REGISTRY[section_key]["defaults"]
                override = (
                    {}
                    if reset
                    else deep_merge(old, data)
                    if mode == "patch"
                    else deepcopy(data)
                )
                effective = deep_merge(defaults, override)
                validate_section_config(section_key, effective)
                _strict_tree(defaults, effective)
                pruned = _prune_default_equal_leaves(defaults, effective)
                raw = (
                    None
                    if pruned is _PRUNED
                    else json.dumps(pruned, ensure_ascii=False, sort_keys=True)
                )
                changed = (
                    (row is not None)
                    if reset
                    else raw != (row.key_value if row else None)
                )
                changes = _leaf_diff(
                    deep_merge(defaults, old),
                    effective,
                    digest=section_key in _PROMPT_HEAVY_SECTIONS,
                )
                if changed:
                    await self._write_app(db, section_key, raw)
                    revision.revision += 1
                committed_revisions = {"app/" + section_key: revision.revision}
                if changed:
                    await db.commit()
                else:
                    await db.rollback()
            except BaseException:
                await db.rollback()
                raise
            effect = await self._apply_committed(
                {section_key: raw}, committed_revisions
            )
        return {
            "section": section_key,
            "changed": changed,
            "existed": changed,
            "changes": changes,
            "persisted": True,
            "effect": effect,
        }

    def allowed_fields(self, user: dict) -> dict[str, ConfigField]:
        return {
            key: item
            for key, item in field_catalog().items()
            if item.scope == "user" or user.get("role") == "super_admin"
        }

    def resolve(self, field_id: str, user: dict) -> ConfigField:
        item = self.allowed_fields(user).get(field_id)
        if field_id.startswith("model/") and user.get("role") == "super_admin":
            from urllib.parse import unquote

            parts = field_id.split("/", 3)
            if len(parts) == 4:
                try:
                    item = model_fields(parts[1], unquote(parts[2])).get(field_id)
                except ValueError:
                    pass
        if item is None:
            # No distinction between a hidden administrative field and an unknown field.
            raise ConfigError("unknown_field", status=404)
        return item

    @staticmethod
    def unit(item: ConfigField, user: dict) -> str:
        if item.scope == "user":
            return f"user/{int(user['user_id'])}/{item.key}"
        return "app/" + item.key

    async def _snapshot(self, db, user: dict, extra=None, *, lock=False):
        async def query(statement):
            return await db.execute(statement.with_for_update() if lock else statement)

        fields = {} if lock else self.allowed_fields(user)
        fields.update(extra or {})
        app_keys = {item.key for item in fields.values() if item.scope == "global"}
        # Personal output language may inherit this one public preference only.
        app_keys.add("output_language")
        if "pr_dependency_graph_mode" in app_keys:
            app_keys.add("strategy.pr_dependency_graph")
        result = await query(
            select(AppConfig)
            .where(AppConfig.key_name.in_(app_keys))
            .execution_options(populate_existing=True)
        )
        app = {row.key_name: row for row in result.scalars().all()}
        result = await query(
            select(WebUIConfig)
            .where(WebUIConfig.user_id == int(user["user_id"]))
            .execution_options(populate_existing=True)
        )
        prefs = result.scalar_one_or_none()
        result = await query(
            select(UserConfig)
            .where(
                UserConfig.user_id == int(user["user_id"]),
                UserConfig.config_key.in_(["output_language"]),
            )
            .execution_options(populate_existing=True)
        )
        personal = {row.config_key: row for row in result.scalars().all()}
        units = {self.unit(item, user) for item in fields.values()}
        if "pr_dependency_graph_mode" in app_keys:
            units.add("app/strategy.pr_dependency_graph")
        result = await query(
            select(ConfigRevision)
            .where(ConfigRevision.scope_key.in_(units))
            .execution_options(populate_existing=True)
        )
        versions = {row.scope_key: row.revision for row in result.scalars().all()}
        return app, prefs, personal, versions

    def _state(self, item, user, snapshot, deployment):
        app, prefs, personal, versions = snapshot
        source, override = "builtin", None
        if item.storage == "preference":
            if item.key == "output_language":
                global_row = app.get(item.key)
                inherited = (
                    _cast_config_type(global_row.key_value, str)
                    if global_row is not None and global_row.key_value is not None
                    else getattr(deployment, item.key)
                )
                source = (
                    "database"
                    if global_row is not None and global_row.key_value is not None
                    else (
                        "environment"
                        if item.key in deployment.model_fields_set
                        else "builtin"
                    )
                )
                row = personal.get(item.key)
                if row is not None and row.config_value is not None:
                    override, source = row.config_value, "user"
                effective = override if override is not None else inherited
                identity = [getattr(row, "updated_at", None), override, effective]
            else:
                inherited = PERSONAL_DEFAULTS[item.key]
                override = getattr(prefs, item.key, None) if prefs else None
                effective = override if override is not None else inherited
                source = "user" if override is not None else "builtin"
                identity = [getattr(prefs, "updated_at", None), override]
        elif item.storage in {"section", "binding", "model"}:
            row = app.get(item.key)
            raw = _parse_override(row.key_value if row else None)
            effective = lookup_path(raw, item.path, deepcopy(item.default))
            if isinstance(item.default, dict) and isinstance(effective, dict):
                effective = deep_merge(item.default, effective)
            inherited = deepcopy(item.default)
            marker = object()
            override = lookup_path(raw, item.path, marker)
            source = (
                "database"
                if override is not marker and (item.path or raw)
                else "builtin"
            )
            identity = [getattr(row, "updated_at", None), raw]
        else:
            inherited = getattr(deployment, item.key)
            if item.key == "log_level":
                inherited = effective_log_level(deployment)
            source = (
                "environment" if item.key in deployment.model_fields_set else "builtin"
            )
            if item.key == "log_level" and deployment.is_development:
                source = "environment"
            row = app.get(item.key)
            override = row.key_value if row else None
            effective = (
                _cast_config_type(override, _get_field_type(item.key))
                if override is not None
                else inherited
            )
            if override is not None:
                source = "database"
            identity = [getattr(row, "updated_at", None), override]
            if item.key == "pr_dependency_graph_mode":
                section_row = app.get("strategy.pr_dependency_graph")
                section = _parse_override(
                    section_row.key_value if section_row else None
                )
                if "mode" in section:
                    effective, source = section["mode"], "database"
                identity.extend(
                    [
                        section.get("mode"),
                        versions.get("app/strategy.pr_dependency_graph", 0),
                    ]
                )
        revision = versions.get(self.unit(item, user), 0)
        if item.key.endswith("_currency") and isinstance(effective, str):
            from backend.core.config import validate_currency_config_value

            try:
                effective = validate_currency_config_value(item.key, effective)
            except ValueError:
                pass  # Keep unsupported historical evidence visible without rewriting it.
        if item.key in DEPLOYMENT_KEYS:
            effective = getattr(get_settings(), item.key, None)
            source = "deployment"
        if item.sensitive:
            identity = [bool(effective), revision]
        pending = self.unit(item, user) in config_runtime_sync.pending
        if revision and item.storage == "app" and not item.requires_restart:
            current = (
                effective_log_level(get_settings())
                if item.key == "log_level"
                else getattr(get_settings(), item.key, effective)
            )
            pending = pending or effective != current
        elif revision and item.storage == "section":
            # Remote commits cannot populate this process's pending set. Compare
            # the cached section value until the revision poll applies the change.
            current = lookup_path(get_section_config(item.key), item.path, item.default)
            pending = pending or effective != current
        return {
            "value": None if item.sensitive else effective,
            "default": None if item.sensitive else deepcopy(item.default),
            "inheritedValue": None if item.sensitive else inherited,
            "source": source,
            "hasValue": bool(effective),
            "version": config_version(revision, identity, source),
            "overridden": source == ("user" if item.scope == "user" else "database"),
            "effect": "restart_required"
            if item.requires_restart
            and effective != getattr(get_settings(), item.key, effective)
            else "pending"
            if pending
            else "live",
        }

    @staticmethod
    def _text(item, lang):
        from backend.webui.i18n import i18n

        aliases = {
            "enable_context_compression": "ai_enable_context_compression",
            "context_compression_threshold": "ai_context_compression_threshold",
            "activity_reasoning_capture_enabled": "ai_capture_reasoning",
            "activity_request_response_capture_enabled": "ai_capture_request_response",
            "activity_reasoning_provider_allowlist": "ai_reasoning_provider_allowlist",
            "activity_reasoning_protocol_allowlist": "ai_reasoning_protocol_allowlist",
            "activity_artifact_retention_days": "ai_artifact_retention_days",
            "activity_artifact_encryption_key_id": "ai_artifact_key_id",
            "activity_artifact_super_admin_read_enabled": "ai_super_admin_artifact_access",
        }
        candidates = [
            f"config_center.label.{item.key}",
            f"config.label.{item.key}",
            f"system_config.key_{item.key}",
            f"config.{item.key}",
        ]
        if item.key in aliases:
            candidates.insert(0, f"config.{aliases[item.key]}")
        if item.storage == "section" and not item.path:
            candidates.insert(0, f"config_center.group.{item.key}")
        if item.scope == "user":
            candidates.insert(0, f"settings.{item.key}")
        label = (
            item.path[-1].replace("_", " ") if item.path else item.key.replace("_", " ")
        )
        for key in candidates:
            translated = i18n.t(key, lang=lang)
            if translated != key:
                label = translated
                break
        if item.path:
            parts = []
            for part in item.path:
                key = f"config_center.field.{part}"
                translated = i18n.t(key, lang=lang)
                parts.append(
                    part.replace("_", " ") if translated == key else translated
                )
            label = " / ".join(parts)
            if item.storage == "binding":
                label = i18n.t(f"config.ai_role_{item.path[0]}", lang=lang)
        desc_keys = [
            "config_center.help." + ".".join([item.key, *item.path]),
            f"config.desc.{item.key}",
            f"system_config.key_{item.key}_desc",
            f"config.{item.key}_desc",
        ]
        if item.path:
            desc_keys.insert(1, f"config_center.help.{item.path[-1]}")
        if item.storage == "binding":
            desc_keys.insert(0, "config_center.binding_help")
        if item.key.startswith("activity_"):
            desc_keys.append("config.ai_observability_capture_desc")
        description = ""
        for key in desc_keys:
            translated = i18n.t(key, lang=lang)
            if translated != key:
                description = translated
                break
        if (
            item.key == "strategy.issue_analysis"
            and item.path
            and item.path[-1].startswith("comment_template")
        ):
            description = i18n.t("strategy.issue_template_layout_hint", lang=lang)
        return label, description

    async def describe(
        self, db, user, *, category=None, lang="zh-CN", search=False, model=None
    ):
        from backend.services.config_form_schema import form_schema
        from backend.webui.i18n import i18n

        allowed = self.allowed_fields(user)
        if model:
            if user.get("role") != "super_admin":
                raise ConfigError("unknown_field", status=404)
            try:
                allowed = model_fields(*model)
            except ValueError:
                raise ConfigError("unknown_field", status=404) from None
        if category and category not in CATEGORIES:
            raise ConfigError("unknown_category", status=404)
        if category and category != "personal" and user.get("role") != "super_admin":
            raise ConfigError("unknown_category", status=404)
        snapshot = None if search else await self._snapshot(db, user, allowed)
        deployment = Settings()
        result = []
        for item in allowed.values():
            if category and item.category != category:
                continue
            label, description = self._text(item, lang)
            other_label, other_description = self._text(
                item, "en" if lang == "zh-CN" else "zh-CN"
            )
            view = {
                "id": item.id,
                "key": item.key,
                "path": list(item.path),
                "category": item.category,
                "group": item.group,
                "type": item.type,
                "label": label,
                "description": description,
                "searchText": f"{item.id} {label} {description} {other_label} {other_description}",
                "scope": item.scope,
                "constraints": item.constraints,
                "sensitive": item.sensitive,
                "requiresRestart": item.requires_restart,
                "writable": item.key not in DEPLOYMENT_KEYS,
                "resettable": item.resettable,
                "permission": item.permission,
                "advanced": item.advanced,
                "submission": "confirmed" if item.sensitive else "automatic",
            }
            view["editorSchema"] = form_schema(item)
            view["optionLabels"] = {}
            for option in item.constraints.get("enum", []):
                option_key = f"config_center.option.{option}"
                translated = i18n.t(option_key, lang=lang)
                if option == "" and item.key in {
                    "output_language",
                    "star_aid_summary_language",
                }:
                    translated = i18n.t(
                        "config_center.follow_request_language", lang=lang
                    )
                view["optionLabels"][str(option)] = (
                    str(option) if translated == option_key else translated
                )
            if snapshot is not None:
                view.update(self._state(item, user, snapshot, deployment))
            result.append(view)
        return result

    async def _write_app(self, db, key, value):
        row = (
            await db.execute(
                select(AppConfig)
                .where(AppConfig.key_name == key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if value is None:
            if row:
                await db.delete(row)
        elif row:
            row.key_value = value
        else:
            db.add(AppConfig(key_name=key, key_value=value, description=key))

    async def _write_preference(self, db, item, user, value, reset):
        uid = int(user["user_id"])
        if item.key == "output_language":
            row = (
                await db.execute(
                    select(UserConfig)
                    .where(UserConfig.user_id == uid, UserConfig.config_key == item.key)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if reset:
                if row:
                    await db.delete(row)
            elif row:
                row.config_value = value
            else:
                db.add(UserConfig(user_id=uid, config_key=item.key, config_value=value))
        else:
            row = (
                await db.execute(
                    select(WebUIConfig)
                    .where(WebUIConfig.user_id == uid)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if not row:
                row = WebUIConfig(
                    user_id=uid, language=None, items_per_page=None, theme=None
                )
                db.add(row)
            setattr(row, item.key, None if reset else value)

    async def mutate(
        self, db, user, field_id, *, value=None, version, reset=False, confirmed=False
    ):
        item = self.resolve(field_id, user)
        if reset and not item.resettable:
            raise ConfigError("not_resettable", field_id=field_id)
        if item.sensitive and not confirmed:
            raise ConfigError("confirmation_required", field_id=field_id)
        if not reset:
            value = validate_field(item, value)
        async with config_runtime_sync.lock:
            with sensitive_log_scope(value if item.sensitive else None):
                try:
                    # Shared validation lock serializes cross-field invariants and
                    # the first creation of the user's multi-column preference row.
                    await lock_config(
                        db,
                        "validation/global"
                        if item.scope == "global"
                        else f"validation/user/{int(user['user_id'])}",
                    )
                    revision = await lock_config(db, self.unit(item, user))
                    snapshot = await self._snapshot(
                        db, user, {item.id: item}, lock=True
                    )
                    state = self._state(item, user, snapshot, Settings())
                    if version != state["version"]:
                        raise ConfigError("conflict", status=409, field_id=field_id)
                    committed = {}
                    committed_revisions = {}
                    if item.storage == "preference":
                        await self._write_preference(db, item, user, value, reset)
                    elif item.storage in {"section", "binding", "model"}:
                        row = snapshot[0].get(item.key)
                        old = _parse_override(row.key_value if row else None)
                        updated = replace_path(old, item.path, value, remove=reset)
                        defaults = (
                            SECTION_REGISTRY[item.key]["defaults"]
                            if item.storage == "section"
                            else {}
                        )
                        effective = deep_merge(defaults, updated)
                        _strict_tree(defaults, effective)
                        try:
                            if item.storage == "section":
                                validate_section_config(item.key, effective)
                            elif item.storage == "binding":
                                from backend.core.ai_protocol import account_store

                                accounts = (
                                    (
                                        await db.execute(
                                            select(AppConfig).where(
                                                AppConfig.key_name.like("ai_account.%")
                                            )
                                        )
                                    )
                                    .scalars()
                                    .all()
                                )
                                bindings, error = (
                                    account_store.validate_role_bindings_payload(
                                        effective,
                                        {
                                            row.key_name.removeprefix("ai_account.")
                                            for row in accounts
                                        },
                                    )
                                )
                                if error:
                                    raise ValueError("Invalid binding")
                                effective = {
                                    role: binding.to_dict()
                                    for role, binding in bindings.items()
                                }
                            elif (
                                item.path == ("reasoning_params", "thinking")
                                and value is not None
                            ):
                                if set(value) != {"type"} or value["type"] not in {
                                    "adaptive",
                                    "disabled",
                                }:
                                    raise ValueError("Invalid thinking mode")
                        except ValueError:
                            raise ConfigError(
                                "invalid_structure", field_id=field_id
                            ) from None
                        pruned = _prune_default_equal_leaves(defaults, effective)
                        raw = (
                            None
                            if pruned is _PRUNED
                            else json.dumps(pruned, ensure_ascii=False)
                        )
                        if item.storage == "binding":
                            await self.validate_app_combination(db, {item.key: raw})
                        await self._write_app(db, item.key, raw)
                        committed[item.key] = raw
                    else:
                        if item.key == "pr_dependency_graph_mode":
                            committed.update(
                                await self._clear_depgraph_mode(db, committed_revisions)
                            )
                        raw = None if reset else serialize_value(value)
                        await self.validate_app_combination(db, {item.key: raw})
                        await self._write_app(db, item.key, raw)
                        committed[item.key] = raw
                    revision.revision += 1
                    committed_revisions[self.unit(item, user)] = revision.revision
                    # Metadata-only audit is in the same transaction; values,
                    # credentials, prompts and exception copies never enter it.
                    db.add(
                        AdminActionLog(
                            admin_id=int(user["user_id"]),
                            action="config_reset" if reset else "config_save",
                            target_type="configuration",
                            target_id=field_id[:255],
                            detail=json.dumps(
                                {
                                    "field": field_id,
                                    "revision": revision.revision,
                                    "scope": item.scope,
                                }
                            ),
                        )
                    )
                    await db.commit()
                except ConfigError:
                    await db.rollback()
                    raise
                except IntegrityError:
                    await db.rollback()
                    raise ConfigError(
                        "conflict", status=409, field_id=field_id
                    ) from None
                except BaseException:
                    await db.rollback()
                    raise
                effect = "restart_required" if item.requires_restart else "live"
                try:
                    await apply_config_values(committed)
                    if item.scope == "user":
                        from backend.webui.deps import invalidate_user_prefs_cache

                        invalidate_user_prefs_cache(int(user["user_id"]))
                        invalidate_user_dynamic_config_cache(int(user["user_id"]))
                    config_runtime_sync.record_applied(committed_revisions)
                except Exception:
                    config_runtime_sync.pending.update(committed_revisions)
                    logger.exception(
                        "Configuration committed; runtime application pending"
                    )
                    effect = "pending"
        from urllib.parse import unquote

        model = (
            (field_id.split("/")[1], unquote(field_id.split("/")[2]))
            if item.storage == "model"
            else None
        )
        fields = await self.describe(db, user, category=item.category, model=model)
        result = next(field for field in fields if field["id"] == field_id)
        if not item.requires_restart or effect == "pending":
            result["effect"] = effect
        result["persisted"] = True
        return result

    async def validate_app_combination(self, db, updates):
        from backend.services.billing_configuration_service import (
            BillingConfigurationError,
            validate_billing_configuration,
        )
        from backend.services.billing_service import BillingError

        try:
            await validate_billing_configuration(db, updates)
        except (BillingConfigurationError, BillingError) as exc:
            raise ConfigError(
                "invalid_value", detail="Invalid billing configuration"
            ) from exc
        result = await db.execute(
            select(AppConfig)
            .where(AppConfig.key_name.in_(Settings.model_fields))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        values = Settings().model_dump()
        for row in result.scalars().all():
            if row.key_value is not None:
                values[row.key_name] = _cast_config_type(
                    row.key_value, _get_field_type(row.key_name)
                )
        deployment = Settings()
        for key, raw in updates.items():
            values[key] = (
                getattr(deployment, key)
                if raw is None
                else _cast_config_type(raw, _get_field_type(key))
            )
        try:
            Settings.model_validate(values)
        except ValueError:
            raise ConfigError("invalid_value") from None
        for lower, upper in (
            ("star_aid_min_interval_minutes", "star_aid_max_interval_minutes"),
            ("code_chunk_overlap", "code_chunk_size"),
            (
                "pr_issue_related_confidence_threshold",
                "pr_issue_closing_confidence_threshold",
            ),
        ):
            if {lower, upper} & updates.keys() and values[lower] > values[upper]:
                raise ConfigError("cross_field", detail=f"{lower} <= {upper}")


config_center_service = ConfigCenterService()
