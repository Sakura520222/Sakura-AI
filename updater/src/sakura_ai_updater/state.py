"""Durable updater state — atomic+dir-fsync write + fail-closed read.

spec §8.4 状态持久化（wrapper schema + error_code + fail-closed + directory fsync）。
Slice 3a 只交付模型 + atomic write + fail-closed load；崩溃恢复 reconcile 在后续 task；
实际写入 active job（ImageAdapter 驱动状态机）在 Slice 4。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

SCHEMA_VERSION = 1

# P0 状态机（spec §8.1）。terminal 状态用于 reconcile 判断。
# INTERRUPTED 不是顶层 state，而是 state="failed" + error_code="interrupted"（§7.6）。
TERMINAL_STATES = frozenset({"success", "failed", "rolled_back"})


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class StateLoadError(RuntimeError):
    """state 文件读取失败（permission / IO）。daemon 启动应拒绝提供 destructive 能力。"""


class StateCorruptionError(StateLoadError):
    """state 文件内容损坏（半截 JSON / 非 dict / active_job 语义不一致）。"""


class UnsupportedStateSchemaError(StateLoadError):
    """state schema_version 不被当前 updater 支持（向前兼容保护，不抹未来字段）。"""


@dataclass
class JobState:
    """单个 update/rollback job 的持久化状态（spec §8.4）。

    ``error_code`` 是结构化失败原因，与 ``state`` 正交：``state="failed"`` +
    ``error_code="interrupted"`` = 崩溃中断。状态机只处理正式 P0 state。
    """

    job_id: str
    operation: str = "update"  # update / rollback
    deployment: str = "image"
    from_version: str | None = None
    from_image: str | None = None
    from_digest: str | None = None
    target_version: str | None = None
    target_image: str | None = None
    state: str = "idle"
    step: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    retry_count: int = 0
    rollback_allowed: bool = False
    error_code: str | None = None
    error: str | None = None

    def is_terminal(self) -> bool:
        """是否终态（success / failed / rolled_back）。"""
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> JobState:
        """从 dict 构造；忽略未知字段（向前兼容）。"""
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class UpdateStateStore:
    """update-state.json 顶层结构（wrapper）。

    ``active_job_id`` 是 destructive task gate（§7.5）：非 null 表示有进行中/未清理的 job。
    ``current_job`` 是该 job 的完整状态。reconcile 在 daemon 启动时处理 active job（§7.6）。
    """

    schema_version: int = SCHEMA_VERSION
    active_job_id: str | None = None
    current_job: JobState | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "active_job_id": self.active_job_id,
            "current_job": self.current_job.to_dict() if self.current_job else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> UpdateStateStore:
        job_data = data.get("current_job")
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            active_job_id=data.get("active_job_id"),
            current_job=JobState.from_dict(job_data) if job_data else None,
        )


def empty_store() -> UpdateStateStore:
    """初始空 store（无文件时）。"""
    return UpdateStateStore()


def load_state(path: str) -> UpdateStateStore:
    """读取持久化 state（**fail-closed**，spec §8.4）。

    - ``FileNotFoundError``（真不存在）→ empty_store()（首次启动，正常）。
    - JSON 损坏 / 非 dict / 无效 UTF-8 → StateCorruptionError（daemon 拒启，不当空 store）。
    - schema_version 不支持 → UnsupportedStateSchemaError（不把未来 v2 当 v1 读）。
    - 其他 OSError（PermissionError 等）→ StateLoadError。

    **绝不用 os.path.exists 预检**：exists() 依赖 stat()，permission 不可达时返回 False
    会误判"不存在"→ fail-open（与 spec"permission denied 必须拒启"冲突）。直接 open 让
    FileNotFoundError 精确区分"真不存在"，PermissionError 走 StateLoadError。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty_store()
    except json.JSONDecodeError as e:
        raise StateCorruptionError(f"corrupt state file {path!r}: {e}") from e
    except UnicodeDecodeError as e:
        # 无效 UTF-8 字节（ValueError 子类，非 OSError）→ 内容损坏，fail-closed
        raise StateCorruptionError(f"corrupt state file {path!r}: {e}") from e
    except OSError as e:
        # PermissionError 等（FileNotFoundError 已在上面单独处理）→ fail-closed
        raise StateLoadError(f"cannot read state file {path!r}: {e}") from e
    if not isinstance(data, dict):
        raise StateCorruptionError(
            f"state file {path!r} is {type(data).__name__}, expected object"
        )
    if data.get("schema_version") != SCHEMA_VERSION:
        raise UnsupportedStateSchemaError(
            f"unsupported schema_version {data.get('schema_version')!r}; "
            f"this updater supports {SCHEMA_VERSION}"
        )
    return UpdateStateStore.from_dict(data)


def _fsync_directory(directory: str) -> None:
    """fsync 父目录使 atomic rename 跨掉电持久（POSIX；Windows 无目录 fsync，跳过）。

    active_job_id 是 destructive gate，rename 后须 fsync 父目录否则掉电可能丢失 rename。
    某些文件系统（tmpfs）不支持目录 fsync → 静默降级（atomic rename 仍是保护）。
    """
    if os.name != "posix":
        return
    dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def save_state(path: str, store: UpdateStateStore) -> None:
    """Atomic + durable write：write temp → fsync file → atomic rename → fsync parent dir。

    断电/崩溃时要么旧文件完整、要么新文件完整；rename 后 fsync 父目录保证 active_job_id
    gate 跨掉电不丢（spec §8.4 durable 要求）。Slice 3a 即做（active_job_id 是安全 gate）。
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".update-state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(store.to_dict(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic rename
        _fsync_directory(directory)  # 持久化 rename 到父目录（掉电保护）
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
