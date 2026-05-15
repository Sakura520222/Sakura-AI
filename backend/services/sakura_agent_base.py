"""Sakura Agent 基类

提供 Agent 会话循环、工具执行、文件操作的共享实现。
合并 Agent 和知识提取 Agent 均继承此基类。
"""

import asyncio
import json
from typing import Dict, Optional, Set

from loguru import logger

from backend.services.ai_reviewer.api_client import AIApiClient


# ── 共享工具定义（OpenAI Function Calling 格式） ──────────────────────────

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
        "description": "读取 .sakura/ 下指定文件的完整内容",
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

WRITE_FILE_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "创建或覆盖文件",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件路径",
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
        "description": "精确替换文件中的文本片段",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件路径",
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


class SakuraAgentBase:
    """Agent 基类，提供共享的会话循环和工具实现"""

    # 子类通过属性覆盖来自定义行为
    log_prefix: str = "[sakura-agent]"

    def __init__(self):
        self._api_client: Optional[AIApiClient] = None
        self._default_model: Optional[str] = None
        self._repo = None
        self._sakura_ref: Optional[str] = None
        self._file_cache: Dict[str, Optional[str]] = {}
        self._modified_files: Set[str] = set()

    def _ensure_client(self):
        """子类实现：初始化 API 客户端和模型"""
        raise NotImplementedError

    def _get_tools(self) -> list:
        """子类实现：返回工具定义列表"""
        raise NotImplementedError

    def _check_write_allowed(self, rel_path: str) -> Optional[str]:
        """子类可选覆盖：检查写入权限，返回错误消息或 None（允许）

        默认允许所有写入。
        """
        return None

    # ── Agent 会话循环 ──────────────────────────────────────────────────

    async def _run_agent_conversation(
        self, system_prompt: str, model: str, max_iterations: int
    ) -> None:
        messages: list = [{"role": "system", "content": system_prompt}]
        tools = self._get_tools()

        for i in range(max_iterations):
            try:
                response = await self._api_client.call_with_retry(
                    messages=messages,
                    model=model,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0.3,
                    max_tokens=4096,
                )
            except Exception as e:
                logger.error(
                    "{} LLM 调用失败 (iteration {}): {}", self.log_prefix, i, e
                )
                break

            if not response or not response.choices:
                logger.warning("{} LLM 返回空响应 (iteration {})", self.log_prefix, i)
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
                        k: v if len(str(v)) <= 80 else f"<{len(str(v))} chars>"
                        for k, v in args_raw.items()
                        if k not in ("content", "old_text", "new_text")
                    }
                    logger.info(
                        "{} 工具调用 #{}) {} ({})",
                        self.log_prefix,
                        i,
                        tool_name,
                        json.dumps(args_display, ensure_ascii=False)
                        if args_display
                        else "",
                    )

                    result = await self._execute_tool(tool_name, tc.function.arguments)

                    result_display = result
                    if len(result_display) > 300:
                        result_display = (
                            result_display[:300] + f"...({len(result)} chars)"
                        )
                    logger.info(
                        "{} 工具返回 #{}) {} -> {}",
                        self.log_prefix,
                        i,
                        tool_name,
                        result_display,
                    )

                    messages.append(
                        {"role": "tool", "content": result, "tool_call_id": tc.id}
                    )
                continue

            logger.info(
                "{} 会话结束 (iteration {}): {}",
                self.log_prefix,
                i,
                (msg.content or "")[:200],
            )
            return

        logger.warning("{} 达到最大迭代次数 {}", self.log_prefix, max_iterations)

    # ── 工具执行 ────────────────────────────────────────────────────────

    async def _execute_extra_tool(self, name: str, args: dict) -> Optional[str]:
        """子类可选覆盖：处理额外的自定义工具

        返回 None 表示该工具名不由子类处理，由基类继续处理。
        """
        return None

    async def _execute_tool(self, name: str, arguments_json: str) -> str:
        try:
            args = (
                json.loads(arguments_json)
                if isinstance(arguments_json, str)
                else arguments_json
            )
        except Exception:
            args = {}

        # 先尝试子类的自定义工具
        extra_result = await self._execute_extra_tool(name, args)
        if extra_result is not None:
            return extra_result

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
            logger.error("{} 工具 {} 执行失败: {}", self.log_prefix, name, e)
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
        except Exception as exc:
            logger.debug("{} list_directory GitHub API 异常: {}", self.log_prefix, exc)

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
                            0, {"name": name, "type": "file", "size": len(content)}
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

    # ── write_file ──────────────────────────────────────────────────────

    async def _tool_write_file(self, rel_path: str, content: str) -> str:
        error = self._check_write_allowed(rel_path)
        if error:
            return error

        self._file_cache[rel_path] = content
        self._modified_files.add(rel_path)
        logger.info(
            "{} write_file: {} ({} chars)", self.log_prefix, rel_path, len(content)
        )
        return f"已写入: {rel_path} ({len(content)} 字符)"

    # ── edit_file ───────────────────────────────────────────────────────

    async def _tool_edit_file(
        self, rel_path: str, old_text: str, new_text: str, replace_all: bool
    ) -> str:
        error = self._check_write_allowed(rel_path)
        if error:
            return error
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
        logger.info("{} edit_file: {} (1 处替换)", self.log_prefix, rel_path)
        return f"已编辑: {rel_path} (1 处替换)"

    # ── GitHub 辅助方法 ─────────────────────────────────────────────────

    async def _read_from_github(self, full_path: str) -> Optional[str]:
        try:
            ref = self._sakura_ref or "HEAD"

            def _read():
                content = self._repo.get_contents(full_path, ref=ref)
                if isinstance(content, list):
                    return None
                return content.decoded_content.decode("utf-8")

            return await asyncio.to_thread(_read)
        except Exception as exc:
            logger.debug("{} 读取 {} 失败: {}", self.log_prefix, full_path, exc)
            return None

    async def _read_from_github_raw(self, path: str, ref: str) -> Optional[str]:
        """读取仓库中的任意文件（不带 .sakura/ 前缀）"""
        try:

            def _read():
                content = self._repo.get_contents(path, ref=ref)
                if isinstance(content, list):
                    return None
                return content.decoded_content.decode("utf-8")

            return await asyncio.to_thread(_read)
        except Exception as exc:
            logger.debug("{} 读取 {} 失败: {}", self.log_prefix, path, exc)
            return None

    # ── 变更收集 ────────────────────────────────────────────────────────

    def _collect_changes(self) -> Dict[str, str]:
        """收集所有变更文件"""
        changes: Dict[str, str] = {}
        for path in self._modified_files:
            content = self._file_cache.get(path)
            if content is not None and content.strip():
                changes[path] = content
        return changes
