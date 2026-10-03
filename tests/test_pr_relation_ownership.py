"""PR ownership lifetime and handoff regressions on a long-lived event loop."""

import asyncio
import gc
import threading
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.core import config
from backend.models.database import PRIssueLink
from backend.services.issues import pr_link_sync
from backend.services.issues.pr_budget import PRInputBudget
from backend.services.issues.pr_verifier import PRVerificationResult
from tests.test_pr_issue_relations import PR, linker


@pytest.mark.asyncio
async def test_completed_pr_keys_do_not_accumulate_on_a_live_loop():
    loop = asyncio.get_running_loop()
    entry_refs = []
    for number in range(1000):
        async with pr_link_sync.pr_relation_ownership("owner/repo", number):
            entry_refs.append(
                weakref.ref(pr_link_sync._LOCKS[loop][("owner/repo", number)])
            )

    gc.collect()
    assert not loop.is_closed()
    assert not pr_link_sync._LOCKS[loop]
    assert all(reference() is None for reference in entry_refs)


@pytest.mark.asyncio
async def test_failed_holder_reclaims_its_key_while_error_is_retained():
    loop = asyncio.get_running_loop()
    with pytest.raises(RuntimeError) as caught:
        async with pr_link_sync.pr_relation_ownership("owner/repo", 1):
            raise RuntimeError("publication failed")

    assert str(caught.value) == "publication failed"
    assert not pr_link_sync._LOCKS[loop]


async def contend(repo, number, queued, entered, release):
    # No suspension occurs between announcing the attempt and entering acquire.
    queued.set()
    async with pr_link_sync.pr_relation_ownership(repo, number):
        entered.set()
        await release.wait()


@pytest.mark.asyncio
async def test_cancelled_waiter_preserves_owner_and_next_waiter_then_reclaims_key():
    loop = asyncio.get_running_loop()
    cancelled_queued, cancelled_entered = asyncio.Event(), asyncio.Event()
    next_queued, next_entered = asyncio.Event(), asyncio.Event()
    release = asyncio.Event()
    async with asyncio.timeout(5), asyncio.TaskGroup() as group:
        async with pr_link_sync.pr_relation_ownership("Owner/Repo", 1):
            entry_ref = weakref.ref(pr_link_sync._LOCKS[loop][("owner/repo", 1)])
            cancelled = group.create_task(
                contend("OWNER/REPO", 1, cancelled_queued, cancelled_entered, release)
            )
            await cancelled_queued.wait()
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError) as caught:
                await cancelled

            next_waiter = group.create_task(
                contend("owner/repo", 1, next_queued, next_entered, release)
            )
            await next_queued.wait()
            gc.collect()
            assert not cancelled_entered.is_set()
            assert not next_entered.is_set()
            assert pr_link_sync._LOCKS[loop][("owner/repo", 1)] is entry_ref()

        await next_entered.wait()
        assert pr_link_sync._LOCKS[loop][("owner/repo", 1)] is entry_ref()
        release.set()
        await next_waiter

    assert isinstance(caught.value, asyncio.CancelledError)
    assert not pr_link_sync._LOCKS[loop]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_new_acquirer_cannot_split_ownership_during_handoff(cancel_waiter):
    loop = asyncio.get_running_loop()
    first_queued, first_entered, release_first = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    racer_ready, start_race = asyncio.Event(), asyncio.Event()
    racer_queued, racer_entered, release_racer = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def race():
        racer_ready.set()
        await start_race.wait()
        await contend("Owner/Repo", 1, racer_queued, racer_entered, release_racer)

    async with asyncio.timeout(5), asyncio.TaskGroup() as group:
        async with pr_link_sync.pr_relation_ownership("owner/repo", 1):
            entry_ref = weakref.ref(pr_link_sync._LOCKS[loop][("owner/repo", 1)])
            first = group.create_task(
                contend("OWNER/REPO", 1, first_queued, first_entered, release_first)
            )
            await first_queued.wait()
            racer = group.create_task(race())
            await racer_ready.wait()
            # Schedule the newcomer before release schedules the old waiter.
            # Neither can run until this holder exits its context below.
            start_race.set()

        if cancel_waiter:
            # Cancel after release wakes the first waiter but before it resumes.
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not first_entered.is_set()
        else:
            await first_entered.wait()
            assert racer_queued.is_set()
            assert not racer_entered.is_set()
            assert pr_link_sync._LOCKS[loop][("owner/repo", 1)] is entry_ref()
            release_first.set()
            await first

        await racer_entered.wait()
        assert pr_link_sync._LOCKS[loop][("owner/repo", 1)] is entry_ref()
        release_racer.set()
        await racer

    assert not pr_link_sync._LOCKS[loop]


