"""Agent 执行信任域合同与本地受控执行实现。

本模块是 Agent Team 唯一允许创建子进程的边界。``LocalExecutionRunner``
只用于源码开发和受信任 Git 控制面；Docker 部署的沙箱执行器将在后续切片
实现同一份 ``ExecutionRunner`` 合同。模型工具不得自行调用 asyncio subprocess
API，也不得从父进程复制环境变量。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, Self, runtime_checkable
from urllib.parse import unquote, urlsplit

from backend.services.agent_team.network_policy import get_agent_team_network_policy
from backend.services.agent_team.workspace_service import (
    AgentTeamWorkspaceService,
    WorkspaceSecurityError,
)

_WINDOWS_ABS_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s'\"`;&|]*")
_POSIX_ABS_RE = re.compile(r"(?<![\w.-])/(?:[^\s'\"`;&|]+)")
_FORBIDDEN_TOKENS = (
    "..",
    "~",
    "$HOME",
    "${HOME}",
    "%USERPROFILE%",
    "%HOMEPATH%",
    "$env:USERPROFILE",
    "$env:HOMEPATH",
)
_TRUSTED_ENV_KEYS = frozenset(
    {
        "GIT_ASKPASS",
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_NOSYSTEM",
    }
)
_TRUSTED_INTERNAL_ENV_KEYS = frozenset(
    {
        "HOME",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_PAGER",
        "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR",
    }
)
_ALLOWED_EXECUTION_ENV_KEYS = _TRUSTED_ENV_KEYS | _TRUSTED_INTERNAL_ENV_KEYS
_HOST_ENV_KEYS = frozenset({"SystemRoot", "SYSTEMROOT", "WINDIR", "PATHEXT"})
_WORKSPACE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

MAX_WORKSPACE_KEY_LENGTH = 128
MAX_COMMAND_LENGTH = 32_768
MAX_ARG_LENGTH = 8_192
MAX_ARGV_COUNT = 256
MAX_CWD_LENGTH = 4_096
MAX_ENV_KEY_LENGTH = 128
MAX_ENV_VALUE_LENGTH = 8_192
MAX_TIMEOUT_SECONDS = 3_600
MAX_GIT_CONFIG_BYTES = 1_048_576
# Metadata scans intentionally avoid the potentially enormous Git object
# database, but refs/logs and the bounded objects subtrees still need an
# explicit fail-closed node budget so a malicious repository cannot turn a
# trusted Git call into an unbounded directory walk.
MAX_GIT_REFS_LOGS_NODES = 65_536
MAX_GIT_OBJECTS_DIRECT_NODES = 512
MAX_GIT_OBJECTS_AUX_NODES = 8_192

_DEFAULT_REMOTE_PORTS = {"http": 80, "https": 443, "ssh": 22}
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# Keep dependency environments separate for each execution backend.  A local
# venv contains host-specific shebangs and interpreter links; a sandbox venv
# is created inside the Linux runner at a different mount point.  Sharing the
# historical ``.venv`` root between the two backends makes a resumed task
# depend on whichever backend happened to create it first.
_LOCAL_DEPENDENCY_VENV = Path(".venv") / "local"
_LOCAL_DEPENDENCY_VENV_ARG = ".venv/local"

_GIT_SAFE_CONFIG = (
    ("credential.helper", ""),
    ("core.fsmonitor", "false"),
    ("core.sshCommand", ""),
    ("core.gitProxy", ""),
    ("protocol.file.allow", "never"),
    ("protocol.ext.allow", "never"),
    ("diff.external", ""),
    ("core.pager", "cat"),
    ("core.editor", "true"),
    ("sequence.editor", "true"),
)

class _ReentrantAsyncLock:
    """Task-reentrant asyncio lock used by nested TrustedGit entry points."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    async def __aenter__(self) -> Self:
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("TrustedGit lock requires an active asyncio task")
        if self._owner is owner:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = owner
        self._depth = 1
        return self

    async def __aexit__(self, *_exc: object) -> None:
        owner = asyncio.current_task()
        if owner is not self._owner:
            raise RuntimeError("TrustedGit lock released by a different task")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


_WORKSPACE_GIT_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    weakref.WeakValueDictionary[str, _ReentrantAsyncLock],
] = weakref.WeakKeyDictionary()
_WORKSPACE_GIT_LOCKS_GUARD = threading.Lock()


def _workspace_git_lock(scope: Path) -> _ReentrantAsyncLock:
    """Return a process-local lock for one controlled repository.

    Base, repository, and linked-worktree paths share the same common
    ``.git`` metadata.  The lock is keyed by the canonical repository scope
    and held from validation through subprocess completion, so cooperating
    trusted Git calls cannot invalidate one another between the final check
    and ``create_subprocess_exec``.  Weak values keep idle repository scopes
    from growing an unbounded process-wide registry.
    """

    # asyncio primitives are event-loop scoped.  Keep a weak per-loop registry
    # so a long-lived runner reused by another loop cannot await the lock bound
    # to the first loop (which otherwise raises RuntimeError when contention
    # causes asyncio.Lock to consult its loop).  A loop object, rather than its
    # reusable integer id, also prevents id reuse from aliasing two loops.
    loop = asyncio.get_running_loop()
    key = os.path.normcase(str(scope.resolve()))
    with _WORKSPACE_GIT_LOCKS_GUARD:
        per_loop_locks = _WORKSPACE_GIT_LOCKS.get(loop)
        if per_loop_locks is None:
            per_loop_locks = weakref.WeakValueDictionary()
            _WORKSPACE_GIT_LOCKS[loop] = per_loop_locks
        lock = per_loop_locks.get(key)
        if lock is None:
            lock = _ReentrantAsyncLock()
            per_loop_locks[key] = lock
        return lock


class ExecutionProfile(StrEnum):
    """执行请求的信任域。"""

    AGENT = "agent"
    DEPENDENCY = "dependency"
    TRUSTED_CONTROL = "trusted_control"


class ExecutionError(RuntimeError):
    """执行请求未能安全提交或完成。"""


class UnsupportedExecutionProfile(ExecutionError):
    """当前执行后端没有实现指定 profile。"""


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """与具体执行后端无关的强类型执行请求。

    Agent/Dependency 请求不接受调用方环境覆盖。``env`` 仅供受信任 Git
    runner 在一次性 askpass 生命周期内注入内部变量，后续沙箱客户端会对
    同一字段再次做协议级 allowlist 校验。
    """

    workspace_key: str
    command: str | None = None
    argv: tuple[str, ...] | None = None
    cwd: PurePosixPath = field(default_factory=lambda: PurePosixPath("."))
    profile: ExecutionProfile = ExecutionProfile.AGENT
    timeout_seconds: float = 600
    env: Mapping[str, str] = field(default_factory=dict)
    # Internal cancellation signal.  It never crosses the sandboxd wire
    # protocol; the runner uses it to issue the matching request-id cancel.
    cancel_event: asyncio.Event | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        has_command = self.command is not None
        has_argv = self.argv is not None
        if has_command == has_argv:
            raise ValueError("command 或 argv 必须且只能提供一个")
        if has_command:
            if not isinstance(self.command, str) or not self.command.strip():
                raise ValueError("command 不能为空")
            if len(self.command) > MAX_COMMAND_LENGTH or "\x00" in self.command:
                raise ValueError("command 超过长度上限或含空字节")
        if has_argv:
            args = tuple(self.argv or ())
            if len(args) > MAX_ARGV_COUNT:
                raise ValueError("argv 数量超过上限")
            if not args or any(
                not isinstance(arg, str)
                or not arg
                or len(arg) > MAX_ARG_LENGTH
                or "\x00" in arg
                for arg in args
            ):
                raise ValueError("argv 不能为空且不能含空参数")
            object.__setattr__(self, "argv", args)
        if (
            not isinstance(self.workspace_key, str)
            or not self.workspace_key
            or len(self.workspace_key) > MAX_WORKSPACE_KEY_LENGTH
            or "\x00" in self.workspace_key
        ):
            raise ValueError("workspace_key 不能为空、过长或含空字节")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
            or self.timeout_seconds > MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds 必须在 0 和上限之间")
        try:
            profile = ExecutionProfile(self.profile)
        except ValueError as exc:
            raise ValueError(f"未知执行 profile: {self.profile}") from exc
        object.__setattr__(self, "profile", profile)
        normalized_env = dict(self.env)
        if profile is not ExecutionProfile.TRUSTED_CONTROL and normalized_env:
            raise ValueError("Agent/Dependency 执行不允许调用方注入环境变量")
        unknown = set(normalized_env).difference(_ALLOWED_EXECUTION_ENV_KEYS)
        if unknown:
            raise ValueError(f"环境变量不在受信任 allowlist: {sorted(unknown)}")
        if any(
            not isinstance(key, str)
            or not key
            or len(key) > MAX_ENV_KEY_LENGTH
            or "\x00" in key
            or not isinstance(value, str)
            or len(value) > MAX_ENV_VALUE_LENGTH
            or "\x00" in value
            for key, value in normalized_env.items()
        ):
            raise ValueError("环境变量键值无效、过长或含空字节")
        object.__setattr__(self, "env", normalized_env)
        if not isinstance(self.cwd, PurePosixPath):
            object.__setattr__(self, "cwd", PurePosixPath(str(self.cwd)))
        cwd_text = str(self.cwd)
        if len(cwd_text) > MAX_CWD_LENGTH or "\x00" in cwd_text:
            raise ValueError("cwd 超过长度上限或含空字节")
        if profile is not ExecutionProfile.TRUSTED_CONTROL:
            if not _WORKSPACE_KEY_RE.fullmatch(self.workspace_key):
                raise ValueError("Agent/Dependency workspace_key 格式无效")
            if (
                self.cwd.is_absolute()
                or "\\" in cwd_text
                or any(part == ".." for part in self.cwd.parts)
            ):
                raise ValueError("Agent/Dependency cwd 必须为不含 .. 的相对 POSIX 路径")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """执行结果，保留 Agent Tool 需要的输出和超时语义。"""

    command: str = ""
    cwd: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False
    infrastructure_error: str | None = None

    @property
    def returncode(self) -> int:
        """旧 Agent Team API 使用的 ``returncode`` 兼容视图。"""

        return self.exit_code if self.exit_code is not None else -1


