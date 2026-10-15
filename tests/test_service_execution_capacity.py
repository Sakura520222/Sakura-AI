"""Real SQL and process evidence for service-wide execution capacity."""

import asyncio
import importlib
import multiprocessing
from contextlib import asynccontextmanager
from datetime import timedelta
from queue import Empty

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.core.time_service import monotonic, now_utc
from backend.models.database import AppConfig, Base
from tests.test_billing_wallet import SQLSession


def core():
    assert importlib.util.find_spec("backend.services.service_execution_capacity"), (
        "Workers need a database-backed service capacity limiter"
    )
    return importlib.import_module("backend.services.service_execution_capacity")


def test_workers_need_a_database_backed_capacity_limiter():
    core()


class RealSQLSession(SQLSession):
    def __init__(self, session):
        self.session = session

    async def execute(self, *args, **kwargs):
        return self.session.execute(*args, **kwargs)

    def add(self, value):
        self.session.add(value)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    @asynccontextmanager
    async def begin(self):
        with self.session.begin():
            yield


class RealSQLFactory:
    def __init__(self, engine):
        self.engine = engine

    @asynccontextmanager
    async def __call__(self):
        with Session(self.engine, expire_on_commit=False) as session:
            yield RealSQLSession(session)


@pytest.fixture
def capacity_db(tmp_path):
    module = core()
    path = tmp_path / "capacity.sqlite"
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"autocommit": False, "timeout": 0.2}
    )
    Base.metadata.create_all(engine)
    factory = RealSQLFactory(engine)
    limiter = module.ServiceExecutionCapacity(
        session_factory=factory,
        max_concurrency=1,
        lease_seconds=2,
        poll_seconds=0.005,
    )
    yield module, limiter, engine, path
    engine.dispose()


@pytest.mark.asyncio
async def test_database_capacity_waits_across_instances_and_releases_on_exception(
    capacity_db,
):
    module, limiter, engine, _ = capacity_db
    second = module.ServiceExecutionCapacity(
        session_factory=RealSQLFactory(engine),
        max_concurrency=1,
        lease_seconds=2,
        poll_seconds=0.005,
    )
    entered = asyncio.Event()

    async def waiting():
        async with second.slot("pr_review"):
            entered.set()

    with pytest.raises(ValueError, match="business failure"):
        async with limiter.slot("pr_review"):
            task = asyncio.create_task(waiting())
            await asyncio.sleep(0.04)
            assert not entered.is_set()
            raise ValueError("business failure")
    await asyncio.wait_for(task, 1)
    with Session(engine) as session:
        assert not session.scalars(select(module.ServiceExecutionLease)).all()


@pytest.mark.asyncio
async def test_waiting_cancel_does_not_acquire_or_consume_slot(capacity_db):
    module, limiter, engine, _ = capacity_db
    cancel = asyncio.Event()
    async with limiter.slot("agent"):

        async def waiting():
            async with limiter.slot("agent", cancel_event=cancel):
                pytest.fail("cancelled work must never execute")

        task = asyncio.create_task(waiting())
        await asyncio.sleep(0.02)
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        with Session(engine) as session:
            assert len(session.scalars(select(module.ServiceExecutionLease)).all()) == 1


@pytest.mark.asyncio
async def test_nested_slot_is_reentrant_only_in_the_same_task(capacity_db, monkeypatch):
    _, limiter, _, _ = capacity_db
    # This checks task identity, not expiry after a slow CI scheduling interval.
    instant = now_utc()
    limiter.clock = lambda: instant
    entered = asyncio.Event()
    child_attempted = asyncio.Event()
    real_acquire = limiter.try_acquire

    async def observe_acquisition(feature):
        lease = await real_acquire(feature)
        child_attempted.set()
        return lease

    task = None
    try:
        async with limiter.slot("issue_analysis") as outer:
            async with limiter.slot("issue_analysis") as nested:
                assert nested is outer
                monkeypatch.setattr(limiter, "try_acquire", observe_acquisition)

                async def child():
                    async with limiter.slot("issue_analysis"):
                        entered.set()
                        # Also wake the assertion if child reentry bypasses SQL.
                        child_attempted.set()

                task = asyncio.create_task(child())
                await asyncio.wait_for(child_attempted.wait(), 10)
                assert not entered.is_set()
        await asyncio.wait_for(task, 10)
        assert entered.is_set()
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_expired_owner_cannot_renew_or_release_replacement(capacity_db):
    _, limiter, _, _ = capacity_db
    instant = now_utc()
    limiter.clock = lambda: instant
    old = await limiter.try_acquire("agent")
    instant += timedelta(seconds=3)
    new = await limiter.try_acquire("agent")
    assert new and old.token != new.token
    assert not await limiter.renew(old)
    await limiter.release(old)
    assert await limiter.try_acquire("agent") is None
    await limiter.release(new)


