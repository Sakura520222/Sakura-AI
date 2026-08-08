# Auto-Update P0 — Slice 3b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 host updater 在真实 Linux 宿主机上可靠运行并被 Web 容器通过只读挂载的 UDS 访问，同时保持生产镜像部署不依赖宿主机 Python。

**Architecture:** `DaemonBackend` 管理 daemon 生命周期，以 PID、`/proc/$pid/stat` starttime 和启动时记录的进程身份共同防御 PID reuse；`start()` 只有在 child 存活且 `/v1/health` 通过后才原子写 PID meta。Updater transport 在 host 进程中预绑定 UDS、设置 `0660 root:sakura-ai` 后将 listener 交给 Uvicorn；FastAPI `ipc.py` 保持纯 HTTP application。`start.sh` 使用 binary-first control-plane resolver，源码模式只能通过显式 dev override 启用。Web 容器仅以补充 GID 9472 和只读 bind mount 连接 UDS，不挂 Docker socket。

**Tech Stack:** Python 3.12+、FastAPI、Uvicorn、Unix domain socket、Linux `/proc`、Bash、Docker Compose、pytest。

**关联设计：** [2026-08-07-auto-update-design.md](../specs/2026-08-07-auto-update-design.md) §7.1、§11、§11.4、§16.4。

**前置：** Slice 3a 已实现 updater state/lock/socket lifecycle、HTTP-over-UDS app 和 backend `UpdaterClient`。

---

## 固定边界

- 生产 control plane 只能执行 `.deploy/updater/sakura-ai-updater`，宿主机不得依赖 Python。
- `SAKURA_UPDATER_DEV=1` 时才允许 `${SAKURA_UPDATER_PYTHON:-python3} -m sakura_ai_updater`。
- `_resolve_executable()` 仅接受存在、是普通文件且具有 execute permission 的 binary。
- binary acquisition、PyInstaller、Release Asset 下载和 SHA256 校验属于 Slice 3c。
- `update apply`、ImageAdapter、destructive endpoint 和 destructive `asyncio.Lock` 属于 Slice 4。
- systemd 属于 P1；本 slice 不写 systemd unit 或 `@reboot` cron。
- 生产 `install/start` 以 root 运行；权限不足必须抛 `PrivilegeError` 并明确提示 `run as root or sudo`。
- Web 容器不得挂 `/var/run/docker.sock`，`/run/sakura-ai` 必须只读挂载。
- 不自主提交；子代理不得 commit 或 push。

## File Structure

| 文件 | 动作 | 单一职责 |
|---|---|---|
| `updater/src/sakura_ai_updater/backends/__init__.py` | Create | backend package marker |
| `updater/src/sakura_ai_updater/backends/daemon.py` | Create | daemon lifecycle、readiness、PID identity、host bootstrap |
| `updater/src/sakura_ai_updater/__main__.py` | Modify | `backend` CLI 与预绑定 listener 的 serve lifecycle |
| `updater/src/sakura_ai_updater/socket_util.py` | Modify | UDS path 检查、预绑定、ownership/mode、owned cleanup |
| `updater/tests/test_daemon_backend.py` | Create | lifecycle、readiness、PID reuse、bootstrap 单测 |
| `updater/tests/test_socket_util.py` | Modify | 预绑定 socket 权限和失败清理测试 |
| `updater/tests/test_ipc.py` | Modify | Uvicorn external listener 集成测试 |
| `start.sh` | Modify | binary-first updater CLI 和 self-healing entry points |
| `tests/test_start_sh_updater.sh` | Create | resolver、ensure、status 行为测试 |
| `docker/docker-compose.yml` | Modify | Web supplemental GID + read-only UDS bind mount |
| `docker/docker-compose.prod.yml` | Modify | Web supplemental GID + read-only UDS bind mount |
| `tests/test_compose_updater_mount.py` | Create | 精确验证 Web service mount/group/security invariant |
| `README.md` / `README_EN.md` | Modify | updater 运维入口、root 要求和安全边界 |

---

## Task 1: DaemonBackend 生命周期、readiness 与 PID identity

**Files:**
- Create: `updater/src/sakura_ai_updater/backends/__init__.py`
- Create: `updater/src/sakura_ai_updater/backends/daemon.py`
- Modify: `updater/src/sakura_ai_updater/__main__.py`
- Create: `updater/tests/test_daemon_backend.py`

- [ ] **Step 1: 写 PID identity 与 executable resolver 的失败测试**

