"""Sakura 记忆系统服务 / Sakura Memory System Service

负责管理 .sakura/ 目录的记忆系统：
- 自动初始化 .sakura/ 目录 / Auto-initialize .sakura/ directory
- 审查后深度反思 / Post-review deep reflection
- 定期合并更新 SAKURA.md 和 memory.md / Periodically consolidate and update files
- 读取上下文注入审查 prompt / Read context for review prompt injection
"""

import asyncio
import functools
import hashlib
import json
from datetime import datetime, timezone
from typing import Dict, Optional

from loguru import logger

from backend.core.config import get_settings, get_strategy_config
from backend.models.database import SakuraMemoryState, async_session
from backend.services.github_write_service import get_github_write_service
from backend.services.ai_reviewer.api_client import AIApiClient


# Reflection prompt template / 反思 Prompt 模板
REFLECTION_PROMPT = """你是一个代码审查反思助手。请对以下审查结果进行深度反思。

## 审查信息
- PR: #{pr_number}
- 提交: {commit_sha}
- 仓库: {repo_full_name}
- 审查策略: {strategy}
- 评分: {score}/10
- 决策: {decision}
- 审查类型: {review_type}
- 本次变更范围: {incremental_scope}

## 新增提交
{new_commits_summary}

## 审查摘要
{review_summary}

## 主要评论
{comments_summary}

## 变更文件
{changed_files}

## 当前项目记忆
{current_memory}

## PR 描述
{pr_description}

## 历史审查上下文
{history_context}

请从以下维度进行深度反思：

1. **审查质量评估**
   - 覆盖度：是否遗漏了重要的代码问题？
   - 准确度：标记的严重程度是否合理？有无误判？
   - 完整性：是否充分理解了变更的意图和上下文？

2. **发现的模式**
   - 代码模式：观察到的编码习惯、常用模式
   - 架构观察：项目架构的特点、依赖关系

3. **规范完善建议**
   - 基于本次审查，建议新增或修改的审查规则
   - 项目特有的规范要点

4. **经验教训**
   - 值得在未来审查中关注的要点
   - 对特定技术或模式的审查经验

{incremental_reflection_prompt}

请用中文输出，保持简洁但深入。"""

# Issue reflection prompt template / Issue 反思 Prompt 模板
ISSUE_REFLECTION_PROMPT = """你是一个 Issue 分析反思助手。请对以下 Issue 分析结果进行深度反思。

## 分析信息
- Issue: #{issue_number}
- 仓库: {repo_full_name}
- 作者: {author}
- 原始标题: {original_title}
- 原始内容摘要: {original_body}

## 分析结果
- 分类: {category}
- 优先级: {priority}
- 可行性: {feasibility}
- 摘要: {summary}

## 建议标签
{suggested_labels}

## 建议指派人
{suggested_assignees}

## 建议标题
{suggested_title}

## 重复检测结果
{duplicate_info}

## 关联 PR
{related_prs}

## 当前项目记忆
{current_memory}

请从以下维度进行深度反思：

1. **分类与优先级准确性**
   - 分类是否准确？是否有更合适的类别？
   - 优先级判断是否合理？关键词匹配是否产生了误判？
   - 是否考虑到 Issue 的实际影响范围？

2. **标签推荐质量**
   - 建议的标签是否贴切？置信度评估是否合理？
   - 是否遗漏了重要的标签？
   - 标签与仓库现有标签体系的一致性如何？

3. **可行性判断质量**
   - 可行性评估是否充分参考了代码库实际情况？
   - 工作量估算是否合理？
   - 是否正确识别了技术依赖和风险？

4. **重复检测准确性**
   - 重复检测的结果是否准确？
   - 是否存在误报（将不同问题标记为重复）？
   - 是否存在漏报（遗漏了实际重复的 Issue）？

5. **标题改写适当性**
   - 建议标题是否比原标题更清晰？
   - 改写是否保留了原始问题的核心信息？
   - 是否过度改写或不必要地改写了原本清晰的标题？

6. **经验教训**
   - 值得在未来 Issue 分析中关注的要点
   - 对特定类型 Issue 的分析经验
   - 仓库特有的模式和建议

请用中文输出，保持简洁但深入。"""

# Consolidation prompt template / 合并 Prompt 模板
CONSOLIDATE_SAKURA_PROMPT = """你是一个项目知识管理助手。请根据最近的审查反思，更新项目的 SAKURA.md 概述文件。

## 最近的审查反思
{reflections}

## 当前 SAKURA.md 内容
{current_sakura_md}

## 仓库信息
- 仓库名: {repo_full_name}
- 语言统计: {languages}
- 累计反思次数: {total_reflections}（此数值已精确计算，必须原样使用，禁止自行加减或重算）

## 硬性约束
- 输出内容不得超过 {max_chars} 字符（含标点和空格），这是严格的硬限制
- 如当前内容已接近上限，优先保留最重要的经验，主动删除过时或重复内容
- 宁可精简也不要超长——超出限制的内容将被丢弃，导致信息损失
- 必须保留累计反思次数标记：累计反思 {total_reflections} 次

请直接输出更新后的 SAKURA.md 内容，包含：
- 项目简介和技术栈
- 架构设计和关键决策
- 已知问题和注意事项
- 审查中发现的重要模式
- 团队约定和规范

直接输出 Markdown 内容，不要包含代码块标记（```），不要包含任何说明文字。"""

