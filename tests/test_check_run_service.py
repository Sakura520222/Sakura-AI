"""CheckRunService 单元测试。

覆盖：decision→conclusion 映射、中英 output、find_or_create 幂等、
异常吞掉、enable_check_runs 开关、output 无 emoji。
"""

import re
from unittest.mock import MagicMock

import pytest

from backend.models.database import ReviewDecision
from backend.services.check_run_service import CheckRunService


# 宽松的 emoji 检测：覆盖常见 emoji Unicode 区间 + 项目评论用过的符号
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"  # symbols & pictographs & emoji
    "\U00002600-\U000027bf"  # misc symbols
    "\U0001f600-\U0001f64f"  # emoticons
    "✅❌⚠⏳✨"  # ✅ ❌ ⚠ ⏳ ✨
    "]",
    flags=re.UNICODE,
)


def _assert_no_emoji(*texts: str) -> None:
    for t in texts:
        if not t:
            continue
        m = _EMOJI_RE.search(t)
        assert not m, f"发现 emoji {m.group()!r} in {t!r}"


@pytest.fixture()
def svc(monkeypatch):
    """启用 check_runs + 默认中文的 CheckRunService（_app 已 mock）。"""
    monkeypatch.setattr(
        "backend.services.check_run_service.get_settings",
        lambda: MagicMock(enable_check_runs=True, output_language="zh"),
    )
    service = CheckRunService()
    service._app = MagicMock()
    return service


# ---------------- 开关 ----------------


@pytest.mark.asyncio
async def test_disabled_returns_early(monkeypatch):
    monkeypatch.setattr(
        "backend.services.check_run_service.get_settings",
        lambda: MagicMock(enable_check_runs=False, output_language="zh"),
    )
    service = CheckRunService()
    service._app = MagicMock()

    await service.report_queued("o", "r", "sha", pr_number=1)

    service._app.cleanup_stale_check_runs.assert_not_called()
    service._app.create_check_run.assert_not_called()


# ---------------- report_queued ----------------


@pytest.mark.asyncio
async def test_report_queued_creates_when_not_found_zh(svc):
    svc._app.cleanup_stale_check_runs.return_value = None
    svc._app.create_check_run.return_value = {"id": 1}

    await svc.report_queued("o", "r", "sha", pr_number=5, output_language="zh")

    svc._app.create_check_run.assert_called_once()
    svc._app.update_check_run.assert_not_called()
    call = svc._app.create_check_run.call_args
    assert call.args == ("o", "r", "Sakura AI Review", "sha")
    assert call.kwargs["status"] == "queued"
    assert call.kwargs["output_title"] == "Sakura AI 审查已排队"
    assert "PR #5 已排队" in call.kwargs["output_summary"]
    _assert_no_emoji(
        call.kwargs["output_title"],
        call.kwargs["output_summary"],
        call.kwargs.get("output_text"),
    )


@pytest.mark.asyncio
async def test_report_queued_updates_when_found(svc):
    svc._app.cleanup_stale_check_runs.return_value = 99

    await svc.report_queued("o", "r", "sha", pr_number=1, output_language="zh")

    svc._app.update_check_run.assert_called_once()
    svc._app.create_check_run.assert_not_called()
    assert svc._app.update_check_run.call_args.kwargs["status"] == "queued"


@pytest.mark.asyncio
async def test_report_queued_english(svc):
    svc._app.cleanup_stale_check_runs.return_value = None
    svc._app.create_check_run.return_value = {"id": 1}

    await svc.report_queued("o", "r", "sha", pr_number=5, output_language="en")

    kw = svc._app.create_check_run.call_args.kwargs
    assert kw["output_title"] == "Review Queued"
    assert "PR #5 queued" in kw["output_summary"]


# ---------------- report_progress ----------------


