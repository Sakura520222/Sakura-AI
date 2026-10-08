"""Versioned, deployment-owned plugin configuration; repository text has no authority."""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from backend.core.config import get_dynamic_config_fresh, get_settings
from backend.services.agent_team.lifecycle_hooks import HookConfig
from backend.services.agent_team.mcp_runtime import MCPConfig

PLUGIN_KEY = "agent_team_harness_plugins"
MASK = "[UNCHANGED SECRET]"
_REFERENCE = re.compile(r"SAKURA_MCP_[A-Z0-9_]+")


class PluginConfigError(ValueError):
    """Credential-free validation failure."""


class HarnessPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1] = 1
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    hooks: tuple[HookConfig, ...] = ()
    mcp_repository_scopes: dict[str, list[str] | None] = Field(default_factory=dict)

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("invalid_version")
        return value

    @field_validator("hooks", mode="before")
    @classmethod
    def hook_tuple(cls, value):
        return tuple(value)

    @model_validator(mode="after")
    def trusted_values(self):
        if len({h.id for h in self.hooks}) != len(self.hooks):
            raise ValueError("duplicate_hook")
        servers = {s.id for s in self.mcp.servers}
        for server in self.mcp.servers:
            if any(
                not _REFERENCE.fullmatch(ref)
                for ref in server.credential_headers.values()
            ):
                raise ValueError("invalid_credential_reference")
        for ident, repos in self.mcp_repository_scopes.items():
            if (
                ident not in servers
                or repos is not None
                and any(
                    not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
                    for repo in repos
                )
            ):
                raise ValueError("invalid_repository_scope")
        return self


def parse_harness_plugins(raw, *, previous: HarnessPluginConfig | None = None):
    try:
        value = json.loads(raw) if isinstance(raw, str) else copy.deepcopy(raw)
        old = {s.id: s for s in previous.mcp.servers} if previous else {}
        for server in value.get("mcp", {}).get("servers", []):
            server.setdefault(
                "timeout_seconds", get_settings().agent_team_mcp_io_timeout_seconds
            )
            for header, secret in server.get("headers", {}).items():
                if secret == MASK:
                    # A masked credential is only reusable for the same endpoint.
                    existing = old.get(server["id"])
                    if (
                        existing is None
                        or existing.url != server["url"]
                        or header not in existing.headers
                    ):
                        raise ValueError("missing_masked_header")
                    server["headers"][header] = existing.headers[header]
        return HarnessPluginConfig.model_validate(value)
    except Exception:
        raise PluginConfigError("invalid_plugin_configuration") from None


def public_plugin_config(config: HarnessPluginConfig):
    value = config.model_dump(mode="json")
    for server in value["mcp"]["servers"]:
        server["headers"] = {key: MASK for key in server["headers"]}
    return value


async def _task_repository(task_id):
    from backend.models import database
    from backend.models.agent_team_models import AgentTeamTask

    if database.async_session is None:
        raise PluginConfigError("task_repository_unavailable")
    async with database.async_session() as db:
        task = await db.get(AgentTeamTask, task_id)
        return task.repo_full_name if task else None


async def load_harness_plugins(*, task_id: int | None = None) -> HarnessPluginConfig:
    config = parse_harness_plugins(await get_dynamic_config_fresh(PLUGIN_KEY))
    repo = None
    if task_id is not None:
        repo = await _task_repository(task_id)
        if repo is None:
            raise PluginConfigError("task_repository_unavailable")
    allowed = tuple(
        s
        for s in config.mcp.servers
        if (
            config.mcp_repository_scopes.get(s.id) is None
            or repo is not None
            and repo.casefold()
            in {r.casefold() for r in config.mcp_repository_scopes[s.id]}
        )
    )
    return config.model_copy(
        update={"mcp": config.mcp.model_copy(update={"servers": allowed})}
    )


async def resolve_mcp_credential(reference: str) -> str:
    if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
        raise PluginConfigError("credential_unavailable")
    value = os.environ.get(reference)
    if not value or "\r" in value or "\n" in value:
        raise PluginConfigError("credential_unavailable")
    return value


async def read_plugin_config(db):
    from backend.models.database import AppConfig

    result = await db.execute(select(AppConfig).where(AppConfig.key_name == PLUGIN_KEY))
    row = result.scalar_one_or_none()
    raw = row.key_value if row else get_settings().agent_team_harness_plugins
    return parse_harness_plugins(raw)


async def save_plugin_config(db, raw, profile: str):
    from backend.core.config import invalidate_dynamic_config_cache
    from backend.models.database import AppConfig
    from backend.services.agent_team.capability_policy import PermissionProfile

    try:
        PermissionProfile(profile)
    except ValueError:
        raise PluginConfigError("invalid_permission_profile") from None
    previous = await read_plugin_config(db)
    config = parse_harness_plugins(raw, previous=previous)
    values = {
        PLUGIN_KEY: config.model_dump_json(),
        "agent_team_permission_profile": profile,
    }
    for key, value in values.items():
        result = await db.execute(select(AppConfig).where(AppConfig.key_name == key))
        row = result.scalar_one_or_none()
        if row is None:
            db.add(AppConfig(key_name=key, key_value=value, description=key))
        else:
            row.key_value = value
    await db.commit()
    invalidate_dynamic_config_cache(set(values))
    return config
