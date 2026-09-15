"""recover_pending_deployment_transaction 中断恢复语义测试。

`prepared` journal 意味着 authoritative deployment.env 可能已被替换但更新未达
health gate——必须先恢复精确旧字节；`committed` 仅在 health gate 之后写入，安全
收尾（删除保留的 rollback copy）。任何畸形 journal 或恢复失败都是硬错误：静默
删除任一 artifact 都会把中断的部署变成不可恢复的混合状态。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sakura_ai_updater.adapters.image import (
    ImageAdapterError,
    _transaction_paths,
    _write_transaction_journal,
    recover_pending_deployment_transaction,
)


def _write_journal(destination: Path, **overrides) -> Path:
    journal, _backup = _transaction_paths(destination)
    data = {
        "schema_version": 1,
        "state": "prepared",
        "deployment_env": str(destination),
        "had_content": True,
        **overrides,
    }
    _write_transaction_journal(journal, data)
    return journal


def test_recovery_noop_without_journal(tmp_path):
    destination = tmp_path / "deployment.env"
    destination.write_text("current\n", encoding="utf-8")
    recover_pending_deployment_transaction(str(destination))
    assert destination.read_text(encoding="utf-8") == "current\n"


def test_recovery_committed_finishes_by_deleting_artifacts(tmp_path):
    destination = tmp_path / "deployment.env"
    destination.write_text("NEW authoritative\n", encoding="utf-8")
    journal, backup = _transaction_paths(destination)
    backup.write_bytes(b"OLD bytes")
    _write_journal(destination, state="committed")

    recover_pending_deployment_transaction(str(destination))

    assert destination.read_text(encoding="utf-8") == "NEW authoritative\n"
    assert not journal.exists()
    assert not backup.exists()


def test_recovery_prepared_restores_exact_rollback_bytes_and_mode(tmp_path):
    destination = tmp_path / "deployment.env"
    destination.write_text("HALF-APPLIED new\n", encoding="utf-8")
    journal, backup = _transaction_paths(destination)
    backup.write_bytes(b"OLD bytes\n")
    _write_journal(destination, mode=0o640)

    recover_pending_deployment_transaction(str(destination))

    assert destination.read_bytes() == b"OLD bytes\n"
    assert os.stat(destination).st_mode & 0o7777 == 0o640
    assert not journal.exists()
    assert not backup.exists()


def test_recovery_prepared_without_prior_content_removes_destination(tmp_path):
    destination = tmp_path / "deployment.env"
    destination.write_text("NEW file that never existed before\n", encoding="utf-8")
    journal, backup = _transaction_paths(destination)
    _write_journal(destination, had_content=False)

    recover_pending_deployment_transaction(str(destination))

    assert not destination.exists()
    assert not journal.exists()
    assert not backup.exists()


def test_recovery_rejects_journal_path_mismatch(tmp_path):
    destination = tmp_path / "deployment.env"
    other = tmp_path / "other.env"
    _write_journal(destination, deployment_env=str(other))
    with pytest.raises(ImageAdapterError, match="journal path mismatch"):
        recover_pending_deployment_transaction(str(destination))


def test_recovery_rejects_backup_escaping_directory(tmp_path, tmp_path_factory):
    destination = tmp_path / "deployment.env"
    outside = tmp_path_factory.mktemp("outside") / "evil"
    _write_journal(destination, backup=str(outside))
    with pytest.raises(ImageAdapterError, match="escapes its directory"):
        recover_pending_deployment_transaction(str(destination))


def test_recovery_fails_closed_when_rollback_copy_missing(tmp_path):
    destination = tmp_path / "deployment.env"
    destination.write_text("half-applied\n", encoding="utf-8")
    journal, _backup = _transaction_paths(destination)
    _write_journal(destination)

    with pytest.raises(ImageAdapterError, match="rollback copy is missing"):
        recover_pending_deployment_transaction(str(destination))

    # 硬错误：artifact 不得被静默清理（混合状态必须可人工恢复）
    assert journal.exists()
    assert destination.exists()


def test_recovery_fails_closed_on_malformed_journal(tmp_path):
    destination = tmp_path / "deployment.env"
    journal, _backup = _transaction_paths(destination)
    journal.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ImageAdapterError, match="unreadable"):
        recover_pending_deployment_transaction(str(destination))


def test_recovery_rejects_invalid_journal_shape(tmp_path):
    destination = tmp_path / "deployment.env"
    journal, _backup = _transaction_paths(destination)
    journal.write_text(
        json.dumps({"schema_version": 1, "state": "prepared"}),
        encoding="utf-8",
    )
    with pytest.raises(ImageAdapterError):
        recover_pending_deployment_transaction(str(destination))


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_crash_recovery_reconverges_runtime_before_discarding_journal(tmp_path, monkeypatch, fail):
    from sakura_ai_updater.adapters.image import ImageAdapter

    destination = tmp_path / "deployment.env"
    destination.write_text("SAKURA_AI_IMAGE=new\n")
    journal, backup = _transaction_paths(destination)
    backup.write_text("SAKURA_AI_IMAGE=old\n")
    _write_journal(destination)
    adapter = ImageAdapter(str(tmp_path / "compose.yml"), str(destination))
    calls = []

    async def converge(snapshot, *, remove_new_sandbox):
        assert journal.exists() and backup.exists()
        assert destination.read_text() == "SAKURA_AI_IMAGE=old\n"
        assert snapshot.values["SAKURA_AI_IMAGE"] == "old"
        assert remove_new_sandbox is True
        calls.append("converge")
        if fail:
            raise ImageAdapterError("runtime recovery failed")

    monkeypatch.setattr(adapter, "_restore_and_reconverge", converge)
    if fail:
        with pytest.raises(ImageAdapterError, match="runtime recovery failed"):
            await adapter.recover_pending_transaction()
        assert journal.exists() and backup.exists()
    else:
        await adapter.recover_pending_transaction()
        assert not journal.exists() and not backup.exists()
        await adapter.recover_pending_transaction()
    assert calls == ["converge"]
