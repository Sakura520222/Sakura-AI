"""Provider usage receipts and task accounting share one durable transaction."""

import asyncio
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import agent_team_models as models
from backend.services.agent_team import conversation_checkpoint as checkpoint_module
from backend.services.agent_team import fullstack_expert as runtime
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from tests import test_agent_compaction_audit as compression_fixtures
from tests import test_agent_subagents as subagent_fixtures
from tests.test_agent_subagents import call, recorded_spawn, response

persistence = subagent_fixtures.persistence
model_factory = subagent_fixtures.model_factory
provider = compression_fixtures.provider


def usage(prompt=1000, completion=200):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


def totals(engine):
    with Session(engine) as db:
        task = db.get(models.AgentTeamTask, 1)
        return task.prompt_tokens, task.completion_tokens, task.estimated_cost


@pytest.mark.asyncio
async def test_receipt_retry_preserves_existing_totals_and_rolls_back_together(
    persistence, monkeypatch
):
    from backend.core.config import get_settings

    monkeypatch.setattr(get_settings(), "review_price_per_1k_prompt", 1.0)
    monkeypatch.setattr(get_settings(), "review_price_per_1k_completion", 2.0)
    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    with Session(engine) as db:
        task = db.get(models.AgentTeamTask, 1)
        task.prompt_tokens, task.completion_tokens, task.estimated_cost = 77, 33, 15
        db.commit()
    await checkpoint.record_usage(parent.id, "request-1", usage())
    first = totals(engine)
    assert first == (1077, 233, 155)
    await checkpoint.record_usage(parent.id, "request-1", usage())
    assert totals(engine) == first

    adapter = checkpoint_module.db_module.async_session
    original = adapter.commit

    async def failed_commit(self):
        await self.flush()
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(adapter, "commit", failed_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await checkpoint.record_usage(parent.id, "request-2", usage(4, 5))
    assert totals(engine) == first
    monkeypatch.setattr(adapter, "commit", original)
    await checkpoint.record_usage(parent.id, "request-2", usage(4, 5))
    assert totals(engine)[:2] == (1081, 238)
    with Session(engine) as db:
        assert len(db.scalars(select(models.AgentTeamUsage)).all()) == 2


@pytest.mark.asyncio
async def test_uncertain_successful_commit_retry_does_not_repeat_increment(
    persistence, monkeypatch
):
    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    adapter = checkpoint_module.db_module.async_session
    original = adapter.commit

    async def committed_but_disconnected(self):
        await original(self)
        raise RuntimeError("connection lost after commit")

    monkeypatch.setattr(adapter, "commit", committed_but_disconnected)
    with pytest.raises(RuntimeError, match="after commit"):
        await checkpoint.record_usage(parent.id, "uncertain", usage(41, 13))
    assert totals(engine)[:2] == (41, 13)
    monkeypatch.setattr(adapter, "commit", original)
    await checkpoint.record_usage(parent.id, "uncertain", usage(41, 13))
    assert totals(engine)[:2] == (41, 13)
    with Session(engine) as db:
        assert len(db.scalars(select(models.AgentTeamUsage)).all()) == 1


@pytest.mark.asyncio
async def test_usage_durable_before_tools_and_resume_does_not_double_count(
    persistence, model_factory, tmp_path
):
    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")

    async def model(**kwargs):
        result = response(call("finish_task", summary="done"))
        result.usage = usage(17, 9)
        return result

    model_factory(model)
    service = AgentTeamWorkspaceService(tmp_path)
    agent = runtime.FullStackExpertAgent(tmp_path, service, checkpoint, parent.id)
    result = await agent.execute("title", "summary")
    assert result.success
    assert totals(engine)[:2] == (17, 9)
    resumed = runtime.FullStackExpertAgent(
        tmp_path,
        service,
        checkpoint,
        parent.id,
        await checkpoint.load_messages(parent.id),
    )
    result = await resumed.execute("title", "summary")
    assert result.success
    assert (result.prompt_tokens, result.completion_tokens) == (17, 9)
    assert totals(engine)[:2] == (17, 9)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["running", "success", "cancelled", "unrecoverable_error"]
)
async def test_parent_recovers_child_usage_in_any_state_and_iterations_add_once(
    persistence, status
):
    from backend.services.agent_team.subagents import SubagentStore

    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    await recorded_spawn(checkpoint, parent.id)
    child = await SubagentStore(1).create(parent.id, "spawn", "Inspect evidence", {})
    await checkpoint.record_usage(parent.id, "parent-request", usage(10, 2))
    await checkpoint.record_usage(child.session_id, "child-request", usage(20, 4))
    if status != "running":
        await checkpoint.finish_session(
            child.session_id, status, {"success": status == "success"}
        )
    # Simulated parent crash: a new service reads all committed child evidence.
    resumed = checkpoint_module.ConversationCheckpointService(1)
    assert await resumed.load_usage(parent.id) == (30, 6)
    await resumed.finish_session(
        parent.id,
        "cancelled",
        {
            "success": False,
            "prompt_tokens": 999999,
            "completion_tokens": 999999,
        },
    )
    saved = await resumed.load_session_result(parent.id)
    assert (saved["prompt_tokens"], saved["completion_tokens"]) == (30, 6)
    await resumed.finish_session(parent.id, "cancelled", {"success": False})
    iteration = await resumed.create_session(2, "agent")
    await resumed.record_usage(iteration.id, "iteration-request", usage(7, 3))
    assert await resumed.load_usage(iteration.id) == (7, 3)
    assert totals(engine)[:2] == (37, 9)


