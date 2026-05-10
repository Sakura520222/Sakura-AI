"""工具注册表 - 统一管理全栈专家和审查角色的工具集

遵循 Claude Code 风格：
- 工具集中注册
- 按角色过滤
- 统一导出 schema 用于 function calling
"""

from __future__ import annotations

from typing import Any

from backend.services.agent_team.tools.base import BaseTool, ToolExecutor
from backend.services.agent_team.tools.edit_tool import EditTool
from backend.services.agent_team.tools.finish_task_tool import FinishTaskTool
from backend.services.agent_team.tools.glob_tool import GlobTool
from backend.services.agent_team.tools.grep_tool import GrepTool
from backend.services.agent_team.tools.insert_lines_tool import InsertLinesTool
from backend.services.agent_team.tools.list_directory_tool import ListDirectoryTool
from backend.services.agent_team.tools.read_tool import ReadTool
from backend.services.agent_team.tools.replace_lines_tool import ReplaceLinesTool
from backend.services.agent_team.tools.shell_tool import ShellTool
from backend.services.agent_team.tools.submit_review_tool import SubmitReviewTool
from backend.services.agent_team.tools.write_tool import WriteTool


# ── 工具实例 ──────────────────────────────────────────

# 全栈专家可用工具
FULLSTACK_TOOL_INSTANCES: list[BaseTool] = [
    ReadTool(),
    ListDirectoryTool(),
    GlobTool(),
    GrepTool(),
    WriteTool(),
    EditTool(),
    ReplaceLinesTool(),
    InsertLinesTool(),
    ShellTool(),
    FinishTaskTool(),
]

# 审查角色可用工具（只读 + 审查提交）
REVIEWER_TOOL_INSTANCES: list[BaseTool] = [
    ReadTool(),
    ListDirectoryTool(),
    GlobTool(),
    GrepTool(),
    ShellTool(),
    SubmitReviewTool(),
]

# 按名称索引的工具注册表
tool_registry: dict[str, BaseTool] = {tool.name: tool for tool in FULLSTACK_TOOL_INSTANCES}
tool_registry.update({tool.name: tool for tool in REVIEWER_TOOL_INSTANCES})


def get_fullstack_tools() -> list[BaseTool]:
    """获取全栈专家可用工具列表。"""
    return list(FULLSTACK_TOOL_INSTANCES)


def get_reviewer_tools() -> list[BaseTool]:
    """获取审查角色可用工具列表。"""
    return list(REVIEWER_TOOL_INSTANCES)


def _sanitize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """清理工具 schema，确保兼容各 AI 厂商。

    某些厂商（如智谱 GLM）对 function calling schema 有更严格的要求：
    - 不接受空 required 数组
    - 不接受 properties 中的 default 值
    """
    import copy

    schema = copy.deepcopy(schema)
    fn = schema.get("function", {})
    params = fn.get("parameters", {})

    # 移除空 required 数组
    if "required" in params and not params["required"]:
        del params["required"]

    # 移除 properties 中的 default 值
    for prop in params.get("properties", {}).values():
        prop.pop("default", None)

    return schema


def get_tool_definitions(role: str = "fullstack") -> list[dict[str, Any]]:
    """获取指定角色的工具 schema 列表（用于 function calling）。

    Args:
        role: "fullstack" 或 "reviewer"
    """
    tools = FULLSTACK_TOOL_INSTANCES if role == "fullstack" else REVIEWER_TOOL_INSTANCES
    return [_sanitize_schema(t.get_schema()) for t in tools]


def create_executor(role: str = "fullstack") -> ToolExecutor:
    """创建指定角色的工具执行器。"""
    tools = FULLSTACK_TOOL_INSTANCES if role == "fullstack" else REVIEWER_TOOL_INSTANCES
    return ToolExecutor(list(tools))
