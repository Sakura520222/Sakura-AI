"""ReplaceLines 工具 - 按行号范围替换文件内容

配合 ReadTool 的行号输出，直接指定行范围替换。
适合替换整个函数体、删除若干行等场景。
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


class ReplaceLinesTool(BaseTool):
    """按行号范围替换文件内容。"""

    name = "replace_lines"

    _schema = {
        "type": "function",
        "function": {
            "name": "replace_lines",
            "description": (
                "按行号范围替换文件内容。将文件的 start_line 到 end_line（含）替换为 new_content。"
                "\n\n典型用法：先用 read_file 查看文件内容（输出带行号），"
                "确定要替换的行号范围后，用本工具直接替换。"
                "\n\n适合以下场景："
                "\n- 替换整个函数体（如第 10-25 行）"
                "\n- 替换一个 class 的某几个方法"
                "\n- 修改配置文件的某一段"
                "\n- 删除若干行（new_content 设为空字符串）"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要编辑的文件路径（相对于项目根目录）",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（从 1 开始，包含该行）。对应 read_file 输出中的行号。",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（包含该行）。对应 read_file 输出中的行号。",
                    },
                    "new_content": {
                        "type": "string",
                        "description": "替换后的新内容（不含末尾换行）。设为空字符串可删除指定行。",
                    },
                },
                "required": ["file_path", "start_line", "end_line", "new_content"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return False

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        sl = args.get("start_line")
        el = args.get("end_line")
        if sl is None or el is None:
            return "缺少 start_line 或 end_line 参数"
        if int(sl) < 1:
            return f"start_line 必须 >= 1，当前: {sl}"
        if int(el) < int(sl):
            return f"end_line ({el}) 不能小于 start_line ({sl})"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file_path = args["file_path"]
        start_line = int(args["start_line"])
        end_line = int(args["end_line"])
        new_content = args.get("new_content", "")

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
            content, encoding, line_ending = read_text_with_metadata(resolved)
        except Exception as exc:
            return ToolResult(success=False, error=f"读取文件失败: {exc}")

        lines = content.split("\n")
        total = len(lines)

        if start_line > total:
            return ToolResult(
                success=False,
                error=f"start_line ({start_line}) 超出文件总行数 ({total})",
            )

        safe_end = min(end_line, total)
        new_lines = new_content.split("\n")
        old_content = content

        # 替换 [start_line-1 : safe_end] 为 new_lines
        result_lines = lines[: start_line - 1] + new_lines + lines[safe_end:]
        result_content = "\n".join(result_lines)

        # 写入
        write_text_preserving(resolved, result_content, encoding, line_ending)

        # 更新 file_state
        if isinstance(file_state, ReadFileState):
            file_state.set(
                resolved,
                content=result_content,
                mtime=resolved.stat().st_mtime,
            )

        replaced_count = safe_end - start_line + 1
        diff = make_unified_diff(file_path, old_content, result_content)

        logger.info(
            "ReplaceLinesTool: {} (L{}-L{}, {} 行被替换)",
            file_path, start_line, safe_end, replaced_count,
        )

        return ToolResult(
            success=True,
            output={
                "path": file_path,
                "lines_replaced": replaced_count,
                "size": len(result_content),
                "diff": diff,
            },
        )

    @staticmethod
    def _resolve(file_path: str, ctx: ToolContext) -> Path | None:
        try:
            return ctx.workspace_service.resolve_inside_workspace(ctx.workspace, file_path)
        except (WorkspaceSecurityError, Exception):
            return None