测试至少覆盖：

```python
def test_is_running_accepts_dev_module_identity(...):
    backend._write_pid_meta(1234, "111", "sakura_ai_updater")
    # cmdline = python -m sakura_ai_updater --serve，starttime 相同
    assert backend.is_running() is True


def test_is_running_accepts_binary_identity(...):
    backend._write_pid_meta(1234, "111", "sakura-ai-updater")
    # argv[0] = /srv/.deploy/updater/sakura-ai-updater，starttime 相同
    assert backend.is_running() is True


def test_is_running_rejects_starttime_mismatch(...):
    # 相同 PID/identity，不同 starttime
    assert backend.is_running() is False


def test_resolve_executable_rejects_non_executable_binary(...):
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o644)
    with pytest.raises(UpdaterNotInstalledError):
        backend._resolve_executable()
```

同时覆盖无 meta、PID dead、cmdline identity mismatch、binary 优先于 dev override，以及 dev override 返回 `[sys.executable, "-m", "sakura_ai_updater"]`。

- [ ] **Step 2: 运行 resolver/identity 测试并确认 RED**

Run:

```bash
python -m pytest updater/tests/test_daemon_backend.py -k "running or executable" -v
```

Expected: import 或行为失败；失败原因必须是 backend 尚不存在或不支持 binary identity / execute permission，而不是测试语法错误。

- [ ] **Step 3: 实现 PID meta 和统一 process identity helper**

`daemon.py` 必须定义：

```python
DEFAULT_BINARY_NAME = "sakura-ai-updater"
DEFAULT_SOCKET_PATH = "/run/sakura-ai/updater.sock"
DEFAULT_RUN_DIR = "/run/sakura-ai"
DEFAULT_GID = 9472
DEFAULT_GROUP = "sakura-ai"
DEFAULT_STARTUP_TIMEOUT = 5.0


class UpdaterNotInstalledError(RuntimeError): ...
class UpdaterStartError(RuntimeError): ...
class GIDConflictError(RuntimeError): ...
class PrivilegeError(RuntimeError): ...


def _read_proc_cmdline(pid: int) -> tuple[str, ...]:
    # 读取 NUL-separated argv；失败返回空 tuple。
    ...


def _read_proc_starttime(pid: int) -> str | None:
    # 去掉 pid/comm 后 fields[19] 即 proc field 22 starttime。
    ...


def _matches_identity(argv: tuple[str, ...], identity: str) -> bool:
    if identity == "sakura_ai_updater":
        return len(argv) >= 3 and argv[1:3] == ("-m", "sakura_ai_updater")
    if identity == "sakura-ai-updater":
        return bool(argv) and os.path.basename(argv[0]) == identity
    return False


def _is_same_process(pid: int, starttime: str, identity: str) -> bool:
    return (
        _pid_alive(pid)
        and _read_proc_starttime(pid) == starttime
        and _matches_identity(_read_proc_cmdline(pid), identity)
    )
```

PID meta 必须是：

```json
{"pid": 1234, "starttime": "123456", "identity": "sakura-ai-updater"}
```

`_write_pid_meta` 拒绝空 `starttime`/`identity`，采用 temp file、`fsync()`、`os.replace()` 原子写。`is_running()` 只调用 `_is_same_process()`；损坏或字段不完整的 meta 返回 false。

Executable resolver：

```python
if os.path.isfile(self.binary_path) and os.access(self.binary_path, os.X_OK):
    return [self.binary_path]
if os.environ.get("SAKURA_UPDATER_DEV") == "1":
    return [sys.executable, "-m", "sakura_ai_updater"]
raise UpdaterNotInstalledError(...)
```

- [ ] **Step 4: 写 startup readiness 的失败测试**

测试至少覆盖：

```python
def test_start_fails_when_child_exits_immediately(...): ...
def test_start_does_not_write_meta_before_ready(...): ...
def test_start_fails_when_starttime_unavailable(...): ...
def test_start_writes_meta_only_after_health_ready(...): ...
def test_start_requires_root(...): ...
```

Fake child 必须实现 `pid`、`poll()`、`terminate()`、`wait()` 和 `kill()`。测试通过 monkeypatch 控制 `_read_proc_starttime`、`_health_ready`、`_is_same_process` 和 monotonic clock；断言失败路径无 PID meta，成功路径 meta 含完整三字段。

- [ ] **Step 5: 运行 readiness 测试并确认 RED**

