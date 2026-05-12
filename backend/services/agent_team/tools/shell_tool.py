"""Shell 工具 - 在工作区内执行命令

通过已有的 AgentTeamShellExecutor 执行。
支持超时和输出截断。
"""

from __future__ import annotations

import shlex
from typing import Any

from loguru import logger

from backend.core.config import get_dynamic_config, get_settings
from backend.services.agent_team.shell_executor import AgentTeamShellExecutor
from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult

# Shell 元字符/模式，出现则拒绝执行以防止命令注入
# 单独的 $ 不拦截，仅拦截 $(...) 和 ${...} 等命令替换模式
_SHELL_META_CHARS = frozenset({"&&", "||", ";", "|", "`", ">", ">>", "<", "&"})
_SHELL_SUBST_PATTERNS = ("$('", "$(", "${")


def _contains_shell_meta(command: str) -> bool:
    """检查命令字符串是否包含 Shell 元字符或命令替换模式。"""
    for meta in _SHELL_META_CHARS:
        if meta in command:
            return True
    # 精细检查命令替换：仅拦截 $(...) 和 ${...}，不拦截普通 $ 变量引用
    for pattern in _SHELL_SUBST_PATTERNS:
        if pattern in command:
            return True
    return False


async def is_agent_command_allowed(command: str) -> bool:
    """检查 Agent 可执行命令是否在配置白名单内。

    使用 shlex.split 提取命令首 token 进行精确匹配，并拒绝包含 Shell 元字符的命令。
    """
    command = command.strip()
    if not command:
        return False

    if _contains_shell_meta(command):
        return False

    try:
        tokens = shlex.split(command)
    except ValueError:
        return False

    if not tokens:
        return False

    first_token = tokens[0]

    raw = await get_dynamic_config("agent_team_test_command_allowlist")
    if raw is None:
        raw = get_settings().agent_team_test_command_allowlist

    allowlist = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    if not allowlist:
        return False

    for allowed in allowlist:
        allowed_tokens = shlex.split(allowed)
        if not allowed_tokens:
            continue
        allowed_first = allowed_tokens[0]
        if first_token != allowed_first:
            continue
        # 白名单项无参数时，允许任意参数；白名单项有参数时，命令参数必须以白名单参数为前缀
        if len(allowed_tokens) == 1:
            return True
        if (
            len(tokens) >= len(allowed_tokens)
            and tokens[: len(allowed_tokens)] == allowed_tokens
        ):
            return True
    return False


class ShellTool(BaseTool):
    """在工作区内执行 Shell 命令。"""

    name = "run_command"

    _schema = {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "在工作区内执行 shell 命令（如运行测试、检查语法、构建等）。"
                "命令在项目根目录执行，不允许跳出工作区，且必须匹配配置的白名单。"
                "\n\n常用命令："
                "\n- 运行测试: pytest -q 或 python -m pytest tests/ -q"
                "\n- 语法检查: python -m py_compile file.py"
                "\n- 类型检查: mypy file.py"
                "\n- Lint: ruff check file.py"
                "\n- 查看文件: cat / head / tail"
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

        if not await is_agent_command_allowed(command):
            return ToolResult(success=False, error="命令不在 Agent 验证命令白名单中")

        executor = AgentTeamShellExecutor(ctx.workspace, ctx.workspace_service)

        try:
            result = await executor.run(command, timeout_seconds=timeout)
        except Exception as exc:
            return ToolResult(
                success=False, error=f"命令执行失败: {type(exc).__name__}: {exc}"
            )

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
