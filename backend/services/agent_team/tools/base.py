"""Agent 工具基类与执行器

提供工具生命周期：schema 解析 → 输入校验 → 权限检查 → 执行 → 结果映射
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from loguru import logger

from backend.core.time_service import monotonic
from backend.services.agent_team.execution import ExecutionRunner
from backend.services.agent_team.skill_scope import SkillRestriction
from backend.services.agent_team.tools.errors import ToolExecutionError
from backend.services.agent_team.workspace_service import (
    AgentTeamWorkspaceService,
)

# ── 数据结构 ──────────────────────────────────────────

if TYPE_CHECKING:
    from backend.services.agent_team.repository_context import (
        RepositoryContext,
        RepositoryInstruction,
    )


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。"""

    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    # 稳定错误码（如 WORKSPACE_WRITE_PERMISSION_DENIED）；空串表示无结构化分类
    error_code: str = ""
    # Set by trusted executor admission, never inferred from tool output JSON.
    terminal_state: str = ""

    @property
    def is_terminal(self) -> bool:
        """Whether this result ends the Agent run."""
        return self.success and self.terminal_state == "success"


@dataclass(frozen=True)
class ToolMetadata:
    """Runtime-owned scheduling/recovery classification, absent from schemas."""

    read_only: bool = False
    parallel_safe: bool = False
    terminal: bool = False


# ── 工具上下文 ────────────────────────────────────────


