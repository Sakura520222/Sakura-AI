"""Runtime adapter contract and deterministic test/runtime implementations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .errors import RuntimeUnavailableError
from .models import ExecutionRequest


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    exit_code: int | None = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False


class RuntimeAdapter(Protocol):
    """Host runtime contract; it receives no arbitrary Docker parameters."""

    @property
    def name(self) -> str:
        ...

    async def execute(
        self,
        request: ExecutionRequest,
        *,
        cancel_event: asyncio.Event,
        max_output_bytes: int,
        deadline: float,
    ) -> RuntimeResult:
        """Run with an absolute monotonic deadline and incremental byte budget."""
        ...

    async def cancel(self, request_id: str, *, deadline: float) -> None:
        """Stop request-owned resources before the supplied absolute deadline."""
        ...

    async def shutdown(self, *, deadline: float) -> None:
        """Drain runtime-owned resources before the supplied absolute deadline."""
        ...


class UnavailableRuntimeAdapter:
    """Default until the OCI runtime adapter is installed in a later slice."""

    name = "unavailable"

    async def execute(
        self,
        request: ExecutionRequest,
        *,
        cancel_event: asyncio.Event,
        max_output_bytes: int,
        deadline: float,
    ) -> RuntimeResult:
        del request, cancel_event, max_output_bytes, deadline
        raise RuntimeUnavailableError()

    async def cancel(self, request_id: str, *, deadline: float) -> None:
        del request_id, deadline

    async def shutdown(self, *, deadline: float) -> None:
        del deadline


class FakeRuntimeAdapter:
    """Small, configurable runtime fake used by daemon and client tests."""

    name = "fake"

    def __init__(
        self,
        result: RuntimeResult | Callable[[ExecutionRequest], RuntimeResult] | None = None,
        *,
        delay_seconds: float = 0.0,
    ) -> None:
        self.result = result or RuntimeResult()
        self.delay_seconds = delay_seconds
        self.requests: list[ExecutionRequest] = []
        self.cancelled: set[str] = set()
        self.active: set[str] = set()
        self.shutdown_called = False
        self._events: dict[str, asyncio.Event] = {}

    async def execute(
        self,
        request: ExecutionRequest,
        *,
        cancel_event: asyncio.Event,
        max_output_bytes: int,
        deadline: float,
    ) -> RuntimeResult:
        self.requests.append(request)
        self.active.add(request.request_id)
        self._events[request.request_id] = cancel_event
        try:
            delay_deadline = min(
                deadline,
                asyncio.get_running_loop().time() + max(self.delay_seconds, 0),
            )
            while self.delay_seconds and asyncio.get_running_loop().time() < delay_deadline:
                if cancel_event.is_set():
                    return RuntimeResult(exit_code=None, cancelled=True)
                await asyncio.sleep(
                    min(
                        0.01,
                        max(delay_deadline - asyncio.get_running_loop().time(), 0),
                    )
                )
            if asyncio.get_running_loop().time() >= deadline:
                return RuntimeResult(exit_code=None, timed_out=True)
            if cancel_event.is_set():
                return RuntimeResult(exit_code=None, cancelled=True)
            value = self.result(request) if callable(self.result) else self.result
            stdout, stderr, truncated = _bound_output(
                value.stdout,
                value.stderr,
                max_output_bytes,
            )
            return RuntimeResult(
                exit_code=value.exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=value.timed_out,
                cancelled=value.cancelled,
                output_truncated=value.output_truncated or truncated,
            )
        finally:
            self.active.discard(request.request_id)
            self._events.pop(request.request_id, None)

    async def cancel(self, request_id: str, *, deadline: float) -> None:
        del deadline
        self.cancelled.add(request_id)
        event = self._events.get(request_id)
        if event is not None:
            event.set()

    async def shutdown(self, *, deadline: float) -> None:
        del deadline
        self.shutdown_called = True
        for event in tuple(self._events.values()):
            event.set()


def _bound_output(stdout: str, stderr: str, max_bytes: int) -> tuple[str, str, bool]:
    """Apply the combined UTF-8 budget before a runtime result is returned."""

    out_bytes = stdout.encode("utf-8", errors="replace")
    err_bytes = stderr.encode("utf-8", errors="replace")
    if len(out_bytes) + len(err_bytes) <= max_bytes:
        return stdout, stderr, False
    bounded_out = out_bytes[:max_bytes].decode("utf-8", errors="ignore")
    remaining = max_bytes - len(bounded_out.encode("utf-8"))
    bounded_err = err_bytes[: max(remaining, 0)].decode("utf-8", errors="ignore")
    return bounded_out, bounded_err, True


__all__ = [
    "FakeRuntimeAdapter",
    "RuntimeAdapter",
    "RuntimeResult",
    "UnavailableRuntimeAdapter",
]

def __getattr__(name: str):
    """Lazily expose the Docker adapter without a module import cycle."""

    if name in {"DockerRuntimeAdapter", "WorkspaceResolver"}:
        from .docker_runtime import DockerRuntimeAdapter, WorkspaceResolver

        return {
            "DockerRuntimeAdapter": DockerRuntimeAdapter,
            "WorkspaceResolver": WorkspaceResolver,
        }[name]
    raise AttributeError(name)
