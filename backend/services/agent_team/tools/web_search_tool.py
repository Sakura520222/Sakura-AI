"""Web 搜索工具 - 为 Agent 专家团队提供互联网搜索能力

包装 AI 审查员已有的 WebSearchToolHandler，复用配置读取和搜索引擎逻辑。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult

# 模块级延迟单例
_web_search_handler: Any | None = None
_handler_unavailable: bool = False


def _get_web_search_handler() -> Any | None:
    """获取或创建 WebSearchToolHandler 单例（仅当启用时）。"""
    global _web_search_handler, _handler_unavailable
    if _handler_unavailable:
        return None
    if _web_search_handler is not None:
        return _web_search_handler

    try:
        from backend.core.config import get_settings

        settings = get_settings()
        if not settings.web_search_enabled:
            _handler_unavailable = True
            return None

        from backend.services.ai_reviewer.tools.web_search_tool import (
            WebSearchToolHandler,
        )

        _web_search_handler = WebSearchToolHandler()
        return _web_search_handler
    except Exception as exc:
        logger.warning("Web 搜索工具初始化失败: {}", exc)
        _handler_unavailable = True
        return None


class WebSearchTool(BaseTool):
    """搜索互联网获取最新文档、API 参考等信息。"""

    name = "search_web"

    _schema = {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "搜索互联网获取最新文档、API 参考、最佳实践等信息。"
                "\n\n使用场景："
                "\n- 查询最新的 API 文档、版本变更或技术规范"
                "\n- 了解特定技术/框架的最佳实践和推荐用法"
                "\n- 获取与代码相关的最新社区讨论和解决方案"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索查询关键词",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回搜索结果数量，默认 3",
                    },
                },
                "required": ["query"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        query = str(args.get("query") or "").strip()
        if not query:
            return "缺少 query 参数"
        if len(query) > 500:
            return "query 不能超过 500 个字符"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        handler = _get_web_search_handler()
        if handler is None:
            return ToolResult(success=False, error="Web 搜索工具未启用")

        query = args["query"]
        top_k = args.get("top_k")

        try:
            result = await handler.search_web(query=query, top_k=top_k)
        except Exception as exc:
            logger.error("Web 搜索执行失败: {}", exc)
            return ToolResult(success=False, error=f"搜索失败: {exc}")

        if result.get("error"):
            return ToolResult(success=False, error=result["error"])

        return ToolResult(success=True, output=result)
