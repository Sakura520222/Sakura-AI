"""PR diff 工具辅助函数。

提供 DiffToolHandler 与 ToolHandler 的组合构建。
diff 工具定义（get_file_diff / list_changed_files）由 ToolManager 始终注册。
"""

from backend.services.ai_reviewer.tools import DiffToolHandler, ToolHandler


def build_tool_handler_with_diff(
    tool_handler: ToolHandler,
    diff_tool: DiffToolHandler,
) -> ToolHandler:
    """基于现有工具处理器创建启用 PR diff 工具的临时处理器"""
    return tool_handler.with_diff_tool(diff_tool)
