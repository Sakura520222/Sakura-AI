"""Optional platform locking must not prevent the backend from starting."""

import subprocess
import sys
from pathlib import Path

import pytest

from backend.services.agent_team import tool_scheduler


def test_backend_import_without_fcntl(tmp_path):
    # A fresh interpreter exercises the actual CI import chain without a cached
    # scheduler or a fake backend. Only the missing POSIX capabilities are
    # simulated; the host platform and all application modules remain real.
    source = """
import os, sys
sys.path.insert(0, sys.argv[1])
sys.modules['fcntl'] = None
for flag in ('O_DIRECTORY', 'O_NOFOLLOW'):
    if hasattr(os, flag):
        delattr(os, flag)
import backend.main
assert backend.main.app is not None
print('backend import ok')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", source, str(Path(__file__).resolve().parents[1])],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "backend import ok" in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["fcntl", "O_DIRECTORY", "O_NOFOLLOW"])
async def test_unavailable_workspace_lock_rejects_effect_without_leaving_waiters(
    tmp_path, monkeypatch, missing
):
    barrier = tool_scheduler.workspace_barrier(str(tmp_path))
    entered = False
    with monkeypatch.context() as patch:
        if missing == "fcntl":
            patch.setattr(tool_scheduler, "fcntl", None)
        else:
            patch.delattr(tool_scheduler.os, missing, raising=False)
        for shared in (True, False):
            with pytest.raises(RuntimeError, match="workspace_lock_unavailable"):
                async with barrier.hold(shared):
                    entered = True
            assert not entered
            assert barrier.readers == 0
            assert not barrier.writer and barrier.waiting_writers == 0
