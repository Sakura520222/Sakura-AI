"""Shell 工具 - 在工作区内执行命令

通过已有的 AgentTeamShellExecutor 执行。
支持超时和输出截断。
安全策略：黑名单模式，默认允许所有命令，仅拦截高危命令。
"""

from __future__ import annotations

import shlex
from typing import Any

from loguru import logger

from backend.core.config import get_settings
from backend.services.agent_team.shell_executor import AgentTeamShellExecutor
from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult

# Shell 元字符/模式，出现则拒绝执行以防止命令注入
# 单独的 $ 不拦截，仅拦截 $(...) 和 ${...} 等命令替换模式
# 命令链接 && || ; 和后台 & 仍拦截，防止命令注入
# 管道 | 允许（在 is_agent_command_allowed 中对每段做黑名单校验）
_SHELL_META_CHARS = frozenset({"&&", "||", ";", "`", "&"})
_SHELL_SUBST_PATTERNS = ("$('", "$(", "${")

# 默认拦截的高危命令（首 token 匹配）
_DEFAULT_BLOCKED_COMMANDS = frozenset({
    # 网络外泄（Agent 有独立的 fetch_url 工具）
    "curl", "wget", "nc", "ncat", "telnet",
    "ssh", "scp", "sftp", "rsync",
    # 系统管理
    "sudo", "su", "passwd", "chown",
    # Shell/解释器嵌套执行
    "bash", "sh", "zsh", "fish", "cmd", "powershell", "pwsh", "eval",
    # 进程控制
    "kill", "pkill", "killall",
    # 系统包管理（pip/npm/yarn 等工作区级别包管理不拦截）
    "apt", "apt-get", "yum", "dnf", "brew", "pacman", "snap", "flatpak",
    # 服务管理
    "systemctl", "service", "crontab", "launchctl",
    # 磁盘/系统
    "dd", "mkfs", "fdisk", "mount", "umount",
    # 容器
    "docker", "podman", "kubectl",
})


def _extract_unquoted(command: str) -> str:
    """提取命令中不在引号内的部分，用于检测 shell 元字符。"""
    result: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        c = command[i]
        if c == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if c == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if c == "\\" and not in_single and i + 1 < len(command):
            i += 2
            continue
        if not in_single and not in_double:
            result.append(c)
        i += 1
    return "".join(result)


def _contains_shell_meta(command: str) -> bool:
    """检查命令的非引号部分是否包含 Shell 元字符或命令替换模式。"""
    unquoted = _extract_unquoted(command)
    for meta in _SHELL_META_CHARS:
        if meta in unquoted:
            return True
    for pattern in _SHELL_SUBST_PATTERNS:
        if pattern in unquoted:
            return True
    return False


def _has_redundant_cd_prefix(command: str) -> bool:
    """检查是否包含多余的 cd ... && 前缀。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return len(tokens) >= 4 and tokens[0] == "cd" and tokens[2] == "&&"


def _command_name(first_token: str) -> str:
    """提取命令 basename（处理 /usr/bin/curl、C:/bin/curl.exe 等路径形式）。"""
    name = first_token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _is_segment_blocked(tokens: list[str], blocklist: set[str]) -> bool:
    """检查命令段是否被黑名单或危险参数策略拦截。"""
    name = _command_name(tokens[0])
    if name in blocklist:
        return True
    if name == "rm" and any(
        token == "--recursive" or (token.startswith("-") and "r" in token.lower())
        for token in tokens[1:]
    ):
        return True
    if name == "chmod" and any(token in {"777", "a+w", "ugo+w"} for token in tokens[1:]):
        return True
    if name in {"python", "python3", "py", "node", "ruby", "perl"} and any(
        token in {"-c", "-e"} for token in tokens[1:]
    ):
        return True
    return False


def _parse_pipe_segments(tokens: list[str]) -> list[list[str]] | None:
    """将 token 列表按管道符分段。返回 None 表示格式异常。"""
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token == "|":
            if not current:
                return None
            segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments if segments else None


async def is_agent_command_allowed(command: str) -> bool:
    """检查 Agent 命令是否允许执行（黑名单模式）。

    默认允许所有命令，仅拦截黑名单中的高危命令。
    管道 | 两侧的命令段分别进行黑名单校验。
    管道和重定向不拦截，由 shell_executor 的路径校验提供隔离。
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

    # 构建黑名单：默认 + 用户配置
    blocklist = set(_DEFAULT_BLOCKED_COMMANDS)
    raw = getattr(get_settings(), "agent_team_test_command_blocklist", "")
    if raw:
        blocklist.update(
            item.strip().lower()
            for item in str(raw).split(",")
            if item.strip()
        )

    # 按管道分段，每段独立校验
    segments = _parse_pipe_segments(tokens)
    if segments is None:
        return False

    for segment in segments:
        if _is_segment_blocked(segment, blocklist):
            logger.warning("Shell 命令被黑名单拦截: {}", segment[0])
            return False
    return True


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
                "\n\n大多数命令允许执行，以下高危命令被拦截："
                "\n- 网络工具: curl, wget, nc, ssh, scp, telnet 等"
                "\n- 系统管理: sudo, su, systemctl, apt-get, yum 等"
                "\n- 进程控制: kill, pkill, killall"
                "\n- 容器: docker, podman, kubectl"
                "\n\n支持管道 (|) 和重定向 (> >> <)。"
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

        if not await is_agent_command_allowed(command):
            return ToolResult(success=False, error="命令被安全策略拦截")

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
