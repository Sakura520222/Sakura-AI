"""Native Windows handle admission and workspace-lock regressions."""

import asyncio
import errno
import os
import shutil
import subprocess

import pytest

from backend.services.agent_team.tool_scheduler import workspace_barrier
from backend.services.agent_team.tools.file_utils import (
    read_workspace_text_with_metadata,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows handle semantics")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le"])
def test_windows_read_preserves_unicode_encoding_line_endings_and_mtime(tmp_path, encoding):
    target = tmp_path / "资料" / "notes.txt"
    target.parent.mkdir()
    content = "\ufeff你好\r\nsecond\r\n" if encoding == "utf-16-le" else "你好\r\nsecond\r\n"
    target.write_bytes(content.encode(encoding))
    text, actual_encoding, endings, mtime = read_workspace_text_with_metadata(tmp_path, target)
    assert text == content.replace("\r\n", "\n")
    assert actual_encoding == encoding and endings == "\r\n"
    assert mtime == target.stat().st_mtime


def test_windows_read_rejects_junction_before_content_access(tmp_path):
    import _winapi

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret").write_text("private")
    junction = workspace / "junction"
    _winapi.CreateJunction(str(outside), str(junction))
    try:
        with pytest.raises(OSError) as error:
            read_workspace_text_with_metadata(workspace, junction / "secret")
        assert error.value.errno == errno.ELOOP
    finally:
        junction.rmdir()


def test_windows_read_rejects_hardlink_and_alternate_stream(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("ordinary")
    alias = tmp_path / "alias.txt"
    os.link(target, alias)
    with pytest.raises(ValueError, match="hard links"):
        read_workspace_text_with_metadata(tmp_path, alias)
    with pytest.raises(ValueError, match="alternate data streams"):
        read_workspace_text_with_metadata(tmp_path, tmp_path / "file.txt:hidden")


@pytest.mark.asyncio
async def test_windows_workspace_lock_pins_directory_and_releases_after_cancellation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    barrier = workspace_barrier(str(workspace))
    async with barrier.hold(True):
        with pytest.raises(PermissionError):
            workspace.rename(tmp_path / "moved")

        async def writer():
            async with barrier.hold(False):
                pytest.fail("Writer overlapped an active reader")

        pending = asyncio.create_task(writer())
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert barrier.readers == 0 and not barrier.writer and barrier.waiting_writers == 0
    async with barrier.hold(False):
        (workspace / "child").write_text("works inside a held lock")
        assert (workspace / "child").read_text() == "works inside a held lock"
    workspace.rename(tmp_path / "moved")


@pytest.mark.asyncio
async def test_windows_casing_aliases_share_writer_preference(tmp_path):
    barrier = workspace_barrier(str(tmp_path))
    assert workspace_barrier(str(tmp_path).swapcase()) is barrier


@pytest.mark.asyncio
async def test_windows_workspace_lock_allows_git_file_rename(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git for Windows is required")
    (tmp_path / "old.txt").write_text("tracked")
    await asyncio.to_thread(subprocess.run, [git, "init", "--quiet"], cwd=tmp_path, check=True)
    await asyncio.to_thread(subprocess.run, [git, "add", "old.txt"], cwd=tmp_path, check=True)
    async with workspace_barrier(str(tmp_path)).hold(False):
        await asyncio.to_thread(subprocess.run, [git, "mv", "old.txt", "new.txt"], cwd=tmp_path, check=True)
    assert (tmp_path / "new.txt").read_text() == "tracked"


@pytest.mark.asyncio
async def test_windows_worker_temp_settings_do_not_split_workspace_locks(tmp_path):
    from tests.test_agent_workspace_process_lock import child_holder

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker_temp = tmp_path / "worker-temp"
    worker_temp.mkdir()
    env = {**os.environ, "TMP": str(worker_temp), "TEMP": str(worker_temp), "TMPDIR": str(worker_temp)}
    async with child_holder(workspace, False, env=env) as child:
        assert (await child.stdout.readline()).rstrip(b"\r\n") == b"entered"
        entered = asyncio.Event()

        async def read():
            async with workspace_barrier(str(workspace)).hold(True):
                entered.set()

        pending = asyncio.create_task(read())
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(entered.wait(), 0.1)
            child.stdin.write(b"release\n")
            await child.stdin.drain()
            await asyncio.wait_for(pending, 5)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
