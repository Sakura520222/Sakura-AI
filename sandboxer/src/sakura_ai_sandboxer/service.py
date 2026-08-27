"""Request admission, bounded runtime lifecycle and replay protection."""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, TypeVar

from .config import SandboxdConfig
from .errors import (
    CleanupFailedError,
    ConcurrencyLimitError,
    DaemonShuttingDownError,
    InvalidRequestError,
    PolicyDeniedError,
    RequestConflictError,
)
from .models import (
    REQUEST_ID_PATTERN,
    CancelData,
    ExecutionData,
    ExecutionRequest,
    HealthData,
    NetworkMode,
)
from .runtime import RuntimeAdapter, RuntimeResult, UnavailableRuntimeAdapter

_REQUEST_ID_RE = re.compile(REQUEST_ID_PATTERN)
_T = TypeVar("_T")


class _DeadlineExceeded(Exception):
    """Internal signal that a cooperative await missed its hard deadline."""


@dataclass(slots=True)
class _LedgerEntry:
    state: str
    expires_at: float | None
    result: ExecutionData | None = None


@dataclass(slots=True)
class _RequestRecord:
    request: ExecutionRequest
    cancel_event: asyncio.Event
    task: asyncio.Task[ExecutionData] | None = None
    cancel_requested: bool = False


