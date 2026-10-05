"""Local browser fixture for the real Agent routes, templates and CSRF checks.

Run from the repository root with ``uv run python tests/harness_ui_app.py``.
Database/authentication and background job submission use test doubles. This
fixture never executes model calls or connects to GitHub or a real database.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import FastAPI
from starlette.responses import StreamingResponse

from backend.core.time_service import now_utc as utc_now
from backend.services.agent_team.conversation_checkpoint import (
    ConversationCheckpointService,
)
from backend.webui import deps
from backend.webui.routes import agent_team

task = SimpleNamespace(
    id=912,
    status="failed",
    current_phase="blocked",
    title="Harness no-progress recovery",
    repo_full_name="fixture/repository",
    source_type="manual_issue",
    source_issue_number=1,
    workspace_path="/fixture/workspace",
    branch_name="feature/fixture",
    base_branch="develop",
    pr_number=None,
    iteration_count=0,
    completed_at=None,
    last_checkpoint_at=utc_now(),
    updated_at=utc_now(),
    created_at=utc_now(),
    pr_url=None,
    started_by="fixture-admin",
    resume_count=0,
)
events: list[dict] = []


class Result:
    def __init__(self, rows=None):
        self.rows = [task] if rows is None else rows

    def scalar_one_or_none(self):
        return task

    def scalar(self):
        return 1

    def scalars(self):
        return self

    def all(self):
        return self.rows


class Database:
    async def execute(self, _statement):
        if _statement._group_by_clauses:
            return Result([(task.status, 1)])
        return Result()

    async def scalar(self, _statement):
        # Render accurate fixture counters, including zero waiting-human tasks.
        parameters = _statement.compile().params
        status_filter = next(
            (value for key, value in parameters.items() if key.startswith("status_")),
            None,
        )
        if isinstance(status_filter, (list, tuple)):
            return int(task.status in status_filter)
        if status_filter is not None:
            return int(task.status == status_filter)
        return 1

    async def commit(self):
        events.append({"event": "commit", "status": task.status})


async def database():
    yield Database()


async def user():
    return {"sub": "fixture-admin", "user_id": 1, "role": "super_admin"}


async def preferences():
    return {"language": "zh-CN", "items_per_page": 20}


async def has_checkpoint(_self):
    return True


async def audit(*_args, **_kwargs):
    events.append({"event": "admin_audit"})


async def submit_background(task_id):
    events.append({"event": "resume_submitted", "task_id": task_id})


ConversationCheckpointService.has_resume_state = has_checkpoint
agent_team.log_admin_action = audit
agent_team._resume_agent_task_background = submit_background
app = FastAPI()
app.dependency_overrides[deps.get_db] = database
app.dependency_overrides[deps.require_auth] = user
app.dependency_overrides[deps.get_user_preferences] = preferences
app.include_router(agent_team.router)


@app.get("/__evidence")
async def evidence():
    return {"status": task.status, "phase": task.current_phase, "events": events}


@app.get("/version/info")
async def version():
    return {"current_version": "test-fixture", "update_available": False}


@app.get("/api/v1/announcements/unread")
async def unread():
    return {"unread_count": 0}


@app.get("/sse/events")
async def stream():
    async def body():
        yield ": browser fixture\n\n"
        await asyncio.Event().wait()

    return StreamingResponse(body(), media_type="text/event-stream")


@app.post("/__shutdown")
async def shutdown():
    app.state.fixture_server.should_exit = True
    return {"stopping": True}


if __name__ == "__main__":
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=18762))
    app.state.fixture_server = server
    server.run()
