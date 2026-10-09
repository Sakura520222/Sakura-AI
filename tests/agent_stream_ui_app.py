"""Browser fixture for production live-view pagination and SQLite queries.

Run ``uv run --no-sync python tests/agent_stream_ui_app.py`` and click the
fixture's completed-task button. Authentication is a local test double; the
stream endpoint, conversation template, JavaScript and ledger queries are real.
No AI, GitHub, MySQL, Redis or production credentials are used.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
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
from backend.webui.deps import get_db, require_auth
from backend.webui.i18n import i18n
from backend.webui.routes.agent_team import router

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
ledger = Session(engine, expire_on_commit=False)
ledger.add(
    AgentTeamTask(
        id=1,
        source_type="issue",
        repo_full_name="fixture/repository",
        repo_owner="fixture",
        repo_name="repository",
        title="Completed conversation pagination",
        started_by="fixture",
        status="completed",
    )
)
ledger.add(
    AgentTeamSession(
        id=1, task_id=1, iteration_number=1, role_name="agent", status="completed"
    )
)


def append(role, content, metadata=None):
    seq = ledger.scalar(select(func.count()).select_from(AgentTeamMessage)) + 1
    payload = {"role": role, "content": content}
    if metadata is not None:
        payload["metadata"] = metadata
    ledger.add(
        AgentTeamMessage(
            session_id=1,
            seq=seq,
            role=role,
            content=content,
            message_json=json.dumps(payload),
        )
    )
    ledger.flush()


for number in range(450):
    key = "harness_event" if number % 2 else "context_compaction"
    append("user", "", {key: {"index": number}})
for number in range(205):
    content = (
        "All tests passed, task complete."
        if number == 204
        else "Please implement the issue"
        if number == 0
        else f"Progress {number}"
    )
    append("user" if number == 0 else "assistant", content)
    append("audit", "", {"harness_event": {"index": number}})
ledger.commit()

app = FastAPI()
stream_requests = []
hold_next_response = False
snapshot_ready = asyncio.Event()
release_response = asyncio.Event()


class AsyncAdapter:
    async def get(self, model, ident):
        return ledger.get(model, ident)

    async def execute(self, statement):
        return ledger.execute(statement)


async def database():
    yield AsyncAdapter()


async def user():
    return {"sub": "fixture", "role": "super_admin", "user_id": 1}


app.dependency_overrides[get_db] = database
app.dependency_overrides[require_auth] = user
app.include_router(router)


@app.middleware("http")
async def record_requests(request, call_next):
    global hold_next_response
    if request.url.path.endswith("stream-data"):
        stream_requests.append(dict(request.query_params))
        hold = hold_next_response
        hold_next_response = False
        response = await call_next(request)
        if hold:
            snapshot_ready.set()
            await release_response.wait()
        return response
    return await call_next(request)


@app.get("/")
async def page(lang: str = "zh-CN"):
    i18n.reload()
    fragment = Template(
        Path(
            "backend/webui/templates/components/agent_team_live_view_fragment.html"
        ).read_text()
    ).render(_=lambda key: i18n.t(key, lang=lang))
    return HTMLResponse(
        """<!doctype html><html><head><meta charset="utf-8"><link rel="icon" href="data:,">
        <script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/dompurify/dist/purify.min.js"></script>
        <style>[x-cloak]{display:none!important}#live-messages{height:650px;overflow-y:auto}
        article{margin:10px;border-bottom:1px solid #eee}.whitespace-pre-wrap{white-space:pre-wrap}</style>
        </head><body><button id="view-completed" onclick="window.agentTeamLiveState.watchTask(1)">
        View completed fixture</button>
        <button id="completion-race" onclick="runCompletionRace()">Complete during pending snapshot</button>
        <script>
        async function runCompletionRace() {
            await fetch('/__arm_race', {method: 'POST'});
            const loading = window.agentTeamLiveState._loadIncremental();
            await fetch('/__snapshot_ready');
            await fetch('/__append_completion', {method: 'POST'});
            window.dispatchEvent(new CustomEvent('sse:agent:message_added', {detail: {task_id: 1}}));
            await new Promise(resolve => setTimeout(resolve, 350));
            await fetch('/__release_response', {method: 'POST'});
            await loading;
        }
        </script>"""
        + fragment
        + "</body></html>"
    )


@app.get("/__evidence")
async def evidence():
    return {
        "requests": stream_requests,
        "durable_rows": ledger.scalar(
            select(func.count()).select_from(AgentTeamMessage)
        ),
    }


@app.post("/__arm_race")
async def arm_race():
    global hold_next_response
    snapshot_ready.clear()
    release_response.clear()
    hold_next_response = True
    return {"armed": True}


@app.get("/__snapshot_ready")
async def ready():
    await snapshot_ready.wait()
    return {"snapshot_ready": True}


@app.post("/__append_completion")
async def append_completion():
    append("assistant", "Final SSE completion after snapshot")
    ledger.commit()
    return {"appended": True}


@app.post("/__release_response")
async def release():
    release_response.set()
    return {"released": True}


@app.post("/__shutdown")
async def shutdown():
    app.state.fixture_server.should_exit = True
    return {"stopping": True}


if __name__ == "__main__":
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=18766))
    app.state.fixture_server = server
    server.run()
