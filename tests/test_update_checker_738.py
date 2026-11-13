"""Update check scheduling, deployment routing and concurrent request regressions."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.services import update_checker as module
from backend.services.database_reset_runtime_service import (
    DatabaseResetRuntimeSupervisor,
    bind_runtime_supervisor,
    reset_runtime_supervisor,
)


@pytest.fixture
def cache(monkeypatch):
    store = {}

    async def read():
        return store.copy() or None

    async def write(data):
        store.clear()
        store.update(data)

    monkeypatch.setattr(module, "read_cache", read)
    monkeypatch.setattr(module, "write_cache", write)
    return store


@pytest.mark.asyncio
async def test_overlapping_checks_share_one_request_and_survive_waiter_cancel(cache):
    checker = module.UpdateChecker(repo="example/project")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def discover():
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return []

    checker._client.list_releases = discover
    first = asyncio.create_task(checker.check_once())
    await entered.wait()
    second = asyncio.create_task(checker.check_once())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    try:
        assert (await second)["check_error"] is None
        assert calls == 1
    finally:
        await checker.stop()


@pytest.mark.asyncio
async def test_channel_check_uses_same_cycle_and_keeps_last_good_on_error(cache):
    callback = AsyncMock(
        return_value={
            "data": {
                "target": {"channel": "development", "version": "3.0.2"},
                "update_available": True,
            }
        }
    )
    checker = module.UpdateChecker(repo="example/project", channel_check=callback)
    checker._client.list_releases = AsyncMock(return_value=[])
    try:
        first = await checker.check_once()
        assert first["channel_result"]["data"]["target"]["channel"] == "development"
        callback.side_effect = RuntimeError("network")
        failed = await checker.check_once()
        assert failed["channel_result"] == first["channel_result"]
        assert failed["check_error"] == "network"
        assert failed["last_checked"]
        callback.side_effect = None
        assert (await checker.check_once())["check_error"] is None
        assert callback.await_count == 3
    finally:
        await checker.stop()


@pytest.mark.asyncio
async def test_scheduler_status_starts_stops_and_can_restart(cache, monkeypatch):
    monkeypatch.setattr(module.UpdateChecker, "STARTUP_DELAY_SECONDS", 3600)
    checker = module.UpdateChecker(repo="example/project")
    assert checker.status()["scheduler_running"] is False
    token = bind_runtime_supervisor(DatabaseResetRuntimeSupervisor())
    checker.start()
    try:
        assert checker.status()["scheduler_running"] is True
        assert checker.status()["next_check_at"]
        original = checker._task
        checker.start()
        assert checker._task is original
    finally:
        await checker.stop()
    assert checker.status()["next_check_at"] is None
    assert checker.status()["scheduler_running"] is False
    checker.start()
    try:
        assert not checker._client._http.is_closed
        assert checker.status()["scheduler_running"] is True
    finally:
        await checker.stop()
        reset_runtime_supervisor(token)


def test_development_status_uses_last_good_channel_check_when_updater_is_offline(
    monkeypatch,
):
    from backend.api.v1 import version

    monkeypatch.setattr(
        version,
        "get_build_info",
        lambda: {"channel": "development", "revision": "a" * 40},
    )
    info = version.build_version_info(
        "image",
        update_info={
            "latest_version": "99.0.0",
            "channel_build": {"channel": "development", "revision": "a" * 40},
            "last_checked": "2026-11-13T01:00:00Z",
            "check_error": "timeout",
            "channel_result": {
                "data": {
                    "target": {"channel": "development", "version": "3.2.6"},
                    "update_available": True,
                }
            },
        },
    )
    assert info["latest_version"] == "3.2.6"
    assert info["update_available"] is True
    assert info["check_error"] == "timeout"
    assert info["last_checked"] == "2026-11-13T01:00:00Z"
    assert info["update_supported"] is False


@pytest.mark.asyncio
async def test_manual_request_joins_the_scheduler_inflight_cycle(cache):
    import json
    from types import SimpleNamespace

    from fastapi import Request

    from backend.api.v1 import version

    checker = module.UpdateChecker(repo="example/project")
    entered, release = asyncio.Event(), asyncio.Event()

    async def discover():
        entered.set()
        await release.wait()
        return []

    checker._client.list_releases = AsyncMock(side_effect=discover)
    scheduled = asyncio.create_task(checker.check_once())
    await entered.wait()
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(update_checker=checker)),
        }
    )
    manual = asyncio.create_task(version.trigger_check(request, user={}, _csrf=""))
    await asyncio.sleep(0)
    release.set()
    try:
        automatic_data, response = await asyncio.gather(scheduled, manual)
        assert json.loads(response.body) == automatic_data
        assert checker._client.list_releases.await_count == 1
    finally:
        await checker.stop()


def test_stable_image_uses_checked_digest_availability_instead_of_release_semver(
    monkeypatch,
):
    from backend import __version__
    from backend.api.v1 import version

    build = {"channel": "stable", "revision": "a" * 40, "digest": "sha256:" + "1" * 64}
    monkeypatch.setattr(version, "get_build_info", lambda: build)
    info = version.build_version_info(
        "image",
        update_info={
            "latest_version": __version__,
            "channel_build": build,
            "channel_result": {
                "data": {
                    "target": {
                        "channel": "stable",
                        "version": __version__,
                        "digest": "sha256:" + "2" * 64,
                    },
                    "update_available": True,
                }
            },
        },
    )
    assert info["latest_version"] == __version__
    assert info["update_available"] is True


@pytest.mark.asyncio
async def test_scheduled_cycle_recovers_after_network_failure(cache, monkeypatch):
    monkeypatch.setattr(module.UpdateChecker, "STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(module.UpdateChecker, "CHECK_INTERVAL_SECONDS", 0)
    recovered = asyncio.Event()
    calls = 0

    async def discover():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary outage")
        recovered.set()
        return []

    token = bind_runtime_supervisor(DatabaseResetRuntimeSupervisor())
    checker = module.UpdateChecker(repo="example/project")
    checker._client.list_releases = discover
    checker.start()
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
        assert cache["check_error"] is None
        assert checker.status()["scheduler_running"] is True
    finally:
        await checker.stop()
        reset_runtime_supervisor(token)
