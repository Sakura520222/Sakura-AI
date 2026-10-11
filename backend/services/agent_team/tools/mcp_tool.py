"""Adapter from a trusted MCP policy and untrusted schema to the tool executor."""

from __future__ import annotations

import copy

from backend.services.agent_team.mcp_runtime import schema_validator, tool_name
from backend.services.agent_team.tools.base import BaseTool, ToolMetadata, ToolResult


class MCPTool(BaseTool):
    def __init__(self, runtime, server_id, remote_name, policy, definition, digest):
        self.runtime = runtime
        self.server_id = server_id
        self.remote_name = remote_name
        self.policy = policy
        self.digest = digest
        self.name = tool_name(server_id, remote_name)
        self._schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": definition["description"],
                "parameters": copy.deepcopy(definition["input_schema"]),
            },
        }

    def is_read_only(self):
        return self.policy.read_only

    def runtime_metadata(self):
        return ToolMetadata(
            read_only=self.policy.read_only,
            parallel_safe=self.policy.read_only,
            delegation_safe=self.policy.read_only,
            workspace_access=False,
        )

    def get_schema(self):
        return copy.deepcopy(self._schema)

    def validate_input(self, args, ctx):
        try:
            if schema_validator(self._schema["function"]["parameters"]).is_valid(args):
                return None
        except Exception:
            pass
        return "Invalid MCP arguments"

    async def execute(self, args, ctx):
        if not ctx.allows_skill_tool(self.name, args) or (
            ctx.executor is not None and not ctx.executor.allows_tool(self.name, args)
        ):
            await self.runtime._record(
                "invocation", "denied", self.server_id, self.name, "executor_denied"
            )
            return ToolResult(
                False, error="MCP tool denied", error_code="MCP_POLICY_DENIED"
            )
        return await self.runtime.invoke(
            self.server_id,
            self.remote_name,
            args,
            expected_digest=self.digest,
            expected_policy=self.policy,
        )
