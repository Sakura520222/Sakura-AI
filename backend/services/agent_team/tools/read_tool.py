"""Read 工具 - 读取工作区文件

支持完整读取和行范围读取，输出带行号。
读取后更新 file_state 缓存，防止后续编辑覆盖外部修改。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult
from backend.services.agent_team.tools.file_state import ReadFileState
from backend.services.agent_team.tools.file_utils import read_text_with_metadata
from backend.services.agent_team.workspace_service import WorkspaceSecurityError


class ReadTool(BaseTool):
    """读取工作区内文件。"""

    name = "read_file"

    _schema = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取工作区内指定文件的内容。支持完整读取或行范围读取，返回带行号的文件内容。"
                "\n\n使用场景："
                "\n- 查看源代码文件"
                "\n- 确认要修改的代码段（修改前务必先读取）"
                "\n- 查看配置文件"
                "\n- 行范围读取大文件的某一段"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要读取的文件路径（相对于项目根目录）",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（从 1 开始），可选。不指定则从第 1 行开始。",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（包含该行），可选。不指定则读到文件末尾。",
                    },
                },
                "required": ["file_path"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        if not args.get("file_path"):
            return "缺少 file_path 参数"
        sl = args.get("start_line")
        el = args.get("end_line")
        if sl is not None and int(sl) < 1:
            return f"start_line 必须 >= 1，当前: {sl}"
        if sl is not None and el is not None and int(el) < int(sl):
            return f"end_line ({el}) 不能小于 start_line ({sl})"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file_path = args["file_path"]
        start_line = args.get("start_line")
        end_line = args.get("end_line")

        resolved = self._resolve(file_path, ctx)
        if resolved is None:
            return ToolResult(success=False, error=f"路径安全校验失败: {file_path}")

        if not resolved.exists():
            return ToolResult(success=False, error=f"文件不存在: {file_path}")
        if resolved.is_dir():
            return ToolResult(success=False, error=f"路径是目录，不是文件: {file_path}")

        try:
            content, _encoding, _line_ending = await asyncio.to_thread(
                read_text_with_metadata, resolved
            )
        except Exception as exc:
            return ToolResult(success=False, error=f"读取文件失败: {exc}")

        lines = content.split("\n")
        total_lines = len(lines)

        # 行范围读取
        s = max(0, (start_line or 1) - 1)
        e = min(total_lines, end_line or total_lines)
        selected = lines[s:e]
        is_full_read = start_line is None and end_line is None

        # 添加行号输出
        numbered = []
        for i, line in enumerate(selected, start=s + 1):
            numbered.append(f"{i:>6}\t{line}")
        output_content = "\n".join(numbered)

        # 更新文件状态缓存
        file_state = ctx.extra.get("file_state")
        if isinstance(file_state, ReadFileState):
            mtime = await asyncio.to_thread(lambda: resolved.stat().st_mtime)
            file_state.set(
                resolved,
                content=content,
                mtime=mtime,
                start_line=start_line,
                end_line=end_line,
                is_full_read=is_full_read,
            )

        logger.debug("ReadTool: {} ({} 行, L{}-L{})", file_path, total_lines, s + 1, e)

        return ToolResult(
            success=True,
            output={
                "content": output_content,
                "path": file_path,
                "total_lines": total_lines,
                "start_line": s + 1,
                "end_line": e,
            },
        )

    @staticmethod
    def _resolve(file_path: str, ctx: ToolContext) -> Path | None:
        try:
            return ctx.workspace_service.resolve_inside_workspace(
                ctx.workspace, file_path
            )
        except (WorkspaceSecurityError, Exception):
            return None
