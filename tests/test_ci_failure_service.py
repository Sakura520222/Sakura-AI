"""CIFailureService 单元测试。"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.models.database import CIFailure, HeadShaPRMap
from backend.services.ci_failure_service import CIFailureService


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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

    def scalar_one_or_none(self):
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
        if isinstance(obj, CIFailure):
            existing = self._find_ci_failure(
                obj.repo_full_name,
                obj.head_sha,
                obj.source,
                obj.external_id,
            )
            if existing is not None:
                existing.repo_owner = obj.repo_owner
                existing.repo_name = obj.repo_name
                existing.pr_number = obj.pr_number
                existing.name = obj.name
                existing.conclusion = obj.conclusion
                existing.output_title = obj.output_title
                existing.output_summary = obj.output_summary
                existing.output_text = obj.output_text
                existing.failed_steps_json = obj.failed_steps_json
                existing.annotations_json = obj.annotations_json
                existing.details_url = obj.details_url
                return
            if obj.id is None:
                obj.id = self.store["next_ci_id"]
                self.store["next_ci_id"] += 1
            if obj.created_at is None:
                obj.created_at = _utcnow_naive()
            self.store["ci_failures"][obj.id] = obj
        elif isinstance(obj, HeadShaPRMap):
            existing = self._find_head_map(obj.repo_full_name, obj.head_sha)
            if existing is not None:
                existing.pr_number = obj.pr_number
                existing.repo_owner = obj.repo_owner
                existing.repo_name = obj.repo_name
                existing.updated_at = _utcnow_naive()
                return
            if obj.id is None:
                obj.id = self.store["next_map_id"]
                self.store["next_map_id"] += 1
            if obj.updated_at is None:
                obj.updated_at = _utcnow_naive()
            self.store["head_maps"][obj.id] = obj

    async def commit(self):
        return None

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is CIFailure:
            rows = list(self.store["ci_failures"].values())
            rows = self._apply_where(rows, statement)
            rows = sorted(
                rows, key=lambda row: row.created_at or datetime.min, reverse=True
            )
            return _Result(rows)
        if entity is HeadShaPRMap:
            rows = list(self.store["head_maps"].values())
            rows = self._apply_where(rows, statement)
            return _Result(rows)
        return _Result([])

    async def delete(self, obj):
        if isinstance(obj, CIFailure):
            self.store["ci_failures"].pop(obj.id, None)
        elif isinstance(obj, HeadShaPRMap):
            self.store["head_maps"].pop(obj.id, None)

    @staticmethod
    def _apply_where(rows, statement):
        """解析 whereclause 的比较操作符（eq/ne/lt/le/gt/ge），支持范围查询。"""
        from sqlalchemy.sql import operators

        where = statement.whereclause
        if where is None:
            return rows
        clauses = getattr(where, "clauses", None) or [where]
        for clause in clauses:
            col = getattr(clause, "left", None)
            right = getattr(clause, "right", None)
            op = getattr(clause, "operator", None)
            attr = getattr(col, "key", None)
            if attr is None or right is None:
                continue
            value = getattr(right, "value", right)

            def keep(row, attr=attr, op=op, value=value):
                cell = getattr(row, attr)
                if op == operators.eq:
                    return cell == value
                if op == operators.ne:
                    return cell != value
                if cell is None or value is None:
                    return False
                if op == operators.lt:
                    return cell < value
                if op == operators.le:
                    return cell <= value
                if op == operators.gt:
                    return cell > value
                if op == operators.ge:
                    return cell >= value
                return True

            rows = [row for row in rows if keep(row)]
        return rows

    def _find_ci_failure(self, repo_full_name, head_sha, source, external_id):
        for item in self.store["ci_failures"].values():
            if (
                item.repo_full_name == repo_full_name
                and item.head_sha == head_sha
                and item.source == source
                and item.external_id == external_id
            ):
                return item
        return None

    def _find_head_map(self, repo_full_name, head_sha):
        for item in self.store["head_maps"].values():
            if item.repo_full_name == repo_full_name and item.head_sha == head_sha:
                return item
        return None


class _MemorySessionFactory:
    def __init__(self, store):
        self.store = store

    def __call__(self):
        return _MemoryDb(self.store)


def _make_store():
    return {
        "ci_failures": {},
        "head_maps": {},
        "next_ci_id": 1,
        "next_map_id": 1,
    }


@pytest.fixture
def ci_store(monkeypatch):
    store = _make_store()
    monkeypatch.setattr(
        "backend.services.ci_failure_service.db_module.async_session",
        _MemorySessionFactory(store),
    )
    monkeypatch.setattr(
        "backend.services.ci_failure_service.get_strategy_config",
        lambda: SimpleNamespace(
            get_context_enhancement_config=lambda: {
                "ci_failure_injection": {
                    "enabled": True,
                    "retention_days": 7,
                    "max_records": 2,
                    "max_annotations_per_record": 1,
                }
            }
        ),
    )
    return store


@pytest.mark.asyncio
async def test_record_check_run_failure_filters_sakura_self_check(ci_store):
    service = CIFailureService()
    payload = {
        "id": 100,
        "name": "Sakura AI Review",
        "head_sha": "sha1",
        "conclusion": "failure",
    }

    await service.record_check_run_failure(
        "owner", "repo", "owner/repo", 7, "sha1", payload
    )

    assert ci_store["ci_failures"] == {}


@pytest.mark.asyncio
async def test_record_check_run_failure_stores_output_and_annotations(ci_store):
    service = CIFailureService()
    service._app = MagicMock()
    service._app.get_check_run_annotations.return_value = [
        {
            "path": "src/app.py",
            "start_line": 12,
            "end_line": 12,
            "annotation_level": "failure",
            "title": "lint",
            "message": "undefined name 'x'",
            "raw_details": "full detail",
        }
    ]
    payload = {
        "id": 101,
        "name": "lint",
        "head_sha": "sha1",
        "conclusion": "failure",
        "details_url": "https://ci.example/lint/101",
        "output": {
            "title": "Lint failed",
            "summary": "1 error",
            "text": "complete output text must not be truncated",
        },
    }

    await service.record_check_run_failure(
        "owner", "repo", "owner/repo", 7, "sha1", payload
    )

    failures = list(ci_store["ci_failures"].values())
    assert len(failures) == 1
    failure = failures[0]
    assert failure.source == "check_run"
    assert failure.name == "lint"
    assert failure.external_id == "101"
    assert failure.output_title == "Lint failed"
    assert failure.output_summary == "1 error"
    assert failure.output_text == "complete output text must not be truncated"
    assert json.loads(failure.annotations_json)[0]["message"] == "undefined name 'x'"


@pytest.mark.asyncio
async def test_record_workflow_job_failure_stores_failed_steps(ci_store):
    service = CIFailureService()
    payload = {
        "id": 202,
        "name": "tests",
        "head_sha": "sha2",
        "conclusion": "timed_out",
        "html_url": "https://github.com/owner/repo/actions/jobs/202",
        "steps": [
            {"name": "Install", "conclusion": "success"},
            {"name": "pytest", "conclusion": "failure"},
            {"name": "upload logs", "conclusion": "skipped"},
        ],
    }

    await service.record_workflow_job_failure(
        "owner", "repo", "owner/repo", 8, "sha2", payload
    )

    failure = next(iter(ci_store["ci_failures"].values()))
    assert failure.source == "workflow_job"
    assert failure.name == "tests"
    assert failure.conclusion == "timed_out"
    assert failure.details_url == "https://github.com/owner/repo/actions/jobs/202"
    assert json.loads(failure.failed_steps_json) == [
        {"name": "pytest", "conclusion": "failure"}
    ]


@pytest.mark.asyncio
async def test_fetch_for_review_applies_record_and_annotation_limits_without_truncating_text(
    ci_store,
):
    service = CIFailureService()
    long_message = "x" * 1200
    for index in range(3):
        ci_store["ci_failures"][index + 1] = CIFailure(
            id=index + 1,
            repo_owner="owner",
            repo_name="repo",
            repo_full_name="owner/repo",
            pr_number=7,
            head_sha="sha1",
            source="check_run",
            name=f"check-{index}",
            conclusion="failure",
            output_summary="summary",
            output_text=long_message,
            annotations_json=json.dumps(
                [{"path": "a.py", "start_line": 1, "message": long_message}],
                ensure_ascii=False,
            ),
            annotations_total=2,  # 原始 2 条，record 时已限额存储 1 条
            external_id=str(index),
            created_at=_utcnow_naive() + timedelta(seconds=index),
        )

    result = await service.fetch_for_review("owner/repo", "sha1")

    assert len(result) == 2
    assert result[0]["output_text"] == long_message
    assert result[0]["omitted_annotations"] == 1
    assert result[0]["annotations"][0]["message"] == long_message
    assert result[0]["omitted_records"] == 1


@pytest.mark.asyncio
async def test_fetch_dedupes_same_name_keep_latest(ci_store):
    """同名 CI 失败多次触发（不同 external_id）时，fetch 只保留最新一条。"""
    service = CIFailureService()
    base = _utcnow_naive()
    for index, (summary, offset) in enumerate([("old", 0), ("new", 10)]):
        ci_store["ci_failures"][index + 1] = CIFailure(
            id=index + 1,
            repo_owner="owner",
            repo_name="repo",
            repo_full_name="owner/repo",
            pr_number=7,
            head_sha="sha1",
            source="check_run",
            name="Gitflow",
            conclusion="failure",
            output_summary=summary,
            external_id=str(index),
            created_at=base + timedelta(seconds=offset),
        )

    result = await service.fetch_for_review("owner/repo", "sha1")

    assert len(result) == 1
    assert result[0]["output_summary"] == "new"


@pytest.mark.asyncio
async def test_fetch_keeps_same_name_across_different_sources(ci_store):
    """同 name 但不同 source（check_run vs workflow_job）不去重，各自保留。"""
    service = CIFailureService()
    base = _utcnow_naive()
    for index, source in enumerate(["check_run", "workflow_job"]):
        ci_store["ci_failures"][index + 1] = CIFailure(
            id=index + 1,
            repo_owner="owner",
            repo_name="repo",
            repo_full_name="owner/repo",
            pr_number=7,
            head_sha="sha1",
            source=source,
            name="Gitflow",
            conclusion="failure",
            external_id=str(index),
            created_at=base + timedelta(seconds=index),
        )

    result = await service.fetch_for_review("owner/repo", "sha1")

    assert len(result) == 2
    assert {r["source"] for r in result} == {"check_run", "workflow_job"}


@pytest.mark.asyncio
async def test_record_caps_annotations_at_storage_time(ci_store):
    """record 时即按 max_annotations_per_record 限额存储，原始总数记入 annotations_total。"""
    service = CIFailureService()
    service._app = MagicMock()
    service._app.get_check_run_annotations.return_value = [
        {"path": f"f{i}.py", "start_line": i, "message": f"err {i}"} for i in range(5)
    ]
    payload = {
        "id": 303,
        "name": "lint",
        "head_sha": "sha3",
        "conclusion": "failure",
        "output": {"title": "t", "summary": "s"},
    }

    await service.record_check_run_failure(
        "owner", "repo", "owner/repo", 7, "sha3", payload
    )

    failure = next(iter(ci_store["ci_failures"].values()))
    # 配置 max_annotations_per_record=1，存储 1 条
    stored = json.loads(failure.annotations_json)
    assert len(stored) == 1
    assert failure.annotations_total == 5  # 原始 5 条


@pytest.mark.asyncio
async def test_head_sha_map_upsert_and_lookup(ci_store):
    service = CIFailureService()

    await service.upsert_head_sha_pr_map("owner", "repo", "owner/repo", "sha1", 7)
    await service.upsert_head_sha_pr_map("owner", "repo", "owner/repo", "sha1", 8)

    assert len(ci_store["head_maps"]) == 1
    assert await service.lookup_pr_number("owner/repo", "sha1") == 8


@pytest.mark.asyncio
async def test_delete_failures_on_success_rerun(ci_store):
    """同一 check name 重跑成功后，应清除该 (repo, head_sha, name) 的旧失败记录。"""
    service = CIFailureService()
    ci_store["ci_failures"][1] = CIFailure(
        id=1,
        repo_owner="owner",
        repo_name="repo",
        repo_full_name="owner/repo",
        pr_number=7,
        head_sha="sha1",
        source="check_run",
        name="lint",
        conclusion="failure",
        external_id="100",
        created_at=_utcnow_naive(),
    )
    ci_store["ci_failures"][2] = CIFailure(
        id=2,
        repo_owner="owner",
        repo_name="repo",
        repo_full_name="owner/repo",
        pr_number=7,
        head_sha="sha1",
        source="check_run",
        name="tests",
        conclusion="failure",
        external_id="200",
        created_at=_utcnow_naive(),
    )

    deleted = await service.delete_failures("owner/repo", "sha1", "check_run", "lint")

    assert deleted == 1
    remaining_names = [r.name for r in ci_store["ci_failures"].values()]
    assert remaining_names == ["tests"]


@pytest.mark.asyncio
async def test_cleanup_for_pr_and_expired(ci_store):
    service = CIFailureService()
    ci_store["ci_failures"][1] = CIFailure(
        id=1,
        repo_owner="owner",
        repo_name="repo",
        repo_full_name="owner/repo",
        pr_number=7,
        head_sha="sha1",
        source="check_run",
        name="lint",
        conclusion="failure",
        external_id="1",
        created_at=_utcnow_naive() - timedelta(days=10),
    )
    ci_store["ci_failures"][2] = CIFailure(
        id=2,
        repo_owner="owner",
        repo_name="repo",
        repo_full_name="owner/repo",
        pr_number=8,
        head_sha="sha2",
        source="check_run",
        name="lint",
        conclusion="failure",
        external_id="2",
        created_at=_utcnow_naive(),
    )

    assert await service.cleanup_for_pr("owner/repo", 8) == 1
    assert 2 not in ci_store["ci_failures"]

    assert await service.cleanup_expired() == 1
    assert ci_store["ci_failures"] == {}
