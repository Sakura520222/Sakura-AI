"""Sakura 记忆合并 Agent

通过工具调用让 AI 自主读取反思、README、当前文件，
然后通过 edit_file/write_file 精确修改 SAKURA.md 或 memory.md。
每次会话只处理一个目标文件，两次串行会话分别处理两个文件。
"""

import asyncio
import json
from typing import Dict, List, Optional, Set

from loguru import logger

from backend.core.config import get_settings
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
                    "description": "相对于 .sakura/ 的目录路径，空字符串表示根目录",
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
        "description": "读取 .sakura/ 下指定文件的完整内容（反思文件、SAKURA.md、memory.md 等）",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "相对于 .sakura/ 的文件路径（如 'memory/2026-04-20_PR190.md', 'SAKURA.md'）",
                }
            },
            "required": ["file_path"],
        },
    },
}

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

WRITE_FILE_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "创建或覆盖目标文件（仅限 SAKURA.md 或 memory.md）",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件名（SAKURA.md 或 memory.md）",
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
        "description": "精确替换目标文件中的文本片段（仅限 SAKURA.md 或 memory.md）",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件名（SAKURA.md 或 memory.md）",
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


class SakuraConsolidationAgent:
    """记忆合并 Agent，通过工具调用让 AI 精确编辑目标文件"""

    def __init__(self):
        self._api_client: Optional[AIApiClient] = None
        self._default_model: Optional[str] = None
        self._repo = None
        self._sakura_ref: Optional[str] = None
        self._file_cache: Dict[str, Optional[str]] = {}
        self._modified_files: Set[str] = set()

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
        """运行一次 Agent 会话，合并单个目标文件

        Args:
            repo: PyGithub Repository 对象
            repo_full_name: 仓库完整名称
            sakura_ref: .sakura/ 所在分支
            target_file: "SAKURA.md" 或 "memory.md"
            new_reflection_files: 上次合并后新增的反思文件名列表
            total_reflections: 累计反思次数
            max_chars: 文件最大字符数
            languages: 语言统计字符串
            model: LLM 模型名称
            max_iterations: 最大工具调用轮数

        Returns:
            变更文件路径到内容的映射
        """
        self._repo = repo
        self._sakura_ref = sakura_ref
        self._file_cache = {}
        self._modified_files = set()
        self._ensure_client()

        effective_model = model or self._default_model

        # 选择 system prompt 模板
        if target_file == "SAKURA.md":
            template = SAKURA_SYSTEM_TEMPLATE
        else:
            template = MEMORY_SYSTEM_TEMPLATE

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

        # 收集变更
        changes: Dict[str, str] = {}
        for path in self._modified_files:
            content = self._file_cache.get(path)
            if content is not None and content.strip():
                changes[path] = content

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
                    tools=CONSOLIDATION_TOOLS,
                    tool_choice="auto",
                    temperature=0.3,
                    max_tokens=4096,
                )
            except Exception as e:
                logger.error(
                    "[consolidate-agent] LLM 调用失败 (iteration {}): {}", i, e
                )
                break

            if not response or not response.choices:
                logger.warning(
                    "[consolidate-agent] LLM 返回空响应 (iteration {})", i
                )
                break

            choice = response.choices[0]
            msg = choice.message

            if choice.finish_reason == "tool_calls" and msg.tool_calls:
                messages.append(msg)
                for tc in msg.tool_calls:
                    tool_name = tc.function.name
                    try:
                        args_raw = (
                            json.loads(tc.function.arguments)
                            if isinstance(tc.function.arguments, str)
                            else tc.function.arguments
                        )
                    except Exception:
                        args_raw = {}
                    args_display = {
                        k: v
                        if len(str(v)) <= 80
                        else f"<{len(str(v))} chars>"
                        for k, v in args_raw.items()
                        if k not in ("content", "old_text", "new_text")
                    }
                    logger.info(
                        "[consolidate-agent] 工具调用 #{}) {} ({})",
                        i,
                        tool_name,
                        json.dumps(args_display, ensure_ascii=False)
                        if args_display
                        else "",
                    )

                    result = await self._execute_tool(
                        tool_name, tc.function.arguments
                    )

                    result_display = result
                    if len(result_display) > 300:
                        result_display = (
                            result_display[:300] + f"...({len(result)} chars)"
                        )
                    logger.info(
                        "[consolidate-agent] 工具返回 #{}) {} -> {}",
                        i,
                        tool_name,
                        result_display,
                    )

                    messages.append(
                        {"role": "tool", "content": result, "tool_call_id": tc.id}
                    )
                continue

            # AI 输出总结，会话结束
            logger.info(
                "[consolidate-agent] 会话结束 (iteration {}): {}",
                i,
                (msg.content or "")[:200],
            )
            return

        logger.warning(
            "[consolidate-agent] 达到最大迭代次数 {}", max_iterations
        )

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
            elif name == "read_readme":
                return await self._tool_read_readme()
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
            logger.error("[consolidate-agent] 工具 {} 执行失败: {}", name, e)
            return f"工具执行失败: {e}"

    # ── list_directory（合并 GitHub + 内存缓存） ─────────────────────────

    async def _tool_list_directory(self, rel_path: str) -> str:
        path = rel_path.strip("/")
        prefix = f"{path}/" if path else ""
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
            pass

        # 合并内存中的新文件
        for cached_path in sorted(self._file_cache):
            if not cached_path.startswith(prefix):
                continue
            remainder = cached_path[len(prefix) :]
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
                            {"name": name, "type": "file", "size": len(content)},
                        )

        if not entries:
            return json.dumps({"files": [], "note": "目录为空或不存在"})
        return json.dumps(entries, ensure_ascii=False)

    # ── read_file ───────────────────────────────────────────────────────

    async def _tool_read_file(self, rel_path: str) -> str:
        if not rel_path or "../" in rel_path:
            return "无效文件路径"

        if rel_path in self._file_cache:
            content = self._file_cache[rel_path]
            return content if content is not None else "(文件不存在)"

        content = await self._read_from_github(f".sakura/{rel_path}")
        self._file_cache[rel_path] = content
        return content or "(文件不存在或无法读取)"

    # ── read_readme ─────────────────────────────────────────────────────

    async def _tool_read_readme(self) -> str:
        try:
            ref = self._sakura_ref or "HEAD"

            # 尝试多个常见 README 文件名
            for name in ("README.md", "README.MD", "README", "readme.md"):
                content = await self._read_from_github_raw(name, ref)
                if content:
                    return content
            return "(仓库无 README 文件)"
        except Exception as e:
            return f"(读取 README 失败: {e})"

    # ── write_file ──────────────────────────────────────────────────────

    async def _tool_write_file(self, file_name: str, content: str) -> str:
        if file_name not in _WRITABLE_FILES:
            return f"不允许写入: {file_name}（只能编辑 {', '.join(_WRITABLE_FILES)}）"

        self._file_cache[file_name] = content
        self._modified_files.add(file_name)
        logger.info(
            "[consolidate-agent] write_file: {} ({} chars)", file_name, len(content)
        )
        return f"已写入: {file_name} ({len(content)} 字符)"

    # ── edit_file ───────────────────────────────────────────────────────

    async def _tool_edit_file(
        self, file_name: str, old_text: str, new_text: str, replace_all: bool
    ) -> str:
        if file_name not in _WRITABLE_FILES:
            return f"不允许编辑: {file_name}（只能编辑 {', '.join(_WRITABLE_FILES)}）"
        if old_text == new_text:
            return "old_text 和 new_text 相同，无需替换"

        # 确保文件内容已缓存
        if file_name not in self._file_cache:
            content = await self._read_from_github(f".sakura/{file_name}")
            if content is None:
                return f"文件不存在: {file_name}"
            self._file_cache[file_name] = content

        current = self._file_cache[file_name]
        if current is None:
            return f"文件不存在: {file_name}"

        if old_text not in current:
            return (
                f"在 {file_name} 中未找到要替换的文本。"
                "请先 read_file 查看文件内容，确保 old_text 完全一致。"
            )

        if not replace_all:
            count = current.count(old_text)
            if count > 1:
                return (
                    f"在 {file_name} 中找到 {count} 处匹配。"
                    "请扩大 old_text 范围使匹配唯一，或设 replace_all=true。"
                )

        if replace_all:
            new_content = current.replace(old_text, new_text)
        else:
            new_content = current.replace(old_text, new_text, 1)

        self._file_cache[file_name] = new_content
        self._modified_files.add(file_name)
        logger.info("[consolidate-agent] edit_file: {} (1 处替换)", file_name)
        return f"已编辑: {file_name} (1 处替换)"

    # ── 辅助方法 ────────────────────────────────────────────────────────

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

    async def _read_from_github_raw(self, path: str, ref: str) -> Optional[str]:
        """读取仓库根目录下的文件（不带 .sakura/ 前缀）"""
        try:

            def _read():
                content = self._repo.get_contents(path, ref=ref)
                if isinstance(content, list):
                    return None
                return content.decoded_content.decode("utf-8")

            return await asyncio.to_thread(_read)
        except Exception:
            return None
