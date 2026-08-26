from __future__ import annotations

import asyncio

import pytest
from sakura_ai_updater.jobs import (
    JobOrchestrator,
    PreflightFailedError,
    UpdateInProgressError,
    UpdaterMaintenanceError,
)
from sakura_ai_updater.state import load_state, reconcile_interrupted_job


class _Release:
    def __init__(self, version="3.1.0"):
        self.manifest = {
            "schema_version": 1,
            "version": version,
            "channel": "stable",
            "min_upgrade_from": "0.0.0",
            "image": f"ghcr.io/example/app:v{version}",
            "updater": {"protocol_version": 1},
        }

    async def fetch_manifest(self, version=None):
        return self.manifest

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


class _Deployment:
    deployment_env = "."

    def __init__(self, mode="image", current="3.0.0"):
        self.mode = mode
        self.current = current
        self.materialized = False

    async def resolve_current_version(self):
        return self.current

    def read_deploy_mode(self):
        return self.mode

    async def disk_space_sufficient(self, threshold):
        return True, threshold * 2

    async def capture_from_image(self):
        return "ghcr.io/example/app:latest"

    async def capture_from_digest(self):
        return "sha256:old"

    async def materialize_current_anchor(self):
        self.materialized = True
        return "ghcr.io/example/app:v3.0.0@sha256:old"


class _Adapter:
    def __init__(self, *, cancel=False):
        self.cancel = cancel
        self.calls = []

    async def preflight_image(self, image):
        self.calls.append(("preflight", image))

    async def pull(self, image):
        self.calls.append(("pull", image))
        if self.cancel:
            raise asyncio.CancelledError

    async def activate(self, image):
        self.calls.append(("activate", image))

    async def health_check(self, version):
        self.calls.append(("health", version))


@pytest.mark.asyncio
async def test_preflight_four_gates_are_explicit(tmp_path):
    path = str(tmp_path / "state.json")
    deployment = _Deployment(mode="source", current="3.2.0")
    orchestrator = JobOrchestrator(
        path, _Adapter(), _Release(), deployment, disk_space_threshold=1
    )
    result = await orchestrator.preflight("3.1.0")
    failed = {item["name"] for item in result["checks"] if not item["passed"]}
    assert {"deployment_mode_image", "target_newer"}.issubset(failed)
    assert result["can_update"] is False


@pytest.mark.asyncio
async def test_preflight_records_status_readiness_snapshot(tmp_path):
    orchestrator = JobOrchestrator(
        str(tmp_path / "state.json"),
        _Adapter(),
        _Release(),
        _Deployment(),
        disk_space_threshold=1,
    )
    result = await orchestrator.preflight("3.1.0")
    assert result["can_update"] is True
    assert orchestrator.readiness_snapshot == {
        "update_ready": True,
        "readiness": {
            "manifest_found": True,
            "manifest_valid": True,
            "image_pullable": True,
            "protocol_compatible": True,
            "min_upgrade_from_satisfied": True,
            "updater_asset_present": True,
            "sha256sums_present": True,
            "target_newer": True,
            "deployment_mode_image": True,
        },
        "target": {
            "version": "3.1.0",
            "image": "ghcr.io/example/app:v3.1.0",
            "channel": "stable",
            "sandboxd_image": "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:"
            + "a" * 64,
            "runner_image": "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:"
            + "b" * 64,
        },
    }


@pytest.mark.asyncio
async def test_preflight_rejects_partial_persisted_sandbox_pair(tmp_path):
    class _PartialDeployment(_Deployment):
        def sandbox_image_refs(self):
            return {
                "sandboxd_image": "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:"
                + "a" * 64,
                "runner_image": None,
            }

    orchestrator = JobOrchestrator(
        str(tmp_path / "state.json"),
        _Adapter(),
        _Release(),
        _PartialDeployment(),
        disk_space_threshold=1,
    )
    result = await orchestrator.preflight("3.1.0")
    checks = {item["name"]: item for item in result["checks"]}
    assert result["can_update"] is False
    assert checks["current_sandbox_pair_complete"]["passed"] is False
    assert "only one sandbox image digest" in checks[
        "current_sandbox_pair_complete"
    ]["detail"]


