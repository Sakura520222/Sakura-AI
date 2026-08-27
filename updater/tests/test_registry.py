from __future__ import annotations

import pytest
from sakura_ai_updater.registry import (
    DevelopmentSandboxPair,
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


def _image_manifest(config_digest: str) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": 1,
        },
        "layers": [],
    }


def _registry_image_mock(
    monkeypatch,
    client,
    target: DevelopmentTarget,
    *,
    mutate_manifest=None,
    mutate_config=None,
    manifest_digest=None,
    missing_digest_header: bool = False,
) -> list[tuple[str, str]]:
    from sakura_ai_updater.registry import RUNNER_REPOSITORY, SANDBOXD_REPOSITORY

    digests = {
        SANDBOXD_REPOSITORY: "sha256:" + "a" * 64,
        RUNNER_REPOSITORY: "sha256:" + "b" * 64,
    }
    config_digests = {
        SANDBOXD_REPOSITORY: "sha256:" + "c" * 64,
        RUNNER_REPOSITORY: "sha256:" + "d" * 64,
    }
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        client,
        "_token_sync",
        lambda repository: f"token-{repository.rsplit('/', 1)[-1]}",
    )

    def json_sync(url: str, headers: dict[str, str]):
        repository = next(
            (
                candidate
                for candidate in (SANDBOXD_REPOSITORY, RUNNER_REPOSITORY)
                if url.startswith(f"https://ghcr.io/v2/{candidate.removeprefix('ghcr.io/')}/")
            ),
            None,
        )
        assert repository is not None
        path = url.split(f"/v2/{repository.removeprefix('ghcr.io/')}/", 1)[1]
        if path.startswith("manifests/"):
            reference = path.split("manifests/", 1)[1]
            calls.append((repository, reference))
            payload = _image_manifest(config_digests[repository])
            if mutate_manifest is not None:
                payload = mutate_manifest(repository, reference, payload)
            response_digest = digests[repository]
            if manifest_digest is not None:
                response_digest = manifest_digest(repository, reference, response_digest)
            response_headers = (
                {}
                if missing_digest_header
                else {"docker-content-digest": response_digest}
            )
            return payload, response_headers
        if path.startswith("blobs/"):
            digest = path.split("blobs/", 1)[1]
            labels = {
                "org.opencontainers.image.revision": target.revision,
                "org.opencontainers.image.version": target.version,
                "com.sakura-ai.component": (
                    "sandboxd" if repository == SANDBOXD_REPOSITORY else "agent-runner"
                ),
                "com.sakura-ai.build.channel": target.channel,
            }
            payload: dict[str, object] = {"config": {"Labels": labels}}
            if mutate_config is not None:
                payload = mutate_config(repository, digest, payload)
            return payload, {}
        raise AssertionError(f"unexpected registry URL: {url}")

    monkeypatch.setattr(client, "_json_sync", json_sync)
    return calls


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


@pytest.mark.asyncio
async def test_stable_resolver_returns_target_only_when_tag_and_latest_match(monkeypatch):
    from sakura_ai_updater.registry import RegistryClient

    digest = "sha256:" + "c" * 64
    client = RegistryClient()
    calls: list[str] = []
    monkeypatch.setattr(client, "_token_sync", lambda repository: "token")

    def manifest(tag, token):
        assert token == "token"
        calls.append(tag)
        return digest

    monkeypatch.setattr(client, "_manifest_sync", manifest)
    target = await client.resolve_stable_target("3.0.2")
    assert target == parse_stable_target(
        {
            "channel": "stable",
            "version": "3.0.2",
            "tag": "v3.0.2",
            "digest": digest,
        }
    )
    assert sorted(calls) == ["latest", "v3.0.2"]


@pytest.mark.asyncio
async def test_stable_resolver_rejects_tag_alias_digest_mismatch(monkeypatch):
    from sakura_ai_updater.registry import RegistryClient

    client = RegistryClient()
    monkeypatch.setattr(client, "_token_sync", lambda repository: "token")
    monkeypatch.setattr(
        client,
        "_manifest_sync",
        lambda tag, token: (
            "sha256:" + "c" * 64 if tag == "v3.0.2" else "sha256:" + "d" * 64
        ),
    )
    with pytest.raises(RegistryTargetError, match="latest head"):
        await client.resolve_stable_target("3.0.2")


