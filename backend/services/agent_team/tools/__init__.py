"""Agent 工具包 - 基于 Claude Code 风格的工具框架

每个工具是独立类，拥有完整的生命周期：
schema 解析 → 输入校验 → 权限检查 → 执行 → 结果映射
"""

from backend.services.agent_team.tools.base import (
    BaseTool,
    ToolContext,
    ToolExecutor,
    ToolResult,
)
from backend.services.agent_team.tools.registry import (
    get_fullstack_tools,
    get_reviewer_tools,
    get_tool_definitions,
    tool_registry,
)

__all__ = [
    "BaseTool",
    "ToolContext",
    "ToolExecutor",
    "ToolResult",
    "get_fullstack_tools",
    "get_reviewer_tools",
    "get_tool_definitions",
    "tool_registry",
]
