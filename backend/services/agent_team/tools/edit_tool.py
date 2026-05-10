"""Edit 工具 - 精确字符串替换

核心编辑工具，基于 old_string → new_string 替换。
支持容错匹配（弯引号/空格归一化）。
多处匹配时拒绝并要求 AI 提供更多上下文。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult
from backend.services.agent_team.tools.file_state import ReadFileState
from backend.services.agent_team.tools.file_utils import (
    find_actual_string,
    make_unified_diff,
    read_text_with_metadata,
    write_text_preserving,
)
from backend.services.agent_team.workspace_service import WorkspaceSecurityError


class EditTool(BaseTool):
    """精确替换文件中的文本片段。"""

    name = "edit_file"

    _schema = {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "精确替换文件中的文本片段。查找 old_text 并替换为 new_text。"
                "适用于小范围修改（修 bug、改函数签名、改返回值等）。"
                "\n\n重要规则："
                "\n- 修改前必须先 read_file 查看文件内容"
                "\n- old_text 必须与文件内容完全一致（包括缩进、空格、换行）"
                "\n  建议从 read_file 输出中精确复制（去掉行号前缀后的内容）"
                "\n- 如果 old_text 在文件中匹配多处，必须扩大上下文使匹配唯一，"
                "或设 replace_all=true"
                "\n- 不适合大段修改，大段修改请用 replace_lines 或 write_file"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要编辑的文件路径（相对于项目根目录）",
                    },
                    "old_text": {
                        "type": "string",
                        "description": (
                            "要被替换的原始文本，必须与文件内容完全一致。"
                            "建议从 read_file 的输出中精确复制。"
                        ),
                    },
                    "new_text": {
                        "type": "string",
                        "description": "替换后的新文本",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换所有匹配项，默认 false（只替换第一个匹配）",
                        "default": False,
                    },
                },
                "required": ["file_path", "old_text", "new_text"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return False

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        if not args.get("file_path"):
            return "缺少 file_path 参数"
        if not args.get("old_text"):
            return "缺少 old_text 参数"
        old = args["old_text"]
        new = args["new_text"]
        if old == new:
            return "old_text 和 new_text 完全相同，无需替换"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file_path = args["file_path"]
        old_text = args["old_text"]
        new_text = args["new_text"]
        replace_all = args.get("replace_all", False)

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

        # 读取并检测编码/行尾
        try:
            content, encoding, line_ending = read_text_with_metadata(resolved)
        except Exception as exc:
            return ToolResult(success=False, error=f"读取文件失败: {exc}")

        # 容错查找
        actual_old = find_actual_string(content, old_text)
        if actual_old is None:
            return ToolResult(
                success=False,
                error=(
                    f"在 {file_path} 中未找到要替换的文本。"
                    "请先用 read_file 查看文件内容，"
                    "确保 old_text 与文件中的文本完全一致（包括缩进和空行）。"
                ),
            )

        # 多匹配检查
        if not replace_all:
            count = content.count(actual_old)
            if count > 1:
                return ToolResult(
                    success=False,
                    error=(
                        f"在 {file_path} 中找到 {count} 处匹配。"
                        "请扩大 old_text 的范围（包含更多上下文行）使匹配唯一，"
                        "或者使用 replace_all=true 替换所有匹配。"
                    ),
                )

        # 执行替换
        old_content = content
        if replace_all:
            new_content = content.replace(actual_old, new_text)
            replacements = count
        else:
            new_content = content.replace(actual_old, new_text, 1)
            replacements = 1

        # 写入保留编码/行尾
        write_text_preserving(resolved, new_content, encoding, line_ending)

        # 更新 file_state
        if isinstance(file_state, ReadFileState):
            file_state.set(
                resolved,
                content=new_content,
                mtime=resolved.stat().st_mtime,
            )

        # 生成 diff
        diff = make_unified_diff(file_path, old_content, new_content)

        logger.info("EditTool: {} ({} 处替换)", file_path, replacements)

        return ToolResult(
            success=True,
            output={
                "path": file_path,
                "replacements": replacements,
                "size": len(new_content),
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
