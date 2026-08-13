from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.requests import Request

import backend.webui.routes.version as version_routes

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_backend_channel_switch_confirmation_requires_json_boolean(value):
    parsed, error = version_routes._confirm_channel_switch(
        {"confirm_channel_switch": value}
    )
    assert parsed is None
    assert error is not None
    assert error.status_code == 422
    assert json.loads(error.body) == {"error": "invalid_confirm_channel_switch"}


def test_backend_channel_switch_confirmation_defaults_to_false_and_accepts_bool():
    parsed, error = version_routes._confirm_channel_switch({})
    assert parsed is False
    assert error is None
    parsed, error = version_routes._confirm_channel_switch({"confirm_channel_switch": True})
    assert parsed is True
    assert error is None


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


def test_all_channel_readiness_checks_are_localized_in_both_languages():
    template = (ROOT / "backend/webui/templates/version_manager.html").read_text(encoding="utf-8")
    translations = [
        (ROOT / "backend/webui/translations/zh-CN.yaml").read_text(encoding="utf-8"),
        (ROOT / "backend/webui/translations/en.yaml").read_text(encoding="utf-8"),
    ]
    labels = {
        "target_identity_valid": "check_target_identity_valid",
        "target_channel_head": "check_target_channel_head",
        "channel_switch_confirmed": "check_channel_switch_confirmed",
        "registry_digest_matches": "check_registry_digest_matches",
        "already_current": "check_already_current",
    }
    for check_name, translation_key in labels.items():
        assert f"{check_name}:" in template
        assert f'version_manager.{translation_key}' in template
        for locale in translations:
            assert f"  {translation_key}:" in locale


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


@pytest.mark.asyncio
async def test_structured_update_audit_records_target_version(monkeypatch):
    target = {
        "channel": "development",
        "version": "3.1.0",
        "revision": "a" * 40,
        "tag": "dev-20260813040000-v3.1.0-" + "a" * 40,
        "digest": "sha256:" + "5" * 64,
    }

    async def resolve(body):
        assert body["target"] == target
        return target, None

    expected_target = target

    class FakeUpdater:
        async def update(self, target_version=None, *, target=None, confirm_channel_switch=False):
            assert target_version is None
            assert target == expected_target
            assert confirm_channel_switch is True
            return {"data": {"job_id": "upd_audit"}}

    audit_calls = []

    async def audit(*args):
        audit_calls.append(args)

    monkeypatch.setattr(version_routes, "_resolve_catalog_target", resolve)
    monkeypatch.setattr(version_routes, "UpdaterClient", FakeUpdater)
    monkeypatch.setattr(version_routes, "log_admin_action", audit)
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
            "path": "/version/update",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )
    response = await version_routes.updater_update(
        request,
        db=object(),
        user={"user_id": 7},
        _csrf="",
    )
    assert response.status_code == 202
    assert len(audit_calls) == 1
    details = audit_calls[0][5]
    assert details["target_version"] == "3.1.0"
    assert details["target_channel"] == "development"