@pytest.mark.asyncio
@pytest.mark.parametrize("repo,number", [("owner/other", 1), ("owner/repo", 2)])
async def test_distinct_pr_keys_remain_concurrent(repo, number):
    loop = asyncio.get_running_loop()
    queued, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async with asyncio.timeout(5), asyncio.TaskGroup() as group:
        async with pr_link_sync.pr_relation_ownership("owner/repo", 1):
            independent = group.create_task(
                contend(repo, number, queued, entered, release)
            )
            await entered.wait()
            assert len(pr_link_sync._LOCKS[loop]) == 2
            release.set()
            await independent
            assert set(pr_link_sync._LOCKS[loop]) == {("owner/repo", 1)}

    assert not pr_link_sync._LOCKS[loop]


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_fails", [False, True])
async def test_cancelled_sync_reclaims_key_only_after_publication_drains(
    monkeypatch, edit_fails
):
    loop = asyncio.get_running_loop()
    edit_started, release_edit = asyncio.Event(), threading.Event()
    next_queued, next_entered, release_next = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    effects = []
    engine = create_engine("sqlite:///:memory:")
    PRIssueLink.__table__.create(engine)
    session = Session(engine)
    session.add(
        PRIssueLink(repo_name="o/r", pr_id=618, issue_number=570, link_type="semantic")
    )
    session.commit()

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, statement):
            return session.execute(statement)

        async def delete(self, row):
            session.delete(row)

        async def flush(self):
            session.flush()

        async def commit(self):
            session.commit()
            effects.append("commit")

        async def rollback(self):
            session.rollback()
            effects.append("rollback")

    pr = PR(
        "Human\n<!-- sakura-ai-issue-links-start -->Closes #570<!-- sakura-ai-issue-links-end -->"
    )
    old_body, original_edit = pr.body, pr.edit

    def blocked_edit(**kwargs):
        loop.call_soon_threadsafe(edit_started.set)
        assert release_edit.wait(5)
        if edit_fails:
            raise RuntimeError("GitHub edit failed")
        original_edit(**kwargs)
        effects.append("edit")

    pr.edit = blocked_edit
    monkeypatch.setattr(
        config,
        "get_dynamic_config",
        AsyncMock(
            side_effect=lambda key, **_: {
                "semantic_issue_max_links": 5,
                "semantic_issue_similarity_threshold": 0.5,
            }[key]
        ),
    )
    service = pr_link_sync.PRRelationSyncService(
        retriever=SimpleNamespace(retrieve=AsyncMock(return_value=[])),
        verifier=SimpleNamespace(
            resolve_budget=AsyncMock(return_value=PRInputBudget(128, 64000)),
            verify=AsyncMock(return_value=PRVerificationResult(True)),
        ),
        session_factory=DB,
        linker=linker(),
    )

    try:
        async with asyncio.timeout(5), asyncio.TaskGroup() as group:
            sync = group.create_task(
                service.synchronize(
                    SimpleNamespace(get_pull=lambda _: pr), "o", "r", 618
                )
            )
            try:
                await edit_started.wait()
                entry_ref = weakref.ref(pr_link_sync._LOCKS[loop][("o/r", 618)])
                sync.cancel()
                following = group.create_task(
                    contend("O/R", 618, next_queued, next_entered, release_next)
                )
                await next_queued.wait()
                sync.cancel()  # Repeated cancellation still must drain publication.
                cancellation_processed = asyncio.Event()
                loop.call_soon(cancellation_processed.set)
                await cancellation_processed.wait()
                gc.collect()
                assert not sync.done()
                assert not next_entered.is_set()
                assert effects == []
                assert pr_link_sync._LOCKS[loop][("o/r", 618)] is entry_ref()
            finally:
                release_edit.set()

            with pytest.raises(asyncio.CancelledError) as caught:
                await sync
            await next_entered.wait()
            if edit_fails:
                assert isinstance(caught.value.__cause__, RuntimeError)
                assert effects == ["rollback"]
                assert pr.body == old_body
                assert session.scalar(select(PRIssueLink.issue_number)) == 570
            else:
                assert effects == ["edit", "commit"]
                assert pr.body == "Human"
                assert session.scalar(select(PRIssueLink.issue_number)) is None
            assert pr_link_sync._LOCKS[loop][("o/r", 618)] is entry_ref()
            release_next.set()
            await following

        assert not pr_link_sync._LOCKS[loop]
    finally:
        release_edit.set()
        session.close()
        engine.dispose()