@pytest.mark.asyncio
async def test_worker_state_updates_cannot_overwrite_durable_usage(persistence):
    from backend.workers.agent_team_worker import AgentTeamWorker

    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    await checkpoint.record_usage(parent.id, "request", usage(1000, 200))
    before = totals(engine)
    worker = AgentTeamWorker()
    await worker._update_task(
        1, prompt_tokens=5, completion_tokens=6, estimated_cost=999
    )
    assert totals(engine) == before


@pytest.mark.asyncio
async def test_missing_and_partially_reported_usage_never_estimated(persistence):
    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    await checkpoint.record_usage(parent.id, "absent", None)
    partial = SimpleNamespace(
        input_tokens=999,
        output_tokens=7,
        prompt_tokens=999,
        completion_tokens=7,
        reported_fields={"output_tokens"},
    )
    await checkpoint.record_usage(parent.id, "partial", partial)
    assert totals(engine)[:2] == (0, 7)
    with Session(engine) as db:
        assert db.get(models.AgentTeamUsage, "absent").prompt_tokens is None
        assert db.get(models.AgentTeamUsage, "partial").prompt_tokens is None


@pytest.mark.asyncio
async def test_failed_usage_commit_prevents_model_selected_write(
    persistence, model_factory, tmp_path, monkeypatch
):
    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")

    async def model(**kwargs):
        result = response(call("write_file", file_path="written.txt", content="bad"))
        result.usage = usage(11, 3)
        return result

    model_factory(model)
    adapter = checkpoint_module.db_module.async_session
    original = adapter.commit

    async def failed_usage_commit(self):
        if any(isinstance(row, models.AgentTeamUsage) for row in self.db.new):
            await self.flush()
            raise RuntimeError("usage persistence failed")
        await original(self)

    monkeypatch.setattr(adapter, "commit", failed_usage_commit)
    agent = runtime.FullStackExpertAgent(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint, parent.id
    )
    with pytest.raises(RuntimeError, match="usage persistence failed"):
        await agent.execute("title", "summary")
    assert not (tmp_path / "written.txt").exists()
    assert totals(engine)[:2] == (0, 0)
    with Session(engine) as db:
        assert not db.scalars(select(models.AgentTeamUsage)).all()


