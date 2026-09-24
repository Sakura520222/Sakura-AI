from __future__ import annotations

import asyncio
import json
import os
import stat
from types import SimpleNamespace

import pytest
import sakura_ai_updater.adapters.image as image_module
from sakura_ai_updater.adapters.image import (
    HealthCheckVersionMismatch,
    ImageAdapter,
    ImageAdapterError,
    ImageCommandError,
    _trusted_start_script,
)
from sakura_ai_updater.contract import REPOSITORIES


class _Process:
    def __init__(
        self, returncode: int = 0, stdout: bytes = b"ok\n", stderr: bytes = b""
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


def _image_id(digit):
    return "sha256:" + digit * 64


def _cleanup_docker(monkeypatch, adapter, images, current, *, used=(), failed=()):
    calls = []
    current_refs = dict(zip(current, list(images)[:3], strict=True))

    async def run(argv, **kwargs):
        calls.append(argv)
        if argv[:4] == ["docker", "image", "inspect", "--format"]:
            ref = argv[5]
            image_id = current_refs.get(ref, ref)
            for key, image in images.items():
                if image_id == key or ref in image["RepoTags"] + image["RepoDigests"]:
                    return json.dumps({"Id": key, **image}), ""
            raise ImageCommandError(
                "not found", argv=argv, returncode=1, stderr="No such image"
            )
        if argv[:3] == ["docker", "container", "ls"]:
            return ("container-1\n" if used else ""), ""
        if argv[:3] == ["docker", "container", "inspect"]:
            return ("\n".join(used) + "\n"), ""
        if argv[:3] == ["docker", "image", "ls"]:
            return "\n".join(images) + "\n", ""
        if argv[:3] == ["docker", "image", "rm"]:
            assert argv[3] == "--no-prune"
            ref = argv[4]
            if ref in failed:
                raise ImageCommandError("image in use", argv=argv, returncode=1)
            if ref in images:
                if len(images[ref]["RepoTags"]) > 1:
                    raise ImageCommandError("multiple tags", argv=argv, returncode=1)
                del images[ref]
            else:
                for key, image in list(images.items()):
                    if ref in image["RepoTags"]:
                        image["RepoTags"].remove(ref)
                        if not image["RepoTags"]:
                            del images[key]
                        break
                else:
                    raise AssertionError(f"unexpected removal: {ref}")
            return "Deleted", ""
        raise AssertionError(argv)

    monkeypatch.setattr(adapter, "_run_command", run)
    return calls


@pytest.mark.asyncio
async def test_cleanup_only_removes_unused_official_images(tmp_path, monkeypatch):
    current = tuple(
        f"{repo}@sha256:{digit * 64}"
        for repo, digit in zip(REPOSITORIES.values(), "abc", strict=True)
    )
    images = {
        _image_id(digit): {
            "RepoTags": [
                f"{repo}:{'current' if digit in 'abc' else 'v1'}"
            ] if digit != "6" else [],
            "RepoDigests": [f"{repo}@sha256:{digit * 64}"],
        }
        for digit, repo in zip("abc456", (*REPOSITORIES.values(), *REPOSITORIES.values()), strict=True)
    }
    images[_image_id("7")] = {
        "RepoTags": ["ghcr.io/sakura520222/sakura-ai-evil:v1"],
        "RepoDigests": [],
    }
    images[_image_id("8")] = {
        "RepoTags": [f"{REPOSITORIES['web']}:shared", "ghcr.io/unrelated/service:shared"],
        "RepoDigests": [],
    }
    images[_image_id("9")] = {
        "RepoTags": [f"{REPOSITORIES['web']}:in-use"],
        "RepoDigests": [],
    }
    images[_image_id("0")] = {"RepoTags": [], "RepoDigests": []}
    adapter = ImageAdapter("compose.yml", str(tmp_path / "deployment.env"))
    calls = _cleanup_docker(
        monkeypatch, adapter, images, current, used=(_image_id("9"),)
    )

    removed, warnings = await adapter.cleanup_unused_sakura_images(current)

    assert warnings == []
    assert {entry.split(" ", 1)[0] for entry in removed} == {
        _image_id(digit) for digit in "456"
    }
    assert [call[4] for call in calls if call[:3] == ["docker", "image", "rm"]] == [
        f"{REPOSITORIES['web']}:v1",
        f"{REPOSITORIES['sandboxd']}:v1",
        _image_id("6"),
    ]
    assert set(images) == {_image_id(digit) for digit in "abc7890"}
    assert all("-f" not in call and "--force" not in call for call in calls)


@pytest.mark.asyncio
async def test_cleanup_untags_multiple_official_tags_without_force(tmp_path, monkeypatch):
    current = tuple(
        f"{repo}@sha256:{digit * 64}"
        for repo, digit in zip(REPOSITORIES.values(), "abc", strict=True)
    )
    images = {
        _image_id(digit): {"RepoTags": [], "RepoDigests": [ref]}
        for digit, ref in zip("abc", current, strict=True)
    }
    tags = [f"{REPOSITORIES['web']}:v1", f"{REPOSITORIES['web']}:old"]
    images[_image_id("4")] = {"RepoTags": tags.copy(), "RepoDigests": []}
    adapter = ImageAdapter("compose.yml", str(tmp_path / "deployment.env"))
    calls = _cleanup_docker(monkeypatch, adapter, images, current)

    removed, warnings = await adapter.cleanup_unused_sakura_images(current)

    assert len(removed) == 1 and removed[0].startswith(_image_id("4"))
    assert warnings == []
    assert [call[4] for call in calls if call[:3] == ["docker", "image", "rm"]] == tags
    assert _image_id("4") not in images


@pytest.mark.asyncio
async def test_cleanup_removal_failure_continues_without_force(tmp_path, monkeypatch):
    current = tuple(
        f"{repo}@sha256:{digit * 64}"
        for repo, digit in zip(REPOSITORIES.values(), "abc", strict=True)
    )
    images = {
        _image_id(digit): {"RepoTags": [], "RepoDigests": [ref]}
        for digit, ref in zip("abc", current, strict=True)
    }
    for digit in "45":
        images[_image_id(digit)] = {
            "RepoTags": [f"{REPOSITORIES['web']}:v{digit}"], "RepoDigests": []
        }
    adapter = ImageAdapter("compose.yml", str(tmp_path / "deployment.env"))
    _cleanup_docker(
        monkeypatch, adapter, images, current,
        failed=(f"{REPOSITORIES['web']}:v4",),
    )

    removed, warnings = await adapter.cleanup_unused_sakura_images(current)

    assert len(removed) == 1 and removed[0].startswith(_image_id("5"))
    assert len(warnings) == 1 and _image_id("4") in warnings[0]
    assert _image_id("4") in images


@pytest.mark.asyncio
async def test_cleanup_single_tag_replaced_by_unrelated_repo_is_not_removed(
    tmp_path, monkeypatch
):
    current = tuple(
        f"{repo}@sha256:{digit * 64}"
        for repo, digit in zip(REPOSITORIES.values(), "abc", strict=True)
    )
    images = {
        _image_id(digit): {"RepoTags": [], "RepoDigests": [ref]}
        for digit, ref in zip("abc", current, strict=True)
    }
    old_tag = f"{REPOSITORIES['web']}:v1"
    images[_image_id("4")] = {"RepoTags": [old_tag], "RepoDigests": []}
    adapter = ImageAdapter("compose.yml", str(tmp_path / "deployment.env"))
    calls = _cleanup_docker(monkeypatch, adapter, images, current)
    original_run = adapter._run_command

    async def retag_before_removal(argv, **kwargs):
        if argv[:3] == ["docker", "image", "rm"] and argv[4] == old_tag:
            images[_image_id("4")]["RepoTags"] = ["ghcr.io/unrelated/service:now-owned"]
            raise ImageCommandError("tag no longer exists", argv=argv, returncode=1)
        return await original_run(argv, **kwargs)

    monkeypatch.setattr(adapter, "_run_command", retag_before_removal)
    removed, warnings = await adapter.cleanup_unused_sakura_images(current)

    assert removed == []
    assert len(warnings) == 1
    assert images[_image_id("4")]["RepoTags"] == [
        "ghcr.io/unrelated/service:now-owned"
    ]
    assert not any(
        call[:3] == ["docker", "image", "rm"] and call[4] == _image_id("4")
        for call in calls
    )


@pytest.mark.asyncio
async def test_cleanup_rechecks_remaining_references_before_removing_image_id(
    tmp_path, monkeypatch
):
    current = tuple(
        f"{repo}@sha256:{digit * 64}"
        for repo, digit in zip(REPOSITORIES.values(), "abc", strict=True)
    )
    images = {
        _image_id(digit): {"RepoTags": [], "RepoDigests": [ref]}
        for digit, ref in zip("abc", current, strict=True)
    }
    old_tag = f"{REPOSITORIES['web']}:v1"
    images[_image_id("4")] = {
        "RepoTags": [old_tag],
        "RepoDigests": [f"{REPOSITORIES['web']}@sha256:{'4' * 64}"],
    }
    adapter = ImageAdapter("compose.yml", str(tmp_path / "deployment.env"))
    calls = _cleanup_docker(monkeypatch, adapter, images, current)
    original_run = adapter._run_command

    async def newly_shared_image(argv, **kwargs):
        result = await original_run(argv, **kwargs)
        if argv[:3] == ["docker", "image", "rm"] and argv[4] == old_tag:
            images[_image_id("4")] = {
                "RepoTags": ["ghcr.io/unrelated/service:new"],
                "RepoDigests": [f"{REPOSITORIES['web']}@sha256:{'4' * 64}"],
            }
        return result

    monkeypatch.setattr(adapter, "_run_command", newly_shared_image)
    removed, warnings = await adapter.cleanup_unused_sakura_images(current)

    assert removed == []
    assert len(warnings) == 1 and "references changed" in warnings[0]
    assert _image_id("4") in images
    assert not any(
        call[:3] == ["docker", "image", "rm"] and call[4] == _image_id("4")
        for call in calls
    )


@pytest.mark.asyncio
async def test_cleanup_fails_closed_when_container_inventory_is_incomplete(
    tmp_path, monkeypatch
):
    current = tuple(
        f"{repo}@sha256:{digit * 64}"
        for repo, digit in zip(REPOSITORIES.values(), "abc", strict=True)
    )
    images = {
        _image_id(digit): {"RepoTags": [], "RepoDigests": [ref]}
        for digit, ref in zip("abc", current, strict=True)
    }
    images[_image_id("4")] = {
        "RepoTags": [f"{REPOSITORIES['web']}:old"], "RepoDigests": []
    }
    adapter = ImageAdapter("compose.yml", str(tmp_path / "deployment.env"))
    calls = _cleanup_docker(monkeypatch, adapter, images, current, used=(_image_id("4"),))
    original_run = adapter._run_command

    async def incomplete_inventory(argv, **kwargs):
        if argv[:3] == ["docker", "container", "inspect"]:
            return "", ""
        return await original_run(argv, **kwargs)

    monkeypatch.setattr(adapter, "_run_command", incomplete_inventory)
    with pytest.raises(ImageAdapterError, match="invalid Docker container image inventory"):
        await adapter.cleanup_unused_sakura_images(current)
    assert not any(call[:3] == ["docker", "image", "rm"] for call in calls)
    assert _image_id("4") in images


@pytest.mark.asyncio
async def test_cleanup_has_single_deadline_for_complete_image_scan(tmp_path, monkeypatch):
    current = tuple(
        f"{repo}@sha256:{digit * 64}"
        for repo, digit in zip(REPOSITORIES.values(), "abc", strict=True)
    )
    adapter = ImageAdapter(
        "compose.yml", str(tmp_path / "deployment.env"), command_timeout=0.01
    )

    async def slow_inventory(argv, **kwargs):
        await asyncio.sleep(1)
        raise AssertionError("cleanup deadline did not cancel the inventory command")

    monkeypatch.setattr(adapter, "_run_command", slow_inventory)

    with pytest.raises(ImageCommandError) as caught:
        await adapter.cleanup_unused_sakura_images(current)

    assert caught.value.error_code == "cleanup_timeout"


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
async def test_activate_writes_env_and_uses_explicit_compose_env_file(
    tmp_path, monkeypatch
):
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
    assert "SAKURA_AI_IMAGE=ghcr.io/example/app:v3.1.0" in env.read_text(
        encoding="utf-8"
    )
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
async def test_activate_rejects_invalid_persisted_compose_project(
    tmp_path, monkeypatch
):
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


@pytest.mark.asyncio
async def test_activate_is_three_image_transaction_and_reconciles_sandbox_first(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    docker_dir = project / "docker"
    docker_dir.mkdir(parents=True)
    compose = docker_dir / "docker-compose.prod.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    start = project / "start.sh"
    start.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    env = tmp_path / "deployment.env"
    env.write_text(
        "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.0.0\n"
        "SAKURA_SANDBOXD_IMAGE_DIGEST=ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:"
        + "a" * 64
        + "\nSAKURA_AGENT_RUNNER_IMAGE_DIGEST=ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:"
        + "b" * 64
        + "\nCOMPOSE_PROJECT_NAME=sakura-ai\nOTHER=keep\n",
        encoding="utf-8",
    )
    sandboxd = "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:" + "c" * 64
    runner = "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:" + "d" * 64
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*argv, **kwargs):
        calls.append(tuple(argv))
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter(str(compose), str(env))
    await adapter.activate("ghcr.io/sakura520222/sakura-ai:v3.1.0", sandboxd, runner)

    content = env.read_text(encoding="utf-8")
    assert "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.1.0\n" in content
    assert f"SAKURA_SANDBOXD_IMAGE_DIGEST={sandboxd}\n" in content
    assert f"SAKURA_AGENT_RUNNER_IMAGE_DIGEST={runner}\n" in content
    assert calls[0][0:4] == ("bash", str(start.resolve()), "sandboxd", "reinstall")
    assert calls[1][-2:] == ("up", "-d")


@pytest.mark.asyncio
async def test_sandbox_restart_failure_restores_all_three_image_keys(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    docker_dir = project / "docker"
    docker_dir.mkdir(parents=True)
    compose = docker_dir / "docker-compose.prod.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    (project / "start.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    old_web = "ghcr.io/sakura520222/sakura-ai:v3.0.0"
    old_sandboxd = "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:" + "a" * 64
    old_runner = "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:" + "b" * 64
    env = tmp_path / "deployment.env"
    env.write_text(
        f"SAKURA_AI_IMAGE={old_web}\n"
        f"SAKURA_SANDBOXD_IMAGE_DIGEST={old_sandboxd}\n"
        f"SAKURA_AGENT_RUNNER_IMAGE_DIGEST={old_runner}\n"
        "COMPOSE_PROJECT_NAME=sakura-ai\n",
        encoding="utf-8",
    )
    new_sandboxd = "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:" + "c" * 64
    new_runner = "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:" + "d" * 64
    calls: list[tuple[str, ...]] = []
    restart_attempts = 0

    async def fake_exec(*argv, **kwargs):
        nonlocal restart_attempts
        command = tuple(argv)
        calls.append(command)
        if command[0:4] == ("bash", str((project / "start.sh").resolve()), "sandboxd", "reinstall"):
            restart_attempts += 1
            if restart_attempts == 1:
                return _Process(1, b"", b"sandbox not ready")
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter(str(compose), str(env))
    with pytest.raises(ImageCommandError):
        await adapter.activate("ghcr.io/sakura520222/sakura-ai:v3.1.0", new_sandboxd, new_runner)

    content = env.read_text(encoding="utf-8")
    assert f"SAKURA_AI_IMAGE={old_web}\n" in content
    assert f"SAKURA_SANDBOXD_IMAGE_DIGEST={old_sandboxd}\n" in content
    assert f"SAKURA_AGENT_RUNNER_IMAGE_DIGEST={old_runner}\n" in content
    assert restart_attempts == 2


@pytest.mark.asyncio
async def test_partial_old_sandbox_pair_is_rejected_before_any_write_or_reinstall(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    docker_dir = project / "docker"
    docker_dir.mkdir(parents=True)
    compose = docker_dir / "docker-compose.prod.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    (project / "start.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    old_partial = "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:" + "a" * 64
    env = tmp_path / "deployment.env"
    env.write_text(
        "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.0.0\n"
        f"SAKURA_SANDBOXD_IMAGE_DIGEST={old_partial}\n"
        "COMPOSE_PROJECT_NAME=sakura-ai\n",
        encoding="utf-8",
    )
    before = env.read_bytes()
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*argv, **kwargs):
        calls.append(tuple(argv))
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter(str(compose), str(env))
    sandboxd = "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:" + "c" * 64
    runner = "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:" + "d" * 64
    with pytest.raises(ImageAdapterError, match="incomplete deployment has no verified sandbox rollback pair"):
        await adapter.activate("ghcr.io/sakura520222/sakura-ai:v3.1.0", sandboxd, runner)

    assert env.read_bytes() == before
    assert all(call[:2] == ("docker", "inspect") for call in calls)


@pytest.mark.asyncio
async def test_legacy_activation_failure_uninstalls_new_sandbox_before_old_compose(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    docker_dir = project / "docker"
    docker_dir.mkdir(parents=True)
    compose = docker_dir / "docker-compose.prod.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    start = project / "start.sh"
    start.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    env = tmp_path / "deployment.env"
    old_web = "ghcr.io/sakura520222/sakura-ai:v3.0.0"
    env.write_text(
        f"SAKURA_AI_IMAGE={old_web}\nCOMPOSE_PROJECT_NAME=sakura-ai\n",
        encoding="utf-8",
    )
    new_sandboxd = "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:" + "c" * 64
    new_runner = "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:" + "d" * 64
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*argv, **kwargs):
        command = tuple(argv)
        calls.append(command)
        if command[-2:] == ("up", "-d") and calls.count(command) == 1:
            return _Process(1, b"", b"new Web failed")
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter(str(compose), str(env))
    with pytest.raises(ImageCommandError):
        await adapter.activate("ghcr.io/sakura520222/sakura-ai:v3.1.0", new_sandboxd, new_runner)

    assert calls == [
        ("bash", str(start.resolve()), "sandboxd", "reinstall"),
        (
            "docker",
            "compose",
            "--env-file",
            str(env),
            "--project-name",
            "sakura-ai",
            "-f",
            str(compose),
            "up",
            "-d",
        ),
        ("bash", str(start.resolve()), "sandboxd", "uninstall"),
        (
            "docker",
            "compose",
            "--env-file",
            str(env),
            "--project-name",
            "sakura-ai",
            "-f",
            str(compose),
            "up",
            "-d",
        ),
    ]
    assert env.read_text(encoding="utf-8") == (
        f"SAKURA_AI_IMAGE={old_web}\nCOMPOSE_PROJECT_NAME=sakura-ai\n"
    )


@pytest.mark.asyncio
async def test_legacy_rollback_does_not_start_old_compose_after_bounded_uninstall_failure(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    docker_dir = project / "docker"
    docker_dir.mkdir(parents=True)
    compose = docker_dir / "docker-compose.prod.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    start = project / "start.sh"
    start.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    env = tmp_path / "deployment.env"
    old_web = "ghcr.io/sakura520222/sakura-ai:v3.0.0"
    env.write_text(
        f"SAKURA_AI_IMAGE={old_web}\nCOMPOSE_PROJECT_NAME=sakura-ai\n",
        encoding="utf-8",
    )
    new_sandboxd = "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:" + "c" * 64
    new_runner = "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:" + "d" * 64
    calls: list[tuple[str, ...]] = []
    compose_attempts = 0

    async def fake_exec(*argv, **kwargs):
        nonlocal compose_attempts
        command = tuple(argv)
        calls.append(command)
        if command[-2:] == ("up", "-d"):
            compose_attempts += 1
            if compose_attempts == 1:
                return _Process(1, b"", b"new Web failed")
            pytest.fail("old Web Compose must not run after uninstall failure")
        if command[-2:] == ("sandboxd", "uninstall"):
            return _Process(1, b"", b"sandbox busy")
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter(str(compose), str(env))
    with pytest.raises(ImageAdapterError, match="rollback incomplete"):
        await adapter.activate("ghcr.io/sakura520222/sakura-ai:v3.1.0", new_sandboxd, new_runner)

    assert calls == [
        ("bash", str(start.resolve()), "sandboxd", "reinstall"),
        (
            "docker",
            "compose",
            "--env-file",
            str(env),
            "--project-name",
            "sakura-ai",
            "-f",
            str(compose),
            "up",
            "-d",
        ),
        ("bash", str(start.resolve()), "sandboxd", "uninstall"),
        ("bash", str(start.resolve()), "sandboxd", "uninstall"),
    ]
    assert env.read_text(encoding="utf-8") == (
        f"SAKURA_AI_IMAGE={old_web}\nCOMPOSE_PROJECT_NAME=sakura-ai\n"
    )


@pytest.mark.asyncio
async def test_legacy_rollback_retries_uninstall_then_starts_old_compose(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    docker_dir = project / "docker"
    docker_dir.mkdir(parents=True)
    compose = docker_dir / "docker-compose.prod.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    start = project / "start.sh"
    start.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    env = tmp_path / "deployment.env"
    old_web = "ghcr.io/sakura520222/sakura-ai:v3.0.0"
    env.write_text(
        f"SAKURA_AI_IMAGE={old_web}\nCOMPOSE_PROJECT_NAME=sakura-ai\n",
        encoding="utf-8",
    )
    new_sandboxd = "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:" + "c" * 64
    new_runner = "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:" + "d" * 64
    calls: list[tuple[str, ...]] = []
    compose_attempts = 0
    uninstall_attempts = 0

    async def fake_exec(*argv, **kwargs):
        nonlocal compose_attempts, uninstall_attempts
        command = tuple(argv)
        calls.append(command)
        if command[-2:] == ("up", "-d"):
            compose_attempts += 1
            if compose_attempts == 1:
                return _Process(1, b"", b"new Web failed")
            return _Process()
        if command[-2:] == ("sandboxd", "uninstall"):
            uninstall_attempts += 1
            if uninstall_attempts == 1:
                return _Process(1, b"", b"sandbox busy")
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter(str(compose), str(env))
    with pytest.raises(ImageCommandError):
        await adapter.activate("ghcr.io/sakura520222/sakura-ai:v3.1.0", new_sandboxd, new_runner)

    assert uninstall_attempts == 2
    assert compose_attempts == 2
    assert calls[-3:] == [
        ("bash", str(start.resolve()), "sandboxd", "uninstall"),
        ("bash", str(start.resolve()), "sandboxd", "uninstall"),
        (
            "docker",
            "compose",
            "--env-file",
            str(env),
            "--project-name",
            "sakura-ai",
            "-f",
            str(compose),
            "up",
            "-d",
        ),
    ]
    assert env.read_text(encoding="utf-8") == (
        f"SAKURA_AI_IMAGE={old_web}\nCOMPOSE_PROJECT_NAME=sakura-ai\n"
    )


def _fake_root_lstat(monkeypatch, *, file_mode=0o700, file_uid=0, parent_mode=0o755):
    real_lstat = os.lstat

    def fake_lstat(path):
        path = os.fspath(path)
        result = real_lstat(path)
        if stat.S_ISDIR(result.st_mode):
            mode = stat.S_IFDIR | parent_mode
        else:
            mode = stat.S_IFREG | file_mode
        return SimpleNamespace(st_mode=mode, st_uid=file_uid)

    monkeypatch.setattr(image_module.os, "lstat", fake_lstat)
    return fake_lstat


def test_production_start_script_trusted_file_accepts_root_owned_tree(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    start = project / "start.sh"
    start.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    _fake_root_lstat(monkeypatch)
    assert _trusted_start_script(start, project, lstat=image_module.os.lstat) == start


@pytest.mark.parametrize(
    ("file_mode", "file_uid", "parent_mode", "message"),
    [
        (0o700, 1000, 0o755, "owned by root"),
        (0o720, 0, 0o755, "group/other writable"),
        (0o700, 0, 0o777, "parent"),
    ],
)
def test_production_start_script_rejects_owner_or_shared_writable_tree(
    tmp_path, monkeypatch, file_mode, file_uid, parent_mode, message
):
    project = tmp_path / "project"
    project.mkdir()
    start = project / "start.sh"
    start.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    _fake_root_lstat(
        monkeypatch,
        file_mode=file_mode,
        file_uid=file_uid,
        parent_mode=parent_mode,
    )
    with pytest.raises(ImageAdapterError, match=message):
        _trusted_start_script(start, project, lstat=image_module.os.lstat)


def test_production_start_script_rejects_symlink_and_outside_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    real_start = project / "real-start.sh"
    real_start.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    symlink = project / "start.sh"
    try:
        symlink.symlink_to(real_start)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(ImageAdapterError, match="symlinks"):
        _trusted_start_script(symlink, project, lstat=os.lstat)


@pytest.mark.asyncio
async def test_rollback_auto_pulls_missing_baseline_runner_on_reinstall_failure(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    docker_dir = project / "docker"
    docker_dir.mkdir(parents=True)
    compose = docker_dir / "docker-compose.prod.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    start = project / "start.sh"
    start.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    old_web = "ghcr.io/sakura520222/sakura-ai:v3.0.0"
    old_sandboxd = "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:" + "a" * 64
    old_runner = "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:" + "b" * 64
    env = tmp_path / "deployment.env"
    env.write_text(
        f"SAKURA_AI_IMAGE={old_web}\n"
        f"SAKURA_SANDBOXD_IMAGE_DIGEST={old_sandboxd}\n"
        f"SAKURA_AGENT_RUNNER_IMAGE_DIGEST={old_runner}\n"
        "COMPOSE_PROJECT_NAME=sakura-ai\n",
        encoding="utf-8",
    )
    new_sandboxd = "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:" + "c" * 64
    new_runner = "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:" + "d" * 64
    calls: list[tuple[str, ...]] = []
    reinstall_attempts = 0

    async def fake_exec(*argv, **kwargs):
        nonlocal reinstall_attempts
        command = tuple(argv)
        calls.append(command)
        if command == ("docker", "image", "inspect", old_runner) and (
            "docker", "pull", old_runner
        ) not in calls:
            return _Process(1, b"", b"runner image missing")
        if command[0:4] == ("bash", str((project / "start.sh").resolve()), "sandboxd", "reinstall"):
            reinstall_attempts += 1
            if reinstall_attempts == 1:
                # Activation reinstall fails (e.g. new sandboxd error)
                return _Process(1, b"", b"[FAIL] sandboxd failed to start")
            elif reinstall_attempts == 2:
                # First rollback reinstall fails because old runner was deleted locally!
                return _Process(1, b"", b"[FAIL] agent-runner image missing")
            elif reinstall_attempts == 3:
                # After auto-pull of runner, second rollback reinstall succeeds
                return _Process()
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter(str(compose), str(env))
    # Activation will fail, but rollback should auto-pull old_runner and succeed!
    with pytest.raises(ImageCommandError):
        await adapter.activate("ghcr.io/sakura520222/sakura-ai:v3.1.0", new_sandboxd, new_runner)

    content = env.read_text(encoding="utf-8")
    assert f"SAKURA_AI_IMAGE={old_web}\n" in content
    assert f"SAKURA_SANDBOXD_IMAGE_DIGEST={old_sandboxd}\n" in content
    assert f"SAKURA_AGENT_RUNNER_IMAGE_DIGEST={old_runner}\n" in content
    # Verify old_runner was pulled during rollback self-healing
    assert ("docker", "pull", old_runner) in calls
    assert reinstall_attempts == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("missing_runner", "pull_fails", "retry_fails", "expected", "reinstall_count"),
    [
        (False, False, False, "baseline images present", 1),
        (True, True, False, "missing agent-runner image", 1),
        (True, False, True, "retry: second reinstall failure", 2),
    ],
)
async def test_rollback_reports_missing_image_and_retry_failure(
    tmp_path, monkeypatch, missing_runner, pull_fails, retry_fails, expected, reinstall_count
):
    project = tmp_path / "project"
    docker_dir = project / "docker"
    docker_dir.mkdir(parents=True)
    compose = docker_dir / "docker-compose.prod.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    (project / "start.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    runner = "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:" + "b" * 64
    sandboxd = "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:" + "a" * 64
    env = tmp_path / "deployment.env"
    env.write_text(
        f"SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.0.0\n"
        f"SAKURA_SANDBOXD_IMAGE_DIGEST={sandboxd}\n"
        f"SAKURA_AGENT_RUNNER_IMAGE_DIGEST={runner}\n"
        "COMPOSE_PROJECT_NAME=sakura-ai\n",
        encoding="utf-8",
    )
    calls = []
    reinstall = 0

    async def fake_exec(*argv, **kwargs):
        nonlocal reinstall
        command = tuple(argv)
        calls.append(command)
        if command[0] == "bash":
            reinstall += 1
            detail = b"first reinstall failure" if reinstall == 1 else b"second reinstall failure"
            return _Process(1 if reinstall == 1 or retry_fails else 0, b"", detail)
        if command == ("docker", "image", "inspect", runner) and missing_runner:
            if ("docker", "pull", runner) not in calls or pull_fails:
                return _Process(1, b"", b"image missing")
        if command == ("docker", "pull", runner) and pull_fails:
            return _Process(1, b"", b"registry unavailable")
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    adapter = ImageAdapter(str(compose), str(env))
    snapshot = await adapter.capture_snapshot()
    with pytest.raises(ImageAdapterError, match=expected) as error:
        await adapter.rollback(snapshot)
    assert "first reinstall failure" in str(error.value)
    assert reinstall == reinstall_count
    assert (("docker", "pull", runner) in calls) is missing_runner
    assert ("docker", "pull", sandboxd) not in calls
    if retry_fails:
        assert "second reinstall failure" in str(error.value)
    if pull_fails:
        assert "registry unavailable" in str(error.value)
