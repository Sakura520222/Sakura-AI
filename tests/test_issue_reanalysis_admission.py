"""Closed Issues must be rejected before reanalysis work is admitted."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.api.v1 import issues as api_routes
from backend.models.database import IssueAnalysis


@pytest.fixture
def analysis_db(monkeypatch):
    # This fixture exercises Issue state/authorization with its one-table DB.
    # Wallet admission and receipt fencing use the complete real schema in the
    # billing integration tests; this fixture isolates Issue state/authorization.
    monkeypatch.setattr(
        "backend.services.issues.reanalysis_admission.admit_reanalysis_operation",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "backend.services.issues.reanalysis_dispatch.claim_reanalysis_dispatch",
        AsyncMock(),
    )
    engine = create_engine("sqlite:///:memory:")
    IssueAnalysis.__table__.create(engine)
    with Session(engine) as session:
        record = IssueAnalysis(
            issue_number=7,
            repo_owner="owner",
            repo_name="repo",
            author="alice",
            title="Current issue",
            body="Raw body",
            issue_state="open",
            status="completed",
            analysis_version=2,
        )
        session.add(record)
        session.commit()
        statements = []

        class AsyncFacade:
            def add(self, row):
                session.add(row)

            async def flush(self):
                session.flush()

            async def commit(self):
                session.commit()

            async def rollback(self):
                session.rollback()

            async def execute(self, statement):
                statements.append(statement)
                return session.execute(statement)

        yield SimpleNamespace(
            record=record,
            session=session,
            db=AsyncFacade(),
            statements=statements,
        )
    engine.dispose()


async def _reanalyze(db, user):
    user = {"user_id": 42, **user}
    return await api_routes.reanalyze_issue(1, db=db, user=user)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "cancelled", "pending", "analyzing"])
async def test_closed_reanalysis_conflicts_before_version_lookup_or_enqueue(
    monkeypatch, analysis_db, status
):
    analysis_db.record.issue_state = "closed"
    analysis_db.record.status = status
    analysis_db.session.commit()
    submit = AsyncMock(return_value="unexpected-task")
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )

    response = await _reanalyze(analysis_db.db, {"sub": "owner"})

    assert response.status_code == 409
    assert json.loads(response.body) == {
        "success": False,
        "error": "已关闭的 Issue 不支持重新分析，请先在 GitHub 上重新打开",
    }
    assert len(analysis_db.statements) == 1
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_reanalysis_submit_with_next_version(monkeypatch, analysis_db):
    analysis_db.record.issue_state = "open"
    analysis_db.session.commit()
    submit = AsyncMock(side_effect=lambda info: info["task_id"])
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )
    admission = AsyncMock()
    monkeypatch.setattr(
        "backend.services.issues.reanalysis_admission.admit_reanalysis_operation",
        admission,
    )

    response = await _reanalyze(analysis_db.db, {"sub": "alice", "user_id": 42})

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["success"] is True
    task_id = payload["data"]["task_id"]
    assert task_id == submit.await_args.args[0]["task_id"]
    assert len(analysis_db.statements) == 2
    submit.assert_awaited_once()
    admission.assert_awaited_once()
    sent = dict(submit.await_args.args[0])
    context = sent.pop("billing_context")
    assert sent.pop("task_id") == task_id
    pending_id = sent.pop("_reanalysis_analysis_id")
    assert analysis_db.session.get(IssueAnalysis, pending_id).status == "pending"
    assert context["user_id"] == 42 and context["feature"] == "issue_analysis"
    assert context["source"] == {
        "repo_full_name": "owner/repo",
        "issue_number": 7,
        "trigger_user_id": 42,
        "task_id": task_id,
        "issue_analysis_id": pending_id,
    }
    assert context["operation_id"] == admission.await_args.args[1].operation_id
    assert sent == {
        "issue_number": 7,
        "repo_name": "repo",
        "repo_owner": "owner",
        "author": "alice",
        "title": "Current issue",
        "body": "Raw body",
        "state": "open",
        "analysis_version": 3,
        "user_id": 42,
        "billing_user_id": 42,
        "trigger_user_id": 42,
        "repo_full_name": "owner/repo",
    }


@pytest.mark.asyncio
async def test_out_of_scope_closed_issue_still_returns_not_found(
    monkeypatch, analysis_db
):
    analysis_db.record.issue_state = "closed"
    analysis_db.session.commit()
    submit = AsyncMock(return_value="unexpected-task")
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )

    response = await _reanalyze(analysis_db.db, {"sub": "outsider"})

    assert response.status_code == 404
    assert json.loads(response.body) == {
        "success": False,
        "error": "记录不存在或无权访问",
    }
    assert len(analysis_db.statements) == 1
    submit.assert_not_awaited()
