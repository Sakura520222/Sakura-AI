"""Shell 工具 - 在工作区内执行命令

通过已有的 AgentTeamShellExecutor 执行。
支持超时和输出截断。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from backend.services.agent_team.shell_executor import AgentTeamShellExecutor
from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult


class ShellTool(BaseTool):
    """在工作区内执行 Shell 命令。"""

    name = "run_command"

    _schema = {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "在工作区内执行 shell 命令（如运行测试、检查语法、构建等）。"
                "命令在项目根目录执行，不允许跳出工作区。"
                "\n\n常用命令："
                "\n- 运行测试: pytest -q 或 python -m pytest tests/ -q"
                "\n- 语法检查: python -m py_compile file.py"
                "\n- 类型检查: mypy file.py"
                "\n- Lint: ruff check file.py"
                "\n- 查看文件: cat / head / tail"
                "\n- Git 操作: git status / git diff / git log"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 shell 命令",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒数，默认 120。长命令如测试可设为 300。",
                        "default": 120,
                    },
                },
                "required": ["command"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return False

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        if not args.get("command"):
            return "缺少 command 参数"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args["command"]
        timeout = min(int(args.get("timeout", 120)), 600)  # 最大 600 秒

        executor = AgentTeamShellExecutor(ctx.workspace, ctx.workspace_service)

        try:
            result = await executor.run(command, timeout_seconds=timeout)
        except Exception as exc:
            return ToolResult(success=False, error=f"命令执行失败: {type(exc).__name__}: {exc}")

        stdout = result.stdout
        stderr = result.stderr

        # 截断大输出
        max_stdout = 8000
        max_stderr = 3000
        truncated_stdout = len(stdout) > max_stdout
        truncated_stderr = len(stderr) > max_stderr
        stdout = stdout[:max_stdout]
        stderr = stderr[:max_stderr]

        logger.info(
            "ShellTool: {} (rc={}, {}ms)",
            command[:60],
            result.returncode,
            "timed_out" if result.timed_out else "ok",
        )

        return ToolResult(
            success=True,
            output={
                "returncode": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": result.timed_out,
                "truncated_stdout": truncated_stdout,
                "truncated_stderr": truncated_stderr,
            },
        )
