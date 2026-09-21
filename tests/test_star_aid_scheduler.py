"""Scheduler lifecycle and dynamic runtime control tests for Star Aid."""

from unittest.mock import AsyncMock

import pytest

from backend.services.star_aid_scheduler import StarAidScheduler


def test_scheduler_disabled_at_start_does_not_run(monkeypatch):
    """配置 star_aid_scheduler_enabled=False 时启动不创建运行 scheduler。"""
    class FakeSettings:
        star_aid_scheduler_enabled = False
        enable_scheduler = True

    monkeypatch.setattr("backend.services.star_aid_scheduler.get_settings", lambda: FakeSettings())

    scheduler = StarAidScheduler()
    scheduler.start()

    assert scheduler.is_running is False
    assert scheduler._scheduler is None


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
    assert scheduler.is_running is False


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
    async def fake_cfg(k):
        return k != "star_aid_scheduler_enabled" 

    monkeypatch.setattr("backend.services.star_aid_scheduler.get_dynamic_config", fake_cfg)

    await scheduler._run_tick()
    assert fake_worker.run_tick.call_count == 0

    # 2. 配置为 True
    async def fake_cfg_enabled(k):
        return True

    monkeypatch.setattr("backend.services.star_aid_scheduler.get_dynamic_config", fake_cfg_enabled)

    await scheduler._run_tick()
    assert fake_worker.run_tick.call_count == 1
