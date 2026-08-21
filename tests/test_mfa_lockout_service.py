"""MFA lockout fallback runtime-task regression tests."""

from __future__ import annotations

import asyncio

import pytest

import backend.services.mfa_lockout_service as mfa
from backend.services.database_reset_runtime_service import (
    DatabaseResetRuntimeSupervisor,
    bind_runtime_supervisor,
    reset_runtime_supervisor,
)


@pytest.fixture
def runtime_supervisor():
    supervisor = DatabaseResetRuntimeSupervisor()
    token = bind_runtime_supervisor(supervisor)
    mfa._fail_fallback.clear()
    mfa._lock_fallback.clear()
    try:
        yield supervisor
    finally:
        mfa._fail_fallback.clear()
        mfa._lock_fallback.clear()
        reset_runtime_supervisor(token)


@pytest.mark.asyncio
async def test_mfa_fallback_notification_is_registered(runtime_supervisor, monkeypatch):
    notified = asyncio.Event()

    async def notify(_user_id):
        notified.set()

    monkeypatch.setattr(mfa, "_notify_lockout", notify)

    assert mfa._record_mfa_failure_fallback(42, threshold=1, lock_ttl=60) == 1
    assert len(runtime_supervisor.tasks) == 1
    await asyncio.wait_for(notified.wait(), timeout=0.1)
    await asyncio.sleep(0)
    assert not runtime_supervisor.tasks


@pytest.mark.asyncio
async def test_mfa_fallback_notification_is_rejected_after_quiesce(
    runtime_supervisor,
    monkeypatch,
):
    called = False

    async def notify(_user_id):
        nonlocal called
        called = True

    monkeypatch.setattr(mfa, "_notify_lockout", notify)
    runtime_supervisor.begin_quiesce()

    assert mfa._record_mfa_failure_fallback(43, threshold=1, lock_ttl=60) == 1
    await asyncio.sleep(0)
    assert not called
    assert not runtime_supervisor.tasks
