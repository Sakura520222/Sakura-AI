"""Check Run 服务：将 PR 审查生命周期映射到 GitHub Check Runs。

主从式三 Check：

- ``Sakura AI Review``（主）：流程步骤清单 + 最终 conclusion，全生命周期，唯一建议
  配置为 required status check。
- ``Sakura AI - Analysis``（副）：AI 运行时指标（轮次/工具调用/Token/上下文/模型/耗时），
  仅工具模式下 reviewing 阶段出现。
- ``Sakura AI - Findings``（副）：发现分级统计 + 发布状态，仅有 publishable findings 时出现。

服务内部处理：

- ``ReviewRunKey`` 执行上下文 + ``check_name`` 维度的 Check Run 定位/缓存；
- ``external_id`` 编码（跨进程恢复标识）+ DB 持久化优先恢复；
- 中英双语 output（跟随 ``output_language`` 单语渲染，severity 标签不翻译）；
- Analysis 高频更新节流；
- 不追溯改写已 completed 的副 Check；
- 配置开关（``enable_check_runs`` / ``enable_analysis_check`` / ``enable_findings_check``）；
- 异常吞掉（绝不影响主审查流程）。

所有方法均为 async；output 文本一律纯文本，不使用 emoji。
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from loguru import logger

from backend.core.config import get_settings
from backend.core.github_app import GitHubAppClient


@dataclass(frozen=True)
class ReviewRunKey:
    """单次审查执行的上下文标识。

    Check Run 挂在 commit 上，仅靠 head_sha 无法代表一次具体审查（同 SHA 可能被
    多 PR / 重投 / 重试触发）。本键结合仓库、PR、SHA 与 review_job_id 唯一标识本次
    执行；跨执行的 Check Run 幂等由 head_sha + name + external_id 恢复机制保证
    （见 ``_find_or_create`` 的 cleanup 兜底），不依赖 review_job_id 在重投时是否变化。
    """

    repo_full_name: str
    pr_number: int
    head_sha: str
    review_job_id: str  # = PRReview.id（review_worker review_id）


@dataclass(frozen=True)
class ReviewProgressSnapshot:
    """工具模式审查的运行时快照，驱动 Analysis Check。

    ``tool_call_count`` 是 AI 调用工具次数累计（每轮 ``len(tool_calls)`` 累加），不是
    ``tracker.api_call_count``（后者含压缩重试等 API 请求，两者在压缩重试时会分叉）。
    所有 token/context 字段可选：不可得时填 None，渲染时省略该行，不显示 0/0 或 unknown。
    """

    current_round: int
    max_rounds: int
    tool_call_count: int
    total_input_tokens: Optional[int] = None
    total_output_tokens: Optional[int] = None
    current_context_tokens: Optional[int] = None
    context_limit: Optional[int] = None
    model_name: Optional[str] = None
    elapsed_seconds: Optional[float] = None


# check_kind 常量（用于 external_id 编码）
KIND_REVIEW = "review"
KIND_ANALYSIS = "analysis"
KIND_FINDINGS = "findings"

# severity 标签（枚举值，不进翻译表）
_SEVERITY_ORDER = ("critical", "major", "minor", "suggestion")


class CheckRunService:
    """多 Check Run 报告服务（主 Review / 副 Analysis / 副 Findings）。"""

    # Check Run 名称（Checks 面板固定检查项名）
    CHECK_RUN_NAME_REVIEW = "Sakura AI Review"
    CHECK_RUN_NAME_ANALYSIS = "Sakura AI - Analysis"
    CHECK_RUN_NAME_FINDINGS = "Sakura AI - Findings"
    CHECK_RUN_NAME = CHECK_RUN_NAME_REVIEW  # 向后兼容

    # 自身拥有的全部 Check 名（CI 失败过滤、批量收敛用）
    OWNED_CHECK_NAMES = (
        CHECK_RUN_NAME_REVIEW,
        CHECK_RUN_NAME_ANALYSIS,
        CHECK_RUN_NAME_FINDINGS,
    )

    EXTERNAL_ID_PREFIX = "sakura-ai"
    EXTERNAL_ID_VERSION = "v1"

    # 主 Review 中间阶段（5 个）；stage -> (zh, en)
    _STAGE_DESC: dict[str, tuple[str, str]] = {
        "fetching": ("正在获取变更", "Fetching changes"),
        "indexing": ("正在索引代码", "Indexing code"),
        "summary": ("正在生成总结", "Generating summary"),
        "reviewing": ("AI 审查进行中", "AI review in progress"),
        "reporting": ("正在生成报告", "Generating report"),
    }
    _STAGE_TITLE: dict[str, tuple[str, str]] = {
        "fetching": ("Sakura AI 正在获取变更", "Sakura AI Fetching Changes"),
        "indexing": ("Sakura AI 正在索引代码", "Sakura AI Indexing Code"),
        "summary": ("Sakura AI 正在生成总结", "Sakura AI Generating Summary"),
        "reviewing": ("Sakura AI 正在审查", "Sakura AI Reviewing"),
        "reporting": ("Sakura AI 正在生成报告", "Sakura AI Generating Report"),
    }
    _STAGE_NAME: dict[str, tuple[str, str]] = {
        "fetching": ("获取变更", "Fetch changes"),
        "indexing": ("索引代码", "Index code"),
        "summary": ("生成总结", "Generate summary"),
        "reviewing": ("AI 审查", "AI review"),
        "reporting": ("生成报告", "Generate report"),
    }
    _STAGE_ORDER = ["fetching", "indexing", "summary", "reviewing", "reporting"]

    # 步骤清单符号（► 选用 U+25BA，无 emoji 变体，渲染稳定）
    _SYM_DONE = "✓"
    _SYM_ACTIVE = "►"
    _SYM_PENDING = "○"
    _SYM_FAILED = "✗"

    # 决策中英文本 + conclusion 映射
    _DECISION_TEXT: dict[str, tuple[str, str]] = {
        "approve": ("通过", "Approved"),
        "comment": ("仅评论", "Commented"),
        "request_changes": ("请求修改", "Changes Requested"),
    }
    _DECISION_CONCLUSION: dict[str, str] = {
        "approve": "success",
        "comment": "neutral",
        "request_changes": "neutral",
    }

    # cancel_reason -> (zh, en)；unknown 为通用文案
    _CANCEL_REASON_TEXT: dict[str, tuple[str, str]] = {
        "user_cancelled": ("用户取消", "User cancelled"),
        "superseded": ("被新审查取代", "Superseded by new review"),
        "pr_closed_merged": ("PR 已关闭或合并", "PR closed or merged"),
        "worker_cancelled": ("任务已取消", "Task cancelled"),
        "system_shutdown": ("系统停止", "System shutdown"),
        "unknown": ("审查任务已取消", "Review cancelled"),
    }

    def __init__(self) -> None:
        self._app = GitHubAppClient()
        # (ReviewRunKey, check_name) -> id：中间态复用跳过 GitHub 查询（性能优化，
        # 非唯一事实来源；DB 持久化 + external_id 兜底才是恢复依据）。
        self._check_run_ids: dict[tuple[ReviewRunKey, str], int] = {}
        # 节流：最近一次成功写入的 monotonic 时间戳
        self._last_update_ts: dict[tuple[ReviewRunKey, str], float] = {}
        # 已 finalize 的 Check：(run_key, check_name) -> conclusion
        # 幂等：重复 finalize 跳过；不追溯改写已 completed 的副 Check。
        self._finalized: dict[tuple[ReviewRunKey, str], str] = {}

    # ------------------------------------------------------------------ utils

    @staticmethod
    def _is_english(output_language: Optional[str]) -> bool:
        """判断输出语言是否为英文（与 CommentService._is_english 一致）。"""
        lang = (
            output_language
            if output_language is not None
            else get_settings().output_language
        )
        return lang == "en"

    @staticmethod
    def _decision_key(decision: Any) -> str:
        """归一化 decision 为小写字符串键。"""
        if decision is None:
            return ""
        if hasattr(decision, "value"):
            return str(decision.value)
        return str(decision)

    @staticmethod
    def _split_repo(repo_full_name: str) -> tuple[str, str]:
        """owner/repo 拆分，非法格式返回 ("", "")。"""
        parts = repo_full_name.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return "", ""
        return parts[0], parts[1]

    @classmethod
    def encode_external_id(cls, review_job_id: str, check_kind: str) -> str:
        """编码 external_id：``sakura-ai:v1:{review_job_id}:{check_kind}``。"""
        return (
            f"{cls.EXTERNAL_ID_PREFIX}:{cls.EXTERNAL_ID_VERSION}"
            f":{review_job_id}:{check_kind}"
        )

    def get_cached_check_run_id(
        self, run_key: "ReviewRunKey", check_name: str
    ) -> Optional[int]:
        """读取本执行已登记的 Check Run id（缓存命中才返回，否则 None）。

        供 worker 持久化到 PRReview，跨进程恢复时优先从 DB 读 id。
        """
        return self._check_run_ids.get((run_key, check_name))

    # check_name → PRReview 列名（DB 持久化恢复用）
    _CHECK_NAME_TO_COLUMN: dict[str, str] = {
        CHECK_RUN_NAME_REVIEW: "review_check_run_id",
        CHECK_RUN_NAME_ANALYSIS: "analysis_check_run_id",
        CHECK_RUN_NAME_FINDINGS: "findings_check_run_id",
    }

    @staticmethod
    async def _read_db_check_run_id(
        review_job_id: str, check_name: str
    ) -> Optional[int]:
        """从 PRReview 读取持久化的 check_run_id（跨进程恢复主索引）。

        review_job_id = str(PRReview.id)。失败/无记录返回 None。
        """
        if not review_job_id or not review_job_id.isdigit():
            return None
        col = CheckRunService._CHECK_NAME_TO_COLUMN.get(check_name)
        if not col:
            return None
        try:
            from backend.models import database as _db
            from backend.models.database import PRReview

            async with _db.async_session() as session:
                row = await session.get(PRReview, int(review_job_id))
                if row is None:
                    return None
                return getattr(row, col, None)
        except Exception:
            return None

    def _should_throttle(
        self, run_key: ReviewRunKey, check_name: str, *, force: bool
    ) -> bool:
        """Analysis 快照节流：距上次写入不足 analysis_min_interval_sec 则跳过。"""
        if force:
            return False
        min_interval = get_settings().analysis_min_interval_sec
        if min_interval <= 0:
            return False
        key = (run_key, check_name)
        last = self._last_update_ts.get(key)
        if last is None:
            return False
        return (time.monotonic() - last) < min_interval

    # ------------------------------------------------------------------ locate

    async def _find_or_create(
        self,
        run_key: ReviewRunKey,
        check_name: str,
        *,
        status: Optional[str] = None,
        conclusion: Optional[str] = None,
        output_title: Optional[str] = None,
        output_summary: Optional[str] = None,
        output_text: Optional[str] = None,
        finalize: bool = False,
        check_kind: Optional[str] = None,
        skip_if_completed: bool = False,
    ) -> Optional[int]:
        """按 run_key + check_name 定位 Check Run 并更新；未命中则创建。

        带三级定位：缓存 → cleanup 列举（同 name 最新 active）→ create。
        finalize=True 时登记 ``_finalized``，重复调用跳过（幂等 + 不追溯改写）。
        external_id 在 create 时写入（check_kind 提供时），作跨进程恢复标识。
        head_sha 为空或 repo 非法时返回 None（异常由 GitHubAppClient 吞掉）。
        """
        if not run_key.head_sha:
            logger.debug("CheckRunService: head_sha 为空，跳过 Check Run 操作")
            return None

        repo_owner, repo_name = self._split_repo(run_key.repo_full_name)
        if not repo_owner:
            logger.debug("CheckRunService: repo_full_name 非法 {}", run_key.repo_full_name)
            return None

        cache_key = (run_key, check_name)

        # 已 finalize 的 Check 不追溯改写（幂等）
        if finalize and cache_key in self._finalized:
            logger.debug(
                "CheckRunService: {} 已 finalize (conclusion={})，跳过",
                check_name,
                self._finalized[cache_key],
            )
            return self._check_run_ids.get(cache_key)

        external_id = (
            self.encode_external_id(run_key.review_job_id, check_kind)
            if check_kind
            else None
        )

        update_kwargs = dict(
            status=status,
            conclusion=conclusion,
            output_title=output_title,
            output_summary=output_summary,
            output_text=output_text,
            external_id=external_id,
            skip_if_completed=skip_if_completed,
        )

        def _mark(finalize_flag: bool) -> None:
            self._last_update_ts[cache_key] = time.monotonic()
            if finalize_flag:
                self._finalized[cache_key] = conclusion or "completed"

        # 1. 缓存命中
        cached_id = self._check_run_ids.get(cache_key)
        if cached_id is not None:
            ok = await asyncio.to_thread(
                self._app.update_check_run,
                repo_owner,
                repo_name,
                cached_id,
                **update_kwargs,
            )
            if ok:
                _mark(finalize)
                return cached_id
            self._check_run_ids.pop(cache_key, None)

        # 1b. DB 持久化恢复（run_key.review_job_id → PRReview.check_run_id）
        # 跨进程/换 worker 后缓存丢失时，优先从 DB 读 id（在 external_id 兜底之前）。
        if check_kind:
            db_id = await self._read_db_check_run_id(
                run_key.review_job_id, check_name
            )
            if db_id is not None:
                self._check_run_ids[cache_key] = db_id
                ok = await asyncio.to_thread(
                    self._app.update_check_run,
                    repo_owner,
                    repo_name,
                    db_id,
                    **update_kwargs,
                )
                if ok:
                    _mark(finalize)
                    return db_id
                self._check_run_ids.pop(cache_key, None)

        # 2. cleanup 列举（同 name + external_id 最新 active run，顺带收敛悬挂）。
        # external_id 匹配避免并发/重复 webhook 时误复用其他执行的 Check Run。
        existing_id = await asyncio.to_thread(
            self._app.cleanup_stale_check_runs,
            repo_owner,
            repo_name,
            run_key.head_sha,
            check_name,
            external_id,
        )
        if existing_id is not None:
            ok = await asyncio.to_thread(
                self._app.update_check_run,
                repo_owner,
                repo_name,
                existing_id,
                **update_kwargs,
            )
            if ok:
                self._check_run_ids[cache_key] = existing_id
                _mark(finalize)
                return existing_id

        # 3. 创建
        result = await asyncio.to_thread(
            self._app.create_check_run,
            repo_owner,
            repo_name,
            check_name,
            run_key.head_sha,
            status=status or "queued",
            conclusion=conclusion,
            output_title=output_title,
            output_summary=output_summary,
            output_text=output_text,
            external_id=external_id,
        )
        new_id = result.get("id") if result else None
        if new_id is not None:
            self._check_run_ids[cache_key] = new_id
            _mark(finalize)
        return new_id

    # ------------------------------------------------------------------ 主 Review

    async def report_queued(
        self,
        run_key: ReviewRunKey,
        *,
        pr_number: Any,
        output_language: Optional[str] = None,
    ) -> None:
        """审查已排队（queued）。"""
        if not get_settings().enable_check_runs:
            return
        try:
            is_en = self._is_english(output_language)
            title = "Review Queued" if is_en else "Sakura AI 审查已排队"
            summary = (
                f"PR #{pr_number} queued"
                if is_en
                else f"PR #{pr_number} 已排队，等待处理"
            )
            await self._find_or_create(
                run_key,
                self.CHECK_RUN_NAME_REVIEW,
                status="queued",
                output_title=title,
                output_summary=summary,
                check_kind=KIND_REVIEW,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_queued 失败: {}", exc)

    async def report_stage_progress(
        self,
        run_key: ReviewRunKey,
        *,
        stage: str,
        completed_stages: Optional[Iterable[str]] = None,
        output_language: Optional[str] = None,
    ) -> None:
        """审查进行中（in_progress），更新当前阶段 output + 步骤清单。"""
        if not get_settings().enable_check_runs:
            return
        try:
            is_en = self._is_english(output_language)
            desc_zh, desc_en = self._STAGE_DESC.get(stage, (stage, stage))
            stage_desc = desc_en if is_en else desc_zh
            title_zh, title_en = self._STAGE_TITLE.get(
                stage, ("Sakura AI 正在审查", "Sakura AI Reviewing")
            )
            title = title_en if is_en else title_zh

            done = list(completed_stages or [])
            idx = self._STAGE_ORDER.index(stage) if stage in self._STAGE_ORDER else -1
            progress = idx + 1 if idx >= 0 else 0
            summary = (
                f"Stage {progress}/5 · {stage_desc}"
                if is_en
                else f"阶段 {progress}/5 · {stage_desc}"
            )
            text = self._render_steps(active=stage, failed=None, completed=done, is_en=is_en)

            await self._find_or_create(
                run_key,
                self.CHECK_RUN_NAME_REVIEW,
                status="in_progress",
                output_title=title,
                output_summary=summary,
                output_text=text,
                check_kind=KIND_REVIEW,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_stage_progress 失败: {}", exc)

    async def report_completed(
        self,
        run_key: ReviewRunKey,
        *,
        decision: Any,
        overall_score: Any,
        findings_count: int,
        severity_counts: Optional[dict[str, int]] = None,
        summary_excerpt: str = "",
        output_language: Optional[str] = None,
    ) -> None:
        """审查完成（completed），conclusion 由 decision 映射。"""
        if not get_settings().enable_check_runs:
            return
        try:
            is_en = self._is_english(output_language)
            key = self._decision_key(decision)
            decision_zh, decision_en = self._DECISION_TEXT.get(
                key, (key or "N/A", key or "N/A")
            )
            decision_label = decision_en if is_en else decision_zh
            conclusion = self._DECISION_CONCLUSION.get(key, "neutral")

            score_str = overall_score if overall_score is not None else "N/A"
            title = (
                f"Sakura AI Review Completed · {decision_label}"
                if is_en
                else f"Sakura AI 审查完成 · {decision_label}"
            )
            if is_en:
                summary = (
                    f"Decision: {decision_label} · Score: {score_str}/10 · "
                    f"Findings: {findings_count}"
                )
            else:
                summary = (
                    f"决策: {decision_label} · 评分: {score_str}/10 · "
                    f"发现: {findings_count} 条"
                )

            lines = [
                self._render_steps(
                    active=None,
                    failed=None,
                    completed=self._STAGE_ORDER,
                    is_en=is_en,
                ),
            ]
            if is_en:
                lines.extend(
                    [
                        f"Decision: {decision_label}",
                        f"Score: {score_str}/10",
                        f"Findings: {findings_count}",
                    ]
                )
            else:
                lines.extend(
                    [
                        f"决策: {decision_label}",
                        f"评分: {score_str}/10",
                        f"发现: {findings_count} 条",
                    ]
                )
            sev_line = self._render_severity_inline(severity_counts, is_en)
            if sev_line:
                lines.append(sev_line)
            if summary_excerpt:
                lines.append("")
                lines.append(summary_excerpt)
            text = "\n".join(lines)

            await self._find_or_create(
                run_key,
                self.CHECK_RUN_NAME_REVIEW,
                status="completed",
                conclusion=conclusion,
                output_title=title,
                output_summary=summary,
                output_text=text,
                check_kind=KIND_REVIEW,
                finalize=True,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_completed 失败: {}", exc)

    async def report_failed(
        self,
        run_key: ReviewRunKey,
        *,
        failed_stage: Optional[str] = None,
        error_reference: Optional[str] = None,
        completed_stages: Optional[Iterable[str]] = None,
        output_language: Optional[str] = None,
    ) -> None:
        """审查失败（completed + failure）。错误信息脱敏，仅显故障编号。"""
        if not get_settings().enable_check_runs:
            return
        try:
            is_en = self._is_english(output_language)
            title = "Sakura AI Review Failed" if is_en else "Sakura AI 审查失败"
            stage_desc = self._failed_stage_label(failed_stage, is_en)
            ref_part = f" · Ref {error_reference}" if error_reference else ""
            ref_part_zh = f" · 故障编号 {error_reference}" if error_reference else ""
            summary = (
                f"Failed at: {stage_desc}{ref_part}"
                if is_en
                else f"失败阶段: {stage_desc}{ref_part_zh}"
            )
            text = self._render_steps(
                active=None,
                failed=failed_stage,
                completed=list(completed_stages or []),
                is_en=is_en,
            )
            # 步骤清单后补失败阶段 + 故障编号（关键信息双视图，便于扫读）
            if is_en:
                text += f"\n\nFailed at: {stage_desc}"
                if error_reference:
                    text += f"\nRef: {error_reference}"
            else:
                text += f"\n\n失败阶段: {stage_desc}"
                if error_reference:
                    text += f"\n故障编号: {error_reference}"

            await self._find_or_create(
                run_key,
                self.CHECK_RUN_NAME_REVIEW,
                status="completed",
                conclusion="failure",
                output_title=title,
                output_summary=summary,
                output_text=text,
                check_kind=KIND_REVIEW,
                finalize=True,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_failed 失败: {}", exc)

    async def report_cancelled(
        self,
        run_key: ReviewRunKey,
        *,
        cancel_reason: str = "unknown",
        completed_stages: Optional[Iterable[str]] = None,
        output_language: Optional[str] = None,
    ) -> None:
        """审查取消（completed + cancelled）。结构化 reason，三 Check 复用。"""
        if not get_settings().enable_check_runs:
            return
        try:
            is_en = self._is_english(output_language)
            title = "Sakura AI Review Cancelled" if is_en else "Sakura AI 审查已取消"
            reason_zh, reason_en = self._CANCEL_REASON_TEXT.get(
                cancel_reason, self._CANCEL_REASON_TEXT["unknown"]
            )
            if is_en:
                summary = f"Review cancelled · {reason_en}"
            else:
                summary = f"审查任务已取消 · {reason_zh}"
            text = self._render_steps(
                active=None,
                failed=None,
                completed=list(completed_stages or []),
                is_en=is_en,
                all_pending_if_empty=True,
            )

            await self._find_or_create(
                run_key,
                self.CHECK_RUN_NAME_REVIEW,
                status="completed",
                conclusion="cancelled",
                output_title=title,
                output_summary=summary,
                output_text=text,
                check_kind=KIND_REVIEW,
                finalize=True,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_cancelled 失败: {}", exc)

    async def report_skipped(
        self,
        run_key: ReviewRunKey,
        *,
        reason: str,
        output_language: Optional[str] = None,
    ) -> None:
        """审查跳过（completed + neutral）。reason 直接使用原值（中英一致）。"""
        if not get_settings().enable_check_runs:
            return
        try:
            is_en = self._is_english(output_language)
            title = "Review Skipped" if is_en else "Sakura AI 审查已跳过"
            summary = reason if reason else ("Skipped" if is_en else "已跳过")
            await self._find_or_create(
                run_key,
                self.CHECK_RUN_NAME_REVIEW,
                status="completed",
                conclusion="neutral",
                output_title=title,
                output_summary=summary,
                check_kind=KIND_REVIEW,
                finalize=True,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_skipped 失败: {}", exc)

    # ------------------------------------------------------------------ 向后兼容
    # 旧签名基于 owner/repo/head_sha（无 review_job_id）；迁移到 ReviewRunKey 后移除。
    # 降级 run_key 不享受副 Check / external_id 关联，仅保证主 Review 基本功能不断。
    # 新代码应直接调 report_stage_progress(run_key, ...)。

    async def report_progress(
        self,
        repo_owner: str,
        repo_name: str,
        head_sha: str,
        *,
        stage: str,
        completed_stages: Optional[Iterable[str]] = None,
        output_language: Optional[str] = None,
    ) -> None:
        """旧签名兼容（head_sha 单 Check）。迁移到 report_stage_progress(run_key) 后移除。"""
        run_key = ReviewRunKey(
            repo_full_name=f"{repo_owner}/{repo_name}",
            pr_number=0,
            head_sha=head_sha or "",
            review_job_id="legacy",
        )
        await self.report_stage_progress(
            run_key,
            stage=stage,
            completed_stages=completed_stages,
            output_language=output_language,
        )

    # ------------------------------------------------------------------ 副 Analysis

    async def report_analysis_snapshot(
        self,
        run_key: ReviewRunKey,
        snapshot: ReviewProgressSnapshot,
        *,
        force: bool = False,
        output_language: Optional[str] = None,
    ) -> None:
        """更新 Analysis Check（in_progress，带节流）。force=True 跳过节流。"""
        if not get_settings().enable_check_runs:
            return
        if not get_settings().enable_analysis_check:
            return
        if self._should_throttle(run_key, self.CHECK_RUN_NAME_ANALYSIS, force=force):
            return
        try:
            is_en = self._is_english(output_language)
            title = (
                "Sakura AI Tool Analysis In Progress"
                if is_en
                else "Sakura AI 工具分析中"
            )
            ctx_pct = self._context_pct(snapshot)
            summary = (
                f"Round {snapshot.current_round}/{snapshot.max_rounds} · "
                f"{snapshot.tool_call_count} tool calls"
                + (f" · {ctx_pct}% context" if ctx_pct is not None else "")
            )
            text = self._render_analysis(snapshot, is_en, final=False)

            await self._find_or_create(
                run_key,
                self.CHECK_RUN_NAME_ANALYSIS,
                status="in_progress",
                output_title=title,
                output_summary=summary,
                output_text=text,
                check_kind=KIND_ANALYSIS,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_analysis_snapshot 失败: {}", exc)

    async def finalize_analysis(
        self,
        run_key: ReviewRunKey,
        conclusion: str,
        *,
        snapshot: Optional[ReviewProgressSnapshot] = None,
        cancel_reason: str = "unknown",
        error_reference: Optional[str] = None,
        output_language: Optional[str] = None,
    ) -> None:
        """定格 Analysis Check（success/failure/cancelled）。force 刷新不受节流。"""
        if not get_settings().enable_check_runs:
            return
        if not get_settings().enable_analysis_check:
            return
        try:
            is_en = self._is_english(output_language)
            if conclusion == "success":
                title = (
                    "Sakura AI Tool Analysis Completed"
                    if is_en
                    else "Sakura AI 工具分析完成"
                )
            elif conclusion == "cancelled":
                title = (
                    "Sakura AI Tool Analysis Cancelled"
                    if is_en
                    else "Sakura AI 工具分析已取消"
                )
            else:
                title = (
                    "Sakura AI Tool Analysis Failed"
                    if is_en
                    else "Sakura AI 工具分析失败"
                )
            summary = self._analysis_summary(
                snapshot, conclusion, cancel_reason, error_reference, is_en
            )
            text = self._render_analysis(
                snapshot or ReviewProgressSnapshot(0, 0, 0),
                is_en,
                final=True,
                conclusion=conclusion,
                cancel_reason=cancel_reason,
                error_reference=error_reference,
            )

            await self._find_or_create(
                run_key,
                self.CHECK_RUN_NAME_ANALYSIS,
                status="completed",
                conclusion=conclusion,
                output_title=title,
                output_summary=summary,
                output_text=text,
                check_kind=KIND_ANALYSIS,
                finalize=True,
            )
        except Exception as exc:
            logger.debug("CheckRunService.finalize_analysis 失败: {}", exc)

    # ------------------------------------------------------------------ 副 Findings

    async def report_findings_snapshot(
        self,
        run_key: ReviewRunKey,
        *,
        severity_counts: dict[str, int],
        files_count: int,
        total_count: int,
        published_count: int,
        failed_count: int,
        output_language: Optional[str] = None,
    ) -> None:
        """更新 Findings Check（in_progress）。创建后发布完成前调。"""
        if not get_settings().enable_check_runs:
            return
        if not get_settings().enable_findings_check:
            return
        try:
            is_en = self._is_english(output_language)
            title = (
                "Sakura AI Findings Summary"
                if is_en
                else "Sakura AI 发现统计"
            )
            summary = self._findings_summary(
                severity_counts, total_count, is_en
            )
            text = self._render_findings(
                severity_counts=severity_counts,
                files_count=files_count,
                total_count=total_count,
                published_count=published_count,
                failed_count=failed_count,
                conclusion=None,
                is_en=is_en,
            )

            await self._find_or_create(
                run_key,
                self.CHECK_RUN_NAME_FINDINGS,
                status="in_progress",
                output_title=title,
                output_summary=summary,
                output_text=text,
                check_kind=KIND_FINDINGS,
            )
        except Exception as exc:
            logger.debug("CheckRunService.report_findings_snapshot 失败: {}", exc)

    async def finalize_findings(
        self,
        run_key: ReviewRunKey,
        conclusion: str,
        *,
        severity_counts: Optional[dict[str, int]] = None,
        files_count: int = 0,
        total_count: int = 0,
        published_count: int = 0,
        failed_count: int = 0,
        cancel_reason: str = "unknown",
        error_reference: Optional[str] = None,
        output_language: Optional[str] = None,
    ) -> None:
        """定格 Findings Check（neutral/failure/cancelled）。"""
        if not get_settings().enable_check_runs:
            return
        if not get_settings().enable_findings_check:
            return
        severity_counts = severity_counts or {}
        try:
            is_en = self._is_english(output_language)
            if conclusion == "neutral":
                title = (
                    "Sakura AI Findings Summary"
                    if is_en
                    else "Sakura AI 发现统计"
                )
            elif conclusion == "cancelled":
                title = (
                    "Sakura AI Findings Cancelled"
                    if is_en
                    else "Sakura AI 发现已取消"
                )
            else:
                title = (
                    "Sakura AI Findings Failed to Publish"
                    if is_en
                    else "Sakura AI 发现发布失败"
                )
            summary = self._findings_summary_final(
                severity_counts, total_count, published_count,
                failed_count, conclusion, cancel_reason, is_en,
            )
            text = self._render_findings(
                severity_counts=severity_counts,
                files_count=files_count,
                total_count=total_count,
                published_count=published_count,
                failed_count=failed_count,
                conclusion=conclusion,
                cancel_reason=cancel_reason,
                error_reference=error_reference,
                is_en=is_en,
            )

            await self._find_or_create(
                run_key,
                self.CHECK_RUN_NAME_FINDINGS,
                status="completed",
                conclusion=conclusion,
                output_title=title,
                output_summary=summary,
                output_text=text,
                check_kind=KIND_FINDINGS,
                finalize=True,
            )
        except Exception as exc:
            logger.debug("CheckRunService.finalize_findings 失败: {}", exc)

    # ------------------------------------------------------------------ 批量收敛

    async def finalize_review_run(
        self,
        run_key: ReviewRunKey,
        conclusion: str,
        *,
        failed_stage: Optional[str] = None,
        cancel_reason: str = "unknown",
        error_reference: Optional[str] = None,
        completed_stages: Optional[Iterable[str]] = None,
        output_language: Optional[str] = None,
    ) -> None:
        """主 Review 终态 + 同步收敛本次已登记且仍非 completed 的副 Check。

        conclusion 映射：
        - failure → 主 failure + Analysis/Findings 按自身状态（未 finalized 才收敛）
        - cancelled → 主 cancelled + 副 cancelled（保留最后快照，不强制刷新）
        - success/neutral → 直接走 report_completed / report_skipped，不调本方法。
        error_reference 由本次失败统一传入，主/副 Check 共用同一编号。
        """
        if not get_settings().enable_check_runs:
            return
        if conclusion == "failure":
            await self.report_failed(
                run_key,
                failed_stage=failed_stage,
                error_reference=error_reference,
                completed_stages=completed_stages,
                output_language=output_language,
            )
        elif conclusion == "cancelled":
            await self.report_cancelled(
                run_key,
                cancel_reason=cancel_reason,
                completed_stages=completed_stages,
                output_language=output_language,
            )
        else:
            logger.warning("finalize_review_run 仅用于 failure/cancelled，收到 {}", conclusion)
            return

        # 收敛副 Check：只处理本次已登记（缓存命中过）且未 finalize 的
        for check_name in (self.CHECK_RUN_NAME_ANALYSIS, self.CHECK_RUN_NAME_FINDINGS):
            cache_key = (run_key, check_name)
            if cache_key not in self._check_run_ids:
                continue  # 未创建过，不补建
            if cache_key in self._finalized:
                continue  # 已 finalize，不追溯改写
            sub_conclusion = "cancelled" if conclusion == "cancelled" else "failure"
            if check_name == self.CHECK_RUN_NAME_ANALYSIS:
                await self.finalize_analysis(
                    run_key,
                    sub_conclusion,
                    cancel_reason=cancel_reason,
                    error_reference=error_reference,
                    output_language=output_language,
                )
            else:
                await self.finalize_findings(
                    run_key,
                    sub_conclusion,
                    cancel_reason=cancel_reason,
                    error_reference=error_reference,
                    output_language=output_language,
                )

    async def cancel_active_runs_by_sha(
        self,
        repo_owner: str,
        repo_name: str,
        head_sha: str,
        *,
        cancel_reason: str = "unknown",
        output_language: Optional[str] = None,
    ) -> None:
        """兜底：按 head_sha 把本 App 所有 active Check Run 标 cancelled。

        用于 webhook PR-closed 等无 ReviewRunKey 的场景；不影响已 completed 的 Check。
        """
        if not get_settings().enable_check_runs:
            return
        if not head_sha:
            return
        try:
            is_en = self._is_english(output_language)
            reason_zh, reason_en = self._CANCEL_REASON_TEXT.get(
                cancel_reason, self._CANCEL_REASON_TEXT["unknown"]
            )
            title = "Sakura AI Review Cancelled" if is_en else "Sakura AI 审查已取消"
            summary = (
                f"Review cancelled · {reason_en}"
                if is_en
                else f"审查任务已取消 · {reason_zh}"
            )
            for name in self.OWNED_CHECK_NAMES:
                existing_id = await asyncio.to_thread(
                    self._app.cleanup_stale_check_runs,
                    repo_owner,
                    repo_name,
                    head_sha,
                    name,
                )
                if existing_id is None:
                    continue
                await asyncio.to_thread(
                    self._app.update_check_run,
                    repo_owner,
                    repo_name,
                    existing_id,
                    status="completed",
                    conclusion="cancelled",
                    output_title=title,
                    output_summary=summary,
                    skip_if_completed=True,
                )
        except Exception as exc:
            logger.debug("cancel_active_runs_by_sha 失败: {}", exc)

    # ------------------------------------------------------------------ 渲染 helper

    def _render_steps(
        self,
        *,
        active: Optional[str],
        failed: Optional[str],
        completed: list[str],
        is_en: bool,
        all_pending_if_empty: bool = False,
    ) -> str:
        """渲染主 Review 的 5 步清单。始终完整 5 步。"""
        completed_set = set(completed)
        lines: list[str] = []
        any_marked = False
        for i, stage in enumerate(self._STAGE_ORDER, 1):
            name_zh, name_en = self._STAGE_NAME.get(stage, (stage, stage))
            label = name_en if is_en else name_zh
            if stage == failed:
                sym = self._SYM_FAILED
                any_marked = True
            elif stage == active:
                sym = self._SYM_ACTIVE
                any_marked = True
            elif stage in completed_set:
                sym = self._SYM_DONE
                any_marked = True
            else:
                sym = self._SYM_PENDING
            lines.append(f"{sym} {i}. {label}")
        if not any_marked and all_pending_if_empty:
            # cancelled 且无已完成阶段：保留全 ○ 清单（至少有 text 内容）
            pass
        return "\n".join(lines)

    @staticmethod
    def _context_pct(snapshot: ReviewProgressSnapshot) -> Optional[int]:
        if (
            snapshot.current_context_tokens is None
            or not snapshot.context_limit
        ):
            return None
        return round(snapshot.current_context_tokens / snapshot.context_limit * 100)

    def _render_severity_inline(
        self, severity_counts: Optional[dict[str, int]], is_en: bool
    ) -> Optional[str]:
        """主 Review text 的分级行：只列非零级，中英都用英文标签。"""
        if not severity_counts:
            return None
        parts = [
            f"{sev} {severity_counts[sev]}"
            for sev in _SEVERITY_ORDER
            if severity_counts.get(sev)
        ]
        if not parts:
            return None
        prefix = "Findings breakdown" if is_en else "发现明细"
        return f"{prefix}: " + " · ".join(parts)

    def _failed_stage_label(self, failed_stage: Optional[str], is_en: bool) -> str:
        if not failed_stage:
            return "Unknown" if is_en else "未知"
        zh, en = self._STAGE_NAME.get(failed_stage, (failed_stage, failed_stage))
        return en if is_en else zh

    def _render_analysis(
        self,
        snapshot: ReviewProgressSnapshot,
        is_en: bool,
        *,
        final: bool,
        conclusion: Optional[str] = None,
        cancel_reason: str = "unknown",
        error_reference: Optional[str] = None,
    ) -> str:
        """渲染 Analysis text（指标行，不可用字段省略）。"""
        lines: list[str] = []
        if final:
            if conclusion == "cancelled":
                round_label = (
                    f"Rounds: {snapshot.current_round} (max {snapshot.max_rounds})"
                    if is_en
                    else f"实际轮次: {snapshot.current_round}（上限 {snapshot.max_rounds}）"
                )
            else:
                round_label = (
                    f"Rounds: {snapshot.current_round} (max {snapshot.max_rounds})"
                    if is_en
                    else f"实际轮次: {snapshot.current_round}（上限 {snapshot.max_rounds}）"
                )
        else:
            round_label = (
                f"Current round: {snapshot.current_round} (max {snapshot.max_rounds})"
                if is_en
                else f"当前轮次: {snapshot.current_round}（上限 {snapshot.max_rounds}）"
            )
        lines.append(round_label)

        tc_label = "Tool calls" if is_en else "工具调用"
        lines.append(f"{tc_label}: {snapshot.tool_call_count}")

        if snapshot.model_name:
            model_label = "Model" if is_en else "模型"
            lines.append(f"{model_label}: {snapshot.model_name}")

        if snapshot.total_input_tokens is not None or snapshot.total_output_tokens is not None:
            tok_label = "Total tokens" if is_en else "累计 Token"
            inp = snapshot.total_input_tokens or 0
            out = snapshot.total_output_tokens or 0
            if is_en:
                lines.append(f"{tok_label}: {inp:,} input / {out:,} output")
            else:
                lines.append(f"{tok_label}: 输入 {inp:,} / 输出 {out:,}")

        # final 显示峰值，运行中显示当前
        if final:
            if snapshot.current_context_tokens is not None and snapshot.context_limit:
                ctx_label = "Peak context" if is_en else "上下文峰值"
                pct = round(snapshot.current_context_tokens / snapshot.context_limit * 100)
                if is_en:
                    lines.append(f"{ctx_label}: {snapshot.current_context_tokens:,} / {snapshot.context_limit:,} ({pct}%)")
                else:
                    lines.append(f"{ctx_label}: {snapshot.current_context_tokens:,} / {snapshot.context_limit:,}（{pct}%）")
            if snapshot.elapsed_seconds is not None:
                lines.append(
                    f"Duration: {self._format_duration(snapshot.elapsed_seconds, is_en)}"
                )
        else:
            if snapshot.current_context_tokens is not None and snapshot.context_limit:
                ctx_label = "Current context" if is_en else "当前上下文"
                pct = round(snapshot.current_context_tokens / snapshot.context_limit * 100)
                if is_en:
                    lines.append(f"{ctx_label}: {snapshot.current_context_tokens:,} / {snapshot.context_limit:,} ({pct}%)")
                else:
                    lines.append(f"{ctx_label}: {snapshot.current_context_tokens:,} / {snapshot.context_limit:,}（{pct}%）")

        if conclusion == "failure":
            tail = "See main Review for error details" if is_en else "详见主 Review 获取错误详情"
            lines.append(tail)
        elif conclusion == "cancelled":
            round_tail = (
                f"Cancelled at round {snapshot.current_round}"
                if is_en
                else f"取消时进度: 第 {snapshot.current_round} 轮"
            )
            lines.append(round_tail)

        return "\n".join(lines)

    def _analysis_summary(
        self,
        snapshot: Optional[ReviewProgressSnapshot],
        conclusion: str,
        cancel_reason: str,
        error_reference: Optional[str],
        is_en: bool,
    ) -> str:
        ref_part = f" · Ref {error_reference}" if error_reference else ""
        ref_part_zh = f" · 故障编号 {error_reference}" if error_reference else ""
        if conclusion == "success":
            if snapshot:
                if is_en:
                    return f"{snapshot.current_round} rounds · {snapshot.tool_call_count} tool calls"
                return f"{snapshot.current_round} 轮 · 工具调用 {snapshot.tool_call_count} 次"
            return "Completed" if is_en else "已完成"
        if conclusion == "cancelled":
            if snapshot:
                if is_en:
                    return f"{snapshot.current_round} rounds done · {snapshot.tool_call_count} tool calls"
                return f"已执行 {snapshot.current_round} 轮 · 工具调用 {snapshot.tool_call_count} 次"
            reason = self._CANCEL_REASON_TEXT.get(cancel_reason, self._CANCEL_REASON_TEXT["unknown"])[1 if is_en else 0]
            return reason
        # failure
        if snapshot:
            base = (
                f"Failed at round {snapshot.current_round}"
                if is_en
                else f"第 {snapshot.current_round} 轮出错"
            )
        else:
            base = "Failed" if is_en else "出错"
        return base + (ref_part if is_en else ref_part_zh)

    def _findings_summary(
        self, severity_counts: dict[str, int], total_count: int, is_en: bool
    ) -> str:
        parts = [f"{sev} {severity_counts.get(sev, 0)}" for sev in _SEVERITY_ORDER]
        if is_en:
            return f"{total_count} findings · " + " · ".join(parts)
        return f"{total_count} 条发现 · " + " · ".join(parts)

    def _findings_summary_final(
        self,
        severity_counts: dict[str, int],
        total_count: int,
        published_count: int,
        failed_count: int,
        conclusion: str,
        cancel_reason: str,
        is_en: bool,
    ) -> str:
        if conclusion == "neutral":
            return self._findings_summary(severity_counts, total_count, is_en)
        if conclusion == "cancelled":
            reason = self._CANCEL_REASON_TEXT.get(cancel_reason, self._CANCEL_REASON_TEXT["unknown"])[1 if is_en else 0]
            pending = max(total_count - published_count, 0)
            if is_en:
                return f"Publish cancelled, {pending} pending · {reason}"
            return f"发布已取消，{pending} 条待发布 · {reason}"
        # failure
        if failed_count >= total_count and total_count > 0:
            base = (
                f"All {total_count} findings failed to publish"
                if is_en
                else f"{total_count} 条发现均未能发布"
            )
        else:
            base = (
                f"{published_count} of {total_count} published · {failed_count} failed"
                if is_en
                else f"{total_count} 条中已发布 {published_count} 条 · {failed_count} 条失败"
            )
        tail = "See main Review" if is_en else "详见主 Review"
        return f"{base} · {tail}"

    def _render_findings(
        self,
        *,
        severity_counts: dict[str, int],
        files_count: int,
        total_count: int,
        published_count: int,
        failed_count: int,
        conclusion: Optional[str],
        cancel_reason: str = "unknown",
        error_reference: Optional[str] = None,
        is_en: bool,
    ) -> str:
        """渲染 Findings text。分级列全四级含 0；发布失败附发布状态。"""
        lines: list[str] = []
        if conclusion == "failure":
            if is_en:
                lines.append("Publishing status:")
                lines.append(f"- Total: {total_count}")
                lines.append(f"- Published: {published_count}")
                lines.append(f"- Failed: {failed_count}")
                lines.append("")
                lines.append("Severity totals:")
            else:
                lines.append("发布状态:")
                lines.append(f"- 总计: {total_count}")
                lines.append(f"- 已发布: {published_count}")
                lines.append(f"- 失败: {failed_count}")
                lines.append("")
                lines.append("全部发现分级:")
        else:
            if total_count > 0:
                if is_en:
                    lines.append(f"{total_count} findings across {files_count} files")
                else:
                    lines.append(f"共 {total_count} 条发现，涉及 {files_count} 个文件")
                lines.append("")

        for sev in _SEVERITY_ORDER:
            lines.append(f"{sev}: {severity_counts.get(sev, 0)}")

        if conclusion == "failure":
            tail = "See main Review for error details" if is_en else "详见主 Review 获取错误详情"
            lines.append("")
            lines.append(tail)
        elif conclusion == "cancelled":
            pending = max(total_count - published_count, 0)
            if is_en:
                lines.append("")
                lines.append(f"Publish cancelled, {pending} pending")
            else:
                lines.append("")
                lines.append(f"发布已取消，{pending} 条待发布")

        return "\n".join(lines)

    @staticmethod
    def _format_duration(seconds: float, is_en: bool) -> str:
        """秒 → '2 分 18 秒' / '2m 18s'。"""
        total = int(seconds)
        minutes, secs = divmod(total, 60)
        if is_en:
            return f"{minutes}m {secs}s"
        return f"{minutes} 分 {secs} 秒"
