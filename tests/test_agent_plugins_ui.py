import json
import re

import httpx
import pytest

from backend.services.agent_team.plugin_config import MASK, PLUGIN_KEY
from tests.harness_plugins_ui_app import create_app


@pytest.fixture
def app(monkeypatch):
    return create_app(monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lang,title", [("en", "Agent Plugins"), ("zh-CN", "Agent 插件")]
)
async def test_real_page_bilingual_mask_save_reload(app, lang, title):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        page = await client.get(f"/agent-plugins/?lang={lang}")
        assert page.status_code == 200
        assert title in page.text
        assert "fixture-header-token" not in page.text
        assert MASK in page.text
        assert "/agent-skills/" in page.text
        assert "agent_plugins." not in page.text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
        public = (await client.get("/agent-plugins/config")).json()
        public["plugins"]["mcp"]["servers"][0]["enabled"] = False
        result = await client.post(
            "/agent-plugins/save",
            data={
                "csrf_token": csrf,
                "profile": "workspace_write",
                "plugins": json.dumps(public["plugins"]),
            },
        )
        assert result.status_code == 302
        stored = json.loads(app.state.rows[PLUGIN_KEY].key_value)
        assert (
            stored["mcp"]["servers"][0]["headers"]["Authorization"]
            == "fixture-header-token"
        )
        assert stored["mcp"]["servers"][0]["enabled"] is False
        after = (await client.get("/agent-plugins/config")).json()
        assert after["profile"] == "workspace_write"
        assert not after["plugins"]["mcp"]["servers"][0]["enabled"]
        assert "fixture-header-token" not in json.dumps(app.state.events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,method",
    [
        ("/agent-plugins/", "GET"),
        ("/agent-plugins/config", "GET"),
        ("/agent-plugins/save", "POST"),
    ],
)
@pytest.mark.parametrize(
    "role,expected", [("admin", 403), ("user", 403), ("anonymous", 401)]
)
async def test_real_role_boundary(app, path, method, role, expected):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.request(method, path, headers={"x-fixture-role": role})
        assert result.status_code == expected
        assert not app.state.events


@pytest.mark.asyncio
async def test_bad_csrf_invalid_config_and_generic_cannot_mutate(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        page = await client.get("/agent-plugins/")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
        original = app.state.rows[PLUGIN_KEY].key_value
        bad_csrf = await client.post(
            "/agent-plugins/save",
            data={"csrf_token": "invalid", "plugins": "{}", "profile": "autonomous"},
        )
        assert bad_csrf.status_code == 403
        assert not app.state.events
        for raw, profile in [
            ("not-json-secret", "autonomous"),
            ("{}", "invalid"),
            ('{"version":2}', "autonomous"),
        ]:
            result = await client.post(
                "/agent-plugins/save",
                data={"csrf_token": csrf, "plugins": raw, "profile": profile},
            )
            assert "_toast_type=error" in result.headers["location"]
            assert "not-json-secret" not in str(result.headers)
            assert app.state.rows[PLUGIN_KEY].key_value == original
        for key in (
            PLUGIN_KEY,
            "agent_team_permission_profile",
            "agent_team_mcp_io_timeout_seconds",
        ):
            result = await client.post(
                "/config/general/save", data={"csrf_token": csrf, key: "invalid"}
            )
            assert "_toast_type=error" in result.headers["location"]
            assert app.state.rows[PLUGIN_KEY].key_value == original
        assert "commit" not in app.state.events


@pytest.mark.asyncio
async def test_add_delete_hook_and_mcp(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        page = await client.get("/agent-plugins/")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
        value = {
            "version": 1,
            "mcp": {"servers": []},
            "hooks": [{"id": "audit", "event": "before_finish", "kind": "audit"}],
        }
        result = await client.post(
            "/agent-plugins/save",
            data={
                "csrf_token": csrf,
                "profile": "autonomous",
                "plugins": json.dumps(value),
            },
        )
        assert "_toast_type=error" not in result.headers["location"]
        data = (await client.get("/agent-plugins/config")).json()["plugins"]
        assert data["hooks"][0]["id"] == "audit" and not data["mcp"]["servers"]
        value["hooks"] = []
        await client.post(
            "/agent-plugins/save",
            data={
                "csrf_token": csrf,
                "profile": "autonomous",
                "plugins": json.dumps(value),
            },
        )
        assert not (await client.get("/agent-plugins/config")).json()["plugins"][
            "hooks"
        ]
