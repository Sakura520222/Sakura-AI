"""PR diff 工具辅助函数。

提供 get_file_diff / list_changed_files 工具的注册和 handler 构建。
"""

from typing import Any, Dict, List

from backend.services.ai_reviewer.constants import (
    DIFF_TOOLS,
    TOOL_NAME_TO_DEFINITION,
)
from backend.services.ai_reviewer.tools import DiffToolHandler, ToolHandler


def extend_with_diff_tools(
    enabled_tools: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """追加 PR diff 工具定义（get_file_diff / list_changed_files）"""
    diff_enabled_tools = list(enabled_tools)
    enabled_names = {
        tool.get("function", {}).get("name")
        for tool in diff_enabled_tools
        if isinstance(tool, dict)
    }

    for tool_name in DIFF_TOOLS:
        if tool_name in enabled_names:
            continue
        tool_def = TOOL_NAME_TO_DEFINITION.get(tool_name)
        if tool_def:
            diff_enabled_tools.append(tool_def)
            enabled_names.add(tool_name)

    return diff_enabled_tools


def build_tool_handler_with_diff(
    tool_handler: ToolHandler,
    diff_tool: DiffToolHandler,
) -> ToolHandler:
    """基于现有工具处理器创建启用 PR diff 工具的临时处理器"""
    return tool_handler.with_diff_tool(diff_tool)
