"""仓库扫描软超时与协议失败终态测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models.scan_models import ScanStatus
from backend.services.ai_task_deadline import AITaskDeadline
from backend.workers import scan_worker as scan_worker_module
from backend.workers.scan_worker import ScanWorker


class _AsyncSession:
    def __init__(self, scan):
        self.scan = scan

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, *_args):
        return self.scan


class _Execution:
    def __init__(self):
        self.finish = AsyncMock()


@pytest.mark.asyncio
async def test_protocol_failure_marks_scan_failed_before_side_effects(
    monkeypatch, tmp_path
):
    scan = SimpleNamespace(
        status=ScanStatus.PENDING.value,
        repo_name="owner/repo",
        triggered_by="webui:1",
    )

    import backend.models.database as database_module

    monkeypatch.setattr(
        database_module,
        "async_session",
        lambda: _AsyncSession(scan),
    )

    worker = ScanWorker.__new__(ScanWorker)
    execution = _Execution()
    updates = []
    save_findings = AsyncMock()
    generate_reports = AsyncMock()

    async def _update_scan(_scan_id, **kwargs):
        updates.append(kwargs)

    async def _full_scan(**_kwargs):
        return (
            {
                "findings": [],
                "overall_score": None,
                "summary": "scan protocol could not be repaired",
                "parse_source": "scan_protocol_error",
            },
            4,
        )

    monkeypatch.setattr(worker, "_start_threaded_execution", AsyncMock(return_value=execution))
    monkeypatch.setattr(worker, "_clone_repo", AsyncMock(return_value=str(tmp_path)))
    monkeypatch.setattr(worker, "_get_commit_sha", AsyncMock(return_value="abc"))
    monkeypatch.setattr(
        worker,
        "_index_repository",
        AsyncMock(return_value={"total_chunks": 2, "indexed": 1}),
    )
    monkeypatch.setattr(
        scan_worker_module,
        "get_user_dynamic_config",
        AsyncMock(return_value="zh-CN"),
    )
    import backend.services.scan_prompt_builder as prompt_builder

    monkeypatch.setattr(
        prompt_builder,
        "collect_code_files",
        lambda _repo_path: [{"path": "app.py"}],
    )
    monkeypatch.setattr(worker, "_update_scan", _update_scan)
    monkeypatch.setattr(worker, "_full_scan_with_tools", _full_scan)
    monkeypatch.setattr(worker, "_save_findings", save_findings)
    monkeypatch.setattr(worker, "_generate_reports", generate_reports)

    await worker._process_scan_inner(
        7,
        deadline=AITaskDeadline.from_timeout(60),
    )

    assert any(
        update.get("status") == ScanStatus.FAILED.value for update in updates
    )
    assert not any(
        update.get("status") in {ScanStatus.REPORTING.value, ScanStatus.COMPLETED.value}
        for update in updates
    )
    save_findings.assert_not_awaited()
    generate_reports.assert_not_awaited()
    execution.finish.assert_awaited_once_with(
        "failed", error_message="scan protocol could not be repaired"
    )


@pytest.mark.asyncio
async def test_process_scan_passes_one_deadline_through_semaphore(monkeypatch):
    worker = ScanWorker.__new__(ScanWorker)
    observed = {}

    class _Semaphore:
        async def __aenter__(self):
            observed["entered"] = True
            return self

        async def __aexit__(self, *_args):
            return False

    async def _get_semaphore():
        assert "deadline" not in observed
        return _Semaphore()

    async def _inner(scan_id, *, deadline):
        observed["scan_id"] = scan_id
        observed["deadline"] = deadline

    monkeypatch.setattr(scan_worker_module, "_get_scan_semaphore", _get_semaphore)
    monkeypatch.setattr(worker, "_process_scan_inner", _inner)

    await worker.process_scan(11)

    assert observed["scan_id"] == 11
    assert isinstance(observed["deadline"], AITaskDeadline)
    assert observed["entered"] is True