@pytest.mark.asyncio
async def test_report_progress_text_includes_completed_stages(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_progress(
        "o",
        "r",
        "sha",
        stage="reviewing",
        completed_stages=["indexing", "summary"],
        output_language="zh",
    )

    kw = svc._app.update_check_run.call_args.kwargs
    assert kw["status"] == "in_progress"
    text = kw["output_text"]
    assert "当前阶段: AI 审查进行中" in text
    assert "已完成: 代码索引、PR 总结" in text
    _assert_no_emoji(kw["output_title"], kw["output_summary"], text)


@pytest.mark.asyncio
async def test_report_progress_english_completed(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_progress(
        "o",
        "r",
        "sha",
        stage="reporting",
        completed_stages=["reviewing"],
        output_language="en",
    )

    kw = svc._app.update_check_run.call_args.kwargs
    text = kw["output_text"]
    assert "Current stage: Generating report" in text
    assert "Completed: AI review" in text


# ---------------- report_completed（decision→conclusion） ----------------


@pytest.mark.asyncio
async def test_report_completed_approve_success(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_completed(
        "o",
        "r",
        "sha",
        decision=ReviewDecision.APPROVE,
        overall_score=9,
        comment_count=2,
        output_language="zh",
    )

    kw = svc._app.update_check_run.call_args.kwargs
    assert kw["status"] == "completed"
    assert kw["conclusion"] == "success"
    assert "通过" in kw["output_summary"]
    assert "9/10" in kw["output_summary"]
    _assert_no_emoji(kw["output_title"], kw["output_summary"])


@pytest.mark.asyncio
async def test_report_completed_request_changes_neutral(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_completed(
        "o",
        "r",
        "sha",
        decision=ReviewDecision.REQUEST_CHANGES,
        overall_score=4,
        comment_count=6,
        output_language="zh",
    )

    kw = svc._app.update_check_run.call_args.kwargs
    assert kw["conclusion"] == "neutral"
    assert "建议修改" in kw["output_summary"]


@pytest.mark.asyncio
async def test_report_completed_comment_neutral(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_completed(
        "o",
        "r",
        "sha",
        decision="comment",
        overall_score=None,
        comment_count=0,
        output_language="en",
    )

    kw = svc._app.update_check_run.call_args.kwargs
    assert kw["conclusion"] == "neutral"
    assert "N/A" in kw["output_summary"]
    assert "Comment" in kw["output_summary"]


@pytest.mark.asyncio
async def test_report_completed_includes_summary_excerpt(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_completed(
        "o",
        "r",
        "sha",
        decision="approve",
        overall_score=8,
        comment_count=1,
        summary_excerpt="代码质量良好，建议合并。",
        output_language="zh",
    )

    kw = svc._app.update_check_run.call_args.kwargs
    assert kw["output_text"] == "代码质量良好，建议合并。"


# ---------------- report_failed / cancelled / skipped ----------------


@pytest.mark.asyncio
async def test_report_failed_uses_failure_conclusion(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_failed(
        "o", "r", "sha", error_message="ConnectionError(...)", output_language="zh"
    )

    kw = svc._app.update_check_run.call_args.kwargs
    assert kw["status"] == "completed"
    assert kw["conclusion"] == "failure"
    # 错误原文不直接写入 output（脱敏）
    assert "ConnectionError" not in (kw["output_summary"] or "")
    _assert_no_emoji(kw["output_title"], kw["output_summary"])


@pytest.mark.asyncio
async def test_report_cancelled(svc):
    svc._app.cleanup_stale_check_runs.return_value = 1

    await svc.report_cancelled("o", "r", "sha", output_language="zh")

    kw = svc._app.update_check_run.call_args.kwargs
    assert kw["status"] == "completed"
    assert kw["conclusion"] == "cancelled"


@pytest.mark.asyncio
async def test_report_skipped_creates_when_not_found(svc):
    # should_skip 发生在 report_queued 之前，check run 可能不存在
    svc._app.cleanup_stale_check_runs.return_value = None
    svc._app.create_check_run.return_value = {"id": 1}

    await svc.report_skipped("o", "r", "sha", reason="无代码变更", output_language="zh")

    svc._app.create_check_run.assert_called_once()
    kw = svc._app.create_check_run.call_args.kwargs
    assert kw["status"] == "completed"
    assert kw["conclusion"] == "neutral"
    assert kw["output_summary"] == "无代码变更"


# ---------------- 异常吞掉 ----------------


@pytest.mark.asyncio
async def test_exception_in_app_is_swallowed(svc):
    svc._app.cleanup_stale_check_runs.side_effect = RuntimeError("github down")

    # 不应抛出
    await svc.report_queued("o", "r", "sha", pr_number=1, output_language="zh")
    await svc.report_completed(
        "o",
        "r",
        "sha",
        decision="approve",
        overall_score=8,
        comment_count=1,
        output_language="zh",
    )


# ---------------- 端到端幂等回归（日志 #403 实测 bug） ----------------


@pytest.mark.asyncio
async def test_full_lifecycle_creates_once_then_updates(svc):
    """模拟完整审查生命周期，验证 find 命中后只 create 一次、其余全 update。

    回归日志 pr_log.log 暴露的 bug：find_check_run_for_sha 因 API 错误永远
    miss，导致一次审查创建 6 个 Check Run。修复后 find 命中应走 update。
    """
    # 首次 find miss（queued 创建），此后 find 命中（返回已创建的 id）
    svc._app.cleanup_stale_check_runs.side_effect = [None, 100, 100, 100, 100]
    svc._app.create_check_run.return_value = {"id": 100}

    await svc.report_queued("o", "r", "sha", pr_number=1, output_language="zh")
    await svc.report_progress("o", "r", "sha", stage="indexing", output_language="zh")
    await svc.report_progress("o", "r", "sha", stage="reviewing", output_language="zh")
    await svc.report_progress("o", "r", "sha", stage="reporting", output_language="zh")
    await svc.report_completed(
        "o",
        "r",
        "sha",
        decision=ReviewDecision.APPROVE,
        overall_score=9,
        comment_count=0,
        output_language="zh",
    )

    # 期望：只 create 一次（queued），其余 4 次全部 update，绝不重复 create
    assert svc._app.create_check_run.call_count == 1
    assert svc._app.update_check_run.call_count == 4
    # 所有 update 都指向同一个 check run id（existing_id 是位置参数 args[2]）
    for call in svc._app.update_check_run.call_args_list:
        assert call.args[2] == 100
