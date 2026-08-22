# Auto-Update P0 — Slice 3a: Updater 骨架 + durable state/锁/reconcile + UDS IPC + backend 连接

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立独立的 `sakura_ai_updater` Python 项目（src layout，dev 模式源码运行），实现 durable state 持久化（atomic + directory-fsync write + fail-closed 读取）、OS-level flock 进程唯一性、崩溃恢复 reconcile（6 条 invariant）、HTTP over UDS IPC（协议 v1 body envelope + live/stale socket 真检测 + socket lifecycle 自管），backend 经 UDS client 连接 updater（校验 envelope shape + malformed JSON 降级）并在 `/version/info` 暴露连接状态。

**Architecture:** updater 是仓库内独立 Python 项目（`updater/` + 自己的 `pyproject.toml`，src layout，依赖 fastapi + uvicorn + pydantic）。`state.py` 用 atomic write（write temp → fsync file → atomic rename → fsync parent dir）持久化 wrapper 结构的 `update-state.json`，读取 **fail-closed**（直接 `open`，`FileNotFoundError`→空 store，corrupt/不支持 schema/IO 错误抛异常，**不用 `os.path.exists` 预检**避免 permission 误判）；启动时 OS-level `flock`（`LOCK_EX|LOCK_NB`）保证进程唯一，reconcile 按 **6 条 invariant** 处理 active job；`ipc.py` 是 FastAPI app，成功响应经 `envelope()` helper 包成 `{protocol_version, updater_version, data}`（版本字段只在 envelope 顶层）；`socket_util.py` 自管 UDS lifecycle（**不创建父目录**——`/run/sakura-ai` bootstrap 属 3b；AF_UNIX connect probe 区分 live/stale socket，绝不 unlink live socket）；`__main__.py` 的 `--serve` 在单个 `try/finally` 内完成 flock → socket 准备 → reconcile → uvicorn，finally 清 socket + 释放 flock。backend `UpdaterClient` 用 `httpx.AsyncHTTPTransport(uds=...)` 连接并校验 v1 envelope shape（malformed JSON / shape 非法 / 连不上均返回 None）。

**Tech Stack:** FastAPI / uvicorn（非 standard，减少 3c 打包面）/ httpx（UDS transport）/ fcntl flock / pydantic / pytest + pytest-asyncio / starlette TestClient。

**关联设计：** [2026-08-07-auto-update-design.md](../specs/2026-08-07-auto-update-design.md)（§7 IPC、§7.5 两层锁、§7.6 崩溃 reconcile 6 invariant、§8.4 状态持久化、§16.1 项目结构）。本 plan 已与 spec §7.6/§8.4 同步（wrapper schema + fail-closed + error_code + 6 条 reconcile invariant + directory fsync）。

**前置：** Slice 1（`4425f34b`）+ Slice 2（`2b02fb2a`）已提交。

---

## ⚠️ 提交合规

按 `CLAUDE.md`：**执行者不得自主 `git commit`，也不允许子代理提交**。每个 task 完成后只 `git add` 暂存，由用户审查后决定是否提交。计划中的 commit 信息仅为建议，**仅在用户明确授权后执行**。

## 固定边界（用户拍板，不得越界）

- **backend 不写 `.deploy/deployment.env`** — 只有 `start.sh` bootstrap + Host Updater 可写。
- **GID 9472 / `/run/sakura-ai` 目录创建与权限 / compose `group_add`+socket 挂载 / `start.sh updater` CLI / 真实端到端 backend 容器连 updater → Slice 3b**。`socket_util.prepare_socket_path` **不创建父目录**——父目录不存在直接 `SocketPathError`，由 3b bootstrap / dev 手动 `mkdir`。
- **`update apply`（ImageAdapter + 状态机驱动 + 实际更新动作）→ Slice 4**，3a/3b 不提前实现。
- **PyInstaller 打包 → Slice 3c**；3a/3b 开发阶段直接 `python -m sakura_ai_updater --serve`。
- 本 slice 不实现 `/v1/check`、`/v1/preflight`、`/v1/update`、`/v1/jobs/*` 等动作端点（Slice 4），只交付 `GET /v1/status` + `GET /v1/health`。
- **§7.5 第二层锁（destructive task `asyncio.Lock`）留 Slice 4**——本 slice 没有 destructive endpoint，无对象加锁。3a 只交付 daemon process flock + persisted `active_job_id` gate 基础。

## 平台说明（关键）

updater 是 **Linux 宿主机组件**（spec §4 ADR-2/3）。本仓库开发机为 Windows，但：

- **跨平台可测（Windows 本地 green）：** `state.py`（纯 Python atomic + fail-closed；`chmod` 权限测试 POSIX only）、`socket_util.py` 的非 bind 部分、`ipc.py` 的 TestClient 部分（HTTP 层）、`UpdaterClient` 的 shape 校验 + 连不上、`build_version_info` 纯函数测试。
- **POSIX only（Windows skip，WSL/CI green）：** `locks.py`（`fcntl.flock`）、UDS 传输层（uvicorn `uds=`、httpx UDS transport、`__main__ --serve`）、`socket_util` 的 bind/connect socket 测试、`state.py` 的 `chmod` 权限测试。

POSIX-only 测试用 `pytest.importorskip("fcntl")`（模块级，避免 collect 阶段 ImportError）或 `@pytest.mark.skipif(sys.platform == "win32", ...)`（函数级）。**执行者须在 WSL/CI（Linux）补跑 POSIX 测试**；Windows 本地跑这些用例会 skip（非 fail）。

## 范围与非目标

**交付：**
- `updater/` 独立项目（`pyproject.toml` + src layout + `__main__ --serve/--socket-path/--state-dir`）
- `state.py`：`JobState`（含 `error_code`）/ `UpdateStateStore` + 异常族 + atomic + directory-fsync `save_state` + fail-closed `load_state` + `reconcile_interrupted_job`（6 条 invariant）
- `locks.py`：OS-level `flock` 进程唯一性
- `socket_util.py`：UDS 文件 lifecycle（父目录须存在 + live/stale connect probe + owned 清理 + 非 socket 拒删）
- `ipc.py`：`create_app()` + `envelope()` helper + `GET /v1/status` + `GET /v1/health`
- `__main__.py`：`--serve`（单 try/finally：flock → socket 准备 → reconcile → uvicorn → 清理）
- `backend/services/updater_client.py`：`UpdaterClient`（httpx UDS + envelope shape 校验 + malformed JSON 降级）
- `backend/core/config.py`：Settings 加 `sakura_updater_socket_path`
- `backend/webui/routes/version.py`：`build_version_info` 加 `updater_info` 参数 + `updater_connected/version/protocol_version` 字段
- `version_manager.html`：部署卡加 Host Updater 连接状态行

**不做：** 动作端点（Slice 4）/ ImageAdapter + 状态机驱动（Slice 4）/ destructive `asyncio.Lock`（Slice 4）/ GID group + compose 挂载 + socket 权限 bootstrap + `/run/sakura-ai` 创建（3b）/ PyInstaller（3c）/ start.sh CLI（3b）。

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `updater/pyproject.toml` | Create | 项目元数据 + 依赖（fastapi/uvicorn/pydantic）+ src layout + entry point |
| `updater/src/sakura_ai_updater/__init__.py` | Create | `__version__` + `PROTOCOL_VERSION` |
| `updater/src/sakura_ai_updater/state.py` | Create | `JobState`（含 `error_code`）/ `UpdateStateStore` + 异常族 + atomic+dir-fsync `save` + fail-closed `load` + `reconcile_interrupted_job`（6 invariant） |
| `updater/src/sakura_ai_updater/locks.py` | Create | OS-level `flock` 进程唯一性 |
| `updater/src/sakura_ai_updater/socket_util.py` | Create | UDS lifecycle（父目录须存在 + live/stale connect probe + owned 清理 + 非 socket 拒删） |
| `updater/src/sakura_ai_updater/ipc.py` | Create | FastAPI app + `envelope()` helper + `/v1/status` + `/v1/health` |
| `updater/src/sakura_ai_updater/__main__.py` | Create | argparse `--serve` → 单 try/finally flock+socket+reconcile+uvicorn |
| `updater/tests/test_state.py` | Create | atomic+dir-fsync write / fail-closed load / round-trip（跨平台 + chmod POSIX） |
| `updater/tests/test_state_reconcile.py` | Create | reconcile 6 条 invariant（跨平台，纯函数） |
| `updater/tests/test_locks.py` | Create | flock 互斥（进程内 + subprocess，POSIX only） |
| `updater/tests/test_socket_util.py` | Create | socket lifecycle（4 跨平台 + 3 POSIX only） |
| `updater/tests/test_ipc.py` | Create | envelope + /v1/status（TestClient 跨平台）+ UDS 集成（POSIX only） |
| `backend/services/updater_client.py` | Create | httpx UDS client + `get_status` + `is_valid_v1_envelope`（malformed JSON 降级） |
| `backend/core/config.py` | Modify | Settings 加 `sakura_updater_socket_path` |
| `backend/webui/routes/version.py` | Modify | `build_version_info` 加 updater 连接状态 |
| `backend/webui/templates/version_manager.html` | Modify | 部署卡 Host Updater 连接行 |
| `tests/test_updater_client.py` | Create | shape 校验 + 连不上 + 连得上 + malformed JSON |
| `tests/test_version_info.py` | Modify | updater 连接状态测试 |

