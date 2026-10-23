"""Phase 2 browser fixture: real config page/save handlers and CSRF validation.

Run: uv run --no-sync python tests/harness_phase2_ui_app.py
Only localhost is exposed. Authentication, storage, unrelated section saves and
network status use test doubles; no model, GitHub or production database calls.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import FastAPI, Request
from starlette.responses import StreamingResponse

from backend.core import config
from backend.models.database import _settings_default_to_str
from backend.webui import deps
from backend.webui.routes import config as routes

# Build defaults without consulting host environment credentials.
settings = config.Settings.model_construct(webui_secret_key="local-fixture-only")
config.get_settings = lambda: settings
routes.get_settings = lambda: settings
rows = {
    key: SimpleNamespace(
        key_name=key, key_value=_settings_default_to_str(getattr(settings, key))
    )
    for group in config.DYNAMIC_CONFIG_GROUPS.values()
    for key in group["keys"]
}
removed = [
    "agent_team_max_model_rounds",
    "agent_team_max_tool_calls",
    "agent_team_max_parallel_reads",
    "agent_team_max_no_progress_rounds",
    "agent_team_repository_file_bytes",
    "agent_team_repository_total_bytes",
    "agent_team_repository_scan_entries",
    "agent_team_repository_skill_count",
    "agent_team_repository_metadata_bytes",
]
for key in removed:
    rows[key] = SimpleNamespace(key_name=key, key_value="1")
events = []


class Database:
    async def execute(self, statement):
        key = statement.compile().params.get("key_name_1")
        return SimpleNamespace(
            scalar_one_or_none=lambda: rows.get(key),
            scalars=lambda: SimpleNamespace(all=lambda: list(rows.values())),
        )

    def add(self, row):
        rows[row.key_name] = row

    async def commit(self):
        events.append({"event": "commit"})

    async def rollback(self):
        events.append({"event": "rollback"})


async def database():
    yield Database()


async def user():
    return {"sub": "fixture-admin", "user_id": 1, "role": "super_admin"}


async def preferences(request: Request):
    return {"language": request.query_params.get("lang", "zh-CN"), "items_per_page": 20}


async def audit(_db, _user_id, _action, _kind, _ident, detail):
    events.append({"event": "config_saved", "keys": sorted(detail)})


async def unrelated_section_save(*_args, **_kwargs):
    return deps.toast_redirect("/config", "toast.config_saved_live", lang="zh-CN")


routes.log_admin_action = audit
routes.section_config_service.resolve_depgraph_mode = AsyncMock(return_value="auto")
for name in (
    "save_strategies_section",
    "save_labels_definitions",
    "save_recommendation_settings",
    "save_conflict_rules",
):
    setattr(routes, name, unrelated_section_save)

app = FastAPI()
app.dependency_overrides[deps.get_db] = database
app.dependency_overrides[deps.require_auth] = user
app.dependency_overrides[deps.require_super_admin] = user
app.dependency_overrides[deps.get_user_preferences] = preferences


@app.get("/config/agent-network-status")
async def network_status():
    return {
        "backend": "sandbox",
        "backend_ready": False,
        "sandbox_ready": False,
        "egress_capability": "unavailable",
        "egress_available": False,
        "policy": "blocked",
        "policy_revision": "fixture",
        "full_access_risk": False,
        "local_host_network": False,
        "agent_network_mode": "none",
        "dependency_network_mode": "none",
        "dependency_egress_available": False,
    }


app.include_router(routes.router)


@app.get("/__evidence")
async def evidence():
    keys = ["agent_team_skills_enabled"]
    return {
        "values": {key: rows[key].key_value for key in keys},
        "legacy_values": {key: rows[key].key_value for key in removed},
        "events": events,
    }


@app.get("/version/info")
async def version():
    return {"current_version": "test-fixture", "update_available": False}


@app.get("/favicon.ico", status_code=204)
async def favicon():
    return None


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
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=18763))
    app.state.fixture_server = server
    server.run()
