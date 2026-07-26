"""仓库扫描 Worker"""

import asyncio
import json as _json
import os
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError

from backend.core.config import get_settings
from backend.models.scan_models import RepoScan, ScanFinding, ScanStatus
from backend.services.ai_reviewer.token_tracker import TokenTracker

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


class ScanTokenBudget:
    """扫描 Token 预算管理器

    max_tokens=0 表示无上限。
    """

    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens  # 0 = 无限制
        self.used_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    @property
    def unlimited(self) -> bool:
        return self.max_tokens <= 0

    def can_proceed(self, estimated_tokens: int = 3000) -> bool:
        """检查是否还有足够预算（0=无限制时始终返回 True）"""
        if self.unlimited:
            return True
        return (self.used_tokens + estimated_tokens) <= self.max_tokens

    def remaining(self) -> int:
        """剩余可用 token（无限制时返回大数）"""
        if self.unlimited:
            return 100000
        return max(0, self.max_tokens - self.used_tokens)

    def consume(self, prompt_tokens: int, completion_tokens: int):
        """消耗 token"""
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.used_tokens += prompt_tokens + completion_tokens


class ScanWorker:
    """仓库扫描 Worker"""

    def __init__(self):
        from backend.core.github_app import GitHubAppClient

        self.github_app = GitHubAppClient()
        from backend.services.activity_observability.integration_service import (
            ActivityIntegrationService,
        )
        self.activity_integration = ActivityIntegrationService()

    @staticmethod
    async def _log_activity(
        scan_id: int,
        event_type: str,
        content: dict[str, Any] | None = None,
    ) -> None:
        """Legacy activity event hook — now a no-op.

        The new observability system (ActivityOutbox + user-scoped SSE, driven by
        ``execution.finish`` and the Attempt observer) replaces the legacy
        ``activity_events`` table and global ``activity:*`` SSE channel. Retained
        as a shim so existing call sites remain harmless; it writes nothing.
        """
        return None

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
            cutoff = datetime.now(UTC) - timedelta(hours=settings.scan_cooldown_hours)
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
        semaphore = await _get_scan_semaphore()
        async with semaphore:
            await self._process_scan_inner(scan_id)

    async def _process_scan_inner(self, scan_id: int):
        """扫描内部逻辑"""
        from backend.models.database import async_session

        settings = get_settings()
        budget = ScanTokenBudget(max_tokens=settings.scan_max_tokens_per_repo)
        repo_path = None

        try:
            # 1. 加载扫描记录
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

            logger.info(f"开始扫描仓库: {repo_name} (scan_id={scan_id})")

            execution = None
            try:
                execution = await self.activity_integration.start_scan_execution(
                    {
                        "task_id": str(scan_id),
                        "delivery_id": str(scan_id),
                        "repo_full_name": repo_name,
                    },
                    task_id=scan_id,
                )
            except Exception as observability_exc:
                logger.warning("扫描 observability admission skipped: {}", observability_exc)

            # 2. 更新状态为 INDEXING
            await self._update_scan(
                scan_id,
                status=ScanStatus.INDEXING.value,
                current_phase="indexing",
                started_at=datetime.now(UTC),
            )
            await self._log_activity(
                scan_id,
                "status",
                {
                    "status": "indexing",
                    "message": f"开始索引仓库: {repo_name}",
                },
            )

            # 3. Clone 仓库到临时目录
            await self._log_activity(
                scan_id,
                "thinking",
                {
                    "message": f"正在克隆仓库 {repo_name} ...",
                },
            )
            repo_path = await self._clone_repo(repo_name)
            if not repo_path:
                await self._update_scan(
                    scan_id,
                    status=ScanStatus.FAILED.value,
                    error_message="克隆仓库失败",
                )
                await self._log_activity(
                    scan_id,
                    "error",
                    {
                        "message": "克隆仓库失败",
                    },
                )
                if execution is not None:
                    await execution.finish(
                        "failed",
                        error_message="克隆仓库失败",
                    )
                return

            # 获取 commit SHA
            commit_sha = await self._get_commit_sha(repo_path)

            # 4. 索引代码
            await self._log_activity(
                scan_id,
                "tool_call",
                {
                    "tool": "index_repository",
                    "status": "running",
                    "detail": f"索引 {repo_name} 代码",
                },
            )
            index_result = await self._index_repository(
                repo_name, repo_path, commit_sha
            )
            file_count = index_result.get("total_chunks", 0)
            await self._log_activity(
                scan_id,
                "tool_result",
                {
                    "tool": "index_repository",
                    "status": "completed",
                    "detail": f"已索引 {file_count} 个文件块",
                },
            )

            await self._update_scan(
                scan_id,
                commit_sha=commit_sha,
                file_count=file_count,
                code_file_count=file_count,
            )

            # 5. 更新状态为 ANALYZING
            await self._update_scan(
                scan_id, status=ScanStatus.ANALYZING.value, current_phase="analyzing"
            )
            await self._log_activity(
                scan_id,
                "status",
                {
                    "status": "analyzing",
                    "message": "开始 AI 分析",
                },
            )

            # 6. 使用 AIReviewer 工具链进行全仓扫描
            (
                all_findings,
                ai_health_score,
                scan_rounds,
            ) = await self._full_scan_with_tools(
                scan_id=scan_id,
                repo_name=repo_name,
                repo_path=repo_path,
                commit_sha=commit_sha,
                budget=budget,
            )

            # 7. 聚合结果（使用 AI 评估的健康评分）
            aggregated = self._aggregate_findings(
                all_findings, ai_health_score=ai_health_score
            )

            # 8. 写入 ScanFinding 记录
            await self._save_findings(scan_id, aggregated["findings"])

            # 9. 更新状态为 REPORTING
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
                prompt_tokens=budget.prompt_tokens,
                completion_tokens=budget.completion_tokens,
            )
            await self._log_activity(
                scan_id,
                "status",
                {
                    "status": "reporting",
                    "message": "生成扫描报告",
                    "total_findings": aggregated["total_findings"],
                    "health_score": aggregated["health_score"],
                },
            )

            # 10. 生成报告（直接传递聚合数据，避免 DB 读取时序问题）
            report_data = {
                "code_file_count": file_count,
                "total_findings": aggregated["total_findings"],
                "critical_count": aggregated["critical_count"],
                "major_count": aggregated["major_count"],
                "minor_count": aggregated["minor_count"],
                "suggestion_count": aggregated["suggestion_count"],
                "overall_health_score": aggregated["health_score"],
                "prompt_tokens": budget.prompt_tokens,
                "completion_tokens": budget.completion_tokens,
            }
            report_info = await self._generate_reports(scan_id, report_data)
            issue_number = report_info.get("issue_number")
            issue_url = report_info.get("issue_url")

            # 11. 计算 estimated_cost
            s = get_settings()
            cost_tracker = TokenTracker()
            cost_tracker.add_tokens(budget.prompt_tokens, budget.completion_tokens)
            estimated_cost = cost_tracker.calculate_cost(
                s.review_price_per_1k_prompt,
                s.review_price_per_1k_completion,
            )

            # 12. 完成
            await self._update_scan(
                scan_id,
                status=ScanStatus.COMPLETED.value,
                current_phase=None,
                progress=100,
                report_issue_number=issue_number,
                report_issue_url=issue_url,
                estimated_cost=estimated_cost,
                completed_at=datetime.now(UTC),
            )
            await self._log_activity(
                scan_id,
                "result",
                {
                    "status": "completed",
                    "message": "扫描完成",
                    "total_findings": aggregated["total_findings"],
                    "health_score": aggregated["health_score"],
                    "scan_rounds": scan_rounds,
                    "report_issue_url": issue_url,
                },
            )

            logger.info(
                "扫描完成: {} | 轮数={}, tokens={}+{}, cost={} | "
                "发现 {} 个问题, 健康评分 {}/100",
                repo_name,
                scan_rounds,
                budget.prompt_tokens,
                budget.completion_tokens,
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
            await self._log_activity(
                scan_id,
                "error",
                {
                    "message": f"扫描失败: {str(e)[:500]}",
                },
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
            logger.info(
                f"仓库 {repo_name} 索引完成: "
                f"{result.get('indexed', 0)} 文件, "
                f"{result.get('total_chunks', 0)} 代码块"
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
        budget: ScanTokenBudget,
    ) -> tuple[list[dict], int | None, int]:
        """使用 AIReviewer 工具链进行全仓扫描

        让 AI 自主使用 read_file / list_directory / search_code_context
        浏览整个仓库，真正实现全量扫描。

        Returns:
            (findings, ai_health_score, iteration) 三元组。
            ai_health_score 为 AI 评估的评分（可能为 None）。
            iteration 为实际执行的轮次数。
        """
        from backend.services.ai_reviewer.reviewer import AIReviewer
        from backend.services.scan_prompt_builder import (
            build_scan_context,
            collect_code_files,
            parse_scan_result,
        )

        # 1. 收集仓库中的代码文件
        file_list = collect_code_files(repo_path)
        if not file_list:
            logger.warning(f"仓库 {repo_name} 中无代码文件")
            return [], None

        logger.info(f"仓库 {repo_name} 共 {len(file_list)} 个代码文件")

        # 2. 构建扫描上下文（与 AIReviewer 的 PR context 结构对齐）
        scan_context = build_scan_context(
            repo_name=repo_name,
            repo_path=repo_path,
            file_list=file_list,
            commit_sha=commit_sha,
        )

        # 3. 构建消息
        # 注入输出语言指令 / Inject output language directive
        from backend.core.config import get_settings as _get_settings
        from backend.services.scan_prompt_builder import SCAN_SYSTEM_PROMPT

        _settings = _get_settings()
        scan_system_prompt = SCAN_SYSTEM_PROMPT
        output_lang = _settings.output_language
        if output_lang:
            language_names = {
                "zh-CN": "中文 (Simplified Chinese)",
                "en": "English",
            }
            lang_display = language_names.get(output_lang, output_lang)
            scan_system_prompt += f"\n\n## Output Language\n**Important**: You MUST write all scan findings, summaries, and suggestions in {lang_display}."

        messages = [
            {"role": "system", "content": scan_system_prompt},
            {
                "role": "user",
                "content": (
                    f"## 仓库信息\n"
                    f"- 仓库名称: {repo_name}\n"
                    f"- Commit: {commit_sha or 'unknown'}\n"
                    f"- 代码文件数: {len(file_list)}\n"
                    f"- 总大小: {scan_context['total_size']}\n\n"
                    f"## 项目结构\n"
                    f"```\n{scan_context['project_structure']}\n```\n\n"
                    f"## 文件列表（前 200 个）\n"
                    f"```\n{scan_context['file_tree']}\n```\n\n"
                    f"请使用工具浏览代码，按 5 个维度（安全/性能/可靠性/可维护性/架构）进行全仓扫描。"
                ),
            },
        ]

        # 4. 获取工具列表（复用 AIReviewer 的工具管理）
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

        # 5. 获取 repo 对象供工具使用（优先 GitHub API，回退本地适配器）
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

        # 5.5 注入 .sakura/ 记忆上下文 / Inject .sakura/ memory context
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
                if sakura_md or memory_md:
                    sakura_section = (
                        "\n\n## 项目知识（来自 .sakura/ 目录，请主动参考）\n\n"
                        "以下是该项目积累的审查经验和知识，请在扫描中参考：\n"
                        "- 如果项目有已知的审查规则，按照规则检查代码\n"
                        "- 如果项目记忆中记录了常见问题，重点排查类似问题是否重现\n"
                        "- 避免提出与项目记忆中已确认的做法相矛盾的建议\n"
                    )
                    if sakura_md:
                        sakura_section += f"\n### 项目概述\n{sakura_md}"
                    if memory_md:
                        sakura_section += f"\n\n### 项目记忆\n{memory_md}"
                    messages[1]["content"] += sakura_section
                    parts = []
                    if sakura_md:
                        parts.append(f"SAKURA.md({len(sakura_md)}字)")
                    if memory_md:
                        parts.append(f"memory.md({len(memory_md)}字)")
                    logger.info(f"已注入 .sakura/ 记忆上下文: {', '.join(parts)}")
        except Exception as e:
            logger.warning(
                f".sakura/ 记忆上下文注入失败（不影响扫描）: {e}",
                exc_info=True,
            )

        # 6. 多轮工具调用（使用扫描独立配置）
        from backend.core.config import get_settings

        settings = get_settings()
        # 扫描模型由 main 角色绑定解析；旧 scan/openai 模型配置不得参与请求。
        max_iterations = settings.scan_max_iterations
        scan_temperature = settings.scan_temperature

        tracker = TokenTracker()
        role_model, role_context_tokens = await reviewer.api_client.resolve_role_model_context(
            "main"
        )
        if role_context_tokens and role_context_tokens > 0:
            safe_context = int(
                role_context_tokens * settings.scan_context_safety_threshold
            )
        elif reviewer.model_context_mgr:
            safe_context = reviewer.model_context_mgr.calculate_safe_context(
                None,
                settings.scan_context_safety_threshold,
            )
        else:
            safe_context = 0
        logger.info(
            "扫描使用 main 角色绑定: model={}, context_tokens={}",
            role_model or "<role metadata>",
            role_context_tokens or "<conservative fallback>",
        )

        iteration = 0
        while iteration < max_iterations:
            if not budget.can_proceed(estimated_tokens=3000):
                logger.warning(f"Token 预算耗尽 ({budget.used_tokens})，停止工具调用")
                break

            iteration += 1
            logger.info(
                f"全仓扫描 第 {iteration}/{max_iterations} 轮 AI 调用 (模型: main role)..."
            )

            # 记录 AI 思考事件
            await self._log_activity(
                scan_id,
                "thinking",
                {
                    "message": f"第 {iteration}/{max_iterations} 轮 AI 分析",
                    "round": iteration,
                },
            )

            try:
                response = await reviewer.api_client.call_with_retry(
                    model="",
                    messages=messages,
                    tools=enabled_tools,
                    tool_choice="auto",
                    temperature=scan_temperature,
                    role="main",
                )

                tracker.accumulate(response)
                usage = getattr(response, "usage", None)
                if usage and budget:
                    budget.consume(
                        getattr(usage, "prompt_tokens", 0) or 0,
                        getattr(usage, "completion_tokens", 0) or 0,
                    )

                # 检查工具调用
                tool_calls = getattr(response.choices[0].message, "tool_calls", None)
                if not tool_calls:
                    # AI 完成分析
                    review_text = response.choices[0].message.content
                    result = parse_scan_result(review_text)
                    logger.info(
                        f"全仓扫描完成（{iteration} 轮对话）: "
                        f"{len(result.get('findings', []))} 个问题, "
                        f"评分={result.get('overall_score', '-')}"
                    )
                    await self._log_activity(
                        scan_id,
                        "ai_response",
                        {
                            "message": "AI 分析完成",
                            "round": iteration,
                            "findings_count": len(result.get("findings", [])),
                            "overall_score": result.get("overall_score"),
                            "content_preview": (review_text or "")[:500],
                        },
                    )
                    return (
                        result.get("findings", []),
                        result.get("overall_score"),
                        iteration,
                    )

                # 处理工具调用
                assistant_message = response.choices[0].message
                assistant_msg_dict = {
                    "role": "assistant",
                    "content": getattr(assistant_message, "content", None),
                    "tool_calls": tool_calls,
                }

                # DeepSeek-R1 reasoning_content 兼容
                if (
                    hasattr(assistant_message, "reasoning_content")
                    and assistant_message.reasoning_content
                ):
                    assistant_msg_dict["reasoning_content"] = (
                        assistant_message.reasoning_content
                    )

                messages.append(assistant_msg_dict)

                import json

                for tool_call in tool_calls:
                    tool_name = tool_call.function.name
                    tool_args_raw = tool_call.function.arguments[:200]
                    await self._log_activity(
                        scan_id,
                        "tool_call",
                        {
                            "tool": tool_name,
                            "status": "running",
                            "detail": tool_args_raw,
                            "round": iteration,
                        },
                    )
                    try:
                        result = await reviewer.tool_handler.handle_tool_call(
                            tool_call,
                            _scan_repo,
                            None,  # pr（scan 无 PR）
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps(result, ensure_ascii=False),
                            }
                        )
                        logger.info(
                            f"执行工具 {tool_call.function.name}: {tool_call.function.arguments[:100]}"
                        )
                        result_preview = (
                            _json.dumps(result, ensure_ascii=False)[:300]
                            if result
                            else ""
                        )
                        await self._log_activity(
                            scan_id,
                            "tool_result",
                            {
                                "tool": tool_name,
                                "status": "completed",
                                "detail": result_preview,
                                "round": iteration,
                            },
                        )
                    except Exception as e:
                        logger.error(f"执行工具失败: {e}")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": json.dumps({"error": str(e)}),
                            }
                        )
                        await self._log_activity(
                            scan_id,
                            "tool_result",
                            {
                                "tool": tool_name,
                                "status": "failed",
                                "detail": str(e)[:300],
                                "round": iteration,
                            },
                        )

                # 每轮记录上下文使用率
                try:
                    current_tokens = (
                        reviewer.context_compressor.estimate_messages_tokens(messages)
                    )
                    tracker.log_context_usage(current_tokens, safe_context, iteration)
                except Exception:
                    logger.warning("token estimation failed, skipping", exc_info=True)
                    current_tokens = 0

                # 上下文压缩检查（使用扫描独立配置）
                if reviewer.enable_compression:
                    try:
                        threshold_tokens = int(
                            safe_context * settings.scan_compression_threshold
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

        logger.warning(f"全仓扫描达到最大轮次 ({max_iterations})，停止")
        return [], None, iteration

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
                        scan.updated_at = datetime.now(UTC)
                    await session.commit()

        try:
            await _db_retry(_update)
        except Exception as e:
            logger.error(f"更新扫描记录失败 (scan_id={scan_id}): {e}")