---

## Task 1: updater 项目骨架 + state.py（atomic+dir-fsync write + fail-closed load）

**Files:**
- Create: `updater/pyproject.toml`
- Create: `updater/src/sakura_ai_updater/__init__.py`
- Create: `updater/src/sakura_ai_updater/state.py`
- Create: `updater/tests/test_state.py`

- [ ] **Step 1: 创建 pyproject.toml（src layout）**

Create `updater/pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "sakura-ai-updater"
version = "0.1.0"
description = "Sakura AI Host Updater — independent update orchestrator (spec auto-update §4 ADR-1)"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.109.0",
    "uvicorn>=0.27.0",
    "pydantic>=2.5.3",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "httpx>=0.27.0",
]

[project.scripts]
sakura-ai-updater = "sakura_ai_updater.__main__:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

> 用 `uvicorn`（非 `uvicorn[standard]`）：纯 UDS daemon 不需要 uvloop/httptools/watchfiles/websockets，Slice 3c PyInstaller 打包面更小。主项目 venv 已含 fastapi/uvicorn/pydantic/pytest/httpx，editable install 只装 updater 包本身。

- [ ] **Step 2: 创建包 __init__.py（须在 editable install 之前，否则 setuptools 找不到包）**

Create `updater/src/sakura_ai_updater/__init__.py`:

```python
"""Sakura AI Host Updater — 宿主机独立更新编排进程。

仓库内独立 Python 项目（src layout）。dev 模式 ``python -m sakura_ai_updater --serve``；
Slice 3c 再 PyInstaller 打包为单二进制（spec §16.1）。
"""

__version__ = "0.1.0"

# IPC 协议版本（spec §7.2 body envelope）。Slice 3a 实现 v1。
PROTOCOL_VERSION = 1
```

- [ ] **Step 3: editable 安装 updater 到当前 venv**

Run（PowerShell 与 bash 通用）:
```bash
pip install -e ./updater
```
Expected: `Successfully installed sakura-ai-updater-0.1.0`（依赖已满足则不重复装）。

- [ ] **Step 4: 验证 import**

Run: `python -c "import sakura_ai_updater; print(sakura_ai_updater.__version__, sakura_ai_updater.PROTOCOL_VERSION)"`
Expected: `0.1.0 1`。若失败说明 Step 1/2 顺序错误或 setuptools 未发现 src 包。

- [ ] **Step 5: 写失败测试**

Create `updater/tests/test_state.py`:

```python
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
    assert not JobState(job_id="x", state="downloading").is_terminal()
    assert not JobState(job_id="x", state="idle").is_terminal()


def test_job_state_from_dict_ignores_unknown_fields():
    """向前兼容：未来 schema 加字段，旧 updater 读不崩溃（已知字段正常构造）。"""
    job = JobState.from_dict({"job_id": "x", "state": "idle", "future_field": "v2"})
    assert job.job_id == "x"
```

- [ ] **Step 6: 运行测试确认失败**

Run: `python -m pytest updater/tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sakura_ai_updater.state'`

- [ ] **Step 7: 实现 state.py**

Create `updater/src/sakura_ai_updater/state.py`:

```python
"""Durable updater state — atomic+dir-fsync write + fail-closed read + 崩溃恢复 reconcile。

spec §8.4 状态持久化（wrapper schema + error_code + fail-closed + directory fsync）
+ §7.6 崩溃恢复（6 条 invariant）。Slice 3a 只交付模型 + atomic write + fail-closed
load + reconcile；实际写入 active job（ImageAdapter 驱动状态机）在 Slice 4。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

SCHEMA_VERSION = 1

# P0 状态机（spec §8.1）。terminal 状态用于 reconcile 判断。
# INTERRUPTED 不是顶层 state，而是 state="failed" + error_code="interrupted"（§7.6）。
TERMINAL_STATES = frozenset({"success", "failed", "rolled_back"})


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    - JSON 损坏 / 非 dict → StateCorruptionError（daemon 拒启，不当空 store）。
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
```

> 注：`save_state` 的 `os.makedirs(directory, exist_ok=True)` 创建的是 **state 文件父目录**（`.deploy/updater/`，updater 自己的状态目录），**不是** `/run/sakura-ai`（socket 父目录，3b 职责）。两者不同：state 目录是 updater 持久化数据的归属，socket 目录是易失 runtime。

- [ ] **Step 8: 运行测试确认通过**

Run: `python -m pytest updater/tests/test_state.py -v`
Expected:
- POSIX（WSL/Linux）：12 passed（含 chmod permission 测试）。
- Windows：11 passed + 1 skipped（chmod）。

- [ ] **Step 9: ruff 检查**

Run: `python run_ruff.py --check updater/src/sakura_ai_updater/state.py updater/src/sakura_ai_updater/__init__.py updater/tests/test_state.py`
Expected: 无错误（本地"拒绝访问"warning 可忽略）。

- [ ] **Step 10: 暂存变更（不提交）**

```bash
git add updater/pyproject.toml updater/src/sakura_ai_updater/__init__.py updater/src/sakura_ai_updater/state.py updater/tests/test_state.py
```

**建议 commit 信息（待用户授权）：** `feat(updater): project scaffold + atomic+dir-fsync durable state (fail-closed load, error_code)`

---

## Task 2: OS flock 进程唯一性 + 崩溃 reconcile（6 条 invariant）

**Files:**
- Create: `updater/src/sakura_ai_updater/locks.py`
- Modify: `updater/src/sakura_ai_updater/state.py`（追加 `ERROR_CODE_INTERRUPTED` + `reconcile_interrupted_job`）
- Create: `updater/tests/test_locks.py`
- Create: `updater/tests/test_state_reconcile.py`

- [ ] **Step 1: 写 locks 失败测试（进程内 + subprocess 跨进程）**

Create `updater/tests/test_locks.py`:

```python
"""locks.py — OS flock 进程唯一性（POSIX only；Windows 整模块 skip）。"""

import os

import pytest

# fcntl 仅 POSIX；Windows 在 collect 阶段即 skip 整个模块（避免 ImportError）。
fcntl = pytest.importorskip("fcntl")

from sakura_ai_updater.locks import (  # noqa: E402
    LockBusyError,
    acquire_process_lock,
    release_process_lock,
)


def test_second_acquire_fails_busy(tmp_path):
    """同一进程内两次 open 同一 lock 文件，第二个 acquire 必须失败（spec §7.5）。

    不同 open-file-description 在 Linux 上 flock 互斥（man flock.2）。
    """
    lock_path = str(tmp_path / "updater.lock")
    fd1 = acquire_process_lock(lock_path)
    try:
        with pytest.raises(LockBusyError):
            acquire_process_lock(lock_path)
    finally:
        release_process_lock(fd1)


def test_release_allows_reacquire(tmp_path):
    lock_path = str(tmp_path / "updater.lock")
    fd1 = acquire_process_lock(lock_path)
    release_process_lock(fd1)
    fd2 = acquire_process_lock(lock_path)  # 释放后可重新获取
    release_process_lock(fd2)


def test_acquire_creates_lock_file_and_parent_dir(tmp_path):
    lock_path = str(tmp_path / "nested" / "updater.lock")
    fd = acquire_process_lock(lock_path)
    try:
        assert os.path.exists(lock_path)
    finally:
        release_process_lock(fd)


def test_second_process_cannot_start(tmp_path):
    """子进程 acquire 同一 lock → LockBusyError（真实跨进程 daemon 互斥 invariant）。

    不只靠 Task 3 手动三终端验证，把"第二 daemon 进程不能启动"自动化。
    """
    import subprocess
    import sys as _sys

    lock_path = tmp_path / "updater.lock"
    fd = acquire_process_lock(str(lock_path))
    try:
        result = subprocess.run(
            [
                _sys.executable,
                "-c",
                "import sys\n"
                "from sakura_ai_updater.locks import acquire_process_lock, LockBusyError\n"
                "try:\n"
                "    acquire_process_lock(sys.argv[1])\n"
                "except LockBusyError:\n"
                "    sys.exit(3)\n"
                "sys.exit(0)\n",
                str(lock_path),
            ],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 3, (
            f"expected LockBusyError (exit 3); got {result.returncode}; "
            f"stderr={result.stderr.decode()!r}"
        )
    finally:
        release_process_lock(fd)
```

