from __future__ import annotations

import pytest
from sakura_ai_updater.registry import (
    DevelopmentTarget,
    RegistryTargetError,
    StableTarget,
    parse_development_target,
    parse_stable_target,
)


def _target() -> dict[str, str]:
    return {
        "channel": "development",
        "version": "3.0.2",
        "revision": "a" * 40,
        "tag": "dev-20260813040000-v3.0.2-" + "a" * 40,
        "digest": "sha256:" + "b" * 64,
    }


def test_development_target_requires_tag_identity_and_digest_pin():
    parsed = parse_development_target(_target())
    assert isinstance(parsed, DevelopmentTarget)
    assert parsed.image.endswith("@sha256:" + "b" * 64)

    for field, value in (
        ("channel", "stable"),
        ("tag", "edge"),
        ("revision", "B" * 40),
        ("digest", "sha256:bad"),
        ("repository", "evil.example/sakura-ai"),
    ):
        candidate = _target()
        candidate[field] = value
        with pytest.raises(RegistryTargetError):
            parse_development_target(candidate)


def test_development_target_rejects_tag_version_or_revision_mismatch():
    candidate = _target()
    candidate["version"] = "3.0.1"
    with pytest.raises(RegistryTargetError):
        parse_development_target(candidate)


def test_stable_target_requires_exact_semver_tag_and_digest_pin():
    candidate = {
        "channel": "stable",
        "version": "3.0.2",
        "tag": "v3.0.2",
        "digest": "sha256:" + "c" * 64,
    }
    parsed = parse_stable_target(candidate)
    assert isinstance(parsed, StableTarget)
    assert parsed.image.endswith("v3.0.2@sha256:" + "c" * 64)
    for field, value in (
        ("version", "v3.0.2"),
        ("tag", "latest"),
        ("digest", "sha256:bad"),
        ("channel", "development"),
    ):
        invalid = candidate | {field: value}
        with pytest.raises(RegistryTargetError):
            parse_stable_target(invalid)


def test_manifest_request_accepts_oci_and_docker_multiarch_indexes(monkeypatch):
    from sakura_ai_updater.registry import RegistryClient

    digest = "sha256:" + "f" * 64
    seen = {}

    def request(url, headers):
        seen["url"] = url
        seen["headers"] = headers
        return {}, {"docker-content-digest": digest}

    client = RegistryClient()
    monkeypatch.setattr(client, "_json_sync", request)
    assert client._manifest_sync("edge", "token") == digest
    accept = seen["headers"]["Accept"]
    assert "application/vnd.oci.image.index.v1+json" in accept
    assert "application/vnd.docker.distribution.manifest.list.v2+json" in accept


@pytest.mark.asyncio
async def test_stable_registry_target_must_match_latest_head(monkeypatch):
    from sakura_ai_updater.registry import RegistryClient

    target = parse_stable_target(
        {
            "channel": "stable",
            "version": "3.0.2",
            "tag": "v3.0.2",
            "digest": "sha256:" + "c" * 64,
        }
    )
    client = RegistryClient()
    monkeypatch.setattr(
        client,
        "_json_sync",
        lambda url, headers: ({"token": "token"}, {}),
    )
    calls = []

    def manifest(tag, token):
        calls.append(tag)
        return target.digest if tag == target.tag else "sha256:" + "d" * 64

    monkeypatch.setattr(client, "_manifest_sync", manifest)
    with pytest.raises(RegistryTargetError, match="current latest head"):
        await client.verify_target(target)
    assert calls == [target.tag, "latest"]
