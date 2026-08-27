"""Real CLI/runtime-factory and readiness-contract tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sakura_ai_sandboxer.app import create_app
from sakura_ai_sandboxer.config import SandboxdConfig
from sakura_ai_sandboxer.docker_runtime import DockerRuntimeAdapter
from sakura_ai_sandboxer.runtime_factory import create_runtime

LOCAL_IMAGE_ID = "sha256:" + "a" * 64


def _docker_config(tmp_path: Path, **overrides: object) -> SandboxdConfig:
    values: dict[str, object] = {
        "socket_path": str(tmp_path / "run" / "sandboxd.sock"),
        "socket_root": str(tmp_path / "run"),
        "workspace_root": str(tmp_path / "workplace"),
        "instance_id": "sandbox-12345678",
        "runner_image_digest": LOCAL_IMAGE_ID,
        "runtime_name": "docker",
    }
    values.update(overrides)
    return SandboxdConfig(**values)


def test_factory_constructs_real_docker_adapter_for_local_immutable_id(tmp_path: Path):
    config = _docker_config(tmp_path)
    runtime = create_runtime(config)

    assert isinstance(runtime, DockerRuntimeAdapter)
    assert runtime.name == "docker"
    assert runtime.image_reference == LOCAL_IMAGE_ID


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"runtime_name": "unavailable"}, "explicitly configured as docker"),
        ({"workspace_root": None}, "workspace_root"),
        ({"instance_id": None}, "instance_id"),
        ({"runner_image_digest": None}, "immutable runner"),
    ],
)
def test_factory_fails_closed_when_docker_configuration_is_incomplete(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
):
    with pytest.raises(ValueError, match=message):
        create_runtime(_docker_config(tmp_path, **overrides))


def test_config_rejects_mutable_runner_tag_but_accepts_local_image_id(tmp_path: Path):
    assert _docker_config(tmp_path).runner_image_digest == LOCAL_IMAGE_ID
    with pytest.raises(ValueError, match="immutable sha256"):
        _docker_config(tmp_path, runner_image_digest="sakura-ai-agent-runner:dev")


def test_server_serve_injects_factory_runtime_before_uvicorn(monkeypatch, tmp_path: Path):
    from sakura_ai_sandboxer import server

    config = _docker_config(tmp_path)
    root = tmp_path / "run"
    socket_path = root / "sandboxd.sock"
    captured: dict[str, object] = {}

    class _Listener:
        def fileno(self) -> int:
            return 7

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(server, "validate_socket_filesystem", lambda _config: (root, socket_path))
    monkeypatch.setattr(server, "_bind_secure_socket", lambda _config, _path: _Listener())

    import uvicorn

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    server.serve(config)

    app = captured["app"]
    assert isinstance(app.state.sandbox_service.runtime, DockerRuntimeAdapter)
    assert captured["kwargs"] == {"fd": 7, "log_level": "info"}
    assert captured["closed"] is True


class _RecoveringDockerRuntime:
    name = "docker"

    def __init__(self) -> None:
        self.validated = False
        self.recovered = False

    async def validate_egress_network(self, *, deadline: float) -> None:
        del deadline
        self.validated = True

    async def recover_orphans(self, *, deadline: float) -> None:
        del deadline
        self.recovered = True

    async def execute(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("health test must not execute a request")

    async def cancel(self, request_id: str, *, deadline: float) -> None:
        del request_id, deadline

    async def shutdown(self, *, deadline: float) -> None:
        del deadline


@pytest.mark.asyncio
async def test_health_is_strict_and_ready_only_after_orphan_recovery(tmp_path: Path):
    runtime = _RecoveringDockerRuntime()
    app = create_app(_docker_config(tmp_path), runtime=runtime)

    service = app.state.sandbox_service
    assert service.health().ready is False

    async with app.router.lifespan_context(app):
        assert runtime.recovered is True
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://sandboxd") as client:
            response = await client.get("/v1/health")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"protocol_version", "sandboxd_version", "data"}
    assert set(payload["data"]) == {
        "ready",
        "runtime",
        "profiles",
        "egress_capability",
        "instance_id",
        "workspace_root",
        "runner_image_digest",
    }
    assert payload["data"]["ready"] is True
    assert payload["data"]["runtime"] == "docker"
    assert payload["data"]["instance_id"] == "sandbox-12345678"
    assert payload["data"]["runner_image_digest"] == LOCAL_IMAGE_ID


@pytest.mark.asyncio
async def test_named_egress_network_is_validated_before_ready(tmp_path: Path):
    runtime = _RecoveringDockerRuntime()
    app = create_app(
        _docker_config(tmp_path, egress_network="sakura-egress"),
        runtime=runtime,
    )

    async with app.router.lifespan_context(app):
        assert runtime.validated is True
        assert runtime.recovered is True
        assert app.state.sandbox_service.health().ready is True