```bash
python -m pytest updater/tests/test_daemon_backend.py -k "start_" -v
```

Expected: `UpdaterStartError`/readiness 行为尚未实现导致失败。

- [ ] **Step 6: 实现 health gate 和失败 child 清理**

`_health_ready(socket_path, timeout)` 使用标准库 `socket.AF_UNIX` 发出：

```http
GET /v1/health HTTP/1.1
Host: updater
Connection: close


```

仅 HTTP 200 视为 ready。`start()` 顺序必须是：

```text
is_running true → 幂等返回
require root
resolve executable + identity
Popen(start_new_session=True, log redirect)
循环至 startup_timeout：
  child.poll() 必须仍为 None
  /proc starttime 必须取得非空值且保持一致
  child process identity 必须匹配
  /v1/health 必须返回 HTTP 200
全部成立 → 原子写 PID meta → 返回成功
```

child 提前退出、starttime 一直不可用、identity 变化或超时：不写 meta；只通过该 `Popen` 实例 `terminate()` / bounded `wait()` / `kill()` 清理 child；抛 `UpdaterStartError` 并带 log path。

- [ ] **Step 7: 写并验证 stop 全程 PID reuse 防御**

新增：

```python
def test_stop_never_signals_initial_identity_mismatch(...): ...
def test_stop_does_not_sigkill_reused_pid_after_sigterm(...): ...
def test_stop_sigkills_only_when_same_process_survives_timeout(...): ...
```

实现必须保存原始 `pid/starttime/identity`，等待循环和最终 SIGKILL 每次均调用 `_is_same_process(pid, expected_starttime, expected_identity)`。身份变化等价于原 updater 已退出，绝不再发信号。

- [ ] **Step 8: 加 `backend` CLI 分发**

`__main__.py` 的 `main(argv)` 在原 `--serve` parser 前识别：

```text
backend install|start|stop|status|is-running
--state-dir
--socket-path
--binary-path
--startup-timeout
```

`status` 输出 JSON；`is-running` 用退出码；已知 backend 异常输出单行 `ERROR: ...` 并退出 1，不打印 traceback。原 `--serve` 入口必须保持兼容。

- [ ] **Step 9: 验证 Task 1**

```bash
python -m pytest updater/tests/test_daemon_backend.py -v
python run_ruff.py --check updater/src/sakura_ai_updater/backends/daemon.py updater/src/sakura_ai_updater/__main__.py updater/tests/test_daemon_backend.py
```

Expected: 全部通过；不提交。

---

## Task 2: Host bootstrap 与 UDS 预绑定 transport lifecycle

**Files:**
- Modify: `updater/src/sakura_ai_updater/backends/daemon.py`
- Modify: `updater/src/sakura_ai_updater/socket_util.py`
- Modify: `updater/src/sakura_ai_updater/__main__.py`
- Modify: `updater/tests/test_daemon_backend.py`
- Modify: `updater/tests/test_socket_util.py`
- Modify: `updater/tests/test_ipc.py`

- [ ] **Step 1: 写双向 group/GID conflict 和 privilege RED 测试**

覆盖四种 NSS 状态：

```python
def test_ensure_group_creates_when_name_and_gid_absent(...): ...
def test_ensure_group_is_idempotent_when_name_and_gid_match(...): ...
def test_ensure_group_rejects_gid_owned_by_other_name(...): ...
def test_ensure_group_rejects_name_with_other_gid(...): ...
def test_install_requires_root(...): ...
def test_ensure_run_dir_uses_os_chown_root_and_expected_gid(...): ...
```

`ensure_run_dir` 测试 monkeypatch `os.chown`，断言 `(path, uid, gid) == (run_dir, 0, 9472)`；不得断言 subprocess `chown`。

- [ ] **Step 2: 运行 bootstrap 测试确认 RED**

```bash
python -m pytest updater/tests/test_daemon_backend.py -k "group or run_dir or privilege or install" -v
```

Expected: helper 不存在或行为不完整导致失败。

- [ ] **Step 3: 实现 root privilege、双向 group lookup 和 run dir**

`install()` 和生产 `start()` 调用 `_require_root(action)`；权限不足抛：

```text
<action> requires root privileges; run as root or sudo
```

`ensure_group()` 分别执行：

```bash
getent group 9472
getent group sakura-ai
```

判定：

