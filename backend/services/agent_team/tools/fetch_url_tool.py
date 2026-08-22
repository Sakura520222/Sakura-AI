"""URL 抓取工具 - 为 Agent 专家团队提供网页内容抓取能力

包装 AI 审查员已有的 FetchUrlToolHandler，复用 SSRF 防护、域名过滤等安全机制。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult

# 模块级延迟单例
_fetch_url_handler: Any | None = None
_handler_unavailable: bool = False


def _get_fetch_url_handler() -> Any | None:
    """获取或创建 FetchUrlToolHandler 单例（仅当启用时）。"""
    global _fetch_url_handler, _handler_unavailable
    if _handler_unavailable:
        return None
    if _fetch_url_handler is not None:
        return _fetch_url_handler

    try:
        from backend.core.config import get_settings

        settings = get_settings()
        if not settings.web_search_enabled or not settings.fetch_url_enabled:
            _handler_unavailable = True
            return None

        from backend.services.ai_reviewer.tools.fetch_url_tool import (
            FetchUrlToolHandler,
        )

        _fetch_url_handler = FetchUrlToolHandler()
        return _fetch_url_handler
    except Exception as exc:
        logger.warning("URL 抓取工具初始化失败: {}", exc)
        _handler_unavailable = True
        return None


class FetchUrlTool(BaseTool):
    """抓取指定 URL 的网页内容并转换为纯文本。"""

    name = "fetch_url"

    _schema = {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "抓取指定 URL 的网页内容并转换为纯文本。"
                "\n\n使用场景："
                "\n- 深入阅读搜索结果中的链接内容"
                "\n- 获取官方文档、API 参考的完整页面"
                "\n- 查看特定技术文章或博客的详细内容"
                "\n\n注意：仅支持 HTTP/HTTPS 协议，大页面内容会被截断。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要抓取的网页 URL（必须以 http:// 或 https:// 开头）",
                    },
                },
                "required": ["url"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        url = str(args.get("url") or "").strip()
        if not url:
            return "缺少 url 参数"
        if not url.startswith(("http://", "https://")):
            return "URL 必须以 http:// 或 https:// 开头"
        if len(url) > 2048:
            return "URL 长度不能超过 2048 个字符"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        handler = _get_fetch_url_handler()
        if handler is None:
            return ToolResult(success=False, error="URL 抓取工具未启用")

        url = args["url"]

        try:
            result = await handler.fetch_url(url=url)
        except Exception as exc:
            logger.error("URL 抓取执行失败: {}", exc)
            return ToolResult(success=False, error=f"抓取失败: {exc}")

        if result.get("error"):
            return ToolResult(success=False, error=result["error"])

        output = {
            "url": result.get("url", url),
            "content": result.get("content", ""),
            "content_length": result.get("content_length", 0),
            "truncated": result.get("truncated", False),
        }

        return ToolResult(success=True, output=output)
