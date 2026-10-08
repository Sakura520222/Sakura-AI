"""Workspace exclusion across real worker processes sharing the same directory."""

import asyncio
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from backend.services.agent_team.tool_scheduler import workspace_barrier

_CHILD = """
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from backend.services.agent_team.tool_scheduler import workspace_barrier
async def main():
    print('waiting', flush=True)
    async with workspace_barrier(sys.argv[2]).hold(sys.argv[3] == 'shared'):
        print('entered', flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    print('released', flush=True)
asyncio.run(main())
"""


@asynccontextmanager
async def child_holder(workspace, shared, *, expected_returncode=0):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        _CHILD,
        str(Path(__file__).resolve().parents[1]),
        str(workspace),
        "shared" if shared else "exclusive",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b"waiting\n"
        yield process
    finally:
        if process.returncode is None:
            process.stdin.write(b"release\n")
            await process.stdin.drain()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        assert process.returncode == expected_returncode, (
            await process.stderr.read()
        ).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent_shared,child_shared", [(False, False), (False, True), (True, False)]
)
async def test_process_writer_excludes_other_workspace_effects(
    tmp_path, parent_shared, child_shared
):
    async with child_holder(tmp_path, child_shared) as child:
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b"entered\n"
        entered = asyncio.Event()

        async def operation():
            async with workspace_barrier(str(tmp_path)).hold(parent_shared):
                entered.set()

        pending = asyncio.create_task(operation())
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(entered.wait(), 0.1)
            child.stdin.write(b"release\n")
            await child.stdin.drain()
            await asyncio.wait_for(pending, 5)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_process_shared_reads_overlap_and_cancelled_writer_releases(tmp_path):
    async with child_holder(tmp_path, True) as child:
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b"entered\n"
        async with asyncio.timeout(5):
            async with workspace_barrier(str(tmp_path)).hold(True):
                pass

        async def writer():
            async with workspace_barrier(str(tmp_path)).hold(False):
                pytest.fail("writer overlapped the other process reader")

        pending = asyncio.create_task(writer())
        await asyncio.sleep(0.05)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        # A cancelled waiter must not retain the event-loop writer preference.
        async with asyncio.timeout(5):
            async with workspace_barrier(str(tmp_path)).hold(True):
                pass


@pytest.mark.asyncio
async def test_other_workspace_is_independent(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    async with child_holder(first, False) as child:
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b"entered\n"
        async with asyncio.timeout(5):
            async with workspace_barrier(str(second)).hold(False):
                pass


@pytest.mark.asyncio
async def test_worker_exit_releases_kernel_lock(tmp_path):
    async with child_holder(
        tmp_path, False, expected_returncode=-signal.SIGKILL
    ) as child:
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b"entered\n"
        child.kill()
        await child.wait()
        async with asyncio.timeout(5):
            async with workspace_barrier(str(tmp_path)).hold(False):
                pass


@pytest.mark.asyncio
async def test_missing_workspace_fails_closed_without_leaving_writer(tmp_path):
    missing = tmp_path / "missing"
    barrier = workspace_barrier(str(missing))
    with pytest.raises(FileNotFoundError):
        async with barrier.hold(False):
            pytest.fail("missing workspace admitted")
    missing.mkdir()
    async with asyncio.timeout(5):
        async with barrier.hold(False):
            pass
