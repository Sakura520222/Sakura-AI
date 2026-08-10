"""跨文件搜索工具处理器

为 AI 审查员提供在仓库中跨文件搜索关键词的能力，
类似于 grep 搜索，返回所有匹配的文件和行内容。
"""

import asyncio
from typing import Any
from urllib.parse import urlencode

from loguru import logger

from backend.core.config import get_strategy_config, path_matches_skip
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


def _scan_content(
    file_path: str,
    decoded_content: bytes,
    keyword_lower: str,
    context_lines: int,
) -> dict[str, Any] | None:
    """同步解码并搜索关键词匹配（纯 CPU，线程安全，供 to_thread 调用）。

    Args:
        file_path: 文件路径
        decoded_content: 原始字节内容
        keyword_lower: 小写关键词
        context_lines: 上下文行数

    Returns:
        匹配结果字典；无匹配返回 None
    """
    decoded = decoded_content.decode("utf-8")
    lines = decoded.split("\n")
    matches = [idx for idx, line in enumerate(lines) if keyword_lower in line.lower()]
    if not matches:
        return None
    numbered_content = format_search_results(lines, matches, context_lines)
    return {
        "file_path": file_path,
        "content": numbered_content,
        "match_count": len(matches),
        "total_lines": len(lines),
    }


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
            "concurrency": max(1, int(search_config.get("concurrency", 8))),
        }

    async def search_in_files(
        self,
        keyword: str,
        repo: Any,
        pr: Any,
        file_extension: str | None = None,
        directory: str | None = None,
        context_lines: int | None = None,
        max_results: int | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """在仓库中跨文件搜索指定关键词

        主路径使用 GitHub Search API，回退到逐文件搜索。

        - PR 场景：使用 ``pr.head.sha`` 作为 ref，忽略 ``branch``。
        - 非 PR 场景：优先使用显式 ``branch``，失败或不可访问时回退默认分支。

        零匹配是有效结果，不会触发回退；仅当搜索过程因 ref 不可访问或
        API 读取失败时才回退下一个候选 ref。

        Args:
            keyword: 搜索关键词
            repo: GitHub 仓库对象
            pr: GitHub PR 对象（可选）
            file_extension: 限定文件后缀，如 ".py"、".ts"
            directory: 限定搜索目录
            context_lines: 匹配行上下文行数
            max_results: 最大返回匹配结果数
            branch: 非 PR 场景下指定搜索的分支名（可选）

        Returns:
            搜索结果字典
        """
        try:
            # 读取配置 / Read config
            config = self._get_config()
            effective_context_lines = (
                context_lines
                if context_lines is not None
                else config["default_context_lines"]
            )
            effective_max_results = (
                max_results
                if max_results is not None
                else config["default_max_results"]
            )
            skip_binary = config["skip_binary"]

            # 读取 skip_paths / Read skip paths
            skip_paths = get_strategy_config().get_file_filters().get("skip_paths", [])

            # 构造候选 ref / Build candidate refs
            # PR 场景：pr.head.sha；非 PR 场景：显式 branch -> 默认分支
            normalized_branch = (branch or "").strip() or None
            branch_requested: str | None = None
            candidate_refs: list[str] = []
            # GitHub Search API 仅索引默认分支；非默认分支 ref 需在零匹配时回退逐文件搜索
            default_branch = getattr(repo, "default_branch", None)

            if pr is not None:
                candidate_refs.append(pr.head.sha)
            else:
                branch_requested = normalized_branch
                if normalized_branch:
                    candidate_refs.append(normalized_branch)
                if default_branch:
                    candidate_refs.append(default_branch)

            tried_branches: list[str] = []
            last_result: dict[str, Any] | None = None

            for ref in candidate_refs:
                tried_branches.append(ref)
                # ref 不是默认分支时，Search API 看不到其内容，零匹配需回退逐文件确认
                may_miss_index = ref != default_branch
                result = await self._dispatch_search_round(
                    keyword,
                    repo,
                    ref,
                    skip_paths,
                    skip_binary,
                    file_extension,
                    directory,
                    effective_context_lines,
                    effective_max_results,
                    config,
                    may_miss_index=may_miss_index,
                )

                if result is not None and "error" not in result:
                    # 搜索成功（含零匹配），不回退 / Search succeeded (incl. zero matches)
                    result["branch_requested"] = branch_requested
                    result["branch_used"] = ref
                    result["tried_branches"] = list(tried_branches)
                    return result

                last_result = result
                if pr is None and normalized_branch and ref == normalized_branch:
                    logger.warning(
                        f"分支 {normalized_branch} 搜索失败或不可访问，回退到默认分支"
                    )

            # 所有候选 ref 均失败 / All candidate refs failed
            if last_result is None:
                last_result = {
                    "keyword": keyword,
                    "error": "无可用分支进行搜索",
                    "results": [],
                    "total_matches": 0,
                }
            last_result["branch_requested"] = branch_requested
            last_result["branch_used"] = None
            last_result["tried_branches"] = list(tried_branches)
            return last_result

        except Exception as e:
            logger.error(f"跨文件搜索 '{keyword}' 时发生错误: {e}", exc_info=True)
            return {
                "keyword": keyword,
                "error": f"搜索失败: {e}",
                "results": [],
                "total_matches": 0,
            }

    async def _dispatch_search_round(
        self,
        keyword: str,
        repo: Any,
        ref: str,
        skip_paths: list[str],
        skip_binary: bool,
        file_extension: str | None,
        directory: str | None,
        effective_context_lines: int,
        effective_max_results: int,
        config: dict,
        may_miss_index: bool = False,
    ) -> dict[str, Any]:
        """对单个 ref 执行一轮搜索：优先 Search API，失败回退逐文件搜索。

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
            config: 工具配置字典
            may_miss_index: 该 ref 是否不被 GitHub Search API 索引（非默认分支）。
                为 True 时，Search API 零匹配会回退逐文件搜索以确认，避免漏掉
                分支专属代码；默认分支零匹配仍是有效结果，不回退。

        Returns:
            搜索结果字典；ref 不可访问或搜索失败时含 ``error`` 字段
        """
        if config["use_search_api"]:
            try:
                from github.Repository import Repository

                if not isinstance(repo, Repository):
                    raise NotImplementedError("当前 repo 对象不支持 GitHub Search API")
                result = await self._search_via_api(
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
                # 非默认分支：Search API 仅索引默认分支，零匹配可能是索引看不到
                # 而非真的不存在，回退逐文件搜索以确认。
                if (
                    may_miss_index
                    and result is not None
                    and "error" not in result
                    and result.get("total_matches", 0) == 0
                ):
                    logger.debug(
                        f"Search API 在非默认分支 ref={ref} 零匹配，回退逐文件搜索"
                    )
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
                return result
            except ImportError:
                logger.warning("无法导入 PyGithub，回退到逐文件搜索")
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

    async def _search_via_api(
        self,
        keyword: str,
        repo: Any,
        ref: str,
        skip_paths: list[str],
        skip_binary: bool,
        file_extension: str | None,
        directory: str | None,
        effective_context_lines: int,
        effective_max_results: int,
    ) -> dict[str, Any]:
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
            escaped_dir = directory.replace(" ", "\\ ").replace('"', '\\"')
            query += f" path:{escaped_dir}"

        logger.debug(f"GitHub Search API 查询: {query}")

        # search_code 在 Github 主客户端上，不在 Repository 上
        # Use repo._requester to call the Search API directly
        # COMPAT: repo._requester 是 PyGithub 私有 API，升级 PyGithub 时需验证兼容性
        encoded_query = urlencode({"q": query})
        requester = repo._requester
        # 同步 HTTP 调用放入线程池，避免阻塞事件循环
        _, data = await asyncio.to_thread(
            requester.requestJsonAndCheck,
            "GET",
            f"/search/code?{encoded_query}",
        )

        keyword_lower = keyword.lower()

        # 过滤候选文件（skip_paths / 二进制）/ Filter candidates
        candidates: list[str] = []
        for item in data.get("items", []):
            file_path = item.get("path", "")
            if path_matches_skip(file_path, skip_paths):
                continue
            if skip_binary:
                _, ext = (
                    file_path.rsplit(".", 1) if "." in file_path else (file_path, "")
                )
                if f".{ext}" in BINARY_EXTENSIONS:
                    continue
            candidates.append(file_path)

        # 并发 get_contents + 搜索；信号量限流避免触发 GitHub 次级速率限制
        # Concurrent fetch + search; semaphore caps in-flight requests
        concurrency = self._get_config()["concurrency"]
        sem = asyncio.Semaphore(concurrency)

        async def _fetch_and_match(
            file_path: str,
        ) -> tuple[str, dict[str, Any] | None, bool]:
            """返回 (file_path, 匹配结果或 None, 是否读取失败)。"""
            async with sem:
                try:
                    content_file = await asyncio.to_thread(
                        repo.get_contents, file_path, ref
                    )
                except Exception as e:
                    logger.debug(f"搜索文件 {file_path} 时出错，跳过: {e}")
                    return (file_path, None, True)

            if content_file.size > MAX_FILE_SIZE_BYTES:
                return (file_path, None, False)
            decoded_content = content_file.decoded_content
            if decoded_content is None:
                return (file_path, None, False)

            result = await asyncio.to_thread(
                _scan_content,
                file_path,
                decoded_content,
                keyword_lower,
                effective_context_lines,
            )
            return (file_path, result, False)

        raw_results = await asyncio.gather(
            *[_fetch_and_match(fp) for fp in candidates]
        )

        files_searched = len(candidates)
        fetch_failures = sum(1 for _, _, failed in raw_results if failed)
        all_results: list[dict[str, Any]] = [
            r for _, r, _ in raw_results if r is not None
        ]

        # ref 不可访问检测 / Ref-inaccessible detection
        # Search API 找到匹配文件但全部 get_contents 失败，通常意味着 ref 无效，
        # 让上层回退到默认分支；零匹配（api_items == 0）不在此列，不触发回退。
        api_items = len(data.get("items", []))
        if api_items > 0 and files_searched > 0 and fetch_failures == files_searched:
            logger.warning(
                f"ref '{ref}' 可能不可访问：Search API 返回 {api_items} 个匹配但全部读取失败"
            )
            return {
                "keyword": keyword,
                "error": (
                    f"ref '{ref}' 可能不可访问：Search API 返回 {api_items} 个匹配但全部读取失败"
                ),
                "results": [],
                "total_matches": 0,
                "files_searched": files_searched,
                "ref": ref,
                "search_method": "github_search_api",
            }

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
        skip_paths: list[str],
        skip_binary: bool,
        file_extension: str | None,
        directory: str | None,
        effective_context_lines: int,
        effective_max_results: int,
        max_files_to_search: int,
    ) -> dict[str, Any]:
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
        # 获取完整文件树 / Get full file tree（同步调用放入线程池）
        try:
            tree = await asyncio.to_thread(
                repo.get_git_tree, sha=ref, recursive=True
            )
        except Exception as e:
            logger.error(f"获取仓库文件树失败: {e}", exc_info=True)
            return {
                "keyword": keyword,
                "error": f"获取仓库文件树失败: {e}",
                "results": [],
                "total_matches": 0,
            }

        # 过滤文件列表 / Filter file list
        candidate_files: list[str] = []
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
            if path_matches_skip(path, skip_paths):
                continue

            # 跳过二进制文件 / Skip binary files
            if skip_binary:
                _, ext = path.rsplit(".", 1) if "." in path else (path, "")
                if f".{ext}" in BINARY_EXTENSIONS:
                    continue

            candidate_files.append(path)

        # 截断到 max_files_to_search / Cap candidates before concurrent fetch
        if len(candidate_files) > max_files_to_search:
            logger.debug(
                f"候选文件 {len(candidate_files)} 超过 "
                f"max_files_to_search={max_files_to_search}，截断"
            )
            candidate_files = candidate_files[:max_files_to_search]

        logger.debug(f"跨文件搜索 '{keyword}': 候选文件 {len(candidate_files)} 个")

        # 并发 get_contents + 搜索 / Concurrent fetch + search
        keyword_lower = keyword.lower()
        concurrency = self._get_config()["concurrency"]
        sem = asyncio.Semaphore(concurrency)

        async def _fetch_and_match(
            file_path: str,
        ) -> dict[str, Any] | None:
            """返回匹配结果字典；无匹配或读取失败返回 None。"""
            async with sem:
                try:
                    content_file = await asyncio.to_thread(
                        repo.get_contents, file_path, ref
                    )
                except Exception as e:
                    logger.debug(f"搜索文件 {file_path} 时出错，跳过: {e}")
                    return None

            if content_file.size > MAX_FILE_SIZE_BYTES:
                return None
            decoded_content = content_file.decoded_content
            if decoded_content is None:
                return None

            return await asyncio.to_thread(
                _scan_content,
                file_path,
                decoded_content,
                keyword_lower,
                effective_context_lines,
            )

        raw_results = await asyncio.gather(
            *[_fetch_and_match(fp) for fp in candidate_files]
        )

        files_scanned = len(candidate_files)
        all_results: list[dict[str, Any]] = [r for r in raw_results if r is not None]

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
