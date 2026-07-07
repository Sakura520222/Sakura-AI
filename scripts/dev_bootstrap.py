#!/usr/bin/env python3
"""Start Sakura AI in local Setup Wizard development mode."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_DEV_CONFIG_PATH = ".sakura/dev/connection.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start local development server for debugging Setup Wizard.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument(
        "--config-path",
        default=DEFAULT_DEV_CONFIG_PATH,
        help="Isolated connection config path for local setup debugging",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the isolated dev connection config before starting",
    )
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="Disable uvicorn auto reload",
    )
    parser.add_argument(
        "--log-level",
        default="debug",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="Uvicorn log level",
    )
    parser.add_argument(
        "--python",
        default="",
        help="Python executable used to run uvicorn; defaults to .venv if present",
    )
    return parser.parse_args()


def resolve_config_path(repo_root: Path, path_value: str) -> Path:
    config_path = Path(path_value)
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    return config_path


def find_server_python(repo_root: Path, python_arg: str) -> Path:
    if python_arg:
        return Path(python_arg)

    venv_python = (
        repo_root
        / ".venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if venv_python.exists():
        return venv_python

    return Path(sys.executable)


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parent.parent
    config_path = resolve_config_path(repo_root, args.config_path)
    server_python = find_server_python(repo_root, args.python)

    if args.reset and config_path.exists():
        config_path.unlink()
        print(f"已重置本地 dev 配置: {config_path}")

    config_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    # SAKURA_ENV enables local runtime conveniences such as localhost CORS.
    env.setdefault("SAKURA_ENV", "development")
    # SAKURA_DEV_BOOTSTRAP identifies first-run setup debugging and also skips
    # background tasks if the generic skip flag is not explicitly set.
    env.setdefault("SAKURA_DEV_BOOTSTRAP", "1")
    # SAKURA_SKIP_BACKGROUND_TASKS is a broader switch that can be reused by CI
    # or other local runners without implying Setup Wizard debugging.
    env.setdefault("SAKURA_SKIP_BACKGROUND_TASKS", "1")
    env.setdefault("SAKURA_CONNECTION_CONFIG_PATH", str(config_path))
    env.setdefault("APP_DOMAIN", "localhost")
    env.setdefault("APP_PORT", str(args.port))
    env.setdefault("LOG_LEVEL", args.log_level.upper())
    env.setdefault("WEBUI_COOKIE_SECURE", "false")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    command = [
        str(server_python),
        "-m",
        "uvicorn",
        "backend.main:app",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--log-level",
        args.log_level,
    ]
    if not args.no_reload:
        command.append("--reload")

    print("Sakura AI 本地 Setup Wizard 调试模式")
    print(f"访问地址: http://{args.host}:{args.port}/setup")
    print(f"连接配置: {config_path}")
    print(
        "安全提示: dev 配置可能包含数据库凭证，默认路径 .sakura/dev/ 已加入 .gitignore"
    )
    print(f"Python: {server_python}")
    print("后台任务: 已跳过")
    print("")

    try:
        return subprocess.call(command, cwd=repo_root, env=env)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
