"""Trusted lifecycle operations; repository and model text never selects argv."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.services.agent_team.capability_policy import AuditSink, CapabilitySession
from backend.services.agent_team.dependency_bootstrap import (
    sanitize_dependency_diagnostic,
)
from backend.services.agent_team.execution import (
    ExecutionRequest,
    execute_request,
    execution_workspace_key,
    resolve_execution_runner,
)
from backend.services.agent_team.tool_scheduler import workspace_barrier

HookEvent = Literal[
    "session_start",
    "before_model",
    "after_model",
    "before_tool",
    "after_tool",
    "before_write",
    "after_write",
    "before_finish",
    "after_finish",
    "task_failed",
    "task_cancelled",
]


class HookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    event: HookEvent
    kind: Literal["command", "audit"] = "command"
    argv: tuple[str, ...] = ()
    required: bool = True
    read_only: bool = False
    enabled: bool = True

    @field_validator("argv", mode="before")
    @classmethod
    def arguments(cls, value):
        if not isinstance(value, (list, tuple)):
            raise ValueError("hook_argv_must_be_array")
        return tuple(value)

    @model_validator(mode="after")
    def valid_command(self):
        if self.kind == "command":
            if not self.argv or self.read_only:
                raise ValueError("command_hooks_require_write_profile")
            for arg in self.argv:
                if (
                    not arg
                    or "\x00" in arg
                    or (("{" in arg or "}" in arg) and arg != "{workspace}")
                ):
                    raise ValueError("invalid_hook_argument")
        elif self.argv:
            raise ValueError("audit_hooks_do_not_execute")
        return self


class HookFailure(RuntimeError):
    """Sanitized, untrusted corrective evidence; never a system instruction."""


class LifecycleHooks:
    def __init__(
        self,
        load_hooks: Callable[[], Awaitable[tuple[HookConfig, ...]]],
        capabilities: CapabilitySession,
        audit: AuditSink,
        *,
        read_only=False,
    ):
        self.load_hooks = load_hooks
        self.capabilities = capabilities
        self.audit = audit
        self._read_only = read_only

    async def plan(self):
        try:
            hooks = await self.load_hooks()
            if not isinstance(hooks, tuple) or any(
                not isinstance(h, HookConfig) for h in hooks
            ):
                raise ValueError
            # System command hooks target the main implementation session.
            # Children record lifecycle/audit hooks, never inherit Shell authority.
            return tuple(
                h
                for h in hooks
                if h.enabled and (not self._read_only or h.kind == "audit")
            )
        except Exception:
            raise HookFailure("hook_configuration_unavailable") from None

    async def emit(
        self,
        event: HookEvent,
        ctx,
        *,
        status="",
        tool_call_id=None,
        plan=None,
        workspace_locked=False,
        audit_only=False,
    ):
        await self.audit(
            {
                "kind": "lifecycle",
                "event": event,
                "status": status,
                "tool_call_id": tool_call_id,
            }
        )
        current = await self.plan()
        hooks = tuple(h for h in current if h.event == event)
        if plan is not None and hooks != tuple(h for h in plan if h.event == event):
            raise HookFailure("hook_configuration_changed")
        failures = []
        for hook in hooks:
            if audit_only and hook.kind == "command":
                continue
            reason = evidence = ""
            try:
                if hook.kind == "command":
                    if workspace_locked:
                        result = await self._command(hook, ctx, tool_call_id)
                    else:
                        async with workspace_barrier(ctx.workspace).hold(False):
                            result = await self._command(hook, ctx, tool_call_id)
                    if (
                        result.exit_code != 0
                        or result.timed_out
                        or result.infrastructure_error
                    ):
                        reason = "hook_command_failed"
                        evidence = sanitize_dependency_diagnostic(
                            "\n".join((result.stdout or "", result.stderr or ""))
                        )
                else:
                    decision = await self.capabilities.check("hook", ())
                    if not decision.allowed:
                        raise HookFailure(decision.reason)
            except asyncio.CancelledError:
                await self.audit(
                    {
                        "kind": "hook",
                        "event": event,
                        "hook_id": hook.id,
                        "status": "cancelled",
                        "tool_call_id": tool_call_id,
                    }
                )
                raise
            except HookFailure as exc:
                reason = str(exc)
            except Exception:
                reason = "hook_execution_failed"
            await self.audit(
                {
                    "kind": "hook",
                    "event": event,
                    "hook_id": hook.id,
                    "status": "failed" if reason else "completed",
                    "reason": reason,
                    "tool_call_id": tool_call_id,
                }
            )
            if reason and hook.required:
                failures.append(
                    f"{hook.id}: {reason}"
                    + (f"\nUntrusted hook output:\n{evidence}" if evidence else "")
                )
        if failures:
            raise HookFailure("\n".join(failures))

    async def _command(self, hook, ctx, tool_call_id):
        decision = await self.capabilities.check(
            "hook", ("shell.execute", "filesystem.write"), read_only=False
        )
        if not decision.allowed:
            raise HookFailure(decision.reason)
        if hook not in await self.plan():
            raise HookFailure("hook_configuration_changed")
        if ctx.cancel_event and ctx.cancel_event.is_set():
            raise asyncio.CancelledError
        runner = resolve_execution_runner(
            ctx.execution_runner, ctx.workspace, ctx.workspace_service
        )
        workspace = str(Path(ctx.workspace).resolve(strict=True))
        execution_workspace = None
        if "{workspace}" in hook.argv:
            execution_workspace = getattr(runner, "execution_workspace", None)
            if not isinstance(execution_workspace, str) or not execution_workspace:
                raise HookFailure("hook_workspace_mapping_unavailable")
        argv = tuple(
            execution_workspace if arg == "{workspace}" else arg for arg in hook.argv
        )
        if ctx.cancel_event is None:
            ctx.cancel_event = asyncio.Event()
        request = ExecutionRequest(
            workspace_key=execution_workspace_key(workspace, ctx.workspace_service),
            argv=argv,
            cancel_event=ctx.cancel_event,
        )
        # Persist before effect. A readonly tool with an interrupted command hook
        # is a mutation during recovery, even if the hook is later disabled.
        effect_id = str(uuid4())
        await self.audit(
            {
                "kind": "hook_effect",
                "effect_id": effect_id,
                "event": hook.event,
                "hook_id": hook.id,
                "tool_call_id": tool_call_id,
                "effect": "workspace_write",
                "status": "admitted",
            }
        )
        operation = asyncio.create_task(execute_request(runner, request))
        try:
            result = await asyncio.shield(operation)
        except asyncio.CancelledError:
            ctx.cancel_event.set()
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            await asyncio.gather(operation, return_exceptions=True)
            raise
        if getattr(result, "cancelled", False):
            raise asyncio.CancelledError
        await self.audit(
            {
                "kind": "hook_effect",
                "effect_id": effect_id,
                "event": hook.event,
                "hook_id": hook.id,
                "tool_call_id": tool_call_id,
                "effect": "workspace_write",
                "status": "completed",
            }
        )
        return result
