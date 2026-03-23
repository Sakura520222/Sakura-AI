"""工具调用处理器

从原 ai_reviewer.py 迁移的工具调用处理方法：
- _handle_tool_call (1349-1386行)
"""

import json
from typing import Any, Dict

from loguru import logger


class ToolHandler:
    """工具调用处理器

    负责路由和执行AI请求的工具调用。
    """

    def __init__(self, file_tool, search_tool):
        """初始化工具处理器

        Args:
            file_tool: 文件工具处理器
            search_tool: 搜索工具处理器
        """
        self.file_tool = file_tool
        self.search_tool = search_tool

    async def handle_tool_call(
        self, tool_call: Any, repo: Any, pr: Any
    ) -> Dict[str, Any]:
        """处理AI的工具调用请求

        Args:
            tool_call: OpenAI工具调用对象
            repo: GitHub仓库对象
            pr: GitHub PR对象

        Returns:
            工具执行结果
        """
        function_name = tool_call.function.name
        arguments = json.loads(tool_call.function.arguments)

        try:
            if function_name == "read_file":
                return await self.file_tool.read_file(
                    arguments["file_path"], repo, pr
                )
            elif function_name == "list_directory":
                return await self.file_tool.list_directory(
                    arguments["directory"], repo, pr
                )
            elif function_name == "search_project_docs":
                return await self.search_tool.search_project_docs(
                    arguments.get("query", ""),
                    arguments.get("top_k", 5),
                    repo,
                    pr,
                )
            elif function_name == "search_code_context":
                return await self.search_tool.search_code_context(
                    arguments.get("query", ""),
                    arguments.get("language"),
                    arguments.get("file_path"),
                    arguments.get("top_k", 5),
                    repo,
                    pr,
                )
            else:
                return {"error": f"未知工具: {function_name}"}

        except Exception as e:
            logger.error(f"执行工具 {function_name} 失败: {e}", exc_info=True)
            return {"error": f"工具执行失败: {str(e)}"}
