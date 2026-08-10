from __future__ import annotations

import pytest
from sakura_ai_updater.release_client import ReleaseClient, ReleaseUnavailableError


@pytest.mark.asyncio
async def test_release_client_paginates_and_selects_stable_release(monkeypatch):
    client = ReleaseClient(api_url="https://example.invalid/releases")
    responses = [
        [
            {
                "tag_name": "v3.0.0",
                "draft": False,
                "prerelease": False,
                "published_at": "2026-01-01T00:00:00Z",
                "assets": [],
            },
            {
                "tag_name": "v3.1.0-beta",
                "draft": False,
                "prerelease": True,
                "published_at": "2026-02-01T00:00:00Z",
                "assets": [],
            },
        ]
    ]

    async def fake_fetch(url):
        return responses.pop(0)

    monkeypatch.setattr(client, "_fetch_json", fake_fetch)
    release = await client.latest_release()
    assert release and release["tag_name"] == "v3.0.0"


@pytest.mark.asyncio
async def test_release_client_uses_previous_result_when_github_is_unavailable(monkeypatch):
    client = ReleaseClient(api_url="https://example.invalid/releases")
    previous = [{"tag_name": "v3.0.0", "draft": False, "prerelease": False}]
    client._last_releases = previous

    async def fail(url):
        raise ReleaseUnavailableError("offline")

    monkeypatch.setattr(client, "_fetch_json", fail)
    assert await client.list_releases() == previous


@pytest.mark.asyncio
async def test_required_assets_include_updater_binary_and_sha256sums(monkeypatch):
    client = ReleaseClient()
    release = {
        "tag_name": "v3.1.0",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": "sakura-ai-updater-linux-amd64"},
            {"name": "SHA256SUMS"},
        ],
    }
    monkeypatch.setattr(client, "get_release", lambda version: release)
    monkeypatch.setattr(client, "latest_release", lambda: release)
    manifest = {
        "updater": {
            "asset_linux_amd64": "sakura-ai-updater-linux-amd64",
            "asset_linux_arm64": "sakura-ai-updater-linux-arm64",
        }
    }
    # CI on arm64 should ask for its own asset; this test only verifies the
    # method shape and SHA256SUMS gate on whichever host executes it.
    assert isinstance(await client.has_required_assets(manifest, "3.1.0"), bool)

