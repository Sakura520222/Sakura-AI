"""Git Diff 工具 - 查看工作区累积变更

让 Agent 能查看自基础提交以来的所有修改，支持摘要和完整 diff 两种模式。
"""

from __future__ import annotations

from typing import Any

from backend.services.agent_team.shell_executor import AgentTeamShellExecutor
from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


class GitDiffTool(BaseTool):
    """查看工作区 Git 变更（累积 diff）。"""

    name = "check_changes"

    _schema = {
        "type": "function",
        "function": {
            "name": "check_changes",
            "description": (
                "查看自基础提交以来的工作区累积变更。"
                "\n\n使用场景："
                "\n- 实现阶段：检查已修改了哪些文件 (mode=summary)"
                "\n- 验证阶段：查看具体修改内容 (mode=full)"
                "\n- 提交前：确认所有变更符合预期"
                "\n\n建议：每次实现一批修改后调用 summary 模式确认范围，提交前用 full 模式审查细节。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["summary", "full"],
                        "description": (
                            "summary: 文件级统计（文件名、增删行数），适合快速浏览变更范围。"
                            " full: 指定文件的完整 diff 内容，适合审查具体修改细节。"
                        ),
                        "default": "summary",
                    },
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "要查看的文件路径列表（相对于项目根目录）。"
                            "仅在 mode=full 时生效。省略则显示所有已修改文件的 diff。"
                        ),
                    },
                },
                "required": [],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        mode = args.get("mode", "summary")
        file_paths = args.get("file_paths")

        workspace_service = ctx.workspace_service or AgentTeamWorkspaceService()
        executor = AgentTeamShellExecutor(ctx.workspace, workspace_service)

        if mode == "summary":
            return await self._run_summary(executor)
        return await self._run_full(executor, file_paths, ctx)

    async def _run_summary(
        self, executor: AgentTeamShellExecutor
    ) -> ToolResult:
        """git diff --stat + git status --short"""
        stat_result = await executor.run_args(["git", "diff", "--stat"])
        status_result = await executor.run_args(["git", "status", "--short"])
        if stat_result.returncode != 0:
            return ToolResult(
                success=False,
                error=f"git diff --stat 失败: {stat_result.stderr}",
            )

        stat_output = stat_result.stdout.strip()
        status_output = status_result.stdout.strip()

        if not stat_output and not status_output:
            return ToolResult(
                success=True,
                output={
                    "has_changes": False,
                    "message": "工作区无变更",
                },
            )

        return ToolResult(
            success=True,
            output={
                "has_changes": True,
                "stat": stat_output,
                "status": status_output,
            },
        )

    async def _run_full(
        self,
        executor: AgentTeamShellExecutor,
        file_paths: list[str] | None,
        ctx: ToolContext,
    ) -> ToolResult:
        """git diff [files] — 显示完整 diff 内容。"""
        git_args: list[str] = ["git", "diff"]
        if file_paths and isinstance(file_paths, list):
            for fp in file_paths:
                if not isinstance(fp, str) or not fp.strip():
                    continue
                resolved = ctx.workspace_service.resolve_inside_workspace(
                    ctx.workspace, fp.strip()
                )
                rel = str(resolved).replace("\\", "/")
                git_args.append(rel)

        result = await executor.run_args(git_args)
        if result.returncode != 0:
            return ToolResult(
                success=False, error=f"git diff 失败: {result.stderr}"
            )

        diff_output = result.stdout.strip()
        if not diff_output:
            return ToolResult(
                success=True,
                output={
                    "has_changes": False,
                    "message": "指定文件无变更" if file_paths else "工作区无变更",
                },
            )

        # 截断超大 diff
        max_diff = 16000
        truncated = len(diff_output) > max_diff
        diff_output = diff_output[:max_diff]

        return ToolResult(
            success=True,
            output={
                "has_changes": True,
                "diff": diff_output,
                "truncated": truncated,
            },
        )
