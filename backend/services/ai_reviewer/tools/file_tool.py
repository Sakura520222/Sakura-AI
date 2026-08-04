"""文件工具处理器

从原 ai_reviewer.py 迁移的文件工具相关方法：
- _tool_read_file (1476-1585行)
- _tool_list_directory (1587-1711行)
"""

from typing import Any

from loguru import logger

from backend.core.config import get_strategy_config
from backend.services.ai_reviewer.constants import (
    DEFAULT_CONTEXT_LINES,
    MAX_CONTEXT_LINES,
    MAX_FILE_LINES,
    MAX_FILE_SIZE_BYTES,
)


def format_search_results(
    lines: list[str], match_indices: list[int], context_lines: int
) -> str:
    """Format search results with line numbers and match markers.

    Shared utility for search-related tools.
    """
    included_indices: set[int] = set()
    for match_idx in match_indices:
        ctx_start = max(0, match_idx - context_lines)
        ctx_end = min(len(lines), match_idx + context_lines + 1)
        for i in range(ctx_start, ctx_end):
            included_indices.add(i)

    sorted_indices = sorted(included_indices)
    match_set = set(match_indices)
    result_parts = []
    for i in sorted_indices:
        line_prefix = f"{i + 1:>6}\t"
        if i in match_set:
            line_prefix += ">>>\t"
        result_parts.append(f"{line_prefix}{lines[i]}")

    return "\n".join(result_parts)


class _ContentsFetchResult:
    """``repo.get_contents`` 调用结果，附带分支回退元数据。

    用于 read_file / list_directory 共用的非 PR 分支回退逻辑。

    Attributes:
        contents: GitHub content 对象（list 或单文件）；失败时为 None。
        branch_requested: AI 显式请求的分支名（PR 场景或未传时为 None）。
        branch_used: 实际读取成功的分支标识（PR 场景为 "HEAD"/"base"，
            非 PR 场景为真实分支名）；全部失败时为 None。
        tried_branches: 已尝试的所有分支标识列表，按尝试顺序记录。
        error: 失败时的简短错误描述（不含最终返回结构）；成功时为 None。
    """

    __slots__ = (
        "branch_requested",
        "branch_used",
        "contents",
        "error",
        "tried_branches",
    )

    def __init__(self) -> None:
        self.contents: Any = None
        self.branch_requested: str | None = None
        self.branch_used: str | None = None
        self.tried_branches: list[str] = []
        self.error: str | None = None

    @property
    def ok(self) -> bool:
        return self.contents is not None and self.error is None


def _normalize_branch(branch: str | None) -> str | None:
    """规整分支名：去除首尾空白，空字符串视为未传。"""
    normalized = (branch or "").strip()
    return normalized or None