CONSOLIDATE_MEMORY_PROMPT = """你是一个代码审查经验总结助手。请根据最近的审查反思，更新项目的 memory.md 记忆文件。

## 最近的审查反思
{reflections}

## 当前 memory.md 内容
{current_memory_md}

## 仓库信息
- 仓库名: {repo_full_name}
- 累计反思次数: {total_reflections}（此数值已精确计算，必须原样使用，禁止自行加减或重算）

## 硬性约束
- 输出内容不得超过 {max_chars} 字符（含标点和空格），这是严格的硬限制
- 如当前内容已接近上限，优先保留最重要的经验，主动删除过时或重复内容
- 宁可精简也不要超长——超出限制的内容将被丢弃，导致信息损失
- 必须保留累计反思次数标记：累计反思 {total_reflections} 次

请直接输出更新后的 memory.md 内容，包含：
- 常见代码问题和审查要点
- 近期审查模式总结
- 规范建议和经验教训
- 需要特别关注的领域

直接输出 Markdown 内容，不要包含代码块标记（```），不要包含任何说明文字。"""

# Initialization prompt template / 初始化 Prompt 模板
INIT_PROMPT = """你是一个项目分析助手。请根据以下仓库信息，生成一个项目概述文档。

## 仓库名: {repo_full_name}

## 语言统计
{languages}

## 目录结构
{directory_structure}

## README 内容（如有）
{readme_content}

请生成一个项目概述文档（中文），包含以下部分：
1. **项目简介** - 一句话描述项目用途
2. **技术栈** - 使用的主要技术和框架
3. **项目结构** - 核心模块和目录说明
4. **开发约定** - 从代码结构推断的开发规范

保持简洁，总计不超过 2000 字。直接输出 Markdown 内容，不要包含代码块标记。"""


