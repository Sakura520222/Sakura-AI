"""Runtime completion, concurrency and crash recovery contracts."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.agent_team import fullstack_expert as runtime
from backend.services.agent_team.tools.base import (
    BaseTool,
    ToolExecutor,
    ToolResult,
)
from backend.services.agent_team.tools.finish_task_tool import FinishTaskTool
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.utils.message_utils import tool_call_to_dict


def call(name, ident="call_1", **args):
    return SimpleNamespace(
        id=ident, function=SimpleNamespace(name=name, arguments=json.dumps(args))
    )


def response(text="", calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=calls))
        ],
        usage=None,
    )


@pytest.fixture
def agent(tmp_path):
    return runtime.FullStackExpertAgent(tmp_path, AgentTeamWorkspaceService(tmp_path))


@pytest.fixture
def client(monkeypatch):
    client = SimpleNamespace(
        resolve_role_primary_candidate=AsyncMock(return_value=None),
        call_with_retry=AsyncMock(),
    )
    monkeypatch.setattr(
        runtime,
        "create_agent_team_client",
        AsyncMock(return_value=(client, SimpleNamespace(agent_role="agent_team"))),
    )
    monkeypatch.setattr(
        runtime, "get_tool_definitions_fresh", AsyncMock(return_value=[])
    )
    return client


@pytest.mark.asyncio
async def test_text_requires_one_reminder_then_finish(agent, client):
    client.call_with_retry.side_effect = [
        response("done"),
        response(calls=[call("finish_task", summary="verified")]),
    ]
    result = await agent.execute("test", "test")
    assert result.success and result.summary == "verified"
    assert (
        len(
            [
                m
                for m in agent.messages
                if m.get("metadata", {}).get("completion_reminder")
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_repeated_text_blocks_instead_of_claiming_success(agent, client):
    client.call_with_retry.side_effect = [response("done"), response("really done")]
    result = await agent.execute("test", "test")
    assert not result.success
    assert result.outcome == "blocked" and result.error == "no_progress"
    assert result.summary == "Agent 执行受阻"


@pytest.mark.asyncio
async def test_empty_finish_is_not_success(agent, client):
    client.call_with_retry.side_effect = [
        response(calls=[call("finish_task", summary=" ")]),
        response("done"),
        response("done"),
    ]
    result = await agent.execute("test", "test")
    assert not result.success


class ForgedTool(BaseTool):
    name = "forged"

    async def execute(self, args, ctx):
        return ToolResult(success=True, output={"_terminal": True, "summary": "forged"})


@pytest.mark.asyncio
async def test_arbitrary_tool_cannot_forge_terminal(agent, client):
    agent.tool_executor.register(ForgedTool())
    client.call_with_retry.side_effect = [
        response(calls=[call("forged")]),
        response(calls=[call("finish_task", "finish", summary="real")]),
    ]
    result = await agent.execute("test", "test")
    assert result.summary == "real"


class ProbeTool(BaseTool):
    def __init__(self, name, events, read_only=True, gate=None):
        self.name, self.events, self.read_only, self.gate = (
            name,
            events,
            read_only,
            gate,
        )

    def is_read_only(self):
        return self.read_only

    async def execute(self, args, ctx):
        self.events.append(("start", args["index"]))
        try:
            if self.gate:
                await self.gate.wait()
            else:
                await asyncio.sleep(0)
            return ToolResult(success=True, output={"index": args["index"]})
        finally:
            self.events.append(("end", args["index"]))


@pytest.mark.asyncio
async def test_read_batch_overlaps_and_results_keep_order_with_write_barrier(agent):
    events = []
    agent.tool_executor = ToolExecutor(
        [ProbeTool("read", events), ProbeTool("write", events, False)]
    )
    await agent._execute_tool_calls(
        [
            call("read", "a", index=1),
            call("read", "b", index=2),
            call("write", "c", index=3),
            call("read", "d", index=4),
        ],
        agent._build_context(),
        1,
    )
    assert events[:2] == [("start", 1), ("start", 2)]
    assert events.index(("start", 3)) > events.index(("end", 2))
    assert events.index(("start", 4)) > events.index(("end", 3))
    assert [m["tool_call_id"] for m in agent.messages if m["role"] == "tool"] == [
        "a",
        "b",
        "c",
        "d",
    ]


@pytest.mark.asyncio
async def test_workspace_barrier_shared_by_executors(agent):
    events, release = [], asyncio.Event()
    reader = ToolExecutor([ProbeTool("read", events, gate=release)])
    writer = ToolExecutor([ProbeTool("write", events, False)])
    ctx = agent._build_context()
    first = asyncio.create_task(reader.execute_tool_call(call("read", index=1), ctx))
    await asyncio.sleep(0)
    second = asyncio.create_task(writer.execute_tool_call(call("write", index=2), ctx))
    await asyncio.sleep(0)
    observed = list(events)
    release.set()
    await asyncio.gather(first, second)
    assert observed == [("start", 1)]


@pytest.mark.asyncio
async def test_finish_is_barrier_and_skips_later_mutation(agent):
    events = []
    agent.tool_executor = ToolExecutor(
        [FinishTaskTool(), ProbeTool("write", events, False)]
    )
    output = await agent._execute_tool_calls(
        [call("finish_task", summary="done"), call("write", "w", index=1)],
        agent._build_context(),
        1,
    )
    assert output["summary"] == "done" and events == []
    assert (
        json.loads(agent.messages[-1]["content"])["error_code"]
        == "CANCELLED_AFTER_FINISH"
    )


class Checkpoint:
    task_id = 1

    def __init__(self, states=None):
        self.states = states or {}
        self.messages = []

    async def load_tool_call_states(self, session_id):
        return self.states

    async def append_message(self, session_id, message):
        self.messages.append(message)
        return len(self.messages)

    async def mark_tool_call_running(self, session_id, ident):
        self.states[ident] = {"status": "running"}

    async def record_tool_result(self, session_id, ident, message, status, error=""):
        self.states[ident] = {"status": status}
        return await self.append_message(session_id, message)

    async def mark_tool_call_cancelled(self, session_id, ident):
        self.states[ident] = {"status": "cancelled"}


def restore(agent, calls, states, results=()):
    agent.restored_messages = True
    agent.checkpoint = Checkpoint(states)
    agent.session_id = 1
    agent.messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "tool_calls": [tool_call_to_dict(tc) for tc in calls]},
        *results,
    ]


@pytest.mark.asyncio
async def test_interrupted_mutation_requires_reconciliation(agent, client):
    restore(
        agent,
        [call("write_file", file_path="x", content="overwrite")],
        {"call_1": {"status": "running"}},
    )
    result = await agent.execute("test", "test")
    assert result.outcome == "blocked" and result.error == "reconciliation_required"
    assert not (agent.workspace / "x").exists()
    client.call_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_completed_finish_recovers_without_another_model_call(agent, client):
    restore(
        agent,
        [call("finish_task", summary="done")],
        {"call_1": {"status": "completed"}},
        [
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": json.dumps({"_terminal": True, "summary": "done"}),
            }
        ],
    )
    result = await agent.execute("test", "test")
    assert result.success and result.summary == "done"
    client.call_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_interrupted_read_can_retry(agent, client):
    events = []
    agent.tool_executor.register(ProbeTool("read", events))
    restore(agent, [call("read", index=1)], {"call_1": {"status": "running"}})
    client.call_with_retry.side_effect = [
        response(calls=[call("finish_task", "f", summary="done")])
    ]
    result = await agent.execute("test", "test")
    assert result.success and events == [("start", 1), ("end", 1)]


@pytest.mark.asyncio
async def test_cancellation_drains_every_read_child_and_records_cancelled(agent):
    events, gate = [], asyncio.Event()
    agent.tool_executor = ToolExecutor([ProbeTool("read", events, gate=gate)])
    agent.checkpoint, agent.session_id = Checkpoint(), 1
    running = asyncio.create_task(
        agent._execute_tool_calls(
            [call("read", "a", index=1), call("read", "b", index=2)],
            agent._build_context(),
            1,
        )
    )
    for _ in range(10):
        await asyncio.sleep(0)
        if len(events) == 2:
            break
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert sorted(events) == sorted(
        [("start", 1), ("start", 2), ("end", 1), ("end", 2)]
    )
    assert {s["status"] for s in agent.checkpoint.states.values()} == {"cancelled"}


@pytest.mark.asyncio
async def test_read_batch_cap_is_enforced(agent):
    events = []
    agent.tool_executor = ToolExecutor([ProbeTool("read", events)])
    ctx = agent._build_context()
    ctx.max_parallel_reads = 2
    await agent._execute_tool_calls(
        [call("read", str(i), index=i) for i in range(7)], ctx, 1
    )
    active = peak = 0
    for event, _ in events:
        active += 1 if event == "start" else -1
        peak = max(peak, active)
    assert peak == 2 and active == 0


@pytest.mark.asyncio
async def test_failed_finish_cannot_terminate(agent, client, monkeypatch):
    monkeypatch.setattr(
        FinishTaskTool,
        "execute",
        AsyncMock(
            return_value=ToolResult(False, output={"_terminal": True}, error="failed")
        ),
    )
    client.call_with_retry.side_effect = [
        response(calls=[call("finish_task", summary="done")]),
        response("done"),
        response("done"),
    ]
    result = await agent.execute("test", "test")
    assert result.outcome == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "expected"),
    [("max_model_rounds", "model_round_limit"), ("max_tool_calls", "tool_call_limit")],
)
async def test_nonprogress_tool_loops_are_bounded(
    agent, client, monkeypatch, limit, expected
):
    limits = {
        "max_model_rounds": 8,
        "max_tool_calls": 8,
        "max_parallel_reads": 2,
        "max_no_progress_rounds": 8,
    }
    limits[limit] = 1
    monkeypatch.setattr(runtime, "get_runtime_limits", AsyncMock(return_value=limits))
    client.call_with_retry.side_effect = [
        response(calls=[call("unknown", str(i))]) for i in range(8)
    ]
    result = await agent.execute("test", "test")
    assert result.outcome == "blocked" and result.error == expected


@pytest.mark.asyncio
async def test_cancel_event_cancels_active_children(agent, client):
    gate, cancelled, events = asyncio.Event(), asyncio.Event(), []
    agent.tool_executor = ToolExecutor([ProbeTool("read", events, gate=gate)])
    client.call_with_retry.side_effect = [
        response(calls=[call("read", "a", index=1), call("read", "b", index=2)])
    ]
    task = asyncio.create_task(agent.execute("t", "t", cancel_event=cancelled))
    async with asyncio.timeout(2):
        while len(events) < 2:
            await asyncio.sleep(0.001)
    cancelled.set()
    result = await task
    assert result.outcome == "cancelled"
    assert sorted(events) == sorted(
        [("start", 1), ("start", 2), ("end", 1), ("end", 2)]
    )


@pytest.mark.asyncio
async def test_inconsistent_completed_read_is_not_replayed(agent, client):
    restore(
        agent, [call("read_file", file_path="x")], {"call_1": {"status": "completed"}}
    )
    result = await agent.execute("t", "t")
    assert result.error == "checkpoint_inconsistent"
    client.call_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_failed_tool_persists_failed_state(agent):
    agent.checkpoint, agent.session_id = Checkpoint(), 1
    await agent._execute_tool_calls([call("unknown")], agent._build_context(), 1)
    assert agent.checkpoint.states["call_1"]["status"] == "failed"


def test_finish_and_skill_are_runtime_barriers(agent):
    assert not agent.tool_executor.metadata("finish_task").parallel_safe
    assert not agent.tool_executor.metadata("use_skill").parallel_safe


@pytest.mark.asyncio
async def test_failed_result_persists_non_success_session(tmp_path, monkeypatch):
    from backend.services.agent_team.iteration_loop import IterationLoopService

    checkpoint = SimpleNamespace(
        finish_session=AsyncMock(),
        save_session_result=AsyncMock(),
        complete_session=AsyncMock(),
    )
    service = IterationLoopService(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint=checkpoint
    )
    fake_agent = SimpleNamespace(
        session_id=7,
        execute=AsyncMock(
            return_value=runtime.FullStackResult(False, "blocked", error="no_progress")
        ),
    )
    monkeypatch.setattr(service, "_create_agent", AsyncMock(return_value=fake_agent))
    outcome = await service.run("task", "task")
    assert outcome.outcome == "blocked"
    args = checkpoint.finish_session.call_args.args
    assert args[0:2] == (7, "blocked") and args[2]["outcome"] == "blocked"
    checkpoint.complete_session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_iteration_exception_has_durable_session_outcome(
    tmp_path, monkeypatch, failure
):
    from backend.services.agent_team.iteration_loop import IterationLoopService

    checkpoint = SimpleNamespace(finish_session=AsyncMock())
    service = IterationLoopService(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint=checkpoint
    )
    fake_agent = SimpleNamespace(session_id=7, execute=AsyncMock(side_effect=failure))
    monkeypatch.setattr(service, "_create_agent", AsyncMock(return_value=fake_agent))
    with pytest.raises(failure):
        await service.run("task", "task")
    expected = (
        "cancelled" if failure is asyncio.CancelledError else "unrecoverable_error"
    )
    assert checkpoint.finish_session.call_args.args[1] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{"summary": "done"}, {"_terminal": True, "summary": "done"}]
)
async def test_finish_restore_rejects_missing_marker_or_argument_mismatch(
    agent, client, payload
):
    restore(
        agent,
        [call("finish_task", summary="different summary")],
        {"call_1": {"status": "completed"}},
        [{"role": "tool", "tool_call_id": "call_1", "content": json.dumps(payload)}],
    )
    result = await agent.execute("t", "t")
    assert result.error == "checkpoint_inconsistent"
    client.call_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_completed_session_without_successful_finish_cannot_resume(agent, client):
    restore(agent, [], {})
    agent.checkpoint.load_session_result = AsyncMock(return_value={"success": True})
    result = await agent.execute("t", "t")
    assert result.error == "checkpoint_inconsistent"
    client.call_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_thread_mutation_holds_barrier_until_thread_finishes(agent):
    started, release = threading.Event(), threading.Event()
    events = []

    class ThreadWrite(BaseTool):
        name = "thread_write"

        async def execute(self, args, ctx):
            def mutate():
                started.set()
                release.wait(2)
                events.append(("mutation_finished", 0))

            await asyncio.to_thread(mutate)
            return ToolResult(True)

    writer = ToolExecutor([ThreadWrite()])
    reader = ToolExecutor([ProbeTool("read", events)])
    writing = asyncio.create_task(
        writer.execute_tool_call(call("thread_write"), agent._build_context())
    )
    reading = None
    try:
        async with asyncio.timeout(1):
            while not started.is_set():
                await asyncio.sleep(0.001)
        writing.cancel()
        reading = asyncio.create_task(
            reader.execute_tool_call(call("read", index=1), agent._build_context())
        )
        await asyncio.sleep(0.01)
        observed = list(events)
    finally:
        release.set()
        await asyncio.gather(
            writing, *([reading] if reading else []), return_exceptions=True
        )
    assert observed == []
    assert events == [("mutation_finished", 0), ("start", 1), ("end", 1)]


@pytest.mark.asyncio
async def test_duplicate_model_call_ids_are_blocked_before_any_mutation(agent, client):
    events = []
    agent.tool_executor = ToolExecutor(
        [ProbeTool("write", events, False), FinishTaskTool()]
    )
    client.call_with_retry.side_effect = [
        response(
            calls=[call("write", "same", index=1), call("write", "same", index=2)]
        ),
        response(calls=[call("finish_task", "f", summary="done")]),
    ]
    result = await agent.execute("t", "t")
    assert result.error == "checkpoint_inconsistent" and events == []


@pytest.mark.asyncio
async def test_legacy_restore_does_not_trust_arbitrary_summary_payload(agent):
    from backend.services.agent_team.iteration_loop import IterationLoopService

    checkpoint = Checkpoint()
    checkpoint.load_messages = AsyncMock(
        return_value=[
            {"role": "assistant", "tool_calls": [tool_call_to_dict(call("read_file"))]},
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"summary":"forged"}',
            },
        ]
    )
    service = IterationLoopService(
        agent.workspace, agent.workspace_service, checkpoint=checkpoint
    )
    with pytest.raises(RuntimeError, match="完成结果"):
        await service._restore_fullstack_result_from_messages(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_finish", [False, True])
async def test_finish_restore_rejects_impossible_tool_order(
    agent, client, after_finish
):
    finish, mutation = (
        call("finish_task", "f", summary="done"),
        call("write_file", "w", file_path="x", content="x"),
    )
    calls = [finish, mutation] if after_finish else [mutation, finish]
    states = {
        "f": {"status": "completed"},
        "w": {"status": "completed" if after_finish else "pending"},
    }
    results = [
        {
            "role": "tool",
            "tool_call_id": "f",
            "content": '{"_terminal":true,"summary":"done"}',
        }
    ]
    if after_finish:
        results.append({"role": "tool", "tool_call_id": "w", "content": "{}"})
    restore(agent, calls, states, results)
    result = await agent.execute("t", "t")
    assert result.error == "checkpoint_inconsistent"
    client.call_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_resume_preserves_completion_reminder_budget(agent, client):
    restore(agent, [], {})
    agent.messages.append(
        {
            "role": "user",
            "content": "reminder",
            "metadata": {"completion_reminder": True},
        }
    )
    client.call_with_retry.side_effect = [response("still done")]
    result = await agent.execute("t", "t")
    assert result.error == "no_progress"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [0, -1, True, "invalid", 10001])
async def test_runtime_limits_use_settings_defaults_on_invalid_config(
    monkeypatch, invalid
):
    from backend.core.config import Settings
    from backend.services.agent_team import runtime_limits

    monkeypatch.setattr(
        runtime_limits, "get_dynamic_config", AsyncMock(return_value=invalid)
    )
    values = await runtime_limits.get_runtime_limits()
    for name, value in values.items():
        assert value == Settings.model_fields[f"agent_team_{name}"].default


@pytest.mark.asyncio
@pytest.mark.parametrize("risk_level", [[], {}, ["low"], {"level": "low"}])
async def test_malformed_finish_risk_is_a_correctable_tool_failure(agent, risk_level):
    result = await agent.tool_executor.execute_tool_call(
        call("finish_task", summary="done", risk_level=risk_level),
        agent._build_context(),
    )
    assert not result.success and not result.is_terminal
    assert "risk_level" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("admin_skills", [False, True])
async def test_empty_repository_context_is_neither_checkpointed_nor_reinforced(
    agent, client, admin_skills
):
    agent.checkpoint, agent.session_id = Checkpoint(), 1
    client.call_with_retry.side_effect = [
        response(calls=[call("finish_task", summary="verified")])
    ]
    skills_context = (
        {"skills_index": {"admin-docs": {"source_type": "admin", "slug": "admin-docs"}}}
        if admin_skills
        else None
    )
    result = await agent.execute("test", "test", skills_context=skills_context)
    assert result.success
    for messages in (
        agent.messages,
        agent.checkpoint.messages,
        client.call_with_retry.call_args.kwargs["messages"],
    ):
        assert not any(
            m.get("metadata", {}).get("repository_context") for m in messages
        )


@pytest.mark.parametrize(
    "data_kind", ["instructions", "repository_skill", "diagnostic", "workflow"]
)
def test_nonempty_repository_snapshot_is_preserved(agent, data_kind):
    from backend.services.agent_team.repository_context import (
        RepositoryContext,
        RepositoryInstruction,
    )

    ctx = agent._build_context()
    ctx.repository_context = RepositoryContext(agent.workspace)
    if data_kind == "instructions":
        ctx.repository_instructions["AGENTS.md"] = RepositoryInstruction(
            "AGENTS.md", ".", "actual repository instruction"
        )
        expected = "actual repository instruction"
    elif data_kind == "repository_skill":
        ctx.extra["skills_index"] = {
            "docs": {
                "source_type": "repository",
                "slug": "docs",
                "description": "actual repository skill",
            }
        }
        expected = "actual repository skill"
    elif data_kind == "diagnostic":
        ctx.repository_context.diagnostics.append("actual diagnostic")
        expected = "actual diagnostic"
    else:
        ctx.active_skill_tools["docs"] = frozenset({"read_file"})
        expected = "read_file"
    message = agent._repository_message(ctx)
    assert message["role"] == "user"
    assert message["metadata"]["repository_context"] is True
    assert expected in message["content"]


def test_runtime_budgets_default_to_unlimited_productive_work():
    from backend.core.config import Settings

    assert Settings.model_fields["agent_team_max_model_rounds"].default == 0
    assert Settings.model_fields["agent_team_max_tool_calls"].default == 0


@pytest.fixture
def no_progress_window(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "get_runtime_limits",
        AsyncMock(
            return_value={
                "max_model_rounds": 0,
                "max_tool_calls": 0,
                "max_parallel_reads": 2,
                "max_no_progress_rounds": 2,
            }
        ),
    )


@pytest.mark.asyncio
async def test_productive_writes_continue_beyond_no_progress_window(
    agent, client, no_progress_window
):
    client.call_with_retry.side_effect = [
        response(
            calls=[
                call(
                    "write_file",
                    str(i),
                    file_path=f"step_{i}.txt",
                    content=f"completed step {i}",
                )
            ]
        )
        for i in range(5)
    ] + [
        response(
            calls=[call("finish_task", "finish", summary="all five steps completed")]
        )
    ]
    result = await agent.execute("task", "task")
    assert result.success and result.tool_calls_count == 6
    assert [
        path.read_text() for path in sorted(agent.workspace.glob("step_*.txt"))
    ] == [f"completed step {i}" for i in range(5)]


@pytest.mark.asyncio
@pytest.mark.parametrize("work", ["identical_reads", "alternating_reads", "failures"])
async def test_unchanged_tool_work_stops_without_total_budgets(
    agent, client, no_progress_window, work
):
    agent.tool_executor.register(ProbeTool("read", []))
    count = {"identical_reads": 3, "alternating_reads": 4, "failures": 3}[work]
    client.call_with_retry.side_effect = [
        response(
            calls=[
                call(
                    "unknown" if work == "failures" else "read",
                    str(i),
                    index=i % 2 if work == "alternating_reads" else 0,
                )
            ]
        )
        for i in range(count)
    ]
    result = await agent.execute("task", "task")
    assert result.error == "no_progress" and result.outcome == "blocked"
    assert result.tool_calls_count == count
    assert client.call_with_retry.await_count == count


@pytest.mark.asyncio
@pytest.mark.parametrize("new_guidance", [False, True])
async def test_resume_keeps_no_progress_evidence_but_admits_new_guidance(
    agent, client, no_progress_window, new_guidance
):
    agent.tool_executor.register(ProbeTool("read", []))
    calls = [call("read", str(i), index=0) for i in range(3)]
    restore(agent, [], {str(i): {"status": "completed"} for i in range(3)})
    for tool_call in calls:
        agent.messages.extend(
            [
                {"role": "assistant", "tool_calls": [tool_call_to_dict(tool_call)]},
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": '{"index":0}',
                },
            ]
        )
    client.call_with_retry.side_effect = [
        response(
            calls=[
                call(
                    "finish_task", "finish", summary="new evidence confirmed completion"
                )
            ]
        )
    ]
    guidance = (
        AsyncMock(
            return_value="New evidence: the read-only verification is sufficient; finish now."
        )
        if new_guidance
        else None
    )
    result = await agent.execute("task", "task", guidance_callback=guidance)
    assert result.success is new_guidance
    assert client.call_with_retry.await_count == int(new_guidance)
    if not new_guidance:
        assert result.error == "no_progress"


@pytest.mark.asyncio
async def test_distinct_error_diagnostics_are_new_investigation_evidence(
    agent, client, no_progress_window
):
    class Diagnostics(BaseTool):
        name = "diagnostics"

        async def execute(self, args, ctx):
            return ToolResult(False, error=f"Missing dependency {args['index']}")

    agent.tool_executor.register(Diagnostics())
    client.call_with_retry.side_effect = [
        response(calls=[call("diagnostics", str(i), index=i)]) for i in range(5)
    ] + [
        response(calls=[call("finish_task", "finish", summary="diagnostics collected")])
    ]
    result = await agent.execute("task", "task")
    assert result.success and result.tool_calls_count == 6


@pytest.mark.asyncio
async def test_new_tool_evidence_resets_consecutive_text_reminder(agent, client):
    agent.tool_executor.register(ProbeTool("read", []))
    client.call_with_retry.side_effect = [
        response("working"),
        response(calls=[call("read", index=1)]),
        response("verified"),
        response(calls=[call("finish_task", "finish", summary="done")]),
    ]
    result = await agent.execute("task", "task")
    assert result.success
    assert (
        sum(
            bool(m.get("metadata", {}).get("completion_reminder"))
            for m in agent.messages
        )
        == 2
    )


@pytest.mark.asyncio
async def test_new_repository_context_resets_stalled_history(
    agent, client, no_progress_window
):
    agent.tool_executor.register(ProbeTool("read", []))
    restore(agent, [], {str(i): {"status": "completed"} for i in range(3)})
    for i in range(3):
        agent.messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [tool_call_to_dict(call("read", str(i), index=0))],
                },
                {"role": "tool", "tool_call_id": str(i), "content": '{"index":0}'},
            ]
        )
    (agent.workspace / "AGENTS.md").write_text(
        "New repository evidence: finish after the existing verification."
    )
    client.call_with_retry.side_effect = [
        response(
            calls=[call("finish_task", "finish", summary="new instructions applied")]
        )
    ]
    result = await agent.execute("task", "task")
    assert result.success


@pytest.mark.asyncio
async def test_new_guidance_resets_text_reminder_after_resume(agent, client):
    restore(agent, [], {})
    agent.messages.append(
        {
            "role": "user",
            "content": "reminder",
            "metadata": {"completion_reminder": True},
        }
    )
    client.call_with_retry.side_effect = [
        response("checking new evidence"),
        response(calls=[call("finish_task", "finish", summary="done")]),
    ]
    guidance = AsyncMock(
        side_effect=["New requirement: consider the updated verification evidence.", ""]
    )
    result = await agent.execute("task", "task", guidance_callback=guidance)
    assert result.success
    assert (
        sum(
            bool(m.get("metadata", {}).get("completion_reminder"))
            for m in agent.messages
        )
        == 2
    )


@pytest.mark.parametrize("guidance_id", [1, 2])
def test_reminder_reset_distinguishes_new_guidance_admission_from_replay(guidance_id):
    tracker = runtime._NoProgressTracker()
    messages = [
        {
            "role": "user",
            "content": "Retry the verification.",
            "metadata": {"guidance_ids": [1]},
        },
        {
            "role": "user",
            "content": "completion reminder",
            "metadata": {"completion_reminder": True},
        },
    ]
    tracker.update(messages)
    messages.append(
        {
            "role": "user",
            "content": "Retry the verification.",
            "metadata": {"guidance_ids": [guidance_id]},
        }
    )
    tracker.update(messages)
    assert tracker.reminded is (guidance_id == 1)
