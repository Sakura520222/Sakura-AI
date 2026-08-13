from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.quota_scheduler import QuotaResetScheduler


@pytest.mark.asyncio
async def test_quota_reset_cron_trigger_is_explicitly_utc(monkeypatch):
    monkeypatch.setattr(
        "backend.services.quota_scheduler.get_settings",
        lambda: SimpleNamespace(enable_scheduler=True),
    )
    scheduler = QuotaResetScheduler()

    scheduler.start()
    try:
        job = scheduler._scheduler.get_job("quota_reset_daily")
        assert job is not None
        assert str(job.trigger.timezone) == "UTC"
        assert job.next_run_time.tzinfo == job.trigger.timezone
        assert job.next_run_time.hour == 0
        assert job.next_run_time.minute == 0
    finally:
        scheduler.stop()