- [ ] **Step 2: 写 reconcile 失败测试（6 条 invariant）**

Create `updater/tests/test_state_reconcile.py`:

```python
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
    assert result.current_job.error is not None
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
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest updater/tests/test_locks.py updater/tests/test_state_reconcile.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sakura_ai_updater.locks'`（test_locks POSIX 跑；Windows skip。test_state_reconcile 因 `reconcile_interrupted_job`/`ERROR_CODE_INTERRUPTED` 未定义而 FAIL）。

- [ ] **Step 4: 实现 locks.py**

Create `updater/src/sakura_ai_updater/locks.py`:

```python
"""OS-level flock 进程唯一性（spec §7.5 第一层锁）。

daemon 启动时 ``acquire_process_lock``（``LOCK_EX|LOCK_NB``），获取失败则退出——
防止 DaemonBackend 因 race 被拉起两份、两个 Python 进程各有自己的 ``asyncio.Lock``。

POSIX only（fcntl）。updater 是 Linux 宿主机组件（spec §4）；Windows 开发机不跑 updater。
"""

from __future__ import annotations

import fcntl
import os


class LockBusyError(RuntimeError):
    """另一个 updater 进程已持有锁。"""


def acquire_process_lock(path: str) -> int:
    """获取进程唯一锁（非阻塞）。返回 fd（调用方须持有以保持锁）。

    ``LOCK_EX | LOCK_NB``：拿不到立即失败（LockBusyError），不阻塞。
    fd 不能关闭——关闭即释放锁。daemon 进程生命周期内持有。
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        os.close(fd)
        raise LockBusyError(f"another updater process holds the lock: {path}") from e
    return fd


def release_process_lock(fd: int) -> None:
    """释放锁并关闭 fd（正常退出时调用；进程死亡 OS 自动释放）。"""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
```

- [ ] **Step 5: 追加 ERROR_CODE_INTERRUPTED + reconcile_interrupted_job 到 state.py**

在 `updater/src/sakura_ai_updater/state.py` 的 `TERMINAL_STATES` 定义之后、`class StateLoadError` 之前插入常量，并在文件末尾追加 reconcile 函数。

(a) 在 `TERMINAL_STATES = frozenset(...)` 之后插入:

```python
# reconcile 标记中断 job 的 error_code（state="failed" + error_code=interrupted，§7.6）。
# INTERRUPTED 不是顶层 state，而是 FAILED 子态的诊断码。
ERROR_CODE_INTERRUPTED = "interrupted"
```

(b) 在文件末尾追加:

```python
def reconcile_interrupted_job(
    store: UpdateStateStore,
) -> tuple[UpdateStateStore, bool]:
    """崩溃恢复 reconcile（spec §7.6）。daemon 启动时调用。

    Returns:
        (store, changed)：changed=True 表示发生了清理，调用方应 ``save_state``。
        损坏（active_job 语义不一致 / 无 gate 却声称执行中）抛 StateCorruptionError，fail-closed。

    6 条 invariant（§7.6）:
        active_job_id == null AND (current_job == null OR current_job.state terminal)
            → OK（changed=False；保留历史 terminal job）
        active_job_id == null AND current_job 非 null AND current_job.state 非 terminal
            → StateCorruptionError（无 gate 却声称执行中，不可能状态）
        active_job_id != null AND current_job == null
            → StateCorruptionError
        active_job_id != null AND active_job_id != current_job.job_id
            → StateCorruptionError
        active_job_id != null AND current_job.state 非 terminal
            → 中断恢复：state=failed + error_code=interrupted，清 active_job_id，changed=True
        active_job_id != null AND current_job.state terminal
            → stale gate：保留 job 终态记录，清 active_job_id，changed=True
    """
    if store.active_job_id is None:
        # 第 2 条 invariant：无 gate 但 current_job 声称执行中 → corruption
        job = store.current_job
        if job is not None and not job.is_terminal():
            raise StateCorruptionError(
                f"active_job_id is null but current_job {job.job_id!r} is non-terminal "
                f"(state={job.state!r})"
            )
        return store, False
    job = store.current_job
    if job is None:
        raise StateCorruptionError(
            f"active_job_id={store.active_job_id!r} but current_job is null"
        )
    if job.job_id != store.active_job_id:
        raise StateCorruptionError(
            f"active_job_id={store.active_job_id!r} != current_job.job_id={job.job_id!r}"
        )
    if not job.is_terminal():
        # 中断恢复（非 terminal → failed + interrupted）
        job.state = "failed"
        job.error_code = ERROR_CODE_INTERRUPTED
        job.error = job.error or "updater process restarted mid-update"
        job.updated_at = _utcnow()
        store.active_job_id = None
        return store, True
    # terminal + 残留 active_job_id：stale gate，清 active_job_id 保留终态记录
    store.active_job_id = None
    return store, True
```

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest updater/tests/test_locks.py updater/tests/test_state_reconcile.py -v`
Expected:
- POSIX（WSL/Linux）：test_locks 4 passed（含 subprocess）+ test_state_reconcile 8 passed。
- Windows：test_locks 4 skipped（`fcntl` not available）+ test_state_reconcile 8 passed。

- [ ] **Step 7: ruff 检查**

Run: `python run_ruff.py --check updater/src/sakura_ai_updater/locks.py updater/src/sakura_ai_updater/state.py updater/tests/test_locks.py updater/tests/test_state_reconcile.py`
Expected: 无错误（test_locks.py 的 `# noqa: E402` 是 importorskip 后再 import 的故意延迟，已标注）。

- [ ] **Step 8: 暂存变更（不提交）**

```bash
git add updater/src/sakura_ai_updater/locks.py updater/src/sakura_ai_updater/state.py updater/tests/test_locks.py updater/tests/test_state_reconcile.py
```

**建议 commit 信息（待用户授权）：** `feat(updater): OS flock process uniqueness (incl. subprocess) + 6-invariant reconcile`

---

## Task 3: socket lifecycle + IPC server（envelope + /v1/status）+ __main__ --serve

**Files:**
- Create: `updater/src/sakura_ai_updater/socket_util.py`
- Create: `updater/src/sakura_ai_updater/ipc.py`
- Create: `updater/src/sakura_ai_updater/__main__.py`
- Create: `updater/tests/test_socket_util.py`
- Create: `updater/tests/test_ipc.py`

- [ ] **Step 1: 写 socket_util 失败测试（4 跨平台 + 3 POSIX only）**

Create `updater/tests/test_socket_util.py`:

