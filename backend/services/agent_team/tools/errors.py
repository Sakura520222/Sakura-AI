"""工具结构化错误 / Structured errors for agent tools.

工具失败时向模型暴露稳定 error_code，便于直接换策略或终止，
避免模型退化成"Linux 权限诊断工程师"。/ Stable error codes let the
model switch strategy instead of probing the filesystem.
"""

from __future__ import annotations


class ToolExecutionError(Exception):
    """携带稳定 error_code 的工具执行错误 / Tool failure with a stable code."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


WORKSPACE_WRITE_PERMISSION_DENIED = "WORKSPACE_WRITE_PERMISSION_DENIED"
