"""Read batches and workspace-wide, writer-preferring barriers.

An event-loop barrier provides writer preference, and a native advisory lock
coordinates processes using the same workspace filesystem identity.
This is not a distributed task/session lease. No schema field or model argument
participates in scheduling decisions.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary, WeakValueDictionary

try:
    import fcntl
except ImportError:
    fcntl = None

if os.name == "nt":
    from backend.services.agent_team import windows_filesystem
else:
    windows_filesystem = None

if TYPE_CHECKING:
    from backend.services.agent_team.tools.base import (
        ToolContext,
        ToolExecutor,
        ToolResult,
    )


class WorkspaceBarrier:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.condition = asyncio.Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0

    @asynccontextmanager
    async def hold(self, shared: bool) -> AsyncIterator[None]:
        supported = windows_filesystem is not None if os.name == "nt" else (
            fcntl is not None
            and all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW"))
        )
        if not supported:
            raise RuntimeError(
                "workspace_lock_unavailable: Agent workspace operations require "
                "native directory-lock support on the backend host"
            )
        async with self.condition:
            if shared:
                await self.condition.wait_for(
                    lambda: not self.writer and not self.waiting_writers
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
        directory_fd = None
        directory_handle = None
        try:
            # POSIX locks the admitted directory; Windows pins its identity and
            # locks an external identity file. No lock lives in repository content.
            # Nonblocking acquisition keeps cancellation responsive without an
            # execution budget or a blocking worker thread left behind.
            if os.name != "nt":
                directory_fd = os.open(
                    self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
                operation = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
            while True:
                try:
                    if os.name == "nt":
                        directory_handle = windows_filesystem.acquire_workspace_lock(
                            self.workspace, shared=shared
                        )
                    else:
                        fcntl.flock(directory_fd, operation)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.01)
            yield
        finally:
            try:
                if directory_fd is not None:
                    os.close(directory_fd)
                if directory_handle is not None:
                    directory_handle.close()
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
    key = os.path.normcase(str(Path(workspace).resolve()))
    barrier = barriers.get(key)
    if barrier is None:
        barrier = WorkspaceBarrier(key)
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
            while offset + len(group) < len(calls):
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
