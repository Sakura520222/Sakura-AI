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
