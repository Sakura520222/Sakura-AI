"""Conversation pagination must ignore durable harness bookkeeping."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Template
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
from backend.webui.routes.agent_team import task_stream_data


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


@pytest.mark.parametrize(
    "method,page_size", [("_loadFull", 200), ("_loadIncremental", 50)]
)
def test_completed_live_view_drains_has_more_without_sse(tmp_path, method, page_size):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute the shipped live-view JavaScript")
    fragment = Path(
        "backend/webui/templates/components/agent_team_live_view_fragment.html"
    ).read_text()
    rendered = Template(fragment).render(_=lambda key: key)
    script = rendered.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = (
        script
        + r"""
    const assert = require('node:assert/strict');
    global.DOMPurify = {sanitize: value => value};
    global.marked = {parse: value => value};
    global.document = {getElementById: () => null};
    const requests = [];
    const all = Array.from({length: 205}, (_, i) => ({id: i + 1,
      session_id: 1, role: i === 204 ? 'assistant' : 'user',
      content: i === 204 ? 'Completed after all tests' : 'Guidance ' + i}));
    global.fetch = async url => {
      const query = new URL(url, 'http://fixture').searchParams;
      const cursor = Number(query.get('after_id'));
      const limit = Number(query.get('limit'));
      requests.push(cursor);
      const rows = all.filter(row => row.id > cursor);
      return {ok: true, json: async () => ({success: true,
        messages: rows.slice(0, limit), tool_calls: [], prompts: [],
        sessions: [{id: 1, iteration_number: 1, status: 'completed'}],
        has_more: rows.length > limit, task_status: 'completed',
        can_send_prompt: false})};
    };
    (async () => {
      const state = agentTeamLiveView();
      state.$nextTick = callback => callback();
      state.selectedTaskId = '1';
      await state.METHOD();
      assert.equal(state._messages.length, 205);
      assert.equal(state.displayItems.at(-1).content, 'Completed after all tests');
      assert.deepEqual(requests, EXPECTED_REQUESTS);
      assert.equal(state.isLoading, false);
      // An unchanged conversation can still have a changed tool status.
      global.fetch = async () => ({ok: true, json: async () => ({success: true,
        messages: [], prompts: [], sessions: [], has_more: false,
        tool_calls: [{id: 7, tool_call_id: 'read-1', status: 'completed'}]})});
      state._toolCalls['read-1'] = {id: 7, tool_call_id: 'read-1', status: 'running'};
      await state._loadIncremental();
      assert.equal(state._toolCalls['read-1'].status, 'completed');
      assert.equal(state._messages.length, 205);
      // Switching tasks while a page is in flight must not mix conversations.
      let releaseOld;
      global.fetch = () => new Promise(resolve => { releaseOld = resolve; });
      const oldLoad = state._loadFull();
      state.selectedTaskId = '2';
      state._resetStreamState(true);
      const dataFor = content => ({success: true, has_more: false, prompts: [],
        tool_calls: [], sessions: [{id: 2, status: 'completed'}],
        messages: [{id: 777, session_id: 2, role: 'assistant', content}]});
      global.fetch = async () => ({json: async () => dataFor('New task')});
      await state._loadFull();
      releaseOld({json: async () => dataFor('Stale old task')});
      await oldLoad;
      assert.deepEqual(state._messages.map(row => row.content), ['New task']);
      assert.equal(state.isLoading, false);
    })().catch(error => { console.error(error); process.exitCode = 1; });
    """
    )
    harness = harness.replace("METHOD", method).replace(
        "EXPECTED_REQUESTS", json.dumps(list(range(0, 205, page_size)))
    )
    path = tmp_path / "live-view-pagination.js"
    path.write_text(harness)
    result = subprocess.run(
        [node, str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("method", ["_loadFull", "_loadIncremental"])
def test_live_view_retains_sse_refresh_during_inflight_page(tmp_path, method):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute the shipped live-view JavaScript")
    fragment = Path(
        "backend/webui/templates/components/agent_team_live_view_fragment.html"
    ).read_text()
    rendered = Template(fragment).render(_=lambda key: key)
    script = rendered.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = (
        script
        + r"""
    const assert = require('node:assert/strict');
    global.DOMPurify = {sanitize: value => value};
    global.marked = {parse: value => value};
    global.document = {getElementById: () => null};
    let timer;
    let scheduled = 0;
    global.setTimeout = callback => { timer = callback; scheduled++; return 1; };
    const state = agentTeamLiveView();
    state.$nextTick = callback => callback();
    state.selectedTaskId = '1';
    let release;
    let requests = 0;
    let active = 0;
    let maximumActive = 0;
    const payload = (messages, status) => ({success: true, has_more: false,
      messages, sessions: [{id: 1, status: 'completed'}], prompts: [],
      tool_calls: [{id: 7, tool_call_id: 'read-1', status}]});
    global.fetch = async url => {
      requests++;
      active++;
      maximumActive = Math.max(maximumActive, active);
      if (requests === 1) return new Promise(resolve => { release = data => {
        active--; resolve({json: async () => data});
      }; });
      active--;
      return {json: async () => payload([{id: 1, session_id: 1,
        role: 'assistant', content: 'Final completion'}], 'completed')};
    };
    (async () => {
      const inFlight = state.METHOD();
      state._onSSEEvent('sse:agent:message_added', {task_id: 1});
      state._onSSEEvent('sse:agent:tool_completed', {task_id: 1});
      assert.equal(scheduled, 1, 'Repeated events must coalesce');
      // Fire the production debounce callback while the first snapshot waits.
      timer();
      for (let i = 0; i < 8; i++) await Promise.resolve();
      assert.equal(requests, 1, 'Updates must not start overlapping requests');
      release(payload([], 'running'));
      await inFlight;
      assert.equal(requests, 2, 'The pending update must rerun after pages drain');
      assert.deepEqual(state._messages.map(row => row.id), [1]);
      assert.equal(state._toolCalls['read-1'].status, 'completed');
      assert.equal(state.isLoading, false);
      assert.equal(maximumActive, 1);
    })().catch(error => { console.error(error); process.exitCode = 1; });
    """
    ).replace("METHOD", method)
    path = tmp_path / "live-view-sse-race.js"
    path.write_text(harness)
    result = subprocess.run(
        [node, str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
