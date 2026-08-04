"""工具调用处理器

从原 ai_reviewer.py 迁移的工具调用处理方法：
- _handle_tool_call (1349-1386行)
"""

import json
from typing import Any

from loguru import logger


class ToolHandler:
    """工具调用处理器

    负责路由和执行AI请求的工具调用。
    """

    def __init__(
        self,
        file_tool,
        search_tool,
        web_search_tool=None,
        git_tool=None,
        search_files_tool=None,
        sakura_tool=None,
        fetch_url_tool=None,
        diff_tool=None,
    ):
        """初始化工具处理器

        Args:
            file_tool: 文件工具处理器
            search_tool: 搜索工具处理器
            web_search_tool: Web 搜索工具处理器（可选）
            git_tool: Git 信息工具处理器（可选）
            search_files_tool: 跨文件搜索工具处理器（可选）
            sakura_tool: .sakura/ 文档工具处理器（可选）
            fetch_url_tool: URL 抓取工具处理器（可选）
            diff_tool: PR diff 工具处理器（可选）
        """
        self.file_tool = file_tool
        self.search_tool = search_tool
        self.web_search_tool = web_search_tool
        self.git_tool = git_tool
        self.search_files_tool = search_files_tool
        self.sakura_tool = sakura_tool
        self.fetch_url_tool = fetch_url_tool
        self.diff_tool = diff_tool

    def with_diff_tool(self, diff_tool) -> ToolHandler:
        """创建启用指定 PR diff 工具的新处理器"""
        return self.__class__(
            self.file_tool,
            self.search_tool,
            self.web_search_tool,
            self.git_tool,
            self.search_files_tool,
            self.sakura_tool,
            self.fetch_url_tool,
            diff_tool=diff_tool,
        )

    def apply_web_tool_settings(self, settings) -> None:
        """根据运行时配置同步 web_search / fetch_url 工具实例（幂等）。

        web_search 仅在 ``settings.web_search_enabled`` 为真时持有实例；
        fetch_url 仅在 web_search 与 fetch_url 均启用时持有实例，禁用则置 None，
        避免长生命周期 Worker 持有过期工具。已在 AIReviewer / IssueAnalyzer 的
        初始化与运行时刷新中复用，新增 web 工具时只需修改此处。
        """
        if settings.web_search_enabled:
            if self.web_search_tool is None:
                from backend.services.ai_reviewer.tools.web_search_tool import (
                    WebSearchToolHandler,
                )

                self.web_search_tool = WebSearchToolHandler()
        else:
            self.web_search_tool = None

        if settings.web_search_enabled and settings.fetch_url_enabled:
            if self.fetch_url_tool is None:
                from backend.services.ai_reviewer.tools.fetch_url_tool import (
                    FetchUrlToolHandler,
                )

                self.fetch_url_tool = FetchUrlToolHandler()
        else:
            self.fetch_url_tool = None

    async def handle_tool_call(
        self, tool_call: Any, repo: Any, pr: Any
    ) -> dict[str, Any]:
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
                file_path = arguments.get("file_path")
                if not file_path:
                    return {"error": "缺少必填参数: file_path"}
                return await self.file_tool.read_file(
                    file_path=file_path,
                    repo=repo,
                    pr=pr,
                    start_line=arguments.get("start_line"),
                    end_line=arguments.get("end_line"),
                    search_pattern=arguments.get("search_pattern"),
                    context_lines=arguments.get("context_lines"),
                    branch=arguments.get("branch"),
                )
            elif function_name == "list_directory":
                directory = arguments.get("directory")
                if not directory:
                    return {"error": "缺少必填参数: directory"}
                return await self.file_tool.list_directory(
                    directory, repo, pr, branch=arguments.get("branch")
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
            elif function_name == "search_web":
                if not self.web_search_tool:
                    return {"error": "Web 搜索工具未启用"}
                return await self.web_search_tool.search_web(
                    query=arguments.get("query", ""),
                    top_k=arguments.get("top_k"),
                )
            elif function_name == "fetch_url":
                if not self.fetch_url_tool:
                    return {"error": "URL 抓取工具未启用"}
                url = arguments.get("url")
                if not url:
                    return {"error": "缺少必填参数: url"}
                return await self.fetch_url_tool.fetch_url(url=url)
            elif function_name == "search_in_files":
                if not self.search_files_tool:
                    return {"error": "跨文件搜索工具未启用"}
                keyword = arguments.get("keyword")
                if not keyword:
                    return {"error": "缺少必填参数: keyword"}
                return await self.search_files_tool.search_in_files(
                    keyword=keyword,
                    repo=repo,
                    pr=pr,
                    file_extension=arguments.get("file_extension"),
                    directory=arguments.get("directory"),
                    context_lines=arguments.get("context_lines"),
                    max_results=arguments.get("max_results"),
                    branch=arguments.get("branch"),
                )
            elif function_name == "get_git_info":
                if not self.git_tool:
                    return {"error": "Git 信息工具未启用"}
                return await self.git_tool.get_git_info(
                    repo=repo,
                    pr=pr,
                    branch_count=arguments.get("branch_count"),
                )
            elif function_name == "list_commits":
                if not self.git_tool:
                    return {"error": "Git 提交历史工具未启用"}
                return await self.git_tool.list_commits(
                    repo=repo,
                    pr=pr,
                    branch=arguments.get("branch"),
                    per_page=arguments.get("per_page"),
                )
            elif function_name == "read_sakura_docs":
                if not self.sakura_tool:
                    return {"error": ".sakura/ 文档工具未启用"}
                return await self.sakura_tool.read_sakura_docs(
                    doc_path=arguments.get("doc_path"),
                    repo=repo,
                    pr=pr,
                )
            elif function_name == "list_sakura_directory":
                if not self.sakura_tool:
                    return {"error": ".sakura/ 目录工具未启用"}
                return await self.sakura_tool.list_sakura_directory(
                    subdirectory=arguments.get("subdirectory"),
                    repo=repo,
                    pr=pr,
                )
            elif function_name == "read_sakura_memory":
                if not self.sakura_tool:
                    return {"error": ".sakura/ 记忆工具未启用"}
                return await self.sakura_tool.read_sakura_memory(
                    file_name=arguments.get("file_name"),
                    count=arguments.get("count", 5),
                    repo=repo,
                    pr=pr,
                )
            elif function_name == "get_file_diff":
                if not self.diff_tool:
                    return {"error": "PR diff 工具未启用"}
                file_path = arguments.get("file_path")
                if not file_path:
                    return {"error": "缺少必填参数: file_path"}
                return await self.diff_tool.get_file_diff(
                    file_path=file_path,
                )
            elif function_name == "list_changed_files":
                if not self.diff_tool:
                    return {"error": "PR diff 工具未启用"}
                return await self.diff_tool.list_changed_files()
            else:
                return {"error": f"未知工具: {function_name}"}

        except Exception as e:
            logger.error("执行工具 {} 失败: {}", function_name, str(e), exc_info=True)
            return {"error": f"工具执行失败: {e!s}"}
