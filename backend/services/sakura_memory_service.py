"""Sakura 记忆系统服务 / Sakura Memory System Service

负责管理 .sakura/ 目录的记忆系统：
- 自动初始化 .sakura/ 目录 / Auto-initialize .sakura/ directory
- 审查后深度反思 / Post-review deep reflection
- 定期合并更新 SAKURA.md 和 memory.md / Periodically consolidate and update files
- 读取上下文注入审查 prompt / Read context for review prompt injection
"""

import asyncio
import re
from datetime import datetime
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

请用中文输出，保持简洁但深入。"""

# Consolidation prompt template / 合并 Prompt 模板
CONSOLIDATION_PROMPT = """你是一个项目知识管理助手。请根据最近的审查反思，更新项目的知识文件。

## 最近的审查反思
{reflections}

## 当前 SAKURA.md 内容
{current_sakura_md}

## 当前 memory.md 内容
{current_memory_md}

## 仓库信息
- 仓库名: {repo_full_name}
- 语言统计: {languages}
- 累计反思次数: {total_reflections}

请生成更新后的两个文件：

### SAKURA.md（项目概述，最大 {max_sakura_chars} 字符）
应包含：
- 项目简介和技术栈
- 架构设计和关键决策
- 已知问题和注意事项
- 审查中发现的重要模式
- 团队约定和规范

### memory.md（精炼记忆，最大 {max_memory_chars} 字符）
应包含：
- 常见代码问题和审查要点
- 近期审查模式总结
- 规范建议和经验教训
- 需要特别关注的领域

请按以下格式输出（不要包含 markdown 代码块标记）：

<<<SAKURA_MD_START>>>
{更新后的 SAKURA.md 内容}
<<<SAKURA_MD_END>>>

