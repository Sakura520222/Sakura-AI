from __future__ import annotations

import asyncio

import pytest
from sakura_ai_sandboxer.config import SandboxdConfig
from sakura_ai_sandboxer.errors import (
    CleanupFailedError,
    ConcurrencyLimitError,
    RequestConflictError,
    RuntimeUnavailableError,
)
from sakura_ai_sandboxer.models import ExecutionProfile, ExecutionRequest, NetworkMode
from sakura_ai_sandboxer.runtime import FakeRuntimeAdapter, RuntimeResult
from sakura_ai_sandboxer.service import SandboxExecutionService


def _request(request_id: str = "request-1", **overrides) -> ExecutionRequest:
    values = {
        "request_id": request_id,
        "workspace_key": "task-1",
        "command": "printf ok",
        "profile": ExecutionProfile.AGENT,
        "timeout_seconds": 1,
        "network_mode": NetworkMode.NONE,
    }
    values.update(overrides)
    return ExecutionRequest(**values)


class _HungExecuteRuntime:
    name = "hung-execute"

    def __init__(self):
        self.release = asyncio.Event()
        self.cancel_calls = 0

    async def execute(self, request, *, cancel_event, max_output_bytes, deadline):
        del request, cancel_event, max_output_bytes, deadline
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            while not self.release.is_set():
                await asyncio.sleep(0.001)
        return RuntimeResult(exit_code=None, cancelled=True)

    async def cancel(self, request_id, *, deadline):
        del request_id, deadline
        self.cancel_calls += 1
        self.release.set()

    async def shutdown(self, *, deadline):
        del deadline
        self.release.set()


class _HungCancelRuntime(FakeRuntimeAdapter):
    async def cancel(self, request_id, *, deadline):
        del request_id, deadline
        await asyncio.Future()


class _HungShutdownRuntime(FakeRuntimeAdapter):
    async def shutdown(self, *, deadline):
        del deadline
        await asyncio.Future()


@pytest.mark.asyncio
async def test_service_uses_fake_runtime_and_bounds_combined_output():
    runtime = FakeRuntimeAdapter(
        RuntimeResult(stdout="é" * 20, stderr="stderr"),
    )
    service = SandboxExecutionService(
        SandboxdConfig(max_output_bytes=10),
        runtime,
    )
    result = await service.execute(_request())
    assert result.request_id == "request-1"
    assert result.output_truncated is True
    assert len((result.stdout + result.stderr).encode("utf-8")) <= 10
    assert runtime.requests[0].profile is ExecutionProfile.AGENT
    assert service.active_count == 0


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_stops_active_fake_request():
    runtime = FakeRuntimeAdapter(delay_seconds=10)
    service = SandboxExecutionService(
        SandboxdConfig(shutdown_timeout_seconds=1),
        runtime,
    )
    task = asyncio.create_task(service.execute(_request()))
    while service.active_count == 0:
        await asyncio.sleep(0)
    cancelled = await service.cancel("request-1")
    result = await task
    repeated = await service.cancel("request-1")
    assert cancelled.cancelled is True
    assert result.cancelled is True
    assert repeated.cancelled is True
    assert repeated.state == "cancelled"
    assert "request-1" in runtime.cancelled


@pytest.mark.asyncio
async def test_concurrency_limit_and_shutdown_drain():
    runtime = FakeRuntimeAdapter(delay_seconds=10)
    service = SandboxExecutionService(SandboxdConfig(max_concurrent=1), runtime)
    first = asyncio.create_task(service.execute(_request("first")))
    while service.active_count == 0:
        await asyncio.sleep(0)
    with pytest.raises(ConcurrencyLimitError):
        await service.execute(_request("second"))
    await service.shutdown()
    result = await first
    assert result.cancelled is True
    assert runtime.shutdown_called is True
    assert service.active_count == 0


@pytest.mark.asyncio
async def test_unavailable_runtime_is_typed_and_fail_closed():
    service = SandboxExecutionService()
    with pytest.raises(RuntimeUnavailableError):
        await service.execute(_request())