@pytest.mark.asyncio
async def test_update_success_clears_active_gate(tmp_path):
    path = str(tmp_path / "state.json")
    deployment = _Deployment()
    adapter = _Adapter()
    orchestrator = JobOrchestrator(
        path, adapter, _Release(), deployment, disk_space_threshold=1
    )
    job_id = await orchestrator.submit_update("3.1.0")
    await orchestrator.wait_for_job(job_id)
    store = load_state(path)
    assert store.active_job_id is None
    assert store.current_job and store.current_job.state == "success"
    assert store.current_job.target_sandboxd_image == (
        "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:" + "a" * 64
    )
    assert store.current_job.target_runner_image == (
        "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:" + "b" * 64
    )
    assert store.current_job.started_at and store.current_job.started_at.endswith("Z")
    assert store.current_job.updated_at and store.current_job.updated_at.endswith("Z")
    assert [call[0] for call in adapter.calls] == [
        "preflight",
        "preflight",
        "preflight",
        "preflight",
        "preflight",
        "preflight",
        "pull",
        "pull",
        "pull",
        "activate",
        "health",
    ]
    assert orchestrator.readiness_snapshot is not None
    assert orchestrator.readiness_snapshot["update_ready"] is False
    assert orchestrator.readiness_snapshot["readiness"]["target_newer"] is False
    assert orchestrator.readiness_snapshot["target"]["version"] == "3.1.0"


@pytest.mark.asyncio
async def test_prepare_stop_atomically_blocks_submit_finishing_preflight(tmp_path):
    orchestrator = JobOrchestrator(
        str(tmp_path / "state.json"),
        _Adapter(),
        _Release(),
        _Deployment(),
        disk_space_threshold=1,
    )
    entered_preflight = asyncio.Event()
    release_preflight = asyncio.Event()
    original_preflight = orchestrator.preflight

    async def blocked_preflight(*args, **kwargs):
        entered_preflight.set()
        await release_preflight.wait()
        return await original_preflight(*args, **kwargs)

    orchestrator.preflight = blocked_preflight
    submit = asyncio.create_task(orchestrator.submit_update("3.1.0"))
    await entered_preflight.wait()

    assert await orchestrator.prepare_stop() == {"prepared": True}
    release_preflight.set()
    with pytest.raises(UpdaterMaintenanceError):
        await submit
    assert load_state(orchestrator.state_path).active_job_id is None

    assert await orchestrator.cancel_stop() == {"prepared": False}
    job_id = await orchestrator.submit_update("3.1.0")
    await orchestrator.wait_for_job(job_id)


@pytest.mark.asyncio
async def test_prepare_stop_rejects_an_active_job_without_closing_gate(tmp_path):
    adapter = _Adapter(cancel=True)
    orchestrator = JobOrchestrator(
        str(tmp_path / "state.json"),
        adapter,
        _Release(),
        _Deployment(),
        disk_space_threshold=1,
    )
    job_id = await orchestrator.submit_update("3.1.0")
    with pytest.raises(UpdateInProgressError) as caught:
        await orchestrator.prepare_stop()
    assert caught.value.job_id == job_id


@pytest.mark.asyncio
async def test_update_retries_one_timed_out_pull_and_records_retry(tmp_path):
    class _TimeoutOnceAdapter(_Adapter):
        async def pull(self, image):
            self.calls.append(("pull", image))
            if sum(call[0] == "pull" for call in self.calls) == 1:
                error = RuntimeError("pull timed out")
                error.error_code = "command_timeout"
                raise error

    path = str(tmp_path / "state.json")
    adapter = _TimeoutOnceAdapter()
    orchestrator = JobOrchestrator(
        path,
        adapter,
        _Release(),
        _Deployment(),
        disk_space_threshold=1,
    )
    job_id = await orchestrator.submit_update("3.1.0")
    await orchestrator.wait_for_job(job_id)

    job = load_state(path).current_job
    assert job is not None
    assert job.state == "success"
    assert job.retry_count == 1
    assert [call[0] for call in adapter.calls].count("pull") == 4
    assert [call[0] for call in adapter.calls][-2:] == ["activate", "health"]


