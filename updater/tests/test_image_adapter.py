from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import sakura_ai_updater.adapters.image as image_module
from sakura_ai_updater.adapters.image import (
    HealthCheckVersionMismatch,
    ImageAdapter,
    ImageAdapterError,
    ImageCommandError,
)


class _Process:
    def __init__(self, returncode: int = 0, stdout: bytes = b"ok\n", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_pull_failure_does_not_touch_deployment_env(tmp_path, monkeypatch):
    env = tmp_path / "deployment.env"
    env.write_text("SAKURA_AI_IMAGE=old:image\nOTHER=keep\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*argv, **kwargs):
        calls.append(tuple(argv))
        return _Process(1, b"", b"registry unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter("compose.yml", str(env))
    with pytest.raises(ImageCommandError) as caught:
        await adapter.pull("ghcr.io/example/app:v3.1.0")
    assert caught.value.stderr == "registry unavailable"
    assert env.read_text(encoding="utf-8") == "SAKURA_AI_IMAGE=old:image\nOTHER=keep\n"
    assert calls == [("docker", "pull", "ghcr.io/example/app:v3.1.0")]


@pytest.mark.asyncio
async def test_activate_writes_env_and_uses_explicit_compose_env_file(tmp_path, monkeypatch):
    env = tmp_path / "deployment.env"
    env.write_text(
        "SAKURA_AI_IMAGE=old:image\nCOMPOSE_PROJECT_NAME=sakura-ai\nOTHER=keep\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*argv, **kwargs):
        calls.append(tuple(argv))
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter("/srv/docker-compose.prod.yml", str(env))
    await adapter.activate("ghcr.io/example/app:v3.1.0")
    assert "SAKURA_AI_IMAGE=ghcr.io/example/app:v3.1.0" in env.read_text(encoding="utf-8")
    assert calls == [
        (
            "docker",
            "compose",
            "--env-file",
            str(env),
            "--project-name",
            "sakura-ai",
            "-f",
            "/srv/docker-compose.prod.yml",
            "up",
            "-d",
        )
    ]


@pytest.mark.asyncio
async def test_activate_persists_development_tag_and_digest(tmp_path, monkeypatch):
    env = tmp_path / "deployment.env"
    env.write_text(
        "SAKURA_AI_IMAGE=ghcr.io/example/app:v3.1.0\n"
        "COMPOSE_PROJECT_NAME=sakura-ai\n"
        "OTHER=keep\n",
        encoding="utf-8",
    )
    target = (
        "ghcr.io/example/app:dev-20260814010000-v3.1.0-"
        + "a" * 40
        + "@sha256:"
        + "b" * 64
    )

    async def fake_exec(*argv, **kwargs):
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter("/srv/docker-compose.prod.yml", str(env))
    await adapter.activate(target)

    content = env.read_text(encoding="utf-8")
    assert f"SAKURA_AI_IMAGE={target}\n" in content
    assert "OTHER=keep\n" in content


@pytest.mark.asyncio
async def test_activate_rejects_invalid_persisted_compose_project(tmp_path, monkeypatch):
    env = tmp_path / "deployment.env"
    env.write_text(
        "SAKURA_AI_IMAGE=old:image\nCOMPOSE_PROJECT_NAME=../../unsafe\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*argv, **kwargs):
        calls.append(tuple(argv))
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter("/srv/docker-compose.prod.yml", str(env))

    with pytest.raises(ImageAdapterError, match="invalid COMPOSE_PROJECT_NAME"):
        await adapter.activate("ghcr.io/example/app:v3.1.0")

    assert calls == []


@pytest.mark.asyncio
async def test_activate_requires_persisted_compose_project(tmp_path, monkeypatch):
    env = tmp_path / "deployment.env"
    env.write_text("SAKURA_AI_IMAGE=old:image\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*argv, **kwargs):
        calls.append(tuple(argv))
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter("/srv/docker-compose.prod.yml", str(env))

    with pytest.raises(ImageAdapterError, match="missing COMPOSE_PROJECT_NAME"):
        await adapter.activate("ghcr.io/example/app:v3.1.0")

    assert calls == []


@pytest.mark.asyncio
async def test_activate_rejects_noncanonical_compose_project(tmp_path, monkeypatch):
    env = tmp_path / "deployment.env"
    env.write_text(
        "SAKURA_AI_IMAGE=old:image\nCOMPOSE_PROJECT_NAME=docker\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*argv, **kwargs):
        calls.append(tuple(argv))
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter("/srv/docker-compose.prod.yml", str(env))

    with pytest.raises(ImageAdapterError, match="unsupported COMPOSE_PROJECT_NAME"):
        await adapter.activate("ghcr.io/example/app:v3.1.0")

    assert calls == []


@pytest.mark.asyncio
async def test_health_check_requires_target_version(monkeypatch):
    responses = [(200, {"version": "3.0.0"}), (200, {"version": "3.1.0"})]

    def fake_health(url: str, timeout: float):
        return responses.pop(0)

    monkeypatch.setattr(ImageAdapter, "_read_health_sync", staticmethod(fake_health))
    adapter = ImageAdapter(
        "compose.yml",
        "deployment.env",
        health_timeout=1,
        health_poll_interval=0,
    )
    await adapter.health_check("3.1.0")


@pytest.mark.asyncio
async def test_health_check_deadline_uses_monotonic_clock(monkeypatch):
    ticks = iter((100.0, 100.1))
    # Replace the adapter's module reference rather than the process-wide
    # ``time`` module; asyncio itself also depends on ``time.monotonic``.
    monkeypatch.setattr(
        image_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks)),
    )
    monkeypatch.setattr(
        ImageAdapter,
        "_read_health_sync",
        staticmethod(lambda _url, _timeout: (200, {"version": "3.1.0"})),
    )
    adapter = ImageAdapter(
        "compose.yml",
        "deployment.env",
        health_timeout=1,
        health_poll_interval=0,
    )

    await adapter.health_check("3.1.0")


@pytest.mark.asyncio
async def test_health_check_version_mismatch_is_diagnostic(monkeypatch):
    monkeypatch.setattr(
        ImageAdapter,
        "_read_health_sync",
        staticmethod(lambda url, timeout: (200, {"version": "3.0.0"})),
    )
    adapter = ImageAdapter(
        "compose.yml",
        "deployment.env",
        health_timeout=0.01,
        health_poll_interval=0,
    )
    with pytest.raises(HealthCheckVersionMismatch) as caught:
        await adapter.health_check("3.1.0")
    assert caught.value.error_code == "health_check_version_mismatch"


@pytest.mark.asyncio
async def test_subprocess_cancellation_is_re_raised(monkeypatch):
    started = asyncio.Event()
    released = asyncio.Event()

    class HangingProcess(_Process):
        async def communicate(self):
            started.set()
            if self.returncode != -9:
                await released.wait()
            return b"", b""

        def kill(self):
            super().kill()
            released.set()

    async def fake_exec(*argv, **kwargs):
        return HangingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter("compose.yml", "deployment.env", command_timeout=30)
    task = asyncio.create_task(adapter.pull("image:v1"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_subprocess_timeout_kills_and_reaps_process(monkeypatch):
    released = asyncio.Event()
    process_ref: list[_Process] = []

    class TimeoutProcess(_Process):
        async def communicate(self):
            if self.returncode != -9:
                await released.wait()
            return b"drained", b"diagnostic"

        def kill(self):
            super().kill()
            released.set()

    async def fake_exec(*argv, **kwargs):
        process = TimeoutProcess()
        process_ref.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter("compose.yml", "deployment.env", command_timeout=0.01)
    with pytest.raises(ImageCommandError) as caught:
        await adapter.pull("image:v1")
    assert caught.value.error_code == "command_timeout"
    assert process_ref and process_ref[0].returncode == -9