<<<MEMORY_MD_START>>>
{更新后的 memory.md 内容}
<<<MEMORY_MD_END>>>
"""

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
        # 使用主模型配置创建 API 客户端 / Use main model config for API client
        self.api_client = AIApiClient(
            base_url=settings.openai_api_base,
            api_key=settings.openai_api_key,
        )

    def _get_config(self) -> dict:
        """获取 sakura_memory 配置，优先使用 DB/WebUI 配置 / Get config, DB/WebUI overrides yaml"""
        ce_config = get_strategy_config().get_context_enhancement_config()
        yaml_config = ce_config.get("sakura_memory", {})
        settings = get_settings()

        reflection_model = settings.sakura_reflection_model or yaml_config.get("reflection", {}).get("model")
        consolidation_model = settings.sakura_consolidation_model or yaml_config.get("consolidation", {}).get("model")

        return {
            "enabled": settings.sakura_memory_enabled,
            "reflection": {
                "enabled": settings.sakura_reflection_enabled,
                "model": reflection_model,
                "prompt_template": yaml_config.get("reflection", {}).get("prompt_template"),
            },
            "consolidation": {
                "interval": settings.sakura_consolidation_interval,
                "model": consolidation_model,
                "max_memory_chars": settings.sakura_max_memory_chars,
                "max_sakura_chars": settings.sakura_max_sakura_chars,
                "cleanup_old_reflections": yaml_config.get("consolidation", {}).get("cleanup_old_reflections", False),
            },
            "initialization": {
                "auto_init": settings.sakura_auto_init,
                "init_commit_message": yaml_config.get("initialization", {}).get(
                    "init_commit_message",
                    "chore: initialize .sakura/ directory for Sakura AI Reviewer"
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
        return get_settings().openai_model

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
                    logger.debug(f"并发创建状态，重新查询 / Concurrent create, re-querying: {e}")
                    result = await session.execute(
                        select(SakuraMemoryState).where(
                            SakuraMemoryState.repo_full_name == repo_full_name
                        )
                    )
                    state = result.scalar_one_or_none()
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

    async def initialize(self, repo, repo_full_name: str) -> None:
        """初始化 .sakura/ 目录 / Initialize .sakura/ directory

        为新仓库自动创建 .sakura/ 目录，包含 SAKURA.md 和 memory.md。
        Auto-create .sakura/ directory for new repos with SAKURA.md and memory.md.

        Args:
            repo: PyGithub Repository 对象
            repo_full_name: 仓库完整名称 (owner/repo)
        """
        # 检查是否已初始化 / Check if already initialized
        state = await self._get_or_create_state(repo_full_name)
        if state.is_initialized:
            return

        config = self._get_config()
        init_config = config.get("initialization", {})
        if not init_config.get("auto_init", True):
            return

        try:
            # 检查 .sakura/ 是否已存在 / Check if .sakura/ already exists
            if await self.write_service.file_exists(repo, ".sakura"):
                # .sakura exists, just mark as initialized
                await self._update_state(repo_full_name, is_initialized=True)
                logger.info(f".sakura/ 已存在，标记为已初始化: {repo_full_name}")
                return

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
                return

            # 创建初始文件 / Create initial files
            files = {
                ".sakura/SAKURA.md": sakura_md,
                ".sakura/memory.md": "# 项目记忆\n\n（首次初始化，暂无记忆）\n",
            }

            commit_msg = init_config.get(
                "init_commit_message",
                "chore: initialize .sakura/ directory for Sakura AI Reviewer",
            )
            logger.info(f"[sakura] 步骤5: 提交文件到仓库 {repo_full_name}, {len(files)} 个文件")
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
    ) -> None:
        """审查后反思 / Post-review reflection

        审查完成后，对审查结果进行深度反思并写入 .sakura/memory/ 目录。
        After review, reflect on results and write to .sakura/memory/ directory.

        Args:
            repo: PyGithub Repository 对象
            repo_full_name: 仓库完整名称
            pr: PyGithub PullRequest 对象
            review_result: 审查结果字典，包含 overall_score, decision, comments 等
            analysis: PR 分析结果对象，包含 code_files, strategy 等
        """
        config = self._get_config()
        if not config.get("enabled", True):
            return
        if not config.get("reflection", {}).get("enabled", True):
            return

        try:
            # 确保已初始化 / Ensure initialized
            state = await self._get_or_create_state(repo_full_name)
            if not state.is_initialized:
                await self.initialize(repo, repo_full_name)

            # 读取当前 memory.md / Read current memory.md
            current_memory = (
                await self.write_service.read_file(repo, ".sakura/memory.md") or ""
            )

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

            comments_summary = review_result.get("review_summary", "")
            if not comments_summary and "comments" in review_result:
                comments_summary = "\n".join(
                    f"- [{c.get('severity', '?')}] {c.get('content', '')[:100]}"
                    for c in review_result["comments"][:10]
                )

            pr_description = ""
            try:
                pr_description = (getattr(pr, "body", None) or "")[:500]
            except Exception:
                pass

            prompt = REFLECTION_PROMPT.format(
                pr_number=pr_number,
                commit_sha=commit_sha,
                repo_full_name=repo_full_name,
                strategy=getattr(analysis, "strategy", "unknown"),
                score=review_result.get("overall_score", "N/A"),
                decision=review_result.get("decision", "unknown"),
                review_summary=review_result.get("review_summary", "无摘要"),
                comments_summary=comments_summary or "无评论",
                changed_files=changed_files or "无文件信息",
                current_memory=current_memory or "暂无记忆",
                pr_description=pr_description or "无描述",
            )

            # 生成反思内容 / Generate reflection content
            model = self._get_model(config.get("reflection", {}))
            reflection_content = await self._call_llm(prompt, model=model)

            # 格式化反思文件名: YYYY-MM-DD_PR{N}_{sha}.md
            today = datetime.now().strftime("%Y-%m-%d")
            reflection_path = (
                f".sakura/memory/{today}_PR{pr_number}_{commit_sha}.md"
            )

            # 提交反思文件 / Commit reflection file
            files = {reflection_path: reflection_content}
            commit_msg = f"chore(sakura): add reflection for PR#{pr_number}"
            await self.write_service.commit_files(repo, files, commit_msg)

            # 更新反思计数 / Update reflection count
            new_count = state.reflection_count + 1
            await self._update_state(repo_full_name, reflection_count=new_count)

            logger.info(
                f"已写入反思: {repo_full_name} PR#{pr_number} (第{new_count}次反思)"
            )

            # 检查是否需要合并 / Check if consolidation is needed
            consolidation_config = config.get("consolidation", {})
            interval = consolidation_config.get(
                "interval", state.consolidation_interval
            )
            if new_count % interval == 0:
                await self.consolidate(repo, repo_full_name, new_count)

        except Exception as e:
            logger.error(f"反思失败 ({repo_full_name}): {e}", exc_info=True)

    async def consolidate(
        self, repo, repo_full_name: str, total_count: int
    ) -> None:
        """合并反思，更新 SAKURA.md 和 memory.md / Consolidate reflections

        读取最近的反思文件，通过 LLM 合并更新知识文件。
        Read recent reflections, merge via LLM to update knowledge files.

        Args:
            repo: PyGithub Repository 对象
            repo_full_name: 仓库完整名称
            total_count: 当前累计反思次数
        """
        config = self._get_config()
        consolidation_config = config.get("consolidation", {})

        try:
            # 读取最近的反思 / Read recent reflections
            reflections = await self._read_recent_reflections(
                repo, consolidation_config.get("interval", 5)
            )
            if not reflections:
                logger.warning(f"未找到反思文件: {repo_full_name}")
                return

            # 读取当前文件 / Read current files
            current_sakura = (
                await self.write_service.read_file(repo, ".sakura/SAKURA.md") or ""
            )
            current_memory = (
                await self.write_service.read_file(repo, ".sakura/memory.md") or ""
            )

            # 获取仓库信息 / Get repo info
            languages = await asyncio.to_thread(lambda: dict(repo.get_languages()))
            lang_str = ", ".join(f"{k}: {v}" for k, v in languages.items())

            max_sakura = consolidation_config.get("max_sakura_chars", 5000)
            max_memory = consolidation_config.get("max_memory_chars", 2000)

            prompt = CONSOLIDATION_PROMPT.format(
                reflections=reflections,
                current_sakura_md=current_sakura or "（空文件）",
                current_memory_md=current_memory or "（空文件）",
                repo_full_name=repo_full_name,
                languages=lang_str,
                total_reflections=total_count,
                max_sakura_chars=max_sakura,
                max_memory_chars=max_memory,
            )

            # 生成合并内容 / Generate consolidated content
            model = self._get_model(consolidation_config)
            response = await self._call_llm(prompt, model=model)

            # 解析响应 / Parse the response
            sakura_md, memory_md = self._parse_consolidation_response(response)

            if not sakura_md and not memory_md:
                logger.warning(f"合并解析失败: {repo_full_name}")
                return

            # 截断到最大字符数 / Truncate to max chars
            if sakura_md and len(sakura_md) > max_sakura:
                sakura_md = sakura_md[:max_sakura] + "\n\n...（已截断）"
            if memory_md and len(memory_md) > max_memory:
                memory_md = memory_md[:max_memory] + "\n\n...（已截断）"

            # 提交更新 / Commit updates
            files = {}
            if sakura_md:
                files[".sakura/SAKURA.md"] = sakura_md
            if memory_md:
                files[".sakura/memory.md"] = memory_md

            if files:
                commit_msg = (
                    f"chore(sakura): consolidate memory (reflection #{total_count})"
                )
                await self.write_service.commit_files(repo, files, commit_msg)

                await self._update_state(
                    repo_full_name,
                    last_consolidation_at=datetime.utcnow(),
                )

                logger.info(
                    f"已合并记忆: {repo_full_name} (第{total_count}次反思后)"
                )

        except Exception as e:
            logger.error(
                f"合并记忆失败 ({repo_full_name}): {e}", exc_info=True
            )

    async def get_sakura_context(
        self, repo, repo_full_name: str
    ) -> Dict[str, str]:
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

            sakura_md = await self.write_service.read_file(
                repo, ".sakura/SAKURA.md"
            )
            memory_md = await self.write_service.read_file(
                repo, ".sakura/memory.md"
            )

            result = {}
            if sakura_md:
                result["sakura_md"] = sakura_md
            if memory_md:
                result["memory_md"] = memory_md

            return result

        except Exception as e:
            logger.warning(f"获取 .sakura/ 上下文失败 ({repo_full_name}): {e}")
            return {}

    async def _read_recent_reflections(self, repo, count: int) -> str:
        """读取最近的反思文件 / Read recent reflection files

        从 .sakura/memory/ 目录读取最近的反思文件并合并为文本。
        Read recent reflection files from .sakura/memory/ and merge into text.

        Args:
            repo: PyGithub Repository 对象
            count: 要读取的文件数量

        Returns:
            合并后的反思内容字符串
        """
        try:
            contents = await asyncio.to_thread(
                lambda: repo.get_contents(".sakura/memory")
            )
            if not contents:
                return ""

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
            logger.warning(f"读取反思文件失败: {e}")
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
                        except Exception:
                            pass
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
            model=model or get_settings().openai_model,
            temperature=0.7,
            max_tokens=4000,
        )
        if not response.choices:
            logger.warning("LLM 返回空响应 / LLM returned empty choices")
            return ""
        content = response.choices[0].message.content
        return content or ""

    def _parse_consolidation_response(self, response: str) -> tuple[str, str]:
        """解析合并响应 / Parse consolidation response

        尝试用标记提取，回退到按标题分割，最后使用整段响应作为 SAKURA.md。
        Try marker extraction, fall back to header splitting, then use full response.

        Args:
            response: LLM 生成的原始响应文本

        Returns:
            (sakura_md, memory_md) 元组
        """
        sakura_md = ""
        memory_md = ""

        # 尝试使用标记提取 / Try to extract using markers
        sakura_match = re.search(
            r"<<<SAKURA_MD_START>>>(.*?)<<<SAKURA_MD_END>>>",
            response,
            re.DOTALL,
        )
        memory_match = re.search(
            r"<<<MEMORY_MD_START>>>(.*?)<<<MEMORY_MD_END>>>",
            response,
            re.DOTALL,
        )

        if sakura_match:
            sakura_md = sakura_match.group(1).strip()
        if memory_match:
            memory_md = memory_match.group(1).strip()

        # 回退: 如果标记未找到，尝试按标题分割
        # Fallback: if markers not found, try to split by headers
        if not sakura_md and not memory_md:
            lower_response = response.lower()
            if "### memory.md" in lower_response:
                parts = response.split("### memory.md")
            elif "## memory.md" in lower_response:
                parts = response.split("## memory.md")
            else:
                parts = [response]

            if len(parts) == 2:
                sakura_md = parts[0].strip()
                memory_md = parts[1].strip()
            else:
                # 最后手段: 使用整个响应作为 SAKURA.md
                # Last resort: use the whole response as SAKURA.md
                sakura_md = response.strip()

        return sakura_md, memory_md


# Singleton / 单例
_sakura_memory_service: Optional[SakuraMemoryService] = None


def get_sakura_memory_service() -> SakuraMemoryService:
    """获取 Sakura 记忆服务单例 / Get Sakura memory service singleton"""
    global _sakura_memory_service
    if _sakura_memory_service is None:
        _sakura_memory_service = SakuraMemoryService()
    return _sakura_memory_service