class SandboxExecutionService:
    """Own active requests and guarantee bounded cleanup on every path.

    ``_ledger`` is separate from ``_records``.  Active records provide
    cancellation handles; terminal entries prevent replay after the active
    record is removed.  A cancel arriving first creates a tombstone, so a
    later POST with the same request ID cannot execute.
    """

    def __init__(
        self,
        config: SandboxdConfig | None = None,
        runtime: RuntimeAdapter | None = None,
    ) -> None:
        self.config = config or SandboxdConfig()
        self.runtime: RuntimeAdapter = runtime or UnavailableRuntimeAdapter()
        self._records: dict[str, _RequestRecord] = {}
        self._ledger: OrderedDict[str, _LedgerEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._shutting_down = False
        # A Docker adapter is not ready merely because it was constructed.
        # ``create_app`` marks it ready only after bounded orphan recovery has
        # completed successfully.  Fake/unavailable adapters remain
        # intentionally not-ready for fail-closed health checks.
        self._runtime_ready = False

    @property
    def active_count(self) -> int:
        return len(self._records)

    @property
    def ledger_count(self) -> int:
        return len(self._ledger)

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    def mark_runtime_ready(self) -> None:
        """Record successful Docker startup/recovery for the health gate."""

        self._runtime_ready = self.runtime.name == "docker"

    def health(self) -> HealthData:
        return HealthData(
            ready=self._runtime_ready and not self._shutting_down,
            runtime=self.runtime.name,
            profiles=["agent", "dependency"],
            # Health advertises a capability, never the deployment-owned
            # Docker network name.
            egress_capability=(
                "egress" if self.config.egress_network != "none" else "none"
            ),
            instance_id=self.config.instance_id or "",
            workspace_root=self.config.workspace_root or "",
            runner_image_digest=self.config.runner_image_digest or "",
        )

    async def execute(self, request: ExecutionRequest) -> ExecutionData:
        """Atomically admit one request and await its one-shot task."""

        if (
            request.network_mode is NetworkMode.EGRESS
            and self.config.egress_network == "none"
        ):
            raise PolicyDeniedError(
                "egress network capability is unavailable in this sandboxd deployment"
            )

        async with self._lock:
            self._prune_ledger_locked()
            if self._shutting_down:
                raise DaemonShuttingDownError()
            if request.request_id in self._ledger:
                raise RequestConflictError("request id has already been used")
            if len(self._records) >= self.config.max_concurrent:
                raise ConcurrencyLimitError()
            self._reserve_ledger_slot_locked(request.request_id)
            record = _RequestRecord(request, asyncio.Event())
            try:
                task = asyncio.create_task(
                    self._run_record(record),
                    name=f"sandbox-execution-{request.request_id}",
                )
            except BaseException:
                self._ledger.pop(request.request_id, None)
                raise
            record.task = task
            self._records[request.request_id] = record
            self._ledger[request.request_id] = _LedgerEntry("active", None)

        try:
            result = await task
        except asyncio.CancelledError:
            await self._mark_terminal(record, "cancelled", None)
            raise
        except Exception:
            # A terminal infrastructure/runtime failure is still a consumed
            # request ID; callers must not replay it under the same ID.
            await self._mark_terminal(
                record,
                "cancelled" if record.cancel_requested else "completed",
                None,
            )
            raise
        else:
            await self._mark_terminal(
                record,
                "cancelled" if record.cancel_requested or result.cancelled else "completed",
                result,
            )
            return result
        finally:
            async with self._lock:
                if self._records.get(request.request_id) is record:
                    self._records.pop(request.request_id, None)

    async def cancel(self, request_id: str) -> CancelData:
        """Cancel active work or create an idempotent pre-admission tombstone."""

        if not _REQUEST_ID_RE.fullmatch(request_id or ""):
            raise InvalidRequestError("request id is invalid")
        async with self._lock:
            self._prune_ledger_locked()
            record = self._records.get(request_id)
            entry = self._ledger.get(request_id)
            if record is None:
                if entry is not None:
                    if entry.state == "cancelled":
                        return CancelData(
                            request_id=request_id,
                            cancelled=True,
                            state="cancelled",
                        )
                    return CancelData(
                        request_id=request_id,
                        cancelled=False,
                        state=entry.state,
                    )
                self._reserve_ledger_slot_locked(request_id)
                self._ledger[request_id] = _LedgerEntry(
                    "cancelled",
                    time.monotonic() + self.config.request_ledger_ttl_seconds,
                )
                return CancelData(
                    request_id=request_id,
                    cancelled=True,
                    # The tombstone is an internal admission state.  The
                    # public cancel result remains stable across retries.
                    state="cancelled",
                )
            record.cancel_requested = True
            record.cancel_event.set()
            task = record.task

        cancel_deadline = self._deadline_after(self.config.cleanup_margin_seconds)
        await self._cancel_runtime(request_id, cancel_deadline)
        if task is not None and not task.done():
            try:
                await self._wait_task_until(task, cancel_deadline, "execution cancel")
            except _DeadlineExceeded as exc:
                raise CleanupFailedError("cancelled execution did not drain") from exc
        return CancelData(request_id=request_id, cancelled=True, state="cancelled")

    async def shutdown(self) -> None:
        """Stop intake and drain every runtime call under one absolute deadline."""

        shutdown_deadline = self._deadline_after(self.config.shutdown_timeout_seconds)
        async with self._lock:
            self._shutting_down = True
            records = tuple(self._records.values())
            for record in records:
                record.cancel_requested = True
                record.cancel_event.set()

        cleanup_error: CleanupFailedError | None = None
        for record in records:
            try:
                await self._cancel_runtime(
                    record.request.request_id,
                    min(
                        shutdown_deadline,
                        self._deadline_after(self.config.cleanup_margin_seconds),
                    ),
                )
            except CleanupFailedError as exc:
                cleanup_error = cleanup_error or exc

        for record in records:
            task = record.task
            if task is None or task.done():
                continue
            try:
                await self._wait_task_until(task, shutdown_deadline, "execution shutdown")
            except _DeadlineExceeded:
                cleanup_error = cleanup_error or CleanupFailedError(
                    "execution shutdown did not drain"
                )

        try:
            await self._runtime_shutdown(shutdown_deadline)
        except CleanupFailedError as exc:
            cleanup_error = cleanup_error or exc

        async with self._lock:
            for record in records:
                if record.request.request_id in self._ledger:
                    self._ledger[record.request.request_id] = _LedgerEntry(
                        "cancelled",
                        time.monotonic() + self.config.request_ledger_ttl_seconds,
                    )
                self._records.pop(record.request.request_id, None)
        if cleanup_error is not None:
            raise cleanup_error

    async def _run_record(self, record: _RequestRecord) -> ExecutionData:
        request = record.request
        loop = asyncio.get_running_loop()
        effective_timeout = min(
            float(request.timeout_seconds),
            float(self.config.timeout_seconds),
            float(self.config.max_timeout_seconds),
        )
        execution_deadline = loop.time() + effective_timeout
        runtime_task = asyncio.create_task(
            self.runtime.execute(
                request,
                cancel_event=record.cancel_event,
                max_output_bytes=self.config.max_output_bytes,
                deadline=execution_deadline,
            ),
            name=f"sandbox-runtime-{request.request_id}",
        )
        try:
            result = await self._wait_task_until(
                runtime_task,
                execution_deadline,
                "runtime execute",
            )
        except _DeadlineExceeded as exc:
            record.cancel_event.set()
            try:
                await self._cancel_runtime(
                    request.request_id,
                    self._deadline_after(self.config.cleanup_margin_seconds),
                )
            except CleanupFailedError as cleanup_exc:
                raise cleanup_exc from exc
            return ExecutionData(
                request_id=request.request_id,
                exit_code=None,
                timed_out=True,
            )
        except asyncio.CancelledError:
            record.cancel_event.set()
            try:
                await self._cancel_runtime(
                    request.request_id,
                    self._deadline_after(self.config.cleanup_margin_seconds),
                )
            except Exception:
                pass
            raise

        if record.cancel_requested and not result.cancelled:
            result = RuntimeResult(
                exit_code=None,
                stdout=result.stdout,
                stderr=result.stderr,
                cancelled=True,
                output_truncated=result.output_truncated,
            )
        stdout, stderr, truncated = _bound_output(
            result.stdout,
            result.stderr,
            self.config.max_output_bytes,
        )
        return ExecutionData(
            request_id=request.request_id,
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            output_truncated=result.output_truncated or truncated,
        )

    async def _cancel_runtime(self, request_id: str, deadline: float) -> None:
        task = asyncio.create_task(
            self.runtime.cancel(request_id, deadline=deadline),
            name=f"sandbox-runtime-cancel-{request_id}",
        )
        try:
            await self._wait_task_until(task, deadline, "runtime cancel")
        except _DeadlineExceeded as exc:
            raise CleanupFailedError("runtime cancellation exceeded its deadline") from exc
        except CleanupFailedError:
            raise
        except Exception as exc:
            raise CleanupFailedError(f"runtime cancellation failed: {exc}") from exc

    async def _runtime_shutdown(self, deadline: float) -> None:
        task = asyncio.create_task(
            self.runtime.shutdown(deadline=deadline),
            name="sandbox-runtime-shutdown",
        )
        try:
            await self._wait_task_until(task, deadline, "runtime shutdown")
        except _DeadlineExceeded as exc:
            raise CleanupFailedError("runtime shutdown exceeded its deadline") from exc
        except CleanupFailedError:
            raise
        except Exception as exc:
            raise CleanupFailedError(f"runtime shutdown failed: {exc}") from exc

    async def _wait_task_until(
        self,
        task: asyncio.Task[_T],
        deadline: float,
        label: str,
    ) -> _T:
        """Wait until an absolute deadline; never await cancellation indefinitely."""

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            task.cancel()
            _detach_task(task)
            raise _DeadlineExceeded(label)
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            task.cancel()
            _detach_task(task)
            raise
        if not done:
            task.cancel()
            _detach_task(task)
            raise _DeadlineExceeded(label)
        try:
            return task.result()
        except asyncio.CancelledError as exc:
            raise CleanupFailedError(f"{label} was cancelled") from exc

    async def _mark_terminal(
        self,
        record: _RequestRecord,
        state: str,
        result: ExecutionData | None,
    ) -> None:
        async with self._lock:
            entry = self._ledger.get(record.request.request_id)
            if entry is None or entry.state != "active":
                return
            self._ledger[record.request.request_id] = _LedgerEntry(
                state,
                time.monotonic() + self.config.request_ledger_ttl_seconds,
                result,
            )

    def _reserve_ledger_slot_locked(self, request_id: str) -> None:
        self._prune_ledger_locked()
        if len(self._ledger) >= self.config.request_ledger_capacity:
            raise ConcurrencyLimitError("request ledger capacity reached")
        if request_id in self._ledger:
            raise RequestConflictError("request id has already been used")

    def _prune_ledger_locked(self) -> None:
        now = time.monotonic()
        expired = [
            request_id
            for request_id, entry in self._ledger.items()
            if entry.state != "active"
            and entry.expires_at is not None
            and entry.expires_at <= now
        ]
        for request_id in expired:
            self._ledger.pop(request_id, None)

    def _deadline_after(self, seconds: float) -> float:
        return asyncio.get_running_loop().time() + max(seconds, 0.0)


def _detach_task(task: asyncio.Task[Any]) -> None:
    """Consume a non-cooperative task's eventual result without blocking."""

    if task.done():
        try:
            task.result()
        except BaseException:
            pass
        return

    def consume(done: asyncio.Task[Any]) -> None:
        try:
            done.result()
        except BaseException:
            pass

    task.add_done_callback(consume)


def _bound_output(stdout: Any, stderr: Any, max_bytes: int) -> tuple[str, str, bool]:
    """Bound combined UTF-8 output without splitting Python code points."""

    out = stdout if isinstance(stdout, str) else str(stdout or "")
    err = stderr if isinstance(stderr, str) else str(stderr or "")
    out_bytes = out.encode("utf-8", errors="replace")
    err_bytes = err.encode("utf-8", errors="replace")
    if len(out_bytes) + len(err_bytes) <= max_bytes:
        return out, err, False

    remaining = max_bytes
    bounded_out = out_bytes[:remaining].decode("utf-8", errors="ignore")
    remaining -= len(bounded_out.encode("utf-8"))
    bounded_err = err_bytes[: max(remaining, 0)].decode("utf-8", errors="ignore")
    return bounded_out, bounded_err, True


__all__ = ["SandboxExecutionService"]
