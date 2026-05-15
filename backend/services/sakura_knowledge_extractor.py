""".sakura/ 知识提取服务

单次 Agent 会话，AI 通过工具直接读取反思、编辑和新建知识文件。
无截断，无硬编码限制，所有参数可配置。
"""

import asyncio
import json
from typing import Dict, Optional, Set

from loguru import logger

from backend.core.config import get_settings, get_strategy_config
from backend.services.ai_reviewer.api_client import AIApiClient


# ── 工具定义（OpenAI Function Calling 格式） ─────────────────────────────

LIST_DIRECTORY_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": "列出 .sakura/ 下指定目录的文件和子目录",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对于 .sakura/ 的目录路径，空字符串表示根目录（如 'memory', 'rules', ''）",
                }
            },
            "required": [],
        },
    },
}

READ_FILE_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取 .sakura/ 下指定文件的完整内容（反思文件、已有知识文件等均可读取）",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "相对于 .sakura/ 的文件路径（如 'memory/2026-04-20_PR190.md', 'rules/review-rules.md'）",
                }
            },
            "required": ["file_path"],
        },
    },
}

WRITE_FILE_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "创建或覆盖 .sakura/ 下的文件（不可写入 memory/ 目录）",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "相对于 .sakura/ 的文件路径（如 'rules/review-rules.md', 'docs/architecture.md'）",
                },
                "content": {
                    "type": "string",
                    "description": "完整的文件内容（Markdown 格式）",
                },
            },
            "required": ["file_path", "content"],
        },
    },
}

EDIT_FILE_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": "精确替换 .sakura/ 下已有文件中的文本片段（不可编辑 memory/ 目录）",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "相对于 .sakura/ 的文件路径",
                },
                "old_text": {
                    "type": "string",
                    "description": "要被替换的原始文本，必须与文件内容完全一致",
                },
                "new_text": {
                    "type": "string",
                    "description": "替换后的新文本",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换所有匹配项，默认 false",
                    "default": False,
                },
            },
            "required": ["file_path", "old_text", "new_text"],
        },
    },
}

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