@pytest.mark.asyncio
async def test_cancel_during_usage_commit_drains_receipt_and_reports_known_work(
    persistence, model_factory, tmp_path, monkeypatch
):
    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    committing, release = asyncio.Event(), asyncio.Event()

    async def model(**kwargs):
        result = response(call("write_file", file_path="written.txt", content="bad"))
        result.usage = usage(13, 5)
        return result

    model_factory(model)
    adapter = checkpoint_module.db_module.async_session
    original = adapter.commit

    async def pause_usage_commit(self):
        if any(isinstance(row, models.AgentTeamUsage) for row in self.db.new):
            committing.set()
            await release.wait()
        await original(self)

    monkeypatch.setattr(adapter, "commit", pause_usage_commit)
    agent = runtime.FullStackExpertAgent(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint, parent.id
    )
    running = asyncio.create_task(agent.execute("title", "summary"))
    await committing.wait()
    running.cancel()
    await asyncio.sleep(0)
    assert not running.done()
    release.set()
    result = await running
    assert result.outcome == "cancelled"
    assert (result.prompt_tokens, result.completion_tokens) == (13, 5)
    assert totals(engine)[:2] == (13, 5)
    assert not (tmp_path / "written.txt").exists()


@pytest.mark.asyncio
async def test_parent_runtime_aggregates_already_finished_child_without_replaying(
    persistence, model_factory, tmp_path
):
    from backend.services.agent_team.subagents import SubagentStore

    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    await recorded_spawn(checkpoint, parent.id)
    child = await SubagentStore(1).create(parent.id, "spawn", "Inspect evidence", {})
    await checkpoint.record_usage(child.session_id, "old-child", usage(10, 8))
    await checkpoint.finish_session(
        child.session_id,
        "success",
        {"summary": "done", "success": True, "outcome": "success"},
    )

    async def model(**kwargs):
        result = response(call("finish_task", summary="parent done"))
        result.usage = usage(7, 2)
        return result

    requests = model_factory(model)
    agent = runtime.FullStackExpertAgent(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint, parent.id
    )
    result = await agent.execute("parent", "task")
    assert result.success
    assert len(requests) == 1
    assert (result.prompt_tokens, result.completion_tokens) == (17, 10)
    assert totals(engine)[:2] == (17, 10)


@pytest.mark.asyncio
async def test_failed_provider_after_known_work_keeps_usage(
    persistence, model_factory, tmp_path
):
    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    called = False

    async def model(**kwargs):
        nonlocal called
        if called:
            raise RuntimeError("provider unavailable")
        called = True
        result = response(text="continue")
        result.usage = usage(19, 6)
        return result

    model_factory(model)
    agent = runtime.FullStackExpertAgent(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint, parent.id
    )
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await agent.execute("parent", "task")
    assert totals(engine)[:2] == (19, 6)