```text
GID 9472 → other name            => GIDConflictError
name sakura-ai → GID != 9472      => GIDConflictError
二者都不存在                       => groupadd -g 9472 sakura-ai
name=sakura-ai 且 GID=9472         => 幂等成功
```

`getent` 的“未找到”与命令执行失败必须区分；`groupadd` 失败传播为明确 bootstrap error。`ensure_run_dir()` 使用：

```python
os.makedirs(self.run_dir, exist_ok=True)
os.chown(self.run_dir, 0, self.gid)
os.chmod(self.run_dir, 0o770)
```

- [ ] **Step 4: 写 UDS pre-bind RED 测试**

`test_socket_util.py` 新增 POSIX 测试：

```python
def test_bind_socket_listener_sets_owner_mode_before_return(...): ...
def test_bind_socket_listener_cleans_socket_when_chown_fails(...): ...
```

`test_ipc.py` 将真实 UDS 集成改为 external listener：先由 helper bind/listen，再 `server.serve(sockets=[listener])`，连接 `/v1/status` 成功。

`__main__.serve` 测试（放 `test_ipc.py` 或新测试）必须断言：

```text
uvicorn.Config 不含 uds=
uvicorn.Server.serve 收到 sockets=[listener]
```

- [ ] **Step 5: 实现 pre-bound listener**

`socket_util.py` 新增：

```python
def bind_socket_listener(
    socket_path: str,
    *,
    uid: int = 0,
    gid: int = 9472,
    mode: int = 0o660,
) -> socket.socket:
    prepare_socket_path(socket_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(socket_path)
        os.chown(socket_path, uid, gid)
        os.chmod(socket_path, mode)
        listener.listen(socket.SOMAXCONN)
        return listener
    except BaseException:
        listener.close()
        cleanup_owned_socket(socket_path)
        raise
```

ownership 和 mode 必须在 `listen()` 返回及 Uvicorn 接受连接前设置；错误不得吞掉。

`serve()` 顺序：

```text
flock
prepare/reconcile state
create FastAPI app
bind_socket_listener(socket_path, uid=0, gid=socket_gid, mode=0660)
uvicorn.Config(app, log_level="info")
asyncio.run(server.serve(sockets=[listener]))
finally: close listener → cleanup_owned_socket → release flock
```

`--serve` parser 新增 `--socket-gid`，DaemonBackend `_serve_args()` 传自身 GID。`ipc.create_app(state_path)` 保持纯 HTTP app，不增加 socket path/group/mode 或 lifespan chmod。

- [ ] **Step 6: 验证 Task 2**

```bash
python -m pytest updater/tests/test_daemon_backend.py updater/tests/test_socket_util.py updater/tests/test_ipc.py -v
python run_ruff.py --check updater/src/sakura_ai_updater/ updater/tests/
```

POSIX 额外验证：

```bash
python - <<'PY'
import os
from sakura_ai_updater.backends.daemon import _read_proc_starttime
value = _read_proc_starttime(os.getpid())
assert value and value.isdigit(), value
print(value)
PY
```

Expected: 全部通过；`fields[19]` 算法不改。

---

## Task 3: `start.sh` binary-first updater CLI 与 status 自愈

**Files:**
- Modify: `start.sh`
- Create: `tests/test_start_sh_updater.sh`

- [ ] **Step 1: 写 control-plane resolver RED 测试**

测试 source `start.sh` 后覆盖临时 `UPDATER_STATE_DIR`，至少验证：

```text
可执行 binary 存在                 => 直接执行 binary backend ...，不调用 Python
binary 不存在 + 无 dev override    => 返回 127，消息含 updater executable not installed
binary 不存在 + SAKURA_UPDATER_DEV=1 => 调 ${SAKURA_UPDATER_PYTHON} -m sakura_ai_updater backend ...
不可执行 binary                     => 不作为 production executable
```

fake binary / fake Python 把 argv 写入临时日志，测试精确断言参数顺序。

- [ ] **Step 2: 运行 resolver 测试确认 RED**

```bash
bash tests/test_start_sh_updater.sh
```

Expected: `updater_backend` 尚不存在导致失败。

- [ ] **Step 3: 实现 binary-first resolver**

在 deployment state 初始化后定义：

