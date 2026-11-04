"""Session-owned integration of capabilities, plugins and lifecycle auditing."""

from __future__ import annotations

from backend.services.agent_team.capability_policy import (
    READ_ONLY_CAPABILITIES,
    CapabilitySession,
    load_runtime_policy,
)
from backend.services.agent_team.lifecycle_hooks import LifecycleHooks
from backend.services.agent_team.mcp_runtime import MCPRuntime


class HarnessRuntime:
    def __init__(
        self,
        *,
        load_plugins,
        resolve_credential,
        audit,
        task_id=None,
        session_id=None,
        read_only=False,
        load_policy=load_runtime_policy,
    ):
        async def record(event):
            await audit({**event, "task_id": task_id, "session_id": session_id})

        self.audit = record
        self.capabilities = CapabilitySession(
            load_policy,
            task_id=task_id,
            session_id=session_id,
            ceiling=READ_ONLY_CAPABILITIES if read_only else None,
            audit=record,
        )

        async def hooks():
            return (await load_plugins()).hooks

        async def mcp():
            return (await load_plugins()).mcp

        async def authorize(required, read_only):
            return (
                await self.capabilities.check("mcp", required, read_only=read_only)
            ).allowed

        self.hooks = LifecycleHooks(
            hooks, self.capabilities, record, read_only=read_only
        )
        self.mcp = MCPRuntime(
            load_config=mcp,
            authorize=authorize,
            audit=record,
            resolve_credential=resolve_credential,
            read_only=read_only,
        )

    async def close(self):
        try:
            await self.mcp.close()
        finally:
            await self.capabilities.close()


def create_harness_runtime(*, audit, task_id=None, session_id=None, read_only=False):
    from backend.services.agent_team.plugin_config import (
        load_harness_plugins,
        resolve_mcp_credential,
    )

    async def plugins():
        return await load_harness_plugins(task_id=task_id)

    return HarnessRuntime(
        load_plugins=plugins,
        resolve_credential=resolve_mcp_credential,
        audit=audit,
        task_id=task_id,
        session_id=session_id,
        read_only=read_only,
    )
