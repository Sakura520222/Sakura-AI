"""仓库扫描 Worker"""

import asyncio
import json as _json
import os
import shutil
import tempfile
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError

from backend.core.config import (
    get_dynamic_config,
    get_settings,
    get_user_dynamic_config,
)
from backend.core.time_service import now_utc
from backend.models.scan_models import RepoScan, ScanFinding, ScanStatus
from backend.services.ai_reviewer.token_tracker import TokenTracker
from backend.services.ai_task_deadline import AITaskDeadline

# 扫描并发控制信号量
_scan_semaphore: asyncio.Semaphore | None = None


async def _get_scan_semaphore() -> asyncio.Semaphore:
    """获取扫描并发信号量（懒初始化）"""
    global _scan_semaphore
    if _scan_semaphore is None:
        settings = get_settings()
        _scan_semaphore = asyncio.Semaphore(settings.scan_max_concurrent)
        logger.info(
            f"扫描并发信号量初始化: 最大 {settings.scan_max_concurrent} 个并发任务"
        )
    return _scan_semaphore


async def _db_retry(func, max_retries=3, delay=1):
    """数据库操作重试"""
    for attempt in range(max_retries):
        try:
            return await func()
        except (OperationalError, InterfaceError) as e:
            error_str = str(e).lower()
            is_connection_error = any(
                kw in error_str
                for kw in [
                    "lost connection",
                    "server has gone away",
                    "connection was killed",
                    "timeout",
                    "pool exhausted",
                    "can't connect",
                ]
            )
            if is_connection_error and attempt < max_retries - 1:
                logger.warning(f"数据库连接异常，第{attempt + 1}次重试: {e}")
                await asyncio.sleep(delay * (attempt + 1))
                continue
            raise


def _parse_trigger_user_id(triggered_by: str | None) -> int | None:
    """从 triggered_by 解析触发用户数字 ID（"webui:123" → 123）。

    无法解析（scheduled / username / api 名）返回 None，读取语言时回退全局。
    """
    if not triggered_by or ":" not in triggered_by:
        return None
    value = triggered_by.rsplit(":", 1)[-1]
    try:
        return int(value)
    except ValueError:
        return None


