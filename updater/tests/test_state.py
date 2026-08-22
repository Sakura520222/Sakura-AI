"""state.py — atomic+dir-fsync write / fail-closed load / round-trip（跨平台 + chmod POSIX）。"""

import json
import os
import sys

import pytest
from sakura_ai_updater.state import (
    JobState,
    StateCorruptionError,
    StateLoadError,
    UnsupportedStateSchemaError,
    UpdateStateStore,
    empty_store,
    load_state,
    save_state,
)


def test_load_nonexistent_returns_empty(tmp_path):
    store = load_state(str(tmp_path / "absent.json"))
    assert store.active_job_id is None
    assert store.current_job is None
    assert store.schema_version == 1


def test_save_then_load_round_trip(tmp_path):
    path = str(tmp_path / "update-state.json")
    job = JobState(
        job_id="upd_001",
        operation="update",
        deployment="image",
        target_version="3.1.0",
        state="downloading",
        step="docker_pull",
    )
    store = UpdateStateStore(active_job_id="upd_001", current_job=job)
    save_state(path, store)
    loaded = load_state(path)
    assert loaded.active_job_id == "upd_001"
    assert loaded.current_job.job_id == "upd_001"
    assert loaded.current_job.target_version == "3.1.0"
    assert loaded.current_job.state == "downloading"


def test_save_then_load_preserves_error_code(tmp_path):
    """error_code 与 state 正交，round-trip 保留（spec §8.4）。"""
    path = str(tmp_path / "update-state.json")
    job = JobState(
        job_id="upd_001", state="failed", error_code="health_check", error="timeout"
    )
    save_state(path, UpdateStateStore(active_job_id=None, current_job=job))
    loaded = load_state(path)
    assert loaded.current_job.error_code == "health_check"
    assert loaded.current_job.error == "timeout"


def test_save_creates_parent_dir(tmp_path):
    path = str(tmp_path / "nested" / "deep" / "update-state.json")
    save_state(path, empty_store())
    assert os.path.exists(path)


def test_save_is_atomic_no_temp_leftover(tmp_path):
    """atomic write：完成后无临时文件残留（spec §8.4）。"""
    path = str(tmp_path / "update-state.json")
    save_state(path, empty_store())
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".update-state.")]
    assert leftovers == []


def test_save_produces_valid_wrapper_json(tmp_path):
    path = str(tmp_path / "update-state.json")
    save_state(path, empty_store())
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["schema_version"] == 1
    assert data["active_job_id"] is None
    assert data["current_job"] is None


def test_load_corrupt_json_raises(tmp_path):
    """半截 JSON → StateCorruptionError（fail-closed，不当空 store，spec §8.4）。"""
    path = str(tmp_path / "update-state.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"schema_version":1,"active_job_id":"upd')  # 半截
    with pytest.raises(StateCorruptionError):
        load_state(path)


def test_load_unsupported_schema_raises(tmp_path):
    """未来 schema_version=2 → UnsupportedStateSchemaError（不当 v1 静默读，防抹字段）。"""
    path = str(tmp_path / "update-state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"schema_version": 2, "active_job_id": None, "current_job": None}, f)
    with pytest.raises(UnsupportedStateSchemaError):
        load_state(path)


def test_load_non_dict_json_raises(tmp_path):
    """JSON 合法但非 dict（如 list）→ StateCorruptionError。"""
    path = str(tmp_path / "update-state.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write('["not", "an", "object"]')
    with pytest.raises(StateCorruptionError):
        load_state(path)


def test_load_invalid_utf8_raises(tmp_path):
    """无效 UTF-8 字节 → StateCorruptionError（内容损坏，fail-closed，统一异常族）。"""
    path = str(tmp_path / "update-state.json")
    with open(path, "wb") as f:
        f.write(b'{"schema_version":1,"active_job_id":"upd\xff\xfe"}')  # 非法 UTF-8
    with pytest.raises(StateCorruptionError):
        load_state(path)


@pytest.mark.skipif(
    sys.platform == "win32", reason="chmod permission semantics are POSIX-only"
)
def test_load_permission_denied_raises(tmp_path):
    """permission denied → StateLoadError（fail-closed，绝不当空 store，spec §8.4）。

    关键：load_state 不用 os.path.exists 预检——exists() 在 EACCES 时返回 False 会
    误判"不存在"→ fail-open。直接 open 让 PermissionError 精确触发 StateLoadError。
    """
    path = str(tmp_path / "update-state.json")
    save_state(path, empty_store())
    os.chmod(path, 0o000)
    try:
        with pytest.raises(StateLoadError):
            load_state(path)
    finally:
        os.chmod(path, 0o644)  # 恢复以便 tmp_path 清理


def test_job_state_terminal_detection():
    assert JobState(job_id="x", state="success").is_terminal()
    assert JobState(job_id="x", state="failed").is_terminal()
    assert JobState(job_id="x", state="rolled_back").is_terminal()
    assert not JobState(job_id="x", state="downloading").is_terminal()
    assert not JobState(job_id="x", state="idle").is_terminal()


def test_job_state_from_dict_ignores_unknown_fields():
    """向前兼容：未来 schema 加字段，旧 updater 读不崩溃（已知字段正常构造）。"""
    job = JobState.from_dict({"job_id": "x", "state": "idle", "future_field": "v2"})
    assert job.job_id == "x"
