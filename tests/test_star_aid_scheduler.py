"""Scheduler lifecycle and dynamic runtime control tests for Star Aid."""

from unittest.mock import AsyncMock

import pytest

from backend.services.star_aid_scheduler import StarAidScheduler


@pytest.mark.asyncio
async def test_scheduler_disabled_at_start_keeps_control_timer_running(monkeypatch):
    """Disabled work still needs a timer to observe another replica's enable."""
    class FakeSettings:
        star_aid_scheduler_enabled = False
        enable_scheduler = True

    monkeypatch.setattr("backend.services.star_aid_scheduler.get_settings", lambda: FakeSettings())

    scheduler = StarAidScheduler()
    scheduler.start()

    assert scheduler.is_running is True
    scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_dynamic_toggle_and_idempotence(monkeypatch):
    """运行时动态启停测试与幂等性校验。"""
    settings = type("Settings", (), {
        "star_aid_scheduler_enabled": True,
        "enable_scheduler": True,
    })()

    monkeypatch.setattr("backend.services.star_aid_scheduler.get_settings", lambda: settings)

    scheduler = StarAidScheduler()

    # 1. 启动
    scheduler.start()
    assert scheduler.is_running is True

    # 2. 重复启动应幂等，不崩溃
    scheduler.start()
    assert scheduler.is_running is True

    # 3. 停止
    scheduler.stop()
    assert scheduler.is_running is False

    # 4. 重复停止幂等
    scheduler.stop()
    assert scheduler.is_running is False

    # 5. 动态重启测试
    settings.star_aid_scheduler_enabled = True
    scheduler.restart_if_needed()
    assert scheduler.is_running is True

    settings.star_aid_scheduler_enabled = False
    scheduler.restart_if_needed()
    assert scheduler.is_running is True

    settings.enable_scheduler = False
    scheduler.restart_if_needed()
    assert scheduler.is_running is False
    settings.enable_scheduler = True
    scheduler.restart_if_needed()
    assert scheduler.is_running is True
    scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_tick_dynamically_checks_enabled(monkeypatch):
    """_run_tick 运行时如果 star_aid_scheduler_enabled 为 False，跳过 worker.run_tick。"""
    scheduler = StarAidScheduler()
    fake_worker = AsyncMock()
    scheduler._worker = fake_worker

    # 模拟后台任务注册返回非 None
    monkeypatch.setattr(
        "backend.services.database_reset_runtime_service.register_current_background_task",
        lambda name: object(),
    )

    # 1. 配置为 False
    calls = []

    async def fake_cfg(k, *, fresh=False):
        calls.append((k, fresh))
        return k != "star_aid_scheduler_enabled" 

    monkeypatch.setattr("backend.services.star_aid_scheduler.get_dynamic_config", fake_cfg)

    await scheduler._run_tick()
    assert fake_worker.run_tick.call_count == 0
    assert calls == [("star_aid_scheduler_enabled", True)]

    # 2. 配置为 True
    async def fake_cfg_enabled(k, *, fresh=False):
        return True

    monkeypatch.setattr("backend.services.star_aid_scheduler.get_dynamic_config", fake_cfg_enabled)

    await scheduler._run_tick()
    assert fake_worker.run_tick.call_count == 1


@pytest.mark.asyncio
async def test_two_replicas_observe_remote_toggle_after_disabled_start(monkeypatch):
    """The timer remains alive and each replica reads the shared DB switch."""
    settings = type("Settings", (), {
        "star_aid_scheduler_enabled": False,
        "enable_scheduler": True,
    })()
    monkeypatch.setattr(
        "backend.services.star_aid_scheduler.get_settings", lambda: settings
    )
    monkeypatch.setattr(
        "backend.services.database_reset_runtime_service.register_current_background_task",
        lambda name: object(),
    )
    db_state = {"enabled": False}
    reads = []

    async def read_config(key, *, fresh=False):
        if key == "star_aid_scheduler_enabled":
            reads.append(fresh)
            return db_state["enabled"]
        return True

    monkeypatch.setattr(
        "backend.services.star_aid_scheduler.get_dynamic_config", read_config
    )
    replicas = [StarAidScheduler(), StarAidScheduler()]
    try:
        for replica in replicas:
            replica.start()
            assert replica.is_running
            replica._worker = AsyncMock()

        for enabled in (False, True, False):
            db_state["enabled"] = enabled
            for replica in replicas:
                await replica._run_tick()

        assert reads == [True] * 6
        assert all(replica._worker.run_tick.await_count == 1 for replica in replicas)
        assert all(replica.is_running for replica in replicas)
    finally:
        for replica in replicas:
            replica.stop()
