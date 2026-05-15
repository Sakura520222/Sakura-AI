"""Sakura 记忆合并 Agent

通过工具调用让 AI 自主读取反思、README、当前文件，
然后通过 edit_file/write_file 精确修改 SAKURA.md 或 memory.md。
每次会话只处理一个目标文件，两次串行会话分别处理两个文件。
"""

from typing import Dict, List, Optional

from loguru import logger

from backend.core.config import get_settings
from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.sakura_agent_base import (
    EDIT_FILE_TOOL,
    LIST_DIRECTORY_TOOL,
    READ_FILE_TOOL,
    WRITE_FILE_TOOL,
    SakuraAgentBase,
)


# ── consolidation 专有工具 ─────────────────────────────────────────────

READ_README_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "read_readme",
        "description": "读取仓库根目录的 README.md 内容，了解项目背景和技术栈",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

CONSOLIDATION_TOOLS = [
    LIST_DIRECTORY_TOOL,
    READ_FILE_TOOL,
    READ_README_TOOL,
    WRITE_FILE_TOOL,
    EDIT_FILE_TOOL,
]

# 只允许写入这两个文件
_WRITABLE_FILES = {"SAKURA.md", "memory.md"}


# ── System Prompt 模板 ─────────────────────────────────────────────────

SAKURA_SYSTEM_TEMPLATE = """你是项目知识管理助手。你的任务是根据最新的审查反思，更新项目的 SAKURA.md 概述文件。

## 可用工具
- list_directory(path): 查看 .sakura/ 下的目录结构
- read_file(file_path): 读取 .sakura/ 下的任何文件（反思、SAKURA.md、memory.md 等）
- read_readme(): 读取仓库根目录的 README.md
- write_file(file_path, content): 创建或覆盖 SAKURA.md
- edit_file(file_path, old_text, new_text): 精确替换 SAKURA.md 中的文本

## 本次新增的反思文件（建议重点阅读这些）
{new_reflection_files}

## 仓库信息
- 仓库名: {repo_full_name}
- 语言统计: {languages}
- 累计反思次数: {total_reflections}（此数值已精确计算，必须原样使用，禁止自行加减或重算）

## 工作流程
1. 先 read_readme 了解项目背景
2. read_file 读取当前 SAKURA.md
3. 读取本次新增的反思文件
4. 根据反思内容，用 edit_file 局部更新或 write_file 全量更新 SAKURA.md
5. 确认更新完成后输出总结

## 硬性约束
- 输出内容不得超过 {max_chars} 字符（含标点和空格）
- 必须保留累计反思次数标记：累计反思 {total_reflections} 次
- 只能编辑 SAKURA.md，不能修改其他文件
- 如当前内容已接近上限，优先保留最重要的信息，主动删除过时或重复内容"""

MEMORY_SYSTEM_TEMPLATE = """你是代码审查经验总结助手。你的任务是根据最新的审查反思，更新项目的 memory.md 记忆文件。

## 可用工具
- list_directory(path): 查看 .sakura/ 下的目录结构
- read_file(file_path): 读取 .sakura/ 下的任何文件
- read_readme(): 读取仓库根目录的 README.md
- write_file(file_path, content): 创建或覆盖 memory.md
- edit_file(file_path, old_text, new_text): 精确替换 memory.md 中的文本

## 本次新增的反思文件（建议重点阅读这些）
{new_reflection_files}

## 仓库信息
- 仓库名: {repo_full_name}
- 累计反思次数: {total_reflections}（此数值已精确计算，必须原样使用，禁止自行加减或重算）

## 工作流程
1. read_file 读取当前 memory.md
2. 读取本次新增的反思文件
3. 根据反思内容，用 edit_file 局部更新或 write_file 全量更新 memory.md
4. 确认更新完成后输出总结

## 硬性约束
- 输出内容不得超过 {max_chars} 字符（含标点和空格）
- 必须保留累计反思次数标记：累计反思 {total_reflections} 次
- 只能编辑 memory.md，不能修改其他文件
- 如当前内容已接近上限，优先保留最重要的经验，主动删除过时或重复内容"""


class SakuraConsolidationAgent(SakuraAgentBase):
    """记忆合并 Agent，通过工具调用让 AI 精确编辑目标文件"""

    log_prefix = "[consolidate-agent]"

    def _get_tools(self) -> list:
        return CONSOLIDATION_TOOLS

    def _check_write_allowed(self, rel_path: str) -> Optional[str]:
        if rel_path not in _WRITABLE_FILES:
            return f"不允许写入: {rel_path}（只能编辑 {', '.join(_WRITABLE_FILES)}）"
        return None

    def _ensure_client(self):
        if self._api_client is not None:
            return

        settings = get_settings()
        use_summary = getattr(settings, "sakura_use_summary_model", False)

        if use_summary:
            base_url = settings.summary_api_base or settings.openai_api_base
            api_key = settings.summary_api_key or settings.openai_api_key
        else:
            base_url = settings.openai_api_base
            api_key = settings.openai_api_key

        self._api_client = AIApiClient(base_url=base_url, api_key=api_key)

        model = getattr(settings, "sakura_consolidation_model", "")
        if not model:
            model = (
                settings.summary_model or settings.openai_model
                if use_summary
                else settings.openai_model
            )
        self._default_model = model

    async def _execute_extra_tool(self, name: str, args: dict) -> Optional[str]:
        if name == "read_readme":
            return await self._tool_read_readme()
        return None

    async def _tool_read_readme(self) -> str:
        try:
            ref = self._sakura_ref or "HEAD"
            for name in ("README.md", "README.MD", "README", "readme.md"):
                content = await self._read_from_github_raw(name, ref)
                if content:
                    return content
            return "(仓库无 README 文件)"
        except Exception as e:
            return f"(读取 README 失败: {e})"

    async def consolidate_file(
        self,
        repo,
        repo_full_name: str,
        sakura_ref: Optional[str],
        target_file: str,
        new_reflection_files: List[str],
        total_reflections: int,
        max_chars: int,
        languages: str,
        model: Optional[str] = None,
        max_iterations: int = 20,
    ) -> Dict[str, str]:
        """运行一次 Agent 会话，合并单个目标文件"""
        self._repo = repo
        self._sakura_ref = sakura_ref
        self._file_cache = {}
        self._modified_files = set()
        self._ensure_client()

        effective_model = model or self._default_model

        template = (
            SAKURA_SYSTEM_TEMPLATE
            if target_file == "SAKURA.md"
            else MEMORY_SYSTEM_TEMPLATE
        )

        # 格式化反思文件名列表（加上 memory/ 前缀方便 AI 直接读取）
        files_list = (
            "\n".join(
                f"- memory/{f}" if "/" not in f else f"- {f}"
                for f in new_reflection_files
            )
            or "（无新增）"
        )
        system_prompt = template.format(
            new_reflection_files=files_list,
            repo_full_name=repo_full_name,
            languages=languages,
            total_reflections=total_reflections,
            max_chars=max_chars,
        )

        logger.info(
            "[consolidate-agent] 开始合并 {} / {} (model={}, max_iterations={})",
            repo_full_name,
            target_file,
            effective_model,
            max_iterations,
        )

        await self._run_agent_conversation(
            system_prompt, effective_model, max_iterations
        )

        changes = self._collect_changes()

        if not changes:
            logger.warning(
                "[consolidate-agent] {} 无变更: {}", target_file, repo_full_name
            )
        else:
            logger.info(
                "[consolidate-agent] {} 完成: {} chars",
                target_file,
                sum(len(v) for v in changes.values()),
            )
        return changes