```python
"""socket_util — UDS 文件 lifecycle（stat/os.remove 跨平台；bind/connect socket POSIX only）。

prepare_socket_path 不创建父目录（/run/sakura-ai bootstrap 属 3b）；用 AF_UNIX connect
probe 区分 live/stale socket（绝不 unlink live）。
"""

import os
import sys

import pytest

from sakura_ai_updater.socket_util import (
    SocketPathError,
    cleanup_owned_socket,
    prepare_socket_path,
)


def test_prepare_requires_existing_parent(tmp_path):
    """父目录不存在 → SocketPathError（3a 不越界创建 /run/sakura-ai，spec 边界）。"""
    path = str(tmp_path / "nonexistent" / "updater.sock")
    with pytest.raises(SocketPathError):
        prepare_socket_path(path)
    assert not os.path.exists(tmp_path / "nonexistent")  # 未创建


def test_prepare_refuses_non_socket_file(tmp_path):
    """非 socket 文件占用路径 → SocketPathError（绝不乱删用户文件）。"""
    path = str(tmp_path / "updater.sock")
    with open(path, "w") as f:
        f.write("important data")
    with pytest.raises(SocketPathError):
        prepare_socket_path(path)
    assert os.path.exists(path)  # 原文件保留


def test_cleanup_ignores_nonexistent(tmp_path):
    cleanup_owned_socket(str(tmp_path / "absent.sock"))  # 不报错


def test_cleanup_does_not_remove_non_socket(tmp_path):
    """cleanup 只删 socket，不误删普通文件。"""
    path = str(tmp_path / "updater.sock")
    with open(path, "w") as f:
        f.write("data")
    cleanup_owned_socket(path)
    assert os.path.exists(path)


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket bind is POSIX-only")
def test_prepare_removes_stale_socket(tmp_path):
    """已存在的 Unix socket 但无人监听（stale，上次崩溃残留）→ 删除。"""
    import socket

    path = str(tmp_path / "updater.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.close()  # bind 但不 listen → connect 会 ConnectionRefused → stale
    assert os.path.exists(path)
    prepare_socket_path(path)  # 不报错
    assert not os.path.exists(path)  # stale 已删


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket bind is POSIX-only")
def test_prepare_refuses_live_socket(tmp_path):
    """另一个 daemon 正在监听的 socket（live）→ SocketPathError，绝不 unlink。

    防误配置：同 socket path + 不同 lock path 时，第二个 daemon 拿到自己的 flock 后
    不能 unlink 第一 daemon 正在用的 live socket。
    """
    import socket

    path = str(tmp_path / "updater.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)  # listen → connect 会成功 → live
    try:
        with pytest.raises(SocketPathError):
            prepare_socket_path(path)
        assert os.path.exists(path)  # live socket 保留，未删
    finally:
        listener.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket bind is POSIX-only")
def test_cleanup_removes_owned_socket(tmp_path):
    import socket

    path = str(tmp_path / "updater.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.close()
    cleanup_owned_socket(path)
    assert not os.path.exists(path)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest updater/tests/test_socket_util.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sakura_ai_updater.socket_util'`

- [ ] **Step 3: 实现 socket_util.py**

Create `updater/src/sakura_ai_updater/socket_util.py`:

```python
"""UDS socket 文件 lifecycle（live/stale 真检测 + owned 清理）。

Python 3.12 的 ``asyncio.create_unix_server`` 无 ``cleanup_socket`` 参数（3.13 才加），
直接用 ``uvicorn.Server.serve()`` 也绕过 ``uvicorn.run()`` wrapper 的 socket 清理。故
updater 自己管理 socket 文件。

**不创建父目录**：``/run/sakura-ai`` 创建/bootstrap 属 Slice 3b；本模块要求父目录已存在，
否则 SocketPathError（dev/生产须先 mkdir）。

**live vs stale 真检测**：用 AF_UNIX connect probe——connect 成功说明有 daemon 正监听
（live），绝不 unlink；ConnectionRefused 说明是上次崩溃残留（stale），安全删除。避免
"任何已存在 socket 都当 stale 删"导致误配置时 unlink live socket。
"""

from __future__ import annotations

import os
import socket
import stat


class SocketPathError(RuntimeError):
    """socket 路径不可用（父目录缺失 / 非 socket 文件占用 / live socket / 探测失败）。"""


def prepare_socket_path(socket_path: str) -> None:
    """启动前确保 socket 路径可用（**不创建父目录**）。

    - 父目录不存在 → SocketPathError（3a 不越界创建 /run/sakura-ai）。
    - 路径不存在 → OK（uvicorn 将创建）。
    - 已存在但**不是** socket（普通文件/目录）→ SocketPathError（拒绝启动，不乱删）。
    - 已存在且是 Unix socket：
        - AF_UNIX connect 成功 → **live socket**（另一 daemon 正监听）→ SocketPathError
          （防误配置：同 socket path 不同 lock path 时 unlink live socket）。
        - ConnectionRefused → stale（上次崩溃残留）→ unlink。
        - 其他 OSError → SocketPathError（fail-closed，不删）。
    """
    parent = os.path.dirname(socket_path) or "."
    if not os.path.isdir(parent):
        raise SocketPathError(
            f"socket parent directory does not exist: {parent!r} "
            f"(create it before starting the daemon; /run/sakura-ai bootstrap is Slice 3b)"
        )
    if not os.path.exists(socket_path):
        return
    try:
        st = os.stat(socket_path)
    except OSError:
        return  # stat 失败，让 uvicorn bind 时报错
    if not stat.S_ISSOCK(st.st_mode):
        raise SocketPathError(
            f"socket path {socket_path!r} exists but is not a socket; "
            f"refusing to remove a non-socket file"
        )
    # 区分 live vs stale：connect probe
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(socket_path)
    except ConnectionRefusedError:
        probe.close()
        os.remove(socket_path)  # stale，安全删除
        return
    except OSError as e:
        probe.close()
        raise SocketPathError(f"cannot probe socket {socket_path!r}: {e}") from None
    # connect 成功 = live（另一 daemon 正监听），绝不 unlink
    probe.close()
    raise SocketPathError(
        f"socket {socket_path!r} is live (another daemon is listening); "
        f"refusing to unlink — check for misconfigured --lock-path"
    )


def cleanup_owned_socket(socket_path: str) -> None:
    """关闭后删除自己拥有的 socket（仅当它是 socket，避免误删普通文件/静默失败）。"""
    if not os.path.exists(socket_path):
        return
    try:
        if stat.S_ISSOCK(os.stat(socket_path).st_mode):
            os.remove(socket_path)
    except OSError:
        pass
```

- [ ] **Step 4: 运行 socket_util 测试确认通过**

Run: `python -m pytest updater/tests/test_socket_util.py -v`
Expected: POSIX 7 passed；Windows 4 passed + 3 skipped。

- [ ] **Step 5: 写 ipc 失败测试（TestClient 跨平台 + UDS 集成 POSIX only）**

Create `updater/tests/test_ipc.py`:

```python
"""ipc.py — envelope + /v1/status。

TestClient 测 HTTP 逻辑（跨平台）；UDS 端到端集成测试 POSIX only。
版本字段只在 envelope 顶层，data 不重复（spec §7.2）。
"""

import asyncio
import os
import sys

import pytest
from starlette.testclient import TestClient

from sakura_ai_updater import PROTOCOL_VERSION
from sakura_ai_updater.ipc import create_app
from sakura_ai_updater.state import JobState, UpdateStateStore, save_state


def test_status_returns_envelope_with_idle_state(tmp_path):
    app = create_app(str(tmp_path / "update-state.json"))
    client = TestClient(app)
    r = client.get("/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["updater_version"]  # 顶层，非空
    assert body["data"]["state"] == "idle"
    assert body["data"]["has_active_job"] is False
    assert body["data"]["active_job_id"] is None
    # data 不重复版本字段（envelope 顶层独有）
    assert "protocol_version" not in body["data"]
    assert "updater_version" not in body["data"]


def test_status_reflects_active_job(tmp_path):
    """state 文件有 active job → /v1/status 反映（每次读最新 state，非启动快照）。"""
    state_path = str(tmp_path / "update-state.json")
    save_state(
        state_path,
        UpdateStateStore(
            active_job_id="upd_001",
            current_job=JobState(
                job_id="upd_001", deployment="image", state="downloading"
            ),
        ),
    )
    app = create_app(state_path)
    client = TestClient(app)
    r = client.get("/v1/status")
    body = r.json()
    assert body["data"]["has_active_job"] is True
    assert body["data"]["active_job_id"] == "upd_001"
    assert body["data"]["deployment"] == "image"
    assert body["data"]["state"] == "downloading"


def test_health_returns_envelope(tmp_path):
    app = create_app(str(tmp_path / "update-state.json"))
    client = TestClient(app)
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["data"]["ok"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="UDS is POSIX-only")
@pytest.mark.asyncio
async def test_serve_over_real_uds(tmp_path):
    """端到端 UDS：起 uvicorn 监听 unix socket，httpx UDS transport 连。"""
    import httpx
    import uvicorn

    socket_path = str(tmp_path / "updater.sock")
    state_path = str(tmp_path / "update-state.json")
    app = create_app(state_path)
    config = uvicorn.Config(app, uds=socket_path, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if os.path.exists(socket_path):
                break
            await asyncio.sleep(0.05)
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://updater", timeout=2.0
        ) as client:
            r = await client.get("/v1/status")
            assert r.status_code == 200
            assert r.json()["protocol_version"] == PROTOCOL_VERSION
    finally:
        server.should_exit = True
        try:
            await task
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 6: 运行确认失败**

Run: `python -m pytest updater/tests/test_ipc.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sakura_ai_updater.ipc'`

