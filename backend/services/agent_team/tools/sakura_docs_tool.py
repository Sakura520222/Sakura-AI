""".sakura/ 知识文档工具

提供 Agent Team 审查角色浏览和读取 .sakura/ 知识目录的能力：
- read_sakura_docs: 读取 .sakura/ 下的文档内容（留空返回概览）
- list_sakura_directory: 列出 .sakura/ 目录结构
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from loguru import logger

from backend.core.config import get_strategy_config
from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult


# ── 路径安全校验 ──────────────────────────────────────────


def _validate_sakura_path(user_input: str) -> Optional[str]:
    """验证并规范化 .sakura/ 下的路径，防止路径遍历"""
    normalized = user_input.strip().replace("\\", "/")
    if "../" in normalized or "..\\" in user_input:
        return None
    normalized = normalized.strip("/")
    if not normalized.startswith(".sakura/"):
        normalized = f".sakura/{normalized}"
    if not normalized.startswith(".sakura/") or normalized.count("/") < 1:
        return None
    return normalized


def _get_sakura_config() -> dict:
    """获取 .sakura/ 配置"""
    ce_config = get_strategy_config().get_context_enhancement_config()
    return ce_config.get("sakura_memory", {})


# ── read_sakura_docs ─────────────────────────────────────


class ReadSakuraDocsTool(BaseTool):
    """读取 .sakura/ 目录中的知识文档。"""

    name = "read_sakura_docs"

    _schema = {
        "type": "function",
        "function": {
            "name": "read_sakura_docs",
            "description": (
                "读取项目 .sakura/ 目录中的知识文档（审查规则、架构文档、经验计划等）。"
                "\n\n使用场景："
                "\n- 了解项目编码规范和审查规则"
                "\n- 查看架构设计文档和技术决策"
                "\n- 参考历史经验教训提升审查质量"
                "\n\n不指定 doc_path 时返回所有文档概览。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_path": {
                        "type": "string",
                        "description": (
                            ".sakura/ 下的文档路径，如 'rules/review-rules.md'、"
                            "'docs/architecture.md'、'SAKURA.md'。"
                            "留空返回所有文档概览。"
                        ),
                    },
                },
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        doc_path = args.get("doc_path", "")
        if doc_path and "../" in doc_path:
            return "路径不能包含 '..'"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        repo = ctx.extra.get("github_repo")
        sakura_ref = ctx.extra.get("sakura_ref")
        if not repo:
            return ToolResult(success=False, error="GitHub repo 不可用")

        doc_path = args.get("doc_path", "")

        if not doc_path or doc_path.strip() in ("", "/"):
            return await self._get_docs_overview(repo, sakura_ref)

        safe_path = _validate_sakura_path(doc_path)
        if safe_path is None:
            return ToolResult(success=False, error="路径不能包含 '..' 或逃逸 .sakura/ 目录")

        content = await self._read_file(repo, sakura_ref, safe_path)
        if content is None:
            return ToolResult(success=False, error=f"文件不存在: {safe_path}")

        return ToolResult(
            success=True,
            output={"file_path": safe_path, "content": content, "size": len(content)},
        )

    async def _get_docs_overview(
        self, repo: Any, sakura_ref: Optional[str]
    ) -> ToolResult:
        """返回 .sakura/ 所有文档概览"""
        try:
            contents = await self._list_dir(repo, sakura_ref, ".sakura")
            if not contents:
                return ToolResult(success=False, error=".sakura/ 目录不存在或为空")

            overview: dict[str, Any] = {"files": [], "directories": []}

            for item in contents:
                if item.type == "file":
                    content = await self._read_file(
                        repo, sakura_ref, item.path
                    )
                    if content:
                        overview["files"].append(
                            {
                                "name": item.name,
                                "path": item.path,
                                "size": len(content),
                            }
                        )
                elif item.type == "dir":
                    sub_contents = await self._list_dir(
                        repo, sakura_ref, item.path
                    )
                    dir_files = []
                    if sub_contents:
                        for sub in sub_contents:
                            if sub.type == "file":
                                dir_files.append(
                                    {"name": sub.name, "path": sub.path}
                                )
                    overview["directories"].append(
                        {
                            "name": item.name,
                            "path": item.path,
                            "file_count": len(dir_files),
                            "files": dir_files,
                        }
                    )

            return ToolResult(success=True, output=overview)

        except Exception as exc:
            logger.error("获取 .sakura/ 概览失败: {}", exc)
            return ToolResult(
                success=False, error=f"获取概览失败: {type(exc).__name__}: {exc}"
            )

    @staticmethod
    async def _read_file(
        repo: Any, sakura_ref: Optional[str], path: str
    ) -> Optional[str]:
        try:
            ref = sakura_ref or "main"

            def _read():
                content = repo.get_contents(path, ref=ref)
                if isinstance(content, list):
                    return None
                return content.decoded_content.decode("utf-8")

            return await asyncio.to_thread(_read)
        except Exception:
            return None

    @staticmethod
    async def _list_dir(
        repo: Any, sakura_ref: Optional[str], path: str
    ) -> Optional[list]:
        try:
            ref = sakura_ref or "main"

            def _list():
                contents = repo.get_contents(path, ref=ref)
                if isinstance(contents, list):
                    return contents
                return [contents]

            return await asyncio.to_thread(_list)
        except Exception:
            return None


# ── list_sakura_directory ────────────────────────────────


class ListSakuraDirectoryTool(BaseTool):
    """列出 .sakura/ 目录结构。"""

    name = "list_sakura_directory"

    _schema = {
        "type": "function",
        "function": {
            "name": "list_sakura_directory",
            "description": (
                "列出项目 .sakura/ 目录的结构，了解知识文件组织方式。"
                "\n\n使用场景："
                "\n- 发现可用的审查规则和文档"
                "\n- 浏览知识分类（rules/docs/plans 等）"
                "\n\n不指定 subdirectory 时列出根目录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subdirectory": {
                        "type": "string",
                        "description": (
                            ".sakura/ 下的子目录路径，如 'rules'、'docs'、'plans'。"
                            "留空列出根目录。"
                        ),
                    },
                },
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    def validate_input(self, args: dict[str, Any], ctx: ToolContext) -> str | None:
        subdir = args.get("subdirectory", "")
        if subdir and "../" in subdir:
            return "路径不能包含 '..'"
        return None

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        repo = ctx.extra.get("github_repo")
        sakura_ref = ctx.extra.get("sakura_ref")
        if not repo:
            return ToolResult(success=False, error="GitHub repo 不可用")

        subdirectory = args.get("subdirectory", "")

        if not subdirectory or subdirectory.strip() in ("", "/"):
            path = ".sakura"
        else:
            safe_path = _validate_sakura_path(subdirectory)
            if safe_path is None:
                return ToolResult(
                    success=False, error="路径不能包含 '..' 或逃逸 .sakura/ 目录"
                )
            path = safe_path

        contents = await self._list_dir(repo, sakura_ref, path)
        if contents is None:
            return ToolResult(success=False, error=f"目录不存在或为空: {path}")

        config = _get_sakura_config()
        dir_config = config.get("directory_convention", {})
        categories = dir_config.get("categories", {})

        entries = []
        for item in contents:
            entry = {
                "name": item.name,
                "type": item.type,
                "path": item.path,
            }
            if item.type == "dir" and item.name in categories:
                entry["category"] = categories[item.name].get("description", "")
            if item.type == "file":
                entry["size"] = item.size
            entries.append(entry)

        return ToolResult(
            success=True,
            output={"path": path, "entries": entries, "total": len(entries)},
        )

    @staticmethod
    async def _list_dir(
        repo: Any, sakura_ref: Optional[str], path: str
    ) -> Optional[list]:
        try:
            ref = sakura_ref or "main"

            def _list():
                contents = repo.get_contents(path, ref=ref)
                if isinstance(contents, list):
                    return contents
                return [contents]

            return await asyncio.to_thread(_list)
        except Exception:
            return None