class SakuraKnowledgeExtractor:
    """从反思文件中提取结构化知识

    单次 Agent 会话，AI 通过工具直接读取、编辑、新建文件。
    所有变更缓存在内存中，最终统一返回。
    """

    def __init__(self):
        self._api_client: Optional[AIApiClient] = None
        self._default_model: Optional[str] = None
        self._repo = None
        self._sakura_ref: Optional[str] = None
        self._file_cache: Dict[str, Optional[str]] = {}
        self._modified_files: Set[str] = set()

    def _get_config(self) -> dict:
        ce_config = get_strategy_config().get_context_enhancement_config()
        return ce_config.get("sakura_memory", {})

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
        sakura_ref: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, str]:
        """运行知识提取 Agent 会话

        Returns:
            相对于 .sakura/ 的文件路径到新内容的映射（仅包含变更文件）
        """
        config = self._get_config()
        ext_config = config.get("knowledge_extraction", {})
        consolidation_config = config.get("consolidation", {})

        settings = get_settings()
        max_iterations = (
            settings.sakura_extraction_max_iterations
            or ext_config.get("max_iterations", 15)
        )
        max_chars = consolidation_config.get("max_sakura_chars", 5000)

        self._repo = repo
        self._sakura_ref = sakura_ref
        self._file_cache = {}
        self._modified_files = set()
        self._ensure_client()

        effective_model = model or self._default_model
        system_prompt = EXTRACT_KNOWLEDGE_SYSTEM.format(max_chars=max_chars)

        logger.info(
            "[extract] 开始提取: {}, model={}, max_iterations={}",
            repo_full_name,
            effective_model,
            max_iterations,
        )

        await self._run_agent_conversation(
            system_prompt, effective_model, max_iterations
        )

        # 收集所有变更文件
        changes: Dict[str, str] = {}
        for path in self._modified_files:
            content = self._file_cache.get(path)
            if content is not None and content.strip():
                changes[path] = content

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

    # ── Agent 会话循环 ──────────────────────────────────────────────────

    async def _run_agent_conversation(
        self, system_prompt: str, model: str, max_iterations: int
    ) -> None:
        messages: list = [{"role": "system", "content": system_prompt}]

        for i in range(max_iterations):
            try:
                response = await self._api_client.call_with_retry(
                    messages=messages,
                    model=model,
                    tools=EXTRACTION_TOOLS,
                    tool_choice="auto",
                    temperature=0.3,
                    max_tokens=4096,
                )
            except Exception as e:
                logger.error("[extract] LLM 调用失败 (iteration {}): {}", i, e)
                break

            if not response or not response.choices:
                logger.warning("[extract] LLM 返回空响应 (iteration {})", i)
                break

            choice = response.choices[0]
            msg = choice.message

            if choice.finish_reason == "tool_calls" and msg.tool_calls:
                messages.append(msg)
                for tc in msg.tool_calls:
                    # 记录工具调用详情
                    tool_name = tc.function.name
                    try:
                        args_raw = (
                            json.loads(tc.function.arguments)
                            if isinstance(tc.function.arguments, str)
                            else tc.function.arguments
                        )
                    except Exception:
                        args_raw = {}
                    # 只显示短字段，跳过大段文本内容
                    args_display = {
                        k: v if len(str(v)) <= 80 else f"<{len(str(v))} chars>"
                        for k, v in args_raw.items()
                        if k not in ("content", "old_text", "new_text")
                    }
                    logger.info(
                        "[extract] 工具调用 #{}) {} ({})",
                        i,
                        tool_name,
                        json.dumps(args_display, ensure_ascii=False)
                        if args_display
                        else "",
                    )

                    result = await self._execute_tool(
                        tc.function.name, tc.function.arguments
                    )

                    # 记录工具返回摘要
                    result_display = result
                    if len(result_display) > 300:
                        result_display = result_display[:300] + f"...({len(result)} chars)"
                    logger.info(
                        "[extract] 工具返回 #{}) {} -> {}",
                        i,
                        tc.function.name,
                        result_display,
                    )

                    messages.append(
                        {"role": "tool", "content": result, "tool_call_id": tc.id}
                    )
                continue

            # AI 输出最终文本（总结），会话结束
            logger.info(
                "[extract] Agent 会话结束 (iteration {}): {}",
                i,
                (msg.content or "")[:200],
            )
            return

        logger.warning("[extract] 达到最大迭代次数 {}", max_iterations)

    # ── 工具执行 ────────────────────────────────────────────────────────

    async def _execute_tool(self, name: str, arguments_json: str) -> str:
        try:
            args = (
                json.loads(arguments_json)
                if isinstance(arguments_json, str)
                else arguments_json
            )
        except Exception:
            args = {}

        try:
            if name == "list_directory":
                return await self._tool_list_directory(args.get("path", ""))
            elif name == "read_file":
                return await self._tool_read_file(args.get("file_path", ""))
            elif name == "write_file":
                return await self._tool_write_file(
                    args.get("file_path", ""), args.get("content", "")
                )
            elif name == "edit_file":
                return await self._tool_edit_file(
                    args.get("file_path", ""),
                    args.get("old_text", ""),
                    args.get("new_text", ""),
                    args.get("replace_all", False),
                )
            return f"未知工具: {name}"
        except Exception as e:
            logger.error("[extract] 工具 {} 执行失败: {}", name, e)
            return f"工具执行失败: {e}"

    # ── list_directory ──────────────────────────────────────────────────

    async def _tool_list_directory(self, rel_path: str) -> str:
        path = rel_path.strip("/")
        prefix = f"{path}/" if path else ""

        # 收集 GitHub 远程文件
        github_names: set[str] = set()
        entries: list[dict] = []
        github_path = f".sakura/{path}" if path else ".sakura"

        try:
            ref = self._sakura_ref or "HEAD"

            def _list():
                contents = self._repo.get_contents(github_path, ref=ref)
                if isinstance(contents, list):
                    return contents
                return [contents]

            contents = await asyncio.to_thread(_list)
            for item in contents:
                github_names.add(item.name)
                entry = {"name": item.name, "type": item.type}
                if item.type == "file":
                    entry["size"] = item.size
                entries.append(entry)
        except Exception:
            pass  # 目录可能不存在

        # 合并内存中的新文件（AI 刚写但未提交的）
        for cached_path in sorted(self._file_cache):
            if not cached_path.startswith(prefix):
                continue
            remainder = cached_path[len(prefix) :]
            # 只取当前层级的直接子项
            if "/" in remainder:
                name = remainder.split("/")[0]
                if name not in github_names:
                    github_names.add(name)
                    entries.insert(0, {"name": name, "type": "dir"})
            else:
                name = remainder
                if name not in github_names:
                    content = self._file_cache[cached_path]
                    if content is not None:
                        github_names.add(name)
                        entries.insert(
                            0,
                            {
                                "name": name,
                                "type": "file",
                                "size": len(content),
                                "_new": True,
                            },
                        )

        if not entries:
            return json.dumps({"files": [], "note": "目录为空或不存在"})
        return json.dumps(entries, ensure_ascii=False)

    # ── read_file ───────────────────────────────────────────────────────

    async def _tool_read_file(self, rel_path: str) -> str:
        if not rel_path or "../" in rel_path:
            return "无效文件路径"

        # 优先从缓存读取（可能有未提交的修改）
        if rel_path in self._file_cache:
            content = self._file_cache[rel_path]
            return content if content is not None else "(文件不存在)"

        content = await self._read_from_github(f".sakura/{rel_path}")
        self._file_cache[rel_path] = content
        return content or "(文件不存在或无法读取)"

    # ── write_file ──────────────────────────────────────────────────────

    async def _tool_write_file(self, rel_path: str, content: str) -> str:
        if not rel_path or "../" in rel_path:
            return "无效文件路径"
        if self._is_write_blocked(rel_path):
            return f"不允许写入: {rel_path}（memory/ 目录下的反思文件由系统管理）"

        self._file_cache[rel_path] = content
        self._modified_files.add(rel_path)
        logger.info("[extract] write_file: {} ({} 字符)", rel_path, len(content))
        return f"已写入: {rel_path} ({len(content)} 字符)"

    # ── edit_file ───────────────────────────────────────────────────────

    async def _tool_edit_file(
        self, rel_path: str, old_text: str, new_text: str, replace_all: bool
    ) -> str:
        if not rel_path or "../" in rel_path:
            return "无效文件路径"
        if self._is_write_blocked(rel_path):
            return f"不允许编辑: {rel_path}（memory/ 目录下的反思文件由系统管理）"
        if old_text == new_text:
            return "old_text 和 new_text 相同，无需替换"

        # 确保文件内容已缓存
        if rel_path not in self._file_cache:
            content = await self._read_from_github(f".sakura/{rel_path}")
            if content is None:
                return f"文件不存在: {rel_path}"
            self._file_cache[rel_path] = content

        current = self._file_cache[rel_path]
        if current is None:
            return f"文件不存在: {rel_path}"

        if old_text not in current:
            return (
                f"在 {rel_path} 中未找到要替换的文本。"
                "请先 read_file 查看文件内容，确保 old_text 完全一致。"
            )

        if not replace_all:
            count = current.count(old_text)
            if count > 1:
                return (
                    f"在 {rel_path} 中找到 {count} 处匹配。"
                    "请扩大 old_text 范围使匹配唯一，或设 replace_all=true。"
                )

        if replace_all:
            new_content = current.replace(old_text, new_text)
        else:
            new_content = current.replace(old_text, new_text, 1)

        self._file_cache[rel_path] = new_content
        self._modified_files.add(rel_path)
        logger.info("[extract] edit_file: {} (1 处替换)", rel_path)
        return f"已编辑: {rel_path} (1 处替换)"

    # ── 辅助方法 ────────────────────────────────────────────────────────

    @staticmethod
    def _is_write_blocked(rel_path: str) -> bool:
        return any(rel_path.startswith(p) for p in _WRITE_BLOCKED_PREFIXES)

    async def _read_from_github(self, full_path: str) -> Optional[str]:
        try:
            ref = self._sakura_ref or "HEAD"

            def _read():
                content = self._repo.get_contents(full_path, ref=ref)
                if isinstance(content, list):
                    return None
                return content.decoded_content.decode("utf-8")

            return await asyncio.to_thread(_read)
        except Exception:
            return None
