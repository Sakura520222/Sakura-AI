"""ListDirectory 工具 - 列出目录内容"""

from __future__ import annotations

import os
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

    # 整树剪枝的目录（递归前剪掉，避免 .venv 等子孙条目涌入结果）
    # / Dirs pruned entirely before descending (keeps .venv descendants out)
    PRUNED_DIR_NAMES = {
        ".git",
        ".venv",
        "venv",
        ".sakura",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }

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

        def _entry(child: Path) -> dict[str, Any]:
            rel = child.relative_to(workspace_root).as_posix()
            return {
                "name": child.name,
                "path": rel,
                "is_dir": child.is_dir(),
                "size": child.stat().st_size if child.is_file() else 0,
            }

        try:
            if recursive:
                for current, dir_names, file_names in os.walk(resolved):
                    dir_names[:] = [
                        name
                        for name in dir_names
                        if name not in self.PRUNED_DIR_NAMES
                    ]
                    for name in list(dir_names) + file_names:
                        entries.append(_entry(Path(current) / name))
            else:
                for child in resolved.iterdir():
                    if child.is_dir() and child.name in self.PRUNED_DIR_NAMES:
                        continue
                    entries.append(_entry(child))
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
