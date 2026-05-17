"""Glob 工具 - 按文件名模式查找文件

使用 pathlib.glob 进行文件名模式匹配。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from loguru import logger

from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult
from backend.services.agent_team.workspace_service import WorkspaceSecurityError
from backend.utils.search_excludes import SEARCH_EXCLUDES


class GlobTool(BaseTool):
    """按文件名模式查找文件。"""

    name = "glob"

    _schema = {
        "type": "function",
        "function": {
            "name": "glob",
            "description": (
                "按文件名模式快速查找文件。使用标准 glob 语法。"
                "\n\n使用场景："
                "\n- 查找某个后缀的所有文件: **/*.py"
                "\n- 查找特定目录下的文件: src/**/*.ts"
                "\n- 按前缀查找: test_*.py"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "glob 匹配模式，如 **/*.py、src/**/*.ts",
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索的起始目录（相对于项目根目录），默认为项目根",
                        "default": ".",
                    },
                },
                "required": ["pattern"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        if not args.get("pattern"):
            return "缺少 pattern 参数"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = args["pattern"]
        base_dir = args.get("path", ".")

        start_time = time.time()

        resolved_base = self._resolve(base_dir, ctx)
        if resolved_base is None:
            return ToolResult(success=False, error=f"路径安全校验失败: {base_dir}")

        if not resolved_base.exists() or not resolved_base.is_dir():
            return ToolResult(
                success=True,
                output={
                    "pattern": pattern,
                    "filenames": [],
                    "num_files": 0,
                    "duration_ms": int((time.time() - start_time) * 1000),
                },
            )

        # 执行 glob
        max_results = 200
        try:
            matches = sorted(resolved_base.glob(pattern))
        except Exception as exc:
            return ToolResult(success=False, error=f"glob 模式无效: {exc}")

        filenames = []
        for match in matches:
            if len(filenames) >= max_results:
                break
            if any(part in SEARCH_EXCLUDES for part in match.parts):
                continue
            rel = match.relative_to(
                ctx.workspace_service.resolve_inside_workspace(ctx.workspace)
            )
            filenames.append(str(rel))

        duration_ms = int((time.time() - start_time) * 1000)
        truncated = len(matches) > max_results

        logger.debug(
            "GlobTool: {} → {} 文件 ({}ms)", pattern, len(filenames), duration_ms
        )

        return ToolResult(
            success=True,
            output={
                "pattern": pattern,
                "filenames": filenames,
                "num_files": len(filenames),
                "truncated": truncated,
                "duration_ms": duration_ms,
            },
        )

    @staticmethod
    def _resolve(path: str, ctx: ToolContext) -> Path | None:
        try:
            return ctx.workspace_service.resolve_inside_workspace(ctx.workspace, path)
        except (WorkspaceSecurityError, Exception):
            return None
