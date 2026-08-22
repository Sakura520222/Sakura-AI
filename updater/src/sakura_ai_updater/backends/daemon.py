"""DaemonBackend — host updater daemon 生命周期管理（spec §7.1、§11、§16.4）。

生命周期：``start()`` 仅在 child 存活 + 稳定 starttime + 身份匹配 + ``/v1/health``
HTTP 200 全部通过后，才原子写 PID meta（``daemon-meta.json``）。``stop()`` 全程
（初次、等待循环、最终 SIGKILL 前）以 pid/starttime/identity 三重校验防御 PID
reuse——身份变化即原进程已退出，绝不再发信号。``is_running()`` 只信完整 meta。

生产（binary）start/install 需要 root（``PrivilegeError``）；dev 源码模式
（``SAKURA_UPDATER_DEV=1`` + ``python -m sakura_ai_updater``）不需要。
``/proc`` 读取、UDS health probe、root 判定、Popen 全部经模块级 helper 间接，
测试跨 Windows monkeypatch 逐个替换。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

DEFAULT_BINARY_NAME = "sakura-ai-updater"
DEFAULT_SOCKET_PATH = "/run/sakura-ai/updater.sock"
DEFAULT_RUN_DIR = "/run/sakura-ai"
DEFAULT_GID = 9472
DEFAULT_GROUP = "sakura-ai"
DEFAULT_STARTUP_TIMEOUT = 5.0

# 与 __main__.py 的 DEFAULT_STATE_DIR 保持一致（backend CLI 显式传值，此处仅兜底）。
_DEFAULT_STATE_DIR = ".deploy/updater"

_PID_META_FILENAME = "daemon-meta.json"
_LOG_FILENAME = "updater.log"

# Windows 无 SIGKILL（updater 生产只跑 Linux）；None 时 stop 不发送 SIGKILL。
_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", None)

# identity 常量：dev module 与 production binary 两种进程身份（PID meta identity 字段）。
IDENTITY_DEV_MODULE = "sakura_ai_updater"
IDENTITY_BINARY = DEFAULT_BINARY_NAME


def _is_safe_executable(path: str, *, require_root_owner: bool) -> bool:
    """检查 updater executable inode 安全属性；拒绝 symlink 和共享写权限。

    Check the inode itself rather than following a symlink; production additionally
    requires root ownership so an untrusted host user cannot replace the binary.
    """
    try:
        file_stat = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(file_stat.st_mode):
        return False
    if file_stat.st_mode & 0o022:
        return False
    if require_root_owner and file_stat.st_uid != 0:
        return False
    return bool(file_stat.st_mode & 0o111)


class UpdaterNotInstalledError(RuntimeError):
    """updater 可执行文件不可用（binary 不存在/不可执行，且未开 dev override）。"""


class UpdaterStartError(RuntimeError):
    """start 失败：child 提前退出 / starttime 不可用 / identity 变化 / 超时未就绪。"""


class GIDConflictError(RuntimeError):
    """group name/GID 双向冲突（Task 2 bootstrap 使用；Task 1 定义契约）。"""


class PrivilegeError(RuntimeError):
    """权限不足（生产 install/start 必须 root）。"""


class UnsafeDeploymentPathError(RuntimeError):
    """生产 updater 的可执行文件或部署输入不在受信任的 root 路径中。"""


def _read_proc_cmdline(pid: int) -> tuple[str, ...]:
    """读取 ``/proc/<pid>/cmdline``（NUL-separated argv）。

    解析失败（进程已退出 / 无权限 / 非 POSIX）→ 空 tuple（fail-closed 于"身份不可知"）。
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return ()
    raw = raw.rstrip(b"\x00")
    if not raw:
        return ()
    return tuple(part.decode("utf-8", errors="replace") for part in raw.split(b"\x00"))


def _read_proc_starttime(pid: int) -> str | None:
    """读取 ``/proc/<pid>/stat`` 的 field 22（starttime）。

    comm 字段可含空格/括号（如 ``(cpu (2) heavy)``），故按**最后一个** ``)`` 截断；
    去掉 pid/comm 后，``fields[19]`` 即 field 22 starttime（22 - 前 3 个前缀字段）。
    读取失败或字段不足 → None（调用方按"不可用"处理，不猜测）。
    """
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None
    rparen = content.rfind(")")
    if rparen == -1:
        return None
    fields = content[rparen + 1 :].split()
    if len(fields) <= 19:
        return None
    return fields[19]