```bash
UPDATER_STATE_DIR="$DEPLOY_DIR/updater"
UPDATER_BINARY="$UPDATER_STATE_DIR/sakura-ai-updater"
UPDATER_SOCKET_PATH="/run/sakura-ai/updater.sock"

updater_backend() {
    local binary="${UPDATER_BINARY:-$UPDATER_STATE_DIR/sakura-ai-updater}"
    if [[ -x "$binary" ]]; then
        "$binary" backend "$@"
    elif [[ "${SAKURA_UPDATER_DEV:-0}" == "1" ]]; then
        "${SAKURA_UPDATER_PYTHON:-python3}" -m sakura_ai_updater backend "$@"
    else
        fail "updater executable not installed: $binary"
        return 127
    fi
}
```

生产路径禁止无条件执行 `python`、`python3` 或 `sys.executable`。

- [ ] **Step 4: 写 ensure/status RED 测试**

覆盖：

```text
is-running 成功               => ensure 不调用 install/start
is-running 失败               => ensure 依次调用 install、start
install/start 失败             => ensure 返回非 0，Sakura AI status 仍继续输出
cmd_status 首先调用 ensure     => daemon 停止时尝试恢复
cmd_updater 精确透传 action/options
```

- [ ] **Step 5: 实现 CLI、自愈挂载点和帮助**

实现 `cmd_updater` 支持：

```text
install | start | stop | status | is-running
```

`main()` 在 flag parser 前分发位置子命令 `updater`。`ensure_updater_running()` 先 `is-running`，否则 `install` 后 `start`。

挂载点：

1. `build_runner()` health check 后、`set_phase "done"` 前，失败只 warning，不影响 Web 服务。
2. `cmd_status()` 开头必须调用 `ensure_updater_running || warn ...`，随后重新调用 `is-running` 输出 updater 状态，再执行原有构建状态输出。

帮助文本加入：

```text
updater [action]  管理 host updater daemon（生产 install/start 需 root；action 默认 status）
```

- [ ] **Step 6: 验证 Task 3**

```bash
bash -n start.sh
bash tests/test_start_sh_updater.sh
```

Expected: 语法和所有场景通过；grep 确认无 `@reboot`、无 production Python fallback。

---

## Task 4: Read-only compose mount 与真实 UDS E2E

**Files:**
- Modify: `docker/docker-compose.yml`
- Modify: `docker/docker-compose.prod.yml`
- Create: `tests/test_compose_updater_mount.py`

- [ ] **Step 1: 写精确 compose RED 测试**

对两个 compose 逐一解析 `services.web`，精确断言：

```python
assert "9472" in {str(value) for value in web["group_add"]}
mount = next(item for item in web["volumes"] if isinstance(item, dict) and item.get("target") == "/run/sakura-ai")
assert mount == {
    "type": "bind",
    "source": "/run/sakura-ai",
    "target": "/run/sakura-ai",
    "read_only": True,
}
```

另遍历所有 services 的 volumes，断言不存在 source/target `/var/run/docker.sock`。测试必须定位 `web` service，不能全文 grep 字符串。

- [ ] **Step 2: 运行 compose 测试确认 RED**

```bash
python -m pytest tests/test_compose_updater_mount.py -v
```

Expected: 缺少 `group_add` / mount 导致失败。

- [ ] **Step 3: 修改两个 compose**

两个 `web` service 均加入：

```yaml
group_add:
  - "9472"
volumes:
  - type: bind
    source: /run/sakura-ai
    target: /run/sakura-ai
    read_only: true
```

其余 volumes 保持不变；不得加入 Docker socket。

- [ ] **Step 4: 验证 compose render**

```bash
python -m pytest tests/test_compose_updater_mount.py -v
docker compose -f docker/docker-compose.yml config --quiet
docker compose -f docker/docker-compose.prod.yml config --quiet
```

Expected: 精确测试和 Compose schema 均通过。

- [ ] **Step 5: WSL/Linux host lifecycle E2E**

源码模式显式指定 WSL Python，且生产 bootstrap/start 通过 sudo：

```bash
export SAKURA_UPDATER_DEV=1
export SAKURA_UPDATER_PYTHON="$PWD/.venv/bin/python"
sudo --preserve-env=SAKURA_UPDATER_DEV,SAKURA_UPDATER_PYTHON \
  ./start.sh updater install
sudo --preserve-env=SAKURA_UPDATER_DEV,SAKURA_UPDATER_PYTHON \
  ./start.sh updater start
./start.sh updater status
stat -c '%a %U %G' /run/sakura-ai/updater.sock
```

Expected: status running；socket 为 `660 root sakura-ai`。

- [ ] **Step 6: Container → mounted UDS → updater E2E**

启动 Web 后，不使用未认证 `/version/info` curl；直接在容器内调用真实 client：

