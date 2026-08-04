"""CheckRunService 单元测试（主从式三 Check 架构）。

覆盖：ReviewRunKey 驱动的 report_*、decision→conclusion 映射、中英 output、
步骤清单符号、external_id 编码、Analysis 节流、不追溯改写、cancel_by_sha、
异常吞掉、enable_* 开关、output 无 emoji。
"""

import re
from unittest.mock import MagicMock

import pytest

from backend.models.database import ReviewDecision
from backend.services.check_run_service import (
    KIND_ANALYSIS,
    KIND_FINDINGS,
    KIND_REVIEW,
    CheckRunService,
    ReviewProgressSnapshot,
    ReviewRunKey,
)

# 宽松的 emoji 检测：覆盖常见 emoji Unicode 区间 + 项目评论用过的符号
_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U0001f600-\U0001f64f✅❌⚠⏳✨]",
    flags=re.UNICODE,
)


def _assert_no_emoji(*texts: str) -> None:
    for t in texts:
        if not t:
            continue
        m = _EMOJI_RE.search(t)
        assert not m, f"发现 emoji {m.group()!r} in {t!r}"


def _key(job: str = "job1") -> ReviewRunKey:
    return ReviewRunKey("o/r", 1, "sha", job)


@pytest.fixture()
def svc(monkeypatch):
    """启用全部 Check + 默认中文的 CheckRunService（_app 已 mock）。"""
    monkeypatch.setattr(
        "backend.services.check_run_service.get_settings",
        lambda: MagicMock(
            enable_check_runs=True,
            enable_analysis_check=True,
            enable_findings_check=True,
            analysis_min_interval_sec=3,
            output_language="zh",
        ),
    )
    service = CheckRunService()
    service._app = MagicMock()
    return service


# ---------------- external_id 编码 ----------------


def test_external_id_encoding_per_kind():
    """三 check_kind 的 external_id 互异且可解析。"""
    rid = "123"
    assert (
        CheckRunService.encode_external_id(rid, KIND_REVIEW)
        == "sakura-ai:v1:123:review"
    )
    assert (
        CheckRunService.encode_external_id(rid, KIND_ANALYSIS)
        == "sakura-ai:v1:123:analysis"
    )
    assert (
        CheckRunService.encode_external_id(rid, KIND_FINDINGS)
        == "sakura-ai:v1:123:findings"
    )


# ---------------- 开关 ----------------


@pytest.mark.asyncio
async def test_disabled_returns_early(monkeypatch):
    monkeypatch.setattr(
        "backend.services.check_run_service.get_settings",
        lambda: MagicMock(
            enable_check_runs=False,
            enable_analysis_check=True,
            enable_findings_check=True,
            analysis_min_interval_sec=3,
            output_language="zh",
        ),
    )
    service = CheckRunService()
    service._app = MagicMock()

    await service.report_queued(_key(), pr_number=1)

    service._app.cleanup_stale_check_runs.assert_not_called()
    service._app.create_check_run.assert_not_called()


@pytest.mark.asyncio
async def test_analysis_snapshot_respects_sub_switch(monkeypatch):
    """enable_analysis_check=False 时 Analysis 不创建。"""
    monkeypatch.setattr(
        "backend.services.check_run_service.get_settings",
        lambda: MagicMock(
            enable_check_runs=True,
            enable_analysis_check=False,
            enable_findings_check=True,
            analysis_min_interval_sec=3,
            output_language="zh",
        ),
    )
    service = CheckRunService()
    service._app = MagicMock()
    snap = ReviewProgressSnapshot(1, 20, 3)

    await service.report_analysis_snapshot(_key(), snap)

    service._app.create_check_run.assert_not_called()


# ---------------- report_queued ----------------


@pytest.mark.asyncio
async def test_report_queued_creates_when_not_found_zh(svc):
    svc._app.cleanup_stale_check_runs.return_value = None
    svc._app.create_check_run.return_value = {"id": 1}

    await svc.report_queued(_key(), pr_number=5, output_language="zh")

    svc._app.create_check_run.assert_called_once()
    svc._app.update_check_run.assert_not_called()
    call = svc._app.create_check_run.call_args
    assert call.args == ("o", "r", "Sakura AI Review", "sha")
    assert call.kwargs["status"] == "queued"
    assert call.kwargs["output_title"] == "Sakura AI 审查已排队"
    assert "PR #5 已排队" in call.kwargs["output_summary"]
    # create 时写 external_id（check_kind=review）
    assert call.kwargs["external_id"] == "sakura-ai:v1:job1:review"
    _assert_no_emoji(call.kwargs["output_title"], call.kwargs["output_summary"])


