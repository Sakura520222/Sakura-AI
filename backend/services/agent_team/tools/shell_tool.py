"""Shell 工具 - 在工作区内执行命令

通过已有的 AgentTeamShellExecutor 执行。
支持超时并返回完整命令输出。
安全边界由注入的 workspace-scoped 执行器提供；不使用命令词黑名单。
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from typing import Any

from loguru import logger

from backend.services.agent_team.execution import (
    ExecutionProfile,
    ExecutionRequest,
    execute_request,
    execution_workspace_key,
    resolve_execution_runner,
)
from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult


def _agent_command_block_reason(command: str) -> str | None:
    """仅保留产品语义；OS 边界由执行器负责。

    旧版按命令词拦截 curl/bash/python -c 等内容既误伤正常构建步骤，
    也不能作为安全边界。Docker 模式的网络、身份、文件系统和进程限制
    由 sandboxd/OCI 策略执行；local 模式仍由 LocalExecutionRunner 做
    workspace 路径约束。这里只拒绝空输入。
    """
    if not isinstance(command, str) or not command.strip():
        return "Shell 命令不能为空"
    return None


def _has_redundant_cd_prefix(command: str) -> bool:
    """保留产品交互语义：工具已在工作区根目录。"""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return len(tokens) >= 4 and tokens[0] == "cd" and tokens[2] == "&&"


async def is_agent_command_allowed(command: str) -> bool:
    """检查产品级命令输入；不把词法黑名单当作沙箱。"""
    return _agent_command_block_reason(command) is None


class ShellTool(BaseTool):
    """在工作区内执行 Shell 命令。"""

    name = "run_command"

    _schema = {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "在目标仓库工作区根目录执行 shell 命令。"
                "重要：当前工作目录已经是仓库工作区根目录，"
                "请直接运行目标命令，例如 pytest -q、ruff check、npm test。"
                "\n\n命令会在当前 workspace-scoped 执行器中运行；Docker 部署的"
                "网络、权限、进程和文件系统边界由 sandboxd/OCI 策略提供。"
                "\n当前工作目录已经是工作区根目录，请不要添加 cd 前缀。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "要执行的 shell 命令。直接写命令本身，不要加 cd ... && 前缀。"
                        ),
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

        if _has_redundant_cd_prefix(command):
            return ToolResult(
                success=False,
                error=(
                    "当前已处于工作区根目录，请直接运行目标命令，"
                    "不要添加 cd workplace &&、cd home && 或 cd <repo> && 前缀。"
                ),
            )

        block_reason = _agent_command_block_reason(command)
        if block_reason:
            return ToolResult(
                success=False, error=f"命令被安全策略拦截: {block_reason}"
            )

        runner = resolve_execution_runner(
            ctx.execution_runner,
            ctx.workspace,
            ctx.workspace_service,
        )
        request = ExecutionRequest(
            workspace_key=execution_workspace_key(
                ctx.workspace, ctx.workspace_service
            ),
            command=command,
            cwd=PurePosixPath("."),
            profile=ExecutionProfile.AGENT,
            timeout_seconds=timeout,
            cancel_event=ctx.cancel_event,
        )

        try:
            result = await execute_request(runner, request)
        except Exception as exc:
            return ToolResult(
                success=False, error=f"命令执行失败: {type(exc).__name__}: {exc}"
            )

        logger.info(
            "ShellTool completed (rc={}, {})",
            result.returncode,
            "timed_out" if result.timed_out else "ok",
        )

        if result.infrastructure_error:
            return ToolResult(
                success=False,
                error=f"命令执行基础设施失败: {result.infrastructure_error}",
            )

        return ToolResult(
            success=True,
            output={
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": result.timed_out,
            },
        )