class ScanWorker:
    """仓库扫描 Worker"""

    def __init__(self):
        from backend.core.github_app import GitHubAppClient

        self.github_app = GitHubAppClient()
        from backend.services.activity_observability.integration_service import (
            ActivityIntegrationService,
        )

        self.activity_integration = ActivityIntegrationService()

    async def get_scan_candidates(self) -> dict:
        """获取待扫描仓库列表（GitHub App 安装仓库 + 冷却期内未扫描）"""

        from backend.models.database import async_session

        # 从 GitHub App 安装获取仓库列表
        installations = await asyncio.to_thread(
            self.github_app.get_all_installations_with_repos
        )
        active_repos = []
        for inst in installations:
            for repo in inst["repos"]:
                active_repos.append(repo["full_name"])

        settings = get_settings()

        if not active_repos:
            return {
                "candidates": [],
                "total_active": 0,
                "cooldown_count": 0,
                "cooldown_hours": settings.scan_cooldown_hours,
            }

        async with async_session() as session:
            # 排除冷却期内已成功扫描的仓库
            cutoff = now_utc() - timedelta(hours=settings.scan_cooldown_hours)
            recent_result = await session.execute(
                select(RepoScan.repo_name).where(
                    RepoScan.status == ScanStatus.COMPLETED.value,
                    RepoScan.completed_at >= cutoff,
                )
            )
            recent_repo_set = {r[0] for r in recent_result.all()}

            candidates = [r for r in active_repos if r not in recent_repo_set]
            logger.info(
                f"扫描候选仓库: {len(candidates)}/{len(active_repos)} "
                f"({len(recent_repo_set)} 个在冷却期内)"
            )
            return {
                "candidates": candidates,
                "total_active": len(active_repos),
                "cooldown_count": len(recent_repo_set),
                "cooldown_hours": settings.scan_cooldown_hours,
            }

    async def create_scan_record(
        self, repo_name: str, trigger_type: str, triggered_by: str | None = None
    ) -> int:
        """创建扫描记录"""
        from backend.models.database import async_session

        repo_owner = repo_name.split("/")[0] if "/" in repo_name else ""

        async def _create():
            async with async_session() as session:
                scan = RepoScan(
                    repo_name=repo_name,
                    repo_owner=repo_owner,
                    trigger_type=trigger_type,
                    triggered_by=triggered_by,
                    status=ScanStatus.PENDING.value,
                )
                session.add(scan)
                await session.commit()
                await session.refresh(scan)
                return scan.id

        return await _db_retry(_create)

    async def process_scan(self, scan_id: int):
        """执行扫描主流程"""
        # Deadline starts before semaphore admission so queueing time is part of
        # the same task budget.  It is soft: only the next AI call is forced to
        # produce a tool-free final response.
        task_deadline = AITaskDeadline.from_timeout(
            get_settings().review_timeout_seconds
        )
        semaphore = await _get_scan_semaphore()
        async with semaphore:
            await self._process_scan_inner(scan_id, deadline=task_deadline)

    async def _start_threaded_execution(self, scan_id: int, repo_name: str):
        """接入活动观测（threaded）：失败降级为 None，不阻断扫描。"""
        try:
            return await self.activity_integration.start_scan_threaded_execution(
                {
                    "task_id": str(scan_id),
                    "delivery_id": str(scan_id),
                    "repo_full_name": repo_name,
                },
                task_id=scan_id,
            )
        except Exception as observability_exc:
            logger.warning(
                "扫描 observability admission skipped: {}", observability_exc
            )
            return None

    async def _process_scan_inner(
        self,
        scan_id: int,
        *,
        deadline: AITaskDeadline | None = None,
    ):
        """扫描内部逻辑"""
        from backend.models.database import async_session

        # 轮次与 token 均不设上限，由模型自然停止（与 Issue 分析一致）
        tracker = TokenTracker()
        task_deadline = deadline or AITaskDeadline.from_timeout(
            get_settings().review_timeout_seconds
        )
        repo_path = None
        execution = None

        try:
            # 1. 加载扫描记录
            triggered_by = None
            async with async_session() as session:
                scan = await session.get(RepoScan, scan_id)
                if not scan:
                    logger.error(f"扫描记录不存在: {scan_id}")
                    return
                if scan.status not in (
                    ScanStatus.PENDING.value,
                    ScanStatus.FAILED.value,
                ):
                    logger.warning(f"扫描 {scan_id} 状态非 PENDING: {scan.status}")
                    return
                repo_name = scan.repo_name
                triggered_by = scan.triggered_by

            logger.info(f"开始扫描仓库: {repo_name} (scan_id={scan_id})")

            # 2. 观测接入（threaded：对话流实时可见；失败降级继续）
            execution = await self._start_threaded_execution(scan_id, repo_name)

            # 3. 更新状态为 INDEXING
            await self._update_scan(
                scan_id,
                status=ScanStatus.INDEXING.value,
                current_phase="indexing",
                progress=10,
                started_at=now_utc(),
            )

            # 4. Clone 仓库到临时目录
            repo_path = await self._clone_repo(repo_name)
            if not repo_path:
                await self._update_scan(
                    scan_id,
                    status=ScanStatus.FAILED.value,
                    error_message="克隆仓库失败",
                )
                if execution is not None:
                    await execution.finish(
                        "failed",
                        error_message="克隆仓库失败",
                    )
                return

            # 获取 commit SHA
            commit_sha = await self._get_commit_sha(repo_path)

            # 5. 索引代码（仅用于语义检索工具；失败不影响扫描，只记日志）
            index_result = await self._index_repository(
                repo_name, repo_path, commit_sha
            )
            indexed_chunks = index_result.get("total_chunks", 0) or 0
            logger.info(
                f"仓库 {repo_name} 索引完成: "
                f"{index_result.get('indexed', 0)} 文件, {indexed_chunks} 代码块"
            )

            # 6. 收集真实代码文件列表（code_file_count 的单一事实来源）
            from backend.services.scan_prompt_builder import collect_code_files

            file_list = collect_code_files(repo_path)
            if not file_list:
                message = "仓库中无代码文件"
                logger.warning(f"仓库 {repo_name} 中无代码文件")
                await self._update_scan(
                    scan_id,
                    status=ScanStatus.FAILED.value,
                    file_count=0,
                    code_file_count=0,
                    indexed_chunks=indexed_chunks,
                    commit_sha=commit_sha,
                    error_message=message,
                )
                if execution is not None:
                    await execution.finish("failed", error_message=message)
                return

            await self._update_scan(
                scan_id,
                commit_sha=commit_sha,
                file_count=len(file_list),
                code_file_count=len(file_list),
                indexed_chunks=indexed_chunks,
            )

            # 7. 更新状态为 ANALYZING
            await self._update_scan(
                scan_id,
                status=ScanStatus.ANALYZING.value,
                current_phase="analyzing",
                progress=25,
            )

            # 8. 解析输出语言（用户自定义优先，回退全局）
            user_id = _parse_trigger_user_id(triggered_by)
            output_language = await get_user_dynamic_config("output_language", user_id)

            # 9. 使用 AIReviewer 工具链进行全仓扫描
            scan_result, scan_rounds = await self._full_scan_with_tools(
                scan_id=scan_id,
                repo_name=repo_name,
                repo_path=repo_path,
                commit_sha=commit_sha,
                tracker=tracker,
                file_list=file_list,
                execution=execution,
                output_language=output_language or "zh-CN",
                deadline=task_deadline,
            )
            all_findings = scan_result.get("findings", [])
            ai_health_score = scan_result.get("overall_score")
            ai_summary = scan_result.get("summary", "")

            # A protocol repair exhaustion is an explicit scan failure, not an
            # empty healthy repository.  Do not persist findings, generate a
            # report, close an old report Issue, or mark the scan completed.
            if scan_result.get("parse_source") == "scan_protocol_error":
                error_message = str(
                    scan_result.get("summary") or "扫描输出未通过协议校验"
                )[:2000]
                await self._update_scan(
                    scan_id,
                    status=ScanStatus.FAILED.value,
                    current_phase=None,
                    error_message=error_message,
                    scan_rounds=scan_rounds,
                    prompt_tokens=tracker.prompt_tokens,
                    completion_tokens=tracker.completion_tokens,
                )
                if execution is not None:
                    try:
                        await execution.finish("failed", error_message=error_message)
                    except Exception as finish_exc:
                        logger.warning(
                            "扫描协议失败 observability finish 失败: {}", finish_exc
                        )
                logger.error("扫描 {} 协议解析失败，终止报告流程: {}", scan_id, error_message)
                return

            # 10. 聚合结果（使用 AI 评估的健康评分）
            aggregated = self._aggregate_findings(
                all_findings, ai_health_score=ai_health_score
            )

            # 11. 写入 ScanFinding 记录
            await self._save_findings(scan_id, aggregated["findings"])

            # 12. 更新状态为 REPORTING
            await self._update_scan(
                scan_id,
                status=ScanStatus.REPORTING.value,
                current_phase="reporting",
                progress=95,
                total_findings=aggregated["total_findings"],
                critical_count=aggregated["critical_count"],
                major_count=aggregated["major_count"],
                minor_count=aggregated["minor_count"],
                suggestion_count=aggregated["suggestion_count"],
                overall_health_score=aggregated["health_score"],
                summary=ai_summary or None,
                scan_rounds=scan_rounds,
                prompt_tokens=tracker.prompt_tokens,
                completion_tokens=tracker.completion_tokens,
            )

            # 13. 生成报告（直接传递聚合数据，避免 DB 读取时序问题）
            report_data = {
                "code_file_count": len(file_list),
                "indexed_chunks": indexed_chunks,
                "total_findings": aggregated["total_findings"],
                "critical_count": aggregated["critical_count"],
                "major_count": aggregated["major_count"],
                "minor_count": aggregated["minor_count"],
                "suggestion_count": aggregated["suggestion_count"],
                "overall_health_score": aggregated["health_score"],
                "summary": ai_summary or None,
                "scan_rounds": scan_rounds,
                "prompt_tokens": tracker.prompt_tokens,
                "completion_tokens": tracker.completion_tokens,
                "output_language": output_language or "zh-CN",
            }
            report_info = await self._generate_reports(scan_id, report_data)
            issue_number = report_info.get("issue_number")
            issue_url = report_info.get("issue_url")

            # 14. 计算 estimated_cost
            s = get_settings()
            estimated_cost = tracker.calculate_cost(
                s.review_price_per_1k_prompt,
                s.review_price_per_1k_completion,
            )

            # 15. 完成
            await self._update_scan(
                scan_id,
                status=ScanStatus.COMPLETED.value,
                current_phase=None,
                progress=100,
                report_issue_number=issue_number,
                report_issue_url=issue_url,
                estimated_cost=estimated_cost,
                completed_at=now_utc(),
            )

            logger.info(
                "扫描完成: {} | 轮数={}, tokens={}+{}, cost={} | "
                "发现 {} 个问题, 健康评分 {}/100",
                repo_name,
                scan_rounds,
                tracker.prompt_tokens,
                tracker.completion_tokens,
                estimated_cost,
                aggregated["total_findings"],
                aggregated["health_score"],
            )
            if execution is not None:
                await execution.finish("completed")

        except Exception as e:
            logger.error(f"扫描 {scan_id} 执行失败: {e}", exc_info=True)
            if execution is not None:
                try:
                    await execution.finish("failed", error_message=str(e))
                except Exception as finish_exc:
                    logger.warning("扫描 observability finish 失败: {}", finish_exc)
            await self._update_scan(
                scan_id,
                status=ScanStatus.FAILED.value,
                error_message=str(e)[:2000],
            )
        finally:
            # 清理临时目录
            if repo_path and os.path.exists(repo_path):
                try:
                    shutil.rmtree(repo_path, ignore_errors=True)
                except Exception:
                    pass

    async def _clone_repo(self, repo_name: str) -> str | None:
        """克隆仓库到临时目录"""
        try:
            repo_owner, repo_name_only = repo_name.split("/", 1)
            github_app = self.github_app

            # 获取 installation access token
            client = await asyncio.to_thread(
                github_app.get_installation_client, repo_owner, repo_name_only
            )
            if not client:
                logger.error(f"无法获取仓库 {repo_name} 的客户端")
                return None

            # 获取 token 用于 git clone
            installation = github_app.integration.get_installation(
                owner=repo_owner, repo=repo_name_only
            )
            auth_token = github_app.integration.get_access_token(installation.id)

            # 创建临时目录
            tmp_dir = tempfile.mkdtemp(prefix="sakura_scan_")
            clone_url = (
                f"https://x-access-token:{auth_token.token}@github.com/{repo_name}.git"
            )

            # 浅克隆，只获取最新 commit
            proc = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--depth",
                "1",
                clone_url,
                tmp_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

            if proc.returncode != 0:
                logger.error(f"克隆仓库失败: {stderr.decode()[:500]}")
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return None

            logger.info(f"仓库 {repo_name} 克隆完成: {tmp_dir}")
            return tmp_dir

        except TimeoutError:
            logger.error(f"克隆仓库超时: {repo_name}")
            return None
        except Exception as e:
            logger.error(f"克隆仓库异常: {e}")
            return None

    async def _get_commit_sha(self, repo_path: str) -> str | None:
        """获取仓库最新 commit SHA"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                repo_path,
                "rev-parse",
                "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode().strip()
        except Exception:
            pass
        return None

    async def _index_repository(
        self, repo_name: str, repo_path: str, commit_sha: str
    ) -> dict:
        """调用 CodeIndexService 索引仓库代码"""
        try:
            from backend.services.code_index_service import CodeIndexService

            service = CodeIndexService()
            result = await service.index_repository_code(
                repo_full_name=repo_name,
                repo_path=repo_path,
                commit_sha=commit_sha or "unknown",
            )
            return result
        except Exception as e:
            logger.warning(f"索引仓库失败（继续分析）: {e}")
            return {"indexed": 0, "total_chunks": 0}

    async def _full_scan_with_tools(
        self,
        scan_id: int,
        repo_name: str,
        repo_path: str,
        commit_sha: str | None,
        tracker: TokenTracker,
        file_list: list[dict],
        execution: Any = None,
        output_language: str = "zh-CN",
        deadline: AITaskDeadline | None = None,
    ) -> tuple[dict, int]:
        """使用 AIReviewer 工具链进行全仓扫描

        让 AI 自主使用 read_file / list_directory / search_code_context
        浏览整个仓库，真正实现全量扫描。

        Returns:
            (scan_result, iteration) 二元组。
            scan_result 含 findings / overall_score / summary（协议解析产物）。
            iteration 为实际执行的轮次数。
        """
        from backend.services.ai_reviewer.reviewer import AIReviewer
        from backend.services.protocol_repair import (
            append_skipped_tool_results,
            run_protocol_repair_loop,
        )
        from backend.services.scan_prompt_builder import (
            build_sakura_knowledge_section,
            build_scan_context,
            build_scan_system_prompt,
            build_scan_user_message,
            log_sakura_injection,
        )
        from backend.services.scan_protocol import (
            SCAN_REPAIR_INSTRUCTION,
            ScanProtocolError,
            TaggedScanParser,
            safe_scan_protocol_failure,
        )

        task_deadline = deadline or AITaskDeadline.from_timeout(
            get_settings().review_timeout_seconds
        )

        # 1. 构建扫描上下文（与 AIReviewer 的 PR context 结构对齐）
        scan_context = build_scan_context(
            repo_name=repo_name,
            repo_path=repo_path,
            file_list=file_list,
            commit_sha=commit_sha,
        )

        # 2. 构建消息（英文强化 system + 不可信证据边界的 user）
        messages = [
            {
                "role": "system",
                "content": build_scan_system_prompt(
                    repo_name,
                    len(file_list),
                    language=output_language,
                ),
            },
            {"role": "user", "content": ""},
        ]

        # 3. 获取工具列表（复用 AIReviewer 的工具管理）
        reviewer = AIReviewer()
        repo_full_name = repo_name
        try:
            enabled_tools = await reviewer.tool_manager.get_enabled_tools(
                repo_full_name
            )
        except Exception as e:
            logger.warning(f"获取工具列表失败: {e}，使用基础工具")
            from backend.services.ai_reviewer.constants import (
                BASE_TOOLS,
                CODE_INDEX_TOOLS,
            )

            enabled_tools = []
            tool_defs = BASE_TOOLS + CODE_INDEX_TOOLS
            for tool_def in tool_defs:
                enabled_tools.append(tool_def)

        # 4. 获取 repo 对象供工具使用（优先 GitHub API，回退本地适配器）
        _owner, _name = repo_name.split("/", 1) if "/" in repo_name else ("", repo_name)
        _client = await asyncio.to_thread(
            self.github_app.get_installation_client, _owner, _name
        )
        if _client:
            _scan_repo = await asyncio.to_thread(_client.get_repo, repo_name)
            logger.info(f"使用 GitHub API repo 对象: {repo_name}")
        else:
            from backend.services.ai_reviewer.tools.local_repo_adapter import (
                LocalRepoAdapter,
            )

            _scan_repo = LocalRepoAdapter(repo_path, repo_name)
            logger.warning(f"GitHub client 不可用，使用本地文件系统适配器: {repo_name}")

        # 5. 注入 .sakura/ 记忆上下文（放进 user 消息的不可信证据边界内）
        sakura_section = ""
        try:
            from backend.services.sakura_memory_service import get_sakura_memory_service

            sakura_memory_service = get_sakura_memory_service()
            sakura_context = await sakura_memory_service.get_sakura_context(
                repo=_scan_repo,
                repo_full_name=repo_full_name,
            )
            if sakura_context:
                sakura_md = sakura_context.get("sakura_md", "")
                memory_md = sakura_context.get("memory_md", "")
                sakura_section = build_sakura_knowledge_section(sakura_md, memory_md)
                log_sakura_injection(sakura_md, memory_md)
        except Exception as e:
            logger.warning(
                f".sakura/ 记忆上下文注入失败（不影响扫描）: {e}",
                exc_info=True,
            )

        messages[1]["content"] = build_scan_user_message(
            scan_context, project_knowledge=sakura_section
        )

        # 6. 对话流回调：assistant/tool 消息写入活动观测 thread（实时监控）
        async def _event_callback(event_type, data):
            if event_type != "message" or execution is None:
                return
            if execution.thread is None:
                return
            try:
                origin_attempt_id = (
                    getattr(execution.observer, "last_attempt_id", None)
                    if data.get("role") in {"assistant", "tool"}
                    else None
                )
                await execution.tool_service.append_conversation_message(
                    thread_id=execution.thread.id,
                    work_unit_id=execution.work_unit.id,
                    message=data,
                    origin_attempt_id=origin_attempt_id,
                    lease=execution.lease,
                )
                if data.get("role") == "tool" and data.get("tool_call_id"):
                    if execution.tool_service.is_failed_tool_result(data):
                        await execution.tool_service.mark_tool_execution_failed(
                            execution.work_unit.id, data["tool_call_id"]
                        )
                    else:
                        await execution.tool_service.mark_tool_execution_completed(
                            execution.work_unit.id, data["tool_call_id"]
                        )
            except Exception as exc:
                logger.warning("scan event callback failed: {}", exc)

        for initial_message in messages:
            await _event_callback("message", initial_message)

        # 7. 多轮工具调用（模型由 main 角色绑定解析）
        settings = get_settings()

        (
            role_model,
            role_context_tokens,
        ) = await reviewer.api_client.resolve_role_model_context("main")
        # 上下文安全阈值与 Issue 分析对齐（0.8 常量，不设扫描专属配置）
        if role_context_tokens and role_context_tokens > 0:
            safe_context = int(role_context_tokens * 0.8)
        elif reviewer.model_context_mgr:
            safe_context = reviewer.model_context_mgr.calculate_safe_context(
                None,
                0.8,
            )
        else:
            safe_context = 0
        logger.info(
            "扫描使用 main 角色绑定: model={}, context_tokens={}",
            role_model or "<role metadata>",
            role_context_tokens or "<conservative fallback>",
        )

        invocation_context = execution.invocation_context if execution else None
        observer = execution.observer if execution else None

        async def _append_assistant_tool_turn(response, tool_calls) -> None:
            assistant_message = response.choices[0].message
            assistant_msg_dict = {
                "role": "assistant",
                "content": getattr(assistant_message, "content", None),
                "tool_calls": tool_calls,
            }
            if (
                hasattr(assistant_message, "reasoning_content")
                and assistant_message.reasoning_content
            ):
                assistant_msg_dict["reasoning_content"] = (
                    assistant_message.reasoning_content
                )
            messages.append(assistant_msg_dict)
            await _event_callback("message", assistant_msg_dict)

        try:
            max_attempts = int(
                await get_dynamic_config("protocol_repair_max_attempts") or 3
            )
        except ValueError, TypeError:
            max_attempts = 3

        # 不设轮次与 token 上限，依赖模型自然停止（无工具调用即交付最终结果）
        iteration = 0
        while True:
            iteration += 1
            logger.info(f"全仓扫描 第 {iteration} 轮 AI 调用 (模型: main role)...")
            await self._update_scan(
                scan_id,
                progress=min(25 + iteration * 3, 90),
            )

            try:
                prompt_was_sent = task_deadline.timeout_prompt_sent
                call_kwargs = {
                    "model": "",
                    "messages": messages,
                    "tools": enabled_tools,
                    "tool_choice": "auto",
                    "temperature": settings.ai_temperature,
                    "role": "main",
                    "context": invocation_context,
                    "observer": observer,
                }
                call_kwargs.update(task_deadline.prepare_call(messages))
                if (
                    not prompt_was_sent
                    and task_deadline.timeout_prompt_sent
                ):
                    await _event_callback("message", messages[-1])

                response = await reviewer.api_client.call_with_retry(**call_kwargs)

                tracker.accumulate(response)
                tracker.log_context_usage(
                    response,
                    role_context_tokens,
                    iteration,
                )

                # 检查工具调用
                tool_calls = getattr(response.choices[0].message, "tool_calls", None)
                if not tool_calls:
                    # AI 完成分析：信封协议解析（失败进入累积式修复循环）
                    review_text = response.choices[0].message.content
                    logger.info(f"全仓扫描对话完成（{iteration} 轮），进入协议解析...")

                    await _event_callback(
                        "message",
                        {"role": "assistant", "content": review_text},
                    )

                    result = await run_protocol_repair_loop(
                        parse_fn=TaggedScanParser().parse,
                        error_type=ScanProtocolError,
                        base_messages=messages,
                        final_text=review_text,
                        repair_instruction=SCAN_REPAIR_INSTRUCTION,
                        api_client=reviewer.api_client,
                        tracker=tracker,
                        max_attempts=max_attempts,
                        fallback_result_fn=safe_scan_protocol_failure,
                        log_label="扫描",
                        sse_channel="scan:protocol_repair",
                        invocation_context=invocation_context,
                        observer=observer,
                        event_callback=_event_callback,
                        deadline=task_deadline,
                    )
                    logger.info(
                        f"全仓扫描完成（{iteration} 轮对话）: "
                        f"{len(result.get('findings', []))} 个问题, "
                        f"评分={result.get('overall_score', '-')}, "
                        f"parse_source={result.get('parse_source')}"
                    )
                    return result, iteration

                # If the provider returned tool calls after the soft deadline,
                # never execute them.  Parse/repair the text as the final answer
                # under the same shared protocol helper instead.
                if task_deadline.tools_disabled:
                    await _append_assistant_tool_turn(response, tool_calls)
                    await append_skipped_tool_results(
                        messages,
                        tool_calls,
                        event_callback=_event_callback,
                    )
                    review_text = response.choices[0].message.content or ""
                    result = await run_protocol_repair_loop(
                        parse_fn=TaggedScanParser().parse,
                        error_type=ScanProtocolError,
                        base_messages=messages,
                        final_text=review_text,
                        repair_instruction=SCAN_REPAIR_INSTRUCTION,
                        api_client=reviewer.api_client,
                        tracker=tracker,
                        max_attempts=max_attempts,
                        fallback_result_fn=safe_scan_protocol_failure,
                        log_label="扫描",
                        sse_channel="scan:protocol_repair",
                        invocation_context=invocation_context,
                        observer=observer,
                        event_callback=_event_callback,
                        deadline=task_deadline,
                    )
                    return result, iteration

                # A call that started before expiry may return tool calls after
                # the deadline.  Preserve that turn and ask for a final-only
                # response on the next iteration; no tool is executed here.
                if task_deadline.is_expired():
                    await _append_assistant_tool_turn(response, tool_calls)
                    await append_skipped_tool_results(
                        messages,
                        tool_calls,
                        event_callback=_event_callback,
                    )
                    continue

                # 处理工具调用
                await _append_assistant_tool_turn(response, tool_calls)

                for tool_index, tool_call in enumerate(tool_calls):
                    if task_deadline.is_expired():
                        await append_skipped_tool_results(
                            messages,
                            tool_calls[tool_index:],
                            event_callback=_event_callback,
                        )
                        break
                    tool_name = tool_call.function.name
                    try:
                        result = await reviewer.tool_handler.handle_tool_call(
                            tool_call,
                            _scan_repo,
                            None,  # pr（scan 无 PR）
                        )
                        tool_payload = _json.dumps(result, ensure_ascii=False)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": tool_payload,
                            }
                        )
                        logger.info(
                            f"执行工具 {tool_call.function.name}: {tool_call.function.arguments[:100]}"
                        )
                        await _event_callback(
                            "message",
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_name,
                                "content": tool_payload,
                            },
                        )
                    except Exception as e:
                        logger.error(f"执行工具失败: {e}")
                        error_payload = _json.dumps({"error": str(e)})
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": error_payload,
                            }
                        )
                        await _event_callback(
                            "message",
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_name,
                                "content": error_payload,
                            },
                        )

                # 本地估算仅用于决定下一轮发送前是否压缩。
                try:
                    current_tokens = (
                        reviewer.context_compressor.estimate_messages_tokens(messages)
                    )
                except Exception:
                    logger.warning("token estimation failed, skipping", exc_info=True)
                    current_tokens = 0

                # 上下文压缩检查（使用扫描独立配置）
                if reviewer.enable_compression:
                    try:
                        threshold_tokens = int(
                            safe_context * settings.context_compression_threshold
                        )

                        if current_tokens > threshold_tokens:
                            messages = await reviewer.context_compressor.compress_conversation_history(
                                messages,
                                messages[0]["content"],
                                threshold_tokens,
                            )
                            logger.info("扫描上下文压缩完成，继续...")
                    except Exception as e:
                        logger.warning(f"扫描上下文压缩失败: {e}")

            except Exception as e:
                logger.error(f"全仓扫描 AI 调用失败: {e}")
                raise

    def _aggregate_findings(
        self, all_findings: list[dict], ai_health_score: int | None = None
    ) -> dict:
        """聚合并去重 findings，计算健康评分

        Args:
            all_findings: AI 返回的 findings 列表
            ai_health_score: AI 直接评估的健康评分（优先使用）
        """
        # 去重：同一 file_path + title 视为重复
        seen = set()
        deduplicated = []
        for f in all_findings:
            key = (f.get("file_path", ""), f.get("title", ""))
            if key not in seen:
                seen.add(key)
                deduplicated.append(f)

        # 按严重性统计
        severity_counts = {"critical": 0, "major": 0, "minor": 0, "suggestion": 0}
        for f in deduplicated:
            sev = f.get("severity", "minor")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1

        # 健康评分：优先使用 AI 评估值，回退到公式计算
        if ai_health_score is not None and 0 <= ai_health_score <= 100:
            health_score = ai_health_score
        else:
            health_score = max(
                0,
                100
                - severity_counts["critical"] * 20
                - severity_counts["major"] * 10
                - severity_counts["minor"] * 3
                - severity_counts["suggestion"] * 1,
            )

        return {
            "total_findings": len(deduplicated),
            "critical_count": severity_counts["critical"],
            "major_count": severity_counts["major"],
            "minor_count": severity_counts["minor"],
            "suggestion_count": severity_counts["suggestion"],
            "health_score": health_score,
            "findings": deduplicated,
        }

    async def _save_findings(self, scan_id: int, findings: list[dict]):
        """保存 findings 到数据库"""
        from backend.models.database import async_session

        if not findings:
            return

        async def _save():
            async with async_session() as session:
                for f in findings:
                    finding = ScanFinding(
                        scan_id=scan_id,
                        file_path=f.get("file_path"),
                        line_start=f.get("line_start"),
                        line_end=f.get("line_end"),
                        severity=f.get("severity", "minor"),
                        category=f.get("category", "maintainability"),
                        title=f.get("title", ""),
                        description=f.get("description", ""),
                        suggestion=f.get("suggestion"),
                        code_snippet=None,
                        confidence=f.get("confidence", 50),
                    )
                    session.add(finding)
                await session.commit()

        await _db_retry(_save)
        logger.info(f"已保存 {len(findings)} 个 findings (scan_id={scan_id})")

    async def _generate_reports(
        self, scan_id: int, report_data: dict | None = None
    ) -> dict:
        """生成报告（GitHub Issue + Telegram 通知）

        Args:
            scan_id: 扫描记录 ID
            report_data: 直接传递的聚合数据，绕过 DB 读取时序问题
        """
        report_info = {}

        try:
            from backend.services.scan_report_service import ScanReportService

            report_service = ScanReportService()
            report_info = await report_service.generate_and_deliver(
                scan_id, report_data
            )
        except Exception as e:
            logger.error(f"生成报告失败: {type(e).__name__}: {e}", exc_info=True)

        return report_info

    async def _update_scan(self, scan_id: int, **kwargs):
        """更新扫描记录"""
        from backend.models.database import async_session

        async def _update():
            async with async_session() as session:
                scan = await session.get(RepoScan, scan_id)
                if scan:
                    for key, value in kwargs.items():
                        if hasattr(scan, key):
                            setattr(scan, key, value)
                    if "status" not in kwargs:
                        scan.updated_at = now_utc()
                    await session.commit()

        try:
            await _db_retry(_update)
        except Exception as e:
            logger.error(f"更新扫描记录失败 (scan_id={scan_id}): {e}")
