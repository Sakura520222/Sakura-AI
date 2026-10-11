"""PR #661 regressions: actual hook changes, draining and read-only evidence."""

import asyncio
import json
import subprocess
from collections import OrderedDict
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import config
from backend.models import agent_team_models as models
from backend.models.database import AppConfig
from backend.services.agent_team import fullstack_expert as runtime
from backend.services.agent_team.conversation_checkpoint import ResumeCursor
from backend.services.agent_team.execution import LocalExecutionRunner, TrustedGitRunner
from backend.services.agent_team.iteration_loop import IterationLoopService
from backend.services.agent_team.repository_context import RepositoryContext
from backend.services.agent_team.subagents import SubagentManager, SubagentStore
from backend.services.agent_team.tool_scheduler import workspace_barrier
from backend.services.agent_team.tools.base import (
    BaseTool,
    ToolContext,
    ToolExecutor,
    ToolResult,
)
from backend.services.agent_team.tools.project_detect_tool import DetectProjectTool
from backend.services.agent_team.tools.registry import create_executor
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from tests import test_agent_subagents as fixtures
from tests.test_agent_subagents import call, response

persistence = fixtures.persistence
model_factory = fixtures.model_factory


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch):
    monkeypatch.setattr(config, "_dynamic_config_cache", OrderedDict())


async def git(workspace, *args):
    result = await asyncio.to_thread(
        subprocess.run,
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["before_finish", "after_finish"])
@pytest.mark.parametrize("model_writes", [False, True])
async def test_completion_includes_actual_hook_changes_before_durable_result(
    persistence, tmp_path, model_factory, event, model_writes
):
    checkpoint, engine = persistence
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("o", "r")
    (workspace / "tracked.txt").write_text("original\n")
    (workspace / "obsolete.txt").write_text("remove me\n")
    (workspace / "old.txt").write_text("rename me\n")
    await git(workspace, "init", "--quiet")
    await git(workspace, "add", ".")
    await git(
        workspace,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "baseline",
    )
    hook_code = (
        "from pathlib import Path; import subprocess; "
        "Path('tracked.txt').write_text('formatted\\n'); "
        "Path('obsolete.txt').unlink(); "
        "subprocess.run(['git', 'mv', 'old.txt', 'renamed.txt'], check=True); "
        "subprocess.run(['git', 'add', '-u'], check=True); "
        "Path('generated').mkdir(exist_ok=True); "
        "Path('generated/new.txt').write_text('generated\\n'); "
        "Path('generated/中文\\tfile\\n.txt').write_text('literal filename\\n')"
    )
    with Session(engine) as db:
        for key, value in {
            "agent_team_harness_plugins": json.dumps(
                {
                    "version": 1,
                    "hooks": [
                        {
                            "id": "format",
                            "event": event,
                            "argv": ["python3", "-c", hook_code],
                        }
                    ],
                }
            ),
            "agent_team_permission_profile": "autonomous",
            "agent_team_network_policy": "full_access",
        }.items():
            db.add(AppConfig(key_name=key, key_value=value))
        db.commit()

    async def model(messages, **kwargs):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        assert not results or "error" not in results[-1], results
        if model_writes and not any(m["role"] == "tool" for m in messages):
            return response(
                call("write_file", "write", file_path="model.txt", content="model")
            )
        return response(
            call(
                "finish_task",
                "finish",
                summary="verified",
                modified_files=["model.txt"] if model_writes else [],
            )
        )

    model_factory(model)
    loop = IterationLoopService(
        workspace,
        service,
        checkpoint=checkpoint,
        execution_runner=LocalExecutionRunner(workspace, service),
    )
    outcome = await loop.run("format files", "complete implementation and hooks")
    expected = {
        "tracked.txt",
        "obsolete.txt",
        "renamed.txt",
        "generated/new.txt",
        "generated/中文\tfile\n.txt",
    }
    if model_writes:
        expected.add("model.txt")
    assert outcome.success, outcome.reason
    assert set(outcome.modified_files) == expected
    assert set(outcome.fullstack_result.modified_files) == expected
    with Session(engine) as db:
        parent = db.scalar(
            select(models.AgentTeamSession).where(
                models.AgentTeamSession.role_name == "agent"
            )
        )
        assert set(json.loads(parent.result_payload)["modified_files"]) == expected
        parent_id = parent.id
    # Resume from the committed finish ledger: no second hook or model request.
    resumed = IterationLoopService(
        workspace,
        service,
        checkpoint=checkpoint,
        resume_cursor=ResumeCursor(parent_id, 1, "agent", "completed"),
        execution_runner=LocalExecutionRunner(workspace, service),
    )
    assert set((await resumed.run("format files", "resume")).modified_files) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["failure", "task", "event"])
