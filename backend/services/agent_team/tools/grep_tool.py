"""Grep 工具 - 搜索文件内容

优先使用系统 grep 命令，回退到 Python re 搜索。
支持 files_with_matches 和 content 两种输出模式。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult

# VCS 目录排除
VCS_EXCLUDES = {".git", ".svn", ".hg", ".bzr", "__pycache__", "node_modules"}
MAX_GREP_KEYWORD_LENGTH = 500


class GrepTool(BaseTool):
    """搜索工作区内文件内容。"""

    name = "search_in_files"

    _schema = {
        "type": "function",
        "function": {
            "name": "search_in_files",
            "description": (
                "在工作区内搜索指定文本（固定字符串匹配），返回匹配的文件和行内容。"
                "\n\n使用场景："
                "\n- 搜索函数定义、类定义"
                "\n- 查找某个变量的使用位置"
                "\n- 搜索配置项"
                "\n- 确认某个 API 的调用方式"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词（固定字符串匹配）",
                    },
                    "file_extension": {
                        "type": "string",
                        "description": "可选：限定文件后缀，如 .py、.ts",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "content"],
                        "description": (
                            "输出模式：files_with_matches 只返回文件名（默认），"
                            "content 返回匹配行内容"
                        ),
                        "default": "files_with_matches",
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "是否忽略大小写，默认 false",
                        "default": False,
                    },
                },
                "required": ["keyword"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        keyword = str(args.get("keyword") or "")
        if not keyword:
            return "缺少 keyword 参数"
        if len(keyword) > MAX_GREP_KEYWORD_LENGTH:
            return f"keyword 不能超过 {MAX_GREP_KEYWORD_LENGTH} 个字符"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        keyword = args["keyword"]
        file_ext = args.get("file_extension", "")
        output_mode = args.get("output_mode", "files_with_matches")
        case_insensitive = args.get("case_insensitive", False)

        workspace_path = Path(ctx.workspace)

        # 尝试使用系统 grep；使用 -F 固定字符串匹配 + -- 分隔以避免 keyword 被解析为选项
        cmd_parts = ["grep", "-rn", "-F"]
        if case_insensitive:
            cmd_parts.append("-i")
        if file_ext:
            cmd_parts.extend(["--include", f"*{file_ext}"])
        cmd_parts.extend(["--", keyword, "."])

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd_parts,
                cwd=str(workspace_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace").strip()

            if output:
                lines = output.split("\n")
                max_lines = 100
                truncated = len(lines) > max_lines
                lines = lines[:max_lines]

                if output_mode == "files_with_matches":
                    # 提取唯一文件名
                    files = sorted(
                        set(line.split(":")[0] for line in lines if ":" in line)
                    )
                    return ToolResult(
                        success=True,
                        output={
                            "files": files[:50],
                            "num_files": len(files),
                            "keyword": keyword,
                            "truncated": truncated or len(files) > 50,
                        },
                    )

                return ToolResult(
                    success=True,
                    output={
                        "matches": lines,
                        "total": len(output.split("\n")),
                        "keyword": keyword,
                        "truncated": truncated,
                    },
                )

        except (FileNotFoundError, asyncio.TimeoutError, OSError):
            # grep 不可用或超时，回退到 Python 搜索
            pass

        # Python 回退搜索
        return await self._python_search(
            keyword, file_ext, output_mode, case_insensitive, workspace_path
        )

    async def _python_search(
        self,
        keyword: str,
        file_ext: str,
        output_mode: str,
        case_insensitive: bool,
        workspace: Path,
    ) -> ToolResult:
        # 使用 re.escape 保持与系统 grep -F（固定字符串匹配）一致的语义
        escaped_keyword = re.escape(keyword)
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            pattern = re.compile(escaped_keyword, flags)
        except re.error:
            pattern = None

        matches: list[str] = []
        max_results = 100

        for file_path in workspace.rglob("*"):
            if len(matches) >= max_results * 2:
                break
            if not file_path.is_file():
                continue
            if any(part in VCS_EXCLUDES for part in file_path.parts):
                continue
            if file_ext and file_path.suffix != file_ext:
                continue

            try:
                text = await asyncio.to_thread(
                    file_path.read_text,
                    encoding="utf-8",
                    errors="replace",
                )
                rel = str(file_path.relative_to(workspace))
                for i, line in enumerate(text.split("\n"), start=1):
                    if pattern:
                        matched = bool(pattern.search(line))
                    elif case_insensitive:
                        matched = keyword.lower() in line.lower()
                    else:
                        matched = keyword in line
                    if matched:
                        matches.append(f"{rel}:{i}:{line.strip()}")
            except (OSError, UnicodeDecodeError):
                continue

        truncated = len(matches) > max_results
        matches = matches[:max_results]

        if output_mode == "files_with_matches":
            files = sorted(set(m.split(":")[0] for m in matches if ":" in m))
            return ToolResult(
                success=True,
                output={
                    "files": files,
                    "num_files": len(files),
                    "keyword": keyword,
                    "truncated": truncated,
                },
            )

        return ToolResult(
            success=True,
            output={
                "matches": matches,
                "total": len(matches),
                "keyword": keyword,
                "truncated": truncated,
            },
        )
