from __future__ import annotations

import asyncio

import pytest
from sakura_ai_updater.adapters.image import HealthCheckVersionMismatch, ImageAdapter
from sakura_ai_updater.deployment import DeploymentError
from sakura_ai_updater.jobs import JobOrchestrator
from sakura_ai_updater.state import load_state


def _target() -> dict[str, str]:
    revision = "a" * 40
    return {
        "channel": "development",
        "version": "3.0.2",
        "revision": revision,
        "tag": "dev-20260813040000-v3.0.2-" + revision,
        "digest": "sha256:" + "b" * 64,
    }


def _stable_target(digest: str = "sha256:" + "c" * 64) -> dict[str, str]:
    return {
        "channel": "stable",
        "version": "3.0.2",
        "tag": "v3.0.2",
        "digest": digest,
    }


@pytest.mark.asyncio
async def test_development_health_requires_version_channel_and_revision(monkeypatch):
    monkeypatch.setattr(
        ImageAdapter,
        "_read_health_sync",
        staticmethod(
            lambda url, timeout: (
                200,
                {
                    "version": "3.0.2",
                    "build": {"channel": "development", "revision": "a" * 40},
                },
            )
        ),
    )
    adapter = ImageAdapter(
        "compose.yml", "deployment.env", health_timeout=0.01, health_poll_interval=0
    )
    await adapter.health_check(
        {"version": "3.0.2", "channel": "development", "revision": "a" * 40}
    )

    monkeypatch.setattr(
        ImageAdapter,
        "_read_health_sync",
        staticmethod(
            lambda url, timeout: (
                200,
                {
                    "version": "3.0.2",
                    "build": {"channel": "development", "revision": "c" * 40},
                },
            )
        ),
    )
    with pytest.raises(HealthCheckVersionMismatch):
        await adapter.health_check(
            {"version": "3.0.2", "channel": "development", "revision": "a" * 40}
        )


@pytest.mark.asyncio
async def test_structured_stable_health_requires_stable_channel(monkeypatch):
    adapter = ImageAdapter(
        "compose.yml", "deployment.env", health_timeout=0.01, health_poll_interval=0
    )
    monkeypatch.setattr(
        ImageAdapter,
        "_read_health_sync",
        staticmethod(
            lambda url, timeout: (
                200,
                {"version": "3.0.2", "build": {"channel": "development"}},
            )
        ),
    )
    with pytest.raises(HealthCheckVersionMismatch):
        await adapter.health_check({"version": "3.0.2", "channel": "stable"})

    monkeypatch.setattr(
        ImageAdapter,
        "_read_health_sync",
        staticmethod(
            lambda url, timeout: (
                200,
                {"version": "3.0.2", "build": {"channel": "stable"}},
            )
        ),
    )
    await adapter.health_check({"version": "3.0.2", "channel": "stable"})


class _Deployment:
    deployment_env = "."

    async def resolve_current_version(self):
        return "3.0.2"

    def read_deploy_mode(self):
        return "image"

    async def current_state(self):
        return {
            "current_channel": "development",
            "running_container_digest": "sha256:" + "b" * 64,
        }

    async def disk_space_sufficient(self, threshold):
        return True, threshold * 2


class _Adapter:
    async def preflight_image(self, image):
        return None


class _StableRelease:
    async def fetch_manifest(self, version=None):
        version = version or "3.0.2"
        return {
            "schema_version": 1,
            "version": version,
            "channel": "stable",
            "min_upgrade_from": "0.0.0",
            "image": f"ghcr.io/sakura520222/sakura-ai:v{version}",
            "updater": {"protocol_version": 1},
        }

    async def resolve_stable_target(self, version=None, *, expected_digest=None):
        version = version or "3.0.2"
        digest = expected_digest or ("sha256:" + "c" * 64)
        return {
            "channel": "stable",
            "version": version,
            "tag": f"v{version}",
            "digest": digest,
        }

    async def fetch_sandbox_manifest(self, version):
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

    async def has_required_assets(self, manifest, version=None):
        return True