async def test_completion_accounting_failure_and_cancellation_are_durable(
    persistence, tmp_path, model_factory, interruption
):
    checkpoint, engine = persistence
    entered, release, event = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def changed_files(workspace):
        entered.set()
        await release.wait()
        if interruption == "failure":
            raise RuntimeError("PRIVATE-GIT-DIAGNOSTIC")
        return {"actual.txt": {}}

    async def model(**kwargs):
        return response(call("finish_task", summary="completed"))

    model_factory(model)
    loop = IterationLoopService(
        tmp_path,
        AgentTeamWorkspaceService(tmp_path),
        checkpoint=checkpoint,
        git_workspace_service=SimpleNamespace(get_changed_file_stats=changed_files),
    )
    running = asyncio.create_task(loop.run("complete", "complete", cancel_event=event))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if interruption == "task":
            running.cancel("shutdown during accounting")
        elif interruption == "event":
            event.set()
        release.set()
        if interruption == "failure":
            with pytest.raises(RuntimeError) as caught:
                await running
            assert "PRIVATE-GIT-DIAGNOSTIC" not in str(caught.value)
            expected = "unrecoverable_error"
        elif interruption == "task":
            with pytest.raises(asyncio.CancelledError) as caught:
                await running
            assert caught.value.args == ("shutdown during accounting",)
            expected = "cancelled"
        else:
            assert (await running).outcome == "cancelled"
            expected = "cancelled"
        with Session(engine) as db:
            parent = db.scalar(select(models.AgentTeamSession))
            assert parent.status == expected
            assert not json.loads(parent.result_payload)["success"]
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_completion_cancellation_drains_real_git_metadata_before_next_writer(
    persistence, tmp_path, model_factory, monkeypatch
):
    checkpoint, engine = persistence
    service = AgentTeamWorkspaceService(tmp_path / "workplace")
    workspace = service.ensure_workspace("o", "r")
    (workspace / "file.txt").write_text("original\n")
    await git(workspace, "init", "--quiet")
    await git(workspace, "add", ".")
    await git(
        workspace,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "baseline",
    )
    entered, release, drained = Event(), Event(), Event()
    original = TrustedGitRunner._validate_git_metadata_snapshot

    def slow_metadata(runner):
        entered.set()
        try:
            assert release.wait(5)
            original(runner)
        finally:
            drained.set()

    monkeypatch.setattr(
        TrustedGitRunner, "_validate_git_metadata_snapshot", slow_metadata
    )

    async def model(**kwargs):
        return response(call("finish_task", summary="finished"))

    model_factory(model)
    loop = IterationLoopService(workspace, service, checkpoint=checkpoint)
    running = asyncio.create_task(loop.run("finish", "reconcile"))
    writes = []
    writing = None

    async def writer():
        async with workspace_barrier(str(workspace)).hold(False):
            writes.append(drained.is_set())
            (workspace / "file.txt").write_text("new writer\n")

    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        running.cancel("shutdown during metadata read")
        writing = asyncio.create_task(writer())
        await asyncio.sleep(0.02)
        before_release = (running.done(), list(writes))
        running.cancel("repeated shutdown")
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(running, 2)
        await asyncio.wait_for(writing, 2)
        assert caught.value.args == ("shutdown during metadata read",)
        assert before_release == (False, []) and writes == [True]
        with Session(engine) as db:
            parent = db.scalar(select(models.AgentTeamSession))
            assert parent.status == "cancelled"
    finally:
        release.set()
        await asyncio.gather(
            running, *([writing] if writing else []), return_exceptions=True
        )
        async with asyncio.timeout(2):
            while not drained.is_set():
                await asyncio.sleep(0.001)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_method", ["task", "event"])
