"""ListDirectory 工具 - 列出目录内容"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult
from backend.services.agent_team.workspace_service import (
    WorkspaceSecurityError,
)


class ListDirectoryTool(BaseTool):
    """列出指定目录下的文件和子目录。"""

    name = "list_directory"

    # 不允许列出这些目录的内容
    BLOCKED_DIRS = {".git", ".ssh", "__pycache__", "node_modules"}

    _schema = {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "列出指定目录下的文件和子目录，支持递归。"
                "\n\n用于了解项目结构、查找文件位置。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "要列出的目录路径（相对于项目根目录），默认为 '.'",
                        "default": ".",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "是否递归列出子目录，默认 false",
                        "default": False,
                    },
                },
                "required": [],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        directory = args.get("directory", ".")
        recursive = args.get("recursive", False)

        try:
            resolved = ctx.workspace_service.resolve_inside_workspace(
                ctx.workspace, directory
            )
        except WorkspaceSecurityError as exc:
            return ToolResult(success=False, error=str(exc))

        if not resolved.exists() or not resolved.is_dir():
            return ToolResult(success=False, error=f"目录不存在: {directory}")

        workspace_root = Path(ctx.workspace).resolve()
        entries: list[dict[str, Any]] = []

        try:
            children = resolved.rglob("*") if recursive else resolved.iterdir()
            for child in children:
                rel = child.relative_to(workspace_root).as_posix()
                if any(blocked in rel.split("/") for blocked in self.BLOCKED_DIRS):
                    continue
                if child.name.startswith("."):
                    continue
                entries.append(
                    {
                        "name": child.name,
                        "path": rel,
                        "is_dir": child.is_dir(),
                        "size": child.stat().st_size if child.is_file() else 0,
                    }
                )
        except PermissionError:
            return ToolResult(success=False, error=f"没有权限访问目录: {directory}")

        # 排序：目录在前
        entries.sort(key=lambda e: (not e["is_dir"], e["path"]))

        # 限制结果
        max_entries = 200
        truncated = len(entries) > max_entries
        entries = entries[:max_entries]

        logger.debug("ListDirectoryTool: {} ({} 项)", directory, len(entries))

        return ToolResult(
            success=True,
            output={
                "directory": directory,
                "entries": entries,
                "total": len(entries),
                "truncated": truncated,
            },
        )