@pytest.mark.asyncio
async def test_receipts_validate_task_ownership_and_replay_identity(persistence):
    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    other = checkpoint_module.ConversationCheckpointService(2)
    with pytest.raises(ValueError, match="belong"):
        await other.record_usage(parent.id, "forged", usage())
    await checkpoint.record_usage(parent.id, "stable", usage(8, 2))
    with pytest.raises(ValueError, match="changed"):
        await checkpoint.record_usage(parent.id, "stable", usage(9, 2))
    assert totals(engine)[:2] == (8, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancelled", "unrecoverable_error", "success"])
async def test_child_runtime_preserves_prior_usage_through_termination(
    persistence, model_factory, tmp_path, ending
):
    checkpoint, engine = persistence
    waiting = asyncio.Event()
    called = False

    async def model(**kwargs):
        nonlocal called
        if called:
            waiting.set()
            if ending == "cancelled":
                await asyncio.Event().wait()
            if ending == "unrecoverable_error":
                raise RuntimeError("provider unavailable")
            return response(call("finish_task", summary="done"))
        called = True
        result = response(text="investigating")
        result.usage = usage(29, 11)
        return result

    model_factory(model)
    manager, _, parent_id = await subagent_fixtures.manager_for(checkpoint, tmp_path)
    try:
        child = await subagent_fixtures.spawn_recorded(
            manager, checkpoint, parent_id, "spawn"
        )
        await waiting.wait()
        if ending == "cancelled":
            result = await manager.cancel(child["agent_id"])
        else:
            result = await manager.wait(child["agent_id"])
        assert result["result"]["outcome"] == ending
        assert result["result"]["prompt_tokens"] == 29
        assert result["result"]["completion_tokens"] == 11
        assert totals(engine)[:2] == (29, 11)
        assert await checkpoint.load_usage(parent_id) == (29, 11)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_pr_accounting_reads_latest_receipts_instead_of_adding_result_again(
    persistence,
):
    from backend.workers.agent_team_worker import AgentTeamWorker

    checkpoint, engine = persistence
    worker = AgentTeamWorker()
    old_snapshot = await worker._load_task(1)
    parent = await checkpoint.create_session(1, "agent")
    await checkpoint.record_usage(parent.id, "once", usage(31, 12))
    outcome = SimpleNamespace(iterations=1, prompt_tokens=31, completion_tokens=12)
    for _ in range(2):
        counts = await worker._accumulate_iteration_cost(old_snapshot, outcome)
        assert counts[:3] == (1, 31, 12)
    assert totals(engine)[:2] == (31, 12)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_summary", [False, True])
async def test_summary_usage_commits_before_audit_and_main_request(
    persistence, provider, tmp_path, monkeypatch, empty_summary
):
    from backend.services.agent_team.context_compressor import AgentContextCompressor
    from backend.services.ai_reviewer.unified_client import UnifiedAIClient

    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    agent = runtime.FullStackExpertAgent(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint, parent.id
    )
    chat = provider.chat

    async def reported_chat(*args, **kwargs):
        request = args[-1]
        if "compress" not in (request.messages[0].content or "").lower():
            assert totals(engine)[:2] == (8, 4)
            assert any(
                "context_compaction" in m.get("metadata", {})
                for m in await checkpoint.load_messages(parent.id)
            )
        result = await chat(*args, **kwargs)
        result.usage.reported_fields = frozenset({"input_tokens", "output_tokens"})
        if empty_summary and provider.main_calls == 0:
            result.content = ""
        return result

    monkeypatch.setattr(provider, "chat", reported_chat)
    compressor = AgentContextCompressor.from_settings(
        usage_callback=agent._persist_provider_usage,
        audit_callback=agent._persist_compaction_audit,
    )
    original = compression_fixtures.history()
    compressor.bind_source_messages(original)
    client = UnifiedAIClient(compressor=compressor)
    try:
        await client.call_with_retry(
            [compression_fixtures.candidate()], original, model="", role="agent_team"
        )
        # Only the summary passed through the callback in this direct client test.
        with Session(engine) as db:
            receipts = db.scalars(select(models.AgentTeamUsage)).all()
            assert len(receipts) == 1
            assert receipts[0].prompt_tokens == 8
        assert totals(engine)[:2] == (8, 4)
        assert any(
            "context_compaction" in m.get("metadata", {})
            for m in await checkpoint.load_messages(parent.id)
        )
    finally:
        await client.aclose()
        await compressor.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["preflight", "overflow", "stream", "fallback"])
