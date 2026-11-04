"""Exercise production bulk writers and current resource scopes against SQL."""
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

from backend.models.agent_team_models import AgentTeamTask
from backend.models.announcement_models import NotificationDelivery
from backend.models.database import IssueAnalysis
from backend.models.identity_models import NotificationEndpoint
from backend.models.notification_models import (
    NotificationEvent,
    NotificationSubscription,
    UserNotification,
)
from backend.models.scan_models import RepoScan
from backend.models.telegram_models import TelegramUser
from backend.services import notification_events
from backend.services.issue_service import IssueService
from backend.services.notification_events import resource_allowed
from backend.workers.issue_worker import IssueWorker
from tests.test_notification_events_646 import AsyncDB, Provider, service
from tests.test_notification_events_646 import event_db as _event_db


@pytest.fixture
def event_db(tmp_path, monkeypatch):
    yield from _event_db.__wrapped__(tmp_path, monkeypatch)


def issue(record_id=10, **overrides):
    values = {"id": record_id, "repo_owner": "alice", "repo_name": "repo", "author": "bob",
        "issue_number": 646, "analysis_version": 1, "title": "Issue", "status": "analyzing", "issue_state": "open"}
    return IssueAnalysis(**(values | overrides))


async def transition(db, action):
    if action == "completed":
        return await IssueService().save_analysis_result(
            {"summary": "Done", "priority": "critical"},
            {"repo_owner": "alice", "repo_name": "repo", "issue_number": 646},
            db, analysis_id=10,
        )
    if action == "closed":
        return await IssueService().mark_issue_closed("alice", "repo", 646, db)
    worker = IssueWorker.__new__(IssueWorker)
    worker._log_activity = AsyncMock()
    method = worker._mark_analysis_failed if action == "failed" else worker._mark_active_analysis_cancelled
    return await method(db, analysis_id=10, reason="Internal failure detail must not be sent")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["completed", "closed", "failed", "cancelled"])
async def test_production_issue_bulk_writers_emit_once_in_the_state_transaction(event_db, action):
    engine, _ = event_db
    with Session(engine) as db:
        db.add(issue())
        db.commit()
    async with AsyncDB(engine, outbox=True) as db:
        await transition(db, action)
        await transition(db, action)  # duplicate callback/close/worker convergence
    with Session(engine) as db:
        event = db.query(NotificationEvent).one()
        expected = "cancelled" if action == "closed" else action
        assert db.get(IssueAnalysis, 10).status == expected
        assert (event.event_type, event.resource_id, event.run_key) == (f"issue.{expected}", "10", "1")
        assert json.loads(event.recipients_json) == [2]
        assert event.detail_url == "/issues/10"
        assert "Internal failure detail" not in event.content
        if action == "completed":
            assert event.severity == "critical"
            assert json.loads(event.payload_json) == {"priority": "critical", "issue_number": 646}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["completed", "closed", "failed", "cancelled"])
async def test_outbox_failure_rolls_back_bulk_issue_state(event_db, monkeypatch, action):
    engine, _ = event_db
    with Session(engine) as db:
        db.add(issue())
        db.commit()
    original_emit = notification_events.emit_event

    async def fail_after_insert(*args, **kwargs):
        await original_emit(*args, **kwargs)
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(notification_events, "emit_event", fail_after_insert)
    async with AsyncDB(engine, outbox=True) as db:
        with pytest.raises(RuntimeError, match="outbox unavailable"):
            await transition(db, action)
        # A worker may catch the error and commit subsequent cleanup. The
        # failed transition and even an already-inserted event must be gone.
        await db.commit()
    with Session(engine) as db:
        record = db.get(IssueAnalysis, 10)
        assert (record.status, record.issue_state) == ("analyzing", "open")
        assert db.query(NotificationEvent).count() == 0


@pytest.mark.asyncio
async def test_issue_close_cancels_only_active_owner_scoped_versions(event_db):
    engine, _ = event_db
    with Session(engine) as db:
        db.add_all([issue(), issue(11, repo_name="alice/repo", analysis_version=2),
            issue(12, status="completed", analysis_version=3), issue(13, repo_owner="bob")])
        db.commit()
    async with AsyncDB(engine, outbox=True) as db:
        result = await IssueService().mark_issue_closed("alice", "repo", 646, db)
        assert result == {"cancelled": 2, "state_updated": 3}
        assert await transition(db, "completed") is None  # stale result cannot emit completion
        await transition(db, "failed")
    with Session(engine) as db:
        assert {(row.resource_id, row.run_key, row.event_type) for row in db.query(NotificationEvent)} == {
            ("10", "1", "issue.cancelled"), ("11", "2", "issue.cancelled"),
        }
        assert db.get(IssueAnalysis, 12).status == "completed"
        assert (db.get(IssueAnalysis, 13).status, db.get(IssueAnalysis, 13).issue_state) == ("analyzing", "open")


@pytest.mark.asyncio
async def test_issue_close_emits_for_every_row_changed_by_the_update(event_db):
    engine, _ = event_db
    with Session(engine) as db:
        db.add(issue())
        db.commit()

    class InsertBeforeUpdate(AsyncDB):
        inserted = False

        async def execute(self, statement, *args, **kwargs):
            if isinstance(statement, Update) and statement.table.name == "issue_analyses" and not self.inserted:
                self.inserted = True
                # A new version can become visible between a prior SELECT
                # snapshot and the bulk write. Test actual SQL-returned rows.
                self.session.add(issue(11, analysis_version=2))
                self.session.flush()
            return await super().execute(statement, *args, **kwargs)

    async with InsertBeforeUpdate(engine, outbox=True) as db:
        result = await IssueService().mark_issue_closed("alice", "repo", 646, db)
        assert result["cancelled"] == 2
    with Session(engine) as db:
        assert {row.resource_id for row in db.query(NotificationEvent).filter_by(event_type="issue.cancelled")} == {"10", "11"}
        assert {row.status for row in db.query(IssueAnalysis)} == {"cancelled"}


