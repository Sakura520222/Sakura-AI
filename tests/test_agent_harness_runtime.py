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
async def test_twelve_text_turns_continue_until_explicit_finish(agent, client):
    client.call_with_retry.side_effect = [response("done") for _ in range(12)] + [
        response(calls=[call("finish_task", "finish", summary="verified")])
    ]
    result = await agent.execute("test", "test")
    assert result.success and result.summary == "verified"
    assert client.call_with_retry.await_count == 13
    assert (
        sum(
            bool(m.get("metadata", {}).get("completion_reminder"))
            for m in agent.messages
        )
        == 12
    )
    assert (
        sum(
            bool(m.get("metadata", {}).get("strategy_self_check"))
            for m in agent.messages
        )
        == 1
    )


@pytest.mark.asyncio
async def test_same_tool_strategy_warning_is_nonterminal_and_emitted_once(
    agent, client
):
    agent.tool_executor.register(ProbeTool("read", []))
    client.call_with_retry.side_effect = [
        response(calls=[call("read", str(i), index=0)]) for i in range(12)
    ] + [response(calls=[call("finish_task", "finish", summary="changed strategy")])]
    result = await agent.execute("task", "task")
    assert result.success and result.tool_calls_count == 13
    warnings = [
        m for m in agent.messages if m.get("metadata", {}).get("strategy_self_check")
    ]
    assert len(warnings) == 1
    assert warnings[0]["metadata"]["strategy_self_check"]["window"] == 10
    assert warnings[0]["metadata"]["strategy_self_check"]["kind"] == "tool"


@pytest.mark.asyncio
async def test_changing_results_do_not_trigger_repeat_warning(agent, client):
    class Changing(BaseTool):
        name = "changing"
        counter = 0

        async def execute(self, args, ctx):
            self.counter += 1
            return ToolResult(True, output={"state": self.counter})

    agent.tool_executor.register(Changing())
    client.call_with_retry.side_effect = [
        response(calls=[call("changing", str(i), same=True)]) for i in range(12)
    ] + [response(calls=[call("finish_task", "finish", summary="new results")])]
    assert (await agent.execute("task", "task")).success
    assert not any(
        m.get("metadata", {}).get("strategy_self_check") for m in agent.messages
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("guidance", [False, True])
async def test_repeat_detection_rebuilds_on_resume_and_guidance_resets(
    agent, client, guidance
):
    agent.tool_executor.register(ProbeTool("read", []))
    restore(agent, [], {str(i): {"status": "completed"} for i in range(9)})
    for i in range(9):
        agent.messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [tool_call_to_dict(call("read", str(i), index=0))],
                },
                {"role": "tool", "tool_call_id": str(i), "content": '{"index":0}'},
            ]
        )
    client.call_with_retry.side_effect = [
        response(calls=[call("read", "current", index=0)]),
        response(calls=[call("finish_task", "finish", summary="resumed")]),
    ]
    callback = (
        AsyncMock(side_effect=["Change the investigation strategy.", ""])
        if guidance
        else None
    )
    result = await agent.execute("task", "task", guidance_callback=callback)
    assert result.success
    assert sum(
        bool(m.get("metadata", {}).get("strategy_self_check")) for m in agent.messages
    ) == (0 if guidance else 1)


@pytest.mark.asyncio
async def test_empty_finish_requires_a_later_valid_finish(agent, client):
    client.call_with_retry.side_effect = [
        response(calls=[call("finish_task", summary=" ")]),
        response("done"),
        response("done"),
        response(calls=[call("finish_task", "valid", summary="verified")]),
    ]
    result = await agent.execute("test", "test")
    assert result.success and result.summary == "verified"
    rejected = next(m for m in agent.messages if m.get("role") == "tool")
    assert "error" in json.loads(rejected["content"])


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
async def test_read_batch_has_no_artificial_parallel_cap(agent):
    events = []
    agent.tool_executor = ToolExecutor([ProbeTool("read", events)])
    ctx = agent._build_context()
    await agent._execute_tool_calls(
        [call("read", str(i), index=i) for i in range(7)], ctx, 1
    )
    active = peak = 0
    for event, _ in events:
        active += 1 if event == "start" else -1
        peak = max(peak, active)
    assert peak == 7 and active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["finish", "cancel"])
async def test_repeated_failed_finish_does_not_stop_or_forge_success(
    agent, client, monkeypatch, ending
):
    original = FinishTaskTool.execute
    cancelled = asyncio.Event()

    async def finish(self, args, ctx):
        if args["summary"] == "verified":
            return await original(self, args, ctx)
        return ToolResult(False, output={"_terminal": True}, error="failed")

    monkeypatch.setattr(FinishTaskTool, "execute", finish)
    replies = []
    for i in range(12):
        replies.extend(
            [
                response(calls=[call("finish_task", f"failed-{i}", summary="done")]),
                response("done"),
            ]
        )
    replies.append(response(calls=[call("finish_task", "finish", summary="verified")]))
    count = 0

    async def model(**kwargs):
        nonlocal count
        reply = replies[count]
        count += 1
        if ending == "cancel" and count == 24:
            cancelled.set()
        return reply

    client.call_with_retry.side_effect = model
    result = await agent.execute("test", "test", cancel_event=cancelled)
    assert result.success is (ending == "finish")
    assert result.error == ("" if ending == "finish" else "cancelled")
    assert count == (25 if ending == "finish" else 24)
    failed = [
        m
        for m in agent.messages
        if m.get("role") == "tool" and m.get("tool_call_id", "").startswith("failed-")
    ]
    assert len(failed) == 12 and all("failed" in m["content"] for m in failed)


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
async def test_resume_old_completion_reminder_does_not_limit_text_turns(agent, client):
    restore(agent, [], {})
    agent.messages.append(
        {
            "role": "user",
            "content": "reminder",
            "metadata": {"completion_reminder": True},
        }
    )
    client.call_with_retry.side_effect = [response("still done") for _ in range(12)] + [
        response(calls=[call("finish_task", "finish", summary="resumed")])
    ]
    result = await agent.execute("t", "t")
    assert result.success and result.summary == "resumed"
    assert client.call_with_retry.await_count == 13


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
        from backend.services.agent_team.skill_scope import SkillRestriction

        ctx.active_skill_tools["docs"] = SkillRestriction.from_metadata('["read_file"]')
        expected = "read_file"
    message = agent._repository_message(ctx)
    assert message["role"] == "user"
    assert message["metadata"]["repository_context"] is True
    assert expected in message["content"]