def _pid_alive(pid: int) -> bool:
    """pid 是否存活（signal 0 探测）。无权限等 OSError → False（fail-closed）。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError, OSError:
        return False
    return True


def _matches_identity(argv: tuple[str, ...], identity: str) -> bool:
    """argv 是否匹配记录在 meta 中的进程身份。

    - ``sakura_ai_updater``（dev）：只匹配 argv 中**精确相邻**的 ``("-m", "sakura_ai_updater")``
      （即 argv[1:3]），防止 ``-m sakura_ai_updater_x`` 或 ``python sakura_ai_updater`` 误配。
    - ``sakura-ai-updater``（production binary）：只匹配 ``argv[0]`` 的 basename。
    - 未知 identity 一律不匹配。
    """
    if identity == IDENTITY_DEV_MODULE:
        return len(argv) >= 3 and argv[1:3] == ("-m", IDENTITY_DEV_MODULE)
    if identity == IDENTITY_BINARY:
        return bool(argv) and os.path.basename(argv[0]) == IDENTITY_BINARY
    return False


def _is_same_process(pid: int, starttime: str, identity: str) -> bool:
    """pid 当前是否仍是记录中的原进程：alive + starttime 相同 + 身份匹配 三重校验。

    期望字段为空（meta 不完整）→ False。任何一重不满足即视为 PID reuse / 已退出。
    """
    if not starttime or not identity:
        return False
    return (
        _pid_alive(pid)
        and _read_proc_starttime(pid) == starttime
        and _matches_identity(_read_proc_cmdline(pid), identity)
    )


class DaemonBackend:
    """daemon 生命周期后端：start / stop / status / is-running + host bootstrap。

    Args:
        state_dir: PID meta 与 updater.log 所在目录（backend CLI 默认 .deploy/updater）。
        socket_path: UDS socket 路径（daemon 监听；health probe 目标）。
        binary_path: production binary 路径；缺省 <state_dir>/sakura-ai-updater。
        startup_timeout: start 的 readiness 总超时（秒），不硬编码 sleep。
        stop_timeout: stop 的 SIGTERM 等待窗口（秒），超时后才可能 SIGKILL。
        poll_interval: 轮询间隔（秒）；构造参数化，测试可调小避免慢。
        run_dir / gid / group: Task 2 bootstrap 参数（install 使用），Task 1 仅存储。
    """

    def __init__(
        self,
        state_dir: str = _DEFAULT_STATE_DIR,
        socket_path: str = DEFAULT_SOCKET_PATH,
        binary_path: str | None = None,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        stop_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        poll_interval: float = 0.1,
        run_dir: str = DEFAULT_RUN_DIR,
        gid: int = DEFAULT_GID,
        group: str = DEFAULT_GROUP,
        compose_file: str | None = None,
        deployment_env: str | None = None,
    ):
        # The daemon may outlive a checkout replacement. Freeze every child
        # filesystem argument before spawning so it never resolves state or
        # deployment paths through a deleted inherited working directory.
        self.state_dir = os.path.abspath(state_dir)
        self.socket_path = os.path.abspath(socket_path)
        self.binary_path = os.path.abspath(
            binary_path or os.path.join(self.state_dir, DEFAULT_BINARY_NAME)
        )
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout
        self.poll_interval = poll_interval
        self.run_dir = os.path.abspath(run_dir)
        self.gid = gid
        self.group = group
        # These paths are passed explicitly to the child daemon.  ``None`` keeps
        # backwards compatibility for callers that only exercise lifecycle
        # management; production bootstrap supplies absolute configured paths.
        self.compose_file = (
            os.path.abspath(compose_file) if compose_file is not None else None
        )
        self.deployment_env = (
            os.path.abspath(deployment_env) if deployment_env is not None else None
        )
        # Dev mode: socket 用当前用户 uid/gid（非 root 无法 chown 到 root:9472）
        if os.environ.get("SAKURA_UPDATER_DEV") == "1":
            self._socket_uid = getattr(os, "getuid", lambda: 0)()
            self._socket_gid = getattr(os, "getgid", lambda: 0)()
        else:
            self._socket_uid = 0
            self._socket_gid = gid

    @property
    def _pid_meta_path(self) -> str:
        return os.path.join(self.state_dir, _PID_META_FILENAME)

    @property
    def _log_path(self) -> str:
        return os.path.join(self.state_dir, _LOG_FILENAME)

    # ------------------------------------------------------------------ meta

    def _write_pid_meta(self, pid: int, starttime: str, identity: str) -> None:
        """原子写 PID meta（temp + fsync + os.replace）。

        拒绝空 starttime/identity（空身份记录比没有记录更危险——is_running 会
        因字段缺失而误判）。写失败清理 temp 后原样抛。
        """
        if not starttime or not identity:
            raise ValueError(
                f"non-empty starttime and identity required; "
                f"got starttime={starttime!r}, identity={identity!r}"
            )
        os.makedirs(self.state_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self.state_dir, prefix=".daemon-meta.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": pid, "starttime": starttime, "identity": identity}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._pid_meta_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _read_pid_meta(self) -> dict | None:
        """fail-closed 读 PID meta：不存在/损坏/字段类型或值非法 → None。

        只有完整三字段（pid:int, starttime:str 非空, identity:str 非空）才返回。
        """
        try:
            with open(self._pid_meta_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        pid = data.get("pid")
        starttime = data.get("starttime")
        identity = data.get("identity")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or not isinstance(starttime, str)
            or not isinstance(identity, str)
        ):
            return None
        if not starttime or not identity:
            return None
        return {"pid": pid, "starttime": starttime, "identity": identity}

    def _clear_pid_meta(self) -> None:
        try:
            os.remove(self._pid_meta_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass  # 清理尽力而为；不因清理失败掩盖 stop 结果

    # ------------------------------------------------------------- executable

    def _resolve_executable(self) -> tuple[list[str], str]:
        """解析启动命令与对应 identity（binary-first，spec §16.4）。

        - production binary 必须是 root-owned regular executable，且 group/other 不可写。
        - 否则仅 ``SAKURA_UPDATER_DEV=1`` 才允许 ``python -m sakura_ai_updater``
          （identity dev module）。
        - 否则 UpdaterNotInstalledError（生产绝不隐式 fallback 到 Python）。
        """
        if _is_safe_executable(self.binary_path, require_root_owner=True):
            return [self.binary_path], IDENTITY_BINARY
        if os.environ.get("SAKURA_UPDATER_DEV") == "1":
            return [sys.executable, "-m", "sakura_ai_updater"], IDENTITY_DEV_MODULE
        raise UpdaterNotInstalledError(
            f"updater executable not installed: {self.binary_path!r} "
            f"(install the binary or set SAKURA_UPDATER_DEV=1 for dev mode)"
        )

    def _serve_args(self) -> list[str]:
        """返回 child 进程所需的 ``--serve`` 参数。

        Existing argument order/meaning is stable; compose/deployment paths are
        appended only after the legacy socket/lock arguments.
        """
        args = [
            "--serve",
            "--socket-path",
            self.socket_path,
            "--state-dir",
            self.state_dir,
            "--lock-path",
            os.path.join(self.state_dir, "updater.lock"),
            "--socket-uid",
            str(self._socket_uid),
            "--socket-gid",
            str(self._socket_gid),
        ]
        if self.compose_file is not None:
            args.extend(["--compose-file", self.compose_file])
        if self.deployment_env is not None:
            args.extend(["--deployment-env", self.deployment_env])
        return args

    # ------------------------------------------------------------ privileges

    def _require_root(self, action: str) -> None:
        """生产 install/start 必须 root；不足抛 PrivilegeError（明确提示 sudo）。

        Windows 无 geteuid（updater 生产只跑 Linux）→ 视为 root，dev 测试不受影响。
        """
        geteuid = getattr(os, "geteuid", lambda: 0)
        if geteuid() != 0:
            raise PrivilegeError(
                f"{action} requires root privileges; run as root or sudo"
            )

    @staticmethod
    def _trusted_file(path: str, label: str, *, exact_mode: int | None = None) -> str:
        """验证 root daemon 将读取或执行的文件及其完整目录链。

        ``lstat`` 检查每一级以拒绝 symlink。文件必须 root-owned，且 group/other
        不可写；敏感部署状态还可要求精确 mode。所有父目录直到文件系统根都必须
        root-owned 且 group/other 不可写，避免普通用户通过 rename/replace 在校验后
        替换 Compose、deployment.env 或 updater binary。
        """

        absolute = os.path.abspath(path)
        try:
            file_stat = os.lstat(absolute)
        except OSError as exc:
            raise UnsafeDeploymentPathError(
                f"unsafe {label}: cannot lstat {absolute!r}: {exc}"
            ) from exc
        if not stat.S_ISREG(file_stat.st_mode):
            raise UnsafeDeploymentPathError(
                f"unsafe {label}: {absolute!r} must be a regular file, not a symlink"
            )
        if file_stat.st_uid != 0:
            raise UnsafeDeploymentPathError(
                f"unsafe {label}: {absolute!r} must be owned by root"
            )
        mode = stat.S_IMODE(file_stat.st_mode)
        if mode & 0o022:
            raise UnsafeDeploymentPathError(
                f"unsafe {label}: {absolute!r} must not be group/other writable"
            )
        if exact_mode is not None and mode != exact_mode:
            raise UnsafeDeploymentPathError(
                f"unsafe {label}: {absolute!r} must have mode {exact_mode:04o}"
            )

        directory = os.path.dirname(absolute)
        while True:
            try:
                directory_stat = os.lstat(directory)
            except OSError as exc:
                raise UnsafeDeploymentPathError(
                    f"unsafe {label} parent: cannot lstat {directory!r}: {exc}"
                ) from exc
            if not stat.S_ISDIR(directory_stat.st_mode):
                raise UnsafeDeploymentPathError(
                    f"unsafe {label} parent: {directory!r} must be a directory, not a symlink"
                )
            if directory_stat.st_uid != 0:
                raise UnsafeDeploymentPathError(
                    f"unsafe {label} parent: {directory!r} must be owned by root"
                )
            if stat.S_IMODE(directory_stat.st_mode) & 0o022:
                raise UnsafeDeploymentPathError(
                    f"unsafe {label} parent: {directory!r} must not be group/other writable"
                )
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
        return absolute

    def _validate_production_paths(self) -> None:
        """生产启动前冻结为已验证的绝对路径；开发模式不会调用。"""

        self.binary_path = self._trusted_file(self.binary_path, "updater binary")
        if self.compose_file is None and self.deployment_env is None:
            return
        if self.compose_file is None or self.deployment_env is None:
            raise UnsafeDeploymentPathError(
                "compose_file and deployment_env must be configured together"
            )
        self.compose_file = self._trusted_file(self.compose_file, "Compose file")
        self.deployment_env = self._trusted_file(
            self.deployment_env,
            "deployment.env",
            exact_mode=0o600,
        )

    # ------------------------------------------------------------- bootstrap

    def ensure_group(self) -> None:
        """host bootstrap：group name/GID 双向检测，二者都不存在时 groupadd 创建。

        ``getent group`` 返回码语义：0 = 记录存在；1 或 2 = 未找到（Debian
        对 missing key 通常返回 2，其他实现可能返回 1）；其他退出码 = NSS 错误。
        顺序（spec §11.4 双向校验，防误配打到宿主已有 group）：
        1. ``getent group <gid>``：存在 → 解析 name，!= self.group → GIDConflictError。
        2. ``getent group <name>``：存在 → 解析 GID，!= self.gid → GIDConflictError。
        3. 两步都"不存在" → ``groupadd -g <gid> <name>``（check=True，失败传播）。
        4. 双向一致（name=sakura-ai 且 GID=9472）→ 幂等返回。
        getent 非 0/1/2 退出（NSS 故障）→ 抛明确 bootstrap error，绝不静默或误 groupadd。
        """
        # 1. GID 是否已被其他 name 占用
        gid_proc = subprocess.run(
            ["getent", "group", str(self.gid)],
            capture_output=True,
            text=True,
            check=False,  # returncode 0=存在 / 1,2=不存在 / 其他=NSS 错误
        )
        if gid_proc.returncode == 0:
            try:
                name = gid_proc.stdout.split(":", 1)[0]
            except (IndexError, ValueError) as e:
                raise RuntimeError(
                    f"malformed getent output for GID {self.gid}: {e!r}"
                ) from e
            if name != self.group:
                raise GIDConflictError(
                    f"group ID {self.gid} is owned by {name!r}, expected {self.group!r}"
                )
        elif gid_proc.returncode not in (1, 2):
            raise RuntimeError(
                f"cannot query group ID {self.gid}: getent exited "
                f"{gid_proc.returncode} ({gid_proc.stderr.strip()})"
            )
        # 2. name 是否已存在且 GID 一致
        name_proc = subprocess.run(
            ["getent", "group", self.group],
            capture_output=True,
            text=True,
            check=False,  # returncode 0=存在 / 1,2=不存在 / 其他=NSS 错误
        )
        if name_proc.returncode == 0:
            try:
                fields = name_proc.stdout.split(":")
                gid = int(fields[2])
            except (IndexError, ValueError) as e:
                raise RuntimeError(
                    f"malformed getent output for group {self.group!r}: {e!r}"
                ) from e
            if gid != self.gid:
                raise GIDConflictError(
                    f"group {self.group!r} exists with GID {gid}, expected {self.gid}"
                )
            return  # 双向一致 → 幂等成功
        if name_proc.returncode not in (1, 2):
            raise RuntimeError(
                f"cannot query group {self.group!r}: getent exited "
                f"{name_proc.returncode} ({name_proc.stderr.strip()})"
            )
        # 3. name 与 GID 都不存在 → 创建
        try:
            subprocess.run(
                ["groupadd", "-g", str(self.gid), self.group],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"groupadd failed: {e}") from e

    def ensure_run_dir(self) -> None:
        """创建 run dir 并设 0770 root:<gid>（Web 容器经补充 GID 读 socket）。

        用 ``os.chown``/``os.chmod`` 直接完成（root 身份下原子生效），**不调
        subprocess chown**——避免 shell-out 语义漂移与测试复杂性。
        """
        os.makedirs(self.run_dir, exist_ok=True)
        os.chown(self.run_dir, 0, self.gid)
        os.chmod(self.run_dir, 0o770)

    # ------------------------------------------------------------- readiness

    def _health_ready(self, socket_path: str, timeout: float | None = None) -> bool:
        """UDS GET /v1/health，仅 HTTP 200 视为 ready。

        标准库 AF_UNIX 原始 HTTP 请求（不依赖 httpx/requests）。连接/读写失败或
        非 200 → False（调用方重试直至 startup_timeout）。probe timeout 默认取
        ``min(2.0, startup_timeout)``——单次探测不应超过总 readiness 超时。
        """
        effective_timeout = (
            timeout if timeout is not None else min(2.0, self.startup_timeout)
        )
        request = (
            "GET /v1/health HTTP/1.1\r\nHost: updater\r\nConnection: close\r\n\r\n"
        )
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(effective_timeout)
                s.connect(socket_path)
                s.sendall(request.encode("ascii"))
                response = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    response += chunk
        except OSError:
            return False
        status_line = response.split(b"\r\n", 1)[0]
        return status_line in (b"HTTP/1.0 200 OK", b"HTTP/1.1 200 OK")

    # ----------------------------------------------------------------- start

    def _wait_ready(
        self, child: subprocess.Popen, identity: str, log_path: str
    ) -> None:
        """readiness gate：child 存活 + 非空且稳定 starttime + 身份匹配 + health 200。

        任一条件在 startup_timeout 内无法满足 → UpdaterStartError（含 log path）。
        只有全部满足后才返回，由调用方写 PID meta（meta 绝不先于 ready 存在）。
        """
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise UpdaterStartError(
                    f"updater exited before ready (exit={child.poll()}); log: {log_path}"
                )
            starttime = _read_proc_starttime(child.pid)
            if not starttime:
                time.sleep(self.poll_interval)  # /proc 尚未填充，继续等
                continue
            # 每次迭代重读 starttime 并全量校验：值漂移/身份变化都视为 PID reuse
            if not _is_same_process(child.pid, starttime, identity):
                raise UpdaterStartError(
                    f"updater process identity mismatch (pid={child.pid}); log: {log_path}"
                )
            if self._health_ready(self.socket_path):
                self._write_pid_meta(child.pid, starttime, identity)
                return
            time.sleep(self.poll_interval)
        raise UpdaterStartError(
            f"updater did not become ready within {self.startup_timeout}s; log: {log_path}"
        )

    def _cleanup_child(self, child: subprocess.Popen) -> None:
        """失败路径清理：只经该 Popen 实例 terminate → bounded wait → kill。

        SIGKILL 仅当 bounded wait 超时（child 无响应）；清理异常不吞（外层已抛
        UpdaterStartError，清理是尽力而为的附加动作，但绝不掩盖原始失败原因）。
        """
        try:
            child.terminate()
        except OSError:
            pass
        try:
            child.wait(timeout=self.stop_timeout)
        except subprocess.TimeoutExpired:
            try:
                child.kill()
            except OSError:
                pass
            try:
                child.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    def start(self) -> None:
        """启动 daemon（幂等：已运行则直接返回）。

        顺序：is_running 幂等 → 解析 executable/identity → 生产 root gate →
        Popen(start_new_session, log redirect) → readiness gate → 原子写 PID meta。
        """
        if self.is_running():
            return
        argv_exe, identity = self._resolve_executable()
        if identity == IDENTITY_BINARY:
            self._require_root("start")
            self._validate_production_paths()
            argv_exe = [self.binary_path]
        argv = argv_exe + self._serve_args()
        child_env = os.environ.copy()
        if identity == IDENTITY_BINARY:
            # ``backend start`` is itself running inside the PyInstaller onefile
            # executable and launches a second, long-lived instance of that same
            # executable.  Without a reset, the child reuses the parent's _MEI
            # extraction directory; the parent then exits and removes
            # base_library.zip while the daemon still needs it for lazy imports.
            # Treat the daemon as a new top-level onefile application so it owns
            # an independent extraction directory for its full lifetime.
            child_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        os.makedirs(self.state_dir, exist_ok=True)
        log_path = self._log_path
        with open(log_path, "ab") as logf:
            try:
                child = subprocess.Popen(
                    argv,
                    cwd=os.path.abspath(os.sep),
                    start_new_session=True,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=logf,
                    stderr=logf,
                    env=child_env,
                )
            except OSError as e:
                raise UpdaterStartError(
                    f"cannot spawn updater: {e}; log: {log_path}"
                ) from e
        try:
            self._wait_ready(child, identity, log_path)
        except BaseException:
            self._cleanup_child(child)
            raise

    # ------------------------------------------------------------------ stop

    def stop(self) -> None:
        """停止 daemon（幂等），全程 PID reuse 防御。

        读取原始 pid/starttime/identity；初次、等待循环每次、最终 SIGKILL 前都做
        ``_is_same_process``——身份变化即原进程已退出，绝不再发任何信号。无/坏
        meta 幂等清理；正常结束/提前退出都清 meta。
        """
        meta = self._read_pid_meta()
        if meta is None:
            if os.path.exists(self._pid_meta_path):
                self._clear_pid_meta()  # 坏 meta：无法安全指导信号，幂等清理
            return
        pid, starttime, identity = meta["pid"], meta["starttime"], meta["identity"]
        if not _is_same_process(pid, starttime, identity):
            self._clear_pid_meta()
            return
        try:
            os.kill(pid, _SIGTERM)
        except ProcessLookupError:
            self._clear_pid_meta()
            return
        except OSError as e:
            # PermissionError 等：无法向进程发信号 → 清 meta 后转抛清晰错误
            self._clear_pid_meta()
            raise UpdaterStartError(f"cannot stop updater (pid={pid}): {e}") from e
        deadline = time.monotonic() + self.stop_timeout
        while time.monotonic() < deadline:
            if not _is_same_process(pid, starttime, identity):
                self._clear_pid_meta()
                return
            time.sleep(self.poll_interval)
        # 超时：最终检查仍为同一进程才 SIGKILL（PID 被复用绝不打到新进程上）
        if _SIGKILL is not None and _is_same_process(pid, starttime, identity):
            try:
                os.kill(pid, _SIGKILL)
            except ProcessLookupError:
                pass  # 进程在最终检查与发信号之间退出——视为已停止
            except OSError:
                pass  # SIGKILL 失败属尽力而为；meta 照常清理，不掩盖已发出的 SIGTERM
        self._clear_pid_meta()

    # ---------------------------------------------------------------- status

    def is_running(self) -> bool:
        """只信完整 meta + 三重 identity 校验；无 meta / 坏 meta → False。"""
        meta = self._read_pid_meta()
        if meta is None:
            return False
        return _is_same_process(meta["pid"], meta["starttime"], meta["identity"])

    def status(self) -> dict:
        """backend status 输出（JSON 字段契约）。"""
        meta = self._read_pid_meta()
        running = meta is not None and _is_same_process(
            meta["pid"], meta["starttime"], meta["identity"]
        )
        return {
            "running": running,
            "pid": meta["pid"] if running else None,
            "log_path": self._log_path,
            "socket_path": self.socket_path,
            "state_dir": self.state_dir,
        }

    # ------------------------------------------------------------------ misc

    def install(self) -> None:
        """host bootstrap 安装：root gate → group 双向校验/创建 → run dir → state dir。

        **不下载 binary**（Slice 3c 负责 binary acquisition / PyInstaller / release
        asset 校验）。state_dir 供 PID meta 与 updater.log 使用（.deploy/updater）。
        """
        self._require_root("install")
        self.ensure_group()
        self.ensure_run_dir()
        os.makedirs(self.state_dir, exist_ok=True)
