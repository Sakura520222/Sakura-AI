from __future__ import annotations

import pytest

from backend.services.container_registry import (
    ContainerRegistryClient,
    ContainerRegistryError,
    _group_images,
    parse_development_tag,
    parse_stable_tag,
)


def _digest(char: str) -> str:
    return f"sha256:{char * 64}"


def test_tag_parsers_are_strict_and_capture_identity():
    revision = "a" * 40
    parsed = parse_development_tag(f"dev-20260813040000-v3.0.2-{revision}")
    assert parsed == {
        "channel": "development",
        "version": "3.0.2",
        "revision": revision,
        "created_at": "2026-08-13T04:00:00.000000Z",
        "tag": f"dev-20260813040000-v3.0.2-{revision}",
    }
    assert parse_development_tag("edge") is None
    assert parse_development_tag(f"dev-20261313040000-v3.0.2-{revision}") is None
    assert parse_stable_tag("v3.0.2") == {
        "channel": "stable",
        "version": "3.0.2",
        "tag": "v3.0.2",
    }
    assert parse_stable_tag("latest") is None


def test_digest_grouping_merges_aliases_and_only_marks_aligned_heads():
    revision = "b" * 40
    stable_digest = _digest("1")
    development_digest = _digest("2")
    images = _group_images(
        {
            "latest": stable_digest,
            "v3.0.2": stable_digest,
            "edge": development_digest,
            f"dev-20260813040000-v3.0.2-{revision}": development_digest,
        }
    )
    assert len(images) == 2
    stable = next(item for item in images if item["channel"] == "stable")
    development = next(item for item in images if item["channel"] == "development")
    assert stable["selectable"] is True
    assert stable["tags"] == ["latest", "v3.0.2"]
    assert development["selectable"] is True
    assert "edge" in development["tags"]
    assert development["canonical_tag"].startswith("dev-")

    # A moving alias without a legal immutable alias is visible diagnostics,
    # never a selectable development target.
    legacy = _group_images({"edge": _digest("3")})
    assert legacy == [
        {
            "channel": "unknown",
            "version": None,
            "revision": None,
            "created_at": None,
            "digest": _digest("3"),
            "tags": ["edge"],
            "canonical_tag": "edge",
            "is_channel_head": False,
            "selectable": False,
            "legacy_reason": "invalid_tag",
        }
    ]


def test_registry_client_rejects_user_selected_repository():
    assert ContainerRegistryClient().repository == "ghcr.io/sakura520222/sakura-ai"
    assert (
        ContainerRegistryClient("ghcr.io/sakura520222/sakura-ai").repository
        == "ghcr.io/sakura520222/sakura-ai"
    )
    with pytest.raises(ValueError):
        ContainerRegistryClient("evil.example/sakura-ai")


@pytest.mark.asyncio
async def test_manifest_lookup_ignores_only_explicit_not_found(monkeypatch):
    client = ContainerRegistryClient(ttl=0)

    def missing(*args):
        raise ContainerRegistryError("not found", status_code=404)

    monkeypatch.setattr(client, "_request_json", missing)
    assert await client._manifest_digest("token", "v3.0.2") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_manifest_lookup_non404_failures_are_not_ignored(monkeypatch, status):
    client = ContainerRegistryClient(ttl=0)

    def fail(*args):
        raise ContainerRegistryError("registry failed", status_code=status)

    monkeypatch.setattr(client, "_request_json", fail)
    with pytest.raises(ContainerRegistryError) as caught:
        await client._manifest_digest("token", "v3.0.2")
    assert caught.value.status_code == status


@pytest.mark.asyncio
async def test_manifest_lookup_missing_digest_header_fails_refresh(monkeypatch):
    client = ContainerRegistryClient(ttl=0)
    monkeypatch.setattr(client, "_request_json", lambda *args: ({}, {}))
    with pytest.raises(ContainerRegistryError, match="digest header"):
        await client._manifest_digest("token", "v3.0.2")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_manifest_auth_rate_limit_and_server_errors_fail_refresh(status):
    client = ContainerRegistryClient(ttl=0)

    async def fail(*args):
        raise ContainerRegistryError("registry failed", status_code=status)

    client._token = fail
    with pytest.raises(ContainerRegistryError) as caught:
        await client.list_images()
    assert caught.value.status_code == status


@pytest.mark.asyncio
async def test_cached_catalog_becomes_stale_on_non404_refresh_failure():
    client = ContainerRegistryClient(ttl=0)
    previous = {
        "repository": client.repository,
        "fetched_at": "2026-08-13T00:00:00Z",
        "stale": False,
        "images": [{"channel": "stable", "version": "3.0.1"}],
    }
    client._cache = previous
    client._cache_at = 0

    async def fail(*args):
        raise ContainerRegistryError("forbidden", status_code=403)

    client._token = fail
    payload = await client.list_images()
    assert payload["stale"] is True
    assert payload["images"] == previous["images"]
