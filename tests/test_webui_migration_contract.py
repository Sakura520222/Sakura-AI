"""Keep the React migration complete without changing the mounted API surface."""

import ast
from collections import Counter
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.api.v1 import api_v1_router
from backend.webui.routes.entrypoints import router as entrypoints_router

ROOT = Path(__file__).resolve().parents[1]
RETIRED_ROUTES = {
    "action_logs",
    "activity",
    "activity_access",
    "activity_observability",
    "agent_plugins",
    "agent_skills",
    "agent_team",
    "announcements",
    "billing",
    "config",
    "config_center",
    "dashboard",
    "github_app",
    "issues",
    "legal",
    "logs",
    "notifications",
    "pr",
    "queue",
    "repos",
    "sakura_memory",
    "scans",
    "security",
    "settings",
    "sse",
    "system_config",
    "users",
    "vector_db",
    "version",
}


def test_retired_page_modules_and_template_engine_are_absent():
    routes = ROOT / "backend/webui/routes"
    assert not [name for name in RETIRED_ROUTES if (routes / f"{name}.py").exists()]
    assert not (ROOT / "backend/webui/time_filters.py").exists()
    assert not list((ROOT / "backend/webui/templates").rglob("*.html"))
    assert not list((ROOT / "backend/webui/assets/settings").rglob("*.js"))
    for path in (ROOT / "backend/webui").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"fastapi.templating", "jinja2"}, path
            if isinstance(node, ast.FunctionDef):
                assert node.name not in {
                    "get_templates",
                    "render_template",
                    "error_page",
                }, path


def test_versioned_api_has_no_duplicate_method_path_registrations():
    app = FastAPI()
    app.include_router(api_v1_router, prefix="/api/v1")

    def registrations_in(routes, prefix=""):
        for route in routes:
            if isinstance(route, APIRoute):
                for method in route.methods:
                    yield method, prefix + route.path
            else:
                # FastAPI can retain included routers instead of flattening them.
                included = getattr(route, "original_router", None)
                if included is not None:
                    context = route.include_context
                    yield from registrations_in(
                        included.routes, prefix + context.prefix
                    )

    registrations = Counter(registrations_in(app.routes))
    assert ("GET", "/api/v1/webui/bootstrap") in registrations
    assert len(registrations) > 200
    assert not {key: count for key, count in registrations.items() if count > 1}


@pytest.mark.parametrize(
    ("source", "destination"),
    [
        ("/auth/login", "/app/login"),
        ("/auth/2fa", "/app/login"),
        ("/setup/verify", "/app/setup"),
        ("/logs", "/app/reviews"),
        ("/pr/42", "/app/reviews/42"),
        ("/pr/42/files", "/app/reviews/42/files"),
        ("/issues/42", "/app/issues/42"),
        ("/agent-team?task_id=42&lang=en", "/app/agent/42?lang=en"),
        ("/github-app", "/app/github-app"),
        ("/billing/credits", "/app/billing/credits"),
        ("/vector-db/collection/example", "/app/admin/storage/example"),
    ],
)
def test_saved_links_resolve_to_the_react_application(source, destination):
    app = FastAPI()
    app.include_router(entrypoints_router)
    with TestClient(app) as client:
        response = client.get(source, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == destination
    assert response.headers["cache-control"] == "no-store"


def test_saved_links_do_not_forward_setup_or_session_credentials():
    app = FastAPI()
    app.include_router(entrypoints_router)
    with TestClient(app) as client:
        response = client.get(
            "/setup?token=secret&access_token=secret&csrf_token=secret&lang=en",
            follow_redirects=False,
        )
    assert response.headers["location"] == "/app/setup?lang=en"
    assert "secret" not in response.headers["location"]
