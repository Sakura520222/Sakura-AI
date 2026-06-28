"""Revert File 工具 - 将文件恢复到 Git HEAD 状态

Agent 编辑出错时可以撤销修改，重新开始。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from backend.services.agent_team.shell_executor import AgentTeamShellExecutor
from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult


class RevertFileTool(BaseTool):
    """将文件恢复到 Git HEAD 状态。"""

    name = "revert_file"

    _schema = {
        "type": "function",
        "function": {
            "name": "revert_file",
            "description": (
                "将指定文件恢复到 Git HEAD 状态，丢弃所有未提交的修改。"
                "\n\n使用场景："
                "\n- 编辑出错且手动修复比重做更复杂时"
                "\n- 需要将文件恢复到修改前的干净状态重新开始"
                "\n\n注意：此操作不可逆，文件的所有未提交修改将被丢弃。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要恢复的文件路径（相对于项目根目录）",
                    },
                },
                "required": ["file_path"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return False

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        if not args.get("file_path"):
            return "缺少 file_path 参数"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file_path = args["file_path"].strip()
        workspace_service = ctx.workspace_service
        resolved = workspace_service.resolve_inside_workspace(ctx.workspace, file_path)
        rel_path = str(resolved).replace("\\", "/")

        executor = AgentTeamShellExecutor(ctx.workspace, workspace_service)
        result = await executor.run_args(["git", "checkout", "HEAD", "--", rel_path])

        if result.returncode != 0:
            return ToolResult(
                success=False,
                error=f"恢复文件失败: {result.stderr or result.stdout}",
            )

        logger.info("RevertFileTool: 恢复文件 {}", rel_path)
        return ToolResult(
            success=True,
            output={"file_path": rel_path, "message": "文件已恢复到 HEAD 状态"},
        )
