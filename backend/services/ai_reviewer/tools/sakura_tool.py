""".sakura/ 文档工具处理器

提供 AI 在审查过程中浏览和读取 .sakura/ 目录的专用工具：
- read_sakura_docs: 读取 .sakura/ 文档（留空返回概览）
- list_sakura_directory: 列出 .sakura/ 目录结构
"""

import asyncio
from typing import Any

from loguru import logger

from backend.core.config import get_strategy_config


class SakuraToolHandler:
    """.sakura/ 文档工具处理器"""

    def __init__(self):
        pass

    def _get_config(self) -> dict:
        """获取配置"""
        ce_config = get_strategy_config().get_context_enhancement_config()
        return ce_config.get("sakura_memory", {})

    @staticmethod
    def _validate_sakura_path(user_input: str) -> str | None:
        """验证并规范化 .sakura/ 下的路径，防止路径遍历

        Returns:
            规范化后的路径，或 None 表示非法
        """
        normalized = user_input.strip().replace("\\", "/")
        if "../" in normalized or "..\\" in user_input:
            return None
        normalized = normalized.strip("/")
        if not normalized.startswith(".sakura/"):
            normalized = f".sakura/{normalized}"
        if not normalized.startswith(".sakura/") or normalized.count("/") < 1:
            return None
        return normalized

    async def read_sakura_docs(
        self,
        doc_path: str | None = None,
        repo: Any = None,
        pr: Any = None,
    ) -> dict[str, Any]:
        """读取 .sakura/ 目录中的文档

        Args:
            doc_path: .sakura/ 下的文档路径。留空返回所有文档概览
            repo: GitHub 仓库对象
            pr: GitHub PR 对象
        """
        try:
            if not doc_path or doc_path.strip() in ("", "/"):
                return await self._get_docs_overview(repo)

            safe_path = self._validate_sakura_path(doc_path)
            if safe_path is None:
                return {"error": "路径不能包含 '..' 或逃逸 .sakura/ 目录"}

            content = await self._read_file_from_repo(repo, safe_path)

            if content is None:
                return {"error": f"文件不存在: {safe_path}"}

            return {
                "file_path": safe_path,
                "content": content,
                "size": len(content),
            }

        except Exception as e:
            logger.error(f"读取 .sakura/ 文档失败: {e}", exc_info=True)
            return {"error": f"读取失败: {e!s}"}

    async def list_sakura_directory(
        self,
        subdirectory: str | None = None,
        repo: Any = None,
        pr: Any = None,
    ) -> dict[str, Any]:
        """列出 .sakura/ 目录结构

        Args:
            subdirectory: .sakura/ 下的子目录路径。留空列出根目录
            repo: GitHub 仓库对象
            pr: GitHub PR 对象
        """
        try:
            # Build the path
            if not subdirectory or subdirectory.strip() in ("", "/"):
                path = ".sakura"
            else:
                safe_path = self._validate_sakura_path(subdirectory)
                if safe_path is None:
                    return {"error": "路径不能包含 '..' 或逃逸 .sakura/ 目录"}
                path = safe_path

            # Get directory contents
            contents = await self._list_directory(repo, path)

            if contents is None:
                return {"error": f"目录不存在或为空: {path}"}

            # Categorize entries
            config = self._get_config()
            dir_config = config.get("directory_convention", {})
            categories = dir_config.get("categories", {})

            entries = []
            for item in contents:
                entry = {
                    "name": item.name,
                    "type": item.type,  # "dir" or "file"
                    "path": item.path,
                }
                # Add category info for subdirectories
                if item.type == "dir" and item.name in categories:
                    entry["category"] = categories[item.name].get("description", "")

                entries.append(entry)

            return {
                "path": path,
                "entries": entries,
                "total": len(entries),
            }

        except Exception as e:
            logger.error(f"列出 .sakura/ 目录失败: {e}", exc_info=True)
            return {"error": f"列出目录失败: {e!s}"}

    async def read_sakura_memory(
        self,
        file_name: str | None = None,
        count: int = 5,
        repo: Any = None,
        pr: Any = None,
    ) -> dict[str, Any]:
        """读取 .sakura/memory/ 目录下的审查反思文件

        Args:
            file_name: 反思文件名，留空返回最近文件列表
            count: 列出最近 N 个文件
            repo: GitHub 仓库对象
            pr: GitHub PR 对象
        """
        try:
            if file_name:
                safe = file_name.replace("\\", "/").strip("/")
                if "../" in safe or "/" in safe:
                    return {"error": "文件名不能包含路径分隔符或遍历字符"}
                if not safe.endswith(".md"):
                    return {"error": "仅支持读取 .md 文件"}
                path = f".sakura/memory/{safe}"
                content = await self._read_file_from_repo(repo, path)
                if content is None:
                    return {"error": f"文件不存在: {path}"}
                return {"file_path": path, "content": content, "size": len(content)}

            # 列出最近反思文件 / List recent reflection files
            contents = await self._list_directory(repo, ".sakura/memory")
            if not contents:
                return {"error": ".sakura/memory/ 目录不存在或为空"}

            files = [
                {"name": c.name, "path": c.path, "size": c.size}
                for c in contents
                if c.type == "file" and c.name.endswith(".md")
            ]
            files.sort(key=lambda f: f["name"], reverse=True)
            recent = files[:count]

            return {
                "total_files": len(files),
                "showing": len(recent),
                "files": recent,
            }

        except Exception as e:
            logger.error(f"读取 .sakura/memory/ 失败: {e}", exc_info=True)
            return {"error": f"读取失败: {e!s}"}

    async def _get_docs_overview(self, repo: Any) -> dict[str, Any]:
        """获取 .sakura/ 所有文档概览 / Get overview of all .sakura/ documents"""
        try:
            # List root .sakura/ contents
            contents = await self._list_directory(repo, ".sakura")
            if not contents:
                return {"error": ".sakura/ 目录不存在或为空"}

            overview = {
                "files": [],
                "directories": [],
            }

            for item in contents:
                if item.type == "file":
                    # Read file content for overview
                    content = await self._read_file_from_repo(repo, item.path)
                    if content:
                        overview["files"].append(
                            {
                                "name": item.name,
                                "path": item.path,
                                "size": len(content),
                                "preview": content[:200] + "..."
                                if len(content) > 200
                                else content,
                            }
                        )
                elif item.type == "dir":
                    # List directory contents
                    sub_contents = await self._list_directory(repo, item.path)
                    dir_files = []
                    if sub_contents:
                        for sub in sub_contents:
                            if sub.type == "file":
                                dir_files.append(
                                    {
                                        "name": sub.name,
                                        "path": sub.path,
                                    }
                                )
                    overview["directories"].append(
                        {
                            "name": item.name,
                            "path": item.path,
                            "file_count": len(dir_files),
                            "files": dir_files[:10],  # Limit preview
                        }
                    )

            return overview

        except Exception as e:
            logger.error(f"获取 .sakura/ 概览失败: {e}", exc_info=True)
            return {"error": f"获取概览失败: {e!s}"}

    async def _read_file_from_repo(self, repo: Any, path: str) -> str | None:
        """从仓库读取文件内容 / Read file content from repo"""
        try:

            def _read():
                content = repo.get_contents(path)
                if isinstance(content, list):
                    return None
                return content.decoded_content.decode("utf-8")

            return await asyncio.to_thread(_read)
        except Exception:
            return None

    async def _list_directory(self, repo: Any, path: str) -> list | None:
        """列出目录内容 / List directory contents"""
        try:

            def _list():
                contents = repo.get_contents(path)
                if isinstance(contents, list):
                    return contents
                return [contents]

            return await asyncio.to_thread(_list)
        except Exception:
            return None