async def test_project_detection_drains_reader_before_writer_and_stops_next_read(
    tmp_path, monkeypatch, cancel_method
):
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'example'\n")
    entered, release, drained = Event(), Event(), Event()
    reads, writes = [], []
    original = RepositoryContext.read_text

    def slow_read(repository, relative, **kwargs):
        reads.append(str(relative))
        if len(reads) == 1:
            entered.set()
            release.wait(5)
            try:
                return original(repository, relative, **kwargs)
            finally:
                drained.set()
        return original(repository, relative, **kwargs)

    monkeypatch.setattr(RepositoryContext, "read_text", slow_read)

    class Writer(BaseTool):
        name = "write_file"

        async def execute(self, args, ctx):
            writes.append(drained.is_set())
            return ToolResult(True)

    event = asyncio.Event()
    ctx = ToolContext(
        str(tmp_path), AgentTeamWorkspaceService(tmp_path), cancel_event=event
    )
    executor = ToolExecutor([DetectProjectTool(), Writer()])
    detecting = asyncio.create_task(executor.execute_raw("detect_project", {}, ctx))
    writing = None
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        if cancel_method == "event":
            event.set()
        else:
            detecting.cancel("shutdown")
        writing = asyncio.create_task(
            executor.execute_raw(
                "write_file", {}, ToolContext(str(tmp_path), ctx.workspace_service)
            )
        )
        await asyncio.sleep(0.02)
        before_release = (detecting.done(), list(writes))
        if cancel_method == "task":
            detecting.cancel("repeated shutdown")
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(detecting, 2)
        if cancel_method == "task":
            assert caught.value.args == ("shutdown",)
        await asyncio.wait_for(writing, 2)
        assert before_release == (False, [])
        assert writes == [True] and reads == ["requirements.txt"]
    finally:
        release.set()
        await asyncio.gather(
            detecting, *([writing] if writing else []), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["execute_raw", "execute_tool_call", "_execute_tool_call"]
)
async def test_readonly_finish_cannot_publish_claimed_modifications(tmp_path, method):
    executor = create_executor(read_only=True)
    ctx = ToolContext(str(tmp_path), AgentTeamWorkspaceService(tmp_path))
    args = {"summary": "investigated", "modified_files": ["nonexistent.py"]}
    if method == "execute_raw":
        result = await executor.execute_raw("finish_task", args, ctx)
    else:
        result = await getattr(executor, method)(call("finish_task", **args), ctx)
    # Either reject the invalid claim or normalize it at the trusted boundary.
    assert not result.is_terminal or result.output.get("modified_files") == []
    assert not (tmp_path / "nonexistent.py").exists()
    main = await create_executor().execute_raw("finish_task", args, ctx)
    assert main.is_terminal and main.output["modified_files"] == ["nonexistent.py"]