class _StableChannelDeployment(_Deployment):
    async def current_state(self):
        return {
            "current_channel": "stable",
            "running_container_digest": "sha256:" + "d" * 64,
        }


class _UnknownChannelDeployment(_Deployment):
    async def current_state(self):
        # Legacy containers may not expose build identity at all.  They must
        # never be treated as already confirmed to be on the requested channel.
        return {}


class _InvalidImageIdentityDeployment(_Deployment):
    async def current_state(self):
        raise DeploymentError("running image has no matching repository digest")


@pytest.mark.asyncio
async def test_development_missing_sandbox_pair_is_not_updateable(monkeypatch, tmp_path):
    class _MissingPairRelease:
        pass

    async def verify(self, target):
        return target

    monkeypatch.setattr("sakura_ai_updater.jobs.RegistryClient.verify_target", verify)
    result = await JobOrchestrator(
        str(tmp_path / "state.json"),
        _Adapter(),
        _MissingPairRelease(),
        _Deployment(),
        disk_space_threshold=1,
    ).preflight(_target(), confirm_channel_switch=True)
    check = next(
        item for item in result["checks"] if item["name"] == "target_sandbox_pair_revision"
    )
    assert result["can_update"] is False
    assert check["passed"] is False
    assert "cannot resolve development sandbox images" in check["detail"]


@pytest.mark.asyncio
async def test_same_development_digest_is_not_updateable(monkeypatch, tmp_path):
    async def verify(self, target):
        return target

    monkeypatch.setattr("sakura_ai_updater.jobs.RegistryClient.verify_target", verify)
    result = await JobOrchestrator(
        str(tmp_path / "state.json"),
        _Adapter(),
        object(),
        _Deployment(),
        disk_space_threshold=1,
    ).preflight(_target())
    assert result["can_update"] is False
    assert any(
        item["name"] == "target_newer" and not item["passed"]
        for item in result["checks"]
    )


@pytest.mark.asyncio
async def test_deployment_identity_error_is_a_failed_check_not_protocol_error(
    monkeypatch, tmp_path
):
    async def verify(self, target):
        return target

    monkeypatch.setattr("sakura_ai_updater.jobs.RegistryClient.verify_target", verify)
    result = await JobOrchestrator(
        str(tmp_path / "state.json"),
        _Adapter(),
        object(),
        _InvalidImageIdentityDeployment(),
        disk_space_threshold=1,
    ).preflight(_target(), confirm_channel_switch=True)

    identity = next(
        item
        for item in result["checks"]
        if item["name"] == "current_image_identity_valid"
    )
    assert result["can_update"] is False
    assert identity == {
        "name": "current_image_identity_valid",
        "passed": False,
        "detail": "running image has no matching repository digest",
    }


@pytest.mark.asyncio
async def test_development_unknown_current_channel_requires_confirmation(
    monkeypatch, tmp_path
):
    async def verify(self, target):
        return target

    monkeypatch.setattr("sakura_ai_updater.jobs.RegistryClient.verify_target", verify)
    orchestrator = JobOrchestrator(
        str(tmp_path / "state.json"),
        _Adapter(),
        object(),
        _UnknownChannelDeployment(),
        disk_space_threshold=1,
    )
    without_confirmation = await orchestrator.preflight(_target())
    assert without_confirmation["requires_channel_switch_confirmation"] is True
    assert without_confirmation["can_update"] is False
    with_confirmation = await orchestrator.preflight(
        _target(), confirm_channel_switch=True
    )
    assert with_confirmation["can_update"] is True


