"""Focused lifecycle tests for Issue webhook cancellation (Issue #536)."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _issue_payload(action: str) -> dict:
    return {
        "action": action,
        "issue": {
            "number": 536,
            "title": "Lifecycle test",
            "body": "Issue body",
            "state": "closed" if action == "closed" else "open",
            "user": {"login": "contributor"},
        },
        "repository": {
            "name": "sakura-ai-test",
            "full_name": "owner/sakura-ai-test",
            "owner": {"login": "owner"},
        },
    }


class _SessionContext:
    def __init__(self, session=None):
        self.session = session or object()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.fixture
def webhook_dependencies(monkeypatch):
    """Install side-effect-free worker/service/session dependencies."""
    from backend.api import webhook
    from backend.services.issue_service import issue_service
    from backend.workers import issue_worker

    events = []

    async def cancel_task(key):
        events.append(("cancel", key))
        return True

    worker = SimpleNamespace(cancel_task=cancel_task)
    monkeypatch.setattr(issue_worker, "get_issue_worker", lambda: worker)
    monkeypatch.setattr(
        webhook,
        "settings",
        SimpleNamespace(bot_username=None, enable_semantic_issue_linking=False),
    )
    monkeypatch.setattr(webhook, "get_async_session", lambda: _SessionContext())
    return webhook, issue_service, events


@pytest.mark.asyncio
async def test_closed_cancels_before_persisting_lifecycle_state(
    monkeypatch, webhook_dependencies
):
    webhook, issue_service, events = webhook_dependencies

    async def mark_closed(owner, repo, number, db):
        events.append(("close", owner, repo, number))
        return {"cancelled": 1, "state_updated": 1}

    monkeypatch.setattr(issue_service, "mark_issue_closed", mark_closed)

    response = await webhook.handle_issue_event(_issue_payload("closed"))

    assert response.status_code == 200
    assert json.loads(response.body.decode())["action"] == "closed"
    assert events == [
        ("cancel", "owner/sakura-ai-test#536"),
        ("close", "owner", "sakura-ai-test", 536),
    ]


@pytest.mark.asyncio
async def test_deleted_cancels_before_idempotent_cleanup_and_repeats_safely(
    monkeypatch, webhook_dependencies
):
    webhook, issue_service, events = webhook_dependencies

    from backend.workers import issue_worker

    async def cancel_task(key):
        events.append(("cancel", key))
        return False

    monkeypatch.setattr(
        issue_worker,
        "get_issue_worker",
        lambda: SimpleNamespace(cancel_task=cancel_task),
    )

    async def delete_issue(owner, repo, number, db):
        events.append(("delete", owner, repo, number))
        return {"analysis_deleted": 1}

    monkeypatch.setattr(issue_service, "delete_issue_data", delete_issue)

    for _ in range(2):
        response = await webhook.handle_issue_event(_issue_payload("deleted"))
        assert response.status_code == 200

    assert events == [
        ("cancel", "owner/sakura-ai-test#536"),
        ("delete", "owner", "sakura-ai-test", 536),
        ("cancel", "owner/sakura-ai-test#536"),
        ("delete", "owner", "sakura-ai-test", 536),
    ]


@pytest.mark.asyncio
async def test_deleted_waits_for_cancellation_to_converge_before_cleanup(
    monkeypatch, webhook_dependencies
):
    webhook, issue_service, events = webhook_dependencies

    from backend.workers import issue_worker

    cancellation_started = asyncio.Event()
    cancellation_release = asyncio.Event()

    async def cancel_task(key):
        events.append(("cancel-start", key))
        cancellation_started.set()
        await cancellation_release.wait()
        events.append(("cancel-done", key))
        return True

    monkeypatch.setattr(
        issue_worker,
        "get_issue_worker",
        lambda: SimpleNamespace(cancel_task=cancel_task),
    )

    async def delete_issue(owner, repo, number, db):
        events.append(("delete", owner, repo, number))
        return {"analysis_deleted": 1}

    monkeypatch.setattr(issue_service, "delete_issue_data", delete_issue)

    request = asyncio.create_task(webhook.handle_issue_event(_issue_payload("deleted")))
    await asyncio.wait_for(cancellation_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not any(event[0] == "delete" for event in events)

    cancellation_release.set()
    response = await asyncio.wait_for(request, timeout=1)

    assert response.status_code == 200
    assert events == [
        ("cancel-start", "owner/sakura-ai-test#536"),
        ("cancel-done", "owner/sakura-ai-test#536"),
        ("delete", "owner", "sakura-ai-test", 536),
    ]


@pytest.mark.asyncio
async def test_missing_worker_is_idempotent_before_deleted_cleanup(
    monkeypatch, webhook_dependencies
):
    webhook, issue_service, events = webhook_dependencies

    from backend.workers import issue_worker

    monkeypatch.setattr(issue_worker, "get_issue_worker", lambda: None)

    async def delete_issue(owner, repo, number, db):
        events.append(("delete", owner, repo, number))
        return {"analysis_deleted": 0}

    monkeypatch.setattr(issue_service, "delete_issue_data", delete_issue)

    response = await webhook.handle_issue_event(_issue_payload("deleted"))

    assert response.status_code == 200
    assert events == [("delete", "owner", "sakura-ai-test", 536)]


@pytest.mark.asyncio
async def test_cancel_failure_preserves_webhook_retry_and_skips_deleted_cleanup(
    monkeypatch, webhook_dependencies
):
    webhook, issue_service, events = webhook_dependencies

    from backend.workers import issue_worker

    async def cancel_task(key):
        events.append(("cancel", key))
        raise RuntimeError("worker cancellation failed")

    monkeypatch.setattr(
        issue_worker,
        "get_issue_worker",
        lambda: SimpleNamespace(cancel_task=cancel_task),
    )

    async def delete_issue(owner, repo, number, db):
        pytest.fail("cleanup must not run after cancellation failure")

    monkeypatch.setattr(issue_service, "delete_issue_data", delete_issue)

    response = await webhook.handle_issue_event(_issue_payload("deleted"))

    assert response.status_code == 500
    assert json.loads(response.body.decode()) == {
        "status": "error",
        "message": "内部服务错误",
    }
    assert events == [("cancel", "owner/sakura-ai-test#536")]


@pytest.mark.asyncio
async def test_cancel_failure_propagates_from_lifecycle_helper(
    monkeypatch, webhook_dependencies
):
    webhook, _issue_service, events = webhook_dependencies

    from backend.workers import issue_worker

    async def cancel_task(key):
        events.append(("cancel", key))
        raise RuntimeError("worker cancellation failed")

    monkeypatch.setattr(
        issue_worker,
        "get_issue_worker",
        lambda: SimpleNamespace(cancel_task=cancel_task),
    )

    with pytest.raises(RuntimeError, match="worker cancellation failed"):
        await webhook._cancel_issue_analysis_task(
            {
                "repo_owner": "owner",
                "repo_name": "sakura-ai-test",
                "issue_number": 536,
            }
        )

    assert events == [("cancel", "owner/sakura-ai-test#536")]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["closed", "deleted"])
async def test_pull_request_issue_lifecycle_is_ignored(
    monkeypatch, webhook_dependencies, action
):
    webhook, issue_service, events = webhook_dependencies
    payload = _issue_payload(action)
    payload["issue"]["pull_request"] = {
        "url": "https://api.github.com/repos/owner/sakura-ai-test/pulls/536"
    }

    async def unexpected_close(*args):
        pytest.fail("PR issues webhook must not mark an IssueAnalysis closed")

    async def unexpected_delete(*args):
        pytest.fail("PR issues webhook must not delete IssueAnalysis")

    monkeypatch.setattr(issue_service, "mark_issue_closed", unexpected_close)
    monkeypatch.setattr(issue_service, "delete_issue_data", unexpected_delete)

    response = await webhook.handle_issue_event(payload)

    assert response.status_code == 200
    assert json.loads(response.body.decode()) == {
        "status": "ignored",
        "action": action,
        "reason": "pull request issue event",
    }
    assert events == []


@pytest.mark.asyncio
async def test_close_transition_keeps_completed_and_cancels_active_rows():
    from backend.services.issue_service import IssueService

    active = SimpleNamespace(status="analyzing", issue_state="open")
    completed = SimpleNamespace(status="completed", issue_state="open")

    class LifecycleSession:
        def __init__(self):
            self.calls = 0

        async def execute(self, statement):
            self.calls += 1
            if self.calls == 1:
                active.status = "cancelled"
                active.issue_state = "closed"
                return SimpleNamespace(rowcount=1)
            completed.issue_state = "closed"
            return SimpleNamespace(rowcount=2)

        async def commit(self):
            return None

    result = await IssueService().mark_issue_closed(
        "owner", "sakura-ai-test", 536, LifecycleSession()
    )

    assert result == {"cancelled": 1, "state_updated": 2}
    assert active.status == "cancelled"
    assert completed.status == "completed"
    assert active.issue_state == completed.issue_state == "closed"


@pytest.mark.asyncio
async def test_reopen_transition_restores_owner_scoped_short_and_full_rows():
    from sqlalchemy.dialects import sqlite

    from backend.services.issue_service import IssueService

    owner_short = SimpleNamespace(
        repo_owner="owner",
        repo_name="sakura-ai-test",
        issue_state="closed",
        status="cancelled",
    )
    owner_full = SimpleNamespace(
        repo_owner="owner",
        repo_name="owner/sakura-ai-test",
        issue_state="closed",
        status="completed",
    )
    other_owner = SimpleNamespace(
        repo_owner="other-owner",
        repo_name="sakura-ai-test",
        issue_state="closed",
        status="cancelled",
    )

    class LifecycleSession:
        def __init__(self):
            self.statement = None
            self.commits = 0

        async def execute(self, statement):
            self.statement = statement
            for record in (owner_short, owner_full, other_owner):
                if record.repo_owner == "owner" and record.repo_name in {
                    "sakura-ai-test",
                    "owner/sakura-ai-test",
                }:
                    record.issue_state = "open"
            return SimpleNamespace(rowcount=2)

        async def commit(self):
            self.commits += 1

    session = LifecycleSession()
    result = await IssueService().mark_issue_reopened(
        "owner", "sakura-ai-test", 536, session
    )

    compiled = str(
        session.statement.compile(
            dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "repo_owner = 'owner'" in compiled
    assert "'sakura-ai-test'" in compiled
    assert "'owner/sakura-ai-test'" in compiled
    assert result == {"state_updated": 2}
    assert owner_short.issue_state == owner_full.issue_state == "open"
    assert other_owner.issue_state == "closed"
    assert owner_short.status == "cancelled"
    assert owner_full.status == "completed"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_reopen_transition_is_idempotent_when_no_records_match():
    from backend.services.issue_service import IssueService

    class EmptySession:
        def __init__(self):
            self.commits = 0

        async def execute(self, _statement):
            return SimpleNamespace(rowcount=0)

        async def commit(self):
            self.commits += 1

    session = EmptySession()
    result = await IssueService().mark_issue_reopened(
        "owner", "missing-repo", 536, session
    )

    assert result == {"state_updated": 0}
    assert session.commits == 1


@pytest.mark.asyncio
async def test_reopened_syncs_short_name_row_when_semantic_linking_disabled(
    monkeypatch, webhook_dependencies
):
    webhook, issue_service, events = webhook_dependencies
    row = SimpleNamespace(issue_state="closed", status="cancelled")

    async def mark_reopened(owner, repo, number, db):
        events.append(("reopen", owner, repo, number, db))
        row.issue_state = "open"
        return {"state_updated": 1}

    async def disabled(_name):
        return False

    monkeypatch.setattr(issue_service, "mark_issue_reopened", mark_reopened)
    monkeypatch.setattr(webhook, "get_dynamic_config", disabled)

    response = await webhook.handle_issue_event(_issue_payload("reopened"))

    assert response.status_code == 200
    assert json.loads(response.body.decode()) == {
        "status": "skipped",
        "reason": "feature disabled",
    }
    assert row.issue_state == "open"
    assert row.status == "cancelled"
    assert len(events) == 1
    assert events[0][:4] == ("reopen", "owner", "sakura-ai-test", 536)


@pytest.mark.asyncio
async def test_reopened_syncs_db_after_semantic_vector_reopen(
    monkeypatch, webhook_dependencies
):
    webhook, issue_service, events = webhook_dependencies
    webhook.settings.enable_semantic_issue_linking = True
    row_states = {
        ("owner", "sakura-ai-test"): "closed",
        ("owner", "owner/sakura-ai-test"): "closed",
        ("other-owner", "sakura-ai-test"): "closed",
    }

    class FakeEmbeddingService:
        async def upsert_issue(self, owner, repo, number, **kwargs):
            events.append(("vector-reopen", owner, repo, number, kwargs))

    async def mark_reopened(owner, repo, number, _db):
        events.append(("reopen", owner, repo, number))
        for identity in list(row_states):
            if identity[0] == owner and identity[1] in {
                repo,
                f"{owner}/{repo}",
            }:
                row_states[identity] = "open"
        return {"state_updated": 2}

    async def disabled(_name):
        return False

    from backend.services import issue_embedding_service

    monkeypatch.setattr(
        issue_embedding_service, "IssueEmbeddingService", FakeEmbeddingService
    )
    monkeypatch.setattr(issue_service, "mark_issue_reopened", mark_reopened)
    monkeypatch.setattr(webhook, "get_dynamic_config", disabled)

    response = await webhook.handle_issue_event(_issue_payload("reopened"))

    assert response.status_code == 200
    assert row_states == {
        ("owner", "sakura-ai-test"): "open",
        ("owner", "owner/sakura-ai-test"): "open",
        ("other-owner", "sakura-ai-test"): "closed",
    }
    assert [event[0] for event in events] == ["vector-reopen", "reopen"]


@pytest.mark.asyncio
async def test_save_result_drops_stale_worker_after_close_wins_race():
    from backend.services.issue_service import IssueService

    record = SimpleNamespace(id=1, status="cancelled", issue_state="closed")

    class RaceSession:
        def __init__(self):
            self.calls = 0
            self.commits = 0

        async def execute(self, statement):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(scalar_one_or_none=lambda: record)
            # Simulate the conditional UPDATE observing the close commit.
            return SimpleNamespace(rowcount=0)

        async def commit(self):
            self.commits += 1

    result = await IssueService().save_analysis_result(
        {"summary": "stale result"},
        {
            "repo_owner": "owner",
            "repo_name": "sakura-ai-test",
            "repo_full_name": "owner/sakura-ai-test",
            "issue_number": 536,
        },
        RaceSession(),
    )

    assert result is None
    assert record.status == "cancelled"
    assert record.issue_state == "closed"


def test_issue_templates_render_cancelled_status_and_filter():
    template_root = Path(__file__).resolve().parents[1] / "backend" / "webui" / "templates"
    detail = (template_root / "issue_detail.html").read_text(encoding="utf-8")
    fragment = (
        template_root / "components" / "issue_detail_fragment.html"
    ).read_text(encoding="utf-8")
    listing = (
        template_root / "components" / "issue_list_fragment.html"
    ).read_text(encoding="utf-8")
    filters = (
        template_root / "components" / "issue_filters.html"
    ).read_text(encoding="utf-8")

    assert detail.count("analysis.status == 'cancelled'") == 1
    assert "{{ _('issue.cancelled_status') }}" in detail
    assert "analysis.status == 'cancelled'" in fragment
    assert "{{ _('issue.cancelled_status') }}" in fragment
    assert "a.status == 'cancelled'" in listing
    assert '<option value="cancelled"' in filters
