""".sakura/ 知识提取服务

单次 Agent 会话，AI 通过工具直接读取反思、编辑和新建知识文件。
无截断，无硬编码限制，所有参数可配置。
"""


from loguru import logger

from backend.core.config import get_settings, get_strategy_config
from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.sakura_agent_base import (
    EDIT_FILE_TOOL,
    LIST_DIRECTORY_TOOL,
    READ_FILE_TOOL,
    WRITE_FILE_TOOL,
    SakuraAgentBase,
)

# ── 工具定义 ────────────────────────────────────────────────────────────

EXTRACTION_TOOLS = [
    LIST_DIRECTORY_TOOL,
    READ_FILE_TOOL,
    WRITE_FILE_TOOL,
    EDIT_FILE_TOOL,
]

# 禁止写入的路径前缀（反思文件由系统管理）
_WRITE_BLOCKED_PREFIXES = ("memory/",)


# ── 统一 system prompt ─────────────────────────────────────────────────

EXTRACT_KNOWLEDGE_SYSTEM = """你是一个项目知识管理助手。你的任务是从审查反思中提取结构化知识，并直接编辑知识文件。

## 可用工具
- list_directory(path): 查看 .sakura/ 下的目录结构
- read_file(file_path): 读取任何文件的完整内容
- write_file(file_path, content): 创建或覆盖文件
- edit_file(file_path, old_text, new_text): 精确替换文件中的文本

## 工作流程
1. 先 list_directory 了解 .sakura/ 目录结构
2. 读取 memory/ 下的反思文件了解项目审查历史
3. 查看已有的 rules/、docs/、plans/ 文件
4. 根据反思内容，用 write_file 创建新文件或 edit_file 更新已有文件
5. 确认所有文件编辑完成后，输出总结即可，不再调用工具

## 知识分类
- rules/: 审查规则、编码规范、项目约定
- docs/: 架构文档、设计决策、技术栈信息
- plans/: 经验教训、常见问题模式、开发计划

## 硬性约束
- 每个文件不得超过 {max_chars} 字符（含标点和空格）
- 优先提取最有价值的内容，宁缺毋滥
- 使用清晰的 Markdown 格式
- 不可修改 memory/ 目录下的反思文件
- 你可以自由创建新的分类文件，不限于上述三个目录"""

# ── 用户提示词模板 ─────────────────────────────────────────────────────

EXTRACT_KNOWLEDGE_USER_PROMPT = (
    "请对仓库 {repo_full_name} 执行知识提取。\n"
    "该仓库已有 {reflection_count} 次审查反思。\n"
    "请先查看 .sakura/ 目录结构，阅读反思文件，"
    "然后将可复用的知识整理到 rules/、docs/、plans/ 目录下。"
)


class SakuraKnowledgeExtractor(SakuraAgentBase):
    """从反思文件中提取结构化知识

    单次 Agent 会话，AI 通过工具直接读取、编辑、新建文件。
    所有变更缓存在内存中，最终统一返回。
    """

    log_prefix = "[extract]"

    def _get_config(self) -> dict:
        ce_config = get_strategy_config().get_context_enhancement_config()
        return ce_config.get("sakura_memory", {})

    def _get_tools(self) -> list:
        return EXTRACTION_TOOLS

    def _check_write_allowed(self, rel_path: str) -> str | None:
        if not rel_path or "../" in rel_path:
            return "无效文件路径"
        if any(rel_path.startswith(p) for p in _WRITE_BLOCKED_PREFIXES):
            return f"不允许写入: {rel_path}（memory/ 目录下的反思文件由系统管理）"
        return None

    def _ensure_client(self):
        if self._api_client is not None:
            return

        settings = get_settings()
        provider = settings.sakura_extraction_provider

        if provider == "custom":
            base_url = settings.sakura_extraction_api_base or settings.openai_api_base
            api_key = settings.sakura_extraction_api_key or settings.openai_api_key
        elif provider == "summary":
            base_url = settings.summary_api_base or settings.openai_api_base
            api_key = settings.summary_api_key or settings.openai_api_key
        else:
            base_url = settings.openai_api_base
            api_key = settings.openai_api_key

        self._api_client = AIApiClient(base_url=base_url, api_key=api_key)

        model = settings.sakura_extraction_model
        if not model:
            if provider == "summary":
                model = settings.summary_model or settings.openai_model
            else:
                model = settings.openai_model
        self._default_model = model

    async def extract_knowledge(
        self,
        repo,
        repo_full_name: str,
        sakura_ref: str | None = None,
        model: str | None = None,
        reflection_count: int = 0,
    ) -> dict[str, str]:
        """运行知识提取 Agent 会话

        Returns:
            相对于 .sakura/ 的文件路径到新内容的映射（仅包含变更文件）
        """
        config = self._get_config()
        ext_config = config.get("knowledge_extraction", {})
        consolidation_config = config.get("consolidation", {})

        settings = get_settings()
        max_iterations = settings.sakura_extraction_max_iterations or ext_config.get(
            "max_iterations", 15
        )
        max_chars = consolidation_config.get("max_sakura_chars", 5000)

        self._repo = repo
        self._sakura_ref = sakura_ref
        self._file_cache = {}
        self._modified_files = set()
        self._ensure_client()

        effective_model = model or self._default_model
        system_prompt = EXTRACT_KNOWLEDGE_SYSTEM.format(max_chars=max_chars)

        # Build initial user message to provide repo-specific context for the AI
        user_message = EXTRACT_KNOWLEDGE_USER_PROMPT.format(
            repo_full_name=repo_full_name,
            reflection_count=reflection_count,
        )

        logger.info(
            "[extract] 开始提取: {}, model={}, max_iterations={}",
            repo_full_name,
            effective_model,
            max_iterations,
        )

        await self._run_agent_conversation(
            system_prompt,
            effective_model,
            max_iterations,
            initial_user_message=user_message,
        )

        changes = self._collect_changes()

        if not changes:
            logger.warning("[extract] 无文件变更: {}", repo_full_name)
        else:
            logger.info(
                "[extract] 提取完成: {}, 变更 {} 个文件: {}",
                repo_full_name,
                len(changes),
                ", ".join(changes.keys()),
            )
        return changes