@pytest.mark.asyncio
async def test_submit_update_accepts_development_target_and_persists_job(
    monkeypatch, tmp_path
):
    target = _target()
    orchestrator = JobOrchestrator(
        str(tmp_path / "state.json"),
        _Adapter(),
        object(),
        _Deployment(),
        disk_space_threshold=1,
    )
    calls = []

    async def fake_preflight(value, *, confirm_channel_switch=False):
        calls.append((value, confirm_channel_switch))
        return {
            "can_update": True,
            "from_version": "3.0.1",
            "target_version": target["version"],
            "target_image": "ghcr.io/sakura520222/sakura-ai:"
            + target["tag"]
            + "@"
            + target["digest"],
            "target_channel": "development",
            "target_revision": target["revision"],
            "target_digest": target["digest"],
            "target_tag": target["tag"],
            "checks": [],
        }

    class _FakeTask:
        def add_done_callback(self, callback):
            self.callback = callback

    def fake_create_task(coro, *, name):
        # Avoid leaving the coroutine unawaited: this test only exercises the
        # enqueue boundary, not the destructive worker itself.
        coro.close()
        return _FakeTask()

    monkeypatch.setattr(orchestrator, "preflight", fake_preflight)
    monkeypatch.setattr(asyncio, "create_task", fake_create_task)
    job_id = await orchestrator.submit_update(target, confirm_channel_switch=True)

    job = load_state(str(tmp_path / "state.json")).current_job
    assert job_id.startswith("upd_")
    assert job is not None
    assert job.target_channel == "development"
    assert job.target_revision == target["revision"]
    assert job.target_digest == target["digest"]
    assert calls == [(target, True)]


@pytest.mark.asyncio
async def test_development_to_stable_same_semver_requires_confirmation(
    monkeypatch, tmp_path
):
    async def verify(self, target):
        return target

    monkeypatch.setattr("sakura_ai_updater.jobs.RegistryClient.verify_target", verify)
    orchestrator = JobOrchestrator(
        str(tmp_path / "state.json"),
        _Adapter(),
        _StableRelease(),
        _Deployment(),
        disk_space_threshold=1,
    )
    target = _stable_target()
    without_confirmation = await orchestrator.preflight(target)
    assert without_confirmation["target_channel"] == "stable"
    assert without_confirmation["requires_channel_switch_confirmation"] is True
    assert without_confirmation["can_update"] is False
    with_confirmation = await orchestrator.preflight(
        target, confirm_channel_switch=True
    )
    assert with_confirmation["can_update"] is True


@pytest.mark.asyncio
async def test_stable_to_stable_same_semver_is_still_strictly_newer(
    monkeypatch, tmp_path
):
    async def verify(self, target):
        return target

    monkeypatch.setattr("sakura_ai_updater.jobs.RegistryClient.verify_target", verify)
    orchestrator = JobOrchestrator(
        str(tmp_path / "state.json"),
        _Adapter(),
        _StableRelease(),
        _StableChannelDeployment(),
        disk_space_threshold=1,
    )
    result = await orchestrator.preflight(_stable_target(), confirm_channel_switch=True)
    assert result["can_update"] is False
    assert any(
        item["name"] == "target_newer" and not item["passed"]
        for item in result["checks"]
    )


@pytest.mark.asyncio
async def test_structured_stable_target_enforces_manifest_minimum_upgrade(
    monkeypatch, tmp_path
):
    async def verify(self, target):
        return target

    class _MinimumUpgradeRelease(_StableRelease):
        async def fetch_manifest(self, version=None):
            manifest = await super().fetch_manifest(version)
            manifest["min_upgrade_from"] = "3.1.0"
            return manifest

    monkeypatch.setattr("sakura_ai_updater.jobs.RegistryClient.verify_target", verify)
    target = {
        "channel": "stable",
        "version": "3.1.1",
        "tag": "v3.1.1",
        "digest": "sha256:" + "e" * 64,
    }
    result = await JobOrchestrator(
        str(tmp_path / "state.json"),
        _Adapter(),
        _MinimumUpgradeRelease(),
        _Deployment(),
        disk_space_threshold=1,
    ).preflight(target, confirm_channel_switch=True)
    minimum = next(
        item for item in result["checks"] if item["name"] == "min_upgrade_from"
    )
    assert minimum == {
        "name": "min_upgrade_from",
        "passed": False,
        "detail": "3.0.2 >= 3.1.0",
    }
    assert result["can_update"] is False
