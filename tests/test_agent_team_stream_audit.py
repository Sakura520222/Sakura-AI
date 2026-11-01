"""Conversation pagination must ignore durable harness bookkeeping."""

import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from backend.models.agent_team_models import (
    AgentTeamMessage,
    AgentTeamSession,
    AgentTeamTask,
    AgentTeamToolCall,
    AgentTeamUserPrompt,
)
from backend.models.database import Base
from backend.services.agent_team.api_operations import task_stream_data


@pytest.fixture
def stream_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                AgentTeamTask,
                AgentTeamSession,
                AgentTeamMessage,
                AgentTeamToolCall,
                AgentTeamUserPrompt,
            )
        ],
    )
    with Session(engine) as db:
        db.add(
            AgentTeamTask(
                id=1,
                source_type="issue",
                repo_full_name="owner/repo",
                repo_owner="owner",
                repo_name="repo",
                title="Completed task",
                started_by="owner",
                status="completed",
            )
        )
        db.add(
            AgentTeamSession(
                id=1,
                task_id=1,
                iteration_number=1,
                role_name="agent",
                status="completed",
            )
        )
        db.commit()

        class AsyncAdapter:
            async def get(self, model, ident):
                return db.get(model, ident)

            async def execute(self, statement):
                return db.execute(statement)

        yield db, AsyncAdapter()
    engine.dispose()


def append(db, role, content, metadata=None, raw=None):
    seq = db.scalar(select(func.count()).select_from(AgentTeamMessage)) + 1
    payload = {"role": role, "content": content}
    if metadata is not None:
        payload["metadata"] = metadata
    row = AgentTeamMessage(
        session_id=1,
        seq=seq,
        role=role,
        content=content,
        message_json=raw if raw is not None else json.dumps(payload),
    )
    db.add(row)
    db.flush()
    return row.id


async def stream(adapter, after_id=0, limit=200, user=None):
    response = await task_stream_data(
        1, after_id, limit, user or {"sub": "owner", "role": "user"}, adapter
    )
    return response.status_code, json.loads(response.body)


@pytest.mark.asyncio
async def test_audits_before_real_conversation_do_not_consume_visible_page(stream_db):
    db, adapter = stream_db
    for index in range(450):
        key = "harness_event" if index % 2 else "context_compaction"
        append(db, "user", "", {key: {"event": "recorded", "index": index}})
    append(db, "audit", "", {"harness_event": {"event": "new-format"}})
    append(db, "harness_control", "", {"harness_event": {"event": "cancelled"}})
    expected = [
        append(db, "user", "Fix this issue", {"guidance_ids": [77]}),
        append(db, "assistant", "Task completed"),
    ]
    db.commit()

    status, data = await stream(adapter)

    assert status == 200
    assert [item["id"] for item in data["messages"]] == expected
    assert data["messages"][0]["guidance_ids"] == [77]
    assert data["has_more"] is False
    assert db.scalar(select(func.count()).select_from(AgentTeamMessage)) == 454


@pytest.mark.asyncio
async def test_audit_filter_preserves_real_empty_user_tools_and_malformed_metadata(
    stream_db,
):
    db, adapter = stream_db
    expected = [
        append(db, "user", ""),
        append(db, "user", '{"metadata":{"harness_event":{}}}'),
        append(db, "user", "Human guidance", {"guidance_ids": [9]}),
        append(db, "user", "Broken saved JSON", raw="not-json"),
        append(db, "user", "Odd metadata", metadata=["harness_event"]),
        append(db, "tool", "Valid tool result", {"harness_event": {"tool": "data"}}),
    ]
    append(db, "user", "", {"harness_event": {"event": "audit"}})
    db.commit()
    _, data = await stream(adapter)
    assert [item["id"] for item in data["messages"]] == expected


@pytest.mark.asyncio
async def test_pagination_uses_visible_messages_and_global_ids(stream_db):
    db, adapter = stream_db
    expected = []
    for index in range(205):
        append(db, "user", "", {"harness_event": {"index": index}})
        expected.append(append(db, "assistant", f"Visible {index}"))
    append(db, "user", "", {"context_compaction": {"event": "tail"}})
    db.commit()
    _, first = await stream(adapter)
    assert [item["id"] for item in first["messages"]] == expected[:200]
    assert first["has_more"] is True
    _, second = await stream(adapter, first["messages"][-1]["id"])
    assert [item["id"] for item in second["messages"]] == expected[200:]
    assert second["has_more"] is False


@pytest.mark.asyncio
async def test_stream_owner_scope_still_denies_other_user(stream_db):
    _, adapter = stream_db
    status, data = await stream(adapter, user={"sub": "other", "role": "user"})
    assert status == 403
    assert data["error"] == "Forbidden"