async def test_summary_receipt_failure_aborts_inner_client_before_main_request(
    persistence, provider, tmp_path, monkeypatch, path
):
    from backend.services.agent_team.context_compressor import AgentContextCompressor
    from backend.services.ai_reviewer.unified_client import UnifiedAIClient

    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    agent = runtime.FullStackExpertAgent(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint, parent.id
    )

    async def fail(*args):
        raise RuntimeError("SECRET_DATABASE_URL")

    monkeypatch.setattr(checkpoint, "record_usage", fail)
    compressor = AgentContextCompressor.from_settings(
        usage_callback=agent._persist_provider_usage
    )
    original = compression_fixtures.history()
    candidates = [compression_fixtures.candidate()]
    if path in {"overflow", "fallback"}:
        candidates = [compression_fixtures.candidate(window=100000)]
        provider.overflow_once = path == "overflow"
        provider.fail_primary = path == "fallback"
        if path == "fallback":
            candidates.append(compression_fixtures.candidate("secondary"))
    compressor.bind_source_messages(original)
    client = UnifiedAIClient(compressor=compressor)
    try:
        with pytest.raises(RuntimeError, match="usage persistence failed") as error:
            if path == "stream":
                async for _ in client.stream_with_retry(
                    candidates, original, model="", role="agent_team"
                ):
                    pytest.fail("usage failure must precede streamed output")
            else:
                await client.call_with_retry(
                    candidates, original, model="", role="agent_team"
                )
        assert "SECRET_DATABASE_URL" not in str(error.value)
        assert "SECRET_DATABASE_URL" not in "".join(
            traceback.format_exception(error.value)
        )
        assert error.value.__cause__ is None
        assert error.value.__context__ is None or "SECRET_DATABASE_URL" not in str(
            error.value.__context__
        )
        assert provider.main_calls == (1 if path in {"overflow", "fallback"} else 0)
        assert totals(engine)[:2] == (0, 0)
    finally:
        await client.aclose()
        await compressor.aclose()


@pytest.mark.asyncio
async def test_fullstack_accounts_for_summary_and_main_responses_once(
    persistence, provider, model_factory, tmp_path, monkeypatch
):
    from backend.core.ai_protocol.models import UnifiedToolCall
    from backend.services.ai_reviewer.unified_client import UnifiedAIClient

    checkpoint, engine = persistence
    parent = await checkpoint.create_session(1, "agent")
    old_history = [
        {"role": "system", "content": "runtime policy"},
        {"role": "user", "content": "current task"},
        {"role": "assistant", "content": "old observation " * 4000},
    ]
    for message in old_history:
        await checkpoint.append_message(parent.id, message)
    chat = provider.chat

    async def reported_chat(*args, **kwargs):
        result = await chat(*args, **kwargs)
        result.usage.reported_fields = frozenset({"input_tokens", "output_tokens"})
        if provider.main_calls:
            # The Provider's terminal tool is the normal runtime finish path.
            result.tool_calls = [
                UnifiedToolCall(
                    id="finish", name="finish_task", arguments='{"summary":"done"}'
                )
            ]
        return result

    monkeypatch.setattr(provider, "chat", reported_chat)
    value = compression_fixtures.candidate(window=32000)
    clients = []

    async def create(**kwargs):
        client = UnifiedAIClient(compressor=kwargs["compressor"])
        clients.append(client)

        async def invoke(**request):
            return await client.call_with_retry([value], **request)

        return SimpleNamespace(
            resolve_role_primary_candidate=AsyncMock(return_value=value),
            call_with_retry=invoke,
        ), SimpleNamespace(agent_role="agent_team")

    monkeypatch.setattr(runtime, "create_agent_team_client", create)
    agent = runtime.FullStackExpertAgent(
        tmp_path,
        AgentTeamWorkspaceService(tmp_path),
        checkpoint,
        parent.id,
        old_history,
    )
    try:
        result = await agent.execute("title", "summary")
        assert result.success
        assert len(provider.requests) > 1  # At least one real summary and main call.
        with Session(engine) as db:
            receipts = db.scalars(select(models.AgentTeamUsage)).all()
            assert len(receipts) == len(provider.requests)
        assert (result.prompt_tokens, result.completion_tokens) == (
            8 * len(provider.requests),
            4 * len(provider.requests),
        )
        assert totals(engine)[:2] == (result.prompt_tokens, result.completion_tokens)
    finally:
        for client in clients:
            await client.aclose()
