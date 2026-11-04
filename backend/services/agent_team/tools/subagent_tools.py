"""Parent-only orchestration tools with no workspace lock held while waiting."""

from backend.services.agent_team.tools.base import BaseTool, ToolMetadata, ToolResult


class SubagentTool(BaseTool):
    def runtime_metadata(self):
        # Safe to replay: spawn has a durable identity; wait/cancel are idempotent.
        # Keep orchestration ordered with writes/finish, without a workspace lock.
        return ToolMetadata(read_only=True, workspace_access=False)

    def is_read_only(self):
        return True

    def validate_input(self, args, ctx):
        if ctx.subagents is None:
            return "Subagents require a durable parent session"
        if self.name == "spawn_agent":
            if (
                set(args) != {"task"}
                or not isinstance(args["task"], str)
                or not args["task"].strip()
            ):
                return "task must be a non-empty string; runtime permissions cannot be selected"
            if not ctx.tool_call_id:
                return "Spawn requires its durable tool call ID"
        elif (
            set(args) != {"agent_id"}
            or type(args.get("agent_id")) is not int
            or args["agent_id"] < 1
        ):
            return "agent_id must be a positive integer belonging to this parent"
        return None

    async def execute(self, args, ctx):
        error = self.validate_input(args, ctx)
        if error:
            return ToolResult(
                False, error=error, error_code="SUBAGENT_CONTEXT_REQUIRED"
            )
        try:
            if self.name == "spawn_agent":
                result = await ctx.subagents.spawn(ctx.tool_call_id, args["task"])
            elif self.name == "wait_agent":
                result = await ctx.subagents.wait(args["agent_id"])
            else:
                result = await ctx.subagents.cancel(args["agent_id"])
        except ValueError as exc:
            return ToolResult(
                False, error=str(exc), error_code="SUBAGENT_SCOPE_REJECTED"
            )
        return ToolResult(True, output=result)


class SpawnAgentTool(SubagentTool):
    name = "spawn_agent"
    _schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": "Delegate an independent read-only investigation. Queues when active slots are occupied. Returns a durable agent_id; only the parent can modify files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Self-contained investigation and evidence to return",
                    }
                },
                "required": ["task"],
                "additionalProperties": False,
            },
        },
    }


class WaitAgentTool(SubagentTool):
    name = "wait_agent"
    _schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": "Wait for this parent's child and return its structured success, failure or cancellation result.",
            "parameters": {
                "type": "object",
                "properties": {"agent_id": {"type": "integer"}},
                "required": ["agent_id"],
                "additionalProperties": False,
            },
        },
    }


class CancelAgentTool(SubagentTool):
    name = "cancel_agent"
    _schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": "Cancel this parent's queued or running child, wait for cleanup and return its structured result.",
            "parameters": {
                "type": "object",
                "properties": {"agent_id": {"type": "integer"}},
                "required": ["agent_id"],
                "additionalProperties": False,
            },
        },
    }