@pytest.mark.asyncio
async def test_child_claims_do_not_survive_checkpoint_wait_or_parent_resume(
    persistence, tmp_path, model_factory
):
    checkpoint, engine = persistence

    async def model(messages, **kwargs):
        results = [m for m in messages if m["role"] == "tool"]
        return response(
            call(
                "finish_task",
                f"finish-{len(results)}",
                summary="investigated",
                modified_files=[] if results else ["nonexistent.py"],
            )
        )

    model_factory(model)
    manager, ctx, parent_id = await fixtures.manager_for(checkpoint, tmp_path)
    try:
        child = await fixtures.spawn_recorded(manager, checkpoint, parent_id, "spawn")
        outcome = await manager.wait(child["agent_id"])
        assert outcome["result"]["modified_files"] == []
        with Session(engine) as db:
            saved = db.get(models.AgentTeamSession, child["agent_id"])
            assert json.loads(saved.result_payload)["modified_files"] == []
        await manager.close()
        restored = SubagentManager(checkpoint, parent_id, ctx, concurrency=1)
        try:
            await restored.start()
            assert (await restored.wait(child["agent_id"]))["result"][
                "modified_files"
            ] == []
        finally:
            await restored.close()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_legacy_child_finish_and_saved_result_never_report_claimed_files(
    persistence, tmp_path, model_factory
):
    from backend.services.agent_team.subagents import SubagentStore
    from backend.utils.message_utils import tool_call_to_dict

    checkpoint, _ = persistence
    parent = await checkpoint.create_session(1, "agent")
    await fixtures.recorded_spawn(checkpoint, parent.id)
    store = SubagentStore(1)
    child = await store.create(parent.id, "spawn", "Inspect evidence", {})
    finish = call(
        "finish_task",
        "legacy-finish",
        summary="investigated",
        modified_files=["nonexistent.py"],
    )
    await checkpoint.append_message(
        child.session_id,
        {"role": "assistant", "tool_calls": [tool_call_to_dict(finish)]},
    )
    await checkpoint.record_tool_result(
        child.session_id,
        finish.id,
        {
            "role": "tool",
            "tool_call_id": finish.id,
            "content": json.dumps(
                {
                    "_terminal": True,
                    "summary": "investigated",
                    "modified_files": ["nonexistent.py"],
                }
            ),
        },
        "completed",
    )
    await checkpoint.finish_session(
        child.session_id,
        "success",
        {
            "outcome": "success",
            "success": True,
            "summary": "investigated",
            "modified_files": ["nonexistent.py"],
        },
    )
    requests = model_factory(
        AsyncMock(side_effect=AssertionError("completed child must not call a model"))
    )
    agent = runtime.FullStackExpertAgent(
        tmp_path,
        AgentTeamWorkspaceService(tmp_path),
        checkpoint=checkpoint,
        session_id=child.session_id,
        initial_messages=await checkpoint.load_messages(child.session_id),
    )
    result = await agent.execute("child", "resume")
    assert result.success and result.modified_files == [] and not requests
    manager = SubagentManager(
        checkpoint,
        parent.id,
        ToolContext(str(tmp_path), agent.workspace_service),
        concurrency=1,
    )
    try:
        await manager.start()
        assert (await manager.wait(child.session_id))["result"]["modified_files"] == []
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize(
    "name,args",
    [
        ("search_in_files", {"keyword": "needle"}),
        ("glob", {"pattern": "backend/**/*.py"}),
    ],
)
async def test_recursive_investigation_receives_scoped_rules_before_results(
    persistence, tmp_path, model_factory, child, name, args
):
    checkpoint, _ = persistence
    (tmp_path / "backend/nested").mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("ROOT_RULE")
    (tmp_path / "backend/AGENTS.md").write_text("BACKEND_RULE")
    (tmp_path / "backend/nested/AGENTS.md").write_text("NESTED_RULE")
    (tmp_path / "backend/nested/example.py").write_text("needle = True\n")
    scopes_received = []

    async def model(messages, **kwargs):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not scopes_received and (not results or results[-1].get("error_code")):
            return response(call(name, f"search-{len(results)}", **args))
        if not scopes_received:
            rendered = str(messages)
            scopes_received.append(
                "BACKEND_RULE" in rendered and "NESTED_RULE" in rendered
            )
            assert results[-1].get("num_files", 0) == 1
            # Rules retain user/data authority; none enters the system seed.
            assert "BACKEND_RULE" not in messages[0]["content"]
        return response(
            call("finish_task", f"finish-{len(results)}", summary="inspected")
        )

    model_factory(model)
    service = AgentTeamWorkspaceService(tmp_path.parent)
    runner = LocalExecutionRunner(tmp_path, service)
    if child:
        manager, ctx, parent_id = await fixtures.manager_for(checkpoint, tmp_path)
        ctx.workspace_service = service
        ctx.execution_runner = runner
        try:
            spawned = await fixtures.spawn_recorded(
                manager, checkpoint, parent_id, "spawn"
            )
            result = await manager.wait(spawned["agent_id"])
            assert result["result"]["success"]
        finally:
            await manager.close()
    else:
        result = await runtime.FullStackExpertAgent(
            tmp_path, service, execution_runner=runner
        ).execute("inspect", "inspect only")
        assert result.success, result.error
    assert scopes_received == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize(
    "name,args",
    [
        ("search_in_files", {"keyword": "needle"}),
        ("glob", {"pattern": "backend/**/*.py"}),
    ],
)
async def test_completed_investigation_resume_refreshes_its_scope_before_model(
    persistence, tmp_path, model_factory, child, name, args
):
    checkpoint, _ = persistence
    (tmp_path / "backend/nested").mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("ROOT_RULE")
    backend_rule = tmp_path / "backend/AGENTS.md"
    backend_rule.write_text("OLD_BACKEND_RULE")
    nested_rule = tmp_path / "backend/nested/AGENTS.md"
    nested_rule.write_text("OLD_NESTED_RULE")
    (tmp_path / "backend/nested/example.py").write_text("needle = True\n")
    parent = await checkpoint.create_session(1, "agent")
    session_id = parent.id
    if child:
        await fixtures.recorded_spawn(checkpoint, parent.id)
        session_id = (
            await SubagentStore(1).create(parent.id, "spawn", "Inspect evidence", {})
        ).session_id
    service = AgentTeamWorkspaceService(tmp_path.parent)
    runner = LocalExecutionRunner(tmp_path, service)

    async def first(messages, **kwargs):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not results or results[-1].get("error_code"):
            return response(call(name, f"inspect-{len(results)}", **args))
        assert results[-1].get("num_files") == 1
        assert "OLD_NESTED_RULE" in str(messages)
        raise RuntimeError("crash after persisted investigation")

    model_factory(first)
    agent = runtime.FullStackExpertAgent(
        tmp_path,
        service,
        checkpoint=checkpoint,
        session_id=session_id,
        execution_runner=runner,
    )
    with pytest.raises(RuntimeError, match="crash after persisted"):
        await agent.execute("inspect", "inspect only")
    durable = await checkpoint.load_messages(session_id)
    backend_rule.unlink()
    nested_rule.write_text("CURRENT_NESTED_RULE")
    observed = []

    async def resumed(messages, **kwargs):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        assert results[-1].get("num_files") == 1
        rendered = str(messages)
        assert "ROOT_RULE" in rendered and "CURRENT_NESTED_RULE" in rendered
        assert "OLD_BACKEND_RULE" not in rendered and "OLD_NESTED_RULE" not in rendered
        assert "CURRENT_NESTED_RULE" not in messages[0]["content"]
        observed.append(True)
        return response(call("finish_task", "finish-resumed", summary="inspected"))

    model_factory(resumed)
    restored = runtime.FullStackExpertAgent(
        tmp_path,
        service,
        checkpoint=checkpoint,
        session_id=session_id,
        initial_messages=durable,
        execution_runner=runner,
    )
    result = await restored.execute("inspect", "resume investigation")
    assert result.success and observed == [True]