@pytest.mark.asyncio
async def test_heartbeat_keeps_running_task_and_loss_stops_owner(
    capacity_db, monkeypatch
):
    module, limiter, engine, _ = capacity_db
    instant = now_utc()
    limiter.clock = lambda: instant
    limiter.lease_seconds = 2
    limiter.heartbeat_seconds = 0.025
    renewed_twice = asyncio.Event()
    renewals = 0
    real_renew = limiter.renew

    async def advance_and_renew(lease):
        nonlocal instant, renewals
        # Advance the domain clock by less than one TTL per actual SQL renewal.
        # This proves extension past the initial expiry without a CPU/IO race.
        instant += timedelta(seconds=1)
        renewed = await real_renew(lease)
        if renewed:
            renewals += 1
            if renewals >= 2:
                renewed_twice.set()
        return renewed

    monkeypatch.setattr(limiter, "renew", advance_and_renew)
    async with limiter.slot("pr_review") as lease:
        initial_expiry = instant + timedelta(seconds=lease.duration_seconds)
        await asyncio.wait_for(renewed_twice.wait(), 10)
        assert instant >= initial_expiry
        with Session(engine) as session:
            assert (
                session.get(module.ServiceExecutionLease, lease.token).expires_at
                > instant
            )
        assert await limiter.try_acquire("pr_review") is None

    async def database_failure(_lease):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(limiter, "renew", database_failure)
    with pytest.raises(module.ServiceExecutionLeaseLost):
        async with asyncio.timeout(10), limiter.slot("pr_review"):
            await asyncio.Event().wait()
    assert await limiter.try_acquire("pr_review") is not None


@pytest.mark.asyncio
async def test_database_configuration_limits_each_feature_without_billing(capacity_db):
    module, _, engine, _ = capacity_db
    with Session(engine) as session:
        session.add(AppConfig(key_name="max_concurrent_reviews", key_value="2"))
        session.add(AppConfig(key_name="agent_team_max_concurrent", key_value="1"))
        session.commit()
    limiter = module.ServiceExecutionCapacity(
        session_factory=RealSQLFactory(engine),
        lease_seconds=2,
        poll_seconds=0.005,
    )
    leases = [await limiter.try_acquire("pr_review") for _ in range(3)]
    assert leases[0] and leases[1] and leases[2] is None
    agent = await limiter.try_acquire("agent")
    assert agent and await limiter.try_acquire("agent") is None
    for lease in [*leases[:2], agent]:
        await limiter.release(lease)


def capacity_process(path, queue, ready, release):
    module = core()
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"autocommit": False, "timeout": 0.2}
    )

    async def run():
        limiter = module.ServiceExecutionCapacity(
            session_factory=RealSQLFactory(engine),
            max_concurrency=2,
            lease_seconds=2,
            poll_seconds=0.005,
        )
        await asyncio.to_thread(ready.wait, 20)
        async with limiter.slot("pr_review"):
            queue.put(("enter", monotonic()))
            if not await asyncio.to_thread(release.wait, 20):
                raise TimeoutError("Parent did not release the test capacity lease")
            queue.put(("leave", monotonic()))

    try:
        asyncio.run(run())
    except BaseException as exc:
        queue.put(("error", repr(exc)))
    finally:
        engine.dispose()


def test_multiple_processes_share_one_database_capacity(capacity_db):
    _, _, _, path = capacity_db
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    ready = ctx.Barrier(6)
    release = ctx.Event()
    workers = [
        ctx.Process(target=capacity_process, args=(path, queue, ready, release))
        for _ in range(5)
    ]
    events = []
    try:
        for worker in workers:
            worker.start()
        # Spawn/import latency must not determine the observed concurrency.
        # Every worker is ready before acquisition, and both admitted workers
        # retain their SQL leases until the parent has observed them together.
        ready.wait(timeout=20)
        events = [queue.get(timeout=20) for _ in range(2)]
        assert all(kind == "enter" for kind, _ in events)
        with pytest.raises(Empty):
            queue.get(timeout=0.1)
    finally:
        release.set()
        for worker in workers:
            if worker.pid is not None:
                worker.join(20)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(5)
    assert all(worker.exitcode == 0 for worker in workers)
    events.extend(queue.get(timeout=1) for _ in range(8))
    queue.close()
    assert all(kind != "error" for kind, _ in events)
    running = maximum = 0
    for kind, _ in sorted(events, key=lambda event: event[1]):
        running += 1 if kind == "enter" else -1
        maximum = max(maximum, running)
    assert maximum == 2
    assert running == 0


