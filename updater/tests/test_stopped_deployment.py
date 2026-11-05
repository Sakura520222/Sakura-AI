"""Offline identity is verified independently from the running-container gate."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sakura_ai_updater.contract import REPOSITORIES
from sakura_ai_updater.deployment import DeploymentError, DeploymentStateProvider

DIGEST = "sha256:" + "a" * 64
IMAGE_ID = "sha256:" + "b" * 64
REVISION = "c" * 40


@pytest.fixture
def stopped(tmp_path, monkeypatch):
    def factory(channel="development", status="missing", alias=False):
        tag = "v3.2.5" if channel == "stable" else f"dev-20261104080949-v3.2.5-{REVISION}"
        if alias:
            tag = "latest" if channel == "stable" else "edge"
        image = f"{REPOSITORIES['web']}:{tag}@{DIGEST}"
        env = tmp_path / "deployment.env"
        env.write_text(f"SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE={image}\n", encoding="utf-8")
        metadata = {
            "Id": IMAGE_ID,
            "RepoDigests": [f"{REPOSITORIES['web']}@{DIGEST}"],
            "Config": {"Labels": {
                "org.opencontainers.image.version": "3.2.5",
                "org.opencontainers.image.revision": REVISION,
                "com.sakura-ai.build.channel": channel,
                "com.sakura-ai.component": "web",
            }},
        }
        provider = DeploymentStateProvider(str(env))
        monkeypatch.setattr(provider, "_health_payload", AsyncMock(return_value=None))
        monkeypatch.setattr(provider, "sandbox_runtime_identity", AsyncMock(return_value={"runtime_verified": False}))
        commands = []

        async def command(argv):
            commands.append(tuple(argv))
            if argv[:3] == ["docker", "container", "ls"]:
                return ("" if status == "missing" else "d" * 64), ""
            if argv[:3] == ["docker", "container", "inspect"]:
                return json.dumps({"Name": "/sakura-ai", "Image": IMAGE_ID,
                                   "State": {"Running": status == "running", "Status": status}}), ""
            if argv[:3] == ["docker", "image", "inspect"]:
                return json.dumps(metadata), ""
            if argv[:2] == ["docker", "inspect"]:
                if status == "missing":
                    raise DeploymentError("no such object: sakura-ai")
                return IMAGE_ID, ""
            raise AssertionError(argv)

        monkeypatch.setattr(provider, "_run_docker_command", command)
        return provider, metadata, commands

    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["stable", "development"])
@pytest.mark.parametrize("status", ["missing", "exited", "created"])
@pytest.mark.parametrize("alias", [False, True])
async def test_stopped_identity_and_rollback_digest_need_no_live_health(stopped, channel, status, alias):
    provider, _, _ = stopped(channel, status, alias)
    before = Path(provider.deployment_env).read_bytes()

    state = await provider.current_state()

    assert state["current_channel"] == channel
    assert state["current_version"] == "3.2.5"
    assert state["current_revision"] == REVISION
    assert state["identity_source"] == "stopped_image"
    assert state["running_container_digest"] is None
    assert state["from_digest"] == await provider.capture_rollback_digest() == DIGEST
    assert await provider.resolve_current_version() is None
    assert Path(provider.deployment_env).read_bytes() == before


@pytest.mark.asyncio
async def test_missing_container_does_not_weaken_post_activation_verification(stopped):
    provider, _, _ = stopped()

    assert await provider.capture_rollback_digest() == DIGEST
    with pytest.raises(DeploymentError, match="no such object"):
        await provider.capture_from_digest()


@pytest.mark.asyncio
@pytest.mark.parametrize(("label", "value"), [
    ("org.opencontainers.image.version", "3.2.4"),
    ("org.opencontainers.image.revision", "e" * 40),
    ("org.opencontainers.image.revision", "short"),
    ("com.sakura-ai.build.channel", "stable"),
    ("com.sakura-ai.build.channel", []),
    ("com.sakura-ai.component", "sandboxd"),
])
async def test_stopped_identity_rejects_labels_inconsistent_with_pinned_tag(stopped, label, value):
    provider, metadata, _ = stopped()
    metadata["Config"]["Labels"][label] = value

    with pytest.raises(DeploymentError):
        await provider.current_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "exited", "missing"])
async def test_mismatched_digest_never_becomes_an_offline_baseline(stopped, status):
    provider, metadata, _ = stopped(status=status)
    metadata["RepoDigests"] = [f"{REPOSITORIES['web']}@sha256:{'e' * 64}"]

    with pytest.raises(DeploymentError, match="do not match pinned"):
        await provider.current_state()


@pytest.mark.asyncio
async def test_stopped_container_must_match_saved_image(stopped):
    provider, metadata, _ = stopped(status="exited")
    metadata["Id"] = "sha256:" + "e" * 64

    with pytest.raises(DeploymentError, match="stopped container does not match"):
        await provider.current_state()


@pytest.mark.asyncio
async def test_running_unhealthy_container_does_not_borrow_offline_channel(stopped):
    provider, _, _ = stopped(status="running")

    state = await provider.current_state()

    assert state["identity_source"] == "running"
    assert state["current_channel"] is None
    assert state["running_container_digest"] == DIGEST


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paused", "restarting", "removing"])
async def test_transitional_container_is_not_a_stopped_baseline(stopped, status):
    provider, _, _ = stopped(status=status)

    with pytest.raises(DeploymentError, match="cannot verify stopped"):
        await provider.current_state()


@pytest.mark.asyncio
async def test_docker_failure_is_not_treated_as_container_absence(stopped, monkeypatch):
    provider, _, _ = stopped()
    monkeypatch.setattr(provider, "_run_docker_command", AsyncMock(side_effect=DeploymentError("Docker unavailable")))

    with pytest.raises(DeploymentError, match="Docker unavailable"):
        await provider.current_state()