class SakuraMemoryService:
    """Sakura 记忆系统服务 / Sakura Memory System Service

    管理 .sakura/ 目录下的项目记忆文件，提供四个核心功能：
    - initialize: 初始化 .sakura/ 目录
    - reflect: 审查后深度反思
    - consolidate: 合并反思更新知识文件
    - get_sakura_context: 读取上下文注入审查 prompt
    """

    def __init__(self):
        """初始化服务 / Initialize service"""
        self.write_service = get_github_write_service()
        settings = get_settings()
        if settings.sakura_use_summary_model:
            self.api_client = AIApiClient(
                base_url=settings.summary_api_base or settings.openai_api_base,
                api_key=settings.summary_api_key or settings.openai_api_key,
            )
            self._default_model = settings.summary_model or settings.openai_model
        else:
            self.api_client = AIApiClient(
                base_url=settings.openai_api_base,
                api_key=settings.openai_api_key,
            )
            self._default_model = settings.openai_model

    def _get_config(self) -> dict:
        """获取 sakura_memory 配置，优先使用 DB/WebUI 配置 / Get config, DB/WebUI overrides yaml"""
        ce_config = get_strategy_config().get_context_enhancement_config()
        yaml_config = ce_config.get("sakura_memory", {})
        settings = get_settings()

        reflection_model = settings.sakura_reflection_model or yaml_config.get(
            "reflection", {}
        ).get("model")
        consolidation_model = settings.sakura_consolidation_model or yaml_config.get(
            "consolidation", {}
        ).get("model")
        issue_reflection_model = (
            settings.sakura_issue_reflection_model
            or yaml_config.get("issue_reflection", {}).get("model")
        )

        return {
            "enabled": settings.sakura_memory_enabled,
            "reflection": {
                "enabled": settings.sakura_reflection_enabled,
                "model": reflection_model,
                "prompt_template": yaml_config.get("reflection", {}).get(
                    "prompt_template"
                ),
            },
            "issue_reflection": {
                "enabled": settings.sakura_issue_reflection_enabled,
                "model": issue_reflection_model,
                "prompt_template": yaml_config.get("issue_reflection", {}).get(
                    "prompt_template"
                ),
            },
            "consolidation": {
                "interval": settings.sakura_consolidation_interval,
                "model": consolidation_model,
                "max_memory_chars": settings.sakura_max_memory_chars,
                "max_sakura_chars": settings.sakura_max_sakura_chars,
                "cleanup_old_reflections": yaml_config.get("consolidation", {}).get(
                    "cleanup_old_reflections", False
                ),
                "partial_commit": yaml_config.get("consolidation", {}).get(
                    "partial_commit", False
                ),
            },
            "initialization": {
                "auto_init": settings.sakura_auto_init,
                "init_commit_message": yaml_config.get("initialization", {}).get(
                    "init_commit_message",
                    "chore: initialize .sakura/ directory for Sakura AI Reviewer",
                ),
            },
            "directory_convention": yaml_config.get("directory_convention", {}),
        }

    def _get_model(self, config_section: dict) -> str:
        """获取模型配置，null 表示使用默认审查模型

        Get model config, null means use default review model.
        """
        model = config_section.get("model")
        if model:
            return model
        return self._default_model

    async def _get_or_create_state(self, repo_full_name: str) -> SakuraMemoryState:
        """获取或创建仓库的记忆状态 / Get or create memory state for a repo"""
        from sqlalchemy import select

        async with async_session() as session:
            result = await session.execute(
                select(SakuraMemoryState).where(
                    SakuraMemoryState.repo_full_name == repo_full_name
                )
            )
            state = result.scalar_one_or_none()
            if not state:
                state = SakuraMemoryState(
                    repo_full_name=repo_full_name,
                    reflection_count=0,
                    is_initialized=False,
                    consolidation_interval=5,
                )
                session.add(state)
                try:
                    await session.commit()
                    await session.refresh(state)
                except Exception as e:
                    await session.rollback()
                    # Concurrent insert — re-query
                    logger.info(
                        "并发创建状态，重新查询 / Concurrent create, re-querying: {}", e
                    )
                    result = await session.execute(
                        select(SakuraMemoryState).where(
                            SakuraMemoryState.repo_full_name == repo_full_name
                        )
                    )
                    state = result.scalar_one_or_none()
            if state is None:
                raise RuntimeError(
                    f"Failed to get or create memory state for {repo_full_name}"
                )
            return state

    async def _update_state(self, repo_full_name: str, **kwargs) -> None:
        """更新仓库记忆状态 / Update memory state for a repo"""
        from sqlalchemy import select

        async with async_session() as session:
            result = await session.execute(
                select(SakuraMemoryState).where(
                    SakuraMemoryState.repo_full_name == repo_full_name
                )
            )
            state = result.scalar_one_or_none()
            if state:
                for key, value in kwargs.items():
                    setattr(state, key, value)
                await session.commit()

    async def initialize(
        self,
        repo,
        repo_full_name: str,
        prepare_only: bool = False,
    ) -> Optional[dict]:
        """初始化 .sakura/ 目录 / Initialize .sakura/ directory

        为新仓库自动创建 .sakura/ 目录，包含 SAKURA.md 和 memory.md。
        Auto-create .sakura/ directory for new repos with SAKURA.md and memory.md.

        Args:
            repo: PyGithub Repository 对象
            repo_full_name: 仓库完整名称 (owner/repo)
            prepare_only: 只收集文件不提交，返回文件字典供调用方合并提交

        Returns:
            prepare_only=True 时返回文件字典，否则返回 None
        """
        # 检查是否已初始化 / Check if already initialized
        state = await self._get_or_create_state(repo_full_name)
        if state.is_initialized:
            return {} if prepare_only else None

        config = self._get_config()
        init_config = config.get("initialization", {})
        if not init_config.get("auto_init", True):
            return {} if prepare_only else None

        try:
            # 检查核心文件是否已存在 / Check if core files already exist
            has_sakura_md = await self.write_service.file_exists(
                repo, ".sakura/SAKURA.md"
            )
            has_memory_md = await self.write_service.file_exists(
                repo, ".sakura/memory.md"
            )
            if has_sakura_md or has_memory_md:
                await self._update_state(repo_full_name, is_initialized=True)
                logger.info(
                    ".sakura/ 已初始化 (SAKURA.md={}, memory.md={}), 跳过: {}",
                    has_sakura_md,
                    has_memory_md,
                    repo_full_name,
                )
                return {} if prepare_only else None

            # 收集仓库信息 / Collect repo info
            logger.info(f"[sakura] 步骤1: 收集仓库信息 {repo_full_name}")
            languages = await asyncio.to_thread(lambda: dict(repo.get_languages()))
            lang_str = ", ".join(f"{k}: {v}" for k, v in languages.items())

            # 获取目录结构（前2层）/ Get directory structure (top 2 levels)
            logger.info(f"[sakura] 步骤2: 获取目录结构 {repo_full_name}")
            dir_structure = await self._get_directory_overview(repo)

            # 获取 README / Get README
            logger.info(f"[sakura] 步骤3: 获取README {repo_full_name}")
            readme_content = (
                await self.write_service.read_file(repo, "README.md") or "（无 README）"
            )

            # 生成初始 SAKURA.md / Generate initial SAKURA.md
            logger.info(f"[sakura] 步骤4: 生成SAKURA.md {repo_full_name}")
            prompt = INIT_PROMPT.format(
                repo_full_name=repo_full_name,
                languages=lang_str,
                directory_structure=dir_structure,
                readme_content=readme_content[:3000],
            )

            sakura_md = await self._call_llm(
                prompt, model=self._get_model(config.get("reflection", {}))
            )

            if not sakura_md:
                logger.warning(f"LLM 返回空内容，跳过初始化: {repo_full_name}")
                return {} if prepare_only else None

            # 创建初始文件 / Create initial files
            files = {
                ".sakura/SAKURA.md": sakura_md,
                ".sakura/memory.md": "# 项目记忆\n\n（首次初始化，暂无记忆）\n",
            }

            # 自动创建子目录占位文件 / Auto-create subdirectory placeholders
            dir_config = config.get("directory_convention", {})
            if dir_config.get("enabled", True) and dir_config.get(
                "auto_create_subdirs", True
            ):
                categories = dir_config.get("categories", {})
                for cat_name, cat_data in categories.items():
                    if cat_data.get("skip_placeholder", False):
                        continue
                    placeholder = cat_data.get("placeholder", "")
                    if placeholder:
                        files[f".sakura/{cat_name}/README.md"] = placeholder

            if prepare_only:
                logger.info(
                    "[sakura] prepare_only: {} init files for {}",
                    len(files),
                    repo_full_name,
                )
                return files

            commit_msg = init_config.get(
                "init_commit_message",
                "chore: initialize .sakura/ directory for Sakura AI Reviewer",
            )
            logger.info(
                f"[sakura] 步骤5: 提交文件到仓库 {repo_full_name}, {len(files)} 个文件"
            )
            await self.write_service.commit_files(repo, files, commit_msg)

            # 更新状态 / Update state
            logger.info(f"[sakura] 步骤6: 更新数据库状态 {repo_full_name}")
            await self._update_state(repo_full_name, is_initialized=True)
            logger.info(f"已初始化 .sakura/ 目录: {repo_full_name}")

        except Exception as e:
            import traceback

            logger.error(
                f"初始化 .sakura/ 失败 ({repo_full_name}): [{type(e).__name__}] {e}"
            )
            logger.error(f"完整堆栈:\n{traceback.format_exc()}")
            raise

    async def reflect(
        self,
        repo,
        repo_full_name: str,
        pr,
        review_result: dict,
        analysis,
        pr_info: dict = None,
        history_summary: str = None,
        review_id: int = None,
    ) -> None:
        """审查后反思 / Post-review reflection

        审查完成后，对审查结果进行深度反思并写入 .sakura/memory/ 目录。
        After review, reflect on results and write to .sakura/memory/ directory.

        Args:
            repo: PyGithub Repository 对象
            repo_full_name: 仓库完整名称
            pr: PyGithub PullRequest 对象
            review_result: 审查结果字典，包含 overall_score, decision, comments 等
            analysis: PR 分析结果对象，包含 code_files, strategy, is_incremental 等
            pr_info: PR webhook 信息字典，包含 action 等
            history_summary: 历史审查上下文摘要（增量审查时由 HistoryContextService 生成）
            review_id: 数据库审查记录 ID，用于从数据库读取准确的评论数据
        """
        config = self._get_config()
        if not config.get("enabled", True):
            return
        if not config.get("reflection", {}).get("enabled", True):
            return

        try:
            # 确保已初始化 / Ensure initialized
            state = await self._get_or_create_state(repo_full_name)
            init_files = {}
            needs_init = not state.is_initialized
            if needs_init:
                init_files = (
                    await self.initialize(
                        repo,
                        repo_full_name,
                        prepare_only=True,
                    )
                    or {}
                )

            # 从 PR 分支或 main 读取 memory.md / Read memory.md from PR branch or main
            sakura_ref = await self.write_service.get_sakura_branch(repo)
            current_memory = (
                await self.write_service.read_file(
                    repo,
                    ".sakura/memory.md",
                    ref=sakura_ref,
                )
                or ""
            )

            # 提取增量审查上下文 / Extract incremental review context
            pr_info = pr_info or {}
            action = pr_info.get("action", "")
            is_incremental = getattr(analysis, "is_incremental", False)
            new_commits = getattr(analysis, "new_commits", None) or []

            if action == "full_review":
                review_type = "手动全量重审（/full-review）"
                new_commits_summary = "全部文件重新审查"
                incremental_scope = "全量"
                incremental_reflection_prompt = (
                    "5. **手动全量重审反思**\n"
                    "   - 本次是用户手动触发的全量重审，之前可能已有增量审查\n"
                    "   - 重新全量审查是否发现了之前增量审查遗漏的问题？"
                )
            elif is_incremental:
                review_type = "增量审查"
                new_commits_summary = (
                    "\n".join(f"  - {c['sha']}: {c['title']}" for c in new_commits[:10])
                    or "无新增提交信息"
                )
                incremental_scope = f"新增 {len(new_commits)} 个提交"
                incremental_reflection_prompt = (
                    "5. **增量审查反思**\n"
                    "   - 新增提交是否解决了之前审查提出的问题？\n"
                    "   - 增量变更是否引入了新的风险？\n"
                    "   - 审查覆盖度是否因只看增量部分而有所下降？"
                )
            else:
                review_type = "首次全量审查"
                new_commits_summary = "无（首次审查）"
                incremental_scope = "全部变更"
                incremental_reflection_prompt = ""

            # 构建反思 Prompt / Build reflection prompt
            pr_number = getattr(pr, "number", 0)
            commit_sha = ""
            if hasattr(analysis, "code_files") and analysis.code_files:
                try:
                    commit_sha = getattr(pr.head, "sha", "")[:7]
                except Exception:
                    commit_sha = "unknown"

            changed_files = "\n".join(
                f"- {f.path} ({f.status}, +{f.additions}/-{f.deletions})"
                for f in (analysis.code_files or [])[:20]
            )

            # 优先从数据库读取准确的评论数据（severity 从 emoji 精确提取）
            if review_id is not None and isinstance(review_id, int) and review_id > 0:
                comments_summary = await self._fetch_comments_from_db(review_id)
            else:
                comments_summary = ""
                if "comments" in review_result:
                    comments_summary = "\n".join(
                        f"- [{c.get('severity', '?')}] {c.get('content', '')[:100]}"
                        for c in review_result["comments"][:10]
                    )

            def _escape_braces(s: str) -> str:
                return s.replace("{", "{{").replace("}", "}}")

            pr_description = ""
            try:
                pr_description = (getattr(pr, "body", None) or "")[:500]
            except Exception as e:
                logger.debug("读取 PR 描述失败，跳过: {}", e)

            prompt = REFLECTION_PROMPT.format(
                pr_number=pr_number,
                commit_sha=commit_sha,
                repo_full_name=repo_full_name,
                strategy=getattr(analysis, "strategy", "unknown"),
                score=review_result.get("overall_score", "N/A"),
                decision=review_result.get("decision", "unknown"),
                review_type=review_type,
                incremental_scope=incremental_scope,
                new_commits_summary=new_commits_summary,
                incremental_reflection_prompt=incremental_reflection_prompt,
                review_summary=review_result.get("summary", "无摘要"),
                comments_summary=comments_summary or "无评论",
                changed_files=changed_files or "无文件信息",
                current_memory=current_memory or "暂无记忆",
                pr_description=_escape_braces(pr_description) or "无描述",
                history_context=history_summary or "无历史审查记录（首次审查）",
            )

            # 生成反思内容 / Generate reflection content
            model = self._get_model(config.get("reflection", {}))
            reflection_content = await self._call_llm(prompt, model=model)

            # 格式化反思文件名 / Format reflection filename
            today = datetime.now().strftime("%Y-%m-%d")
            if is_incremental:
                round_num = await self._count_pr_reflections(repo, pr_number) + 1
                reflection_path = f".sakura/memory/{today}_PR{pr_number}_incr{round_num}_{commit_sha}.md"
            else:
                reflection_path = (
                    f".sakura/memory/{today}_PR{pr_number}_{commit_sha}.md"
                )

            # 提交反思文件 / Commit reflection file
            files = {reflection_path: reflection_content}
            commit_msg = f"chore(sakura): add reflection for PR#{pr_number}"
            if init_files:
                files.update(init_files)
                commit_msg = f"chore(sakura): initialize .sakura/ and add reflection for PR#{pr_number}"
                logger.info(
                    "[sakura] combining init ({}) + reflection into single commit for {}",
                    len(init_files),
                    repo_full_name,
                )
            await self.write_service.commit_files(repo, files, commit_msg)

            # 更新状态 / Update state
            new_count = state.reflection_count + 1
            state_updates = {"reflection_count": new_count}
            if needs_init:
                state_updates["is_initialized"] = True
            await self._update_state(repo_full_name, **state_updates)

            logger.info(
                "已写入反思: {} PR#{} [{}] (第{}次反思{})",
                repo_full_name,
                pr_number,
                review_type,
                new_count,
                ", 初始化完成" if needs_init else "",
            )

            # 检查是否需要合并 / Check if consolidation is needed
            consolidation_config = config.get("consolidation", {})
            interval = consolidation_config.get(
                "interval", state.consolidation_interval
            )
            since_last = new_count - (state.last_consolidation_count or 0)
            if since_last >= interval:
                await self.consolidate(repo, repo_full_name, new_count)

        except Exception as e:
            logger.error("反思失败 ({}): {}", repo_full_name, e, exc_info=True)

    async def _count_pr_reflections(self, repo, pr_number: int) -> int:
        """统计某 PR 已有的反思文件数 / Count existing reflection files for a PR"""
        try:
            contents = await asyncio.to_thread(
                lambda: repo.get_contents(".sakura/memory")
            )
            if not contents or not isinstance(contents, list):
                return 0
            return sum(1 for c in contents if f"_PR{pr_number}_" in c.name)
        except Exception:
            return 0

    async def reflect_issue(
        self,
        repo,
        repo_full_name: str,
        issue_number: int,
        issue_info: dict,
        analysis_result: dict,
        analysis_record,
    ) -> None:
        """Issue 分析后反思 / Post-issue-analysis reflection

        Issue 分析完成后，对分析结果进行深度反思并写入 .sakura/memory/ 目录。
        After issue analysis, reflect on results and write to .sakura/memory/ directory.

        Args:
            repo: PyGithub Repository 对象
            repo_full_name: 仓库完整名称
            issue_number: Issue 编号
            issue_info: Issue webhook 信息字典
            analysis_result: AI 分析结果字典
            analysis_record: IssueAnalysis ORM 对象
        """
        config = self._get_config()
        if not config.get("enabled", True):
            return
        if not config.get("issue_reflection", {}).get("enabled", True):
            return

        try:
            # 确保已初始化 / Ensure initialized
            state = await self._get_or_create_state(repo_full_name)
            init_files = {}
            needs_init = not state.is_initialized
            if needs_init:
                init_files = (
                    await self.initialize(
                        repo,
                        repo_full_name,
                        prepare_only=True,
                    )
                    or {}
                )

            # 读取 memory.md / Read memory.md
            sakura_ref = await self.write_service.get_sakura_branch(repo)
            current_memory = (
                await self.write_service.read_file(
                    repo,
                    ".sakura/memory.md",
                    ref=sakura_ref,
                )
                or ""
            )

            # 从 analysis_record 提取数据 / Extract data from analysis_record
            suggested_labels = analysis_record.suggested_labels or "无"
            try:
                labels_data = (
                    json.loads(suggested_labels)
                    if isinstance(suggested_labels, str)
                    else suggested_labels
                )
                if isinstance(labels_data, list):
                    formatted_labels = []
                    for label in labels_data[:10]:
                        if isinstance(label, dict):
                            name = label.get("name", label)
                            confidence = label.get("confidence", "N/A")
                            formatted_labels.append(f"- {name}（置信度: {confidence}）")
                        else:
                            formatted_labels.append(f"- {label}")
                    suggested_labels = "\n".join(formatted_labels)
            except (ValueError, TypeError):
                pass

            suggested_assignees = analysis_record.suggested_assignees or "无"
            try:
                assignees_data = (
                    json.loads(suggested_assignees)
                    if isinstance(suggested_assignees, str)
                    else suggested_assignees
                )
                if isinstance(assignees_data, list):
                    suggested_assignees = ", ".join(
                        a.get("username", a) if isinstance(a, dict) else str(a)
                        for a in assignees_data[:10]
                    )
            except (ValueError, TypeError):
                pass

            duplicate_of = analysis_record.duplicate_of
            duplicate_info = (
                f"可能是 #{duplicate_of} 的重复" if duplicate_of else "未检测到重复"
            )

            related_prs = analysis_record.related_prs or "无"
            try:
                prs_data = (
                    json.loads(related_prs)
                    if isinstance(related_prs, str)
                    else related_prs
                )
                if isinstance(prs_data, list):
                    related_prs = "\n".join(
                        f"- PR #{p.get('number', p)}: {p.get('title', '')}"
                        if isinstance(p, dict)
                        else f"- {p}"
                        for p in prs_data[:10]
                    )
            except (ValueError, TypeError):
                pass

            # 构建 Prompt / Build prompt
            def _escape_braces(s: str) -> str:
                return s.replace("{", "{{").replace("}", "}}")

            prompt = ISSUE_REFLECTION_PROMPT.format(
                issue_number=issue_number,
                repo_full_name=repo_full_name,
                author=issue_info.get("sender", {}).get("login", "unknown"),
                original_title=_escape_braces(issue_info.get("title", "")),
                original_body=_escape_braces((issue_info.get("body", "") or "")[:500]),
                category=analysis_result.get("category", "unknown"),
                priority=analysis_result.get("priority", "unknown"),
                feasibility=analysis_result.get("feasibility", "unknown"),
                summary=analysis_result.get("summary", "无摘要"),
                suggested_labels=suggested_labels,
                suggested_assignees=suggested_assignees,
                suggested_title=analysis_record.suggested_title or "无建议",
                duplicate_info=duplicate_info,
                related_prs=related_prs,
                current_memory=current_memory or "暂无记忆",
            )

            # 生成反思 / Generate reflection
            model = self._get_model(config.get("issue_reflection", {}))
            reflection_content = await self._call_llm(prompt, model=model)

            # 格式化文件名 / Format filename
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            short_hash = hashlib.md5(
                f"{repo_full_name}#{issue_number}#{now.isoformat()}".encode()
            ).hexdigest()[:7]
            reflection_path = (
                f".sakura/memory/{today}_ISSUE{issue_number}_{short_hash}.md"
            )

            # 提交 / Commit
            files = {reflection_path: reflection_content}
            commit_msg = f"chore(sakura): add reflection for Issue#{issue_number}"
            if init_files:
                files.update(init_files)
                commit_msg = f"chore(sakura): initialize .sakura/ and add reflection for Issue#{issue_number}"
                logger.info(
                    "[sakura] combining init ({}) + issue reflection into single commit for {}",
                    len(init_files),
                    repo_full_name,
                )
            await self.write_service.commit_files(repo, files, commit_msg)

            # 更新状态 / Update state
            new_count = state.reflection_count + 1
            state_updates = {"reflection_count": new_count}
            if needs_init:
                state_updates["is_initialized"] = True
            await self._update_state(repo_full_name, **state_updates)

            logger.info(
                "已写入 Issue 反思: {} Issue#{} (第{}次反思{})",
                repo_full_name,
                issue_number,
                new_count,
                ", 初始化完成" if needs_init else "",
            )

            # 检查合并触发 / Check consolidation trigger
            consolidation_config = config.get("consolidation", {})
            interval = consolidation_config.get(
                "interval", state.consolidation_interval
            )
            since_last = new_count - (state.last_consolidation_count or 0)
            if since_last >= interval:
                await self.consolidate(repo, repo_full_name, new_count)

        except Exception as e:
            logger.error("Issue 反思失败 ({}): {}", repo_full_name, e, exc_info=True)

    async def consolidate(self, repo, repo_full_name: str, total_count: int) -> None:
        """合并反思，更新 SAKURA.md 和 memory.md

        通过两次独立 Agent 会话分别更新两个知识文件，
        AI 通过工具自主读取反思、README 和当前文件，精确编辑目标文件。

        Args:
            repo: PyGithub Repository 对象
            repo_full_name: 仓库完整名称
            total_count: 当前累计反思次数
        """
        config = self._get_config()
        consolidation_config = config.get("consolidation", {})

        try:
            logger.info(
                "[consolidate] 开始合并记忆: {} (第{}次反思, interval={})",
                repo_full_name,
                total_count,
                consolidation_config.get("interval", 5),
            )

            sakura_ref = await self.write_service.get_sakura_branch(repo)

            # 获取上次合并后新增的反思文件名
            new_files = await self._get_new_reflection_files(
                repo, sakura_ref, consolidation_config
            )
            if not new_files:
                logger.warning("[consolidate] 未找到新增反思文件: {}", repo_full_name)
                return
            logger.info(
                "[consolidate] 新增 {} 个反思文件: {}",
                len(new_files),
                ", ".join(new_files[:5]) + ("..." if len(new_files) > 5 else ""),
            )

            # 获取仓库信息
            languages = await asyncio.to_thread(lambda: dict(repo.get_languages()))
            lang_str = ", ".join(f"{k}: {v}" for k, v in languages.items())

            max_sakura = consolidation_config.get("max_sakura_chars", 5000)
            max_memory = consolidation_config.get("max_memory_chars", 2000)
            model = self._get_model(consolidation_config)

            settings = get_settings()
            max_iterations = (
                settings.sakura_consolidation_max_iterations
                or consolidation_config.get("max_iterations", 20)
            )

            from backend.services.sakura_consolidation_agent import (
                SakuraConsolidationAgent,
            )

            agent = SakuraConsolidationAgent()
            files = {}

            # 串行处理 SAKURA.md
            sakura_changes = await agent.consolidate_file(
                repo=repo,
                repo_full_name=repo_full_name,
                sakura_ref=sakura_ref,
                target_file="SAKURA.md",
                new_reflection_files=new_files,
                total_reflections=total_count,
                max_chars=max_sakura,
                languages=lang_str,
                model=model,
                max_iterations=max_iterations,
            )
            for k, v in sakura_changes.items():
                clean = k.removeprefix(".sakura/").lstrip("/")
                files[f".sakura/{clean}"] = v

            # 串行处理 memory.md
            memory_changes = await agent.consolidate_file(
                repo=repo,
                repo_full_name=repo_full_name,
                sakura_ref=sakura_ref,
                target_file="memory.md",
                new_reflection_files=new_files,
                total_reflections=total_count,
                max_chars=max_memory,
                languages=lang_str,
                model=model,
                max_iterations=max_iterations,
            )
            for k, v in memory_changes.items():
                clean = k.removeprefix(".sakura/").lstrip("/")
                files[f".sakura/{clean}"] = v

            # 字符限制告警
            for path, content in files.items():
                fname = path.split("/")[-1]
                if fname == "SAKURA.md" and len(content) > max_sakura:
                    logger.warning(
                        "[consolidate] SAKURA.md 超出限制: {} > {}",
                        len(content),
                        max_sakura,
                    )
                elif fname == "memory.md" and len(content) > max_memory:
                    logger.warning(
                        "[consolidate] memory.md 超出限制: {} > {}",
                        len(content),
                        max_memory,
                    )

            # 提交更新
            if files:
                commit_msg = (
                    f"chore(sakura): consolidate memory (reflection #{total_count})"
                )
                await self.write_service.commit_files(repo, files, commit_msg)

                await self._update_state(
                    repo_full_name,
                    last_consolidation_at=datetime.now(timezone.utc),
                    last_consolidation_count=total_count,
                )

                logger.info(
                    "[consolidate] 合并完成: {} (第{}次反思后), 更新文件: {}",
                    repo_full_name,
                    total_count,
                    ", ".join(files.keys()),
                )

                await self._maybe_extract_knowledge(repo, repo_full_name, total_count)
            else:
                logger.warning(
                    "[consolidate] 两个文件均无变更: {}",
                    repo_full_name,
                )

        except Exception as e:
            logger.error(f"合并记忆失败 ({repo_full_name}): {e}", exc_info=True)

    async def _get_new_reflection_files(
        self, repo, sakura_ref: Optional[str], consolidation_config: dict
    ) -> list[str]:
        """获取上次合并后新增的反思文件名列表"""
        try:
            ref = sakura_ref or "HEAD"

            def _list():
                contents = repo.get_contents(".sakura/memory", ref=ref)
                if isinstance(contents, list):
                    return [
                        c.name
                        for c in contents
                        if hasattr(c, "name") and c.name.endswith(".md")
                    ]
                return []

            all_files = await asyncio.to_thread(_list)
            all_files.sort(reverse=True)

            # 取最近 interval 个文件作为本次新增
            interval = consolidation_config.get("interval", 5)
            return all_files[:interval]

        except Exception as e:
            logger.warning("[consolidate] 获取反思文件列表失败: {}", e)
            return []

    async def _maybe_extract_knowledge(
        self, repo, repo_full_name: str, reflection_count: int
    ) -> None:
        """检查是否需要触发一次性知识提取 / Check if one-time knowledge extraction is needed"""
        try:
            state = await self._get_or_create_state(repo_full_name)
            config = self._get_config()

            # 检查配置是否启用（settings 优先）
            from backend.core.config import get_settings

            settings = get_settings()
            if not settings.sakura_knowledge_extraction_enabled:
                return

            # 已提取过或反思数不足则跳过
            if state.knowledge_extracted:
                return
            min_reflections = settings.sakura_extraction_min_reflections or config.get(
                "knowledge_extraction", {}
            ).get("min_reflections", 10)
            if reflection_count < min_reflections:
                return

            logger.info(
                "[extract] 触发知识提取: {} ({}次反思)",
                repo_full_name,
                reflection_count,
            )
            await self.extract_and_save_knowledge(repo, repo_full_name)

        except Exception as e:
            logger.warning("[extract] 知识提取触发失败: {} - {}", repo_full_name, e)

    async def extract_and_save_knowledge(self, repo, repo_full_name: str) -> bool:
        """执行知识提取并保存到 .sakura/ 子目录

        Args:
            repo: PyGithub Repository 对象
            repo_full_name: 仓库完整名称

        Returns:
            是否提取成功
        """
        from backend.services.sakura_knowledge_extractor import (
            SakuraKnowledgeExtractor,
        )

        extractor = SakuraKnowledgeExtractor()
        config = self._get_config()
        model = config.get("consolidation", {}).get("model")
        sakura_ref = await self.write_service.get_sakura_branch(repo)

        extracted = await extractor.extract_knowledge(
            repo=repo,
            repo_full_name=repo_full_name,
            sakura_ref=sakura_ref,
            model=model,
        )

        if not extracted:
            logger.warning("[extract] 知识提取无结果: {}", repo_full_name)
            return False

        # 合并子目录前缀（AI 可能已带 .sakura/ 前缀，需去重）
        files = {}
        for k, v in extracted.items():
            clean = k.removeprefix(".sakura/").lstrip("/")
            files[f".sakura/{clean}"] = v

        commit_msg = "chore(sakura): extract structured knowledge from reflections"
        await self.write_service.commit_files(repo, files, commit_msg)

        await self._update_state(repo_full_name, knowledge_extracted=True)
        logger.info(
            "[extract] 知识提取完成: {}, 生成 {} 个文件",
            repo_full_name,
            len(files),
        )
        return True

    async def get_sakura_context(self, repo, repo_full_name: str) -> Dict[str, str]:
        """获取 SAKURA.md 和 memory.md 用于注入审查 prompt

        Get SAKURA.md and memory.md for review prompt injection.

        Args:
            repo: PyGithub Repository 对象
            repo_full_name: 仓库完整名称

        Returns:
            包含 sakura_md 和/或 memory_md 的字典，未启用时返回空字典
        """
        config = self._get_config()
        if not config.get("enabled", True):
            return {}

        try:
            state = await self._get_or_create_state(repo_full_name)
            if not state.is_initialized:
                return {}

            sakura_ref = await self.write_service.get_sakura_branch(repo)
            sakura_md = await self.write_service.read_file(
                repo,
                ".sakura/SAKURA.md",
                ref=sakura_ref,
            )
            memory_md = await self.write_service.read_file(
                repo,
                ".sakura/memory.md",
                ref=sakura_ref,
            )

            result = {}
            if sakura_md:
                result["sakura_md"] = sakura_md
            if memory_md:
                result["memory_md"] = memory_md

            return result

        except Exception as e:
            logger.warning("获取 .sakura/ 上下文失败 ({}): {}", repo_full_name, e)
            return {}

    async def _fetch_comments_from_db(self, review_id: int) -> str:
        """从数据库读取审查评论（severity 准确，来自 emoji 精确提取）

        Args:
            review_id: 数据库审查记录 ID

        Returns:
            格式化的评论摘要文本
        """
        from sqlalchemy import select

        from backend.models.database import ReviewComment

        async with async_session() as session:
            stmt = (
                select(ReviewComment)
                .where(ReviewComment.review_id == review_id)
                .order_by(ReviewComment.severity, ReviewComment.created_at)
            )
            result = await session.execute(stmt)
            comments = list(result.scalars().all())

        if not comments:
            return "无评论"

        lines = []
        for comment in comments[:20]:
            location = ""
            if comment.file_path:
                location = f" [{comment.file_path}"
                if comment.line_number:
                    location += f":{comment.line_number}"
                location += "]"
            lines.append(f"- [{comment.severity}]{location}: {comment.content[:150]}")
        return "\n".join(lines)

    async def _read_recent_reflections(
        self,
        repo,
        count: int,
        ref: Optional[str] = None,
    ) -> str:
        """读取最近的反思文件 / Read recent reflection files

        从 .sakura/memory/ 目录读取最近的反思文件并合并为文本。
        Read recent reflection files from .sakura/memory/ and merge into text.

        Args:
            repo: PyGithub Repository 对象
            count: 要读取的文件数量
            ref: 分支引用，默认 HEAD（main）

        Returns:
            合并后的反思内容字符串
        """
        try:
            contents = await asyncio.to_thread(
                lambda: repo.get_contents(".sakura/memory", ref=ref or "HEAD")
            )
            if not contents:
                return ""

            # GitHub Contents API 对大目录可能截断(1000文件上限)，回退到 Git Trees API
            if isinstance(contents, list) and len(contents) >= 1000:
                logger.warning(
                    "[_read_recent_reflections] 目录可能被截断 ({} 个文件), "
                    "尝试 Git Trees API",
                    len(contents),
                )
                try:
                    tree = await asyncio.to_thread(
                        functools.partial(repo.get_git_tree, "HEAD", recursive=True)
                    )
                    memory_entries = [
                        e
                        for e in tree.tree
                        if e.path.startswith(".sakura/memory/")
                        and e.path.endswith(".md")
                    ]

                    class _TreeEntry:
                        __slots__ = ("name",)

                        def __init__(self, name: str):
                            self.name = name

                    contents = [
                        _TreeEntry(e.path.split("/")[-1]) for e in memory_entries
                    ]
                except Exception as tree_err:
                    logger.warning(
                        "[_read_recent_reflections] Git Trees API 失败: {}",
                        tree_err,
                    )

            # 过滤 .md 文件并按文件名排序（文件名包含日期）
            # Filter .md files and sort by name (which contains date)
            md_files = [c for c in contents if c.name.endswith(".md")]
            md_files.sort(key=lambda c: c.name, reverse=True)
            recent = md_files[:count]

            reflections = []
            for f in recent:
                content = await self.write_service.read_file(
                    repo, f".sakura/memory/{f.name}"
                )
                if content:
                    reflections.append(f"### {f.name}\n\n{content}")

            return "\n\n---\n\n".join(reflections)

        except Exception as e:
            logger.warning("读取反思文件失败: {}", e)
            return ""

    async def _get_directory_overview(self, repo) -> str:
        """获取目录结构概览 / Get directory overview

        获取仓库前两层目录结构用于初始化时分析项目架构。
        Get top 2 levels of directory structure for project architecture analysis.

        Args:
            repo: PyGithub Repository 对象

        Returns:
            目录结构文本
        """
        try:

            def _list_dir(path=""):
                items = repo.get_contents(path)
                lines = []
                for item in items[:30]:
                    if item.type == "dir":
                        lines.append(f"  {item.name}/")
                        try:
                            sub = repo.get_contents(item.path)
                            for s in sub[:10]:
                                icon = "/" if s.type == "dir" else ""
                                lines.append(f"    {s.name}{icon}")
                        except Exception as e:
                            logger.debug(
                                "读取 .sakura/ 子目录失败，跳过 {}: {}", item.path, e
                            )
                    else:
                        lines.append(f"  {item.name}")
                return "\n".join(lines)

            return await asyncio.to_thread(_list_dir)
        except Exception as e:
            return f"（无法获取目录结构: {e}）"

    async def _call_llm(self, prompt: str, model: Optional[str] = None) -> str:
        """调用 LLM 生成文本 / Call LLM to generate text

        Args:
            prompt: 完整的 Prompt 文本
            model: 模型名称，为空时使用默认审查模型

        Returns:
            LLM 生成的文本内容
        """
        messages = [{"role": "user", "content": prompt}]
        response = await self.api_client.call_with_retry(
            messages=messages,
            model=model or self._default_model,
            temperature=0.7,
            max_tokens=4000,
        )
        if not response.choices:
            logger.warning("LLM 返回空响应 / LLM returned empty choices")
            return ""
        content = response.choices[0].message.content
        return content or ""

    @staticmethod
    def _clean_llm_output(response: str | None) -> str:
        """清理 LLM 输出：去除代码块标记和前后说明文字

        Args:
            response: LLM 原始响应

        Returns:
            清理后的纯 Markdown 内容
        """
        if not response:
            return ""
        content = response.strip()
        # 去除 markdown 代码块包裹
        if content.startswith("```markdown") and content.endswith("```"):
            content = content[len("```markdown") : -len("```")].strip()
        elif content.startswith("```md") and content.endswith("```"):
            content = content[len("```md") : -len("```")].strip()
        elif content.startswith("```") and content.endswith("```"):
            content = content[3:-3].strip()
        return content


# Singleton / 单例
_sakura_memory_service: Optional[SakuraMemoryService] = None


def get_sakura_memory_service() -> SakuraMemoryService:
    """获取 Sakura 记忆服务单例 / Get Sakura memory service singleton"""
    global _sakura_memory_service
    if _sakura_memory_service is None:
        _sakura_memory_service = SakuraMemoryService()
    return _sakura_memory_service
