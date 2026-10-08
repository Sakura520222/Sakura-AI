"""Search only projects matches from descriptor-admitted candidate files."""

import asyncio
import os

import pytest

from backend.services.agent_team.execution import (
    ExecutionProfile,
    ExecutionRequest,
    ExecutionResult,
    LocalExecutionRunner,
)
from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.tools.grep_tool import GrepTool
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


class CandidateRunner:
    def __init__(self, result, after_enumeration=None):
        self.result = result
        self.after_enumeration = after_enumeration
        self.requests = []

    def supports_profile(self, profile):
        return profile is ExecutionProfile.READ_ONLY

    async def execute(self, request):
        self.requests.append(request)
        if self.after_enumeration:
            self.after_enumeration()
        return self.result


def context(workspace, runner):
    return ToolContext(
        str(workspace), AgentTeamWorkspaceService(workspace.parent), runner
    )


@pytest.mark.asyncio
async def test_real_readonly_search_rejects_outside_hardlink(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "host-file"
    secret.write_text("needle HOST PRIVATE CONTENT\n")
    os.link(secret, workspace / "leak.txt")
    (workspace / "regular.txt").write_text("needle regular\n")
    service = AgentTeamWorkspaceService(workspace.parent)
    runner = LocalExecutionRunner(workspace, service)
    result = await GrepTool().execute(
        {"keyword": "needle", "output_mode": "content"}, context(workspace, runner)
    )
    assert result.success, result.error
    assert result.output["matches"] == ["./regular.txt:1:needle regular"]
    assert result.output["total"] == 1
    assert "HOST PRIVATE" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement", ["hardlink", "symlink", "directory_symlink", "fifo"]
)
async def test_search_rejects_candidates_replaced_after_runner(tmp_path, replacement):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    nested = workspace / "nested"
    nested.mkdir()
    target = nested / "candidate.txt"
    target.write_text("needle original\n")
    outside = tmp_path / "host-dir"
    outside.mkdir()
    secret = outside / "candidate.txt"
    secret.write_text("needle HOST PRIVATE CONTENT\n")
    (workspace / "regular.txt").write_text("needle regular\n")

    def replace():
        if replacement == "directory_symlink":
            nested.rename(workspace / "old-nested")
            nested.symlink_to(outside, target_is_directory=True)
        else:
            target.unlink()
            if replacement == "hardlink":
                os.link(secret, target)
            elif replacement == "symlink":
                target.symlink_to(secret)
            else:
                os.mkfifo(target)

    runner = CandidateRunner(
        ExecutionResult(exit_code=0, stdout="./nested/candidate.txt\0./regular.txt\0"),
        replace,
    )
    result = await GrepTool().execute(
        {"keyword": "needle", "output_mode": "content"}, context(workspace, runner)
    )
    assert result.success, result.error
    assert result.output["matches"] == ["./regular.txt:1:needle regular"]
    assert result.output["total"] == 1


@pytest.mark.asyncio
async def test_search_parses_only_complete_nul_records_and_preserves_filenames(
    tmp_path,
):
    name = "colon:name\nsecond.txt"
    (tmp_path / name).write_text("needle\n")
    (tmp_path / "partial.txt").write_text("needle hidden\n")
    runner = CandidateRunner(
        ExecutionResult(
            exit_code=0,
            stdout=f"./{name}\0./partial.txt",
            output_truncated=True,
        )
    )
    result = await GrepTool().execute({"keyword": "needle"}, context(tmp_path, runner))
    assert result.success, result.error
    assert result.output == {
        "files": [f"./{name}"],
        "num_files": 1,
        "keyword": "needle",
        "truncated": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keyword,ignore_case,expected",
    [
        ("foo[bar", True, ["./regular.txt:1:FOO[BAR"]),
        ("STRASSE", True, ["./regular.txt:2:Straße"]),
        ("σίγμα", True, ["./regular.txt:3:ΣΊΓΜΑ"]),
        (
            "FOO[BAR\nStraße",
            False,
            ["./regular.txt:1:FOO[BAR", "./regular.txt:2:Straße"],
        ),
        ("missing", False, []),
        (
            "FOO[BAR\n",
            False,
            [
                "./regular.txt:1:FOO[BAR",
                "./regular.txt:2:Straße",
                "./regular.txt:3:ΣΊΓΜΑ",
            ],
        ),
    ],
)
async def test_search_literal_unicode_and_newline_pattern_semantics(
    tmp_path, keyword, ignore_case, expected
):
    (tmp_path / "regular.txt").write_text("FOO[BAR\nStraße\nΣΊΓΜΑ\n")
    runner = CandidateRunner(ExecutionResult(exit_code=0, stdout="./regular.txt\0"))
    result = await GrepTool().execute(
        {"keyword": keyword, "case_insensitive": ignore_case, "output_mode": "content"},
        context(tmp_path, runner),
    )
    assert result.success, result.error
    assert result.output["matches"] == expected
    assert result.output["total"] == len(expected)
    assert not result.output["truncated"]
    assert runner.requests[0].argv[-3:] == ("--", "^", ".")
    assert "-i" not in runner.requests[0].argv


