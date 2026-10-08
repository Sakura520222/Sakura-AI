"""Durable worker liveness and cancellation windows use actual SQL state."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.time_service import now_utc
from backend.models.database import AppConfig
from backend.services.billing_context import BillingContext, bind_billing_context
from tests.test_service_execution_capacity import RealSQLFactory
from tests.test_service_execution_capacity import capacity_db as capacity_db_fixture

capacity_db = capacity_db_fixture


@pytest.mark.asyncio
async def test_repeated_cancellation_during_heartbeat_join_still_releases(
    capacity_db, monkeypatch
):
    module, limiter, engine, _ = capacity_db
    limiter.heartbeat_seconds = 0.01
    started = asyncio.Event()
    proceed = asyncio.Event()
    body_done = asyncio.Event()
    real_renew = limiter.renew

    async def delayed_renew(lease):
        started.set()
        await proceed.wait()
        return await real_renew(lease)

    monkeypatch.setattr(limiter, "renew", delayed_renew)

    async def work():
        async with limiter.slot("agent"):
            await body_done.wait()

    task = asyncio.create_task(work())
    await asyncio.wait_for(started.wait(), 1)
    body_done.set()
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    with Session(engine) as session:
        assert not session.scalars(select(module.ServiceExecutionLease)).all()


@pytest.mark.asyncio
async def test_new_work_reads_persisted_timing_without_settings_cache(capacity_db):
    module, _, engine, _ = capacity_db
    defaults = SimpleNamespace(
        service_execution_lease_seconds=300, service_execution_poll_seconds=0.25
    )
    limiter = module.ServiceExecutionCapacity(
        session_factory=RealSQLFactory(engine),
        settings_provider=lambda: defaults,
        max_concurrency=2,
    )
    with Session(engine) as session:
        session.add(
            AppConfig(key_name="service_execution_lease_seconds", key_value="90")
        )
        session.add(
            AppConfig(key_name="service_execution_poll_seconds", key_value="0.05")
        )
        session.commit()
    first = await limiter.try_acquire("agent")
    assert first.duration_seconds == 90
    assert (await limiter._fresh_timings())[1] == 0.05
    with Session(engine) as session:
        session.scalar(
            select(AppConfig).where(
                AppConfig.key_name == "service_execution_lease_seconds"
            )
        ).key_value = "60"
        session.scalar(
            select(AppConfig).where(
                AppConfig.key_name == "service_execution_poll_seconds"
            )
        ).key_value = "0.1"
        session.commit()
    second = await limiter.try_acquire("agent")
    assert second.duration_seconds == 60 and first.duration_seconds == 90
    assert (await limiter._fresh_timings())[1] == 0.1
    await limiter.release(first)
    await limiter.release(second)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,value",
    [
        ("service_execution_lease_seconds", "nan"),
        ("service_execution_poll_seconds", "inf"),
        ("service_execution_lease_seconds", "0"),
        ("service_execution_poll_seconds", "-1"),
    ],
)
async def test_invalid_saved_timing_fails_closed(capacity_db, key, value):
    module, _, engine, _ = capacity_db
    with Session(engine) as session:
        session.add(AppConfig(key_name=key, key_value=value))
        session.commit()
    limiter = module.ServiceExecutionCapacity(
        session_factory=RealSQLFactory(engine), max_concurrency=1
    )
    with pytest.raises(module.ServiceExecutionCapacityError):
        await limiter.try_acquire("agent")


@pytest.mark.asyncio
async def test_queued_billing_execution_has_renewable_ownership_without_slot(
    capacity_db,
):
    module, limiter, engine, _ = capacity_db
    limiter.lease_seconds = 0.12
    limiter.heartbeat_seconds = 0.02
    blocker = await limiter.try_acquire("agent")
    # Keep only the blocker slot's TTL long enough to observe an ownership renewal.
    with Session(engine) as session:
        from datetime import timedelta

        session.get(module.ServiceExecutionLease, blocker.token).expires_at = (
            now_utc() + timedelta(seconds=2)
        )
        session.commit()
    context = BillingContext(None, str(uuid4()), "agent", {}, "isolated_liveness_test")

    async def waiting():
        with bind_billing_context(context):
            async with limiter.slot("agent"):
                pytest.fail("queued operation must not consume an execution slot")

    task = asyncio.create_task(waiting())
    try:
        await asyncio.sleep(0.18)
        with Session(engine) as session:
            row = session.scalar(
                select(module.ServiceExecutionOwnership).where(
                    module.ServiceExecutionOwnership.operation_id
                    == context.operation_id
                )
            )
            assert row.state == "queued" and row.expires_at > now_utc()
            assert len(session.scalars(select(module.ServiceExecutionLease)).all()) == 1
        async with RealSQLFactory(engine)() as session:
            assert await module.has_live_service_execution_ownership(
                session, context.operation_id
            )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await limiter.release(blocker)
    with Session(engine) as session:
        assert (
            session.scalar(
                select(module.ServiceExecutionOwnership).where(
                    module.ServiceExecutionOwnership.operation_id
                    == context.operation_id
                )
            ).state
            == "released"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["try_acquire", "acquire_ownership"])
async def test_cancel_after_sql_commit_keeps_resource_for_release(
    capacity_db, monkeypatch, method
):
    module, limiter, engine, _ = capacity_db
    committed = asyncio.Event()
    proceed = asyncio.Event()
    original = getattr(limiter, method)

    async def delayed_result(feature):
        resource = await original(feature)
        committed.set()
        await proceed.wait()
        return resource

    monkeypatch.setattr(limiter, method, delayed_result)
    context = BillingContext(None, str(uuid4()), "agent", {}, "isolated_commit_test")

    async def work():
        with bind_billing_context(context):
            async with limiter.slot("agent"):
                pytest.fail("cancelled acquisition must never dispatch work")

    task = asyncio.create_task(work())
    await asyncio.wait_for(committed.wait(), 1)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    with Session(engine) as session:
        assert not session.scalars(select(module.ServiceExecutionLease)).all()
        ownership = session.scalar(
            select(module.ServiceExecutionOwnership).where(
                module.ServiceExecutionOwnership.operation_id == context.operation_id
            )
        )
        assert ownership.state == "released"


@pytest.mark.asyncio
async def test_expired_and_released_owner_never_revives_or_releases_other_token(
    capacity_db,
):
    from datetime import timedelta

    module, limiter, engine, _ = capacity_db
    instant = now_utc()
    limiter.clock = lambda: instant
    context = BillingContext(None, str(uuid4()), "agent", {}, "isolated_fencing_test")
    with bind_billing_context(context):
        old = await limiter.acquire_ownership("agent")
        instant += timedelta(seconds=3)
        assert not await limiter.renew_ownership(old)
        replacement = await limiter.acquire_ownership("agent")
        assert replacement.token != old.token
        await limiter.release_ownership(old)
        assert await limiter.renew_ownership(replacement, running=True)
        async with RealSQLFactory(engine)() as session:
            assert await module.has_live_service_execution_ownership(
                session, context.operation_id, instant=instant
            )
        await limiter.release_ownership(replacement)
        assert not await limiter.renew_ownership(replacement)
        async with RealSQLFactory(engine)() as session:
            assert not await module.has_live_service_execution_ownership(
                session, context.operation_id, instant=instant
            )


@pytest.mark.asyncio
async def test_ownership_heartbeat_loss_cancels_queued_owner_before_expiry(
    capacity_db, monkeypatch
):
    module, limiter, engine, _ = capacity_db
    limiter.heartbeat_seconds = 0.01
    blocker = await limiter.try_acquire("agent")
    context = BillingContext(None, str(uuid4()), "agent", {}, "isolated_loss_test")
    observed = []

    async def failed_renew(ownership, *, running=False):
        return False

    monkeypatch.setattr(limiter, "renew_ownership", failed_renew)
    with bind_billing_context(context):
        with pytest.raises(module.ServiceExecutionLeaseLost):
            try:
                async with limiter.slot("agent"):
                    pytest.fail("queued work must not dispatch after ownership loss")
            except module.ServiceExecutionLeaseLost as error:
                observed.append(error)
                raise
    assert len(observed) == 1
    with Session(engine) as session:
        owner = session.scalar(
            select(module.ServiceExecutionOwnership).where(
                module.ServiceExecutionOwnership.operation_id == context.operation_id
            )
        )
        assert owner.state == "released"
        assert len(session.scalars(select(module.ServiceExecutionLease)).all()) == 1
    await limiter.release(blocker)


@pytest.mark.asyncio
async def test_long_queued_owner_and_short_new_slot_keep_independent_heartbeats(
    capacity_db,
):
    module, limiter, engine, _ = capacity_db
    limiter.lease_seconds = 0.3
    limiter.heartbeat_seconds = None
    blocker = await limiter.try_acquire("agent")
    context = BillingContext(
        None, str(uuid4()), "agent", {}, "isolated_timing_change_test"
    )
    entered = asyncio.Event()
    complete = asyncio.Event()

    async def work():
        with bind_billing_context(context):
            async with limiter.slot("agent") as lease:
                assert lease.duration_seconds == 0.06
                entered.set()
                await complete.wait()

    task = asyncio.create_task(work())
    try:
        await asyncio.sleep(0.03)
        limiter.lease_seconds = 0.06
        await limiter.release(blocker)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0.2)
        assert not task.done()
        async with RealSQLFactory(engine)() as session:
            assert await module.has_live_service_execution_ownership(
                session, context.operation_id
            )
        complete.set()
        await asyncio.wait_for(task, 1)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await limiter.release(blocker)