```bash
docker compose -f docker/docker-compose.yml exec -T web python - <<'PY'
import asyncio
import json
from backend.services.updater_client import UpdaterClient

result = asyncio.run(UpdaterClient().get_status())
print(json.dumps(result, ensure_ascii=False))
assert result is not None
assert result["protocol_version"] == 1
assert result["updater_version"]
PY
```

此步骤证明 container supplemental GID → read-only mount → UDS connect → HTTP envelope 全链路。`/version/info` 的 `updater_connected=true` 由已有 authenticated route integration test 验证；人工 E2E 必须携带认证 session/cookie，不能用 unauthenticated curl 假验收。

- [ ] **Step 7: 验证 stop 幂等和 PID safety**

```bash
sudo --preserve-env=SAKURA_UPDATER_DEV,SAKURA_UPDATER_PYTHON ./start.sh updater stop
sudo --preserve-env=SAKURA_UPDATER_DEV,SAKURA_UPDATER_PYTHON ./start.sh updater stop
./start.sh updater status
```

Expected: 第二次 stop 幂等；status 为 not running（注意 `cmd_status` 会尝试恢复，纯 daemon 状态用 `./start.sh updater status` 或 `is-running` 检查）。

- [ ] **Step 8: 全量回归**

```bash
python -m pytest updater/tests/ tests/test_updater_client.py tests/test_version_info.py tests/test_compose_updater_mount.py -v
python run_ruff.py --check updater/src/sakura_ai_updater/ updater/tests/ tests/test_compose_updater_mount.py
bash -n start.sh
bash tests/test_start_sh_updater.sh
```

Expected: 全部通过；不提交。

---

## Task 5: 同步运维文档（项目固定要求）

**Files:**
- Modify: `README.md`
- Modify: `README_EN.md`

- [ ] **Step 1: 中文 README 增加 host updater 运维说明**

说明：

```text
./start.sh updater install|start|stop|status
生产 binary 路径 .deploy/updater/sakura-ai-updater
install/start 需要 root 或 sudo
SAKURA_UPDATER_DEV=1 仅供源码开发
Web 仅只读挂载 /run/sakura-ai，不挂 docker.sock
```

- [ ] **Step 2: 英文 README 同步同等信息**

英文内容与中文语义一致，不增删行为承诺。

- [ ] **Step 3: 验证文档与实现一致**

搜索两个 README 中的命令、binary 路径、root 要求和 read-only UDS 安全边界，确认不存在“生产依赖 Python”或“挂 docker.sock”的表述。

---

## Self-Review

### 8 条 correctness 反馈映射

| # | 修订位置 |
|---|---|
| 1. 生产宿主机 Python 依赖 | Task 3 binary-first resolver；dev fallback 必须显式开启；Task 1 binary 检查 execute permission |
| 2. binary PID identity | Task 1 meta 增加 identity，同时支持 module 与 hyphenated binary identity |
| 3. lifespan 时序错误 | Task 2 移除 ipc lifespan chmod，host 预绑定并在交给 Uvicorn 前 chown/chmod/listen |
| 4. Popen 非 readiness | Task 1 child alive + valid starttime + identity + `/v1/health` gate，成功后才写 meta |
| 5. stop 后半程 PID reuse | Task 1 等待和 SIGKILL 前均比较原始 starttime + identity |
| 6. status 未自愈 | Task 3 `cmd_status()` 首先调用 `ensure_updater_running()`，失败不影响原状态输出 |
| 7. group/bootstrap | Task 2 name/GID 双向冲突、`os.chown` 测试、生产 root privilege model |
| 8. E2E 未认证 | Task 4 容器内直接调用 `UpdaterClient`；route 由 authenticated integration test 验证 |

### 安全增强

- 两个 compose 均使用 long syntax `read_only: true` 挂载 `/run/sakura-ai`。
- 测试精确检查 `services.web` 的 source、target、read_only 和 supplemental GID。
- 所有 services 均不得挂 `/var/run/docker.sock`。

### 已确认无需修改

`/proc/$pid/stat` 去掉 `pid` 和 parenthesized `comm` 后，`fields[19]` 对应 field 22 `starttime`；保留算法并在 POSIX 上跑真实 PID smoke test。

### 非目标

不包含 3c binary acquisition/PyInstaller，不包含 Slice 4 update apply，不包含 systemd/P1，也不新增网络下载或 Docker socket 权限。
