"""跨文件搜索工具处理器

为 AI 审查员提供在仓库中跨文件搜索关键词的能力，
类似于 grep 搜索，返回所有匹配的文件和行内容。
"""

from typing import Any, Dict, List, Optional

from loguru import logger

from backend.core.config import get_strategy_config
from backend.services.ai_reviewer.constants import MAX_FILE_SIZE_BYTES
from backend.services.ai_reviewer.tools.file_tool import format_search_results


# 常见的二进制文件后缀 / Common binary file extensions
BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".svg",
        ".webp",
        ".tiff",
        ".tif",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".7z",
        ".rar",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".class",
        ".pyc",
        ".pyo",
        ".o",
        ".obj",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".wmv",
        ".flac",
        ".wav",
        ".webm",
        ".jar",
        ".war",
        ".nupkg",
        ".db",
        ".sqlite",
        ".sqlite3",
    }
)


class SearchFilesToolHandler:
    """跨文件搜索工具处理器

    在仓库中跨文件搜索指定关键词，返回所有匹配的文件和行内容。
    支持按文件后缀和目录过滤，自动跳过 skip_paths 和二进制文件。
    """

    def _get_config(self) -> dict:
        """从策略配置读取 search_in_files 相关配置

        Returns:
            包含默认参数的配置字典
        """
        ce = get_strategy_config().get_context_enhancement_config()
        search_config = ce.get("search_in_files", {})
        return {
            "default_context_lines": int(search_config.get("default_context_lines", 3)),
            "default_max_results": int(search_config.get("default_max_results", 20)),
            "skip_binary": search_config.get("skip_binary", True),
            "use_search_api": search_config.get("use_search_api", True),
            "max_files_to_search": int(search_config.get("max_files_to_search", 100)),
        }

    async def search_in_files(
        self,
        keyword: str,
        repo: Any,
        pr: Any,
        file_extension: Optional[str] = None,
        directory: Optional[str] = None,
        context_lines: Optional[int] = None,
        max_results: Optional[int] = None,
    ) -> Dict[str, Any]:
        """在仓库中跨文件搜索指定关键词

        主路径使用 GitHub Search API，回退到逐文件搜索。

        Args:
            keyword: 搜索关键词
            repo: GitHub 仓库对象
            pr: GitHub PR 对象（可选）
            file_extension: 限定文件后缀，如 ".py"、".ts"
            directory: 限定搜索目录
            context_lines: 匹配行上下文行数
            max_results: 最大返回匹配结果数

        Returns:
            搜索结果字典
        """
        try:
            # 读取配置 / Read config
            config = self._get_config()
            effective_context_lines = (
                context_lines if context_lines is not None else config["default_context_lines"]
            )
            effective_max_results = (
                max_results if max_results is not None else config["default_max_results"]
            )
            skip_binary = config["skip_binary"]

            # 读取 skip_paths / Read skip paths
            skip_paths = get_strategy_config().get_file_filters().get("skip_paths", [])

            # 确定引用 ref / Determine ref
            ref = None
            if pr is not None:
                ref = pr.head.sha
            else:
                ref = repo.default_branch

            if config["use_search_api"]:
                try:
                    return await self._search_via_api(
                        keyword,
                        repo,
                        ref,
                        skip_paths,
                        skip_binary,
                        file_extension,
                        directory,
                        effective_context_lines,
                        effective_max_results,
                    )
                except Exception as e:
                    logger.warning(f"GitHub Search API 失败，回退到逐文件搜索: {e}")

            return await self._search_per_file(
                keyword,
                repo,
                ref,
                skip_paths,
                skip_binary,
                file_extension,
                directory,
                effective_context_lines,
                effective_max_results,
                config["max_files_to_search"],
            )

        except Exception as e:
            logger.error(f"跨文件搜索 '{keyword}' 时发生错误: {e}", exc_info=True)
            return {
                "keyword": keyword,
                "error": f"搜索失败: {e}",
                "results": [],
                "total_matches": 0,
            }

    async def _search_via_api(
        self,
        keyword: str,
        repo: Any,
        ref: str,
        skip_paths: List[str],
        skip_binary: bool,
        file_extension: Optional[str],
        directory: Optional[str],
        effective_context_lines: int,
        effective_max_results: int,
    ) -> Dict[str, Any]:
        """使用 GitHub Search API 搜索关键词

        Args:
            keyword: 搜索关键词
            repo: GitHub 仓库对象
            ref: Git 引用 (SHA or branch)
            skip_paths: 需要跳过的路径前缀列表
            skip_binary: 是否跳过二进制文件
            file_extension: 限定文件后缀
            directory: 限定搜索目录
            effective_context_lines: 上下文行数
            effective_max_results: 最大返回结果数

        Returns:
            搜索结果字典
        """
        # 构造 GitHub Search API 查询 / Build GitHub Search API query
        escaped_keyword = keyword.replace("\\", "\\\\").replace('"', '\\"')
        query = f'"{escaped_keyword}" repo:{repo.full_name}'
        if file_extension:
            ext = file_extension.lstrip(".")
            query += f" extension:{ext}"
        if directory:
            query += f" path:{directory}"

        logger.debug(f"GitHub Search API 查询: {query}")

        # search_code 在 Github 主客户端上，不在 Repository 上
        # Use repo._requester to call the Search API directly
        from urllib.parse import urlencode

        encoded_query = urlencode({"q": query})
        requester = repo._requester
        _, data = requester.requestJsonAndCheck(
            "GET", f"/search/code?{encoded_query}"
        )

        all_results: List[Dict[str, Any]] = []
        keyword_lower = keyword.lower()
        files_searched = 0

        for item in data.get("items", []):
            if len(all_results) >= effective_max_results:
                break

            file_path = item.get("path", "")

            # 跳过 skip_paths / Skip paths in skip list
            should_skip = False
            for skip_path in skip_paths:
                if file_path.startswith(skip_path.rstrip("/")):
                    should_skip = True
                    break
            if should_skip:
                continue

            # 跳过二进制文件 / Skip binary files
            if skip_binary:
                _, ext = file_path.rsplit(".", 1) if "." in file_path else (file_path, "")
                if f".{ext}" in BINARY_EXTENSIONS:
                    continue

            files_searched += 1

            try:
                # 获取文件内容 / Fetch file content
                content_file = repo.get_contents(file_path, ref)

                if content_file.size > MAX_FILE_SIZE_BYTES:
                    continue

                decoded = content_file.decoded_content.decode("utf-8")
                lines = decoded.split("\n")

                # 搜索匹配行 / Search for matching lines
                matches: List[int] = []
                for idx, line in enumerate(lines):
                    if keyword_lower in line.lower():
                        matches.append(idx)

                if not matches:
                    continue

                numbered_content = format_search_results(
                    lines, matches, effective_context_lines
                )

                all_results.append(
                    {
                        "file_path": file_path,
                        "content": numbered_content,
                        "match_count": len(matches),
                        "total_lines": len(lines),
                    }
                )

            except Exception as e:
                logger.debug(f"搜索文件 {file_path} 时出错，跳过: {e}")
                continue

        # 截断到 max_results / Truncate to max_results
        total_matches = sum(r["match_count"] for r in all_results)
        truncated = all_results[:effective_max_results]

        return {
            "keyword": keyword,
            "results": truncated,
            "files_searched": files_searched,
            "files_with_matches": len(all_results),
            "total_matches": total_matches,
            "returned_files": len(truncated),
            "context_lines": effective_context_lines,
            "ref": ref,
            "search_method": "github_search_api",
            "hint": (
                f"在 {files_searched} 个文件中搜索，"
                f"{len(all_results)} 个文件包含匹配，"
                f"共 {total_matches} 处匹配。"
                if len(truncated) < len(all_results)
                else None
            ),
        }

    async def _search_per_file(
        self,
        keyword: str,
        repo: Any,
        ref: str,
        skip_paths: List[str],
        skip_binary: bool,
        file_extension: Optional[str],
        directory: Optional[str],
        effective_context_lines: int,
        effective_max_results: int,
        max_files_to_search: int,
    ) -> Dict[str, Any]:
        """逐文件遍历搜索关键词（回退路径）

        Args:
            keyword: 搜索关键词
            repo: GitHub 仓库对象
            ref: Git 引用 (SHA or branch)
            skip_paths: 需要跳过的路径前缀列表
            skip_binary: 是否跳过二进制文件
            file_extension: 限定文件后缀
            directory: 限定搜索目录
            effective_context_lines: 上下文行数
            effective_max_results: 最大返回结果数
            max_files_to_search: 最多扫描文件数

        Returns:
            搜索结果字典
        """
        # 获取完整文件树 / Get full file tree
        try:
            tree = repo.get_git_tree(sha=ref, recursive=True)
        except Exception as e:
            logger.error(f"获取仓库文件树失败: {e}", exc_info=True)
            return {
                "keyword": keyword,
                "error": f"获取仓库文件树失败: {e}",
                "results": [],
                "total_matches": 0,
            }

        # 过滤文件列表 / Filter file list
        candidate_files: List[str] = []
        for entry in tree.tree:
            if entry.type != "blob":
                continue

            path = entry.path

            # 按 directory 过滤 / Filter by directory
            if directory and not path.startswith(directory.rstrip("/") + "/"):
                continue

            # 按 file_extension 过滤 / Filter by file extension
            if file_extension:
                normalized_ext = file_extension.lstrip(".")
                if not path.endswith(f".{normalized_ext}"):
                    continue

            # 跳过 skip_paths / Skip paths in skip list
            should_skip = False
            for skip_path in skip_paths:
                if path.startswith(skip_path.rstrip("/")):
                    should_skip = True
                    break
            if should_skip:
                continue

            # 跳过二进制文件 / Skip binary files
            if skip_binary:
                _, ext = path.rsplit(".", 1) if "." in path else (path, "")
                if f".{ext}" in BINARY_EXTENSIONS:
                    continue

            candidate_files.append(path)

        logger.debug(
            f"跨文件搜索 '{keyword}': 候选文件 {len(candidate_files)} 个"
        )

        # 逐文件搜索，带总数限制 / Per-file search with total limit
        all_results: List[Dict[str, Any]] = []
        keyword_lower = keyword.lower()
        files_scanned = 0

        for file_path in candidate_files:
            if len(all_results) >= effective_max_results:
                break
            if files_scanned >= max_files_to_search:
                logger.debug(
                    f"已达到 max_files_to_search={max_files_to_search} 限制，停止扫描"
                )
                break

            files_scanned += 1

            try:
                content_file = repo.get_contents(file_path, ref)

                # 跳过大文件 / Skip large files
                if content_file.size > MAX_FILE_SIZE_BYTES:
                    continue

                content = content_file.decoded_content.decode("utf-8")
                lines = content.split("\n")

                # 搜索匹配行 / Search for matching lines
                matches: List[int] = []
                for idx, line in enumerate(lines):
                    if keyword_lower in line.lower():
                        matches.append(idx)

                if not matches:
                    continue

                numbered_content = format_search_results(
                    lines, matches, effective_context_lines
                )

                all_results.append(
                    {
                        "file_path": file_path,
                        "content": numbered_content,
                        "match_count": len(matches),
                        "total_lines": len(lines),
                    }
                )

            except Exception as e:
                logger.debug(f"搜索文件 {file_path} 时出错，跳过: {e}")
                continue

        # 截断到 max_results / Truncate to max_results
        total_matches = sum(r["match_count"] for r in all_results)
        truncated = all_results[:effective_max_results]

        return {
            "keyword": keyword,
            "results": truncated,
            "files_searched": files_scanned,
            "files_with_matches": len(all_results),
            "total_matches": total_matches,
            "returned_files": len(truncated),
            "context_lines": effective_context_lines,
            "ref": ref,
            "search_method": "per_file_traversal",
            "hint": (
                f"在 {files_scanned} 个文件中搜索，"
                f"{len(all_results)} 个文件包含匹配，"
                f"共 {total_matches} 处匹配。"
                if len(truncated) < len(all_results)
                else None
            ),
        }
