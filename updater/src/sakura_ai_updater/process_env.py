"""Keep host commands independent of the updater's bundled native libraries."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def host_process_env(
    base_env: dict[str, str] | None = None,
    pyinstaller_root: str | None = None,
) -> dict[str, str]:
    """Remove PyInstaller loader entries while preserving administrator settings.

    Onefile binaries prepend their extraction directory to LD_LIBRARY_PATH.
    Host tools such as openssl, systemctl, and commands run by start.sh must
    resolve their own libraries instead of the updater's bundled versions.
    Source executions and unrelated environment variables remain unchanged.
    """
    env = dict(os.environ if base_env is None else base_env)
    if pyinstaller_root is None:
        pyinstaller_root = getattr(sys, "_MEIPASS", None)
    if pyinstaller_root is None:
        return env

    root = Path(pyinstaller_root).resolve(strict=False)

    def is_pyinstaller_entry(entry: str) -> bool:
        try:
            path = Path(entry).resolve(strict=False)
        except (OSError, RuntimeError):
            return False
        return path == root or root in path.parents

    for name in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        value = env.get(name)
        if value is None:
            continue
        host_entries = [
            entry for entry in value.split(os.pathsep) if not is_pyinstaller_entry(entry)
        ]
        if host_entries:
            env[name] = os.pathsep.join(host_entries)
        else:
            env.pop(name, None)
    return env
