from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.models.database import PRReview, PRReviewIncrementalQueue
from backend.services.pr_review_incremental_queue import (
    PRReviewIncrementalQueueService,
)


class _Result:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def all(self):
        if isinstance(self.value, list):
            return self.value
        if self.value is None:
            return []
        return [self.value]

    def first(self):
        values = self.all()
        return values[0] if values else None


class _MemoryDb:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def add(self, obj):
        if isinstance(obj, PRReview):
            if obj.id is None:
                obj.id = self.store["next_review_id"]
                self.store["next_review_id"] += 1
            if obj.created_at is None:
                obj.created_at = datetime.utcnow()
            self.store["reviews"][obj.id] = obj
        elif isinstance(obj, PRReviewIncrementalQueue):
            if obj.id is None:
                obj.id = self.store["next_queue_id"]
                self.store["next_queue_id"] += 1
            if obj.created_at is None:
                obj.created_at = datetime.utcnow()
            self.store["queue"][obj.id] = obj

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        params = statement.compile().params
        if entity is PRReview:
            rows = list(self.store["reviews"].values())
            rows = [
                row
                for row in rows
                if self._matches(row, params, {
                    "repo_owner_1": "repo_owner",
                    "repo_name_1": "repo_name",
                    "pr_id_1": "pr_id",
                })
            ]
            status_filter = params.get("status_1")
            if status_filter is not None:
                rows = [row for row in rows if row.status in status_filter]
            rows = sorted(
                rows,
                key=lambda row: (row.created_at or datetime.min, row.id or 0),
                reverse=True,
            )
            return _Result(rows)

        if entity is PRReviewIncrementalQueue:
            rows = list(self.store["queue"].values())
            rows = [
                row
                for row in rows
                if self._matches(row, params, {
                    "delivery_id_1": "delivery_id",
                    "repo_full_name_1": "repo_full_name",
                    "pr_number_1": "pr_number",
                    "head_sha_1": "head_sha",
                    "status_1": "status",
                })
            ]
            rows = sorted(
                rows,
                key=lambda row: (row.created_at or datetime.min, row.id or 0),
            )
            return _Result(rows)

        return _Result([])

    @staticmethod
    def _matches(row, params, mapping):
        for param_name, attr_name in mapping.items():
            if param_name in params and getattr(row, attr_name) != params[param_name]:
                return False
        return True


class _MemorySessionFactory:
    def __init__(self, store):
        self.store = store

    def __call__(self):
        return _MemoryDb(self.store)


def _make_store():
    return {
        "reviews": {},
        "queue": {},
        "next_review_id": 1,
        "next_queue_id": 1,
    }


@pytest.fixture
def queue_store(monkeypatch):
    store = _make_store()
    monkeypatch.setattr(
        "backend.services.pr_review_incremental_queue.db_module.async_session",
        _MemorySessionFactory(store),
    )
    return store


def _pr_info(head_sha="head2"):
    return {
        "repo_owner": "owner",
        "repo_name": "repo",
        "repo_full_name": "owner/repo",
        "pr_id": 1001,
        "pr_number": 7,
        "before": "base1",
        "after": head_sha,
        "head_sha": head_sha,
    }


def _add_review(store, status, created_offset=0):
    review = PRReview(
        id=store["next_review_id"],
        pr_id=1001,
        repo_owner="owner",
        repo_name="repo",
        author="alice",
        title="PR",
        branch="feature",
        strategy="standard",
        status=status,
        created_at=datetime.utcnow() + timedelta(seconds=created_offset),
    )
    store["next_review_id"] += 1
    store["reviews"][review.id] = review
    return review


def _add_queue(store, *, base_sha, head_sha, created_offset=0):
    item = PRReviewIncrementalQueue(
        id=store["next_queue_id"],
        repo_owner="owner",
        repo_name="repo",
        repo_full_name="owner/repo",
        pr_number=7,
        base_sha=base_sha,
        head_sha=head_sha,
        status="pending",
        created_at=datetime.utcnow() + timedelta(seconds=created_offset),
    )
    store["next_queue_id"] += 1
    store["queue"][item.id] = item
    return item


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_by_delivery_and_pending_head(queue_store):
    _add_review(queue_store, "reviewing")
    service = PRReviewIncrementalQueueService()
    pr_info = _pr_info(head_sha="head2")

    first = await service.enqueue_from_webhook(pr_info, delivery_id="delivery-1")
    duplicate_delivery = await service.enqueue_from_webhook(
        pr_info,
        delivery_id="delivery-1",
    )
    duplicate_head = await service.enqueue_from_webhook(pr_info)

    assert first is not None
    assert duplicate_delivery.id == first.id
    assert duplicate_head.id == first.id
    assert len(queue_store["queue"]) == 1


@pytest.mark.asyncio
async def test_find_active_review_only_returns_pending_or_reviewing(queue_store):
    service = PRReviewIncrementalQueueService()
    _add_review(queue_store, "completed")

    assert await service.find_active_review(_pr_info()) is None

    pending = _add_review(queue_store, "pending", created_offset=10)

    active = await service.find_active_review(_pr_info())

    assert active is pending


@pytest.mark.asyncio
async def test_consume_pending_merges_events_and_marks_consumed(queue_store):
    _add_queue(queue_store, base_sha="base1", head_sha="head2", created_offset=1)
    _add_queue(queue_store, base_sha="head2", head_sha="head3", created_offset=2)
    service = PRReviewIncrementalQueueService()

    file_obj = SimpleNamespace(
        filename="backend/app.py",
        status="modified",
        additions=2,
        deletions=1,
        patch="@@ -1 +1 @@\n-old\n+new",
    )
    commit_obj = SimpleNamespace(
        sha="head333333",
        commit=SimpleNamespace(
            message="fix: update app\n\nbody",
            author=SimpleNamespace(name="Alice"),
        ),
    )
    repo = SimpleNamespace(
        compare=lambda base, head: SimpleNamespace(
            files=[file_obj],
            commits=[commit_obj],
            base=base,
            head=head,
        )
    )

    message = await service.consume_pending_for_review(
        pr_info=_pr_info(head_sha="head3"),
        review_id=12,
        session_id=34,
        repo=repo,
    )

    assert message is not None
    assert message["role"] == "user"
    assert "base1...head3" in message["content"]
    assert "backend/app.py" in message["content"]
    assert "fix: update app" in message["content"]
    assert {item.status for item in queue_store["queue"].values()} == {"consumed"}
    assert {item.consumed_review_id for item in queue_store["queue"].values()} == {12}
    assert {item.consumed_session_id for item in queue_store["queue"].values()} == {34}