@dataclass
class ToolContext:
    """工具执行上下文，贯穿一次工具调用的全生命周期。"""

    workspace: str
    workspace_service: AgentTeamWorkspaceService
    # Agent 外部命令必须经由 worker 注入的 workspace-scoped 执行器；None
    # 只用于文件类工具或显式测试，任何外部命令工具都会 fail closed。
    execution_runner: ExecutionRunner | None = None
    # Worker cancellation is propagated to the current sandbox request.  This
    # is an internal object reference and never part of the wire payload.
    cancel_event: asyncio.Event | None = field(default=None, repr=False)
    # 文件读状态缓存：path → {content, mtime}
    read_file_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    # 写操作追踪：记录被修改的文件路径（相对于 workspace）
    modified_files: set[str] = field(default_factory=set)
    repository_context: RepositoryContext | None = field(default=None, repr=False)
    repository_instructions: dict[str, RepositoryInstruction] = field(
        default_factory=dict, repr=False
    )
    active_skill_tools: dict[str, SkillRestriction] = field(default_factory=dict)
    skills_enabled: bool = True
    repository_targets: tuple[str, ...] = ()
    repository_whole_scope: bool = False
    pending_repository_snapshot: dict[str, RepositoryInstruction] | None = field(
        default=None, repr=False
    )
    pending_repository_targets: tuple[str, ...] = ()
    pending_repository_whole_scope: bool = False

    def allows_skill_tool(self, name: str, args: dict[str, Any] | None = None) -> bool:
        """Workflow restrictions intersect the existing runtime ceiling."""
        return name in {"use_skill", "finish_task"} or all(
            scope.allows(name, args) for scope in self.active_skill_tools.values()
        )

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

    def runtime_metadata(self) -> ToolMetadata:
        read_only = self.is_read_only()
        return ToolMetadata(read_only=read_only, parallel_safe=read_only)

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

    def metadata(self, name: str) -> ToolMetadata:
        tool = self.get_tool(name)
        return tool.runtime_metadata() if tool else ToolMetadata()

    def all_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def get_schemas(self) -> list[dict[str, Any]]:
        """获取所有注册工具的 schema（用于 function calling）。"""
        return [t.get_schema() for t in self._tools.values()]

    async def execute_tool_call(self, tool_call: Any, ctx: ToolContext) -> ToolResult:
        from backend.services.agent_team.tool_scheduler import workspace_barrier

        metadata = self.metadata(tool_call.function.name)
        barrier = workspace_barrier(ctx.workspace)
        async with barrier.hold(metadata.parallel_safe):
            if ctx.cancel_event and ctx.cancel_event.is_set():
                raise asyncio.CancelledError
            if metadata.parallel_safe:
                return await self._execute_tool_call(tool_call, ctx)
            # A cancelled asyncio.to_thread await does not stop the underlying
            # write. Keep the exclusive barrier until the admitted operation
            # actually finishes; runner cancellation travels through its event.
            if ctx.cancel_event is None:
                ctx.cancel_event = asyncio.Event()
            operation = asyncio.create_task(self._execute_tool_call(tool_call, ctx))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                ctx.cancel_event.set()
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                # Retrieve failures even when cancellation won the race.
                await asyncio.gather(operation, return_exceptions=True)
                raise

    async def _execute_tool_call(self, tool_call: Any, ctx: ToolContext) -> ToolResult:
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
        if not isinstance(arguments, dict):
            return ToolResult(success=False, error="工具参数必须是对象")

        if not ctx.allows_skill_tool(function_name, arguments):
            return ToolResult(
                False,
                error=f"Active Skill does not allow tool: {function_name}",
                error_code="SKILL_TOOL_RESTRICTED",
            )
        if ctx.repository_context:
            try:
                docs = await self.repository_requirements(
                    [tool_call], ctx, preserve_batch=True
                )
            except ValueError as exc:
                return ToolResult(
                    False, error=str(exc), error_code="REPOSITORY_CONTEXT_REJECTED"
                )
            if ctx.pending_repository_snapshot is not None:
                return ToolResult(
                    False,
                    output={"repository_context": ctx.repository_context.render(docs)},
                    error="Receive applicable repository instructions before retrying this tool",
                    error_code="REPOSITORY_CONTEXT_REQUIRED",
                )

        # 3. 输入校验
        validation_error = tool.validate_input(arguments, ctx)
        if validation_error:
            return ToolResult(success=False, error=validation_error)

        # 4. 执行
        try:
            result = await tool.execute(arguments, ctx)
        except ToolExecutionError as exc:
            logger.error("工具 {} 执行失败[{}]: {}", function_name, exc.error_code, exc)
            result = ToolResult(
                success=False,
                error=str(exc),
                error_code=exc.error_code,
            )
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

        from backend.services.agent_team.tools.finish_task_tool import FinishTaskTool

        terminal = type(tool) is FinishTaskTool and result.success
        output = dict(result.output)
        if not terminal:
            output.pop("_terminal", None)
        return replace(
            result, output=output, terminal_state="success" if terminal else ""
        )

    async def repository_requirements(
        self, calls: list[Any], ctx: ToolContext, *, preserve_batch: bool = False
    ) -> list[RepositoryInstruction]:
        """Preflight a whole model batch before admitting workspace effects."""
        repository = ctx.repository_context
        if repository is None:
            return []

        def discover():
            targets = set(ctx.repository_targets) if preserve_batch else set()
            whole = ctx.repository_whole_scope if preserve_batch else False
            repository.diagnostics.clear()
            for call in calls:
                try:
                    args = json.loads(call.function.arguments)
                except ValueError, TypeError:
                    continue
                if not isinstance(args, dict):
                    continue
                name = call.function.name
                targets.update(
                    args[key]
                    for key in ("file_path", "path", "directory")
                    if isinstance(args.get(key), str)
                )
                for key in ("file_paths", "modified_files"):
                    if isinstance(args.get(key), list):
                        targets.update(
                            item for item in args[key] if isinstance(item, str)
                        )
                if name == "use_skill":
                    entry = ctx.extra.get("skills_index", {}).get(
                        str(args.get("slug") or ""), {}
                    )
                    if entry.get("source_type") == "repository":
                        main = repository.relative(entry["install_path"])
                        targets.add(
                            str(main.parent / str(args.get("file") or "SKILL.md"))
                        )
                if name == "run_command":
                    whole = True
            current_targets = tuple(sorted(targets))
            docs = repository.snapshot(current_targets, whole=whole)
            current = {doc.path: doc for doc in docs}
            ctx.pending_repository_targets = current_targets
            ctx.pending_repository_whole_scope = whole
            # Includes deletions and complete scope replacement, not only new docs.
            ctx.pending_repository_snapshot = (
                current if current != ctx.repository_instructions else None
            )
            return docs if ctx.pending_repository_snapshot is not None else []

        return await asyncio.to_thread(discover)

    async def execute_raw(
        self, tool_name: str, arguments: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """直接以字典形式调用工具（用于测试）。"""
        call = SimpleNamespace(
            function=SimpleNamespace(name=tool_name, arguments=json.dumps(arguments))
        )
        return await self.execute_tool_call(call, ctx)
