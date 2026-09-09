"""Agent Team 本地执行器兼容 facade。

实际执行合同和唯一 subprocess 边界位于 :mod:`execution`。保留
``AgentTeamShellExecutor`` 名称是为了兼容旧调用方；新代码应依赖
``LocalExecutionRunner`` 或注入 ``ExecutionRunner``。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.services.agent_team.execution import (
    ExecutionProfile,
    ExecutionRequest,
    ExecutionResult,
    ExecutionRunner,
    LocalExecutionRunner,
    TrustedGitRunner,
    UnsupportedExecutionProfile,
)


@dataclass(frozen=True)
class ShellCommandResult:
    """旧调用方使用的结果结构。"""

    command: str
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def exit_code(self) -> int:
        return self.returncode


class AgentTeamShellExecutor(LocalExecutionRunner):
    """旧名称兼容层，行为由 ``LocalExecutionRunner`` 提供。"""


__all__ = [
    "AgentTeamShellExecutor",
    "ExecutionProfile",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionRunner",
    "LocalExecutionRunner",
    "ShellCommandResult",
    "TrustedGitRunner",
    "UnsupportedExecutionProfile",
]
