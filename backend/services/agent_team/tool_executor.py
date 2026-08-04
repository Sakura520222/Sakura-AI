"""Agent 专家团队 - 工具执行器

负责执行 AI 通过 function calling 请求的工具调用，
包括读文件、写文件、搜索、执行命令等。
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from backend.services.agent_team.file_tools import AgentTeamFileTools
from backend.services.agent_team.shell_executor import AgentTeamShellExecutor
from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.tools.grep_tool import (
    MAX_GREP_KEYWORD_LENGTH,
    GrepTool,
)
from backend.services.agent_team.tools.shell_tool import is_agent_command_allowed
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


class AgentToolExecutor:
    """执行 Agent 工具调用。

    .. deprecated::
        旧版工具执行器，使用 handler 字典分发。
        新代码应使用 ``backend.services.agent_team.tools.base.ToolExecutor``。
    """

    def __init__(
        self,
        workspace: str | Any,
        workspace_service: AgentTeamWorkspaceService | None = None,
    ):
        self.workspace_service = workspace_service or AgentTeamWorkspaceService()
        self.workspace = self.workspace_service.resolve_inside_workspace(workspace)
        self.file_tools = AgentTeamFileTools(self.workspace, self.workspace_service)
        self.executor = AgentTeamShellExecutor(self.workspace, self.workspace_service)

    async def execute_tool_call(self, tool_call: Any) -> dict[str, Any]:
        """执行单个工具调用，返回结果字典。"""
        function_name = tool_call.function.name
        try:
            arguments = json.loads(tool_call.function.arguments)
        except (json.JSONDecodeError, TypeError):
            return {"error": f"无法解析工具参数: {tool_call.function.arguments}"}

        handler = {
            "read_file": self._handle_read_file,
            "list_directory": self._handle_list_directory,
            "write_file": self._handle_write_file,
            "edit_file": self._handle_edit_file,
            "replace_lines": self._handle_replace_lines,
            "insert_lines": self._handle_insert_lines,
            "search_in_files": self._handle_search_in_files,
            "run_command": self._handle_run_command,
            "finish_task": self._handle_finish_task,
            "submit_review": self._handle_submit_review,
        }.get(function_name)

        if not handler:
            return {"error": f"未知工具: {function_name}"}

        try:
            return await handler(arguments)
        except Exception as e:
            logger.error("Agent 工具执行失败 {}: {}", function_name, e)
            return {"error": f"工具执行失败: {type(e).__name__}: {e}"}

    # ── 具体工具实现 ──────────────────────────────────────

    async def _handle_read_file(self, args: dict) -> dict:
        file_path = args.get("file_path", "")
        if not file_path:
            return {"error": "缺少 file_path 参数"}

        result = await self.file_tools.read_file_async(file_path)
        if not result.exists:
            return {"error": f"文件不存在: {file_path}"}

        content = result.content
        start_line = args.get("start_line")
        end_line = args.get("end_line")

        if start_line or end_line:
            lines = content.split("\n")
            s = max(0, (start_line or 1) - 1)
            e = min(len(lines), end_line or len(lines))
            selected = lines[s:e]
            # 添加行号
            numbered = []
            for i, line in enumerate(selected, start=s + 1):
                numbered.append(f"{i:>6}  {line}")
            return {
                "content": "\n".join(numbered),
                "path": file_path,
                "total_lines": len(lines),
            }

        # 完整内容加行号
        lines = content.split("\n")
        numbered = []
        for i, line in enumerate(lines, start=1):
            numbered.append(f"{i:>6}  {line}")
        return {
            "content": "\n".join(numbered),
            "path": file_path,
            "total_lines": len(lines),
            "size": result.size,
        }

    async def _handle_list_directory(self, args: dict) -> dict:
        directory = args.get("directory", ".")
        recursive = args.get("recursive", False)

        entries = await self.file_tools.list_files_async(directory, recursive=recursive)
        items = []
        for entry in entries:
            prefix = "📁" if entry.is_dir else "📄"
            items.append(f"{prefix} {entry.path} ({entry.size} bytes)")

        return {
            "directory": directory,
            "entries": items,
            "total": len(items),
        }

    async def _handle_write_file(self, args: dict) -> dict:
        file_path = args.get("file_path", "")
        content = args.get("content", "")
        if not file_path:
            return {"error": "缺少 file_path 参数"}

        result = await self.file_tools.write_file_async(file_path, content)
        logger.info(
            "Agent 写入文件: {} ({} bytes, created={})",
            file_path,
            result.size,
            result.created,
        )
        return {
            "success": True,
            "path": file_path,
            "size": result.size,
            "created": result.created,
        }

    async def _handle_edit_file(self, args: dict) -> dict:
        file_path = args.get("file_path", "")
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        replace_all = args.get("replace_all", False)
        if not file_path:
            return {"error": "缺少 file_path 参数"}
        if not old_text:
            return {"error": "缺少 old_text 参数"}
        try:
            result = await self.file_tools.edit_file_async(
                file_path, old_text, new_text, replace_all=replace_all
            )
            logger.info("Agent 字符串替换: {} ({} 处)", file_path, result.replacements)
            return {
                "success": True,
                "path": result.path,
                "replacements": result.replacements,
                "size": result.size,
            }
        except (FileNotFoundError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_replace_lines(self, args: dict) -> dict:
        file_path = args.get("file_path", "")
        start_line = args.get("start_line")
        end_line = args.get("end_line")
        new_content = args.get("new_content", "")
        if not file_path:
            return {"error": "缺少 file_path 参数"}
        if start_line is None or end_line is None:
            return {"error": "缺少 start_line 或 end_line 参数"}
        try:
            result = await self.file_tools.replace_lines_async(
                file_path, int(start_line), int(end_line), new_content
            )
            logger.info(
                "Agent 行号替换: {} (L{}-L{}, {} 行被替换)",
                file_path,
                start_line,
                end_line,
                result.replacements,
            )
            return {
                "success": True,
                "path": result.path,
                "lines_replaced": result.replacements,
                "size": result.size,
            }
        except (FileNotFoundError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_insert_lines(self, args: dict) -> dict:
        file_path = args.get("file_path", "")
        after_line = args.get("after_line")
        content = args.get("content", "")
        if not file_path:
            return {"error": "缺少 file_path 参数"}
        if after_line is None:
            return {"error": "缺少 after_line 参数"}
        try:
            result = await self.file_tools.insert_lines_async(
                file_path, int(after_line), content
            )
            logger.info(
                "Agent 行号插入: {} (after L{}, {} 行插入)",
                file_path,
                after_line,
                result.replacements,
            )
            return {
                "success": True,
                "path": result.path,
                "lines_inserted": result.replacements,
                "size": result.size,
            }
        except (FileNotFoundError, ValueError) as e:
            return {"error": str(e)}

    async def _handle_search_in_files(self, args: dict) -> dict:
        keyword = args.get("keyword", "")
        if not keyword:
            return {"error": "缺少 keyword 参数"}
        if len(keyword) > MAX_GREP_KEYWORD_LENGTH:
            return {"error": f"keyword 不能超过 {MAX_GREP_KEYWORD_LENGTH} 个字符"}

        # 旧工具执行器复用新 GrepTool，避免维护另一套 shell grep 路径与校验逻辑。
        tool = GrepTool()
        tool_args = {
            "keyword": keyword,
            "file_extension": args.get("file_extension", ""),
            "output_mode": args.get("output_mode", "content"),
            "case_insensitive": args.get("case_insensitive", False),
        }
        ctx = ToolContext(
            workspace=str(self.workspace),
            workspace_service=self.workspace_service,
            extra={},
        )
        error = tool.validate_input(tool_args, ctx)
        if error:
            return {"error": error}
        result = await tool.execute(tool_args, ctx)
        if not result.success:
            return {"error": result.error}
        return result.output

    async def _handle_run_command(self, args: dict) -> dict:
        command = args.get("command", "")
        if not command:
            return {"error": "缺少 command 参数"}
        if not await is_agent_command_allowed(command):
            return {"error": "命令被安全策略拦截"}

        result = await self.executor.run(command, timeout_seconds=120)
        return {
            "returncode": result.returncode,
            "stdout": result.stdout[:5000],  # 限制输出大小
            "stderr": result.stderr[:2000],
            "timed_out": result.timed_out,
        }

    async def _handle_finish_task(self, args: dict) -> dict:
        """全栈专家完成任务，返回结构化结果供调用者处理。"""
        return {
            "_finish": True,
            "summary": args.get("summary", ""),
            "modified_files": args.get("modified_files", []),
            "risk_level": args.get("risk_level", "medium"),
            "test_result": args.get("test_result", ""),
        }

    async def _handle_submit_review(self, args: dict) -> dict:
        """审查角色提交审查结果。"""
        return {
            "_review": True,
            "verdict": args.get("verdict", "reject"),
            "score": args.get("score", 0),
            "summary": args.get("summary", ""),
            "findings": args.get("findings", []),
            "improvement_suggestions": args.get("improvement_suggestions", []),
        }