- [ ] **Step 7: 实现 ipc.py**

Create `updater/src/sakura_ai_updater/ipc.py`:

```python
"""Updater IPC server — HTTP over UDS，协议 v1 body envelope（spec §7）。

Slice 3a：``envelope()`` helper + ``GET /v1/status`` + ``GET /v1/health``。
动作端点（check / preflight / update / rollback / jobs）在 Slice 4 接入。

所有成功（2xx）响应经 ``envelope()`` 包成 ``{protocol_version, updater_version, data}``
（§7.2）。版本字段**只在 envelope 顶层**，``data`` 不重复（避免内外两份漂移）。错误响应
（4xx/5xx，如 Slice 4 的 409 Conflict）直接返回，不走 envelope——spec §7.5 的 409 用
``{error, job_id}`` 格式。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from sakura_ai_updater import PROTOCOL_VERSION, __version__
from sakura_ai_updater.state import UpdateStateStore, load_state


def envelope(data: dict, status_code: int = 200) -> JSONResponse:
    """包 body envelope（spec §7.2）。成功响应统一用此；版本字段只在顶层。"""
    return JSONResponse(
        {
            "protocol_version": PROTOCOL_VERSION,
            "updater_version": __version__,
            "data": data,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def create_app(state_path: str) -> FastAPI:
    """构建 updater IPC app。

    Args:
        state_path: ``update-state.json`` 路径。``/v1/status`` 每次请求读最新 state
            （更新过程中 state 文件由 Slice 4 ImageAdapter 写入，非启动快照）。
            state 文件损坏时 ``load_state`` fail-closed 抛异常 → 500（不返回假数据）。
    """
    app = FastAPI(title="Sakura AI Updater", version=__version__)
    app.state.state_path = state_path

    @app.get("/v1/status")
    async def get_status() -> JSONResponse:
        """当前 updater 状态 + 是否有进行中的 job（spec §7.3）。"""
        store: UpdateStateStore = load_state(app.state.state_path)
        job = store.current_job
        has_active = (
            store.active_job_id is not None
            and job is not None
            and not job.is_terminal()
        )
        return envelope(
            {
                "state": job.state if job else "idle",
                "has_active_job": has_active,
                "active_job_id": store.active_job_id,
                "deployment": job.deployment if job else None,
            }
        )

    @app.get("/v1/health")
    async def health() -> JSONResponse:
        """健康检查（liveness）。"""
        return envelope({"ok": True})

    return app
```

- [ ] **Step 8: 实现 __main__.py（单 try/finally：flock + socket + reconcile + uvicorn）**

Create `updater/src/sakura_ai_updater/__main__.py`:

```python
"""sakura_ai_updater 入口 — dev 模式 ``python -m sakura_ai_updater --serve``。

Slice 3a：``--serve`` 在**单个 try/finally** 内完成 flock → socket 准备 → reconcile →
uvicorn，finally 清 socket + 释放 flock（覆盖 load/reconcile/save/create_app/Config 全部
异常路径）。Slice 3c PyInstaller 二进制模式同样走此入口。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sakura_ai_updater import __version__
from sakura_ai_updater.ipc import create_app
from sakura_ai_updater.locks import (
    LockBusyError,
    acquire_process_lock,
    release_process_lock,
)
from sakura_ai_updater.socket_util import cleanup_owned_socket, prepare_socket_path
from sakura_ai_updater.state import load_state, reconcile_interrupted_job, save_state

DEFAULT_SOCKET_PATH = "/run/sakura-ai/updater.sock"
DEFAULT_STATE_DIR = ".deploy/updater"


def serve(socket_path: str, state_path: str, lock_path: str) -> None:
    """启动 updater daemon：flock → socket 准备 → reconcile → uvicorn UDS。

    资源生命周期：lock_fd 与 socket 的清理覆盖全部步骤（含 load/reconcile/save/
    create_app/Config 异常路径）。Python 3.12 的 ``create_unix_server`` 无 cleanup_socket
    （3.13 才加），且直接用 ``Server.serve()`` 绕过 ``uvicorn.run()`` wrapper 的 socket
    清理——故 updater 经 socket_util 自管 socket 文件。
    """
    # 1. 进程唯一性（OS-level flock，§7.5 第一层锁）
    try:
        lock_fd = acquire_process_lock(lock_path)
    except LockBusyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        # 2. socket 准备（live/stale 真检测；父目录须存在，3b bootstrap）
        prepare_socket_path(socket_path)

        # 3. 崩溃恢复（§7.6 6 invariant）：中断/stale-gate job 处理 + 清 active_job_id
        store = load_state(state_path)
        store, changed = reconcile_interrupted_job(store)
        if changed:
            save_state(state_path, store)
            job_id = store.current_job.job_id if store.current_job else "?"
            print(
                f"WARN: reconciled job {job_id} (cleared stale active_job_id)",
                file=sys.stderr,
            )

        # 4. 启动 UDS server
        import uvicorn

        app = create_app(state_path)
        config = uvicorn.Config(app, uds=socket_path, log_level="info")
        server = uvicorn.Server(config)
        print(f"sakura-ai-updater {__version__} listening on {socket_path}")
        asyncio.run(server.serve())
    finally:
        # 5. 清理自己拥有的 socket + 释放 flock（覆盖上面任何步骤的异常）
        cleanup_owned_socket(socket_path)
        release_process_lock(lock_fd)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sakura_ai_updater")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--serve", action="store_true", help="run as UDS daemon")
    parser.add_argument("--socket-path", default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    parser.add_argument(
        "--lock-path",
        default=None,
        help="default: <state-dir>/updater.lock",
    )
    args = parser.parse_args(argv)

    if not args.serve:
        parser.error("no action specified; use --serve")

    state_path = os.path.join(args.state_dir, "update-state.json")
    lock_path = args.lock_path or os.path.join(args.state_dir, "updater.lock")
    serve(args.socket_path, state_path, lock_path)


if __name__ == "__main__":
    main()
```

- [ ] **Step 9: 运行全部 Task 3 测试确认通过**

Run: `python -m pytest updater/tests/test_socket_util.py updater/tests/test_ipc.py -v`
Expected:
- POSIX（WSL/Linux）：socket_util 7 + ipc 4（含 UDS 集成）= 11 passed。
- Windows：socket_util 4 passed + 3 skipped；ipc 3 passed + 1 skipped。

- [ ] **Step 10: ruff 检查**

Run: `python run_ruff.py --check updater/src/sakura_ai_updater/socket_util.py updater/src/sakura_ai_updater/ipc.py updater/src/sakura_ai_updater/__main__.py updater/tests/test_socket_util.py updater/tests/test_ipc.py`
Expected: 无错误。

- [ ] **Step 11: 手动验证 --serve（WSL/Linux，端到端 UDS + lifecycle + 进程唯一性）**

```bash
# 终端 A：dev 模式启动 updater（独立临时目录，不污染 .deploy；先 mkdir 因 3a 不创建父目录）
mkdir -p /tmp/updater-test
python -m sakura_ai_updater --serve \
  --socket-path /tmp/updater-test/updater.sock \
  --state-dir /tmp/updater-test
# 期望：输出 "sakura-ai-updater 0.1.0 listening on /tmp/updater-test/updater.sock"

# 终端 B：curl over UDS 验证 envelope（版本字段只在顶层）
curl --unix-socket /tmp/updater-test/updater.sock http://localhost/v1/status
# 期望：{"protocol_version":1,"updater_version":"0.1.0","data":{"state":"idle","has_active_job":false,"active_job_id":null,"deployment":null}}

curl --unix-socket /tmp/updater-test/updater.sock http://localhost/v1/health
# 期望：{"protocol_version":1,"updater_version":"0.1.0","data":{"ok":true}}

# 进程唯一性：终端 C 再起一份（相同 state-dir → 同一 lock 文件），必须退出码 1
python -m sakura_ai_updater --serve --socket-path /tmp/x.sock --state-dir /tmp/updater-test
# 期望：stderr "ERROR: another updater process holds the lock" + 退出码 1

# socket lifecycle：Ctrl-C 终止终端 A 后，/tmp/updater-test/updater.sock 应被清理
ls /tmp/updater-test/updater.sock
# 期望：No such file or directory（finally 已 cleanup_owned_socket）

# 父目录须存在：不 mkdir 直接指向不存在的目录 → SocketPathError 退出
python -m sakura_ai_updater --serve --socket-path /tmp/no-such-dir/updater.sock --state-dir /tmp/updater-test
# 期望：启动失败（parent directory does not exist）
```

