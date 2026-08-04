"""Write 工具 - 整文件写入

用于创建新文件或全量覆盖已有文件。
包含 stale 检查，防止覆盖外部修改。
"""

from __future__ import annotations

import asyncio
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


class WriteTool(BaseTool):
    """写入文件到工作区。"""

    name = "write_file"

    _schema = {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "写入文件到工作区。如果文件已存在则覆盖，不存在则创建。"
                "content 必须是完整的文件内容。"
                "\n\n使用场景："
                "\n- 创建新文件"
                "\n- 全量重写已有文件（如重构整个模块）"
                "\n\n注意：对于已有文件的小范围修改，优先使用 edit_file 或 replace_lines。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要写入的文件路径（相对于项目根目录）",
                    },
                    "content": {
                        "type": "string",
                        "description": "完整的文件内容",
                    },
                },
                "required": ["file_path", "content"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return False

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file_path = args["file_path"]
        content = args.get("content", "")

        resolved = self._resolve(file_path, ctx)
        if resolved is None:
            return ToolResult(success=False, error=f"路径安全校验失败: {file_path}")

        created = not resolved.exists()
        old_content = ""

        if resolved.exists():
            if resolved.is_dir():
                return ToolResult(
                    success=False, error=f"路径是目录，不是文件: {file_path}"
                )

            # stale 检查
            file_state = ctx.extra.get("file_state")
            if isinstance(file_state, ReadFileState):
                stale_error = await asyncio.to_thread(
                    file_state.check_not_stale, resolved
                )
                if stale_error:
                    return ToolResult(success=False, error=stale_error)

            old_content, encoding, line_ending = await asyncio.to_thread(
                read_text_with_metadata, resolved
            )
        else:
            encoding = "utf-8"
            line_ending = "\n"

        # 创建父目录
        await asyncio.to_thread(resolved.parent.mkdir, parents=True, exist_ok=True)

        # 写入
        normalized_content = content.replace("\r\n", "\n")
        await asyncio.to_thread(
            write_text_preserving,
            resolved,
            normalized_content,
            encoding,
            line_ending,
        )

        # 更新 file_state
        file_state = ctx.extra.get("file_state")
        if isinstance(file_state, ReadFileState):
            file_state.set(
                resolved,
                content=normalized_content,
                mtime=await asyncio.to_thread(lambda: resolved.stat().st_mtime),
            )

        # diff
        diff = ""
        if old_content:
            diff = make_unified_diff(file_path, old_content, normalized_content)

        logger.info(
            "WriteTool: {} ({} bytes, created={})", file_path, len(content), created
        )

        return ToolResult(
            success=True,
            output={
                "path": file_path,
                "size": len(content),
                "created": created,
                "diff": diff,
                "_modified_file": file_path,
            },
        )

    @staticmethod
    def _resolve(file_path: str, ctx: ToolContext) -> Path | None:
        try:
            return ctx.workspace_service.resolve_inside_workspace(
                ctx.workspace, file_path
            )
        except WorkspaceSecurityError, Exception:
            return None