@pytest.mark.asyncio
async def test_update_stops_before_activation_after_second_pull_timeout(tmp_path):
    class _TimeoutAdapter(_Adapter):
        async def pull(self, image):
            self.calls.append(("pull", image))
            error = RuntimeError("pull timed out")
            error.error_code = "command_timeout"
            raise error

    path = str(tmp_path / "state.json")
    adapter = _TimeoutAdapter()
    orchestrator = JobOrchestrator(
        path,
        adapter,
        _Release(),
        _Deployment(),
        disk_space_threshold=1,
    )
    job_id = await orchestrator.submit_update("3.1.0")
    await orchestrator.wait_for_job(job_id)

    job = load_state(path).current_job
    assert job is not None
    assert job.state == "failed"
    assert job.error_code == "command_timeout"
    assert job.retry_count == 1
    assert [call[0] for call in adapter.calls].count("pull") == 2
    assert not any(call[0] in {"activate", "health"} for call in adapter.calls)


@pytest.mark.asyncio
async def test_cancelled_job_keeps_active_gate_for_reconcile(tmp_path):
    path = str(tmp_path / "state.json")
    orchestrator = JobOrchestrator(
        path,
        _Adapter(cancel=True),
        _Release(),
        _Deployment(),
        disk_space_threshold=1,
    )
    job_id = await orchestrator.submit_update("3.1.0")
    task = orchestrator._tasks[job_id]
    with pytest.raises(asyncio.CancelledError):
        await task
    interrupted = load_state(path)
    assert interrupted.active_job_id == job_id
    assert interrupted.current_job and interrupted.current_job.state == "downloading"
    reconciled, changed = reconcile_interrupted_job(interrupted)
    assert changed is True
    assert reconciled.active_job_id is None
    assert reconciled.current_job and reconciled.current_job.error_code == "interrupted"


@pytest.mark.asyncio
async def test_concurrent_submit_is_rejected(tmp_path):
    path = str(tmp_path / "state.json")
    orchestrator = JobOrchestrator(
        path, _Adapter(cancel=True), _Release(), _Deployment(), disk_space_threshold=1
    )
    first = await orchestrator.submit_update("3.1.0")
    with pytest.raises(UpdateInProgressError) as caught:
        await orchestrator.submit_update("3.1.0")
    assert caught.value.job_id == first
    task = orchestrator._tasks[first]
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_preflight_failure_does_not_create_job(tmp_path):
    path = str(tmp_path / "state.json")
    orchestrator = JobOrchestrator(
        path,
        _Adapter(),
        _Release(),
        _Deployment(mode="source"),
        disk_space_threshold=1,
    )
    with pytest.raises(PreflightFailedError):
        await orchestrator.submit_update("3.1.0")
    assert load_state(path).current_job is None


def test_get_job_logs_returns_endpoint_snapshot_payload(tmp_path):
    orchestrator = JobOrchestrator(
        str(tmp_path / "state.json"),
        _Adapter(),
        _Release(),
        _Deployment(),
        disk_space_threshold=1,
    )
    payload = orchestrator.get_job_logs("missing")
    assert payload == {"job_id": "missing", "logs": [], "truncated": False}


@pytest.mark.asyncio
async def test_health_failure_rolls_back_web_and_sandbox_transaction(tmp_path):
    class _TransactionalAdapter(_Adapter):
        def __init__(self):
            super().__init__()
            self.rollback_calls = []
            self.health_calls = []

        async def capture_snapshot(self):
            return {"old": "deployment"}

        async def activate(self, image, sandboxd_image, runner_image):
            self.calls.append(("activate", image, sandboxd_image, runner_image))

        async def rollback(self, snapshot):
            self.rollback_calls.append(snapshot)

        async def health_check(self, version):
            self.health_calls.append(version)
            if version == "3.1.0":
                raise RuntimeError("new Web health failed")

    adapter = _TransactionalAdapter()
    orchestrator = JobOrchestrator(
        str(tmp_path / "state.json"),
        adapter,
        _Release(),
        _Deployment(),
        disk_space_threshold=1,
    )
    job_id = await orchestrator.submit_update("3.1.0")
    await orchestrator.wait_for_job(job_id)

    job = load_state(str(tmp_path / "state.json")).current_job
    assert job is not None
    assert job.state == "failed"
    assert job.rollback_attempted is True
    assert adapter.rollback_calls == [{"old": "deployment"}]
    assert adapter.health_calls == ["3.1.0", "3.0.0"]
