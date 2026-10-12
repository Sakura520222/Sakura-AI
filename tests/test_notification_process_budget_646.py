"""Two independent processes contend for the same durable provider budget."""
import asyncio
import multiprocessing
from types import SimpleNamespace

from sqlalchemy import create_engine

from backend.models import Base


def _budget_process(database_path, barrier, result):
    from backend.services import notification_dispatch
    from backend.services.notification_service import NotificationService
    from tests.test_notification_events_646 import AsyncDB

    engine = create_engine(f"sqlite:///{database_path}")
    notification_dispatch.get_settings = lambda: SimpleNamespace(notification_rate_limit_seconds=60)
    barrier.wait(timeout=30)

    async def reserve():
        async with AsyncDB(engine) as db:
            available = await NotificationService()._reserve_provider_start(db, "email")
            result.put(available is None)

    asyncio.run(reserve())
    engine.dispose()


def test_provider_budget_is_shared_across_processes(tmp_path):
    path = tmp_path / "budget.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_budget_process, args=(str(path), barrier, results)) for _ in range(2)]
    for process in processes:
        process.start()
    try:
        values = [results.get(timeout=45) for _ in processes]
        assert sorted(values) == [False, True]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
