"""Explicit capacity boundary for existing isolated Worker unit tests.

These tests mock their persistence/runtime already. New service capacity and
Worker integration tests instead exercise the real database lease implementation.
"""

import asyncio
from contextlib import asynccontextmanager


def install_unit_worker_capacity(monkeypatch):
    from backend.services import service_execution_capacity
    from backend.workers import agent_team_worker

    semaphores = {
        feature: asyncio.Semaphore(maximum)
        for feature, maximum in (("pr_review", 5), ("issue_analysis", 5), ("agent", 1))
    }

    @asynccontextmanager
    async def slot(feature, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        async with semaphores[feature]:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            yield

    monkeypatch.setattr(service_execution_capacity, "service_execution_slot", slot)
    monkeypatch.setattr(agent_team_worker, "service_execution_slot", slot)
