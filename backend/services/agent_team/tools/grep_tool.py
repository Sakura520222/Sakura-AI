"""Grep 工具 - 通过 workspace-scoped 执行器搜索文件内容。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from backend.services.agent_team.execution import (
    ExecutionProfile,
    ExecutionRequest,
    execute_request,
    execution_workspace_key,
    resolve_execution_runner,
)
from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult
from backend.utils.search_excludes import SEARCH_EXCLUDES

MAX_GREP_KEYWORD_LENGTH = 500


class GrepTool(BaseTool):
    """搜索工作区内文件内容。"""

    name = "search_in_files"

    _schema = {
        "type": "function",
        "function": {
            "name": "search_in_files",
            "description": (
                "在工作区内搜索指定文本（固定字符串匹配），返回匹配的文件和行内容。"
                "\n\n使用场景："
                "\n- 搜索函数定义、类定义"
                "\n- 查找某个变量的使用位置"
                "\n- 搜索配置项"
                "\n- 确认某个 API 的调用方式"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词（固定字符串匹配）",
                    },
                    "file_extension": {
                        "type": "string",
                        "description": "可选：限定文件后缀，如 .py、.ts",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "content"],
                        "description": (
                            "输出模式：files_with_matches 只返回文件名（默认），"
                            "content 返回匹配行内容"
                        ),
                        "default": "files_with_matches",
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "是否忽略大小写，默认 false",
                        "default": False,
                    },
                },
                "required": ["keyword"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        keyword = str(args.get("keyword") or "")
        if not keyword:
            return "缺少 keyword 参数"
        if len(keyword) > MAX_GREP_KEYWORD_LENGTH:
            return f"keyword 不能超过 {MAX_GREP_KEYWORD_LENGTH} 个字符"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        keyword = args["keyword"]
        file_ext = args.get("file_extension", "")
        output_mode = args.get("output_mode", "files_with_matches")
        case_insensitive = args.get("case_insensitive", False)

        # Grep is always executed through the injected runner.  In particular,
        # there is no host-side Python fallback: a missing/unhealthy sandbox
        # must fail closed instead of reading the workspace in the Web process.
        cmd_parts = ["grep", "-rn", "-F"]
        if case_insensitive:
            cmd_parts.append("-i")
        if file_ext:
            cmd_parts.extend(["--include", f"*{file_ext}"])
        for excl in SEARCH_EXCLUDES:
            cmd_parts.extend(["--exclude-dir", excl])
        cmd_parts.extend(["--", keyword, "."])

        try:
            runner = resolve_execution_runner(
                ctx.execution_runner,
                ctx.workspace,
                ctx.workspace_service,
            )
            request = ExecutionRequest(
                workspace_key=execution_workspace_key(
                    ctx.workspace, ctx.workspace_service
                ),
                argv=tuple(cmd_parts),
                cwd=PurePosixPath("."),
                profile=ExecutionProfile.AGENT,
                timeout_seconds=30,
                cancel_event=ctx.cancel_event,
            )
            result = await execute_request(runner, request)
            output = result.stdout.strip()

            if result.returncode == 0 and output:
                lines = output.split("\n")
                max_lines = 100
                truncated = len(lines) > max_lines
                lines = lines[:max_lines]

                if output_mode == "files_with_matches":
                    # 提取唯一文件名
                    files = sorted(
                        {line.split(":")[0] for line in lines if ":" in line}
                    )
                    return ToolResult(
                        success=True,
                        output={
                            "files": files[:50],
                            "num_files": len(files),
                            "keyword": keyword,
                            "truncated": truncated or len(files) > 50,
                        },
                    )

                return ToolResult(
                    success=True,
                    output={
                        "matches": lines,
                        "total": len(output.split("\n")),
                        "keyword": keyword,
                        "truncated": truncated,
                    },
                )

        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"grep 沙箱执行失败: {type(exc).__name__}: {exc}",
            )

        if result.returncode == 1:
            # grep's stable no-match status is a successful search result.
            return ToolResult(
                success=True,
                output=(
                    {
                        "files": [],
                        "num_files": 0,
                        "keyword": keyword,
                        "truncated": False,
                    }
                    if output_mode == "files_with_matches"
                    else {
                        "matches": [],
                        "total": 0,
                        "keyword": keyword,
                        "truncated": False,
                    }
                ),
            )

        return ToolResult(
            success=False,
            error=f"grep 执行失败 (rc={result.returncode}): {result.stderr}",
        )
