"""Typed Slice 4 updater IPC action contract tests."""

from dataclasses import dataclass

from fastapi.testclient import TestClient
from sakura_ai_updater.ipc import create_app


@dataclass
class _Job:
    job_id: str
    state: str = "downloading"

    def to_dict(self):
        return {"job_id": self.job_id, "state": self.state}


class _Orchestrator:
    async def check(self):
        return {"update_available": True, "update_ready": True}

    async def preflight(self, target_version):
        return {"can_update": True, "target_version": target_version, "checks": []}

    async def submit_update(self, target_version):
        return "upd_test"

    def get_job(self, job_id):
        return _Job(job_id) if job_id == "upd_test" else None

    def get_job_logs_payload(self, job_id):
        return {"job_id": job_id, "logs": [], "truncated": False}


def test_actions_use_success_envelope_and_error_bodies(tmp_path):
    client = TestClient(create_app(str(tmp_path / "state.json"), orchestrator=_Orchestrator()))

    response = client.post("/v1/check")
    assert response.status_code == 200
    assert response.json()["data"]["update_ready"] is True

    response = client.post("/v1/preflight", json={"target_version": "3.1.0"})
    assert response.status_code == 200
    assert response.json()["protocol_version"] == 1

    response = client.post("/v1/update", json={"target_version": "3.1.0"})
    assert response.status_code == 202
    assert response.json()["data"]["job_id"] == "upd_test"

    response = client.get("/v1/jobs/upd_test")
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "downloading"

    response = client.get("/v1/jobs/upd_test/logs")
    assert response.status_code == 200
    assert response.json()["data"]["job_id"] == "upd_test"
    assert client.get("/v1/jobs/missing/logs").status_code == 404

    response = client.post("/v1/rollback")
    assert response.status_code == 501
    assert response.json() == {"error": "not_implemented"}


def test_actions_without_orchestrator_are_not_success(tmp_path):
    client = TestClient(create_app(str(tmp_path / "state.json")))
    response = client.post("/v1/check")
    assert response.status_code == 503
    assert response.json() == {"error": "updater_not_ready"}
    assert "protocol_version" not in response.json()


def test_conflict_and_preflight_errors_are_unwrapped():
    class _Conflict(_Orchestrator):
        async def submit_update(self, target_version):
            from sakura_ai_updater.jobs import UpdateInProgressError

            raise UpdateInProgressError("upd_existing")

    client = TestClient(create_app("unused", orchestrator=_Conflict()))
    response = client.post("/v1/update", json={"target_version": "3.1.0"})
    assert response.status_code == 409
    assert response.json() == {"error": "update_in_progress", "job_id": "upd_existing"}


def test_release_error_exposes_only_safe_classification():
    class ReleaseUnavailableError(RuntimeError):
        detail = "tls_certificate_verification_failed"

    class _Unavailable(_Orchestrator):
        async def check(self):
            raise ReleaseUnavailableError("signed URL must not escape")

    client = TestClient(create_app("unused", orchestrator=_Unavailable()))
    response = client.post("/v1/check")

    assert response.status_code == 502
    assert response.json() == {
        "error": "release_unavailable",
        "detail": "tls_certificate_verification_failed",
    }
