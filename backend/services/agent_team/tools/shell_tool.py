"""Shell 工具 - 在工作区内执行命令

通过已有的 AgentTeamShellExecutor 执行。
支持超时并返回完整命令输出。
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
# fd 重定向 2>&1 不拦截；管道 | 允许并按命令段做黑名单校验。
_SHELL_META_TOKENS = ("&&", "||", ";", "`")
_SHELL_SUBST_PATTERNS = ("$('", "$(", "${")
_ALLOWED_FD_REDIRECTS = frozenset({"1>&2", "2>&1"})

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


def _shell_meta_block_reason(command: str) -> str | None:
    """检查命令的非引号部分是否包含不允许的 Shell 元字符。"""
    unquoted = _extract_unquoted(command)
    for meta in _SHELL_META_TOKENS:
        if meta in unquoted:
            return f"包含未允许的 shell 元字符: {meta}"
    for pattern in _SHELL_SUBST_PATTERNS:
        if pattern in unquoted:
            return f"包含未允许的 shell 命令替换模式: {pattern}"
    for token in unquoted.split():
        if "&" in token and token not in _ALLOWED_FD_REDIRECTS:
            return "包含未允许的 shell 元字符: &"
    return None


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


def _segment_block_reason(tokens: list[str], blocklist: set[str]) -> str | None:
    """检查命令段是否被黑名单或危险参数策略拦截。"""
    name = _command_name(tokens[0])
    if name in blocklist:
        return f"命令位于黑名单: {name}"
    if name == "rm" and any(
        token == "--recursive" or (token.startswith("-") and "r" in token.lower())
        for token in tokens[1:]
    ):
        return "递归 rm 命令被拦截"
    if name == "chmod" and any(token in {"777", "a+w", "ugo+w"} for token in tokens[1:]):
        return "高权限 chmod 命令被拦截"
    if name in {"python", "python3", "py", "node", "ruby", "perl"} and any(
        token in {"-c", "-e"} for token in tokens[1:]
    ):
        return f"解释器内联执行被拦截: {name}"
    return None


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


def _agent_command_block_reason(command: str) -> str | None:
    """返回 Agent 命令被安全策略拦截的原因。"""
    command = command.strip()
    if not command:
        return "Shell 命令不能为空"

    meta_reason = _shell_meta_block_reason(command)
    if meta_reason:
        return meta_reason

    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return f"Shell 命令解析失败: {exc}"

    if not tokens:
        return "Shell 命令不能为空"

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
        return "管道格式不合法"

    for segment in segments:
        reason = _segment_block_reason(segment, blocklist)
        if reason:
            logger.warning("Shell 命令被安全策略拦截: {}", reason)
            return reason
    return None


async def is_agent_command_allowed(command: str) -> bool:
    """检查 Agent 命令是否允许执行（黑名单模式）。

    默认允许所有命令，仅拦截黑名单中的高危命令。
    管道 | 两侧的命令段分别进行黑名单校验。
    管道和重定向不拦截，由 shell_executor 的路径校验提供隔离。
    """
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

        block_reason = _agent_command_block_reason(command)
        if block_reason:
            return ToolResult(success=False, error=f"命令被安全策略拦截: {block_reason}")

        executor = AgentTeamShellExecutor(ctx.workspace, ctx.workspace_service)

        try:
            result = await executor.run(command, timeout_seconds=timeout)
        except Exception as exc:
            return ToolResult(
                success=False, error=f"命令执行失败: {type(exc).__name__}: {exc}"
            )

        logger.info(
            "ShellTool: {} (rc={}, {})",
            command,
            result.returncode,
            "timed_out" if result.timed_out else "ok",
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