> Windows 开发机无法跑此步（fcntl/UDS）；标 DONE_WITH_CONCERNS 并注明"待 WSL/CI 验证"，或直接在 WSL 跑。reconcile 的手动验证（伪造中断 state 文件后重启）由 Task 2 的 8 条单测覆盖；live socket 拒启由 test_prepare_refuses_live_socket 覆盖。

- [ ] **Step 12: 暂存变更（不提交）**

```bash
git add updater/src/sakura_ai_updater/socket_util.py updater/src/sakura_ai_updater/ipc.py updater/src/sakura_ai_updater/__main__.py updater/tests/test_socket_util.py updater/tests/test_ipc.py
```

**建议 commit 信息（待用户授权）：** `feat(updater): socket lifecycle (live/stale probe) + UDS IPC (envelope v1, /v1/status) + --serve entrypoint`

---

## Task 4: backend UDS client + /version/info updater 连接状态

**Files:**
- Create: `backend/services/updater_client.py`
- Modify: `backend/core/config.py`（加 `sakura_updater_socket_path`）
- Modify: `backend/webui/routes/version.py`（`build_version_info` 加 `updater_info`）
- Modify: `backend/webui/templates/version_manager.html`（部署卡 Host Updater 行）
- Create: `tests/test_updater_client.py`
- Modify: `tests/test_version_info.py`

> **本 task 只 import Task 4 用到的：`UpdaterClient`、`is_valid_v1_envelope`、`get_settings`。不引入 Slice 4 的 update 端点逻辑。**

- [ ] **Step 1: 写 UpdaterClient 失败测试（shape 跨平台 + 连不上跨平台 + 连得上 POSIX + malformed JSON POSIX）**

Create `tests/test_updater_client.py`:

```python
"""UpdaterClient — backend → updater UDS client + envelope shape 校验。

shape 校验（纯函数）+ 连不上 → None（跨平台）；连得上 / malformed JSON（POSIX only，起 UDS server）。
"""

import asyncio
import os
import sys

import pytest


def test_is_valid_v1_envelope_shapes():
    """envelope shape 校验（纯函数，跨平台，spec §7.2）。"""
    from backend.services.updater_client import is_valid_v1_envelope

    assert (
        is_valid_v1_envelope(
            {"protocol_version": 1, "updater_version": "0.1.0", "data": {}}
        )
        is True
    )
    # protocol_version 不匹配
    assert (
        is_valid_v1_envelope(
            {"protocol_version": 2, "updater_version": "0.1.0", "data": {}}
        )
        is False
    )
    # 缺 updater_version
    assert is_valid_v1_envelope({"protocol_version": 1, "data": {}}) is False
    # updater_version 非 str
    assert (
        is_valid_v1_envelope({"protocol_version": 1, "updater_version": 1, "data": {}})
        is False
    )
    # data 非 dict
    assert (
        is_valid_v1_envelope(
            {"protocol_version": 1, "updater_version": "x", "data": "not dict"}
        )
        is False
    )
    # 非 dict
    assert is_valid_v1_envelope("not dict") is False
    assert is_valid_v1_envelope(None) is False


@pytest.mark.asyncio
async def test_get_status_returns_none_when_unreachable():
    """连不存在的 socket → None（跨平台；/version/info 据此标 disconnected）。"""
    from backend.services.updater_client import UpdaterClient

    client = UpdaterClient(
        socket_path="/tmp/sakura-updater-not-exist.sock", timeout=1.0
    )
    result = await client.get_status()
    assert result is None


@pytest.mark.skipif(sys.platform == "win32", reason="UDS is POSIX-only")
@pytest.mark.asyncio
async def test_get_status_returns_envelope_when_connected(tmp_path):
    import httpx
    import uvicorn

    from backend.services.updater_client import UpdaterClient

    socket_path = str(tmp_path / "updater.sock")

    async def mini_app(scope, receive, send):
        """最小 ASGI app，回固定 envelope（不依赖 updater 包；版本只在顶层）。"""
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": (
                    b'{"protocol_version":1,"updater_version":"0.1.0",'
                    b'"data":{"state":"idle","has_active_job":false,'
                    b'"active_job_id":null,"deployment":null}}'
                ),
            }
        )

    config = uvicorn.Config(mini_app, uds=socket_path, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if os.path.exists(socket_path):
                break
            await asyncio.sleep(0.05)
        client = UpdaterClient(socket_path=socket_path, timeout=2.0)
        result = await client.get_status()
    finally:
        server.should_exit = True
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert result is not None
    assert result["protocol_version"] == 1
    assert result["updater_version"] == "0.1.0"
    assert result["data"]["state"] == "idle"


@pytest.mark.skipif(sys.platform == "win32", reason="UDS is POSIX-only")
@pytest.mark.asyncio
async def test_get_status_returns_none_for_malformed_json(tmp_path):
    """HTTP 200 + 半截 JSON → ValueError 捕获 → None（不当 connected，防 /version/info 500）。"""
    import httpx
    import uvicorn

    from backend.services.updater_client import UpdaterClient

    socket_path = str(tmp_path / "updater.sock")

    async def mini_app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            # 半截 JSON：resp.json() 会抛 ValueError（JSONDecodeError）
            {"type": "http.response.body", "body": b'{"protocol_version":1,'}
        )

    config = uvicorn.Config(mini_app, uds=socket_path, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if os.path.exists(socket_path):
                break
            await asyncio.sleep(0.05)
        client = UpdaterClient(socket_path=socket_path, timeout=2.0)
        result = await client.get_status()
    finally:
        server.should_exit = True
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert result is None
```

- [ ] **Step 2: 写 build_version_info updater 连接状态失败测试**

在 `tests/test_version_info.py` 末尾追加:

```python
def test_updater_connected_when_info_provided():
    info = build_version_info(
        "image",
        updater_info={
            "protocol_version": 1,
            "updater_version": "0.1.0",
            "data": {"state": "idle"},
        },
    )
    assert info["updater_connected"] is True
    assert info["updater_version"] == "0.1.0"
    assert info["updater_protocol_version"] == 1
    assert info["update_supported"] is False  # Slice 4 才启用 update
    assert info["update_unsupported_reason"] == "update_not_implemented"


def test_updater_disconnected_when_none():
    info = build_version_info("image")
    assert info["updater_connected"] is False
    assert info["updater_version"] is None
    assert info["updater_protocol_version"] is None
    assert info["update_unsupported_reason"] == "updater_not_connected"


def test_source_mode_updater_connected_still_unsupported():
    """source 模式即使 updater 连着，仍不支持更新（spec §5.2）。"""
    info = build_version_info(
        "source",
        updater_info={"protocol_version": 1, "updater_version": "0.1.0", "data": {}},
    )
    assert info["updater_connected"] is True
    assert info["update_supported"] is False
    assert info["update_unsupported_reason"] == "source_updater_not_available"
```

同时，在 `tests/test_version_info.py` 现有的 `test_image_mode_marks_updater_not_connected` 测试末尾追加断言:

```python
    assert info["updater_connected"] is False  # 新增：未连接
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_updater_client.py tests/test_version_info.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.services.updater_client'`（test_updater_client）；`build_version_info() got an unexpected keyword argument 'updater_info'`（test_version_info 新测试）。

- [ ] **Step 4: 实现 UpdaterClient（含 envelope shape 校验 + malformed JSON 降级）**

Create `backend/services/updater_client.py`:

