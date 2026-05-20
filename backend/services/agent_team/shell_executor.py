"""Agent 专家团队受控 Shell 执行器"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from backend.services.agent_team.workspace_service import (
    AgentTeamWorkspaceService,
    WorkspaceSecurityError,
)

_WINDOWS_ABS_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s'\"`;&|]*")
_POSIX_ABS_RE = re.compile(r"(?<![\w.-])/(?:[^\s'\"`;&|]+)")
_FORBIDDEN_TOKENS = (
    "..",
    "~",
    "$HOME",
    "${HOME}",
    "%USERPROFILE%",
    "%HOMEPATH%",
    "$env:USERPROFILE",
    "$env:HOMEPATH",
)


@dataclass(frozen=True)
class ShellCommandResult:
    """Shell 命令执行结果。"""

    command: str
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class AgentTeamShellExecutor:
    """在仓库工作区中执行 Shell 命令。

    该执行器使用项目部署后的当前 Python 虚拟环境，并将工作目录限制在
    ./workplace/<owner>/<repo>/ 内。它不是 OS 级沙箱，但会阻止常见的
    路径穿越、宿主机绝对路径访问和用户家目录访问。
    """

    def __init__(
        self,
        workspace: str | Path,
        workspace_service: AgentTeamWorkspaceService | None = None,
    ):
        self.workspace_service = workspace_service or AgentTeamWorkspaceService()
        self.workspace = self.workspace_service.resolve_inside_workspace(workspace)

    async def run(
        self,
        command: str,
        cwd: str | Path = ".",
        timeout_seconds: int = 600,
    ) -> ShellCommandResult:
        """在工作区内执行 Shell 命令。"""
        safe_cwd = self.workspace_service.resolve_inside_workspace(self.workspace, cwd)
        self._validate_command(command)
        env = self._build_env()

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(safe_cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
            return ShellCommandResult(
                command=command,
                cwd=str(safe_cwd),
                returncode=process.returncode or 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            return ShellCommandResult(
                command=command,
                cwd=str(safe_cwd),
                returncode=-1,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                timed_out=True,
            )

    async def run_args(
        self,
        args: Sequence[str],
        cwd: str | Path = ".",
        timeout_seconds: int = 600,
    ) -> ShellCommandResult:
        """以 argv 形式在工作区内执行命令，优先用于 Git 等结构化命令。"""
        if not args:
            raise WorkspaceSecurityError("Shell 命令不能为空")
        safe_cwd = self.workspace_service.resolve_inside_workspace(self.workspace, cwd)
        for arg in args:
            self._validate_command_arg(arg)
        env = self._build_env()

        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(safe_cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        command_display = " ".join(self._mask_sensitive_arg(arg) for arg in args)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
            return ShellCommandResult(
                command=command_display,
                cwd=str(safe_cwd),
                returncode=process.returncode or 0,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            return ShellCommandResult(
                command=command_display,
                cwd=str(safe_cwd),
                returncode=-1,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                timed_out=True,
            )

    def _validate_command(self, command: str) -> None:
        if not command or not command.strip():
            raise WorkspaceSecurityError("Shell 命令不能为空")
        lowered = command.lower()
        for token in _FORBIDDEN_TOKENS:
            if token.lower() in lowered:
                raise WorkspaceSecurityError(f"Shell 命令包含禁止的路径片段: {token}")

        for match in _WINDOWS_ABS_RE.finditer(command):
            self.workspace_service.resolve_inside_workspace(
                self.workspace, match.group(0)
            )
        for match in _POSIX_ABS_RE.finditer(command):
            path = match.group(0)
            # /dev/null, /dev/stdin 等是标准 Unix 设备，非文件系统路径，放行
            if path.startswith("/dev/"):
                continue
            # Git Bash 风格的 /c/... 路径也按绝对路径处理；常见命令参数如 /? 会被拒绝，
            # 这是为了优先保证不能引用宿主机绝对路径。
            self.workspace_service.resolve_inside_workspace(
                self.workspace, path
            )

    def _validate_command_arg(self, arg: str) -> None:
        if not arg:
            raise WorkspaceSecurityError("Shell 命令参数不能为空")
        if self._is_url(arg):
            return
        lowered = arg.lower()
        for token in _FORBIDDEN_TOKENS:
            if token.lower() in lowered:
                raise WorkspaceSecurityError(f"Shell 命令包含禁止的路径片段: {token}")
        for match in _WINDOWS_ABS_RE.finditer(arg):
            self.workspace_service.resolve_inside_workspace(
                self.workspace, match.group(0)
            )
        for match in _POSIX_ABS_RE.finditer(arg):
            self.workspace_service.resolve_inside_workspace(
                self.workspace, match.group(0)
            )

    def _is_url(self, value: str) -> bool:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https", "ssh", "git"} and bool(parsed.netloc)

    def _mask_sensitive_arg(self, value: str) -> str:
        if "x-access-token:" in value:
            return re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", value)
        return value

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        workspace_venv = self.workspace / ".venv"
        if workspace_venv.exists():
            venv_root = workspace_venv.resolve()
            script_dir = venv_root / ("Scripts" if os.name == "nt" else "bin")
            env["VIRTUAL_ENV"] = str(venv_root)
            env["PATH"] = str(script_dir) + os.pathsep + env.get("PATH", "")
        else:
            venv_root = Path(sys.prefix).resolve()
            script_dir = venv_root / ("Scripts" if os.name == "nt" else "bin")
            env["VIRTUAL_ENV"] = str(venv_root)
            env["PATH"] = str(script_dir) + os.pathsep + env.get("PATH", "")
        env["SAKURA_AGENT_WORKSPACE"] = str(self.workspace)
        return env
