"""Agent 工具基类与执行器

提供工具生命周期：schema 解析 → 输入校验 → 权限检查 → 执行 → 结果映射
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from backend.core.time_service import monotonic
from backend.services.agent_team.workspace_service import (
    AgentTeamWorkspaceService,
)

# ── 数据结构 ──────────────────────────────────────────


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。"""

    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def is_terminal(self) -> bool:
        """是否为终止工具（finish_task / submit_review）。"""
        return bool(self.output.get("_terminal"))


# ── 工具上下文 ────────────────────────────────────────


@dataclass
class ToolContext:
    """工具执行上下文，贯穿一次工具调用的全生命周期。"""

    workspace: str
    workspace_service: AgentTeamWorkspaceService
    # 文件读状态缓存：path → {content, mtime}
    read_file_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    # 写操作追踪：记录被修改的文件路径（相对于 workspace）
    modified_files: set[str] = field(default_factory=set)

    def track_modified_file(self, file_path: str) -> None:
        """记录被修改的文件路径。"""
        ws = str(Path(self.workspace).resolve())
        resolved = str(Path(file_path).resolve())
        if resolved.startswith(ws):
            rel = os.path.relpath(resolved, ws).replace("\\", "/")
        else:
            rel = file_path.replace("\\", "/")
        self.modified_files.add(rel)


# ── 工具协议 ──────────────────────────────────────────


@runtime_checkable
class ToolProtocol(Protocol):
    """工具协议，定义工具的完整生命周期。"""

    name: str

    def description(self) -> str:
        """工具描述，供模型参考。"""
        ...

    def is_read_only(self) -> bool:
        """是否只读工具。"""
        ...

    def get_schema(self) -> dict[str, Any]:
        """返回 OpenAI function calling 格式的工具 schema。"""
        ...

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        """校验输入参数，返回错误信息或 None（通过）。"""
        ...

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具核心逻辑。"""
        ...


# ── 基础工具 ──────────────────────────────────────────


class BaseTool:
    """工具基类，提供默认实现。"""

    name: str = ""
    _schema: dict[str, Any] = {}

    def description(self) -> str:
        return self.name

    def is_read_only(self) -> bool:
        return False

    def get_schema(self) -> dict[str, Any]:
        return self._schema

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        """默认校验：检查 schema 中的 required 字段。"""
        params = self._schema.get("function", {}).get("parameters", {})
        required = params.get("required", [])
        for key in required:
            if key not in args or args[key] is None:
                return f"缺少必要参数: {key}"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError(f"工具 {self.name} 未实现 execute 方法")


# ── 工具执行器 ────────────────────────────────────────


class ToolExecutor:
    """统一工具执行器，管理工具生命周期。"""

    def __init__(self, tools: list[BaseTool] | None = None):
        self._tools: dict[str, BaseTool] = {}
        if tools:
            for tool in tools:
                self.register(tool)

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def all_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def get_schemas(self) -> list[dict[str, Any]]:
        """获取所有注册工具的 schema（用于 function calling）。"""
        return [t.get_schema() for t in self._tools.values()]

    async def execute_tool_call(self, tool_call: Any, ctx: ToolContext) -> ToolResult:
        """执行单个工具调用，完整的生命周期管理。"""
        function_name = tool_call.function.name
        start_time = monotonic()

        # 1. 查找工具
        tool = self._tools.get(function_name)
        if not tool:
            return ToolResult(success=False, error=f"未知工具: {function_name}")

        # 2. 解析参数
        try:
            arguments = json.loads(tool_call.function.arguments)
        except (json.JSONDecodeError, TypeError) as exc:
            return ToolResult(
                success=False,
                error=f"无法解析工具参数: {exc}",
            )

        # 3. 输入校验
        validation_error = tool.validate_input(arguments, ctx)
        if validation_error:
            return ToolResult(success=False, error=validation_error)

        # 4. 执行
        try:
            result = await tool.execute(arguments, ctx)
        except Exception as exc:
            logger.error("工具 {} 执行异常: {}", function_name, exc)
            result = ToolResult(
                success=False,
                error=f"工具执行失败: {type(exc).__name__}: {exc}",
            )

        # 5. 日志与耗时
        duration_ms = int((monotonic() - start_time) * 1000)
        status = "成功" if result.success else f"失败({result.error[:50]})"
        logger.debug("工具 {} {} ({}ms)", function_name, status, duration_ms)

        # 6. 追踪修改的文件（写操作工具成功时自动记录）
        if result.success and not tool.is_read_only():
            tracked = result.output.get("_modified_file")
            if tracked and isinstance(tracked, str):
                ctx.track_modified_file(tracked)

        return result

    async def execute_raw(
        self, tool_name: str, arguments: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """直接以字典形式调用工具（用于测试）。"""
        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(success=False, error=f"未知工具: {tool_name}")

        validation_error = tool.validate_input(arguments, ctx)
        if validation_error:
            return ToolResult(success=False, error=validation_error)

        try:
            return await tool.execute(arguments, ctx)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"工具执行失败: {type(exc).__name__}: {exc}",
            )