def test_removed_runtime_limits_are_not_settings():
    from backend.core.config import Settings

    for key in (
        "agent_team_max_model_rounds",
        "agent_team_max_tool_calls",
        "agent_team_max_parallel_reads",
        "agent_team_max_no_progress_rounds",
    ):
        assert key not in Settings.model_fields


@pytest.mark.asyncio
async def test_productive_writes_have_no_total_round_or_tool_budget(agent, client):
    client.call_with_retry.side_effect = [
        response(
            calls=[
                call(
                    "write_file",
                    str(i),
                    file_path=f"step_{i:02}.txt",
                    content=f"completed step {i}",
                )
            ]
        )
        for i in range(12)
    ] + [response(calls=[call("finish_task", "finish", summary="all steps completed")])]
    result = await agent.execute("task", "task")
    assert result.success and result.tool_calls_count == 13
    assert [
        path.read_text() for path in sorted(agent.workspace.glob("step_*.txt"))
    ] == [f"completed step {i}" for i in range(12)]


@pytest.mark.asyncio
@pytest.mark.parametrize("work", ["identical_reads", "alternating_reads", "failures"])
async def test_repeated_tool_work_remains_autonomous_until_finish(agent, client, work):
    agent.tool_executor.register(ProbeTool("read", []))
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
        for i in range(12)
    ] + [
        response(calls=[call("finish_task", "finish", summary="finished autonomously")])
    ]
    result = await agent.execute("task", "task")
    assert result.success and result.tool_calls_count == 13
    assert client.call_with_retry.await_count == 13


@pytest.mark.asyncio
@pytest.mark.parametrize("new_guidance", [False, True])
async def test_resume_repeated_tool_history_does_not_require_new_guidance(
    agent, client, new_guidance
):
    agent.tool_executor.register(ProbeTool("read", []))
    calls = [call("read", str(i), index=0) for i in range(12)]
    restore(agent, [], {str(i): {"status": "completed"} for i in range(12)})
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
                call("finish_task", "finish", summary="prior verification sufficient")
            ]
        )
    ]
    guidance = (
        AsyncMock(return_value="Finish after considering new guidance.")
        if new_guidance
        else None
    )
    result = await agent.execute("task", "task", guidance_callback=guidance)
    assert result.success and result.tool_calls_count == 13
    assert client.call_with_retry.await_count == 1


@pytest.mark.asyncio
async def test_distinct_error_diagnostics_are_not_an_execution_limit(agent, client):
    class Diagnostics(BaseTool):
        name = "diagnostics"

        async def execute(self, args, ctx):
            return ToolResult(False, error=f"Missing dependency {args['index']}")

    agent.tool_executor.register(Diagnostics())
    client.call_with_retry.side_effect = [
        response(calls=[call("diagnostics", str(i), index=i)]) for i in range(12)
    ] + [
        response(calls=[call("finish_task", "finish", summary="diagnostics collected")])
    ]
    result = await agent.execute("task", "task")
    assert result.success and result.tool_calls_count == 13


@pytest.mark.asyncio
async def test_text_reminders_continue_after_tool_evidence(agent, client):
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
async def test_resume_observes_current_repository_context(agent, client):
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
async def test_new_guidance_survives_old_text_reminder_after_resume(agent, client):
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
def test_repeat_detection_distinguishes_new_guidance_from_replay(guidance_id):
    from backend.services.agent_team.strategy_self_check import StrategySelfCheckState

    tracker = StrategySelfCheckState()
    messages = [
        {"role": "user", "content": "Retry.", "metadata": {"guidance_ids": [1]}}
    ]
    for i in range(9):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        tool_call_to_dict(call("read_file", str(i), a=1, b=2))
                    ],
                },
                {"role": "tool", "tool_call_id": str(i), "content": '{"x":1,"y":2}'},
            ]
        )
    assert tracker.update(messages) == []
    messages.extend(
        [
            {
                "role": "user",
                "content": "Retry.",
                "metadata": {"guidance_ids": [guidance_id]},
            },
            {
                "role": "assistant",
                "tool_calls": [tool_call_to_dict(call("read_file", "new", b=2, a=1))],
            },
            {"role": "tool", "tool_call_id": "new", "content": '{ "y": 2, "x": 1 }'},
        ]
    )
    events = tracker.update(messages)
    assert len(events) == (1 if guidance_id == 1 else 0)
    if events:
        messages.extend(events)
        assert StrategySelfCheckState().update(messages) == []