@pytest.mark.asyncio
async def test_search_does_not_resolve_existing_internal_symlink_candidates(tmp_path):
    (tmp_path / "regular.txt").write_text("needle\n")
    (tmp_path / "alias.txt").symlink_to(tmp_path / "regular.txt")
    runner = CandidateRunner(
        ExecutionResult(exit_code=0, stdout="./alias.txt\0./regular.txt\0")
    )
    result = await GrepTool().execute({"keyword": "needle"}, context(tmp_path, runner))
    assert result.success, result.error
    assert result.output["files"] == ["./regular.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner_result",
    [
        ExecutionResult(exit_code=2, stdout="./secret.txt\0", stderr="HOST PRIVATE"),
        ExecutionResult(exit_code=0, stdout="./secret.txt\0", timed_out=True),
        ExecutionResult(
            exit_code=0, stdout="./secret.txt\0", infrastructure_error="HOST PRIVATE"
        ),
    ],
)
async def test_failed_runner_never_projects_host_content(
    tmp_path, monkeypatch, runner_result
):
    (tmp_path / "secret.txt").write_text("needle HOST PRIVATE\n")
    runner = CandidateRunner(runner_result)

    reads = []

    def forbidden_read(*args):
        reads.append(args)
        raise AssertionError("failed runner triggered a host content read")

    monkeypatch.setattr(
        "backend.services.agent_team.tools.grep_tool.read_workspace_text_with_metadata",
        forbidden_read,
        raising=False,
    )
    result = await GrepTool().execute({"keyword": "needle"}, context(tmp_path, runner))
    assert not result.success
    assert "HOST PRIVATE" not in str(result)
    assert not reads


@pytest.mark.asyncio
async def test_cancelled_runner_never_projects_host_content(tmp_path, monkeypatch):
    runner = CandidateRunner(
        ExecutionResult(exit_code=0, stdout="./secret.txt\0", cancelled=True)
    )

    def forbidden_read(*args):
        raise AssertionError("cancelled runner triggered a host content read")

    monkeypatch.setattr(
        "backend.services.agent_team.tools.grep_tool.read_workspace_text_with_metadata",
        forbidden_read,
        raising=False,
    )
    with pytest.raises(asyncio.CancelledError):
        await GrepTool().execute({"keyword": "needle"}, context(tmp_path, runner))


def test_readonly_request_rejects_keyword_dependent_recursive_grep():
    with pytest.raises(ValueError, match="read-only"):
        ExecutionRequest(
            workspace_key="test",
            profile=ExecutionProfile.READ_ONLY,
            argv=("grep", "-rn", "-F", "--", "needle", "."),
        )


@pytest.mark.asyncio
async def test_search_actual_io_failure_remains_an_error(tmp_path, monkeypatch):
    runner = CandidateRunner(ExecutionResult(exit_code=0, stdout="./regular.txt\0"))

    def deny_read(*args):
        raise PermissionError("HOST PRIVATE diagnostic")

    monkeypatch.setattr(
        "backend.services.agent_team.tools.grep_tool.read_workspace_text_with_metadata",
        deny_read,
    )
    result = await GrepTool().execute({"keyword": "needle"}, context(tmp_path, runner))
    assert not result.success and "PermissionError" in result.error
    assert "HOST PRIVATE" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", [None, object()])
async def test_missing_runner_never_reads_host_content(tmp_path, monkeypatch, runner):
    reads = []

    def forbidden_read(*args):
        reads.append(args)
        raise AssertionError("missing runner triggered a host content read")

    monkeypatch.setattr(
        "backend.services.agent_team.tools.grep_tool.read_workspace_text_with_metadata",
        forbidden_read,
    )
    result = await GrepTool().execute({"keyword": "needle"}, context(tmp_path, runner))
    assert not result.success and "ExecutionError" in result.error
    assert not reads