@runtime_checkable
class ExecutionRunner(Protocol):
    """所有 Agent 执行后端必须实现的最小协议。"""

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """执行一个已验证请求。"""
        ...

    def supports_profile(self, profile: ExecutionProfile) -> bool:
        """报告后端是否实现 profile。"""
        ...


def _display_args(args: Sequence[str]) -> str:
    return " ".join(_mask_sensitive_arg(arg) for arg in args)


def _consume_task(task: asyncio.Task[Any]) -> None:
    """Consume a cancelled/finished internal waiter without leaking warnings."""

    def consume(done: asyncio.Task[Any]) -> None:
        try:
            done.result()
        except BaseException:
            pass

    if task.done():
        consume(task)
    else:
        task.add_done_callback(consume)


def _mask_sensitive_arg(value: str) -> str:
    if "x-access-token:" in value:
        return re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", value)
    return value


def normalize_trusted_remote_url(value: str) -> tuple[str, str, int, str]:
    """返回用于凭据绑定的严格 remote identity。

    Identity 只包含 scheme、host、effective port 和规范化仓库路径；任何
    userinfo、query、fragment、危险 transport 或路径穿越都会被拒绝。固定
    的 ``git@`` SSH user 不是凭据，但除此之外不接受 SSH userinfo。
    """

    if not isinstance(value, str) or not value or "\x00" in value:
        raise ExecutionError("Git remote URL 无效")
    if "::" in value:
        raise ExecutionError("Git remote-helper URL 不被允许")

    if _looks_like_scp_remote(value):
        userinfo, target = value.split("@", 1)
        if userinfo != "git" or ":" in userinfo:
            raise ExecutionError("Git remote URL 不得包含不安全 userinfo")
        if "?" in target or "#" in target:
            raise ExecutionError("Git remote URL 不得包含 query 或 fragment")
        host, separator, raw_path = target.partition(":")
        if not separator or not host or not raw_path:
            raise ExecutionError("Git SCP remote URL 无效")
        scheme = "ssh"
        effective_port = _DEFAULT_REMOTE_PORTS[scheme]
        raw_path = "/" + raw_path.lstrip("/")
    else:
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ExecutionError("Git remote URL 无效") from exc
        scheme = parsed.scheme.lower()
        if scheme not in _DEFAULT_REMOTE_PORTS:
            raise ExecutionError(f"Git remote scheme 不被允许: {scheme or 'unknown'}")
        if not parsed.netloc:
            raise ExecutionError("Git remote URL 缺少 host")
        if parsed.username is not None or parsed.password is not None:
            if not (
                scheme == "ssh"
                and parsed.username == "git"
                and parsed.password is None
            ):
                raise ExecutionError("Git remote URL 不得包含 userinfo")
        if parsed.query or parsed.fragment:
            raise ExecutionError("Git remote URL 不得包含 query 或 fragment")
        try:
            host = parsed.hostname
            explicit_port = parsed.port
        except ValueError as exc:
            raise ExecutionError("Git remote URL 端口无效") from exc
        if not host:
            raise ExecutionError("Git remote URL 缺少 host")
        effective_port = explicit_port or _DEFAULT_REMOTE_PORTS[scheme]
        raw_path = parsed.path

    # Host normalization is case-folding only.  A trailing dot changes the
    # serialized remote identity and must not silently bypass the GitHub API
    # binding contract.
    host = host.lower()
    if not host or "\\" in raw_path:
        raise ExecutionError("Git remote URL host/path 无效")
    try:
        decoded_path = unquote(raw_path)
    except Exception as exc:
        raise ExecutionError("Git remote URL path 无效") from exc
    if "\x00" in decoded_path:
        raise ExecutionError("Git remote URL path 无效")
    parts = [part for part in decoded_path.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ExecutionError("Git remote URL path 不得包含路径穿越")
    if parts[-1].lower().endswith(".git"):
        parts[-1] = parts[-1][:-4]
    if not parts[-1]:
        raise ExecutionError("Git remote URL repository path 为空")
    normalized_path = "/" + "/".join(parts)
    return scheme, host, effective_port, normalized_path


def trusted_remote_urls_match(actual: str, expected: str) -> bool:
    """比较两个 remote URL 的安全、规范化 identity。"""

    try:
        return normalize_trusted_remote_url(actual) == normalize_trusted_remote_url(
            expected
        )
    except ExecutionError:
        return False


class LocalExecutionRunner:
    """本地开发执行器，使用固定环境 allowlist，不继承父环境。

    该类承载当前源码开发模式的 subprocess 实现。它明确拒绝
    未经授权的网络策略；依赖安装只有在超级管理员明确选择
    ``full_access`` 时才允许通过本机受控 argv 执行。
    """

    def __init__(
        self,
        workspace: str | Path,
        workspace_service: AgentTeamWorkspaceService | None = None,
    ) -> None:
        self.workspace_service = workspace_service or AgentTeamWorkspaceService()
        self.workspace = self.workspace_service.resolve_inside_workspace(workspace)
        self.workspace_key = execution_workspace_key(
            self.workspace, self.workspace_service
        )

    def supports_profile(self, profile: ExecutionProfile) -> bool:
        # ``DEPENDENCY`` is a capability of the source-development runner,
        # but execute() still performs the fresh full_access policy gate.  A
        # synchronous capability query cannot safely read the async dynamic
        # configuration without introducing a stale snapshot or event-loop
        # coupling.
        return profile in {ExecutionProfile.AGENT, ExecutionProfile.DEPENDENCY}

    @property
    def dependency_python_executable(self) -> Path:
        """Return the interpreter that owns this source-development process."""

        return Path(sys.executable).resolve()

    def dependency_venv_python(self) -> Path:
        """Return the lexical task-local venv launcher using the host layout.

        POSIX ``venv`` normally creates ``bin/python`` as a symlink to the
        interpreter that bootstrapped it.  The launcher *entry* therefore must
        remain lexical here; resolving it would turn the normal venv link into
        the host interpreter and make the dependency allowlist reject pip.
        The containing venv and scripts directory are still resolved and
        checked, while the final launcher is validated separately by
        ``_validate_dependency_venv_python``.
        """

        script_dir = "Scripts" if os.name == "nt" else "bin"
        executable = "python.exe" if os.name == "nt" else "python"
        venv_root = self.dependency_venv_root()
        safe_script_dir = self.workspace_service.resolve_inside_workspace(
            venv_root,
            script_dir,
        )
        # Do not call resolve_inside_workspace on the launcher itself: on
        # POSIX that would dereference the expected link to sys.executable.
        return safe_script_dir / executable

    def dependency_venv_root(self) -> Path:
        """Resolve the task-local venv root and reject external links."""

        return self.workspace_service.resolve_inside_workspace(
            self.workspace,
            _LOCAL_DEPENDENCY_VENV,
        )

    def _validate_dependency_venv_python(self, launcher: Path) -> None:
        """Validate a venv launcher without rejecting its normal POSIX link.

        A venv launcher may be either a copied executable inside the task
        workspace or a POSIX symlink to the exact interpreter used for venv
        creation.  The latter is the standard Python layout and is the one
        intentional exception to the usual final-path containment check.
        Any other external target remains rejected.
        """

        expected_launcher = self.dependency_venv_python()
        if self._normalize_dependency_executable(launcher) != self._normalize_dependency_executable(
            expected_launcher
        ):
            raise UnsupportedExecutionProfile(
                "LocalExecutionRunner 的依赖解释器路径不受支持"
            )
        try:
            if not launcher.exists():
                raise ExecutionError("Local dependency venv Python launcher 不存在")
            if launcher.is_symlink():
                resolved_launcher = launcher.resolve(strict=True)
                if self._normalize_dependency_executable(
                    resolved_launcher
                ) == self._normalize_dependency_executable(
                    self.dependency_python_executable
                ):
                    return
            # Copied launchers, and symlinks to an in-workspace launcher, must
            # still be contained by the task workspace.  This call is safe
            # because it is intentionally not used for the normal external
            # sys.executable symlink accepted above.
            contained_launcher = self.workspace_service.resolve_inside_workspace(
                self.workspace,
                launcher,
            )
            if not contained_launcher.is_file():
                raise ExecutionError("Local dependency venv Python launcher 不是文件")
        except (OSError, RuntimeError, ValueError, WorkspaceSecurityError) as exc:
            raise ExecutionError(
                "Local dependency venv Python launcher 不在工作区内"
            ) from exc

    @staticmethod
    def _normalize_dependency_executable(value: str | Path) -> str:
        """Normalize a trusted dependency interpreter path for comparison."""

        try:
            # Comparison must preserve a venv launcher's lexical identity;
            # ``Path.resolve`` would collapse the POSIX symlink to the
            # bootstrap interpreter.  The target is validated separately.
            return os.path.normcase(os.path.abspath(os.fspath(value)))
        except (OSError, RuntimeError, TypeError) as exc:
            raise ExecutionError("Local dependency interpreter path 无效") from exc

    def _validate_dependency_request(self, request: ExecutionRequest) -> None:
        """Allow only application-constructed local dependency argv forms.

        Dependency installation is intentionally not a general-purpose shell
        capability.  The only accepted requests are the venv bootstrap and
        the two package-install forms generated by
        ``AgentTeamGitWorkspaceService``.  This keeps the local full-access
        exception bounded while avoiding the sandbox's Linux-only
        ``/workspace`` paths.
        """

        if request.command is not None or request.argv is None:
            raise UnsupportedExecutionProfile(
                "LocalExecutionRunner 的依赖安装只接受受控 argv"
            )
        args = tuple(request.argv)
        if request.cwd != PurePosixPath("."):
            raise UnsupportedExecutionProfile(
                "LocalExecutionRunner 的依赖安装 cwd 必须为工作区根目录"
            )
        if not args:
            raise UnsupportedExecutionProfile("LocalExecutionRunner 依赖 argv 为空")

        executable = self._normalize_dependency_executable(args[0])
        bootstrap_executable = self._normalize_dependency_executable(
            self.dependency_python_executable
        )
        if executable == bootstrap_executable:
            expected = (
                str(self.dependency_python_executable),
                "-m",
                "venv",
                _LOCAL_DEPENDENCY_VENV_ARG,
            )
            if args != expected:
                raise UnsupportedExecutionProfile(
                    "LocalExecutionRunner 的 venv 初始化参数不受支持"
                )
            venv_root = self.dependency_venv_root()
            if venv_root.exists() and not venv_root.is_dir():
                raise UnsupportedExecutionProfile(
                    "LocalExecutionRunner 的依赖 venv 路径不是目录"
                )
            return

        venv_executable = self._normalize_dependency_executable(
            self.dependency_venv_python()
        )
        if executable != venv_executable:
            raise UnsupportedExecutionProfile(
                "LocalExecutionRunner 的依赖解释器路径不受支持"
            )
        self._validate_dependency_venv_python(Path(args[0]))
        if args[1:4] != ("-m", "pip", "install"):
            raise UnsupportedExecutionProfile(
                "LocalExecutionRunner 的 pip 参数不受支持"
            )
        if args[4:] not in {
            ("-e", ".", "--quiet"),
            ("-r", "requirements.txt", "--quiet"),
        }:
            raise UnsupportedExecutionProfile(
                "LocalExecutionRunner 的依赖目标不受支持"
            )

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if not self.supports_profile(request.profile):
            raise UnsupportedExecutionProfile(
                f"LocalExecutionRunner 不支持 profile: {request.profile.value}"
            )
        if request.profile in {
            ExecutionProfile.AGENT,
            ExecutionProfile.DEPENDENCY,
        } and request.workspace_key != self.workspace_key:
            raise ExecutionError(
                "LocalExecutionRunner workspace_key 与工作区 identity 不匹配"
            )
        # Trusted Git control operations are deliberately outside the Agent
        # network policy: they are application-constructed argv calls with a
        # short-lived credential boundary.  Untrusted Agent and Dependency
        # requests both need the fresh local-backend policy gate here.
        if request.profile in {
            ExecutionProfile.AGENT,
            ExecutionProfile.DEPENDENCY,
        }:
            try:
                network_policy = await get_agent_team_network_policy()
            except Exception as exc:
                raise ExecutionError(
                    "local Agent backend 无法读取网络策略，已拒绝执行"
                ) from exc
            if not network_policy.allows_local_backend:
                if request.profile is ExecutionProfile.DEPENDENCY:
                    raise ExecutionError(
                        "local Agent dependency installation 仅在 full_access 下可用；"
                        f"当前策略为 {network_policy.value}，不能在宿主联网安装依赖"
                    )
                raise ExecutionError(
                    "local Agent backend 仅在 full_access 下可用；local 在宿主进程执行，"
                    f"无法兑现 {network_policy.value} 网络隔离。请切换为 sandbox，"
                    "或由超级管理员明确选择 full_access 并确认宿主网络风险"
                )
        safe_cwd = self.workspace_service.resolve_inside_workspace(
            self.workspace, request.cwd
        )
        if request.profile is ExecutionProfile.AGENT:
            if request.command is not None:
                self._validate_command(request.command)
            else:
                self._validate_command_args(request.argv or ())
        elif request.profile is ExecutionProfile.DEPENDENCY:
            self._validate_dependency_request(request)
        env = self._build_env(request.env)
        if request.command is not None:
            process = await asyncio.create_subprocess_shell(
                request.command,
                cwd=str(safe_cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            display_command = request.command
        else:
            args = tuple(request.argv or ())
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(safe_cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            display_command = _display_args(args)

        communicate_task = asyncio.create_task(process.communicate())
        cancel_waiter: asyncio.Task[bool] | None = None
        try:
            if request.cancel_event is None:
                stdout, stderr = await asyncio.wait_for(
                    asyncio.shield(communicate_task),
                    timeout=request.timeout_seconds,
                )
            else:
                cancel_waiter = asyncio.create_task(request.cancel_event.wait())
                done, _ = await asyncio.wait(
                    {communicate_task, cancel_waiter},
                    timeout=request.timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_waiter in done and communicate_task not in done:
                    await self._kill_process(process)
                    stdout, stderr = await communicate_task
                    return self._result(
                        display_command,
                        safe_cwd,
                        process.returncode,
                        stdout,
                        stderr,
                        cancelled=True,
                    )
                if communicate_task not in done:
                    raise TimeoutError
                stdout, stderr = communicate_task.result()
        except TimeoutError:
            await self._kill_process(process)
            stdout, stderr = await communicate_task
            return self._result(
                display_command,
                safe_cwd,
                process.returncode,
                stdout,
                stderr,
                timed_out=True,
            )
        except asyncio.CancelledError:
            await self._kill_process(process)
            await communicate_task
            raise
        finally:
            if cancel_waiter is not None and not cancel_waiter.done():
                cancel_waiter.cancel()
                _consume_task(cancel_waiter)

        return self._result(
            display_command,
            safe_cwd,
            process.returncode,
            stdout,
            stderr,
        )

    async def run(
        self,
        command_or_request: str | ExecutionRequest,
        cwd: str | Path = ".",
        timeout_seconds: float = 600,
        *,
        profile: ExecutionProfile = ExecutionProfile.AGENT,
    ) -> ExecutionResult:
        """兼容旧 ``run(command)``，也接受强类型 ``ExecutionRequest``。"""

        if isinstance(command_or_request, ExecutionRequest):
            return await self.execute(command_or_request)
        request = ExecutionRequest(
            workspace_key=execution_workspace_key(self.workspace, self.workspace_service),
            command=command_or_request,
            cwd=PurePosixPath(str(cwd).replace("\\", "/")),
            profile=profile,
            timeout_seconds=timeout_seconds,
        )
        return await self.execute(request)

    async def run_args(
        self,
        args: Sequence[str],
        cwd: str | Path = ".",
        timeout_seconds: float = 600,
        *,
        profile: ExecutionProfile = ExecutionProfile.AGENT,
        extra_env: Mapping[str, str] | None = None,
    ) -> ExecutionResult:
        """以 argv 形式执行命令；调用方不能为 Agent 注入环境。"""

        if not args:
            raise WorkspaceSecurityError("Shell 命令不能为空")
        args_tuple = tuple(str(arg) for arg in args)
        separator_seen = False
        for arg in args_tuple:
            if arg == "--":
                separator_seen = True
                continue
            # ``grep -- <literal> .`` 中 separator 后的值是搜索文本或由
            # 工具构造的工作区相对路径，不应把搜索关键词 ``..`` 当成路径
            # 穿越；调用方仍需在传入前解析任何用户提供的文件路径。
            if not separator_seen:
                self._validate_command_arg(arg)
        request = ExecutionRequest(
            workspace_key=execution_workspace_key(self.workspace, self.workspace_service),
            argv=args_tuple,
            cwd=PurePosixPath(str(cwd).replace("\\", "/")),
            profile=profile,
            timeout_seconds=timeout_seconds,
            env=extra_env or {},
        )
        return await self.execute(request)

    def _validate_command_args(self, args: Sequence[str]) -> None:
        separator_seen = False
        for arg in args:
            if arg == "--":
                separator_seen = True
                continue
            if not separator_seen:
                self._validate_command_arg(arg)

    def _build_env(self, extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """从空白环境和明确的安全键构造子进程环境。"""

        workspace_venv = self.workspace / _LOCAL_DEPENDENCY_VENV
        if workspace_venv.exists():
            venv_root = self.dependency_venv_root()
            if not venv_root.is_dir():
                raise ExecutionError("Local dependency venv root 不是目录")
            script_dir = venv_root / ("Scripts" if os.name == "nt" else "bin")
        else:
            venv_root = Path(sys.prefix).resolve()
            script_dir = venv_root / ("Scripts" if os.name == "nt" else "bin")

        path_entries = [str(script_dir), str(Path(sys.executable).resolve().parent)]
        for executable in ("git", "grep", "bash", "sh"):
            resolved = shutil.which(executable)
            if resolved:
                path_entries.append(str(Path(resolved).resolve().parent))
        for path_entry in os.defpath.split(os.pathsep):
            if path_entry:
                path_entries.append(path_entry)
        unique_paths = list(dict.fromkeys(path_entries))

        env = {
            "HOME": str(self.workspace / ".agent-home"),
            "PATH": os.pathsep.join(unique_paths),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "CI": "true",
            "TERM": "dumb",
            "VIRTUAL_ENV": str(venv_root),
            "SAKURA_AGENT_WORKSPACE": str(self.workspace),
        }
        # Windows 创建进程需要这些系统键；它们不是用户秘密，也不会把父环境
        # 中的任意变量透传给 Agent。
        for key in _HOST_ENV_KEYS:
            value = os.environ.get(key)
            if value:
                env[key] = value
        if extra_env:
            unknown = set(extra_env).difference(_ALLOWED_EXECUTION_ENV_KEYS)
            if unknown:
                raise ValueError(f"环境变量不在受信任 allowlist: {sorted(unknown)}")
            env.update({str(key): str(value) for key, value in extra_env.items()})
        return env

    @staticmethod
    async def _kill_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.kill()

    def _result(
        self,
        command: str,
        cwd: Path,
        returncode: int | None,
        stdout: bytes,
        stderr: bytes,
        *,
        timed_out: bool = False,
        cancelled: bool = False,
    ) -> ExecutionResult:
        return ExecutionResult(
            command=_mask_sensitive_arg(command),
            cwd=str(cwd),
            exit_code=-1 if timed_out else (returncode if returncode is not None else -1),
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            cancelled=cancelled,
        )

    def _validate_command(self, command: str) -> None:
        if not command or not command.strip():
            raise WorkspaceSecurityError("Shell 命令不能为空")
        lowered = command.lower()
        for token in _FORBIDDEN_TOKENS:
            if token.lower() in lowered:
                raise WorkspaceSecurityError(f"Shell 命令包含禁止的路径片段: {token}")

        for match in _WINDOWS_ABS_RE.finditer(command):
            self.workspace_service.resolve_inside_workspace(
                self.workspace, match.group(0)
            )
        for match in _POSIX_ABS_RE.finditer(command):
            path = match.group(0)
            if path.startswith("/dev/"):
                continue
            self.workspace_service.resolve_inside_workspace(self.workspace, path)

    def _validate_command_arg(self, arg: str) -> None:
        if not arg:
            raise WorkspaceSecurityError("Shell 命令参数不能为空")
        if self._is_url(arg):
            return
        lowered = arg.lower()
        for token in _FORBIDDEN_TOKENS:
            if token.lower() in lowered:
                raise WorkspaceSecurityError(f"Shell 命令包含禁止的路径片段: {token}")
        for match in _WINDOWS_ABS_RE.finditer(arg):
            self.workspace_service.resolve_inside_workspace(
                self.workspace, match.group(0)
            )
        for match in _POSIX_ABS_RE.finditer(arg):
            self.workspace_service.resolve_inside_workspace(
                self.workspace, match.group(0)
            )

    @staticmethod
    def _is_url(value: str) -> bool:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https", "ssh", "git"} and bool(parsed.netloc)

    def _mask_sensitive_arg(self, value: str) -> str:
        return _mask_sensitive_arg(value)


class TrustedGitRunner(LocalExecutionRunner):
    """受信任 Git 控制面 runner。

    只接受以 ``git`` 开头的应用构造 argv。installation token 通过短生命周期
    askpass 文件传递，不会出现在 clone URL、命令行参数或仓库配置中。
    """

    def __init__(
        self,
        workspace: str | Path,
        workspace_service: AgentTeamWorkspaceService | None = None,
    ) -> None:
        super().__init__(workspace, workspace_service)
        self.git_path = self._resolve_system_git(self.workspace)
        self._active_askpass_paths: set[Path] = set()
        self._active_askpass_tokens: dict[Path, str] = {}
        # Serialize the synchronous metadata walk when several entry points on
        # this runner are dispatched concurrently.  Do not cache identities:
        # refs/logs leaves are security-sensitive and a mutable task gitdir can
        # replace one without changing an ancestor directory identity.
        self._metadata_scan_lock = threading.RLock()

    def supports_profile(self, profile: ExecutionProfile) -> bool:
        return profile is ExecutionProfile.TRUSTED_CONTROL

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Serialize trusted Git against the shared repository metadata."""

        async with _workspace_git_lock(self._git_lock_scope()):
            return await self._execute_locked(request)

    async def _execute_locked(self, request: ExecutionRequest) -> ExecutionResult:
        """只执行固定系统 Git 的 TRUSTED_CONTROL argv。"""
        if request.profile is not ExecutionProfile.TRUSTED_CONTROL:
            raise UnsupportedExecutionProfile(
                "TrustedGitRunner 只支持 TRUSTED_CONTROL profile"
            )
        if request.command is not None or not request.argv:
            raise ExecutionError("TrustedGitRunner 拒绝 shell command 或空 argv")

        if set(request.env).intersection(_TRUSTED_INTERNAL_ENV_KEYS):
            raise ExecutionError("Git 内部环境变量只能由 runner 注入")

        first_arg = request.argv[0]
        if first_arg in {"git", "git.exe"}:
            normalized_argv = (str(self.git_path), *request.argv[1:])
            request = replace(request, argv=normalized_argv)
        else:
            try:
                normalized_first = Path(first_arg).resolve(strict=True)
            except (OSError, RuntimeError):
                raise ExecutionError("TrustedGitRunner 拒绝非固定系统 Git 路径")
            if normalized_first != self.git_path:
                raise ExecutionError("TrustedGitRunner 拒绝非固定系统 Git 路径")

        self._reject_url_userinfo(request.argv or ())
        self._reject_unsafe_git_options(request.argv or ())
        self._validate_workspace_git_layout()
        # Git may traverse repository metadata even for read-only commands. Keep
        # this check at the shared execution boundary so direct ``execute`` calls
        # cannot bypass the same fail-closed policy used by ``run_args``.
        await self._validate_git_metadata_before_execution()
        askpass_value = request.env.get("GIT_ASKPASS")
        if askpass_value:
            try:
                askpass_path = Path(askpass_value).resolve(strict=True)
            except (OSError, RuntimeError):
                raise ExecutionError("Git askpass 路径无效")
            if askpass_path not in self._active_askpass_paths:
                raise ExecutionError("Git askpass 路径不是当前请求生成的临时文件")
        for key, expected in {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
        }.items():
            if request.env.get(key) not in {None, expected}:
                raise ExecutionError(f"Git 环境变量 {key} 不符合固定策略")

        runtime_dir, runtime_env, hooks_dir = self._create_git_runtime()
        display_argv = request.argv or ()
        secured_env = {**request.env, **runtime_env}
        secured_argv = self._build_safe_git_argv(display_argv, hooks_dir)
        secured_request = replace(
            request,
            argv=secured_argv,
            env=secured_env,
        )
        try:
            await self._validate_local_git_config(runtime_env, hooks_dir)
            # The local-config preflight above performs a subprocess await.
            # Revalidate immediately before the real Git spawn so a concurrent
            # untrusted writer cannot change the task gitdir during that gap.
            self._validate_workspace_git_layout()
            await self._validate_git_metadata_before_execution()
            result = await super().execute(secured_request)
            active_token = self._active_askpass_tokens.get(
                Path(askpass_value).resolve() if askpass_value else Path()
            )
            if active_token:
                return self._redact_result(
                    result,
                    active_token,
                    display_command=_display_args(display_argv),
                )
            return replace(result, command=_display_args(display_argv))
        finally:
            await self._cleanup_temporary_directory_async(
                runtime_dir,
                "Git 运行时目录",
            )

    def _git_lock_scope(self) -> Path:
        """Return the common repository root shared by base/worktree callers."""

        try:
            return self._controlled_repo_root()
        except ExecutionError:
            # Keep malformed workspaces on a deterministic per-workspace lock;
            # the normal validation path below still rejects them fail-closed.
            return self.workspace

    def _build_env(self, extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """Trusted Git 不使用工作区 venv 中可能被伪造的可执行文件。"""
        env = super()._build_env(extra_env)
        system_path = [str(self.git_path.parent)]
        for path_entry in os.defpath.split(os.pathsep):
            if path_entry:
                system_path.append(path_entry)
        env["PATH"] = os.pathsep.join(dict.fromkeys(system_path))
        env.pop("VIRTUAL_ENV", None)
        return env

    @staticmethod
    def _resolve_system_git(workspace: Path) -> Path:
        """在创建 runner 时解析固定绝对 Git 路径。"""
        candidates: list[Path] = []
        if os.name == "nt":
            candidates.extend(
                Path(root) / "Git" / suffix / "git.exe"
                for root in (
                    r"C:\Program Files",
                    r"C:\Program Files (x86)",
                )
                for suffix in ("cmd", "bin")
            )
        else:
            candidates.extend(
                Path(path)
                for path in (
                    "/usr/bin/git",
                    "/usr/local/bin/git",
                    "/opt/homebrew/bin/git",
                    "/bin/git",
                )
            )
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if resolved == workspace or workspace in resolved.parents:
                continue
            if ".venv" in resolved.parts:
                continue
            if resolved.is_file() and resolved.name.lower() in {"git", "git.exe"}:
                return resolved
        raise ExecutionError("找不到固定的系统 Git 可执行文件")

    @staticmethod
    def _reject_url_userinfo(args: Sequence[str]) -> None:
        for arg in args:
            if "::" in arg:
                raise ExecutionError("Git remote-helper URL 不被允许")
            if _looks_like_scp_remote(arg):
                userinfo, target = arg.split("@", 1)
                if ":" in userinfo or userinfo != "git":
                    raise ExecutionError("Git URL 不得包含不安全的 SSH userinfo")
                if "?" in target or "#" in target:
                    raise ExecutionError("Git URL 不得包含 query 或 fragment")
                continue
            try:
                parsed = urlsplit(arg)
            except ValueError as exc:
                raise ExecutionError("Git URL 参数无效") from exc
            scheme = parsed.scheme.lower()
            if scheme in {"http", "https"}:
                if not parsed.netloc:
                    raise ExecutionError("Git HTTP URL 缺少 host")
                if (
                    parsed.username is not None
                    or parsed.password is not None
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ExecutionError("Git HTTP URL 不得包含 userinfo、query 或 fragment")
            elif scheme == "ssh":
                if (
                    not parsed.netloc
                    or parsed.username != "git"
                    or parsed.password is not None
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ExecutionError("Git SSH URL 不是明确允许的安全形式")
            elif scheme in {"git", "file"}:
                raise ExecutionError(f"Git URL scheme 不被允许: {scheme}")
            elif parsed.netloc or "://" in arg:
                raise ExecutionError(f"Git URL scheme 不被允许: {scheme or 'unknown'}")

    @staticmethod
    def _reject_unsafe_git_options(args: Sequence[str]) -> None:
        """拒绝能改变 Git 执行根、配置或远程 helper 的选项。

        TrustedGitRunner 的调用方是应用控制面，仍然要在 runner 边界重新拒绝
        这些选项：它们可以绕过本 runner 注入的临时 HOME/config/hooks，或者让
        Git 执行调用方指定的 helper。短参数的连写形式也必须拒绝，例如
        ``-cfoo.bar=baz`` 和 ``-C/tmp/outside``。
        """

        exact = {
            "-c",
            "-C",
            "--config-env",
            "--exec-path",
            "--file",
            "--git-dir",
            "--global",
            "--local",
            "--system",
            "--separate-git-dir",
            "--upload-pack",
            "--work-tree",
            "--worktree",
            "--receive-pack",
            "--ext-diff",
            "--textconv",
        }
        prefixes = (
            "--config-env=",
            "--exec-path=",
            "--file=",
            "--git-dir=",
            "--separate-git-dir=",
            "--upload-pack=",
            "--work-tree=",
            "--receive-pack=",
            "-c",
            "-C",
        )
        for arg in args[1:]:
            lowered = arg.lower()
            if arg in exact or lowered.startswith(prefixes):
                raise ExecutionError("Git argv 包含不可覆盖的执行或配置选项")

    def _validate_workspace_git_layout(self) -> None:
        """确认 .git 目录/指针只指向当前受控仓库范围。"""

        git_entry = self.workspace / ".git"
        if not os.path.lexists(str(git_entry)):
            return
        repo_root = self._controlled_repo_root()
        entry_resolved = self._resolve_controlled_path(git_entry, repo_root, ".git")
        if entry_resolved.is_dir():
            self._validate_git_config_paths((entry_resolved,), repo_root)
            return
        if not entry_resolved.is_file():
            raise ExecutionError("Git .git 元数据不是目录或 gitdir 指针文件")
        try:
            pointer = entry_resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ExecutionError("Git gitdir 指针无法读取") from exc
        if "\x00" in pointer or len(pointer) > 4_096:
            raise ExecutionError("Git gitdir 指针格式无效")
        lines = [line.strip() for line in pointer.splitlines() if line.strip()]
        if len(lines) != 1 or not lines[0].lower().startswith("gitdir:"):
            raise ExecutionError("Git gitdir 指针格式无效")
        raw_target = lines[0][len("gitdir:") :].strip()
        if not raw_target:
            raise ExecutionError("Git gitdir 指针为空")
        target = Path(raw_target)
        if not target.is_absolute():
            target = entry_resolved.parent / target
        target_resolved = self._resolve_controlled_path(
            target,
            repo_root,
            "gitdir",
        )
        if not target_resolved.is_dir():
            raise ExecutionError("Git gitdir 指针目标不是目录")
        relative_parts = target_resolved.relative_to(repo_root).parts
        if ".git" not in relative_parts or "worktrees" not in relative_parts:
            raise ExecutionError("Git gitdir 指针不符合受控 worktree 结构")
        git_index = relative_parts.index(".git")
        common_git_dir = repo_root.joinpath(*relative_parts[:git_index], ".git")
        common_git_dir = self._resolve_controlled_path(
            common_git_dir,
            repo_root,
            "common gitdir",
        )
        if not common_git_dir.is_dir():
            raise ExecutionError("Git common gitdir 不是目录")
        self._validate_worktree_pointer(
            target_resolved / "commondir",
            repo_root,
            expected=common_git_dir,
            label="commondir",
        )
        self._validate_worktree_pointer(
            target_resolved / "gitdir",
            repo_root,
            expected=entry_resolved,
            label="worktree gitdir",
        )
        self._validate_git_config_paths(
            (target_resolved, common_git_dir),
            repo_root,
        )

    @classmethod
    def _validate_worktree_pointer(
        cls,
        pointer_path: Path,
        repo_root: Path,
        *,
        expected: Path,
        label: str,
    ) -> None:
        """校验 worktree 元数据中的 commondir/gitdir 指针。

        Git worktree 的 ``.git`` 文件只指向 ``.git/worktrees/<name>``；该目录
        又能通过 ``commondir`` 或 ``gitdir`` 把 Git 控制面重定向到任意路径。
        这些二级指针必须存在于 workspace_service 管理的仓库根内，并且指向
        当前已验证的目标，不能只校验最外层 ``.git`` 文件。
        """

        if not os.path.lexists(str(pointer_path)):
            return
        pointer = cls._resolve_controlled_path(pointer_path, repo_root, label)
        if not pointer.is_file():
            raise ExecutionError(f"Git {label} 指针不是文件")
        try:
            text = pointer.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ExecutionError(f"Git {label} 指针无法读取") from exc
        if "\x00" in text or len(text) > 4_096:
            raise ExecutionError(f"Git {label} 指针格式无效")
        raw_target = text.strip()
        if not raw_target or "\n" in raw_target or "\r" in raw_target:
            raise ExecutionError(f"Git {label} 指针格式无效")
        target = Path(raw_target)
        if not target.is_absolute():
            target = pointer.parent / target
        target_resolved = cls._resolve_controlled_path(target, repo_root, label)
        expected_resolved = cls._resolve_controlled_path(expected, repo_root, label)
        if target_resolved != expected_resolved:
            raise ExecutionError(f"Git {label} 指针不指向当前受控目标")

    def _controlled_repo_root(self) -> Path:
        base_dir = self.workspace_service.base_dir.resolve()
        try:
            relative = self.workspace.relative_to(base_dir)
        except ValueError as exc:
            raise ExecutionError("Git 工作区不在 workspace_service 控制根内") from exc
        if len(relative.parts) < 2:
            raise ExecutionError("Git 工作区缺少 owner/repository 身份")
        repo_root = (base_dir / relative.parts[0] / relative.parts[1]).resolve()
        try:
            repo_root.relative_to(base_dir)
        except ValueError as exc:
            raise ExecutionError("Git 仓库根不在 workspace_service 控制根内") from exc
        return repo_root

    @staticmethod
    def _resolve_controlled_path(path: Path, root: Path, label: str) -> Path:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ExecutionError(f"Git {label} 路径跳出受控仓库范围") from exc
        return resolved

    @classmethod
    def _validate_git_config_paths(
        cls,
        git_dirs: Sequence[Path],
        repo_root: Path,
    ) -> None:
        for git_dir in git_dirs:
            for config_name in ("config", "config.worktree"):
                config_path = git_dir / config_name
                if os.path.lexists(str(config_path)):
                    cls._resolve_controlled_path(config_path, repo_root, config_name)

    def _create_git_runtime(self) -> tuple[Path, dict[str, str], Path]:
        runtime_dir = Path(tempfile.mkdtemp(prefix="sakura-git-runtime-"))
        try:
            home_dir = runtime_dir / "home"
            xdg_dir = runtime_dir / "xdg"
            hooks_dir = runtime_dir / "hooks"
            home_dir.mkdir()
            xdg_dir.mkdir()
            hooks_dir.mkdir()
            global_config = runtime_dir / "global.config"
            system_config = runtime_dir / "system.config"
            global_config.write_text("", encoding="utf-8")
            system_config.write_text("", encoding="utf-8")
            env = {
                "HOME": str(home_dir),
                "USERPROFILE": str(home_dir),
                "XDG_CONFIG_HOME": str(xdg_dir),
                "GIT_CONFIG_GLOBAL": str(global_config),
                "GIT_CONFIG_SYSTEM": str(system_config),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_PAGER": "cat",
                "GIT_EDITOR": "true",
                "GIT_SEQUENCE_EDITOR": "true",
            }
            return runtime_dir, env, hooks_dir
        except Exception:
            self._cleanup_temporary_directory(runtime_dir, "Git 运行时目录")
            raise

    async def _validate_local_git_config(
        self,
        runtime_env: Mapping[str, str],
        hooks_dir: Path,
    ) -> None:
        git_entry = self.workspace / ".git"
        if not os.path.lexists(str(git_entry)):
            return
        config_args = (
            str(self.git_path),
            *_git_config_arguments(hooks_dir),
            "config",
            "--local",
            "--null",
            "--name-only",
            "--list",
            "--no-includes",
        )
        request = ExecutionRequest(
            workspace_key=execution_workspace_key(
                self.workspace, self.workspace_service
            ),
            argv=config_args,
            cwd=PurePosixPath("."),
            profile=ExecutionProfile.TRUSTED_CONTROL,
            env=runtime_env,
        )
        result = await LocalExecutionRunner.execute(self, request)
        if result.returncode != 0:
            raise ExecutionError("Git 配置检查失败")
        keys = [key.strip().lower() for key in result.stdout.split("\x00") if key.strip()]
        unsafe = [key for key in keys if _is_unsafe_git_config_key(key)]
        if unsafe:
            raise ExecutionError(f"Git 配置包含禁止项: {unsafe[0]}")
        await self._validate_local_remote_urls(runtime_env, hooks_dir, keys)

    async def _validate_local_remote_urls(
        self,
        runtime_env: Mapping[str, str],
        hooks_dir: Path,
        keys: Sequence[str],
    ) -> None:
        """校验 local remote URL 值，防止凭据和危险 transport 持久化。"""

        remote_keys = [
            key
            for key in keys
            if key.startswith("remote.")
            and key.rsplit(".", 1)[-1] in {"url", "pushurl"}
        ]
        for key in remote_keys:
            request = ExecutionRequest(
                workspace_key=execution_workspace_key(
                    self.workspace, self.workspace_service
                ),
                argv=(
                    str(self.git_path),
                    *_git_config_arguments(hooks_dir),
                    "config",
                    "--local",
                    "--no-includes",
                    "--get-all",
                    key,
                ),
                cwd=PurePosixPath("."),
                profile=ExecutionProfile.TRUSTED_CONTROL,
                env=runtime_env,
            )
            result = await LocalExecutionRunner.execute(self, request)
            if result.returncode != 0:
                raise ExecutionError("Git remote URL 检查失败")
            for value in result.stdout.splitlines():
                if not value.strip():
                    raise ExecutionError("Git remote URL 不能为空")
                self._reject_url_userinfo(("git", "clone", value.strip()))

    def _build_safe_git_argv(
        self,
        args: Sequence[str],
        hooks_dir: Path,
    ) -> tuple[str, ...]:
        rest = list(args[1:])
        if any(arg in {"-c", "--config-env", "--ext-diff", "--textconv"} for arg in rest):
            raise ExecutionError("Git argv 包含不可覆盖的配置或 diff 选项")
        safe_args: list[str] = [str(self.git_path)]
        safe_args.extend(_git_config_arguments(hooks_dir))
        if rest and rest[0].lower() == "diff":
            safe_args.extend((rest[0], "--no-ext-diff", "--no-textconv", *rest[1:]))
        else:
            safe_args.extend(rest)
        return tuple(safe_args)

    async def run(
        self,
        command_or_request: str | ExecutionRequest,
        cwd: str | Path = ".",
        timeout_seconds: float = 600,
        *,
        profile: ExecutionProfile = ExecutionProfile.TRUSTED_CONTROL,
    ) -> ExecutionResult:
        if isinstance(command_or_request, ExecutionRequest):
            if command_or_request.profile is not ExecutionProfile.TRUSTED_CONTROL:
                raise UnsupportedExecutionProfile(
                    "TrustedGitRunner 只接受 TRUSTED_CONTROL 请求"
                )
            # ExecutionRequest is already the fully specified protocol object.
            # Silently ignoring outer values would make a caller believe its
            # cwd/timeout/profile took effect, and could undermine auditing of
            # the command boundary.  Keep the legacy wrapper usable only when
            # all outer values are their documented defaults.
            if (
                PurePosixPath(str(cwd).replace("\\", "/")) != PurePosixPath(".")
                or timeout_seconds != 600
                or profile is not ExecutionProfile.TRUSTED_CONTROL
            ):
                raise ValueError(
                    "ExecutionRequest 不得同时提供非默认 cwd、timeout 或 profile"
                )
            return await self.execute(command_or_request)
        raise ValueError("TrustedGitRunner 只接受 Git argv，不接受 shell command")

    async def run_args(
        self,
        args: Sequence[str],
        cwd: str | Path = ".",
        timeout_seconds: float = 600,
        *,
        profile: ExecutionProfile = ExecutionProfile.TRUSTED_CONTROL,
        credential_token: str | None = None,
        trusted_expected_remote: str | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> ExecutionResult:
        """Run one trusted Git request under the repository metadata lock."""

        async with _workspace_git_lock(self._git_lock_scope()):
            return await self._run_args_locked(
                args,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                profile=profile,
                credential_token=credential_token,
                trusted_expected_remote=trusted_expected_remote,
                extra_env=extra_env,
            )

    async def _run_args_locked(
        self,
        args: Sequence[str],
        cwd: str | Path = ".",
        timeout_seconds: float = 600,
        *,
        profile: ExecutionProfile = ExecutionProfile.TRUSTED_CONTROL,
        credential_token: str | None = None,
        trusted_expected_remote: str | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> ExecutionResult:
        args_tuple = tuple(str(arg) for arg in args)
        if not args_tuple or args_tuple[0].lower() not in {"git", "git.exe"}:
            raise ValueError("TrustedGitRunner 只接受 git argv")
        if profile is not ExecutionProfile.TRUSTED_CONTROL:
            raise UnsupportedExecutionProfile(
                "TrustedGitRunner 只接受 TRUSTED_CONTROL profile"
            )

        self._reject_url_userinfo(args_tuple)
        self._reject_unsafe_git_options(args_tuple)
        separator_seen = False
        for arg in args_tuple:
            if arg == "--":
                separator_seen = True
                continue
            if not separator_seen:
                self._validate_command_arg(arg)
        requested_env = dict(extra_env or {})
        if set(requested_env).intersection(_TRUSTED_INTERNAL_ENV_KEYS):
            raise ExecutionError("Git 内部环境变量只能由 runner 注入")
        if "GIT_ASKPASS" in requested_env:
            raise ExecutionError("Git askpass 只能由 runner 生成")
        unknown_env = set(requested_env).difference(_ALLOWED_EXECUTION_ENV_KEYS)
        if unknown_env:
            raise ExecutionError(
                f"Git 环境变量不在受信任 allowlist: {sorted(unknown_env)}"
            )
        for key, expected in {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
        }.items():
            if requested_env.get(key) not in {None, expected}:
                raise ExecutionError(f"Git 环境变量 {key} 不符合固定策略")
        if credential_token and not trusted_expected_remote:
            raise ExecutionError(
                "携带 Git 凭据时必须提供 trusted_expected_remote"
            )
        if credential_token:
            normalize_trusted_remote_url(trusted_expected_remote or "")
        self._validate_workspace_git_layout()
        # Run this before any Git-backed config/remote preflight as well as before
        # askpass creation. This keeps every TrustedGit entry point fail-closed.
        await self._validate_git_metadata_before_execution()
        await self._validate_workspace_git_config_before_token()
        if credential_token:
            await self._validate_credential_remote(
                args_tuple,
                trusted_expected_remote or "",
            )

        askpass_dir: Path | None = None
        askpass_path: Path | None = None
        # 即使没有 token，也禁止 Git 进入交互式 prompt 或读取系统级 credential
        # helper；失败必须返回受控错误，不能让 Web worker 无限等待。
        merged_env = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            **requested_env,
        }
        if credential_token:
            askpass_dir = self._create_askpass(credential_token)
            askpass_path = askpass_dir / (
                "askpass.cmd" if os.name == "nt" else "askpass"
            )
            self._active_askpass_paths.add(askpass_path.resolve())
            self._active_askpass_tokens[askpass_path.resolve()] = credential_token
            merged_env.update(
                {
                    "GIT_ASKPASS": str(askpass_path),
                }
            )
        try:
            request = ExecutionRequest(
                workspace_key=execution_workspace_key(
                    self.workspace,
                    self.workspace_service,
                ),
                argv=args_tuple,
                cwd=PurePosixPath(str(cwd).replace("\\", "/")),
                profile=ExecutionProfile.TRUSTED_CONTROL,
                timeout_seconds=timeout_seconds,
                env=merged_env,
            )
            # The outer run_args lock remains held while execute performs its
            # final metadata check and spawns Git.  The repository lock is
            # task-reentrant so the shared public execute boundary can still
            # be used (and remains easy to instrument in tests).
            result = await self.execute(request)
            return self._redact_result(
                result,
                credential_token,
                display_command=_display_args(args_tuple),
            )
        except ExecutionError as exc:
            message = self._redact_text(str(exc), credential_token)
            if message != str(exc):
                raise ExecutionError(message) from exc
            raise
        except Exception as exc:
            message = self._redact_text(str(exc), credential_token)
            raise ExecutionError(f"Trusted Git 执行失败: {message}") from exc
        finally:
            if askpass_path is not None:
                resolved_askpass = askpass_path.resolve()
                self._active_askpass_paths.discard(resolved_askpass)
                self._active_askpass_tokens.pop(resolved_askpass, None)
            if askpass_dir is not None:
                await self._cleanup_temporary_directory_async(
                    askpass_dir,
                    "Git askpass 临时目录",
                )

    async def _validate_workspace_git_config_before_token(self) -> None:
        if not os.path.lexists(str(self.workspace / ".git")):
            return
        runtime_dir, runtime_env, hooks_dir = self._create_git_runtime()
        try:
            await self._validate_local_git_config(runtime_env, hooks_dir)
        finally:
            await self._cleanup_temporary_directory_async(
                runtime_dir,
                "Git 运行时目录",
            )

    async def _validate_credential_remote(
        self,
        args: Sequence[str],
        expected_remote: str,
    ) -> None:
        """把 token 绑定到明确的 clone/fetch remote identity。"""

        expected_identity = normalize_trusted_remote_url(expected_remote)
        if len(args) < 2:
            raise ExecutionError("携带 Git 凭据时必须指定 remote 操作")
        operation = args[1].lower()
        if operation == "clone":
            candidates = [
                arg
                for arg in args[2:]
                if "://" in arg or _looks_like_scp_remote(arg)
            ]
            if len(candidates) != 1:
                raise ExecutionError("Git clone 凭据缺少唯一 expected remote")
            try:
                actual_identity = normalize_trusted_remote_url(candidates[0])
            except ExecutionError as exc:
                raise ExecutionError("Git clone remote 与 expected remote 不匹配") from exc
            if actual_identity != expected_identity:
                raise ExecutionError("Git clone remote 与 expected remote 不匹配")
            return

        if operation != "fetch":
            raise ExecutionError(
                "携带 Git 凭据只允许绑定的 clone/fetch 操作"
            )
        remote_args = [arg for arg in args[2:] if not arg.startswith("-")]
        if not remote_args:
            raise ExecutionError("Git fetch 凭据缺少明确 remote")
        remote_name = remote_args[0]
        if "://" in remote_name or _looks_like_scp_remote(remote_name):
            actual_remote = remote_name
        else:
            if not _REMOTE_NAME_RE.fullmatch(remote_name):
                raise ExecutionError("Git fetch remote 名称无效")
            actual_remote = await self._read_local_remote_url(remote_name)
        try:
            actual_identity = normalize_trusted_remote_url(actual_remote)
        except ExecutionError as exc:
            raise ExecutionError("Git fetch remote 与 expected remote 不匹配") from exc
        if actual_identity != expected_identity:
            raise ExecutionError("Git fetch remote 与 expected remote 不匹配")

    async def _read_local_remote_url(self, remote_name: str) -> str:
        """在同一份受控 Git config 下读取 remote URL，不执行 remote 操作。"""

        runtime_dir, runtime_env, hooks_dir = self._create_git_runtime()
        try:
            await self._validate_local_git_config(runtime_env, hooks_dir)
            request = ExecutionRequest(
                workspace_key=execution_workspace_key(
                    self.workspace, self.workspace_service
                ),
                argv=(
                    str(self.git_path),
                    *_git_config_arguments(hooks_dir),
                    "config",
                    "--local",
                    "--no-includes",
                    "--get-all",
                    f"remote.{remote_name}.url",
                ),
                cwd=PurePosixPath("."),
                profile=ExecutionProfile.TRUSTED_CONTROL,
                env=runtime_env,
            )
            result = await LocalExecutionRunner.execute(self, request)
            if result.returncode != 0:
                raise ExecutionError("Git remote URL 读取失败")
            values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if len(values) != 1:
                raise ExecutionError("Git remote 必须恰好有一个 URL")
            return values[0]
        finally:
            await self._cleanup_temporary_directory_async(
                runtime_dir,
                "Git 运行时目录",
            )

    async def _validate_git_metadata_before_execution(self) -> None:
        """在线程池中检查 Git metadata，避免阻塞 asyncio event loop。

        Git 的 common ``.git`` 目录可能包含数量巨大的 objects。安全边界
        只需要检查 Git 会用来重定向路径、执行 hook 或访问 refs/logs 的
        metadata；不会在每次控制面调用时递归整个 common repository。
        """

        await asyncio.to_thread(self._validate_git_metadata_snapshot)

    def _validate_git_metadata_snapshot(self) -> None:
        """串行化一次 metadata 扫描，避免并发请求重复递归扫描。"""

        with self._metadata_scan_lock:
            self._validate_git_metadata_snapshot_locked()

    def _validate_git_metadata_snapshot_locked(self) -> None:
        """检查安全相关 metadata 路径，不复用可变树的旧快照。"""

        roots = self._git_metadata_roots()
        repo_root = self._controlled_repo_root()
        git_entry = self.workspace / ".git"
        if os.path.lexists(str(git_entry)):
            self._reject_metadata_node(git_entry, repo_root, ".git")

        for root in roots:
            self._reject_metadata_node(root, repo_root, "Git metadata")

            # These files/directories are the bounded set of metadata that the
            # trusted Git commands can use for path redirection, executable
            # hooks, or index/ref state.  In particular, do not recursively
            # walk objects: object databases are routinely very large and the
            # alternates files are checked explicitly below.
            direct_paths = (
                Path("config"),
                Path("config.worktree"),
                Path("HEAD"),
                Path("index"),
                Path("index.lock"),
                Path("packed-refs"),
                Path("shallow"),
                Path("FETCH_HEAD"),
                Path("ORIG_HEAD"),
                Path("MERGE_HEAD"),
                Path("CHERRY_PICK_HEAD"),
                Path("REVERT_HEAD"),
                Path("BISECT_HEAD"),
                Path("hooks"),
                Path("info"),
            )
            for relative_path in direct_paths:
                candidate = root / relative_path
                if os.path.lexists(str(candidate)):
                    self._reject_metadata_node(
                        candidate,
                        repo_root,
                        f"Git metadata {relative_path}",
                    )

            objects = root / "objects"
            if os.path.lexists(str(objects)):
                self._validate_git_objects_layout(objects, repo_root)

            # Refs and reflogs are the only potentially nested trees we need
            # to inspect.  They are rescanned on every invocation because a
            # mutable task gitdir may replace a leaf while preserving the
            # identity/timestamps of its ancestor directory.
            for relative_path in (Path("refs"), Path("logs")):
                candidate = root / relative_path
                if os.path.lexists(str(candidate)):
                    self._reject_metadata_tree(
                        candidate,
                        repo_root,
                        f"Git metadata {relative_path}",
                    )

            for alternate_name in (
                Path("objects") / "info" / "alternates",
                Path("objects") / "info" / "http-alternates",
            ):
                alternate = root / alternate_name
                if os.path.lexists(str(alternate)):
                    self._reject_metadata_node(alternate, repo_root, "Git alternates")
                    raise ExecutionError("Git objects alternates 不被允许")

    def _git_metadata_roots(self) -> tuple[Path, ...]:
        """返回当前 workspace 会被 Git 访问的 common/worktree metadata 根。"""

        git_entry = self.workspace / ".git"
        if not os.path.lexists(str(git_entry)):
            return ()
        repo_root = self._controlled_repo_root()
        entry_resolved = self._resolve_controlled_path(git_entry, repo_root, ".git")
        if entry_resolved.is_dir():
            return (entry_resolved,)
        try:
            pointer = entry_resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ExecutionError("Git gitdir 指针无法读取") from exc
        lines = [line.strip() for line in pointer.splitlines() if line.strip()]
        if len(lines) != 1 or not lines[0].lower().startswith("gitdir:"):
            raise ExecutionError("Git gitdir 指针格式无效")
        target = Path(lines[0][len("gitdir:") :].strip())
        if not target.is_absolute():
            target = entry_resolved.parent / target
        self._reject_metadata_components(target, repo_root, "Git metadata")
        target_resolved = self._resolve_controlled_path(target, repo_root, "gitdir")
        relative_parts = target_resolved.relative_to(repo_root).parts
        try:
            git_index = relative_parts.index(".git")
            worktrees_index = relative_parts.index("worktrees")
        except ValueError as exc:
            raise ExecutionError("Git gitdir 指针不符合受控 worktree 结构") from exc
        if worktrees_index <= git_index:
            raise ExecutionError("Git gitdir 指针不符合受控 worktree 结构")
        common_git_dir = repo_root.joinpath(*relative_parts[:git_index], ".git")
        self._reject_metadata_components(common_git_dir, repo_root, "Git metadata")
        common_git_dir = self._resolve_controlled_path(
            common_git_dir,
            repo_root,
            "common gitdir",
        )
        if not common_git_dir.is_dir():
            raise ExecutionError("Git common gitdir 不是目录")
        return common_git_dir, target_resolved

    @classmethod
    def _reject_metadata_node(
        cls,
        path: Path,
        repo_root: Path,
        label: str,
    ) -> None:
        """lstat 一个 metadata 节点，拒绝 symlink/reparse 路径。"""

        cls._reject_metadata_components(path, repo_root, label)
        try:
            node_stat = os.lstat(path)
        except OSError as exc:
            raise ExecutionError(f"{label} 无法 lstat: {path}") from exc
        if cls._is_reparse_stat(node_stat):
            raise ExecutionError(f"{label} 路径不得包含 symlink/reparse")

    def _reject_metadata_tree(
        self,
        path: Path,
        repo_root: Path,
        label: str,
    ) -> None:
        """有限递归检查 refs/logs，每次都 lstat 每个叶子节点。"""

        self._reject_metadata_node(path, repo_root, label)
        try:
            root_stat = os.lstat(path)
        except OSError as exc:
            raise ExecutionError(f"{label} 无法 lstat: {path}") from exc
        if not stat.S_ISDIR(root_stat.st_mode):
            return

        # Some Windows filesystems do not reliably update a parent directory's
        # timestamp when a child reparse point is created.  Walk every node so
        # a replacement of a refs/logs leaf cannot be hidden by a stale cache.
        stack: list[Path] = [path]
        nodes_seen = 1
        if nodes_seen > MAX_GIT_REFS_LOGS_NODES:
            raise ExecutionError(f"{label} 节点数量超过上限")
        while stack:
            current = stack.pop()
            try:
                current_stat = os.lstat(current)
            except OSError as exc:
                raise ExecutionError(f"{label} 无法 lstat: {current}") from exc
            if self._is_reparse_stat(current_stat):
                raise ExecutionError(f"{label} 路径不得包含 symlink/reparse")
            if not stat.S_ISDIR(current_stat.st_mode):
                continue

            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        child = Path(entry.path)
                        try:
                            child_stat = os.lstat(child)
                        except OSError as exc:
                            raise ExecutionError(
                                f"{label} 无法 lstat: {child}"
                            ) from exc
                        if self._is_reparse_stat(child_stat):
                            raise ExecutionError(
                                f"{label} 不得包含 symlink/reparse: {child.name}"
                            )
                        nodes_seen += 1
                        if nodes_seen > MAX_GIT_REFS_LOGS_NODES:
                            raise ExecutionError(f"{label} 节点数量超过上限")
                        if stat.S_ISDIR(child_stat.st_mode):
                            stack.append(child)
            except OSError as exc:
                raise ExecutionError(f"{label} 无法遍历") from exc

    def _validate_git_objects_layout(
        self,
        objects: Path,
        repo_root: Path,
    ) -> None:
        """Check object fanout roots without walking loose object files.

        The common object database may contain millions of files.  Its
        security-relevant structure is bounded: every direct child is lstat'd,
        two-hex fanout roots are checked as directories, and ``pack``/``info``
        have their direct children checked for reparse points.  We never walk
        inside the fanout directories themselves.
        """

        self._reject_metadata_node(objects, repo_root, "Git objects")
        try:
            objects_stat = os.lstat(objects)
        except OSError as exc:
            raise ExecutionError(f"Git objects 无法 lstat: {objects}") from exc
        if not stat.S_ISDIR(objects_stat.st_mode):
            return

        direct_nodes_seen = 0
        try:
            with os.scandir(objects) as entries:
                for entry in entries:
                    direct_nodes_seen += 1
                    if direct_nodes_seen > MAX_GIT_OBJECTS_DIRECT_NODES:
                        raise ExecutionError("Git objects 直属节点数量超过上限")
                    child = Path(entry.path)
                    try:
                        child_stat = os.lstat(child)
                    except OSError as exc:
                        raise ExecutionError(
                            f"Git objects 无法 lstat: {child}"
                        ) from exc
                    if self._is_reparse_stat(child_stat):
                        raise ExecutionError(
                            f"Git objects 不得包含 symlink/reparse: {child.name}"
                        )
                    if not stat.S_ISDIR(child_stat.st_mode):
                        continue
                    if child.name.lower() in {"pack", "info"}:
                        self._reject_metadata_direct_children(
                            child,
                            repo_root,
                            f"Git objects/{child.name}",
                        )
        except OSError as exc:
            raise ExecutionError("Git objects 无法遍历直属目录") from exc

    def _reject_metadata_direct_children(
        self,
        path: Path,
        repo_root: Path,
        label: str,
    ) -> None:
        """lstat one bounded metadata directory level without recursion."""

        nodes_seen = 0
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    nodes_seen += 1
                    if nodes_seen > MAX_GIT_OBJECTS_AUX_NODES:
                        raise ExecutionError(f"{label} 节点数量超过上限")
                    child = Path(entry.path)
                    try:
                        child_stat = os.lstat(child)
                    except OSError as exc:
                        raise ExecutionError(
                            f"{label} 无法 lstat: {child}"
                        ) from exc
                    if self._is_reparse_stat(child_stat):
                        raise ExecutionError(
                            f"{label} 不得包含 symlink/reparse: {child.name}"
                        )
                    self._reject_metadata_components(child, repo_root, label)
        except OSError as exc:
            raise ExecutionError(f"{label} 无法遍历直属节点") from exc

    @classmethod
    def _reject_metadata_components(
        cls,
        path: Path,
        repo_root: Path,
        label: str,
    ) -> None:
        current = path
        try:
            relative = current.relative_to(repo_root)
        except ValueError as exc:
            raise ExecutionError(f"{label} 路径跳出受控仓库") from exc
        for _ in relative.parts:
            try:
                current_stat = os.lstat(current)
            except OSError as exc:
                raise ExecutionError(f"{label} 无法 lstat: {current}") from exc
            if cls._is_reparse_stat(current_stat):
                raise ExecutionError(f"{label} 路径不得包含 symlink/reparse")
            current = current.parent

    @staticmethod
    def _is_reparse_stat(file_stat: os.stat_result) -> bool:
        reparse_flag = 0x400 if os.name == "nt" else 0
        return stat.S_ISLNK(file_stat.st_mode) or bool(
            getattr(file_stat, "st_file_attributes", 0) & reparse_flag
        )

    @staticmethod
    def _redact_text(value: str, token: str | None) -> str:
        if token:
            return value.replace(token, "***")
        return value

    def _redact_result(
        self,
        result: ExecutionResult,
        token: str | None,
        *,
        display_command: str,
    ) -> ExecutionResult:
        return replace(
            result,
            command=self._redact_text(display_command, token),
            stdout=self._redact_text(result.stdout, token),
            stderr=self._redact_text(result.stderr, token),
            infrastructure_error=self._redact_text(
                result.infrastructure_error or "", token
            )
            or None,
        )

    @staticmethod
    def _cleanup_askpass(directory: Path) -> None:
        TrustedGitRunner._cleanup_temporary_directory(directory, "Git askpass 临时目录")

    @staticmethod
    def _cleanup_temporary_directory(directory: Path, label: str) -> None:
        last_error: OSError | None = None
        for _attempt in range(3):
            try:
                shutil.rmtree(directory)
                return
            except FileNotFoundError:
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.01)
        raise ExecutionError(f"{label}清理失败: {directory}") from last_error

    @staticmethod
    async def _cleanup_temporary_directory_async(
        directory: Path,
        label: str,
    ) -> None:
        """Run retrying temporary-directory cleanup outside the event loop."""

        cleanup_task = asyncio.create_task(
            asyncio.to_thread(
                TrustedGitRunner._cleanup_temporary_directory,
                directory,
                label,
            )
        )
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # A cancelled Git request must still finish its bounded cleanup
            # before propagating cancellation.  The worker thread performs no
            # event-loop work and is limited to the existing three attempts.
            await cleanup_task
            raise

    @staticmethod
    def _create_askpass(token: str) -> Path:
        if not token or "\x00" in token:
            raise ValueError("Git 凭据不能为空或含空字节")
        directory = Path(tempfile.mkdtemp(prefix="sakura-git-"))
        try:
            token_path = directory / "token"
            token_path.write_text(token + "\n", encoding="utf-8")
            try:
                token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            if os.name == "nt":
                askpass_path = directory / "askpass.cmd"
                askpass_path.write_text(
                    "@echo off\r\n"
                    '@echo %~1 | findstr /i "username" >nul\r\n'
                    "@if not errorlevel 1 (echo x-access-token & exit /b 0)\r\n"
                    f'@for /f "usebackq delims=" %%A in ("{token_path}") do @echo(%%A\r\n',
                    encoding="utf-8",
                )
            else:
                askpass_path = directory / "askpass"
                askpass_path.write_text(
                    "#!/bin/sh\n"
                    'case "${1:-}" in\n'
                    "  *[Uu]sername*) printf '%s\\n' 'x-access-token' ;;\n"
                    f"  *) cat {shlex_quote(str(token_path))} ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
                askpass_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            return directory
        except Exception:
            # 清理失败本身比原始写文件错误更重要，必须显式上报。
            TrustedGitRunner._cleanup_askpass(directory)
            raise


def _looks_like_scp_remote(value: str) -> bool:
    """识别 ``user@host:path`` 形式，避免把凭据误当普通参数。"""

    if "://" in value or "\x00" in value:
        return False
    at_index = value.find("@")
    if at_index <= 0:
        return False
    colon_index = value.find(":")
    if 0 < colon_index < at_index:
        return True
    host_path = value[at_index + 1 :]
    return bool(host_path and ":" in host_path and "/" not in value[:at_index])


def _git_config_arguments(hooks_dir: Path) -> tuple[str, ...]:
    """生成不可由工作区配置覆盖的 Git 配置参数。"""

    # 即使工作区没有声明 core.hooksPath，也必须把真实仓库 hooks 隔离到
    # 本次请求创建的空目录。否则 git commit 等控制面操作会执行工作区中的
    # pre-commit/post-commit 脚本。
    arguments: list[str] = ["-c", f"core.hooksPath={hooks_dir}"]
    for key, value in _GIT_SAFE_CONFIG:
        arguments.extend(("-c", f"{key}={value}"))
    return tuple(arguments)


def _is_unsafe_git_config_key(key: str) -> bool:
    """判断 local config key 是否可能执行代码或重定向凭据/网络。"""

    normalized = key.strip().lower()
    if not normalized:
        return False
    section, _, _ = normalized.partition(".")
    last_name = normalized.rsplit(".", 1)[-1]
    if section in {"include", "includeif", "filter", "alias", "pager"}:
        return True
    if normalized.startswith(("filter.", "url.", "include.", "includeif.")):
        return True
    if normalized.startswith("diff.") and last_name in {
        "external",
        "command",
        "textconv",
        "cachetextconv",
    }:
        return True
    if normalized.startswith("merge.") and last_name in {"driver", "recursive"}:
        return True
    if normalized.startswith("mergetool.") and last_name in {"cmd", "path"}:
        return True
    if normalized.startswith("difftool.") and last_name in {"cmd", "path"}:
        return True
    if normalized.startswith("remote.") and last_name in {
        "proxy",
        "uploadpack",
        "receivepack",
        "vcs",
    }:
        return True
    if normalized.startswith("submodule.") and last_name == "update":
        return True
    if normalized.startswith("http."):
        return True
    if section == "credential" and last_name == "helper":
        return True
    if section == "core" and last_name in {
        "fsmonitor",
        "askpass",
        "hookspath",
        "sshcommand",
        "gitproxy",
        "pager",
        "editor",
        "sequenceeditor",
        "worktree",
    }:
        return True
    return normalized.startswith("protocol.")


def shlex_quote(value: str) -> str:
    """最小 POSIX 单引号转义，避免额外引入 shell 依赖。"""

    return "'" + value.replace("'", "'\"'\"'") + "'"


async def execute_request(
    runner: Any,
    request: ExecutionRequest,
) -> ExecutionResult:
    """通过统一 ExecutionRunner 协议执行请求，缺失协议时 fail closed。"""

    execute = getattr(runner, "execute", None)
    if not callable(execute):
        raise ExecutionError("执行器缺少 execute 方法，拒绝协议外 fallback")
    return await execute(request)


def resolve_execution_runner(
    runner: Any,
    workspace: str | Path,
    workspace_service: AgentTeamWorkspaceService,
) -> ExecutionRunner:
    """Resolve an already-created runner, failing closed when omitted.

    Agent tools must receive a workspace-scoped runner from the worker.  This
    function intentionally does not construct ``LocalExecutionRunner``: doing
    so would turn a missing production injection into an implicit host escape.
    Explicit local development and fake runners are created by their caller.
    """

    del workspace, workspace_service
    if runner is None:
        raise ExecutionError(
            "Agent execution runner must be explicitly injected; refusing local fallback"
        )
    if not callable(getattr(runner, "execute", None)):
        raise ExecutionError("Agent execution runner has no execute method")
    return runner


def execution_workspace_key(
    workspace: str | Path,
    workspace_service: AgentTeamWorkspaceService | None = None,
) -> str:
    """生成稳定且不碰撞的 opaque workspace key。

    Phase 2 sandboxd 必须把该值当作服务端映射句柄再次校验；它不是授权凭据。
    使用 workspace_service 相对路径区分同名仓库，再附加稳定哈希避免截断碰撞。
    """

    resolved = Path(workspace).resolve()
    if workspace_service is not None:
        base_dir = workspace_service.base_dir.resolve()
        try:
            identity = "/".join(resolved.relative_to(base_dir).parts)
        except ValueError as exc:
            raise ValueError("工作区不在 workspace_service 控制根内") from exc
    else:
        identity = os.path.normcase(resolved.as_posix())
    if not identity:
        raise ValueError("工作区 identity 不能为空")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", identity).strip("-.") or "workspace"
    max_slug_length = MAX_WORKSPACE_KEY_LENGTH - len(digest) - 1
    slug = slug[:max_slug_length].rstrip("-.") or "workspace"
    key = f"{slug}-{digest}"
    if not _WORKSPACE_KEY_RE.fullmatch(key):
        raise ValueError("工作区 identity 不能转换为有效 workspace_key")
    return key


__all__ = [
    "ExecutionError",
    "ExecutionProfile",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionRunner",
    "LocalExecutionRunner",
    "TrustedGitRunner",
    "UnsupportedExecutionProfile",
    "execute_request",
    "execution_workspace_key",
    "normalize_trusted_remote_url",
    "resolve_execution_runner",
    "trusted_remote_urls_match",
]