class FileToolHandler:
    """文件工具处理器

    负责处理文件读取和目录列出工具调用。
    """

    def _get_tool_limits(self) -> dict:
        """从策略配置读取工具限制参数，确保返回整数类型"""
        ce = get_strategy_config().get_context_enhancement_config()
        return {
            "max_file_lines": int(ce.get("max_file_lines", MAX_FILE_LINES)),
            "default_context_lines": int(
                ce.get("default_context_lines", DEFAULT_CONTEXT_LINES)
            ),
            "max_context_lines": int(ce.get("max_context_lines", MAX_CONTEXT_LINES)),
        }

    def _fetch_contents(
        self,
        path: str,
        repo: Any,
        pr: Any,
        branch: str | None,
        *,
        path_kind: str = "路径",
    ) -> _ContentsFetchResult:
        """统一执行 ``repo.get_contents``，按场景选择 ref 并回退。

        - PR 场景：优先 HEAD，失败回退 base；不消费 ``branch``。
        - 非 PR 场景：优先显式 ``branch``，失败回退 ``repo.default_branch``。

        Args:
            path: 文件或目录路径。
            repo: GitHub 仓库对象。
            pr: GitHub PR 对象（非 PR 场景为 None）。
            branch: AI 显式请求的分支名（仅非 PR 场景生效）。
            path_kind: 路径类型描述（"文件"/"目录"），用于日志。

        Returns:
            :class:`_ContentsFetchResult`，包含内容与分支回退元数据。
        """
        result = _ContentsFetchResult()

        if pr is not None:
            # PR 场景：优先从 HEAD 分支读取，失败则尝试 base 分支
            try:
                result.contents = repo.get_contents(path, pr.head.sha)
                result.tried_branches.append("HEAD")
                result.branch_used = "HEAD"
                logger.debug(f"从PR的HEAD分支读取{path_kind}成功: {path}")
                return result
            except Exception as head_error:
                logger.warning(
                    f"从PR的HEAD分支读取{path_kind}失败: {path}, 错误: {head_error}"
                )
                result.tried_branches.append("HEAD")

            try:
                result.contents = repo.get_contents(path, pr.base.sha)
                result.tried_branches.append("base")
                result.branch_used = "base"
                logger.debug(f"从PR的base分支读取{path_kind}成功: {path}")
                return result
            except Exception as base_error:
                logger.warning(
                    f"从PR的base分支读取{path_kind}也失败: {path}, 错误: {base_error}"
                )
                result.tried_branches.append("base")
                result.error = f"{path_kind}在PR的HEAD和base分支中都不存在"
                return result

        # 非 PR 场景（如 Issue 分析）：显式分支 -> 仓库默认分支
        normalized_branch = _normalize_branch(branch)
        result.branch_requested = normalized_branch
        default_branch = getattr(repo, "default_branch", None)

        candidate_refs: list[str] = []
        if normalized_branch:
            candidate_refs.append(normalized_branch)
        if default_branch:
            candidate_refs.append(default_branch)

        last_error: str | None = None
        for ref in candidate_refs:
            result.tried_branches.append(ref)
            try:
                result.contents = repo.get_contents(path, ref)
                result.branch_used = ref
                logger.debug(f"从分支 {ref} 读取{path_kind}成功: {path}")
                return result
            except Exception as e:
                last_error = str(e)
                logger.warning(f"从分支 {ref} 读取{path_kind}失败: {path}, 错误: {e}")
                if normalized_branch and ref == normalized_branch:
                    logger.warning(
                        f"分支 {normalized_branch} 读取{path_kind} {path} 失败，尝试回退到默认分支"
                    )
                continue

        tried_desc = ", ".join(candidate_refs) if candidate_refs else "无可用分支"
        if last_error is not None:
            result.error = (
                f"{path_kind}不存在或无法访问（已尝试: {tried_desc}）: {last_error}"
            )
        else:
            result.error = f"无法获取{path_kind}内容：未找到可用分支"
        return result

    async def read_file(
        self,
        file_path: str,
        repo: Any,
        pr: Any,
        start_line: int | None = None,
        end_line: int | None = None,
        search_pattern: str | None = None,
        context_lines: int | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """读取文件内容的工具实现

        支持三种模式：
        1. 完整读取（仅指定 file_path）
        2. 行范围读取（指定 start_line 和 end_line）
        3. 内容搜索（指定 search_pattern，返回匹配行及上下文）

        Args:
            file_path: 文件路径
            repo: GitHub仓库对象
            pr: GitHub PR对象
            start_line: 起始行号（从1开始，可选）
            end_line: 结束行号（从1开始，包含，可选）
            search_pattern: 搜索文本（可选，与行范围互斥）
            context_lines: 搜索上下文行数（可选，默认从配置读取）
            branch: 非 PR 场景下指定读取的分支名（可选）；PR 场景忽略此参数

        Returns:
            文件内容字典
        """
        try:
            # 检查是否应该跳过该路径
            if get_strategy_config().is_path_skipped(file_path):
                logger.info(f"跳过读取文件（在skip_paths中）: {file_path}")
                return {
                    "file_path": file_path,
                    "error": "该路径在跳过列表中，无法访问",
                }

            # 参数互斥校验
            if start_line is not None and search_pattern is not None:
                return {
                    "file_path": file_path,
                    "error": "不能同时指定 start_line/end_line 和 search_pattern",
                    "hint": "请选择行范围读取或内容搜索其中一种模式",
                }

            # 行范围参数校验
            if start_line is not None or end_line is not None:
                if start_line is None or end_line is None:
                    return {
                        "file_path": file_path,
                        "error": "start_line 和 end_line 必须同时指定",
                        "hint": "例如：start_line=100, end_line=150",
                    }
                if start_line < 1:
                    return {
                        "file_path": file_path,
                        "error": "start_line 必须大于等于 1",
                    }
                if end_line < start_line:
                    return {
                        "file_path": file_path,
                        "error": "end_line 必须大于等于 start_line",
                        "hint": f"当前值: start_line={start_line}, end_line={end_line}",
                    }

            # 读取工具限制配置
            limits = self._get_tool_limits()

            # 处理 context_lines 参数
            effective_context_lines = limits["default_context_lines"]
            if context_lines is not None:
                effective_context_lines = max(
                    0, min(context_lines, limits["max_context_lines"])
                )

            # 智能分支选择 / Intelligent branch selection
            # PR 场景：HEAD -> base；非 PR 场景：显式 branch -> 默认分支
            fetch = self._fetch_contents(file_path, repo, pr, branch, path_kind="文件")
            content_file = fetch.contents
            branch_requested = fetch.branch_requested
            branch_used = fetch.branch_used
            tried_branches = fetch.tried_branches

            if not fetch.ok:
                hint = (
                    "这可能是一个新增的文件，请基于PR diff中的patch进行审查"
                    if pr is not None
                    else "请确认文件路径是否正确，或检查仓库访问权限"
                )
                return {
                    "file_path": file_path,
                    "error": fetch.error or "无法获取文件内容",
                    "hint": hint,
                    "branch_requested": branch_requested,
                    "branch_used": branch_used,
                    "tried_branches": tried_branches,
                }

            # GitHub API returns a list when the path is a directory
            if isinstance(content_file, list):
                return {
                    "file_path": file_path,
                    "error": "该路径是目录而非文件，请使用 list_directory 工具",
                    "hint": f"目录包含 {len(content_file)} 个项目",
                    "branch_requested": branch_requested,
                    "branch_used": branch_used,
                    "tried_branches": tried_branches,
                }

            if content_file.size > MAX_FILE_SIZE_BYTES:
                return {
                    "file_path": file_path,
                    "error": "文件过大",
                    "size": content_file.size,
                    "content": None,
                    "tried_branches": tried_branches,
                    "branch_requested": branch_requested,
                    "branch_used": branch_used,
                    "hint": "请基于PR diff中的patch进行审查，避免读取完整文件",
                }

            # 解码文件内容
            content = content_file.decoded_content.decode("utf-8")

            # 分割为行列表
            lines = content.split("\n")
            total_lines = len(lines)

            # 模式1: 行范围读取
            if start_line is not None and end_line is not None:
                # 转换为0-based索引
                start_idx = max(0, start_line - 1)
                end_idx = min(len(lines), end_line)

                if start_idx >= len(lines):
                    return {
                        "file_path": file_path,
                        "error": f"start_line {start_line} 超出文件范围",
                        "total_lines": total_lines,
                        "branch": branch_used or "unknown",
                        "branch_requested": branch_requested,
                        "branch_used": branch_used,
                        "tried_branches": tried_branches,
                    }

                selected_lines = lines[start_idx:end_idx]
                # 为每行添加行号前缀
                numbered_content = "\n".join(
                    f"{start_idx + i + 1:>6}\t{line}"
                    for i, line in enumerate(selected_lines)
                )
                return {
                    "file_path": file_path,
                    "content": numbered_content,
                    "mode": "line_range",
                    "start_line": start_line,
                    "end_line": min(end_line, total_lines),
                    "total_lines": total_lines,
                    "returned_lines": len(selected_lines),
                    "size": content_file.size,
                    "branch": branch_used or "unknown",
                    "branch_requested": branch_requested,
                    "branch_used": branch_used,
                    "tried_branches": tried_branches,
                }

            # 模式2: 内容搜索
            if search_pattern is not None:
                matches = []
                search_lower = search_pattern.lower()
                for idx, line in enumerate(lines):
                    if search_lower in line.lower():
                        matches.append(idx)  # 0-based index

                if not matches:
                    return {
                        "file_path": file_path,
                        "mode": "search",
                        "search_pattern": search_pattern,
                        "total_lines": total_lines,
                        "match_count": 0,
                        "content": None,
                        "message": f"未找到包含 '{search_pattern}' 的行",
                        "branch": branch_used or "unknown",
                        "branch_requested": branch_requested,
                        "branch_used": branch_used,
                        "tried_branches": tried_branches,
                    }

                numbered_content = format_search_results(
                    lines, matches, effective_context_lines
                )

                return {
                    "file_path": file_path,
                    "content": numbered_content,
                    "mode": "search",
                    "search_pattern": search_pattern,
                    "total_lines": total_lines,
                    "match_count": len(matches),
                    "context_lines": effective_context_lines,
                    "returned_lines": len(numbered_content.split("\n")),
                    "size": content_file.size,
                    "branch": branch_used or "unknown",
                    "branch_requested": branch_requested,
                    "branch_used": branch_used,
                    "tried_branches": tried_branches,
                    "hint": (
                        f"共找到 {len(matches)} 处匹配。"
                        f"如需查看更多上下文，可增大 context_lines 参数（当前 {effective_context_lines}）。"
                        f"如需查看特定匹配附近的完整代码，请使用行范围读取。"
                    ),
                }

            # 模式3: 完整读取（默认，向后兼容）
            max_file_lines = limits["max_file_lines"]
            if total_lines > max_file_lines:
                truncated_lines = lines[:max_file_lines]
                numbered_content = "\n".join(
                    f"{i + 1:>6}\t{line}" for i, line in enumerate(truncated_lines)
                )
                logger.warning(
                    f"文件 {file_path} 过大 ({total_lines} 行)，已截断为前 {max_file_lines} 行"
                )
                return {
                    "file_path": file_path,
                    "content": numbered_content,
                    "mode": "full",
                    "size": content_file.size,
                    "total_lines": total_lines,
                    "returned_lines": max_file_lines,
                    "truncated_lines": max_file_lines,
                    "warning": (
                        f"文件过大，仅显示前 {max_file_lines} 行（共 {total_lines} 行）。"
                        f"请使用 start_line/end_line 读取后续部分，"
                        f"或使用 search_pattern 搜索特定内容。"
                    ),
                    "branch": branch_used or "unknown",
                    "branch_requested": branch_requested,
                    "branch_used": branch_used,
                    "tried_branches": tried_branches,
                }

            # 正常大小文件 - 也添加行号
            numbered_content = "\n".join(
                f"{i + 1:>6}\t{line}" for i, line in enumerate(lines)
            )
            return {
                "file_path": file_path,
                "content": numbered_content,
                "mode": "full",
                "size": content_file.size,
                "total_lines": total_lines,
                "returned_lines": total_lines,
                "branch": branch_used or "unknown",
                "branch_requested": branch_requested,
                "branch_used": branch_used,
                "tried_branches": tried_branches,
            }

        except Exception as e:
            logger.error(f"读取文件 {file_path} 时发生未预期的错误: {e}", exc_info=True)
            return {
                "file_path": file_path,
                "error": f"读取文件时发生错误: {e!s}",
                "hint": "请检查文件路径是否正确，或基于PR diff进行审查",
            }

    async def list_directory(
        self,
        directory: str,
        repo: Any,
        pr: Any,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """列出目录内容的工具实现

        Args:
            directory: 目录路径
            repo: GitHub仓库对象
            pr: GitHub PR对象
            branch: 非 PR 场景下指定列出的分支名（可选）；PR 场景忽略此参数

        Returns:
            目录内容字典
        """
        try:
            # 检查是否应该跳过该路径
            if get_strategy_config().is_path_skipped(directory):
                logger.info(f"跳过列出目录（在skip_paths中）: {directory}")
                return {
                    "directory": directory,
                    "error": "该路径在跳过列表中，无法访问",
                    "items": [],
                    "count": 0,
                }

            # 智能分支选择 / Intelligent branch selection
            # PR 场景：HEAD -> base；非 PR 场景：显式 branch -> 默认分支
            fetch = self._fetch_contents(directory, repo, pr, branch, path_kind="目录")
            contents = fetch.contents
            branch_requested = fetch.branch_requested
            branch_used = fetch.branch_used
            tried_branches = fetch.tried_branches

            if not fetch.ok:
                hint = (
                    "这可能是一个新增的目录，请基于PR diff中的patch进行审查"
                    if pr is not None
                    else "请确认目录路径是否正确，或检查仓库访问权限"
                )
                return {
                    "directory": directory,
                    "error": fetch.error or "无法获取目录内容",
                    "hint": hint,
                    "items": [],
                    "count": 0,
                    "branch_requested": branch_requested,
                    "branch_used": branch_used,
                    "tried_branches": tried_branches,
                }

            if isinstance(contents, list):
                items = []
                # 过滤掉skip_paths中的项目 / Filter out items in skip_paths
                strategy_cfg = get_strategy_config()
                for item in contents:
                    if strategy_cfg.is_path_skipped(item.path):
                        continue
                    items.append(
                        {
                            "name": item.name,
                            "path": item.path,
                            "type": item.type,
                            "size": item.size if item.type == "file" else None,
                        }
                    )

                return {
                    "directory": directory,
                    "items": items,
                    "count": len(items),
                    "filtered": (
                        len(contents) - len(items) if len(items) < len(contents) else 0
                    ),
                    "branch": branch_used or "unknown",
                    "branch_requested": branch_requested,
                    "branch_used": branch_used,
                    "tried_branches": tried_branches,
                }
            else:
                # 单个文件 - 也需要检查skip_paths / Single file: also check skip_paths
                if get_strategy_config().is_path_skipped(contents.path):
                    return {
                        "directory": directory,
                        "error": "该路径在跳过列表中",
                        "items": [],
                        "count": 0,
                        "branch_requested": branch_requested,
                        "branch_used": branch_used,
                        "tried_branches": tried_branches,
                    }

                # 单个文件
                return {
                    "directory": directory,
                    "items": [
                        {
                            "name": contents.name,
                            "path": contents.path,
                            "type": contents.type,
                            "size": contents.size,
                        }
                    ],
                    "count": 1,
                    "branch": branch_used or "unknown",
                    "branch_requested": branch_requested,
                    "branch_used": branch_used,
                    "tried_branches": tried_branches,
                }

        except Exception as e:
            logger.error(f"列出目录 {directory} 时发生未预期的错误: {e}", exc_info=True)
            return {
                "directory": directory,
                "error": f"列出目录时发生错误: {e!s}",
                "hint": "请检查目录路径是否正确，或基于PR diff进行审查",
                "items": [],
                "count": 0,
            }
