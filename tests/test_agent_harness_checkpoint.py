"""Checkpoint transaction tests using SQLite through a small async adapter."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.models.agent_team_models import (
    AgentTeamMessage,
    AgentTeamSession,
    AgentTeamSubagent,
    AgentTeamTask,
    AgentTeamToolCall,
    AgentTeamUsage,
)
from backend.models.database import Base
from backend.services.agent_team import conversation_checkpoint as checkpoint_module
from backend.services.agent_team.conversation_checkpoint import (
    ConversationCheckpointService,
)


@pytest.fixture
def persistence(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            item.__table__
            for item in (
                AgentTeamTask,
                AgentTeamSession,
                AgentTeamMessage,
                AgentTeamToolCall,
                AgentTeamSubagent,
                AgentTeamUsage,
            )
        ],
    )
    locked = []

    class Adapter:
        def __init__(self):
            self.db = Session(engine, expire_on_commit=False)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.db.close()

        async def get(self, model, ident, **kwargs):
            if model is AgentTeamSession:
                locked.append(kwargs.get("with_for_update", False))
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
    with Session(engine) as db:
        db.add(
            AgentTeamTask(
                id=1,
                source_type="issue",
                repo_full_name="o/r",
                repo_owner="o",
                repo_name="r",
                title="t",
            )
        )
        db.commit()
    yield ConversationCheckpointService(1), engine, locked
    engine.dispose()


@pytest.mark.asyncio
async def test_call_result_and_state_commit_together_with_locked_sequence(persistence):
    service, engine, locked = persistence
    session = await service.create_session(1, "agent")
    await service.append_message(
        session.id,
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "w", "function": {"name": "write_file", "arguments": "{}"}}
            ],
        },
    )
    await service.mark_tool_call_running(session.id, "w")
    message_id = await service.record_tool_result(
        session.id,
        "w",
        {"role": "tool", "tool_call_id": "w", "content": '{"error":"denied"}'},
        "failed",
        "denied",
    )
    with Session(engine) as db:
        row = db.scalar(select(AgentTeamToolCall))
        assert row.status == "failed" and row.result_message_id == message_id
        assert row.completed_at is not None and row.error_message == "denied"
        assert [
            item.seq
            for item in db.scalars(
                select(AgentTeamMessage).order_by(AgentTeamMessage.seq)
            )
        ] == [1, 2]
    assert locked == [True, True]


@pytest.mark.asyncio
async def test_missing_call_rolls_back_result_and_sequence(persistence):
    service, engine, _ = persistence
    session = await service.create_session(1, "agent")
    with pytest.raises(ValueError, match="missing"):
        await service.record_tool_result(
            session.id,
            "missing",
            {"role": "tool", "tool_call_id": "missing", "content": "{}"},
            "completed",
        )
    with Session(engine) as db:
        assert db.scalar(select(AgentTeamMessage)) is None
        assert db.get(AgentTeamSession, session.id).last_seq == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["success", "blocked", "cancelled", "unrecoverable_error"]
)
async def test_session_payload_and_terminal_status_match(persistence, outcome):
    service, engine, _ = persistence
    session = await service.create_session(1, "agent")
    await service.finish_session(
        session.id,
        outcome,
        {"outcome": outcome, "success": outcome == "success", "tool_calls_count": 2},
    )
    with Session(engine) as db:
        row = db.get(AgentTeamSession, session.id)
        assert row.status == ("completed" if outcome == "success" else outcome)
        assert json.loads(row.result_payload)["outcome"] == outcome
        assert row.tool_calls_count == 2 and row.completed_at is not None


@pytest.mark.asyncio
async def test_cancelled_call_is_durable_for_recovery(persistence):
    service, _, _ = persistence
    session = await service.create_session(1, "agent")
    await service.append_message(
        session.id,
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "w", "function": {"name": "write_file", "arguments": "{}"}}
            ],
        },
    )
    await service.mark_tool_call_running(session.id, "w")
    await service.mark_tool_call_cancelled(session.id, "w")
    states = await service.load_tool_call_states(session.id)
    assert states["w"]["status"] == "cancelled"
    assert states["w"]["result_message_id"] is None


@pytest.mark.asyncio
async def test_resume_cursor_uses_latest_session_even_if_older_session_failed(
    persistence,
):
    service, _, _ = persistence
    older = await service.create_session(1, "agent")
    await service.finish_session(older.id, "blocked", {"success": False})
    newer = await service.create_session(2, "agent")
    await service.finish_session(newer.id, "success", {"success": True})
    cursor = await service.get_resume_cursor()
    assert cursor.session_id == newer.id


@pytest.mark.asyncio
async def test_legacy_fullstack_migration_retains_completed_finish_ledger(
    persistence, tmp_path
):
    from backend.services.agent_team.iteration_loop import IterationLoopService
    from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

    checkpoint, _, _ = persistence
    older = await checkpoint.create_session(1, "fullstack")
    await checkpoint.append_message(
        older.id,
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "f",
                    "function": {
                        "name": "finish_task",
                        "arguments": '{"summary":"done"}',
                    },
                }
            ],
        },
    )
    await checkpoint.record_tool_result(
        older.id,
        "f",
        {
            "role": "tool",
            "tool_call_id": "f",
            "content": '{"_terminal":true,"summary":"done"}',
        },
        "completed",
    )
    cursor = await checkpoint.get_resume_cursor()
    service = IterationLoopService(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint=checkpoint
    )
    agent = await service._create_agent("agent", 1, cursor)
    recovered = await agent._recover(agent._build_context())
    assert recovered.success and recovered.summary == "done"


@pytest.mark.asyncio
async def test_missing_durable_call_cannot_start(persistence):
    service, _, _ = persistence
    session = await service.create_session(1, "agent")
    with pytest.raises(ValueError, match="missing"):
        await service.mark_tool_call_running(session.id, "missing")


@pytest.mark.asyncio
async def test_completed_call_cannot_start_again(persistence):
    service, _, _ = persistence
    session = await service.create_session(1, "agent")
    await service.append_message(
        session.id,
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "w", "function": {"name": "write_file", "arguments": "{}"}}
            ],
        },
    )
    await service.record_tool_result(
        session.id,
        "w",
        {"role": "tool", "tool_call_id": "w", "content": "{}"},
        "completed",
    )
    with pytest.raises(ValueError, match="completed"):
        await service.mark_tool_call_running(session.id, "w")


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", [RuntimeError, asyncio.CancelledError])
async def test_interrupted_legacy_copy_never_replays_completed_mutation(
    persistence, tmp_path, monkeypatch, interruption
):
    from backend.services.agent_team.fullstack_expert import _get_missing_tool_calls
    from backend.services.agent_team.iteration_loop import IterationLoopService
    from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

    checkpoint, engine, _ = persistence
    older = await checkpoint.create_session(1, "fullstack")
    await checkpoint.append_message(
        older.id,
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "w",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps(
                            {"file_path": "state.txt", "content": "earlier mutation"}
                        ),
                    },
                },
                {
                    "id": "f",
                    "function": {
                        "name": "finish_task",
                        "arguments": '{"summary":"done"}',
                    },
                },
            ],
        },
    )
    for ident, payload in (
        ("w", {"_modified_file": "state.txt"}),
        ("f", {"_terminal": True, "summary": "done"}),
    ):
        await checkpoint.record_tool_result(
            older.id,
            ident,
            {"role": "tool", "tool_call_id": ident, "content": json.dumps(payload)},
            "completed",
        )
    # The completed write must not overwrite subsequent legitimate changes.
    path = tmp_path / "state.txt"
    path.write_text("later legitimate content")
    service = IterationLoopService(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint=checkpoint
    )
    original_append = checkpoint.append_message_in_session

    async def interrupt_after_copied_assistant(
        db, session_id, message, *args, **kwargs
    ):
        if session_id != older.id and message.get("role") == "tool":
            raise interruption("interrupted before copying the result")
        return await original_append(db, session_id, message, *args, **kwargs)

    monkeypatch.setattr(
        checkpoint, "append_message_in_session", interrupt_after_copied_assistant
    )
    with pytest.raises(interruption):
        await service._create_agent("agent", 1, await checkpoint.get_resume_cursor())
    monkeypatch.setattr(checkpoint, "append_message_in_session", original_append)
    cursor_after_crash = await checkpoint.get_resume_cursor()
    with Session(engine) as db:
        sessions_after_crash = list(db.scalars(select(AgentTeamSession.id)))
    # Exercise the real next-resume recovery and executor, so a pending copy
    # would actually replay the write rather than merely failing a shape check.
    resumed = await service._create_agent("agent", 1, cursor_after_crash)
    ctx = resumed._build_context()
    result = await resumed._recover(ctx)
    if result is None:
        await resumed._execute_tool_calls(
            _get_missing_tool_calls(resumed.messages), ctx, 1
        )
    assert path.read_text() == "later legitimate content"
    assert result is not None and result.success
    assert cursor_after_crash.session_id == older.id
    assert sessions_after_crash == [older.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_read", ["load_messages", "load_tool_call_states"])
async def test_legacy_recovery_read_error_keeps_original_cursor(
    persistence, tmp_path, monkeypatch, failing_read
):
    from backend.services.agent_team.iteration_loop import IterationLoopService
    from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

    checkpoint, engine, _ = persistence
    older = await checkpoint.create_session(1, "fullstack")
    await checkpoint.append_message(
        older.id,
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "w", "function": {"name": "write_file", "arguments": "{}"}}
            ],
        },
    )
    await checkpoint.mark_tool_call_running(older.id, "w")
    monkeypatch.setattr(
        checkpoint,
        failing_read,
        AsyncMock(side_effect=RuntimeError("ledger unavailable")),
    )
    service = IterationLoopService(
        tmp_path, AgentTeamWorkspaceService(tmp_path), checkpoint=checkpoint
    )
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await service._create_agent("agent", 1, await checkpoint.get_resume_cursor())
    assert (await checkpoint.get_resume_cursor()).session_id == older.id
    with Session(engine) as db:
        assert list(db.scalars(select(AgentTeamSession.id))) == [older.id]
        assert db.scalar(select(AgentTeamToolCall)).status == "running"


@pytest.mark.asyncio
async def test_control_audit_is_durable_and_never_selects_resume_cursor(persistence):
    service, engine, _locked = persistence
    await service.record_control_event(
        {"event": "capability_allowed", "action": "dependency"}
    )
    assert await service.get_resume_cursor() is None
    main = await service.create_session(1, "agent")
    await service.record_control_event({"event": "capability_denied", "action": "git"})
    assert (await service.get_resume_cursor()).session_id == main.id
    with Session(engine) as db:
        from sqlalchemy import select

        from backend.models.agent_team_models import AgentTeamMessage, AgentTeamSession

        control = db.execute(
            select(AgentTeamSession).where(
                AgentTeamSession.role_name == "harness_control"
            )
        ).scalar_one()
        rows = (
            db.execute(
                select(AgentTeamMessage).where(
                    AgentTeamMessage.session_id == control.id
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert all(row.content == "" for row in rows)
        assert all(row.role == "audit" for row in rows)
