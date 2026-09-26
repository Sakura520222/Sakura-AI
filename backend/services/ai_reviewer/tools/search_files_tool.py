"""跨文件搜索工具处理器

为 AI 审查员提供在仓库中跨文件搜索关键词的能力，
类似于 grep 搜索，返回所有匹配的文件和行内容。
"""

import asyncio
import io
from typing import Any
from urllib.parse import urlencode

from loguru import logger

from backend.core.config import get_strategy_config, path_matches_skip

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
    max_matches: int = 20,
    max_output_chars: int = 50_000,
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
    match_limit = max(1, int(max_matches))
    output_limit = max(1, int(max_output_chars))
    window: dict[int, str] = {}
    pending_matches: list[int] = []
    result_parts: list[str] = []
    output_chars = 0
    included_through = -1
    match_count = 0
    returned_matches = 0
    lines_seen = 0
    scan_complete = True
    matches_truncated = False
    output_truncated = False

    for line_no, line in enumerate(io.StringIO(decoded), 1):
        idx = line_no - 1
        lines_seen = line_no
        window[idx] = line
        window_size = 2 * context_lines + 2
        while len(window) > window_size:
            window.pop(next(iter(window)))

        if keyword_lower in line.lower():
            if match_count >= match_limit:
                matches_truncated = True
                scan_complete = False
                break
            match_count += 1
            pending_matches.append(idx)

        while pending_matches and idx >= pending_matches[0] + context_lines:
            match_idx = pending_matches.pop(0)
            returned_matches += 1
            block_start = max(0, match_idx - context_lines)
            block_end = match_idx + context_lines
            emit_from = max(block_start, included_through + 1)
            for current_idx in range(emit_from, block_end + 1):
                current_line = window.get(current_idx)
                if current_line is None:
                    scan_complete = False
                    output_truncated = True
                    break
                prefix = f"{current_idx + 1:>6}\t"
                if current_idx == match_idx:
                    prefix += ">>>\t"
                separator = "\n" if result_parts else ""
                part = separator + prefix + current_line
                if output_chars + len(part) > output_limit:
                    scan_complete = False
                    output_truncated = True
                    break
                result_parts.append(part)
                output_chars += len(part)
                included_through = current_idx
            if not scan_complete:
                break
        if not scan_complete:
            break
    else:
        if not decoded or decoded.endswith("\n"):
            window[lines_seen] = ""
            lines_seen += 1
        while pending_matches:
            match_idx = pending_matches.pop(0)
            returned_matches += 1
            block_start = max(0, match_idx - context_lines)
            block_end = min(lines_seen - 1, match_idx + context_lines)
            emit_from = max(block_start, included_through + 1)
            for current_idx in range(emit_from, block_end + 1):
                current_line = window.get(current_idx)
                if current_line is None:
                    scan_complete = False
                    output_truncated = True
                    break
                prefix = f"{current_idx + 1:>6}\t"
                if current_idx == match_idx:
                    prefix += ">>>\t"
                separator = "\n" if result_parts else ""
                part = separator + prefix + current_line
                if output_chars + len(part) > output_limit:
                    scan_complete = False
                    output_truncated = True
                    break
                result_parts.append(part)
                output_chars += len(part)
                included_through = current_idx
            if not scan_complete:
                break

    if not match_count:
        return None

    return {
        "file_path": file_path,
        "content": "\n".join(result_parts),
        "match_count": match_count,
        "returned_matches": returned_matches,
        "matches_truncated": matches_truncated,
        "output_truncated": output_truncated,
        "scan_complete": scan_complete,
        "lines_scanned": lines_seen,
        **({"total_lines": lines_seen} if scan_complete else {}),
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
            "max_file_bytes": max(
                1, int(search_config.get("max_file_bytes", 2 * 1024 * 1024))
            ),
            "max_total_scan_bytes": max(
                1, int(search_config.get("max_total_scan_bytes", 16 * 1024 * 1024))
            ),
            "max_matches_per_file": max(
                1, int(search_config.get("max_matches_per_file", 20))
            ),
            "max_output_chars": max(
                1, int(search_config.get("max_output_chars", 50_000))
            ),
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

    async def _scan_candidate_files(
        self,
        keyword_lower: str,
        repo: Any,
        ref: str,
        candidate_files: list[str],
        effective_context_lines: int,
        effective_max_results: int,
        config: dict,
        candidate_sizes: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Fetch and scan candidates with bounded concurrency and budgets."""
        queue: asyncio.Queue[str] = asyncio.Queue()
        for file_path in candidate_files:
            queue.put_nowait(file_path)

        results_by_path: dict[str, dict[str, Any]] = {}
        skipped_by_path: dict[str, dict[str, Any]] = {}
        budget_lock = asyncio.Lock()
        state = {
            "bytes_scanned": 0,
            "output_chars": 0,
            "files_with_matches": 0,
            "fetch_failures": 0,
            "inflight": 0,
            "scan_complete": True,
            "stop": False,
            "stop_reason": None,
        }

        async def worker() -> None:
            while not state["stop"]:
                try:
                    file_path = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                predeclared_size = int((candidate_sizes or {}).get(file_path, 0))
                if predeclared_size > config["max_file_bytes"]:
                    skipped_by_path[file_path] = {
                        "file_path": file_path,
                        "status": "skipped",
                        "reason": "file_size_limit",
                        "size": predeclared_size,
                        "limit": config["max_file_bytes"],
                    }
                    continue
                try:
                    content_file = await asyncio.to_thread(
                        repo.get_contents, file_path, ref
                    )
                except Exception as e:
                    state["fetch_failures"] += 1
                    logger.debug(f"搜索文件 {file_path} 时出错，跳过: {e}")
                    continue

                state["inflight"] += 1
                try:
                    async with budget_lock:
                        if state["stop"]:
                            continue
                        try:
                            declared_size = int(
                                getattr(content_file, "size", 0) or 0
                            )
                        except (TypeError, ValueError):
                            declared_size = 0

                        max_file_bytes = config["max_file_bytes"]
                        if declared_size and declared_size > max_file_bytes:
                            skipped_by_path[file_path] = {
                                "file_path": file_path,
                                "status": "skipped",
                                "reason": "file_size_limit",
                                "size": declared_size,
                                "limit": max_file_bytes,
                            }
                            continue

                        decoded_content = content_file.decoded_content
                        if decoded_content is None:
                            skipped_by_path[file_path] = {
                                "file_path": file_path,
                                "status": "skipped",
                                "reason": "no_decoded_content",
                            }
                            continue

                        actual_size = declared_size or len(decoded_content)
                        if actual_size > max_file_bytes:
                            skipped_by_path[file_path] = {
                                "file_path": file_path,
                                "status": "skipped",
                                "reason": "file_size_limit",
                                "size": actual_size,
                                "limit": max_file_bytes,
                            }
                            continue

                        remaining_scan = (
                            config["max_total_scan_bytes"]
                            - state["bytes_scanned"]
                        )
                        if actual_size > remaining_scan:
                            skipped_by_path[file_path] = {
                                "file_path": file_path,
                                "status": "skipped",
                                "reason": "total_scan_budget",
                                "size": actual_size,
                                "remaining_bytes": max(0, remaining_scan),
                            }
                            state["scan_complete"] = False
                            state["stop"] = True
                            state["stop_reason"] = "total_scan_budget"
                            continue

                        output_budget = (
                            config["max_output_chars"] - state["output_chars"]
                        )
                        if output_budget <= 0:
                            state["scan_complete"] = False
                            state["stop"] = True
                            state["stop_reason"] = "output_budget"
                            continue

                        # Reserve scan bytes before the await so concurrent
                        # workers cannot collectively exceed the total budget.
                        state["bytes_scanned"] += actual_size
                        result = await asyncio.to_thread(
                            _scan_content,
                            file_path,
                            decoded_content,
                            keyword_lower,
                            effective_context_lines,
                            config["max_matches_per_file"],
                            output_budget,
                        )
                        if result is None:
                            continue

                        results_by_path[file_path] = result
                        state["output_chars"] += len(result["content"])
                        state["files_with_matches"] += 1
                        if not result["scan_complete"]:
                            state["scan_complete"] = False
                            state["stop_reason"] = (
                                "output_budget"
                                if result["output_truncated"]
                                else "matches_per_file"
                            )

                        has_unseen_files = queue.qsize() > 0 or state["inflight"] > 1
                        if (
                            state["output_chars"] >= config["max_output_chars"]
                            and has_unseen_files
                        ) or (
                            state["files_with_matches"] >= effective_max_results
                            and has_unseen_files
                        ):
                            state["scan_complete"] = False
                            state["stop"] = True
                            if state["stop_reason"] is None:
                                state["stop_reason"] = (
                                    "output_budget"
                                    if state["output_chars"]
                                    >= config["max_output_chars"]
                                    else "max_results"
                                )
                finally:
                    state["inflight"] -= 1

        worker_count = min(config["concurrency"], len(candidate_files) or 1)
        await asyncio.gather(*(worker() for _ in range(worker_count)))
        ordered_paths = [path for path in candidate_files if path in results_by_path]
        return {
            "results": [results_by_path[path] for path in ordered_paths],
            "skipped_files": [
                skipped_by_path[path]
                for path in candidate_files
                if path in skipped_by_path
            ],
            "files_searched": len(candidate_files) - queue.qsize(),
            "candidate_count": len(candidate_files),
            "bytes_scanned": state["bytes_scanned"],
            "fetch_failures": state["fetch_failures"],
            "scan_complete": state["scan_complete"],
            "stop_reason": state["stop_reason"],
        }

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
        candidate_sizes: dict[str, int] = {}
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
            try:
                candidate_sizes[file_path] = int(item.get("size", 0) or 0)
            except (TypeError, ValueError):
                candidate_sizes[file_path] = 0

        config = self._get_config()
        candidates_truncated = len(candidates) > config["max_files_to_search"]
        if candidates_truncated:
            candidates = candidates[: config["max_files_to_search"]]
        scan = await self._scan_candidate_files(
            keyword_lower,
            repo,
            ref,
            candidates,
            effective_context_lines,
            effective_max_results,
            config,
            candidate_sizes,
        )
        all_results: list[dict[str, Any]] = scan["results"]
        files_searched = scan["files_searched"]
        fetch_failures = scan["fetch_failures"]

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
                "candidate_files": scan["candidate_count"],
                "ref": ref,
                "search_method": "github_search_api",
            }

        total_matches = sum(r["match_count"] for r in all_results)
        scan_complete = (
            scan["scan_complete"] and not candidates_truncated
        )

        return {
            "keyword": keyword,
            "results": all_results[:effective_max_results],
            "files_searched": files_searched,
            "candidate_files": scan["candidate_count"],
            "files_with_matches": len(all_results),
            "total_matches": total_matches,
            "total_matches_exact": scan_complete,
            "scan_complete": scan_complete,
            "bytes_scanned": scan["bytes_scanned"],
            "skipped_files": scan["skipped_files"],
            "returned_files": min(len(all_results), effective_max_results),
            "context_lines": effective_context_lines,
            "ref": ref,
            "search_method": "github_search_api",
            "stop_reason": scan["stop_reason"],
            "hint": (
                f"在 {files_searched} 个文件中搜索，"
                f"{len(all_results)} 个文件包含匹配，"
                f"至少找到 {total_matches} 处匹配；搜索因预算提前停止。"
                if not scan_complete
                else f"共 {total_matches} 处匹配。"
                if len(all_results) > effective_max_results
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
            tree = await asyncio.to_thread(repo.get_git_tree, sha=ref, recursive=True)
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
        candidate_sizes: dict[str, int] = {}
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
            try:
                candidate_sizes[path] = int(getattr(entry, "size", 0) or 0)
            except (TypeError, ValueError):
                candidate_sizes[path] = 0

        # 截断到 max_files_to_search / Cap candidates before concurrent fetch
        candidates_truncated = len(candidate_files) > max_files_to_search
        if len(candidate_files) > max_files_to_search:
            logger.debug(
                f"候选文件 {len(candidate_files)} 超过 "
                f"max_files_to_search={max_files_to_search}，截断"
            )
            candidate_files = candidate_files[:max_files_to_search]

        logger.debug(f"跨文件搜索 '{keyword}': 候选文件 {len(candidate_files)} 个")

        keyword_lower = keyword.lower()
        config = self._get_config()
        scan = await self._scan_candidate_files(
            keyword_lower,
            repo,
            ref,
            candidate_files,
            effective_context_lines,
            effective_max_results,
            config,
            candidate_sizes,
        )
        all_results: list[dict[str, Any]] = scan["results"]
        files_scanned = scan["files_searched"]
        total_matches = sum(r["match_count"] for r in all_results)
        scan_complete = scan["scan_complete"] and not candidates_truncated
        returned_results = all_results[:effective_max_results]

        return {
            "keyword": keyword,
            "results": returned_results,
            "files_searched": files_scanned,
            "candidate_files": scan["candidate_count"],
            "files_with_matches": len(all_results),
            "total_matches": total_matches,
            "total_matches_exact": scan_complete,
            "scan_complete": scan_complete,
            "bytes_scanned": scan["bytes_scanned"],
            "skipped_files": scan["skipped_files"],
            "returned_files": len(returned_results),
            "context_lines": effective_context_lines,
            "ref": ref,
            "search_method": "per_file_traversal",
            "stop_reason": scan["stop_reason"],
            "hint": (
                f"在 {files_scanned} 个文件中搜索，"
                f"{len(all_results)} 个文件包含匹配，"
                f"至少找到 {total_matches} 处匹配；搜索因预算提前停止。"
                if not scan_complete
                else f"共 {total_matches} 处匹配。"
                if len(returned_results) < len(all_results)
                else None
            ),
        }
