"""sakura_ai_updater 入口 — dev 模式 ``python -m sakura_ai_updater --serve`` + backend CLI。

Slice 3a：``--serve`` 在**单个 try/finally** 内完成 flock → socket 准备 → reconcile →
uvicorn，finally 清 socket + 释放 flock（覆盖 load/reconcile/save/create_app/Config 全部
异常路径）。Slice 3b Task 1：新增 ``backend install|start|stop|status|is-running`` CLI
（DaemonBackend 生命周期，Task 2 再接入 UDS pre-bind/bootstrap）。Slice 3c PyInstaller
二进制模式同样走此入口。

``locks`` 惰性导入：``locks.py`` 模块级 ``import fcntl`` 仅 POSIX 有；Windows 开发机
import 本模块（backend CLI / 测试）时不得失败，serve 时才真正需要锁。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from sakura_ai_updater import __version__
from sakura_ai_updater.backends import DaemonBackend
from sakura_ai_updater.backends.daemon import (
    DEFAULT_GID,
    DEFAULT_STARTUP_TIMEOUT,
    GIDConflictError,
    PrivilegeError,
    UnsafeDeploymentPathError,
    UpdaterNotInstalledError,
    UpdaterStartError,
)
from sakura_ai_updater.ipc import create_app
from sakura_ai_updater.socket_util import (
    bind_socket_listener,
    cleanup_owned_socket,
    prepare_socket_path,
)
from sakura_ai_updater.state import load_state, reconcile_interrupted_job, save_state

DEFAULT_SOCKET_PATH = "/run/sakura-ai/updater.sock"
DEFAULT_STATE_DIR = ".deploy/updater"

# backend CLI 已知异常 → 单行 ERROR stderr + SystemExit(1)，不打印 traceback
_BACKEND_ERRORS = (
    UpdaterNotInstalledError,
    UpdaterStartError,
    GIDConflictError,
    PrivilegeError,
    UnsafeDeploymentPathError,
)


def serve(
    socket_path: str,
    state_path: str,
    lock_path: str,
    *,
    socket_uid: int = 0,
    socket_gid: int = DEFAULT_GID,
    compose_file: str | None = None,
    deployment_env: str | None = None,
    health_url: str = "http://localhost:8000/health",
    disk_space_threshold: int = 2 * 1024 * 1024 * 1024,
) -> None:
    """启动 updater daemon：flock → reconcile → pre-bind listener → uvicorn serve。

    Task 2 transport 架构（spec §11.4）：host 进程预绑定 UDS、设置
    ``0o660 root:<socket_gid>`` 后把 listener 交给 uvicorn（``Config`` 不含
    ``uds=``；FastAPI app 保持纯 HTTP，无 socket path/group/mode 知识）。pre-bound
    listener 的 ownership/mode 在 uvicorn 接受连接前就绪——Web 容器经补充 GID
    只读挂载 connect。

    资源生命周期：lock_fd 与 listener 的清理覆盖全部步骤（含 load/reconcile/save/
    create_app/Config 异常路径）。Python 3.12 的 ``create_unix_server`` 无
    cleanup_socket（3.13 才加），故 updater 经 socket_util 自管 socket 文件。
    """
    # locks 惰性导入：locks.py 顶层 import fcntl，仅 POSIX；Windows 开发机不跑 serve
    from sakura_ai_updater.locks import (
        LockBusyError,
        acquire_process_lock,
        release_process_lock,
    )

    # 1. 进程唯一性（OS-level flock，§7.5 第一层锁）
    try:
        lock_fd = acquire_process_lock(lock_path)
    except LockBusyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    listener = None
    try:
        # 2. socket 准备（live/stale 真检测；父目录须存在，3b bootstrap）
        # Early-fail：在 flock 外提前检测 socket 路径可用性，避免 bind 时才发现目录缺失
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

        # 4. 构造 host-side orchestration dependencies.  The paths are explicit
        # CLI values; no component resolves compose/deployment files from the
        # daemon's current working directory.
        orchestrator = None
        if compose_file is not None and deployment_env is not None:
            from sakura_ai_updater.adapters.image import ImageAdapter
            from sakura_ai_updater.deployment import DeploymentStateProvider
            from sakura_ai_updater.jobs import JobOrchestrator
            from sakura_ai_updater.release_client import ReleaseClient

            adapter = ImageAdapter(
                compose_file=compose_file,
                deployment_env=deployment_env,
                health_url=health_url,
            )
            deployment = DeploymentStateProvider(
                deployment_env=deployment_env,
                health_url=health_url,
            )
            orchestrator = JobOrchestrator(
                state_path=state_path,
                adapter=adapter,
                release_client=ReleaseClient(),
                deployment=deployment,
                disk_space_threshold=disk_space_threshold,
            )

        # Keep the legacy one-argument call when no host paths were configured;
        # this preserves status/health-only development mode and old callers.
        app = (
            create_app(state_path, orchestrator=orchestrator)
            if orchestrator is not None
            else create_app(state_path)
        )
        listener = bind_socket_listener(
            socket_path, uid=socket_uid, gid=socket_gid, mode=0o660
        )

        # 5. uvicorn 用 external listener（Config 不含 uds=）
        import uvicorn

        config = uvicorn.Config(app, log_level="info")
        server = uvicorn.Server(config)
        print(f"sakura-ai-updater {__version__} listening on {socket_path}")
        asyncio.run(server.serve(sockets=[listener]))
    finally:
        # 6. 清理自己拥有的 socket + 释放 flock（覆盖上面任何步骤的异常）
        # Safety net: uvicorn shutdown 时接管 fd，此处 close() 防止 fd 泄漏
        if listener is not None:
            listener.close()
        cleanup_owned_socket(socket_path)
        release_process_lock(lock_fd)


def create_backend(
    state_dir: str = DEFAULT_STATE_DIR,
    socket_path: str = DEFAULT_SOCKET_PATH,
    binary_path: str | None = None,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    compose_file: str | None = None,
    deployment_env: str | None = None,
) -> DaemonBackend:
    """构造 backend（模块级工厂：CLI 与测试共用，便于 monkeypatch 注入）。"""
    return DaemonBackend(
        state_dir=state_dir,
        socket_path=socket_path,
        binary_path=binary_path,
        startup_timeout=startup_timeout,
        compose_file=compose_file,
        deployment_env=deployment_env,
    )


def _run_backend(args: argparse.Namespace) -> None:
    """执行 backend action；已知异常 → 单行 ERROR + SystemExit(1)，无 traceback。"""
    try:
        backend = create_backend(
            state_dir=args.state_dir,
            socket_path=args.socket_path,
            binary_path=args.binary_path,
            startup_timeout=args.startup_timeout,
            compose_file=args.compose_file,
            deployment_env=args.deployment_env,
        )
        if args.action == "install":
            backend.install()
        elif args.action == "start":
            backend.start()
        elif args.action == "stop":
            backend.stop()
        elif args.action == "status":
            print(json.dumps(backend.status(), ensure_ascii=False))
        elif args.action == "is-running":
            sys.exit(0 if backend.is_running() else 1)
        else:  # pragma: no cover — argparse choices 已约束
            raise ValueError(f"unknown backend action: {args.action}")
    except _BACKEND_ERRORS as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _main_backend(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="sakura_ai_updater backend")
    parser.add_argument(
        "action",
        choices=["install", "start", "stop", "status", "is-running"],
        help="daemon 生命周期动作（install/start 需 root）",
    )
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    parser.add_argument("--socket-path", default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--binary-path", default=None)
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=DEFAULT_STARTUP_TIMEOUT,
        help="start readiness 总超时（秒）",
    )
    parser.add_argument("--compose-file", default=None)
    parser.add_argument("--deployment-env", default=None)
    _run_backend(parser.parse_args(argv))


def main(argv: list[str] | None = None) -> None:
    """入口：先识别 backend 子命令，否则走原 --serve parser（完全兼容）。"""
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "backend":
        _main_backend(argv[1:])
        return

    parser = argparse.ArgumentParser(prog="sakura_ai_updater")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--serve", action="store_true", help="run as UDS daemon")
    parser.add_argument("--socket-path", default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    parser.add_argument(
        "--socket-gid",
        type=int,
        default=DEFAULT_GID,
        help="UDS socket 文件 group ID（默认 %(default)s；Web 容器补充 GID 需匹配）",
    )
    parser.add_argument(
        "--socket-uid",
        type=int,
        default=0,
        help="UDS socket 文件 owner UID（默认 %(default)s；dev 模式自动取当前用户）",
    )
    parser.add_argument(
        "--lock-path",
        default=None,
        help="default: <state-dir>/updater.lock",
    )
    parser.add_argument(
        "--compose-file",
        default=None,
        help="absolute production Compose file used by host updater",
    )
    parser.add_argument(
        "--deployment-env",
        default=None,
        help="absolute authoritative .deploy/deployment.env path",
    )
    parser.add_argument(
        "--health-url",
        default="http://localhost:8000/health",
        help="application /health endpoint used for version/health gates",
    )
    parser.add_argument(
        "--disk-space-threshold",
        type=int,
        default=2 * 1024 * 1024 * 1024,
        help="minimum free Docker-root bytes required by preflight",
    )
    args = parser.parse_args(argv)

    if not args.serve:
        parser.error("no action specified; use --serve")

    state_path = os.path.join(args.state_dir, "update-state.json")
    lock_path = args.lock_path or os.path.join(args.state_dir, "updater.lock")
    serve(
        args.socket_path,
        state_path,
        lock_path,
        socket_uid=args.socket_uid,
        socket_gid=args.socket_gid,
        compose_file=args.compose_file,
        deployment_env=args.deployment_env,
        health_url=args.health_url,
        disk_space_threshold=args.disk_space_threshold,
    )


if __name__ == "__main__":
    main()