```python
"""Backend → Host Updater UDS client（spec §7.1）。

容器内 backend 经 ``/run/sakura-ai/updater.sock`` 调 updater 受限 IPC。连不上（updater
未运行 / socket 不可达）、**envelope shape 非法**、**malformed JSON** 均返回 None——
``/version/info`` 据此标 ``updater_connected=false``（不当 connected，不使 /version/info 500）。

Slice 3a：只读 ``get_status``。update / preflight 等动作端点在 Slice 4。

性能：UDS 连接轻量，每次请求新建 transport；连不存在的 socket 立即 OSError（不像 TCP
timeout），故 ``/version/info``（navbar 周期性调）在 updater 未起时也快。
"""

from __future__ import annotations

import httpx

from backend.core.config import get_settings

# v1 协议常量（spec §7.2）。backend 不 import updater 包，故本地定义。
_PROTOCOL_VERSION = 1


def is_valid_v1_envelope(envelope: object) -> bool:
    """校验 body envelope shape（spec §7.2）。

    protocol_version==1 + updater_version is str + data is dict。非法返回 False
    （调用方据此降级为 disconnected，不把坏数据当 connected）。
    """
    if not isinstance(envelope, dict):
        return False
    if envelope.get("protocol_version") != _PROTOCOL_VERSION:
        return False
    if not isinstance(envelope.get("updater_version"), str):
        return False
    if not isinstance(envelope.get("data"), dict):
        return False
    return True


class UpdaterClient:
    """HTTP over UDS client。

    Args:
        socket_path: UDS 路径。None 时从 Settings 读（默认 /run/sakura-ai/updater.sock）。
        timeout: 请求超时（秒）。updater 响应慢时生效；连不上 socket 通常瞬间失败。
    """

    def __init__(self, socket_path: str | None = None, timeout: float = 2.0):
        self._socket_path = socket_path or get_settings().sakura_updater_socket_path
        self._timeout = timeout

    async def get_status(self) -> dict | None:
        """GET /v1/status。成功且 envelope shape 合法返回 envelope，否则 None。

        ValueError 捕获 malformed JSON（resp.json() decode 失败），防 /version/info 500。
        """
        transport = httpx.AsyncHTTPTransport(uds=self._socket_path)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://updater",
                timeout=self._timeout,
            ) as client:
                resp = await client.get("/v1/status")
                resp.raise_for_status()
                envelope = resp.json()  # malformed JSON → ValueError
        except httpx.HTTPError, OSError, ValueError:
            return None
        if not is_valid_v1_envelope(envelope):
            return None
        return envelope
```

- [ ] **Step 5: 加 Settings 字段**

Modify `backend/core/config.py`，在 `sakura_update_repo` 字段（约 line 87-91）之后加:

```python
    # Host Updater UDS 路径（spec §7.1）。容器内由 compose 挂载 /run/sakura-ai（Slice 3b）；
    # dev/源码模式可能指向宿主机路径或测试临时路径。
    sakura_updater_socket_path: str = Field(
        "/run/sakura-ai/updater.sock",
        description="Host Updater UDS 路径，backend 经此连 updater",
    )
```

- [ ] **Step 6: 改造 build_version_info（加 updater_info）**

Modify `backend/webui/routes/version.py`：

(a) 顶部 import 区，在 `from backend.services.update_checker import ...` 之后加:

```python
from backend.services.updater_client import UpdaterClient
```

(b) 整体替换 `build_version_info` 函数（签名加 `updater_info`，新增 updater 连接字段 + image 模式 reason 分支）:

```python
def build_version_info(
    deploy_mode: str,
    update_info: dict | None = None,
    updater_info: dict | None = None,
) -> dict:
    """构造版本与部署信息（纯函数）。

    Args:
        deploy_mode: 部署模式。非法值归一化为 "unknown"。
        update_info: 可选的更新检查缓存数据。None 时相关字段为 null。
        updater_info: 可选的 updater /v1/status envelope（连上时）。None 表示未连接。

    update_available 即时 derive：is_newer_version(__version__, latest)。
    - 无缓存（update_info=None）→ None
    - 有缓存且 latest 有值 → derive 布尔
    - 有缓存但 latest 为 None（空列表/失败无 last-known-good）→ False

    updater 连接状态：image 模式下 updater 已连但 update 尚未实现（Slice 4）→
    update_supported 仍 False，reason=update_not_implemented（明确"在线，功能开发中"）。
    updater_info 的版本字段取自 envelope 顶层（spec §7.2，data 不重复）。
    """
    mode = deploy_mode if deploy_mode in _VALID_MODES else "unknown"

    updater_connected = updater_info is not None
    updater_version = updater_info.get("updater_version") if updater_info else None
    updater_protocol_version = (
        updater_info.get("protocol_version") if updater_info else None
    )

    update_supported = False
    if mode == "source":
        reason = "source_updater_not_available"
    elif mode == "image":
        reason = (
            "update_not_implemented" if updater_connected else "updater_not_connected"
        )
    else:
        reason = "unknown_deployment"

    ui = update_info or {}
    latest = ui.get("latest_version")
    if ui:
        available = is_newer_version(__version__, latest) if latest else False
    else:
        available = None
    return {
        "current_version": __version__,
        "deployment_type": mode,
        "update_supported": update_supported,
        "update_unsupported_reason": reason,
        "update_available": available,
        "latest_version": latest,
        "last_checked": ui.get("last_checked"),
        "check_error": ui.get("check_error"),
        "updater_connected": updater_connected,
        "updater_version": updater_version,
        "updater_protocol_version": updater_protocol_version,
    }
```

(c) `get_version_info` 路由加 updater 连接查询（在 `build_version_info` 调用前）:

```python
@router.get("/version/info")
async def get_version_info(user: dict = Depends(require_auth)):
    """当前版本 + 部署模式 + 更新可用性 + updater 连接状态（所有登录用户，驱动 navbar badge）。"""
    mode = get_settings().sakura_deploy_mode or "unknown"
    update_info = await read_cache()
    updater_info = await UpdaterClient().get_status()
    info = build_version_info(mode, update_info, updater_info)
    return JSONResponse(
        info,
        headers={"Cache-Control": "no-store, max-age=0"},
    )
```

(d) `version_manager_page` 路由也传 updater_info（让版本管理器显示真实连接状态）——把其中的 `info = build_version_info(mode, update_info)` 改为:

```python
    info = build_version_info(mode, update_info, await UpdaterClient().get_status())
```

- [ ] **Step 7: 运行测试确认通过**

Run: `python -m pytest tests/test_version_info.py tests/test_updater_client.py -v`
Expected:
- POSIX：version_info 全 passed（含新增 3 + 更新 1）+ updater_client 4 passed（shape + 连不上 + 连得上 + malformed）。
- Windows：version_info 全 passed + updater_client 2 passed（shape + 连不上）+ 2 skipped（连得上 + malformed，UDS POSIX only）。

- [ ] **Step 8: ruff 检查**

Run: `python run_ruff.py --check backend/services/updater_client.py backend/core/config.py backend/webui/routes/version.py tests/test_updater_client.py tests/test_version_info.py`
Expected: 无错误。

- [ ] **Step 9: 版本管理器部署卡加 Host Updater 连接状态行**

Modify `backend/webui/templates/version_manager.html`，在"当前部署"卡的 `<dl>` 内，"更新支持"那个 `<div>` 之后加:

```html
      <div><dt class="text-gray-500 dark:text-gray-400">Host Updater</dt>
           <dd>{{ "已连接" if version_info.updater_connected else "未连接" }}
               {% if version_info.updater_connected and version_info.updater_version %}
               <span class="text-gray-400 text-xs font-mono">v{{ version_info.updater_version }}</span>
               {% endif %}</dd></div>
```

> Slice 3b 接入 compose 挂载后，生产环境此处会显示"已连接 v0.1.0"。dev 模式 updater 未起时显示"未连接"。

- [ ] **Step 10: 全量回归 + 暂存（不提交）**

```bash
python -m pytest tests/test_version_info.py tests/test_update_checker.py tests/test_updater_client.py updater/tests/ -v
python run_ruff.py --check backend/services/updater_client.py backend/core/config.py backend/webui/routes/version.py backend/webui/templates/version_manager.html tests/test_updater_client.py tests/test_version_info.py updater/src/sakura_ai_updater/
git add backend/services/updater_client.py backend/core/config.py backend/webui/routes/version.py backend/webui/templates/version_manager.html tests/test_updater_client.py tests/test_version_info.py
```
Expected: 测试全 passed（POSIX-only 用例在 Windows skip，非 fail）；ruff 无错。

**建议 commit 信息（待用户授权）：** `feat(version): backend UDS updater client (envelope-validated, malformed-JSON-safe) + /version/info connection state`

---

## Self-Review（计划自检）

**1. spec 覆盖（Slice 3a 范围内）：**