@pytest.mark.asyncio
async def test_bulk_issue_new_analysis_round_has_an_independent_event(event_db):
    engine, _ = event_db
    with Session(engine) as db:
        db.add_all([issue(), issue(11, analysis_version=2)])
        db.commit()
    async with AsyncDB(engine, outbox=True) as db:
        for record_id in (10, 11):
            await IssueService().save_analysis_result({"summary": "Done"},
                {"repo_owner": "alice", "repo_name": "repo", "issue_number": 646}, db, analysis_id=record_id)
    with Session(engine) as db:
        assert {(row.resource_id, row.run_key) for row in db.query(NotificationEvent)} == {("10", "1"), ("11", "2")}


@pytest.mark.asyncio
@pytest.mark.parametrize("repository", ["Acme/repo", "alice/repo"])
async def test_agent_initiator_receives_events_but_subscribers_and_repo_owners_do_not_gain_access(event_db, repository):
    engine, _ = event_db
    with Session(engine, info={"notification_outbox": True}) as db:
        db.add(NotificationEndpoint(id=3, user_id=2, provider="email", address="bob@example.org", enabled=True, verified=True))
        db.add_all([NotificationSubscription(user_id=2, event_type="agent.*"),
            NotificationSubscription(user_id=2, event_type="agent.*", endpoint_id=3),
            NotificationSubscription(user_id=1, event_type="agent.*", endpoint_id=1)])
        task = AgentTeamTask(id=10, repo_full_name=repository, repo_owner=repository.split("/")[0], repo_name="repo",
            source_type="manual_issue", title="Agent task", status="cloning", started_by="bob")
        db.add(task)
        db.commit()
        task.status = "waiting_human"
        db.commit()
        task.resume_count = 1
        task.status = "cloning"
        db.commit()
        task.status = "completed"
        db.commit()
        events = db.query(NotificationEvent).all()
        assert {(row.event_type, row.run_key) for row in events} == {
            ("agent.started", "1:0:0"), ("agent.action_required", "1:0:0"),
            ("agent.started", "1:1:0"), ("agent.completed", "1:1:0"),
        }
        assert all(json.loads(row.recipients_json) == [2] for row in events)
        assert all(row.detail_url == "/agent-team/?task_id=10" for row in events)
        event_id = next(row.id for row in events if row.event_type == "agent.completed")
    provider = Provider()
    dispatcher = service(provider)
    async with AsyncDB(engine) as db:
        event = await db.get(NotificationEvent, event_id)
        assert await resource_allowed(db, await db.get(TelegramUser, 2), event) is True
        assert await resource_allowed(db, await db.get(TelegramUser, 1), event) is False
        await dispatcher.materialize_event(db, event_id)
        rows = (await db.execute(select(NotificationDelivery))).scalars().all()
        assert {row.user_id for row in rows} == {2}
        assert {row.user_id for row in (await db.execute(select(UserNotification))).scalars()} == {2}
        external_id = next(row.id for row in rows if row.endpoint_id == 3)
    with Session(engine) as db:
        db.get(AgentTeamTask, 10).started_by = "alice"  # authorization changed before send
        db.commit()
    async with AsyncDB(engine) as db:
        assert await dispatcher.deliver_event_row(db, external_id) is True
    assert provider.sent == []
    with Session(engine) as db:
        assert db.get(NotificationDelivery, external_id).status == "cancelled"


@pytest.mark.asyncio
async def test_scan_fanout_and_retry_require_current_admin_role(event_db):
    engine, _ = event_db
    with Session(engine, info={"notification_outbox": True}) as db:
        db.get(TelegramUser, 2).role = "admin"
        db.add(NotificationEndpoint(id=3, user_id=2, provider="email", address="bob@example.org", enabled=True, verified=True))
        db.add_all([NotificationSubscription(user_id=user_id, event_type="scan.*", endpoint_id=endpoint_id)
            for user_id, endpoint_id in [(1, None), (1, 1), (2, None), (2, 3)]])
        db.add(RepoScan(id=10, repo_owner="alice", repo_name="repo", status="completed", trigger_type="manual"))
        db.commit()
        event_id = db.query(NotificationEvent).filter_by(event_type="scan.completed").one().id
    provider = Provider()
    dispatcher = service(provider)
    async with AsyncDB(engine) as db:
        await dispatcher.materialize_event(db, event_id)
        rows = (await db.execute(select(NotificationDelivery).where(NotificationDelivery.event_id == event_id))).scalars().all()
        assert {row.user_id for row in rows} == {2}
        assert {row.user_id for row in (await db.execute(select(UserNotification).where(UserNotification.event_id == event_id))).scalars()} == {2}
        external_id = next(row.id for row in rows if row.endpoint_id == 3)
    with Session(engine) as db:
        db.get(TelegramUser, 2).role = "user"
        db.commit()
    async with AsyncDB(engine) as db:
        assert await dispatcher.deliver_event_row(db, external_id) is True
        assert await resource_allowed(db, await db.get(TelegramUser, 2), await db.get(NotificationEvent, event_id)) is False
    assert provider.sent == []
    with Session(engine) as db:
        row = db.get(NotificationDelivery, external_id)
        assert (row.status, row.error_category) == ("cancelled", "ineligible")
