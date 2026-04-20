"""跨文件搜索工具处理器

为 AI 审查员提供在仓库中跨文件搜索关键词的能力，
类似于 grep 搜索，返回所有匹配的文件和行内容。
"""

from typing import Any, Dict, List, Optional

from loguru import logger

from backend.core.config import get_strategy_config
from backend.services.ai_reviewer.constants import MAX_FILE_SIZE_BYTES


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

        使用 repo.get_git_tree 获取完整文件树，按条件过滤后逐文件搜索。

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
            effective_context_lines = context_lines or config["default_context_lines"]
            effective_max_results = max_results or config["default_max_results"]
            skip_binary = config["skip_binary"]

            # 读取 skip_paths / Read skip paths
            skip_paths = get_strategy_config().get_file_filters().get("skip_paths", [])

            # 确定引用 ref / Determine ref
            ref = None
            if pr is not None:
                ref = pr.head.sha
            else:
                ref = repo.default_branch

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
                if file_extension and not path.endswith(file_extension):
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

            # 逐文件搜索 / Search in each file
            all_results: List[Dict[str, Any]] = []
            keyword_lower = keyword.lower()

            for file_path in candidate_files:
                if len(all_results) >= effective_max_results:
                    break

                try:
                    content_file = repo.get_contents(file_path, ref)

                    # 跳过大文件 / Skip large files
                    if content_file.size > MAX_FILE_SIZE_BYTES:
                        continue

                    content = content_file.decoded_content.decode("utf-8")
                    lines = content.split("\n")

                    # 搜索匹配行 / Search for matching lines
                    matches = []
                    for idx, line in enumerate(lines):
                        if keyword_lower in line.lower():
                            matches.append(idx)

                    if not matches:
                        continue

                    # 收集匹配行及上下文 / Collect matched lines with context
                    included_indices = set()
                    for match_idx in matches:
                        ctx_start = max(0, match_idx - effective_context_lines)
                        ctx_end = min(len(lines), match_idx + effective_context_lines + 1)
                        for i in range(ctx_start, ctx_end):
                            included_indices.add(i)

                    # 按行号排序输出 / Sort by line number
                    sorted_indices = sorted(included_indices)
                    match_set = set(matches)
                    result_parts = []
                    for i in sorted_indices:
                        line_prefix = f"{i + 1:>6}\t"
                        if i in match_set:
                            line_prefix += ">>>\t"
                        result_parts.append(f"{line_prefix}{lines[i]}")

                    numbered_content = "\n".join(result_parts)

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
                "files_searched": len(candidate_files),
                "files_with_matches": len(all_results),
                "total_matches": total_matches,
                "returned_files": len(truncated),
                "context_lines": effective_context_lines,
                "ref": ref,
                "hint": (
                    f"在 {len(candidate_files)} 个文件中搜索，"
                    f"{len(all_results)} 个文件包含匹配，"
                    f"共 {total_matches} 处匹配。"
                    if len(truncated) < len(all_results)
                    else None
                ),
            }

        except Exception as e:
            logger.error(f"跨文件搜索 '{keyword}' 时发生错误: {e}", exc_info=True)
            return {
                "keyword": keyword,
                "error": f"搜索失败: {e}",
                "results": [],
                "total_matches": 0,
            }
