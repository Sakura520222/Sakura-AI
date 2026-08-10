from __future__ import annotations

import asyncio

import pytest
from sakura_ai_updater.deployment import DeploymentStateProvider


class _Process:
    returncode = 0

    async def communicate(self):
        return b"sha256:abc\n", b""

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_latest_materializes_to_concrete_tag_and_digest(tmp_path, monkeypatch):
    env = tmp_path / "deployment.env"
    env.write_text(
        "SAKURA_AI_IMAGE=ghcr.io/example/app:latest\nSAKURA_DEPLOY_MODE=image\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        DeploymentStateProvider,
        "_read_health_sync",
        staticmethod(lambda url, timeout: (200, {"version": "3.0.0"})),
    )

    async def fake_exec(*argv, **kwargs):
        assert tuple(argv) == ("docker", "inspect", "--format={{.Image}}", "sakura-ai")
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    provider = DeploymentStateProvider(str(env))
    concrete = await provider.materialize_current_anchor()
    assert concrete == "ghcr.io/example/app:v3.0.0@sha256:abc"
    assert "SAKURA_AI_IMAGE=ghcr.io/example/app:v3.0.0@sha256:abc" in env.read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_concrete_image_is_not_materialized(tmp_path, monkeypatch):
    env = tmp_path / "deployment.env"
    env.write_text("SAKURA_AI_IMAGE=ghcr.io/example/app:v3.0.0\n", encoding="utf-8")

    async def fail_inspect(*argv, **kwargs):
        raise AssertionError("concrete refs must not inspect running container")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_inspect)
    provider = DeploymentStateProvider(str(env))
    assert await provider.materialize_current_anchor() == "ghcr.io/example/app:v3.0.0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image_ref",
    [
        "ghcr.io/example/app:latest@sha256:abc",
        "ghcr.io/example/app:v3.0.0@sha256:abc",
    ],
)
async def test_digest_pinned_image_is_not_materialized(
    tmp_path, monkeypatch, image_ref
):
    """Any existing digest, including ``:latest@digest``, is already immutable."""

    env = tmp_path / "deployment.env"
    env.write_text(f"SAKURA_AI_IMAGE={image_ref}\n", encoding="utf-8")

    def fail_health(*args, **kwargs):
        raise AssertionError("digest-pinned refs must not query /health")

    async def fail_inspect(*argv, **kwargs):
        raise AssertionError("digest-pinned refs must not inspect running container")

    monkeypatch.setattr(DeploymentStateProvider, "_read_health_sync", staticmethod(fail_health))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_inspect)
    provider = DeploymentStateProvider(str(env))
    assert await provider.materialize_current_anchor() == image_ref
    assert env.read_text(encoding="utf-8") == f"SAKURA_AI_IMAGE={image_ref}\n"


def test_deployment_env_mode_takes_precedence(tmp_path, monkeypatch):
    env = tmp_path / "deployment.env"
    env.write_text("SAKURA_DEPLOY_MODE=image\n", encoding="utf-8")
    monkeypatch.setenv("SAKURA_DEPLOY_MODE", "source")
    assert DeploymentStateProvider(str(env)).read_deploy_mode() == "image"


@pytest.mark.asyncio
async def test_concrete_tag_still_uses_health_version_authority(tmp_path, monkeypatch):
    env = tmp_path / "deployment.env"
    env.write_text("SAKURA_AI_IMAGE=ghcr.io/example/app:v3.0.0\n", encoding="utf-8")
    monkeypatch.setattr(
        DeploymentStateProvider,
        "_read_health_sync",
        staticmethod(lambda url, timeout: (200, {"version": "3.1.0"})),
    )
    provider = DeploymentStateProvider(str(env))
    assert await provider.resolve_current_version() == "3.1.0"


@pytest.mark.asyncio
async def test_current_state_reports_running_digest_and_from_digest(tmp_path, monkeypatch):
    env = tmp_path / "deployment.env"
    env.write_text(
        "SAKURA_AI_IMAGE=ghcr.io/example/app:v3.0.0\nSAKURA_DEPLOY_MODE=image\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        DeploymentStateProvider,
        "_read_health_sync",
        staticmethod(lambda url, timeout: (200, {"version": "3.0.0"})),
    )

    async def fake_exec(*argv, **kwargs):
        assert tuple(argv) == ("docker", "inspect", "--format={{.Image}}", "sakura-ai")
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    state = await DeploymentStateProvider(str(env)).current_state()
    assert state["from_digest"] == "sha256:abc"
    assert state["running_container_digest"] == "sha256:abc"
    assert state["current_version"] == "3.0.0"


@pytest.mark.asyncio
async def test_inspect_cancel_kills_and_reaps_process(tmp_path, monkeypatch):
    env = tmp_path / "deployment.env"
    env.write_text("SAKURA_AI_IMAGE=ghcr.io/example/app:v3.0.0\n", encoding="utf-8")
    started = asyncio.Event()
    released = asyncio.Event()
    process_ref: list[_Process] = []

    class HangingProcess(_Process):
        async def communicate(self):
            started.set()
            if self.returncode != -9:
                await released.wait()
            return b"", b""

        def kill(self):
            self.returncode = -9
            released.set()

    async def fake_exec(*argv, **kwargs):
        process = HangingProcess()
        process_ref.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    task = asyncio.create_task(DeploymentStateProvider(str(env)).capture_from_digest())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process_ref and process_ref[0].returncode == -9
