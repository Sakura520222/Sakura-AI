"""Bounded read batches and workspace-wide, writer-preferring barriers.

Locks are shared by executors on the same worker event loop. They do not provide
exclusion across worker processes; no distributed workspace lease is implemented.
No schema field or model argument participates in scheduling decisions.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary, WeakValueDictionary

if TYPE_CHECKING:
    from backend.services.agent_team.tools.base import (
        ToolContext,
        ToolExecutor,
        ToolResult,
    )


class WorkspaceBarrier:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0

    @asynccontextmanager
    async def hold(self, shared: bool, limit: int) -> AsyncIterator[None]:
        async with self.condition:
            if shared:
                await self.condition.wait_for(
                    lambda: (
                        not self.writer
                        and not self.waiting_writers
                        and self.readers < limit
                    )
                )
                self.readers += 1
            else:
                self.waiting_writers += 1
                try:
                    await self.condition.wait_for(
                        lambda: not self.writer and not self.readers
                    )
                    self.writer = True
                finally:
                    self.waiting_writers -= 1
                    self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                if shared:
                    self.readers -= 1
                else:
                    self.writer = False
                self.condition.notify_all()


_barriers: WeakKeyDictionary = WeakKeyDictionary()


def workspace_barrier(workspace: str) -> WorkspaceBarrier:
    loop = asyncio.get_running_loop()
    barriers = _barriers.setdefault(loop, WeakValueDictionary())
    key = str(Path(workspace).resolve())
    barrier = barriers.get(key)
    if barrier is None:
        barrier = WorkspaceBarrier()
        barriers[key] = barrier
    return barrier


async def run_tool_batch(
    calls: list[Any],
    executor: ToolExecutor,
    ctx: ToolContext,
    *,
    before: Callable[[Any], Awaitable[None]],
    after: Callable[[Any, ToolResult, str], Awaitable[None]],
    cancelled: Callable[[Any], Awaitable[None]],
) -> dict[str, Any] | None:
    """Execute contiguous read groups; checkpoint results serially in model order.

    Every running marker is durable before any corresponding side effect. All
    child tasks are drained on cancellation or failure, including checkpoint
    failures. Failed/cancelled writes remain ambiguous until reconciliation.
    """
    from backend.services.agent_team.tools.base import ToolResult

    terminal = None
    offset = 0
    while offset < len(calls):
        if terminal is not None:
            for call in calls[offset:]:
                await after(
                    call,
                    ToolResult(
                        False,
                        error="Skipped after successful finish_task",
                        error_code="CANCELLED_AFTER_FINISH",
                    ),
                    "cancelled",
                )
            break
        group = [calls[offset]]
        if executor.metadata(calls[offset].function.name).parallel_safe:
            while (
                offset + len(group) < len(calls) and len(group) < ctx.max_parallel_reads
            ):
                candidate = calls[offset + len(group)]
                if not executor.metadata(candidate.function.name).parallel_safe:
                    break
                group.append(candidate)
        marked = []
        tasks = []
        persisted = set()
        watcher = None
        try:
            for call in group:
                if ctx.cancel_event and ctx.cancel_event.is_set():
                    raise asyncio.CancelledError
                await before(call)
                marked.append(call)
            tasks = [
                asyncio.create_task(executor.execute_tool_call(call, ctx))
                for call in group
            ]
            if ctx.cancel_event:
                watcher = asyncio.create_task(ctx.cancel_event.wait())
                pending = set(tasks)
                while pending:
                    done, _ = await asyncio.wait(
                        pending | {watcher}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if watcher in done:
                        raise asyncio.CancelledError
                    pending -= done
            results = await asyncio.gather(*tasks)
            for call, result in zip(group, results, strict=True):
                await after(call, result, "completed" if result.success else "failed")
                persisted.add(call.id)
                if result.is_terminal:
                    terminal = result.output
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for call in marked:
                if call.id not in persisted:
                    await cancelled(call)
            raise
        finally:
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
        offset += len(group)
    return terminal
