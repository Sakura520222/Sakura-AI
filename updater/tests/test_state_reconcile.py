"""reconcile_interrupted_job — 崩溃恢复 6 条 invariant（跨平台，纯函数）。

spec §7.6。fail-closed：active_job 语义不一致（含无 gate 却声称执行中）抛 StateCorruptionError。
"""

import pytest
from sakura_ai_updater.state import (
    ERROR_CODE_INTERRUPTED,
    JobState,
    StateCorruptionError,
    UpdateStateStore,
    reconcile_interrupted_job,
)


def test_no_active_job_no_current_ok():
    """active_job_id=null + current_job=null → OK（初始空 store）。"""
    store = UpdateStateStore(active_job_id=None, current_job=None)
    result, changed = reconcile_interrupted_job(store)
    assert changed is False
    assert result.active_job_id is None


def test_no_active_job_terminal_current_ok():
    """active_job_id=null + current_job terminal → OK（保留历史终态记录）。"""
    store = UpdateStateStore(
        active_job_id=None,
        current_job=JobState(job_id="upd_old", state="success"),
    )
    result, changed = reconcile_interrupted_job(store)
    assert changed is False
    assert result.current_job.state == "success"  # 保留历史


def test_no_active_job_but_non_terminal_current_is_corruption():
    """active_job_id=null + current_job 非 terminal → corruption（无 gate 却声称执行中）。"""
    store = UpdateStateStore(
        active_job_id=None,
        current_job=JobState(job_id="upd_001", state="downloading"),
    )
    with pytest.raises(StateCorruptionError):
        reconcile_interrupted_job(store)


def test_non_terminal_active_job_marked_failed_interrupted():
    """active + 非 terminal → state=failed + error_code=interrupted + 清 gate。"""
    store = UpdateStateStore(
        active_job_id="upd_001",
        current_job=JobState(job_id="upd_001", state="downloading", step="docker_pull"),
    )
    result, changed = reconcile_interrupted_job(store)
    assert changed is True
    assert result.current_job.state == "failed"
    assert result.current_job.error_code == ERROR_CODE_INTERRUPTED
    assert result.current_job.error == "updater process restarted mid-update"  # 锁定默认文案
    assert result.active_job_id is None


def test_terminal_success_with_stale_gate_clears_active_job_id():
    """success 终态 + 残留 active_job_id（stale gate）→ 保留终态，清 gate。"""
    store = UpdateStateStore(
        active_job_id="upd_001",
        current_job=JobState(job_id="upd_001", state="success"),
    )
    result, changed = reconcile_interrupted_job(store)
    assert changed is True
    assert result.current_job.state == "success"  # 保留终态记录
    assert result.active_job_id is None  # 清 stale gate


def test_terminal_failed_with_stale_gate_preserves_error():
    """failed 终态 + 残留 gate → 保留原 error_code/error，清 gate（不覆盖诊断）。"""
    store = UpdateStateStore(
        active_job_id="upd_001",
        current_job=JobState(
            job_id="upd_001", state="failed", error_code="health_check", error="timeout"
        ),
    )
    result, changed = reconcile_interrupted_job(store)
    assert changed is True
    assert result.current_job.state == "failed"
    assert result.current_job.error_code == "health_check"  # 保留原诊断
    assert result.current_job.error == "timeout"
    assert result.active_job_id is None


def test_active_job_id_without_current_job_is_corruption():
    """active_job_id 非 null 但 current_job 缺失 → fail-closed。"""
    store = UpdateStateStore(active_job_id="upd_001", current_job=None)
    with pytest.raises(StateCorruptionError):
        reconcile_interrupted_job(store)


def test_mismatched_active_job_id_is_corruption():
    """active_job_id != current_job.job_id → fail-closed。"""
    store = UpdateStateStore(
        active_job_id="upd_001",
        current_job=JobState(job_id="upd_999", state="downloading"),
    )
    with pytest.raises(StateCorruptionError):
        reconcile_interrupted_job(store)
