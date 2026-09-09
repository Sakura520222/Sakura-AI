from __future__ import annotations

from urllib.error import HTTPError, URLError

import pytest
from loguru import logger

from backend.services import container_registry
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


@pytest.mark.asyncio
async def test_force_refresh_bypasses_fresh_catalog_cache():
    client = ContainerRegistryClient(ttl=3600)
    cached = {
        "repository": client.repository,
        "fetched_at": "2026-08-13T00:00:00Z",
        "stale": False,
        "images": [],
        "heads": {"stable": None, "development": None},
    }
    client._cache = cached
    client._cache_at = float("inf")
    calls = []

    async def token():
        calls.append("token")
        return "registry-token"

    async def tags(_token):
        calls.append("tags")
        return []

    client._token = token
    client._tags = tags

    assert await client.list_images() is cached
    refreshed = await client.list_images(force_refresh=True)
    assert calls == ["token", "tags"]
    assert refreshed is not cached


@pytest.mark.asyncio
async def test_failed_force_refresh_persists_stale_cache_until_success():
    client = ContainerRegistryClient(ttl=3600)
    cached = {
        "repository": client.repository,
        "fetched_at": "2026-08-13T00:00:00Z",
        "stale": False,
        "images": [{"channel": "development", "version": "3.1.0"}],
        "heads": {"stable": None, "development": {"version": "3.1.0"}},
    }
    client._cache = cached
    client._cache_at = float("inf")
    attempts = []

    async def fail():
        attempts.append("token")
        raise ContainerRegistryError("registry unavailable", status_code=503)

    client._token = fail
    failed_refresh = await client.list_images(force_refresh=True)
    assert failed_refresh["stale"] is True
    assert client._cache is failed_refresh

    cached_read = await client.list_images()
    assert cached_read is failed_refresh
    assert cached_read["stale"] is True
    assert attempts == ["token"]

    async def token():
        attempts.append("recovered")
        return "registry-token"

    async def tags(_token):
        return []

    client._token = token
    client._tags = tags
    recovered = await client.list_images(force_refresh=True)
    assert recovered["stale"] is False
    assert client._cache is recovered
    assert attempts == ["token", "recovered"]


def test_request_json_reports_discriminating_failure_cause(monkeypatch):
    client = ContainerRegistryClient()

    def rate_limited(request, timeout=None):
        raise HTTPError(request.full_url, 429, "Too Many Requests", None, None)

    monkeypatch.setattr(container_registry, "urlopen", rate_limited)
    with pytest.raises(ContainerRegistryError, match="HTTP 429") as caught:
        client._request_json("https://ghcr.io/token")
    assert caught.value.status_code == 429

    def dns_failure(request, timeout=None):
        raise URLError("<urlopen error [Errno -2] Name or service not known>")

    monkeypatch.setattr(container_registry, "urlopen", dns_failure)
    with pytest.raises(ContainerRegistryError, match="Name or service not known"):
        client._request_json("https://ghcr.io/token")


@pytest.mark.asyncio
async def test_stale_fallback_surfaces_failure_reason():
    client = ContainerRegistryClient(ttl=0)
    client._cache = {
        "repository": client.repository,
        "fetched_at": "2026-08-13T00:00:00Z",
        "stale": False,
        "images": [{"channel": "stable", "version": "3.0.1"}],
    }
    client._cache_at = 0

    async def fail(*args):
        raise ContainerRegistryError(
            "registry request failed: HTTP 429", status_code=429
        )

    client._token = fail
    warnings: list[str] = []
    handler = logger.add(lambda message: warnings.append(str(message)))
    try:
        payload = await client.list_images()
    finally:
        logger.remove(handler)
    assert payload["stale"] is True
    assert "HTTP 429" in payload["stale_reason"]
    assert any("HTTP 429" in entry for entry in warnings)
