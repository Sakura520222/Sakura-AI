"""MFA lockout state survives runtime quiescence without background delivery."""

from __future__ import annotations

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
async def test_mfa_fallback_locks_account_without_background_work(runtime_supervisor):
    assert mfa._record_mfa_failure_fallback(42, threshold=1, lock_ttl=60) == 1
    with pytest.raises(mfa.AccountLockedError):
        mfa._check_mfa_lockout_fallback(42)
    assert not runtime_supervisor.tasks


@pytest.mark.asyncio
async def test_mfa_fallback_locks_account_after_quiesce(
    runtime_supervisor,
):
    runtime_supervisor.begin_quiesce()
    assert mfa._record_mfa_failure_fallback(43, threshold=1, lock_ttl=60) == 1
    with pytest.raises(mfa.AccountLockedError):
        mfa._check_mfa_lockout_fallback(43)
    assert not runtime_supervisor.tasks
