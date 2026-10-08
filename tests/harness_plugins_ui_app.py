"""Local browser fixture using real plugin routes/templates/auth role checks/CSRF.

Only identity lookup, DB adapter and unrelated external widget responses are fake.
Run with the issue worktree environment; binds 127.0.0.1:18764 only.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from itsdangerous import URLSafeTimedSerializer
from starlette.responses import StreamingResponse

from backend.core import config
from backend.services.agent_team import plugin_config
from backend.webui import deps
from backend.webui.routes import agent_plugins
from backend.webui.routes import config as config_routes


def create_app(monkeypatch=None):
    """Patches are isolated by pytest; standalone process owns fixture patches."""

    def patch(obj, key, value):
        if monkeypatch:
            monkeypatch.setattr(obj, key, value)
        else:
            setattr(obj, key, value)

    settings = config.Settings.model_construct(webui_secret_key="plugin-fixture-only")
    patch(config, "get_settings", lambda: settings)
    patch(plugin_config, "get_settings", lambda: settings)
    patch(agent_plugins, "get_settings", lambda: settings)
    patch(
        deps,
        "_csrf_serializer",
        URLSafeTimedSerializer("plugin-fixture-only", salt="webui-csrf"),
    )
    initial = {
        "version": 1,
        "mcp": {
            "servers": [
                {
                    "id": "docs",
                    "url": "https://example.test/mcp",
                    "headers": {"Authorization": "fixture-header-token"},
                }
            ]
        },
        "hooks": [],
    }
    rows = {
        plugin_config.PLUGIN_KEY: SimpleNamespace(
            key_name=plugin_config.PLUGIN_KEY, key_value=json.dumps(initial)
        )
    }
    events = []

    class Database:
        async def execute(self, statement):
            key = statement.compile().params.get("key_name_1")
            return SimpleNamespace(scalar_one_or_none=lambda: rows.get(key))

        def add(self, row):
            rows[row.key_name] = row

        async def commit(self):
            events.append("commit")

        async def rollback(self):
            events.append("rollback")

    async def database():
        yield Database()

    async def identity(request: Request):
        role = request.headers.get("x-fixture-role", "super_admin")
        if role == "anonymous":
            raise HTTPException(401, "login_required")
        return {"sub": "fixture-admin", "user_id": 1, "role": role}

    async def preferences(request: Request):
        return {
            "language": request.query_params.get("lang", "en"),
            "items_per_page": 20,
        }

    async def audit(*args):
        events.append({"audit": copy.deepcopy(args[-1])})

    patch(deps, "require_auth", identity)
    patch(agent_plugins, "log_admin_action", audit)
    patch(
        agent_plugins.AgentSkillService,
        "list_skills",
        AsyncMock(return_value=[SimpleNamespace(enabled=True)]),
    )
    app = FastAPI()
    app.dependency_overrides[deps.get_db] = database
    app.dependency_overrides[deps.get_user_preferences] = preferences
    app.include_router(agent_plugins.router)
    app.include_router(config_routes.router)
    app.state.rows = rows
    app.state.events = events

    @app.get("/__evidence")
    async def evidence():
        return {
            "events": events,
            "stored": plugin_config.public_plugin_config(
                plugin_config.parse_harness_plugins(
                    rows[plugin_config.PLUGIN_KEY].key_value
                )
            ),
        }

    @app.get("/version/info")
    async def version():
        return {"current_version": "fixture", "update_available": False}

    @app.get("/favicon.ico", status_code=204)
    async def favicon():
        return None

    @app.get("/api/v1/announcements/unread")
    async def unread():
        return {"unread_count": 0}

    @app.get("/sse/events")
    async def stream():
        async def body():
            yield ": plugin browser fixture\n\n"
            await asyncio.Event().wait()

        return StreamingResponse(body(), media_type="text/event-stream")

    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=18764)
