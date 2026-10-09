"""Regression evidence for PR #661 cancellation and seed checkpoint findings."""

import asyncio
import json
from collections import OrderedDict
from types import SimpleNamespace

import pytest
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core import config
from backend.models import agent_team_models as models
from backend.services.agent_team import fullstack_expert as runtime
from backend.services.agent_team.iteration_loop import IterationLoopService
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from tests import test_agent_subagents as fixtures
from tests.test_agent_subagents import call, response

persistence = fixtures.persistence
model_factory = fixtures.model_factory


@pytest.fixture(autouse=True)
def isolate_dynamic_config(monkeypatch):
    monkeypatch.setattr(config, "_dynamic_config_cache", OrderedDict())


async def cancellation_scenario(
    checkpoint, tmp_path, model_factory, *, finish_before_cleanup=False
):
    """Keep lifecycle, child execution and durable checkpointing real."""
    parent_entered = asyncio.Event()
    child_entered = asyncio.Event()
    child_cleanup_entered = asyncio.Event()
    release_child_cleanup = asyncio.Event()
    child_drained = asyncio.Event()
    cancel_event = asyncio.Event()

    async def model(messages, **kwargs):
        if messages[0]["content"].startswith("You are Sakura's read-only subagent"):
            child_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                child_cleanup_entered.set()
                await release_child_cleanup.wait()
                child_drained.set()
        outputs = [message for message in messages if message["role"] == "tool"]
        if not outputs:
            return response(call("spawn_agent", "spawn", task="inspect evidence"))
        await child_entered.wait()
        parent_entered.set()
        if finish_before_cleanup:
            return response(call("finish_task", "finish", summary="verified"))
        await cancel_event.wait()
        # The provider cancellation boundary raises without cancelling the
        # owning Task when the caller requests cancellation through the event.
        raise asyncio.CancelledError

    model_factory(model)
    service = IterationLoopService(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint=checkpoint
    )
    running = asyncio.create_task(
        service.run("parent", "main objective", cancel_event=cancel_event)
    )
    await asyncio.wait_for(parent_entered.wait(), 5)
    agent = service._active_agent
    assert isinstance(agent, runtime.FullStackExpertAgent)
    return (
        running,
        agent,
        cancel_event,
        child_cleanup_entered,
        release_child_cleanup,
        child_drained,
    )


def assert_owned_resources_closed(agent, child_drained):
    assert child_drained.is_set()
    assert agent._subagents._closing
    assert not agent._subagents._active
    assert all(worker.done() for worker in agent._subagents._workers)
    assert agent._harness.mcp._closed
    assert agent._harness.capabilities._closed
    assert not agent._active_context.active_skill_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("event_also_set", [False, True])
async def test_direct_task_cancellation_propagates_after_real_child_cleanup(
    persistence, tmp_path, model_factory, event_also_set
):
    checkpoint, engine = persistence
    running, agent, event, cleanup, release, drained = await cancellation_scenario(
        checkpoint, tmp_path, model_factory
    )
    try:
        if event_also_set:
            event.set()
        running.cancel("server shutdown")
        await asyncio.wait_for(cleanup.wait(), 5)
        assert not running.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(running, 5)
        assert caught.value.args == ("server shutdown",)
        assert_owned_resources_closed(agent, drained)
        with Session(engine) as db:
            sessions = list(db.scalars(select(models.AgentTeamSession)))
            assert len(sessions) == 2
            assert all(session.status == "cancelled" for session in sessions)
            assert all(
                json.loads(session.result_payload)["error"] == "cancelled"
                for session in sessions
            )
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_before_cleanup", [False, True])
async def test_repeated_task_cancellation_during_cleanup_drains_resources(
    persistence, tmp_path, model_factory, finish_before_cleanup
):
    checkpoint, _ = persistence
    running, agent, _, cleanup, release, drained = await cancellation_scenario(
        checkpoint,
        tmp_path,
        model_factory,
        finish_before_cleanup=finish_before_cleanup,
    )
    try:
        if not finish_before_cleanup:
            running.cancel()
        await asyncio.wait_for(cleanup.wait(), 5)
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 5)
        assert_owned_resources_closed(agent, drained)
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fault", [False, True])
async def test_original_task_cancellation_survives_cleanup_cancellation_or_failure(
    persistence, tmp_path, model_factory, monkeypatch, cleanup_fault
):
    checkpoint, engine = persistence
    running, agent, _, cleanup, release, drained = await cancellation_scenario(
        checkpoint, tmp_path, model_factory
    )
    diagnostics = []
    sink = logger.add(
        lambda message: diagnostics.append(str(message)),
        level="ERROR",
        format="{message}",
    )
    try:
        if cleanup_fault:

            async def unavailable(*args):
                raise RuntimeError("sensitive-child-store-detail")

            monkeypatch.setattr(agent._subagents.store, "list_children", unavailable)
        running.cancel("server shutdown")
        await asyncio.wait_for(cleanup.wait(), 5)
        if not cleanup_fault:
            running.cancel("second shutdown")
            await asyncio.sleep(0)
        assert not running.done()
        release.set()
        observed = None
        try:
            await asyncio.wait_for(running, 5)
        except BaseException as exc:
            observed = exc
        assert_owned_resources_closed(agent, drained)
        with Session(engine) as db:
            parent = db.get(models.AgentTeamSession, agent.session_id)
            assert (
                type(observed),
                observed.args if observed else None,
                parent.status,
                json.loads(parent.result_payload)["error"],
            ) == (
                asyncio.CancelledError,
                ("server shutdown",),
                "cancelled",
                "cancelled",
            )
        if cleanup_fault:
            assert any("cleanup" in message.lower() for message in diagnostics)
            assert all(
                "sensitive-child-store-detail" not in message for message in diagnostics
            )
    finally:
        logger.remove(sink)
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_event_cancellation_keeps_structured_result_after_real_child_cleanup(
    persistence, tmp_path, model_factory
):
    checkpoint, engine = persistence
    running, agent, event, cleanup, release, drained = await cancellation_scenario(
        checkpoint, tmp_path, model_factory
    )
    try:
        event.set()
        await asyncio.wait_for(cleanup.wait(), 5)
        assert not running.done()
        release.set()
        result = await asyncio.wait_for(running, 5)
        assert result.outcome == "cancelled"
        assert not result.success and not running.cancelled()
        assert_owned_resources_closed(agent, drained)
        with Session(engine) as db:
            assert (
                db.get(models.AgentTeamSession, agent.session_id).status == "cancelled"
            )
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_system_only_crash_resume_never_duplicates_durable_or_model_seed(
    persistence, tmp_path, model_factory
):
    checkpoint, _ = persistence
    session = await checkpoint.create_session(1, "agent")
    # Authoritative crash boundary: the initial seed committed, but no user
    # message or model request exists in the durable ledger yet.
    await checkpoint.append_message(
        session.id, {"role": "system", "content": "original system seed"}
    )
    observed_system_counts = []

    async def model(messages, **kwargs):
        observed_system_counts.append(sum(m["role"] == "system" for m in messages))
        return SimpleNamespace(choices=[], usage=None)

    model_factory(model)
    workspace_service = AgentTeamWorkspaceService(tmp_path)
    for _ in range(2):
        loop = IterationLoopService(
            tmp_path,
            workspace_service,
            checkpoint=checkpoint,
            resume_cursor=await checkpoint.get_resume_cursor(),
        )
        await loop.run("recover seed", "continue the interrupted task")
    durable = await checkpoint.load_messages(session.id)
    assert (
        sum(message["role"] == "system" for message in durable),
        observed_system_counts,
    ) == (1, [1, 1])


