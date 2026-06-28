"""ActivityCheckpointService tests."""

from types import SimpleNamespace

import pytest

from backend.models.activity_conversation_models import (
    ActivityMessage,
    ActivitySession,
    ActivityToolCall,
)
from backend.services.activity_checkpoint_service import (
    ActivityCheckpointService,
    _normalize_tool_call,
)


class _Result:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def all(self):
        if isinstance(self.value, list):
            return self.value
        if self.value is None:
            return []
        return [self.value]

    def first(self):
        values = self.all()
        return values[0] if values else None

    def scalar_one_or_none(self):
        return self.value


class _MemoryDb:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def add(self, obj):
        if isinstance(obj, ActivitySession):
            if obj.id is None:
                obj.id = self.store["next_session_id"]
                self.store["next_session_id"] += 1
            self.store["sessions"][obj.id] = obj
        elif isinstance(obj, ActivityMessage):
            if obj.id is None:
                obj.id = self.store["next_message_id"]
                self.store["next_message_id"] += 1
            self.store["messages"][obj.id] = obj
        elif isinstance(obj, ActivityToolCall):
            if obj.id is None:
                obj.id = self.store["next_tool_call_id"]
                self.store["next_tool_call_id"] += 1
            self.store["tool_calls"][obj.id] = obj

    async def commit(self):
        return None

    async def flush(self):
        return None

    async def refresh(self, _obj):
        return None

    async def get(self, model, obj_id):
        if model is ActivitySession:
            return self.store["sessions"].get(obj_id)
        if model is ActivityMessage:
            return self.store["messages"].get(obj_id)
        if model is ActivityToolCall:
            return self.store["tool_calls"].get(obj_id)
        return None

    async def execute(self, _statement):
        entity = None
        try:
            entity = _statement.column_descriptions[0].get("entity")
        except (AttributeError, IndexError, KeyError):
            entity = None

        params = {}
        try:
            params = _statement.compile().params
        except Exception:
            params = {}

        if entity is ActivityMessage:
            session_id = params.get("session_id_1")
            rows = [
                msg
                for msg in self.store["messages"].values()
                if session_id is None or msg.session_id == session_id
            ]
            return _Result(sorted(rows, key=lambda msg: msg.seq))

        if entity is ActivitySession:
            source_type = params.get("source_type_1")
            source_task_id = params.get("source_task_id_1")
            role_name = params.get("role_name_1")
            status = params.get("status_1")
            rows = [
                session
                for session in self.store["sessions"].values()
                if (source_type is None or session.source_type == source_type)
                and (source_task_id is None or session.source_task_id == source_task_id)
                and (role_name is None or session.role_name == role_name)
                and (status is None or session.status == status)
            ]
            rows = sorted(
                rows,
                key=lambda session: (session.completed_at or 0, session.id or 0),
                reverse=True,
            )
            return _Result(rows)

        return _Result(None)


class _MemorySessionFactory:
    def __init__(self, store):
        self.store = store

    def __call__(self):
        return _MemoryDb(self.store)


def _make_store():
    return {
        "sessions": {},
        "messages": {},
        "tool_calls": {},
        "next_session_id": 1,
        "next_message_id": 1,
        "next_tool_call_id": 1,
    }


def _find_tool_call(store, session_id, tool_call_id):
    for tool_call in store["tool_calls"].values():
        if (
            tool_call.session_id == session_id
            and tool_call.tool_call_id == tool_call_id
        ):
            return tool_call
    return None


@pytest.fixture
def memory_checkpoint(monkeypatch):
    store = _make_store()
    monkeypatch.setattr(
        "backend.services.activity_checkpoint_service.db_module.async_session",
        _MemorySessionFactory(store),
    )

    published = []

    async def fake_publish(event_type, data):
        published.append((event_type, data))

    async def fake_get_tool_call(db, session_id, tool_call_id):
        return _find_tool_call(db.store, session_id, tool_call_id)

    monkeypatch.setattr(
        "backend.services.activity_checkpoint_service._publish", fake_publish
    )
    monkeypatch.setattr(
        ActivityCheckpointService,
        "_get_tool_call",
        staticmethod(fake_get_tool_call),
    )
    return store, published