| spec 条目 | 落地位置 |
|---|---|
| §7.1 UDS 传输 + 路径 | Task 3 `__main__` `--socket-path` + Task 4 `UpdaterClient` UDS transport + config `sakura_updater_socket_path` |
| §7.2 body envelope（版本字段只在顶层） | Task 3 `ipc.envelope()` + test 断言 data 不含版本；Task 4 `is_valid_v1_envelope` 校验顶层 shape |
| §7.5 第一层锁（daemon process flock） | Task 2 `locks.acquire_process_lock`（含 subprocess 跨进程测试）+ Task 3 `__main__` 单 try/finally |
| §7.6 崩溃恢复 reconcile（6 条 invariant） | Task 2 `state.reconcile_interrupted_job`（返回 `(store, changed)`，含"无 gate 却声称执行中"corruption）+ Task 3 `__main__` changed 时 save |
| §8.4 状态持久化（wrapper + error_code + fail-closed + dir fsync） | Task 1 `save_state`（atomic + `_fsync_directory`）+ `JobState.error_code` + `load_state` fail-closed（无 `os.path.exists`，`FileNotFoundError` 精确区分） |
| §16.1 项目结构 | Task 1 `pyproject.toml` + `__init__.py`；Task 3 `__main__.py`/`ipc.py`/`socket_util.py` |
| §5.2 source 模式 update_supported=false | Task 4 `build_version_info` |
| 3a/3b 边界（不创建 /run/sakura-ai） | Task 3 `prepare_socket_path` 父目录不存在 → SocketPathError（不 makedirs） |

**2. 占位符扫描：** 无 TBD/TODO；所有代码块完整可运行；reconcile 6 条 invariant + load fail-closed 分支 + socket live/stale 分支各有对应测试。

**3. 类型一致性：**
- `reconcile_interrupted_job(store) -> tuple[UpdateStateStore, bool]`：Task 2 定义、8 处测试、`__main__`（解包 `store, changed`）一致。
- `acquire_process_lock(path) -> int` / `release_process_lock(fd)`：Task 2 定义、测试（含 subprocess）、`__main__` 一致。
- `prepare_socket_path(path)` / `cleanup_owned_socket(path)` / `SocketPathError`：Task 3 socket_util 定义、7 测试（含 live socket 拒启）、`__main__` 一致。
- `create_app(state_path) -> FastAPI`：Task 3 定义、测试（TestClient + UDS 集成）、`__main__` 一致。
- `is_valid_v1_envelope(envelope) -> bool` / `UpdaterClient.get_status() -> dict | None`：Task 4 定义、4 测试、`version.py` 调用一致。
- `build_version_info(deploy_mode, update_info=None, updater_info=None)`：Task 4 签名，新增字段在函数、测试、`version_manager.html` 三处一致。
- `PROTOCOL_VERSION = 1`（updater）/ `_PROTOCOL_VERSION = 1`（backend 本地）一致。
- `TERMINAL_STATES = {"success","failed","rolled_back"}`：`JobState.is_terminal()` 与 reconcile terminal 判断一致；`INTERRUPTED` = `state="failed"` + `error_code=ERROR_CODE_INTERRUPTED`（非顶层 state）。
- `error_code` 字段：`JobState` 定义、save/load round-trip、reconcile 写入/保留、spec §8.4 四处一致。
- 异常族：`StateLoadError`（IO base）/ `StateCorruptionError`（content，subclass）/ `UnsupportedStateSchemaError`（schema，subclass）；`load_state` 与 reconcile 的 `raise` 一致。

**4. 范围检查：** 4 个 task 各自 green，线性依赖（1→2→3→4）。合并后交付"独立 updater 项目 + durable state（atomic+dir-fsync + fail-closed）+ flock（含跨进程）+ reconcile（6 invariant）+ socket lifecycle（live/stale probe）+ UDS IPC + backend 连接（shape + malformed JSON 降级）+ UI 显示连接状态"。**未夹带** ImageAdapter / update 动作端点（Slice 4）/ destructive `asyncio.Lock`（Slice 4）/ GID group + compose 挂载 + `/run/sakura-ai` 创建（3b）/ PyInstaller（3c）/ start.sh CLI（3b）。

**5. 两层锁交付边界：** 3a 只交付 §7.5 **第一层锁**（daemon process flock）+ persisted `active_job_id` gate。**第二层锁**（destructive task `asyncio.Lock` + 409 Conflict）属 Slice 4（无 destructive endpoint，无对象加锁）。

**6. 平台策略检查（测试函数计数）：**
- Windows 本地 green：Task 1 state 11（chmod skip）；Task 2 reconcile 8（locks skip）；Task 3 socket_util 4/7 + ipc TestClient 3/4；Task 4 version_info 全 + updater_client 2/4（shape + 连不上）。
- WSL/CI 补跑 green：Task 1 state 12（含 chmod）；Task 2 locks 4（含 subprocess）；Task 3 socket_util 3（stale/live/cleanup POSIX）+ ipc UDS 集成 1；Task 4 updater_client 2（连得上 + malformed POSIX）。

**v3 修订（第二轮审查反馈，已整合）：**
1. `load_state` 去 `os.path.exists` 预检：改 `try open` + `except FileNotFoundError`→空 store，`PermissionError`→`StateLoadError`（防 exists() 在 EACCES 误判 fail-open）+ chmod 测试。
2. reconcile 第 6 invariant：`active_job_id=null` + `current_job` 非 terminal → `StateCorruptionError`（无 gate 却声称执行中）；spec §7.6 同步为 6 条。
3. `prepare_socket_path` 不创建父目录：`/run/sakura-ai` bootstrap 属 3b；父目录缺失 → `SocketPathError`（不 makedirs）。
4. socket live/stale 真检测：AF_UNIX `connect` probe——connect 成功=live→拒启（防误配置 unlink live socket），ConnectionRefused=stale→unlink；test_prepare_refuses_live_socket 覆盖。
5. `UpdaterClient` malformed JSON 降级：`except (... ValueError)` 捕获 `resp.json()` decode 异常 → None（防 /version/info 500）+ malformed 测试。
6. `save_state` directory fsync：`os.replace` 后 `_fsync_directory`（POSIX；Windows 跳过）保证 active_job_id gate 跨掉电不丢。
7. Task 4 pytest 计数修正：updater_client 4 个 test function（shape + 连不上 + 连得上 POSIX + malformed POSIX），非"8 passed"。

**执行者重点：**
- Task 1 Step 1-4 顺序严格：`__init__.py` 必须在 `pip install -e` 之前。
- Task 2 `test_locks.py` 顶部 `fcntl = pytest.importorskip("fcntl")`，`from sakura_ai_updater.locks import ...` 在其之后（`# noqa: E402`）。
- Task 2 subprocess 测试：多行 `-c`（`\n` 分行），父进程持锁 → 子进程必 LockBusyError exit 3。
- Task 2 reconcile 第 6 invariant：`active_job_id is None` 分支内必须再查 `current_job` 是否非 terminal（corruption）。
- Task 3 `__main__.py` 顶部 `from ...locks import ...`（fcntl）——Windows 下 import 崩，预期；手动验证须 WSL。
- Task 3 `prepare_socket_path` **不 makedirs 父目录**；dev 手动 `mkdir -p /tmp/updater-test`，生产 `/run/sakura-ai` 由 3b 创建。
- Task 3 socket live/stale：connect probe 区分——**绝不 unlink live socket**（误配置时宁可拒启）。
- Task 3 Step 11 进程唯一性手动验证：第二个进程须相同 `--state-dir`（同一 lock 文件）。
- Task 4 `UpdaterClient.get_status` 的 except 三元组 `(httpx.HTTPError, OSError, ValueError)`：ValueError 是 malformed JSON 的关键补充。
- Task 4 `save_state` 的 `os.makedirs` 创建的是 **state 目录**（`.deploy/updater/`），非 socket 目录（`/run/sakura-ai`）——两者不同，勿混淆。

**后续 slice（本计划不含）：**
- **Slice 3b** DaemonBackend + start.sh CLI（`updater install/start/stop/status`）+ GID 9472 group 创建 + compose `group_add` + `/run/sakura-ai` 挂载与创建 + socket 权限 bootstrap + 端到端 backend 容器连 updater。
- **Slice 3c** PyInstaller amd64/arm64（老 glibc debian:bullseye）+ Release Asset 发布 + `updater install` 下载二进制 + SHA256 校验。
- **Slice 4** ImageAdapter + 状态机驱动 + destructive `asyncio.Lock`（§7.5 第二层锁 + 409 Conflict）+ `/v1/update` + `/v1/preflight` + `/v1/check` + `/v1/jobs/*` + manifest 门禁 + digest 具体化 + 版本验证 gate。