@pytest.mark.asyncio
async def test_real_search_keeps_extension_exclusions_and_nul_names(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "regular:colon\nsecond.py").write_text("needle = 1\n")
    (workspace / "ignored.txt").write_text("needle\n")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "ignored.py").write_text("needle\n")
    (workspace / "binary.py").write_bytes(b"needle\0binary")
    service = AgentTeamWorkspaceService(tmp_path)
    runner = LocalExecutionRunner(workspace, service)
    result = await GrepTool().execute(
        {"keyword": "needle", "file_extension": ".py"}, context(workspace, runner)
    )
    assert result.success, result.error
    assert result.output["files"] == ["./regular:colon\nsecond.py"]
    assert result.output["num_files"] == 1


@pytest.mark.asyncio
async def test_output_caps_count_only_admitted_matches(tmp_path):
    candidates = []
    for index in range(55):
        name = f"./file{index:02}.txt"
        (tmp_path / name).write_text("needle\nneedle\n")
        candidates.append(name)
    runner = CandidateRunner(
        ExecutionResult(exit_code=0, stdout="\0".join(candidates) + "\0")
    )
    ctx = context(tmp_path, runner)
    result = await GrepTool().execute(
        {"keyword": "needle", "output_mode": "content"}, ctx
    )
    assert result.success and len(result.output["matches"]) == 100
    assert result.output["total"] == 110 and result.output["truncated"]
    result = await GrepTool().execute({"keyword": "needle"}, ctx)
    assert result.success and len(result.output["files"]) == 50
    assert result.output["num_files"] == 55 and result.output["truncated"]


@pytest.mark.asyncio
async def test_no_candidates_status_never_reads_untrusted_stdout(tmp_path, monkeypatch):
    runner = CandidateRunner(ExecutionResult(exit_code=1, stdout="./secret.txt\0"))
    reads = []

    def forbidden_read(*args):
        reads.append(args)
        raise AssertionError("no-candidate status triggered a host content read")

    monkeypatch.setattr(
        "backend.services.agent_team.tools.grep_tool.read_workspace_text_with_metadata",
        forbidden_read,
    )
    result = await GrepTool().execute({"keyword": "needle"}, context(tmp_path, runner))
    assert result.success and result.output["num_files"] == 0
    assert not reads


@pytest.mark.asyncio
async def test_runner_exception_and_cancellation_never_trigger_content_reads(
    tmp_path, monkeypatch
):
    class FailingRunner:
        def supports_profile(self, profile):
            return profile is ExecutionProfile.READ_ONLY

        async def execute(self, request):
            raise OSError("HOST PRIVATE DIAGNOSTIC")

    reads = []

    def forbidden_read(*args):
        reads.append(args)
        raise AssertionError("runner exception triggered a host content read")

    monkeypatch.setattr(
        "backend.services.agent_team.tools.grep_tool.read_workspace_text_with_metadata",
        forbidden_read,
    )
    result = await GrepTool().execute(
        {"keyword": "needle"}, context(tmp_path, FailingRunner())
    )
    assert not result.success and "OSError" in result.error
    assert "HOST PRIVATE" not in str(result) and not reads

    class CancelledRunner(FailingRunner):
        async def execute(self, request):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await GrepTool().execute(
            {"keyword": "needle"}, context(tmp_path, CancelledRunner())
        )
    assert not reads


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_task_cancel_drains_projection_thread_before_next_candidate(
    tmp_path, monkeypatch, cancel_count
):
    import threading

    from backend.services.agent_team.tools import grep_tool

    for name in ("first.txt", "second.txt"):
        (tmp_path / name).write_text("needle\n")
    runner = CandidateRunner(
        ExecutionResult(exit_code=0, stdout="./first.txt\0./second.txt\0")
    )
    entered = asyncio.Event()
    release = threading.Event()
    exited = threading.Event()
    loop = asyncio.get_running_loop()
    reads = []
    real_read = grep_tool.read_workspace_text_with_metadata

    def blocking_read(workspace, target):
        reads.append(target.name)
        content = real_read(workspace, target)
        if target.name == "first.txt":
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(5), (
                    "test failed to release the current descriptor read"
                )
            finally:
                exited.set()
        return content

    monkeypatch.setattr(grep_tool, "read_workspace_text_with_metadata", blocking_read)
    operation = asyncio.create_task(
        GrepTool().execute({"keyword": "needle"}, context(tmp_path, runner))
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for _ in range(cancel_count):
            operation.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not operation.done(), (
                "search returned cancellation before its thread drained"
            )
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 5)
        assert exited.is_set()
        assert reads == ["first.txt"], (
            "cancelled search continued into another candidate"
        )
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