@pytest.mark.asyncio
async def test_fresh_run_checkpoints_exactly_one_system_seed(
    persistence, tmp_path, model_factory
):
    checkpoint, _ = persistence
    model_system_counts = []

    async def model(messages, **kwargs):
        model_system_counts.append(sum(m["role"] == "system" for m in messages))
        return SimpleNamespace(choices=[], usage=None)

    model_factory(model)
    loop = IterationLoopService(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint=checkpoint
    )
    await loop.run("fresh task", "start a new task")
    cursor = await checkpoint.get_resume_cursor()
    durable = await checkpoint.load_messages(cursor.session_id)
    assert sum(message["role"] == "system" for message in durable) == 1
    assert model_system_counts == [1]


@pytest.mark.asyncio
async def test_runtime_audit_producers_preserve_durable_metadata_outside_model_turns(
    persistence, tmp_path, model_factory
):
    checkpoint, _ = persistence
    session = await checkpoint.create_session(1, "agent")

    async def model(messages, **kwargs):
        assert not any(message["role"] == "audit" for message in messages)
        assert not any(
            {"harness_event", "context_compaction"} & message.get("metadata", {}).keys()
            for message in messages
        )
        return SimpleNamespace(choices=[], usage=None)

    model_factory(model)
    agent = runtime.FullStackExpertAgent(
        tmp_path,
        AgentTeamWorkspaceService(tmp_path),
        checkpoint=checkpoint,
        session_id=session.id,
    )
    await agent.execute("observe audits", "keep actual conversation visible")
    await agent._persist_compaction_audit(
        {"before_tokens": 120, "after_tokens": 80, "retained_messages": 2}
    )
    durable = await checkpoint.load_messages(session.id)
    audits = [
        message
        for message in durable
        if {"harness_event", "context_compaction"} & message.get("metadata", {}).keys()
    ]
    assert len(audits) >= 2
    assert all(message["role"] == "audit" for message in audits)
    assert any("harness_event" in message["metadata"] for message in audits)
    assert any("context_compaction" in message["metadata"] for message in audits)
    assert all(message["content"] for message in durable if message["role"] == "user")
    # Historical user-role audits remain readable for recovery, and the model
    # projection excludes them by their durable metadata as well as new audits.
    historical = {
        "role": "user",
        "content": "",
        "metadata": {"harness_event": {"event": "historical_capability_allowed"}},
    }
    await checkpoint.append_message(session.id, historical)
    agent.messages = await checkpoint.load_messages(session.id)
    projected = agent._project_model_messages(agent._active_context)
    assert not any(
        {"harness_event", "context_compaction"} & message.get("metadata", {}).keys()
        for message in projected
    )
    assert any(
        "keep actual conversation visible" in message.get("content", "")
        for message in projected
    )