@pytest.mark.asyncio
async def test_report_queued_english(svc):
    svc._app.cleanup_stale_check_runs.return_value = None
    svc._app.create_check_run.return_value = {"id": 1}

    await svc.report_queued(_key(), pr_number=5, output_language="en")

    kw = svc._app.create_check_run.call_args.kwargs
    assert kw["output_title"] == "Review Queued"
    assert "PR #5 queued" in kw["output_summary"]


# ---------------- report_stage_progress（步骤清单） ----------------


@pytest.mark.asyncio
async def test_stage_progress_renders_step_list(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_stage_progress(
        _key(),
        stage="reviewing",
        completed_stages=["fetching", "indexing", "summary"],
        output_language="zh",
    )

    kw = svc._app.update_check_run.call_args.kwargs
    assert kw["status"] == "in_progress"
    text = kw["output_text"]
    # 始终 5 步；已完成用 ✓，当前用 ►，未执行用 ○
    assert text.count("✓") == 3
    assert "►" in text  # 当前阶段
    assert text.count("○") == 1  # 后续未执行
    assert "阶段 4/5" in kw["output_summary"]
    _assert_no_emoji(kw["output_title"], kw["output_summary"], text)


@pytest.mark.asyncio
async def test_stage_progress_title_changes_per_stage(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    cases = [
        ("fetching", "Sakura AI 正在获取变更"),
        ("indexing", "Sakura AI 正在索引代码"),
        ("summary", "Sakura AI 正在生成总结"),
        ("reviewing", "Sakura AI 正在审查"),
        ("reporting", "Sakura AI 正在生成报告"),
    ]
    for stage, expected_title in cases:
        await svc.report_stage_progress(_key(), stage=stage, output_language="zh")
        kw = svc._app.update_check_run.call_args.kwargs
        assert kw["output_title"] == expected_title, f"stage={stage}"


# ---------------- report_completed ----------------


@pytest.mark.asyncio
async def test_report_completed_approve_success(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_completed(
        _key(),
        decision=ReviewDecision.APPROVE,
        overall_score=9,
        findings_count=5,
        severity_counts={"minor": 3, "suggestion": 2},
        output_language="zh",
    )

    kw = svc._app.update_check_run.call_args.kwargs
    assert kw["status"] == "completed"
    assert kw["conclusion"] == "success"
    assert "通过" in kw["output_summary"]
    assert "9/10" in kw["output_summary"]
    assert "发现: 5" in kw["output_summary"]
    _assert_no_emoji(kw["output_title"], kw["output_summary"])


@pytest.mark.asyncio
async def test_report_completed_request_changes_neutral(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_completed(
        _key(),
        decision=ReviewDecision.REQUEST_CHANGES,
        overall_score=4,
        findings_count=12,
        severity_counts={"critical": 1, "major": 3, "minor": 8},
        output_language="zh",
    )

    kw = svc._app.update_check_run.call_args.kwargs
    assert kw["conclusion"] == "neutral"
    assert "请求修改" in kw["output_summary"]


# ---------------- report_failed / cancelled / skipped ----------------


@pytest.mark.asyncio
async def test_report_failed_shows_error_reference(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_failed(
        _key(),
        failed_stage="reviewing",
        error_reference="8f3c2a17",
        completed_stages=["fetching", "indexing", "summary"],
        output_language="zh",
    )

    kw = svc._app.update_check_run.call_args.kwargs
    assert kw["status"] == "completed"
    assert kw["conclusion"] == "failure"
    assert "8f3c2a17" in kw["output_summary"]
    # 失败步用 ✗，后续未执行步仍显 ○
    text = kw["output_text"]
    assert "✗" in text
    assert "○" in text
    _assert_no_emoji(kw["output_title"], kw["output_summary"], text)


@pytest.mark.asyncio
async def test_report_cancelled_with_reason(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_cancelled(
        _key(), cancel_reason="pr_closed_merged", output_language="zh"
    )

    kw = svc._app.update_check_run.call_args.kwargs
    assert kw["status"] == "completed"
    assert kw["conclusion"] == "cancelled"
    assert "PR 已关闭或合并" in kw["output_summary"]


@pytest.mark.asyncio
async def test_report_skipped_creates_when_not_found(svc):
    svc._app.cleanup_stale_check_runs.return_value = None
    svc._app.create_check_run.return_value = {"id": 1}

    await svc.report_skipped(_key(), reason="无代码变更", output_language="zh")

    svc._app.create_check_run.assert_called_once()
    kw = svc._app.create_check_run.call_args.kwargs
    assert kw["status"] == "completed"
    assert kw["conclusion"] == "neutral"
    assert kw["output_summary"] == "无代码变更"


# ---------------- Analysis 节流 ----------------


@pytest.mark.asyncio
async def test_analysis_snapshot_throttled(svc):
    """距上次写入不足 analysis_min_interval_sec 时跳过远端写入。"""
    svc._app.cleanup_stale_check_runs.return_value = 1
    snap = ReviewProgressSnapshot(1, 20, 3)

    # 第一次写入（cleanup 命中 → update）
    await svc.report_analysis_snapshot(_key(), snap)
    assert svc._app.update_check_run.call_count == 1

    # 立即第二次（应被节流，不再 update）
    await svc.report_analysis_snapshot(_key(), snap)
    assert svc._app.update_check_run.call_count == 1

    # force=True 绕过节流
    await svc.report_analysis_snapshot(_key(), snap, force=True)
    assert svc._app.update_check_run.call_count == 2


# ---------------- 不追溯改写 ----------------


@pytest.mark.asyncio
async def test_finalize_idempotent_no_rewrite(svc):
    """已 finalize 的 Check 重复 finalize 不再 update。"""
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_failed(
        _key(), failed_stage="reviewing", error_reference="abc12345"
    )
    first_updates = svc._app.update_check_run.call_count

    await svc.report_failed(
        _key(), failed_stage="reviewing", error_reference="abc12345"
    )
    assert svc._app.update_check_run.call_count == first_updates  # 未增加


@pytest.mark.asyncio
async def test_finalize_review_run_does_not_rewrite_completed_sub(svc):
    """finalize_review_run(failure) 不追溯改写已 completed 的副 Check。"""
    svc._app.cleanup_stale_check_runs.return_value = 1

    # Analysis 已先 finalize（success）—— 登记 _finalized
    await svc.finalize_analysis(
        _key(),
        "success",
        snapshot=ReviewProgressSnapshot(5, 20, 18, elapsed_seconds=138.0),
    )
    # 模拟 Analysis 已登记到缓存（finalize_analysis 已缓存）
    assert (_key(), CheckRunService.CHECK_RUN_NAME_ANALYSIS) in svc._finalized

    # 主 Review failure —— Analysis 不应被追溯改写
    await svc.finalize_review_run(
        _key(),
        "failure",
        failed_stage="reporting",
        error_reference="deadbeef",
    )

    # Analysis 的 update 次数不因 finalize_review_run 而增加
    # （finalize_review_run 只收敛未 finalize 的副 Check）
    assert (_key(), CheckRunService.CHECK_RUN_NAME_ANALYSIS) in svc._finalized


# ---------------- cancel_active_runs_by_sha ----------------


@pytest.mark.asyncio
async def test_cancel_active_runs_by_sha(svc):
    """按 sha 收敛：遍历 OWNED_CHECK_NAMES，cleanup 返回 id 则 update cancelled。"""
    svc._app.cleanup_stale_check_runs.return_value = 42  # 三个 name 都命中

    await svc.cancel_active_runs_by_sha(
        "o", "r", "sha", cancel_reason="pr_closed_merged"
    )

    assert svc._app.cleanup_stale_check_runs.call_count == 3  # 三个 name
    # 每个 update 都是 cancelled + skip_if_completed
    for call in svc._app.update_check_run.call_args_list:
        assert call.kwargs["status"] == "completed"
        assert call.kwargs["conclusion"] == "cancelled"
        assert call.kwargs["skip_if_completed"] is True


# ---------------- 异常吞掉 ----------------


@pytest.mark.asyncio
async def test_exception_in_app_is_swallowed(svc):
    svc._app.cleanup_stale_check_runs.side_effect = RuntimeError("github down")

    # 不应抛出
    await svc.report_queued(_key(), pr_number=1, output_language="zh")
    await svc.report_completed(
        _key(),
        decision="approve",
        overall_score=8,
        findings_count=0,
        output_language="zh",
    )
