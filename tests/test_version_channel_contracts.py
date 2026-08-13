from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.requests import Request

import backend.webui.routes.version as version_routes

ROOT = Path(__file__).parents[1]


def test_template_uses_tabs_safe_dom_and_target_snapshot():
    template = (ROOT / "backend/webui/templates/version_manager.html").read_text(encoding="utf-8")
    assert 'role="tablist"' in template
    assert 'role="tabpanel"' in template
    assert "/version/images" in template
    assert "textContent" in template
    assert "selectedRegistryTarget" in template
    assert "confirm_channel_switch" in template
    assert "registry-panel-other" in template
    assert "CURRENT_BUILD_CHANNEL" in template
    assert "CURRENT_BUILD_CHANNEL !== image.channel" in template
    assert "const candidate" in template
    assert "registryEmpty:" in template
    assert "developmentRisk:" in template
    assert "channelSwitchRisk:" in template
    assert "build.channel" in template
    assert "build.revision" in template
    assert "readinessChecks.innerHTML" not in template


def test_image_route_is_super_admin_and_no_store():
    source = (ROOT / "backend/webui/routes/version.py").read_text(encoding="utf-8")
    start = source.index('@router.get("/version/images")')
    end = source.index('@router.post("/version/check")', start)
    section = source[start:end]
    assert "require_super_admin" in section
    assert "no-store" in section
    assert "registry_unavailable" in section


@pytest.mark.asyncio
async def test_backend_revalidates_registry_head_before_forwarding_target(monkeypatch):
    digest = "sha256:" + "1" * 64
    catalog = {
        "stale": False,
        "heads": {
            "development": {
                "version": "3.0.2",
                "revision": "a" * 40,
                "tag": "dev-20260813040000-v3.0.2-" + "a" * 40,
                "canonical_tag": "dev-20260813040000-v3.0.2-" + "a" * 40,
                "digest": digest,
            }
        },
    }

    class FakeRegistry:
        def __init__(self, repository):
            assert repository

        async def list_images(self):
            return catalog

    monkeypatch.setattr(version_routes, "ContainerRegistryClient", FakeRegistry)
    target = {
        "channel": "development",
        "version": "3.0.2",
        "revision": "a" * 40,
        "tag": "dev-20260813040000-v3.0.2-" + "a" * 40,
        "digest": digest,
    }
    resolved, error = await version_routes._resolve_catalog_target({"target": target})
    assert error is None
    assert resolved == target

    target["digest"] = "sha256:" + "2" * 64
    _resolved, error = await version_routes._resolve_catalog_target({"target": target})
    assert error is not None
    assert error.status_code == 409


@pytest.mark.asyncio
async def test_preflight_accepts_revalidated_development_target(monkeypatch):
    digest = "sha256:" + "3" * 64
    tag = "dev-20260813040000-v3.0.2-" + "d" * 40
    target = {
        "channel": "development",
        "version": "3.0.2",
        "revision": "d" * 40,
        "tag": tag,
        "digest": digest,
    }

    class FakeRegistry:
        def __init__(self, repository):
            pass

        async def list_images(self):
            return {
                "stale": False,
                "heads": {"development": {"version": "3.0.2", "revision": "d" * 40, "tag": tag, "digest": digest}},
            }

    calls = []

    class FakeUpdater:
        async def preflight(self, target_version=None, *, target=None, confirm_channel_switch=False):
            calls.append((target_version, target, confirm_channel_switch))
            return {"data": {"can_update": False}}

    monkeypatch.setattr(version_routes, "ContainerRegistryClient", FakeRegistry)
    monkeypatch.setattr(version_routes, "UpdaterClient", FakeUpdater)
    payload = json.dumps({"target": target}).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/version/preflight",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )
    response = await version_routes.updater_preflight(request, user={}, _csrf="")
    assert response.status_code == 200
    assert calls == [(None, target, False)]


@pytest.mark.asyncio
async def test_backend_revalidates_and_forwards_stable_head_target(monkeypatch):
    digest = "sha256:" + "4" * 64
    target = {
        "channel": "stable",
        "version": "3.0.2",
        "tag": "v3.0.2",
        "digest": digest,
    }

    class FakeRegistry:
        def __init__(self, repository):
            assert repository

        async def list_images(self):
            return {
                "stale": False,
                "heads": {"stable": {**target, "canonical_tag": target["tag"]}},
            }

    calls = []

    class FakeUpdater:
        async def preflight(self, target_version=None, *, target=None, confirm_channel_switch=False):
            calls.append((target_version, target, confirm_channel_switch))
            return {"data": {"can_update": True}}

    monkeypatch.setattr(version_routes, "ContainerRegistryClient", FakeRegistry)
    monkeypatch.setattr(version_routes, "UpdaterClient", FakeUpdater)
    resolved, error = await version_routes._resolve_catalog_target({"target": target})
    assert error is None
    assert resolved == target

    payload = json.dumps({"target": target, "confirm_channel_switch": True}).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/version/preflight",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )
    response = await version_routes.updater_preflight(request, user={}, _csrf="")
    assert response.status_code == 200
    assert calls == [(None, target, True)]
