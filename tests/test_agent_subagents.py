"""Read-only delegation, durable child identity and runtime lifecycle contracts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.models import agent_team_models as models
from backend.models.database import AppConfig, Base
from backend.services.agent_team import conversation_checkpoint as checkpoint_module
from backend.services.agent_team import fullstack_expert as runtime
from backend.services.agent_team.skill_scope import SkillRestriction
from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.tools.registry import create_executor
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.utils.message_utils import tool_call_to_dict


def call(name, ident="call_1", **args):
    return SimpleNamespace(
        id=ident, function=SimpleNamespace(name=name, arguments=json.dumps(args))
    )


def response(*calls, text=""):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=list(calls))
            )
        ],
        usage=None,
    )


@pytest.fixture
def persistence(monkeypatch):
    engine = create_engine("sqlite://")
    tables = [
        models.AgentTeamTask,
        models.AgentTeamSession,
        models.AgentTeamMessage,
        models.AgentTeamToolCall,
        models.AgentTeamSubagent,
        models.AgentTeamUsage,
        AppConfig,
    ]
    Base.metadata.create_all(engine, tables=[model.__table__ for model in tables])

    class Adapter:
        def __init__(self):
            self.db = Session(engine, expire_on_commit=False)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.db.close()

        async def get(self, model, ident, **kwargs):
            return self.db.get(model, ident, **kwargs)

        async def execute(self, statement):
            return self.db.execute(statement)

        def add(self, item):
            self.db.add(item)

        async def flush(self):
            self.db.flush()

        async def commit(self):
            self.db.commit()

        async def refresh(self, item):
            self.db.refresh(item)

    monkeypatch.setattr(checkpoint_module.db_module, "async_session", Adapter)
    monkeypatch.setattr(checkpoint_module, "_publish", AsyncMock())
    for task_id in (1, 2):
        with Session(engine) as db:
            db.add(
                models.AgentTeamTask(
                    id=task_id,
                    source_type="issue",
                    repo_full_name="o/r",
                    repo_owner="o",
                    repo_name="r",
                    title="parent",
                )
            )
            db.commit()
    yield checkpoint_module.ConversationCheckpointService(1), engine
    engine.dispose()


@pytest.mark.asyncio
async def test_newer_child_session_never_becomes_parent_resume_cursor(persistence):
    checkpoint, _ = persistence
    parent = await checkpoint.create_session(1, "agent")
    await checkpoint.create_session(1, "subagent")
    assert (await checkpoint.get_resume_cursor()).session_id == parent.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["execute_raw", "execute_tool_call", "_execute_tool_call"]
)
@pytest.mark.parametrize(
    "name,args",
    [
        ("write_file", {"file_path": "evidence.txt", "content": "changed"}),
        (
            "edit_file",
            {
                "file_path": "evidence.txt",
                "old_string": "original",
                "new_string": "changed",
            },
        ),
        ("run_command", {"command": "touch forbidden"}),
        ("revert_file", {"file_path": "evidence.txt"}),
        ("use_skill", {"slug": "writer", "end_skill": True}),
        ("spawn_agent", {"task": "write for me", "profile": "full_access"}),
    ],
)
async def test_read_only_executor_denies_write_and_delegation_on_every_entrypoint(
    tmp_path, method, name, args
):
    target = tmp_path / "evidence.txt"
    target.write_text("original")
    executor = create_executor(read_only=True)
    ctx = ToolContext(
        str(tmp_path),
        AgentTeamWorkspaceService(tmp_path),
        extra={"read_only": False, "profile": "full_access"},
    )
    if method == "execute_raw":
        result = await executor.execute_raw(name, args, ctx)
    else:
        result = await getattr(executor, method)(call(name, **args), ctx)
    assert not result.success and result.error_code == "SUBAGENT_TOOL_RESTRICTED"
    assert target.read_text() == "original" and not (tmp_path / "forbidden").exists()


@pytest.mark.asyncio
async def test_child_reads_allowed_file_but_inherited_skill_cannot_be_cleared(tmp_path):
    (tmp_path / "evidence.txt").write_text("observed")
    executor = create_executor(
        read_only=True, delegated_scope=SkillRestriction.from_metadata(["read_file"])
    )
    ctx = ToolContext(str(tmp_path), AgentTeamWorkspaceService(tmp_path))
    result = await executor.execute_raw("read_file", {"file_path": "evidence.txt"}, ctx)
    assert result.success and "observed" in str(result.output)
    denied = await executor.execute_raw("list_directory", {}, ctx)
    assert not denied.success and denied.error_code == "SUBAGENT_TOOL_RESTRICTED"
    finished = await executor.execute_raw(
        "finish_task", {"summary": "observed evidence"}, ctx
    )
    assert finished.is_terminal


async def recorded_spawn(checkpoint, parent_id, ident="spawn", task="Inspect evidence"):
    spawn = call("spawn_agent", ident, task=task)
    await checkpoint.append_message(
        parent_id, {"role": "assistant", "tool_calls": [tool_call_to_dict(spawn)]}
    )
    await checkpoint.mark_tool_call_running(parent_id, ident)
    return spawn


@pytest.mark.asyncio
async def test_atomic_spawn_replay_has_one_durable_child_and_scoped_parent(persistence):
    from backend.services.agent_team.subagents import SubagentStore

    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    await recorded_spawn(checkpoint, parent.id)
    store = SubagentStore(1)
    child = await store.create(
        parent.id,
        "spawn",
        "Inspect evidence",
        {"investigation": SkillRestriction.from_metadata(["read_file"])},
    )
    replay = await store.create(parent.id, "spawn", "Inspect evidence", {})
    assert child.session_id == replay.session_id
    assert replay.skill_scopes["investigation"].allows("read_file")
    assert not replay.skill_scopes["investigation"].allows("list_directory")
    with Session(engine) as db:
        assert len(list(db.scalars(select(models.AgentTeamSubagent)))) == 1
    with pytest.raises(ValueError):
        await SubagentStore(2).get(child.session_id, parent.id)
    other = await checkpoint.create_session(1, "agent")
    with pytest.raises(ValueError):
        await store.get(child.session_id, other.id)


@pytest.mark.asyncio
async def test_spawn_rejects_uncheckpointed_or_changed_tool_arguments(persistence):
    from backend.services.agent_team.subagents import SubagentStore

    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    store = SubagentStore(1)
    with pytest.raises(ValueError):
        await store.create(parent.id, "missing", "Inspect evidence", {})
    await recorded_spawn(checkpoint, parent.id)
    with pytest.raises(ValueError):
        await store.create(parent.id, "spawn", "different task", {})
    with Session(engine) as db:
        assert not list(db.scalars(select(models.AgentTeamSubagent)))


@pytest.fixture
def model_factory(monkeypatch):
    from backend.services.agent_team.network_policy import AgentTeamNetworkPolicy
    from backend.services.agent_team.tools import registry

    requests = []

    def install(handler):
        async def create(**kwargs):
            async def invoke(**kwargs):
                requests.append(kwargs)
                return await handler(**kwargs)

            client = SimpleNamespace(
                resolve_role_primary_candidate=AsyncMock(return_value=None),
                call_with_retry=invoke,
            )
            return client, SimpleNamespace(agent_role="agent_team")

        monkeypatch.setattr(runtime, "create_agent_team_client", create)
        return requests

    monkeypatch.setattr(runtime, "skills_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(registry, "skills_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(
        registry,
        "get_agent_team_network_policy",
        AsyncMock(return_value=AgentTeamNetworkPolicy.OFFLINE),
    )
    monkeypatch.setattr(runtime, "_publish_ai_request", AsyncMock())
    return install


async def manager_for(checkpoint, tmp_path, *, concurrency=2):
    from backend.services.agent_team.subagents import SubagentManager

    parent = await checkpoint.create_session(1, "agent")
    ctx = ToolContext(str(tmp_path), AgentTeamWorkspaceService(tmp_path))
    manager = SubagentManager(checkpoint, parent.id, ctx, concurrency=concurrency)
    ctx.subagents = manager
    ctx.executor = create_executor()
    await manager.start()
    return manager, ctx, parent.id


async def spawn_recorded(manager, checkpoint, parent_id, ident, task="inspect"):
    await recorded_spawn(checkpoint, parent_id, ident, task)
    return await manager.spawn(ident, task)


@pytest.mark.asyncio
async def test_live_slots_queue_without_lifetime_spawn_or_model_caps(
    persistence, tmp_path, model_factory
):
    checkpoint, engine = persistence
    release, two_started = asyncio.Event(), asyncio.Event()
    active = peak = started = 0

    async def model(**kwargs):
        nonlocal active, peak, started
        active += 1
        started += 1
        peak = max(peak, active)
        if started >= 2:
            two_started.set()
        try:
            await release.wait()
            return response(call("finish_task", summary="observed"))
        finally:
            active -= 1

    model_factory(model)
    manager, _, parent_id = await manager_for(checkpoint, tmp_path)
    try:
        children = [
            await spawn_recorded(
                manager, checkpoint, parent_id, f"spawn-{i}", f"task-{i}"
            )
            for i in range(3)
        ]
        await asyncio.wait_for(two_started.wait(), 2)
        with Session(engine) as db:
            states = [
                db.get(models.AgentTeamSession, child["agent_id"]).status
                for child in children
            ]
        assert states.count("running") == 2 and states.count("queued") == 1
        release.set()
        for child in children:
            outcome = await asyncio.wait_for(manager.wait(child["agent_id"]), 2)
            assert outcome["status"] == "completed" and outcome["result"]["success"]
        for index in range(12):
            child = await spawn_recorded(
                manager, checkpoint, parent_id, f"next-{index}"
            )
            assert (await asyncio.wait_for(manager.wait(child["agent_id"]), 2))[
                "status"
            ] == "completed"
        assert started == 15 and peak == 2
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_independent_child_context_cannot_write_even_when_model_requests_it(
    persistence, tmp_path, model_factory
):
    checkpoint, _ = persistence
    (tmp_path / "evidence.txt").write_text("evidence")

    async def model(messages, tools, **kwargs):
        names = {schema["function"]["name"] for schema in tools}
        assert "read_file" in names and "finish_task" in names
        assert not names & {"write_file", "run_command", "spawn_agent", "use_skill"}
        results = [m for m in messages if m["role"] == "tool"]
        if not results:
            return response(
                call(
                    "write_file",
                    "unsafe",
                    file_path="evidence.txt",
                    content="overwritten",
                )
            )
        if len(results) == 1:
            assert (
                json.loads(results[0]["content"])["error_code"]
                == "SUBAGENT_TOOL_RESTRICTED"
            )
            return response(call("read_file", "read", file_path="evidence.txt"))
        return response(call("finish_task", "finish", summary="evidence observed"))

    requests = model_factory(model)
    manager, _, parent_id = await manager_for(checkpoint, tmp_path)
    await checkpoint.append_message(
        parent_id, {"role": "user", "content": "PARENT-PRIVATE-CONTEXT"}
    )
    try:
        first = await spawn_recorded(
            manager, checkpoint, parent_id, "first", "scope-one"
        )
        second = await spawn_recorded(
            manager, checkpoint, parent_id, "second", "scope-two"
        )
        outputs = await asyncio.gather(
            manager.wait(first["agent_id"]), manager.wait(second["agent_id"])
        )
        assert all(
            output["result"]["summary"] == "evidence observed" for output in outputs
        )
        assert (tmp_path / "evidence.txt").read_text() == "evidence"
        assert all(
            "PARENT-PRIVATE-CONTEXT" not in str(request["messages"])
            for request in requests
        )
        assert all(
            not (
                "scope-one" in str(request["messages"])
                and "scope-two" in str(request["messages"])
            )
            for request in requests
        )
        assert (await checkpoint.load_messages(first["agent_id"])) != (
            await checkpoint.load_messages(second["agent_id"])
        )
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_cancel_active_and_queued_children_drains_model_and_records_result(
    persistence, tmp_path, model_factory
):
    checkpoint, engine = persistence
    started, drained = asyncio.Event(), asyncio.Event()

    async def model(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            drained.set()

    model_factory(model)
    manager, _, parent_id = await manager_for(checkpoint, tmp_path, concurrency=1)
    first = await spawn_recorded(manager, checkpoint, parent_id, "first")
    second = await spawn_recorded(manager, checkpoint, parent_id, "second")
    await asyncio.wait_for(started.wait(), 2)
    assert (await manager.cancel(second["agent_id"]))["status"] == "cancelled"
    result = await asyncio.wait_for(manager.cancel(first["agent_id"]), 2)
    assert drained.is_set() and result["result"]["error"] == "cancelled"
    await manager.close()
    with Session(engine) as db:
        assert all(
            db.get(models.AgentTeamSession, child["agent_id"]).status == "cancelled"
            for child in (first, second)
        )


@pytest.mark.asyncio
async def test_child_api_failure_is_structured_and_does_not_fail_sibling(
    persistence, tmp_path, model_factory
):
    checkpoint, _ = persistence

    async def model(messages, **kwargs):
        if "fail-investigation" in str(messages):
            raise RuntimeError("provider secret-token must not enter result")
        return response(call("finish_task", summary="valid evidence"))

    model_factory(model)
    manager, _, parent_id = await manager_for(checkpoint, tmp_path)
    try:
        failed = await spawn_recorded(
            manager, checkpoint, parent_id, "failure", "fail-investigation"
        )
        succeeded = await spawn_recorded(manager, checkpoint, parent_id, "success")
        failure = await manager.wait(failed["agent_id"])
        success = await manager.wait(succeeded["agent_id"])
        assert (
            failure["status"] == "unrecoverable_error"
            and failure["result"]["success"] is False
        )
        assert "secret-token" not in str(failure)
        assert success["status"] == "completed"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_wait_tool_releases_workspace_for_parent_write_and_child_read(
    persistence, tmp_path, model_factory
):
    from backend.services.agent_team.tool_scheduler import workspace_barrier

    checkpoint, _ = persistence
    release, entered = asyncio.Event(), asyncio.Event()
    (tmp_path / "evidence.txt").write_text("before")

    async def model(messages, **kwargs):
        if not any(m["role"] == "tool" for m in messages):
            entered.set()
            await release.wait()
            return response(call("read_file", file_path="evidence.txt"))
        return response(
            call("finish_task", "finish", summary="read after parent write")
        )

    model_factory(model)
    manager, ctx, parent_id = await manager_for(checkpoint, tmp_path)
    waiting = None
    try:
        child = await spawn_recorded(manager, checkpoint, parent_id, "spawn")
        await asyncio.wait_for(entered.wait(), 2)
        waiting = asyncio.create_task(
            ctx.executor.execute_raw("wait_agent", {"agent_id": child["agent_id"]}, ctx)
        )
        await asyncio.sleep(0)
        barrier = workspace_barrier(str(tmp_path))
        assert barrier.readers == 0 and not barrier.writer
        writer = await asyncio.wait_for(
            ctx.executor.execute_raw(
                "write_file", {"file_path": "new.txt", "content": "parent write"}, ctx
            ),
            2,
        )
        assert writer.success
        release.set()
        result = await asyncio.wait_for(waiting, 2)
        assert result.success and result.output["status"] == "completed"
    finally:
        release.set()
        if waiting:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_cancelling_child_before_its_coroutine_starts_still_persists_cancellation(
    persistence, tmp_path, model_factory
):
    checkpoint, _ = persistence

    async def model(**kwargs):
        await asyncio.Event().wait()

    model_factory(model)
    manager, _, parent_id = await manager_for(checkpoint, tmp_path, concurrency=1)
    try:
        child = await spawn_recorded(manager, checkpoint, parent_id, "spawn")
        # Give the slot worker one turn to allocate a child task. The child
        # coroutine is queued behind this task and has not entered its try yet.
        await asyncio.sleep(0)
        result = await asyncio.wait_for(manager.cancel(child["agent_id"]), 2)
        assert (
            result["status"] == "cancelled" and result["result"]["error"] == "cancelled"
        )
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_resume_child_from_raw_session_preserves_scope_and_never_writes(
    persistence, tmp_path, model_factory
):
    from backend.services.agent_team.subagents import SubagentStore

    checkpoint, _ = persistence
    (tmp_path / "evidence.txt").write_text("original")
    parent = await checkpoint.create_session(1, "agent")
    await recorded_spawn(checkpoint, parent.id)
    child = await SubagentStore(1).create(
        parent.id,
        "spawn",
        "Inspect evidence",
        {"narrow": SkillRestriction.from_metadata(["read_file", "spawn_agent"])},
    )
    unsafe = call("write_file", "unsafe", file_path="evidence.txt", content="changed")
    await checkpoint.append_message(
        child.session_id,
        {"role": "assistant", "tool_calls": [tool_call_to_dict(unsafe)]},
    )

    async def model(messages, tools, **kwargs):
        assert {s["function"]["name"] for s in tools} == {"read_file", "finish_task"}
        assert (
            json.loads(next(m["content"] for m in messages if m["role"] == "tool"))[
                "error_code"
            ]
            == "SUBAGENT_TOOL_RESTRICTED"
        )
        return response(call("finish_task", "finish", summary="read-only resumed"))

    model_factory(model)
    agent = runtime.FullStackExpertAgent(
        tmp_path,
        AgentTeamWorkspaceService(tmp_path),
        checkpoint,
        child.session_id,
        await checkpoint.load_messages(child.session_id),
    )
    result = await agent.execute("resumed", "Inspect evidence")
    assert result.success and (tmp_path / "evidence.txt").read_text() == "original"


@pytest.mark.asyncio
async def test_resume_reuses_inflight_child_and_replays_same_spawn_identity(
    persistence, tmp_path, model_factory
):
    from backend.services.agent_team.subagents import SubagentManager, SubagentStore

    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    await recorded_spawn(checkpoint, parent.id)
    child = await SubagentStore(1).create(parent.id, "spawn", "Inspect evidence", {})
    await SubagentStore(1).set_active_status(child.session_id, parent.id, "running")
    # Simulate durable finish before a process exits, leaving the child session
    # and parent spawn call running. Recovery must not make another model call.
    finish = call("finish_task", "finish", summary="durable findings")
    await checkpoint.append_message(
        child.session_id,
        {"role": "assistant", "tool_calls": [tool_call_to_dict(finish)]},
    )
    await checkpoint.record_tool_result(
        child.session_id,
        "finish",
        {
            "role": "tool",
            "tool_call_id": "finish",
            "content": json.dumps(
                {
                    "_terminal": True,
                    "summary": "durable findings",
                    "modified_files": [],
                    "risk_level": "medium",
                    "test_result": "",
                }
            ),
        },
        "completed",
    )

    async def model(**kwargs):
        pytest.fail("Durable child finish must not invoke the model on resume")

    model_factory(model)
    ctx = ToolContext(str(tmp_path), AgentTeamWorkspaceService(tmp_path))
    manager = SubagentManager(checkpoint, parent.id, ctx, concurrency=1)
    try:
        await manager.start()
        replay = await manager.spawn("spawn", "Inspect evidence")
        assert replay["agent_id"] == child.session_id
        result = await asyncio.wait_for(manager.wait(child.session_id), 2)
        assert result["result"]["summary"] == "durable findings"
        with Session(engine) as db:
            assert len(list(db.scalars(select(models.AgentTeamSubagent)))) == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "finish", "error"])
async def test_parent_ending_cancels_and_awaits_actual_children(
    persistence, tmp_path, model_factory, monkeypatch, ending
):
    from backend.core import config

    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    child_entered, child_drained = asyncio.Event(), asyncio.Event()

    async def model(messages, **kwargs):
        if messages[0]["content"].startswith("You are Sakura's read-only subagent"):
            child_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                child_drained.set()
        outputs = [m for m in messages if m["role"] == "tool"]
        if not outputs:
            return response(call("spawn_agent", "spawn", task="inspect evidence"))
        await child_entered.wait()
        if ending == "finish":
            return response(call("finish_task", "finish", summary="parent done"))
        if ending == "error":
            raise RuntimeError("upstream API unavailable")
        child_id = json.loads(outputs[-1]["content"])["agent_id"]
        return response(call("wait_agent", "wait", agent_id=child_id))

    model_factory(model)
    original = config.get_dynamic_config_fresh

    async def setting(key):
        return 2 if key == "agent_team_subagent_concurrency" else await original(key)

    monkeypatch.setattr(config, "get_dynamic_config_fresh", setting)
    agent = runtime.FullStackExpertAgent(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint, parent.id
    )
    running = asyncio.create_task(agent.execute("parent", "main objective"))
    await asyncio.wait_for(child_entered.wait(), 2)
    if ending == "cancel":
        running.cancel()
    if ending == "error":
        with pytest.raises(RuntimeError, match="upstream"):
            await asyncio.wait_for(running, 2)
    elif ending == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 2)
        assert running.cancelled()
    else:
        result = await asyncio.wait_for(running, 2)
        assert result.outcome == "success"
    assert child_drained.is_set()
    with Session(engine) as db:
        children = list(
            db.scalars(
                select(models.AgentTeamSession).where(
                    models.AgentTeamSession.role_name == "subagent"
                )
            )
        )
        assert len(children) == 1 and children[0].status == "cancelled"
        assert json.loads(children[0].result_payload)["success"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("child_run", [False, True])
async def test_compaction_audit_is_durable_before_main_or_child_model_request(
    persistence, tmp_path, model_factory, monkeypatch, child_run
):
    from backend.services.agent_team import context_compressor as compression
    from backend.services.agent_team.subagents import SubagentStore

    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    session_id = parent.id
    if child_run:
        await recorded_spawn(checkpoint, parent.id)
        session_id = (
            await SubagentStore(1).create(parent.id, "spawn", "Inspect evidence", {})
        ).session_id
    candidate = SimpleNamespace(model=SimpleNamespace(context_window_tokens=100000))
    compressors = []
    closed = []
    original_factory = compression.AgentContextCompressor.from_settings

    def build(**kwargs):
        compressor = original_factory(**kwargs)
        compressors.append(compressor)

        async def maybe_compress(candidate, messages, **kwargs):
            await compressor._record_audit(
                {
                    "version": 1,
                    "event_type": "context_compaction",
                    "session_id": session_id,
                    "estimated_tokens_before": 200,
                    "estimated_tokens_after": 100,
                }
            )
            return False, messages

        async def close():
            closed.append(compressor)

        monkeypatch.setattr(compressor, "maybe_compress", maybe_compress)
        monkeypatch.setattr(compressor, "aclose", close)
        return compressor

    monkeypatch.setattr(compression.AgentContextCompressor, "from_settings", build)

    async def model(messages, **kwargs):
        with Session(engine) as db:
            audits = [
                json.loads(row.message_json)
                for row in db.scalars(
                    select(models.AgentTeamMessage).where(
                        models.AgentTeamMessage.session_id == session_id
                    )
                )
                if "context_compaction" in row.message_json
            ]
        assert (
            len(audits) == 1
            and audits[0]["metadata"]["context_compaction"]["estimated_tokens_before"]
            == 200
        )
        assert not any(
            m.get("metadata", {}).get("context_compaction") for m in messages
        )
        return response(call("finish_task", summary="audited request"))

    model_factory(model)

    async def create(*, compressor):
        assert compressor is compressors[0]
        return SimpleNamespace(
            resolve_role_primary_candidate=AsyncMock(return_value=candidate),
            call_with_retry=model,
        ), SimpleNamespace(agent_role="agent_team")

    monkeypatch.setattr(runtime, "create_agent_team_client", create)
    agent = runtime.FullStackExpertAgent(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint, session_id
    )
    result = await agent.execute("task", "Inspect evidence")
    assert result.success and closed == compressors
    assert not any(
        m.get("metadata", {}).get("context_compaction") for m in agent.messages
    )


def test_compaction_rows_are_neither_user_guidance_nor_projected_model_context(
    tmp_path,
):
    agent = runtime.FullStackExpertAgent(tmp_path, AgentTeamWorkspaceService(tmp_path))
    audit = {
        "role": "user",
        "content": "",
        "metadata": {"context_compaction": {"version": 1}, "guidance_ids": [99]},
    }
    assert not agent._is_guidance_message(audit)
    agent.messages.append(audit)
    assert agent._project_model_messages(agent._build_context()) == [agent.messages[0]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,content",
    [
        ("requirements.txt", "fastapi==0.100.0"),
        ("package.json", '{"dependencies":{"react":"private"}}'),
    ],
)
@pytest.mark.parametrize("link_type", ["symlink", "hardlink"])
async def test_delegated_project_detection_cannot_read_linked_host_dependencies(
    tmp_path, filename, content, link_type
):
    import os

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "host-secret"
    secret.write_text(content)
    if link_type == "symlink":
        (workspace / filename).symlink_to(secret)
    else:
        os.link(secret, workspace / filename)
    ctx = ToolContext(str(workspace), AgentTeamWorkspaceService(workspace))
    result = await create_executor(read_only=True).execute_raw(
        "detect_project", {}, ctx
    )
    assert not result.success and "frameworks" not in result.output


@pytest.mark.asyncio
async def test_cancellation_after_durable_child_completion_keeps_completed_result(
    persistence, tmp_path, model_factory, monkeypatch
):
    checkpoint, _ = persistence
    published, release = asyncio.Event(), asyncio.Event()

    async def model(**kwargs):
        return response(call("finish_task", summary="durable completed result"))

    model_factory(model)

    async def publish(event, payload):
        if event == "agent:session_completed" and payload.get("status") == "completed":
            published.set()
            await release.wait()

    monkeypatch.setattr(checkpoint_module, "_publish", publish)
    manager, _, parent_id = await manager_for(checkpoint, tmp_path)
    try:
        child = await spawn_recorded(manager, checkpoint, parent_id, "spawn")
        await asyncio.wait_for(published.wait(), 2)
        cancellation = asyncio.create_task(manager.cancel(child["agent_id"]))
        await asyncio.sleep(0)
        release.set()
        result = await asyncio.wait_for(cancellation, 2)
        assert result["status"] == "completed" and result["result"]["success"] is True
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_spawn_mapping_insert_failure_rolls_back_child_session(persistence):
    from sqlalchemy import event

    from backend.services.agent_team.subagents import SubagentStore

    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    await recorded_spawn(checkpoint, parent.id)

    def fail_mapping_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO agent_team_subagents"):
            raise RuntimeError("mapping insert interrupted")

    event.listen(engine, "before_cursor_execute", fail_mapping_insert)
    try:
        with pytest.raises(RuntimeError, match="mapping insert"):
            await SubagentStore(1).create(parent.id, "spawn", "Inspect evidence", {})
    finally:
        event.remove(engine, "before_cursor_execute", fail_mapping_insert)
    with Session(engine) as db:
        assert [row.id for row in db.scalars(select(models.AgentTeamSession))] == [
            parent.id
        ]
        assert not list(db.scalars(select(models.AgentTeamSubagent)))


@pytest.mark.asyncio
async def test_child_repeated_text_self_check_never_terminates_model_rounds(
    persistence, tmp_path, model_factory
):
    checkpoint, _ = persistence

    async def model(messages, **kwargs):
        rounds = sum(message["role"] == "assistant" for message in messages)
        if rounds < 12:
            return response(text="Still investigating the evidence")
        return response(call("finish_task", summary="eventual explicit completion"))

    model_factory(model)
    manager, _, parent_id = await manager_for(checkpoint, tmp_path)
    try:
        child = await spawn_recorded(manager, checkpoint, parent_id, "spawn")
        result = await asyncio.wait_for(manager.wait(child["agent_id"]), 2)
        assert (
            result["status"] == "completed"
            and result["result"]["summary"] == "eventual explicit completion"
        )
        messages = await checkpoint.load_messages(child["agent_id"])
        assert sum(message["role"] == "assistant" for message in messages) == 13
        assert (
            sum(
                bool(message.get("metadata", {}).get("strategy_self_check"))
                for message in messages
            )
            == 1
        )
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_failed_child_checkpoint_is_observable_and_wait_never_restarts_work(
    persistence, tmp_path, model_factory, monkeypatch
):
    checkpoint, _ = persistence
    model_calls = []

    async def model(**kwargs):
        model_calls.append("request")
        return response(call("finish_task", summary="observed"))

    model_factory(model)
    manager, _, parent_id = await manager_for(checkpoint, tmp_path)
    finish = checkpoint.finish_session

    async def fail_commit(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(checkpoint, "finish_session", fail_commit)
    try:
        child = await spawn_recorded(manager, checkpoint, parent_id, "spawn")
        for _ in range(2):
            with pytest.raises(RuntimeError, match="checkpoint persistence failed"):
                await manager.wait(child["agent_id"])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert model_calls == ["request"]
    finally:
        monkeypatch.setattr(checkpoint, "finish_session", finish)
        # Restored persistence permits explicit cancellation and safe shutdown;
        # the two wait failures above remain visible to the caller.
        await manager.close()


@pytest.mark.asyncio
async def test_shutdown_never_starts_queued_model_while_another_child_drains(
    persistence, tmp_path, model_factory, monkeypatch
):
    checkpoint, _ = persistence
    both_running, first_draining = asyncio.Event(), asyncio.Event()
    finish_second, release_first = asyncio.Event(), asyncio.Event()
    third_created = asyncio.Event()
    model_entries = []

    async def model(messages, **kwargs):
        model_entries.append(messages)
        if len(model_entries) == 2:
            both_running.set()
        if "first-child" in str(messages):
            try:
                await asyncio.Event().wait()
            finally:
                first_draining.set()
                await release_first.wait()
        await finish_second.wait()
        return response(call("finish_task", summary="read evidence"))

    model_factory(model)
    original = runtime.create_agent_team_client
    created = 0

    async def create(**kwargs):
        nonlocal created
        created += 1
        if created > 2:
            third_created.set()
        return await original(**kwargs)

    monkeypatch.setattr(runtime, "create_agent_team_client", create)
    manager, _, parent_id = await manager_for(checkpoint, tmp_path)
    closing = None
    try:
        for ident in ("first-child", "second-child", "queued-child"):
            await spawn_recorded(manager, checkpoint, parent_id, ident, ident)
        await asyncio.wait_for(both_running.wait(), 2)
        closing = asyncio.create_task(manager.close())
        await asyncio.wait_for(first_draining.wait(), 2)
        finish_second.set()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(third_created.wait(), 0.1)
    finally:
        finish_second.set()
        release_first.set()
        if closing:
            await asyncio.wait_for(closing, 2)
        else:
            await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["wait", "cancel", "close"])
async def test_owned_child_cleanup_survives_spawn_permission_revocation(
    persistence, tmp_path, model_factory, cleanup
):
    from backend.services.agent_team.capability_policy import (
        CapabilitySession,
        PolicySnapshot,
    )
    from backend.services.agent_team.tools.registry import get_tool_definitions_fresh

    checkpoint, _ = persistence
    entered, release = asyncio.Event(), asyncio.Event()

    async def model(**kwargs):
        entered.set()
        await release.wait()
        return response(call("finish_task", summary="observed"))

    model_factory(model)
    manager, ctx, parent_id = await manager_for(checkpoint, tmp_path)
    profile = "autonomous"

    async def policy():
        return PolicySnapshot(profile, "web_tools")

    capabilities = CapabilitySession(policy)
    ctx.executor.bind_runtime(capabilities, None)
    try:
        await recorded_spawn(checkpoint, parent_id, "start", "inspect")
        created = await ctx.executor.execute_tool_call(
            call("spawn_agent", "start", task="inspect"), ctx
        )
        assert created.success
        child_id = created.output["agent_id"]
        await asyncio.wait_for(entered.wait(), 2)
        profile = "read_only"
        names = {
            schema["function"]["name"]
            for schema in await get_tool_definitions_fresh(ctx=ctx)
        }
        assert "spawn_agent" not in names and {"wait_agent", "cancel_agent"} <= names
        denied = await ctx.executor.execute_tool_call(
            call("spawn_agent", "denied", task="inspect"), ctx
        )
        assert denied.error_code == "CAPABILITY_DENIED"
        if cleanup == "close":
            # Parent cleanup is runtime-owned, even after capability close.
            await capabilities.close()
            await manager.close()
            assert (await manager.store.get(child_id, parent_id)).status == "cancelled"
        else:
            if cleanup == "wait":
                release.set()
            result = await asyncio.wait_for(
                ctx.executor.execute_raw(
                    f"{cleanup}_agent", {"agent_id": child_id}, ctx
                ),
                2,
            )
            assert result.success and result.output["status"] == (
                "completed" if cleanup == "wait" else "cancelled"
            )
        assert not manager._active
    finally:
        release.set()
        await manager.close()