def test_normalize_tool_call_supports_dict():
    tool_call = {
        "id": "call_1",
        "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
    }

    normalized = _normalize_tool_call(tool_call)

    assert normalized == {
        "id": "call_1",
        "name": "read_file",
        "arguments": '{"path":"a.py"}',
    }


def test_normalize_tool_call_supports_object():
    tool_call = SimpleNamespace(
        id="call_2",
        function=SimpleNamespace(name="search", arguments='{"query":"x"}'),
    )

    normalized = _normalize_tool_call(tool_call)

    assert normalized == {
        "id": "call_2",
        "name": "search",
        "arguments": '{"query":"x"}',
    }


@pytest.mark.asyncio
async def test_activity_checkpoint_loads_messages_in_sequence(memory_checkpoint):
    store, _published = memory_checkpoint
    service = ActivityCheckpointService("pr", 101)
    session = await service.create_session(role_name="reviewer")
    await service.append_message(session.id, {"role": "system", "content": "sys"})
    await service.append_message(session.id, {"role": "user", "content": "first"})

    messages = await service.load_messages(session.id)

    assert messages == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
    ]
    assert store["sessions"][session.id].last_seq == 2


@pytest.mark.asyncio
async def test_activity_checkpoint_copies_messages_to_new_session(memory_checkpoint):
    store, _published = memory_checkpoint
    service = ActivityCheckpointService("pr", 101)
    source = await service.create_session(role_name="reviewer")
    target = await service.create_session(role_name="reviewer")
    await service.append_message(source.id, {"role": "system", "content": "sys"})
    await service.append_message(source.id, {"role": "assistant", "content": "old"})

    copied = await service.copy_messages_to_session(source.id, target.id)

    assert copied == 2
    assert await service.load_messages(target.id) == [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "old"},
    ]
    assert store["sessions"][target.id].last_seq == 2


@pytest.mark.asyncio
async def test_activity_checkpoint_message_and_tool_call_flow(memory_checkpoint):
    store, published = memory_checkpoint
    service = ActivityCheckpointService("pr", 123)
    session = await service.create_session(role_name="reviewer", model="test-model")

    assistant_msg = await service.append_message(
        session.id,
        {
            "role": "assistant",
            "content": "I will inspect files.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"file_path":"backend/app.py"}',
                    },
                }
            ],
        },
    )
    await service.mark_tool_call_running(session.id, "call_1")
    tool_msg = await service.append_message(
        session.id,
        {"role": "tool", "tool_call_id": "call_1", "content": "file body"},
    )
    await service.mark_tool_call_completed(session.id, "call_1", tool_msg.id)
    await service.complete_session(session.id, tool_calls_count=1)

    stored_session = store["sessions"][session.id]
    stored_assistant = store["messages"][assistant_msg.id]
    stored_tool = store["messages"][tool_msg.id]
    tool_call = _find_tool_call(store, session.id, "call_1")

    assert stored_session.status == "completed"
    assert stored_session.tool_calls_count == 1
    assert stored_assistant.role == "assistant"
    assert stored_tool.role == "tool"
    assert tool_call is not None
    assert tool_call.status == "completed"
    assert tool_call.assistant_message_id == assistant_msg.id
    assert tool_call.result_message_id == tool_msg.id
    assert [item[0] for item in published] == [
        "activity:session_started",
        "activity:message_added",
        "activity:tool_started",
        "activity:message_added",
        "activity:tool_completed",
        "activity:session_completed",
    ]


@pytest.mark.asyncio
async def test_activity_checkpoint_mark_tool_call_failed(memory_checkpoint):
    store, _published = memory_checkpoint
    service = ActivityCheckpointService("scan", 789)
    session = await service.create_session(role_name="scanner")
    await service.append_message(
        session.id,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_fail",
                    "function": {"name": "search", "arguments": "{}"},
                }
            ],
        },
    )

    await service.mark_tool_call_failed(session.id, "call_fail", "boom")

    tool_call = _find_tool_call(store, session.id, "call_fail")
    assert tool_call is not None
    assert tool_call.status == "failed"
    assert tool_call.error_message == "boom"


@pytest.mark.asyncio
async def test_activity_checkpoint_append_message_missing_session_raises(
    memory_checkpoint,
):
    service = ActivityCheckpointService("issue", 456)

    with pytest.raises(ValueError, match="ActivitySession not found"):
        await service.append_message(999, {"role": "assistant", "content": "x"})
