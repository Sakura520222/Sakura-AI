""".sakura/memory/ 反思文件读取工具

提供 AI 审查 agent 读取 .sakura/memory/ 目录下反思文件的能力：
- 列出最近的反思文件（按日期排序）
- 读取指定反思文件内容
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult


class SakuraMemoryTool(BaseTool):
    """读取 .sakura/memory/ 目录下的审查反思文件。"""

    name = "read_sakura_memory"

    _schema = {
        "type": "function",
        "function": {
            "name": "read_sakura_memory",
            "description": (
                "读取 .sakura/memory/ 目录下的审查反思文件，了解历史审查经验和项目模式。"
                "\n\n使用场景："
                "\n- 了解项目历史审查中发现的常见问题"
                "\n- 查看之前审查模式，避免重复建议"
                "\n- 参考历史审查经验提升当前审查质量"
                "\n\n不指定 file_name 时返回最近反思文件列表。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": (
                            ".sakura/memory/ 下的文件名，如 '2024-01-15_PR42_abc1234.md'。"
                            "留空返回最近反思文件列表。"
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "description": "列出最近 N 个反思文件，默认 5",
                    },
                },
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        file_name = args.get("file_name", "")
        if file_name and ("../" in file_name or "..\\" in file_name):
            return "文件名不能包含路径遍历字符"
        count = args.get("count", 5)
        if count is not None:
            try:
                c = int(count)
                if c < 1 or c > 50:
                    return f"count 须在 1-50 之间，当前: {c}"
            except (ValueError, TypeError):
                return f"count 须为整数，当前: {count}"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        repo = ctx.extra.get("github_repo")
        sakura_ref = ctx.extra.get("sakura_ref")
        if not repo:
            return ToolResult(success=False, error="GitHub repo 不可用")

        file_name = args.get("file_name", "")
        count = int(args.get("count", 5))

        if file_name:
            return await self._read_file(repo, sakura_ref, file_name)
        return await self._list_files(repo, sakura_ref, count)

    async def _list_files(
        self, repo: Any, sakura_ref: str | None, count: int
    ) -> ToolResult:
        """列出 .sakura/memory/ 目录中最近的反思文件"""
        try:
            ref = sakura_ref or "main"

            def _list():
                contents = repo.get_contents(".sakura/memory", ref=ref)
                if isinstance(contents, list):
                    return contents
                return [contents]

            contents = await asyncio.to_thread(_list)

            files = [
                {"name": c.name, "path": c.path, "size": c.size}
                for c in contents
                if c.type == "file" and c.name.endswith(".md")
            ]
            # 按文件名降序（日期格式确保最新在前）
            files.sort(key=lambda f: f["name"], reverse=True)
            recent = files[:count]

            return ToolResult(
                success=True,
                output={
                    "total_files": len(files),
                    "showing": len(recent),
                    "files": recent,
                },
            )

        except Exception as exc:
            logger.error("列出 .sakura/memory/ 失败: {}", exc)
            return ToolResult(
                success=False, error=f"列出反思文件失败: {type(exc).__name__}: {exc}"
            )

    async def _read_file(
        self, repo: Any, sakura_ref: str | None, file_name: str
    ) -> ToolResult:
        """读取 .sakura/memory/ 中指定文件的内容"""
        # 安全校验
        safe = file_name.replace("\\", "/").strip("/")
        if "../" in safe or "/" in safe:
            return ToolResult(success=False, error="文件名不能包含路径分隔符或遍历字符")
        if not safe.endswith(".md"):
            return ToolResult(success=False, error="仅支持读取 .md 文件")

        path = f".sakura/memory/{safe}"
        try:
            ref = sakura_ref or "main"

            def _read():
                content = repo.get_contents(path, ref=ref)
                if isinstance(content, list):
                    return None
                return content.decoded_content.decode("utf-8")

            content = await asyncio.to_thread(_read)
            if content is None:
                return ToolResult(success=False, error=f"文件不存在或路径为目录: {path}")

            return ToolResult(
                success=True,
                output={
                    "file_path": path,
                    "content": content,
                    "size": len(content),
                },
            )

        except Exception as exc:
            logger.error("读取 {} 失败: {}", path, exc)
            return ToolResult(
                success=False, error=f"读取文件失败: {type(exc).__name__}: {exc}"
            )