@pytest.mark.asyncio
async def test_request_id_is_admitted_once_and_completed_id_cannot_replay():
    runtime = FakeRuntimeAdapter(delay_seconds=0.05)
    service = SandboxExecutionService(runtime=runtime)
    first = asyncio.create_task(service.execute(_request("same-id")))
    while service.active_count == 0:
        await asyncio.sleep(0)
    with pytest.raises(RequestConflictError, match="already been used"):
        await service.execute(_request("same-id"))
    await first
    with pytest.raises(RequestConflictError, match="already been used"):
        await service.execute(_request("same-id"))
    assert [item.request_id for item in runtime.requests] == ["same-id"]


@pytest.mark.asyncio
async def test_cancel_before_create_tombstones_id_and_blocks_later_post():
    runtime = FakeRuntimeAdapter()
    service = SandboxExecutionService(runtime=runtime)
    first = await service.cancel("cancel-first")
    repeated = await service.cancel("cancel-first")
    assert first.state == "cancelled"
    assert repeated.state == "cancelled"
    with pytest.raises(RequestConflictError, match="already been used"):
        await service.execute(_request("cancel-first"))
    assert runtime.requests == []


@pytest.mark.asyncio
async def test_completed_ledger_expires_and_capacity_is_bounded():
    runtime = FakeRuntimeAdapter()
    service = SandboxExecutionService(
        SandboxdConfig(
            request_ledger_capacity=1,
            request_ledger_ttl_seconds=0.02,
        ),
        runtime,
    )
    await service.execute(_request("first"))
    with pytest.raises(ConcurrencyLimitError):
        await service.execute(_request("second"))
    await asyncio.sleep(0.03)
    await service.execute(_request("second"))


@pytest.mark.asyncio
async def test_effective_timeout_uses_request_and_both_daemon_caps():
    runtime = FakeRuntimeAdapter(delay_seconds=10)
    service = SandboxExecutionService(
        SandboxdConfig(
            timeout_seconds=0.03,
            max_timeout_seconds=0.05,
            cleanup_margin_seconds=0.2,
        ),
        runtime,
    )
    result = await service.execute(_request("capped", timeout_seconds=1))
    assert result.timed_out is True
    assert result.cancelled is False


@pytest.mark.asyncio
async def test_hung_execute_is_detached_and_does_not_block_timeout_response():
    runtime = _HungExecuteRuntime()
    service = SandboxExecutionService(
        SandboxdConfig(
            timeout_seconds=0.02,
            max_timeout_seconds=0.02,
            cleanup_margin_seconds=0.1,
        ),
        runtime,
    )
    result = await service.execute(_request("hung-execute", timeout_seconds=1))
    assert result.timed_out is True
    assert runtime.cancel_calls == 1
    runtime.release.set()


@pytest.mark.asyncio
async def test_hung_cancel_maps_to_cleanup_failed_with_a_hard_deadline():
    runtime = _HungCancelRuntime(delay_seconds=10)
    service = SandboxExecutionService(
        SandboxdConfig(cleanup_margin_seconds=0.02),
        runtime,
    )
    task = asyncio.create_task(service.execute(_request("hung-cancel")))
    while service.active_count == 0:
        await asyncio.sleep(0)
    with pytest.raises(CleanupFailedError, match="cancellation exceeded"):
        await service.cancel("hung-cancel")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_hung_shutdown_maps_to_cleanup_failed_without_waiting_forever():
    runtime = _HungShutdownRuntime(delay_seconds=10)
    service = SandboxExecutionService(
        SandboxdConfig(
            cleanup_margin_seconds=0.02,
            shutdown_timeout_seconds=0.05,
        ),
        runtime,
    )
    task = asyncio.create_task(service.execute(_request("hung-shutdown")))
    while service.active_count == 0:
        await asyncio.sleep(0)
    with pytest.raises(CleanupFailedError, match="cancellation exceeded|shutdown exceeded"):
        await service.shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
