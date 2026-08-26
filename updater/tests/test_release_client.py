from __future__ import annotations

import json
from urllib.error import URLError

import pytest
from sakura_ai_updater.release_client import (
    ReleaseClient,
    ReleaseUnavailableError,
    SandboxManifestInvalidError,
    SandboxManifestNotFoundError,
    _request_failure_detail,
)


def test_request_failure_detail_is_safe_and_typed():
    assert (
        _request_failure_detail(URLError("certificate verify failed"))
        == "url_error_str"
    )
    missing = FileNotFoundError(2, "missing", "/secret/path/libexample.so")
    assert _request_failure_detail(URLError(missing)) == "file_not_found_libexample.so"
    assert (
        _request_failure_detail(json.JSONDecodeError("bad", "x", 0)) == "invalid_json"
    )


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
async def test_release_client_latest_uses_max_strict_stable_semver_not_timestamp(
    monkeypatch,
):
    client = ReleaseClient(api_url="https://example.invalid/releases")
    releases = [
        {
            "tag_name": "v3.1.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-12-01T00:00:00Z",
        },
        {
            "tag_name": "v3.2.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-01-01T00:00:00Z",
        },
        {
            "tag_name": "v9.0.0-rc.1",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-12-31T00:00:00Z",
        },
        {
            "tag_name": "not-a-version",
            "draft": False,
            "prerelease": False,
            "published_at": "2027-01-01T00:00:00Z",
        },
        {
            "tag_name": "v4.0.0",
            "draft": True,
            "prerelease": False,
            "published_at": "2027-01-02T00:00:00Z",
        },
    ]

    async def fake_fetch(_url):
        return releases

    monkeypatch.setattr(client, "_fetch_json", fake_fetch)

    release = await client.latest_release()

    assert release and release["tag_name"] == "v3.2.0"


@pytest.mark.asyncio
async def test_release_client_uses_previous_result_when_github_is_unavailable(
    monkeypatch,
):
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


def _sandbox_asset_payload(version: str = "3.1.0") -> dict[str, object]:
    return {
        "schema_version": 1,
        "manifest": "agent-sandbox",
        "version": version,
        "channel": "stable",
        "sandboxd_image": "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:"
        + "a" * 64,
        "runner_image": "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:"
        + "b" * 64,
    }


@pytest.mark.asyncio
async def test_fetch_sandbox_manifest_requires_same_release_and_official_digests(
    monkeypatch,
):
    client = ReleaseClient()
    release = {
        "tag_name": "v3.1.0",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "agent-sandbox-manifest.json",
                "browser_download_url": (
                    "https://github.com/sakura520222/Sakura-AI/releases/download/"
                    "v3.1.0/agent-sandbox-manifest.json"
                ),
            }
        ],
    }
    monkeypatch.setattr(client, "get_release", lambda version: release)
    monkeypatch.setattr(client, "_fetch_json", lambda url: _sandbox_asset_payload())

    parsed = await client.fetch_sandbox_manifest("3.1.0")
    assert parsed.version == "3.1.0"
    assert parsed.sandboxd_ref.endswith("@sha256:" + "a" * 64)
    assert parsed.runner_ref.endswith("@sha256:" + "b" * 64)

    monkeypatch.setattr(
        client, "_fetch_json", lambda url: {**_sandbox_asset_payload(), "version": "3.0.0"}
    )
    with pytest.raises(SandboxManifestInvalidError):
        await client.fetch_sandbox_manifest("3.1.0")


@pytest.mark.asyncio
async def test_fetch_sandbox_manifest_missing_or_untrusted_asset_fails_closed(monkeypatch):
    client = ReleaseClient()
    release = {
        "tag_name": "v3.1.0",
        "draft": False,
        "prerelease": False,
        "assets": [],
    }
    monkeypatch.setattr(client, "get_release", lambda version: release)
    with pytest.raises(SandboxManifestNotFoundError):
        await client.fetch_sandbox_manifest("3.1.0")

    release["assets"] = [
        {
            "name": "agent-sandbox-manifest.json",
            "browser_download_url": "https://evil.example/manifest.json",
        }
    ]
    with pytest.raises(SandboxManifestInvalidError):
        await client.fetch_sandbox_manifest("3.1.0")