@pytest.mark.asyncio
async def test_development_web_target_requires_full_oci_identity_labels(monkeypatch):
    from sakura_ai_updater.registry import REPOSITORY, RegistryClient

    target = parse_development_target(_target())
    client = RegistryClient()
    config_digest = "sha256:" + "e" * 64
    manifest_digest = target.digest
    labels = {
        "org.opencontainers.image.revision": target.revision,
        "com.sakura-ai.component": "web",
        "com.sakura-ai.build.channel": "development",
        "org.opencontainers.image.version": target.version,
    }

    monkeypatch.setattr(client, "_token_sync", lambda repository: "token")
    monkeypatch.setattr(
        client,
        "_manifest_sync",
        lambda reference, token: manifest_digest,
    )
    monkeypatch.setattr(
        client,
        "_manifest_response_sync",
        lambda repository, reference, token: (
            _image_manifest(config_digest),
            {"docker-content-digest": manifest_digest},
        ),
    )
    monkeypatch.setattr(
        client,
        "_config_response_sync",
        lambda repository, digest, token: ({"config": {"Labels": labels}}, {}),
    )

    assert await client.verify_target(target) == target
    assert labels == {
        "org.opencontainers.image.revision": target.revision,
        "com.sakura-ai.component": "web",
        "com.sakura-ai.build.channel": "development",
        "org.opencontainers.image.version": target.version,
    }
    assert REPOSITORY == target.repository


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("org.opencontainers.image.revision", "b" * 40),
        ("com.sakura-ai.component", "sandboxd"),
        ("com.sakura-ai.build.channel", "stable"),
        ("org.opencontainers.image.version", "3.0.1"),
    ],
)
async def test_development_web_target_rejects_wrong_oci_identity_label(
    monkeypatch, label, value
):
    from sakura_ai_updater.registry import RegistryClient

    target = parse_development_target(_target())
    client = RegistryClient()
    config_digest = "sha256:" + "e" * 64
    labels = {
        "org.opencontainers.image.revision": target.revision,
        "com.sakura-ai.component": "web",
        "com.sakura-ai.build.channel": "development",
        "org.opencontainers.image.version": target.version,
    }
    labels[label] = value
    monkeypatch.setattr(client, "_token_sync", lambda repository: "token")
    monkeypatch.setattr(
        client,
        "_manifest_sync",
        lambda reference, token: target.digest,
    )
    monkeypatch.setattr(
        client,
        "_manifest_response_sync",
        lambda repository, reference, token: (
            _image_manifest(config_digest),
            {"docker-content-digest": target.digest},
        ),
    )
    monkeypatch.setattr(
        client,
        "_config_response_sync",
        lambda repository, digest, token: ({"config": {"Labels": labels}}, {}),
    )

    with pytest.raises(RegistryTargetError):
        await client.verify_target(target)


@pytest.mark.asyncio
async def test_development_sandbox_pair_requires_same_full_revision_tags(monkeypatch):
    from sakura_ai_updater.registry import (
        RUNNER_REPOSITORY,
        SANDBOXD_REPOSITORY,
        RegistryClient,
    )

    target = parse_development_target(_target())
    client = RegistryClient()
    calls = _registry_image_mock(monkeypatch, client, target)
    pair = await client.resolve_development_sandbox_pair(target)
    assert isinstance(pair, DevelopmentSandboxPair)
    assert pair.revision == target.revision
    assert pair.sandboxd_ref == f"{SANDBOXD_REPOSITORY}@sha256:" + "a" * 64
    assert pair.runner_ref == f"{RUNNER_REPOSITORY}@sha256:" + "b" * 64
    assert sorted(calls) == sorted(
        [
            (SANDBOXD_REPOSITORY, target.tag),
            (SANDBOXD_REPOSITORY, f"sha-{target.revision}"),
            (RUNNER_REPOSITORY, target.tag),
            (RUNNER_REPOSITORY, f"sha-{target.revision}"),
        ]
    )


@pytest.mark.asyncio
async def test_development_sandbox_pair_rejects_tag_digest_mismatch(monkeypatch):
    from sakura_ai_updater.registry import SANDBOXD_REPOSITORY, RegistryClient

    target = parse_development_target(_target())
    client = RegistryClient()
    _registry_image_mock(
        monkeypatch,
        client,
        target,
        manifest_digest=lambda repository, reference, digest: (
            "sha256:" + "a" * 64
            if repository == SANDBOXD_REPOSITORY and reference == target.tag
            else "sha256:" + "b" * 64
        ),
    )
    with pytest.raises(RegistryTargetError, match="do not resolve"):
        await client.resolve_development_sandbox_pair(target)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "missing_revision",
        "wrong_revision",
        "wrong_component",
        "missing_config",
        "bad_config_digest",
    ],
)
async def test_development_sandbox_pair_rejects_invalid_oci_identity(
    monkeypatch, mutation
):
    from sakura_ai_updater.registry import RegistryClient

    target = parse_development_target(_target())
    client = RegistryClient()

    def mutate_config(repository, digest, payload):
        if mutation == "missing_revision":
            payload["config"]["Labels"].pop("org.opencontainers.image.revision")
        elif mutation == "wrong_revision":
            payload["config"]["Labels"]["org.opencontainers.image.revision"] = "b" * 40
        elif mutation == "wrong_component":
            payload["config"]["Labels"]["com.sakura-ai.component"] = "web"
        return payload

    def mutate_manifest(repository, reference, payload):
        if mutation == "missing_config":
            payload.pop("config")
        elif mutation == "bad_config_digest":
            payload["config"]["digest"] = "sha256:bad"
        return payload

    _registry_image_mock(
        monkeypatch,
        client,
        target,
        mutate_manifest=mutate_manifest,
        mutate_config=mutate_config,
    )
    with pytest.raises(RegistryTargetError):
        await client.resolve_development_sandbox_pair(target)


@pytest.mark.asyncio
async def test_development_sandbox_pair_rejects_missing_manifest_digest(monkeypatch):
    from sakura_ai_updater.registry import RegistryClient

    target = parse_development_target(_target())
    client = RegistryClient()
    _registry_image_mock(monkeypatch, client, target, missing_digest_header=True)
    with pytest.raises(RegistryTargetError, match="manifest digest"):
        await client.resolve_development_sandbox_pair(target)