@pytest.mark.asyncio
async def test_running_lease_keeps_its_duration_when_runtime_timing_changes(
    capacity_db,
):
    _, limiter, _, _ = capacity_db
    instant = now_utc()
    limiter.clock = lambda: instant
    lease = await limiter.try_acquire("agent")
    limiter.lease_seconds = 0.12
    assert await limiter.renew(lease)
    instant += timedelta(seconds=0.2)
    assert await limiter.renew(lease)
    await limiter.release(lease)


@pytest.mark.asyncio
async def test_database_failure_is_not_a_local_capacity_success(capacity_db):
    module, limiter, engine, _ = capacity_db
    module.ServiceExecutionGate.__table__.drop(engine)
    with pytest.raises(
        module.ServiceExecutionCapacityError, match="transaction failed"
    ):
        async with limiter.slot("agent"):
            pytest.fail("database failure must prevent execution")


@pytest.mark.asyncio
async def test_capacity_reduction_drains_existing_leases_before_new_admission(
    capacity_db,
):
    module, _, engine, _ = capacity_db
    with Session(engine) as session:
        config = AppConfig(key_name="max_concurrent_reviews", key_value="2")
        session.add(config)
        session.commit()
    limiter = module.ServiceExecutionCapacity(
        session_factory=RealSQLFactory(engine),
        lease_seconds=2,
        poll_seconds=0.005,
    )
    first = await limiter.try_acquire("pr_review")
    second = await limiter.try_acquire("pr_review")
    with Session(engine) as session:
        session.scalar(
            select(AppConfig).where(AppConfig.key_name == "max_concurrent_reviews")
        ).key_value = "1"
        session.commit()
    assert await limiter.try_acquire("pr_review") is None
    await limiter.release(first)
    assert await limiter.try_acquire("pr_review") is None
    await limiter.release(second)
    assert await limiter.try_acquire("pr_review") is not None


@pytest.mark.asyncio
async def test_completion_joins_inflight_renewal_before_releasing_sql_slot(
    capacity_db, monkeypatch
):
    module, limiter, engine, _ = capacity_db
    limiter.heartbeat_seconds = 0.01
    renewal_started = asyncio.Event()
    renewal_proceed = asyncio.Event()
    body_complete = asyncio.Event()
    real_renew = limiter.renew

    async def slow_renew(lease):
        renewal_started.set()
        await renewal_proceed.wait()
        return await real_renew(lease)

    monkeypatch.setattr(limiter, "renew", slow_renew)

    async def execute():
        async with limiter.slot("agent"):
            await body_complete.wait()

    task = asyncio.create_task(execute())
    await asyncio.wait_for(renewal_started.wait(), 1)
    body_complete.set()
    await asyncio.sleep(0.02)
    assert not task.done()
    renewal_proceed.set()
    await asyncio.wait_for(task, 1)
    with Session(engine) as session:
        assert not session.scalars(select(module.ServiceExecutionLease)).all()


@pytest.mark.asyncio
async def test_owner_can_distinguish_lost_lease_from_user_cancellation(
    capacity_db, monkeypatch
):
    module, limiter, _, _ = capacity_db
    limiter.heartbeat_seconds = 0.01

    async def database_failure(_lease):
        raise RuntimeError("lost coordination")

    monkeypatch.setattr(limiter, "renew", database_failure)
    observed = []
    with pytest.raises(module.ServiceExecutionLeaseLost):
        async with limiter.slot("agent"):
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                observed.append(module.current_service_execution_failure())
                child = asyncio.create_task(asyncio.to_thread(lambda: None))
                await child

                async def inherited_child():
                    return module.current_service_execution_failure()

                assert await asyncio.create_task(inherited_child()) is None
                raise
    assert isinstance(observed[0], RuntimeError)
    assert module.current_service_execution_failure() is None
