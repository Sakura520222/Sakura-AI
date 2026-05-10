"""InsertLines 工具 - 在指定行号之后插入新内容

after_line=0 插入到文件开头。
适合在函数后添加新函数、追加 import 等。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult
from backend.services.agent_team.tools.file_state import ReadFileState
from backend.services.agent_team.tools.file_utils import (
    make_unified_diff,
    read_text_with_metadata,
    write_text_preserving,
)
from backend.services.agent_team.workspace_service import WorkspaceSecurityError


class InsertLinesTool(BaseTool):
    """在指定行号之后插入新内容。"""

    name = "insert_lines"

    _schema = {
        "type": "function",
        "function": {
            "name": "insert_lines",
            "description": (
                "在文件的指定行号之后插入新内容。"
                "\n\n典型用法：先用 read_file 查看文件确定插入位置，再用本工具插入。"
                "\n\n适合以下场景："
                "\n- 在某个函数后添加新函数（after_line 设为该函数最后一行的行号）"
                "\n- 在 import 块后添加新 import（after_line 设为最后一个 import 的行号）"
                "\n- 在文件开头插入（after_line=0）"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要编辑的文件路径（相对于项目根目录）",
                    },
                    "after_line": {
                        "type": "integer",
                        "description": (
                            "在哪个行号之后插入。0 = 文件开头（第 1 行之前），"
                            "5 = 第 5 行之后。对应 read_file 输出中的行号。"
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "要插入的文本内容",
                    },
                },
                "required": ["file_path", "after_line", "content"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return False

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        after_line = args.get("after_line")
        if after_line is None:
            return "缺少 after_line 参数"
        if int(after_line) < 0:
            return f"after_line 必须 >= 0，当前: {after_line}"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file_path = args["file_path"]
        after_line = int(args["after_line"])
        content = args.get("content", "")

        resolved = self._resolve(file_path, ctx)
        if resolved is None:
            return ToolResult(success=False, error=f"路径安全校验失败: {file_path}")

        if not resolved.exists():
            return ToolResult(success=False, error=f"文件不存在: {file_path}")
        if resolved.is_dir():
            return ToolResult(success=False, error=f"路径是目录，不是文件: {file_path}")

        # stale 检查
        file_state = ctx.extra.get("file_state")
        if isinstance(file_state, ReadFileState):
            stale_error = file_state.check_not_stale(resolved)
            if stale_error:
                return ToolResult(success=False, error=stale_error)

        # 读取
        try:
            file_content, encoding, line_ending = read_text_with_metadata(resolved)
        except Exception as exc:
            return ToolResult(success=False, error=f"读取文件失败: {exc}")

        lines = file_content.split("\n")
        total = len(lines)

        if after_line > total:
            return ToolResult(
                success=False,
                error=f"after_line ({after_line}) 超出文件总行数 ({total})",
            )

        insert_lines_list = content.split("\n")
        old_content = file_content

        result_lines = lines[:after_line] + insert_lines_list + lines[after_line:]
        result_content = "\n".join(result_lines)

        write_text_preserving(resolved, result_content, encoding, line_ending)

        if isinstance(file_state, ReadFileState):
            file_state.set(
                resolved,
                content=result_content,
                mtime=resolved.stat().st_mtime,
            )

        diff = make_unified_diff(file_path, old_content, result_content)

        logger.info(
            "InsertLinesTool: {} (after L{}, {} 行插入)",
            file_path, after_line, len(insert_lines_list),
        )

        return ToolResult(
            success=True,
            output={
                "path": file_path,
                "lines_inserted": len(insert_lines_list),
                "size": len(result_content),
                "diff": diff,
                "_modified_file": file_path,
            },
        )

    @staticmethod
    def _resolve(file_path: str, ctx: ToolContext) -> Path | None:
        try:
            return ctx.workspace_service.resolve_inside_workspace(ctx.workspace, file_path)
        except (WorkspaceSecurityError, Exception):
            return None
