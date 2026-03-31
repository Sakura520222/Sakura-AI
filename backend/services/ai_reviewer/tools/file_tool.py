"""文件工具处理器

从原 ai_reviewer.py 迁移的文件工具相关方法：
- _tool_read_file (1476-1585行)
- _tool_list_directory (1587-1711行)
"""

from typing import Any, Dict, Optional

from loguru import logger

from backend.core.config import get_strategy_config
from backend.services.ai_reviewer.constants import (
    DEFAULT_CONTEXT_LINES,
    MAX_CONTEXT_LINES,
    MAX_FILE_LINES,
    MAX_FILE_SIZE_BYTES,
)


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

    async def read_file(
        self,
        file_path: str,
        repo: Any,
        pr: Any,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        search_pattern: Optional[str] = None,
        context_lines: Optional[int] = None,
    ) -> Dict[str, Any]:
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

        Returns:
            文件内容字典
        """
        try:
            # 检查是否应该跳过该路径
            skip_paths = get_strategy_config().get_file_filters().get("skip_paths", [])
            for skip_path in skip_paths:
                if file_path.startswith(skip_path.rstrip("/")):
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

            # 智能分支选择：优先尝试PR的HEAD分支
            content_file = None
            tried_branches = []

            # 1. 先尝试从PR的HEAD分支读取
            try:
                content_file = repo.get_contents(file_path, pr.head.sha)
                tried_branches.append("HEAD")
                logger.debug(f"✅ 从PR的HEAD分支读取文件成功: {file_path}")
            except Exception as head_error:
                logger.debug(
                    f"⚠️  从PR的HEAD分支读取失败: {file_path}, 错误: {head_error}"
                )

                # 2. 如果HEAD分支失败，尝试从base分支读取
                try:
                    content_file = repo.get_contents(file_path, pr.base.sha)
                    tried_branches.append("base")
                    logger.debug(f"✅ 从PR的base分支读取文件成功: {file_path}")
                except Exception as base_error:
                    logger.debug(
                        f"⚠️  从PR的base分支读取也失败: {file_path}, 错误: {base_error}"
                    )

                    return {
                        "file_path": file_path,
                        "error": "文件在PR的HEAD和base分支中都不存在",
                        "hint": "这可能是一个新增的文件，请基于PR diff中的patch进行审查",
                        "tried_branches": tried_branches,
                    }

            if not content_file:
                return {
                    "file_path": file_path,
                    "error": "无法获取文件内容",
                    "tried_branches": tried_branches,
                }

            if content_file.size > MAX_FILE_SIZE_BYTES:
                return {
                    "file_path": file_path,
                    "error": "文件过大",
                    "size": content_file.size,
                    "content": None,
                    "tried_branches": tried_branches,
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
                        "branch": tried_branches[0] if tried_branches else "unknown",
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
                    "branch": tried_branches[0] if tried_branches else "unknown",
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
                        "branch": tried_branches[0] if tried_branches else "unknown",
                    }

                # 收集匹配行及其上下文，使用集合避免重复
                included_indices = set()
                for match_idx in matches:
                    ctx_start = max(0, match_idx - effective_context_lines)
                    ctx_end = min(len(lines), match_idx + effective_context_lines + 1)
                    for i in range(ctx_start, ctx_end):
                        included_indices.add(i)

                # 按行号排序输出
                sorted_indices = sorted(included_indices)
                match_set = set(matches)
                result_parts = []
                for i in sorted_indices:
                    line_prefix = f"{i + 1:>6}\t"
                    if i in match_set:
                        line_prefix += ">>>\t"
                    result_parts.append(f"{line_prefix}{lines[i]}")

                numbered_content = "\n".join(result_parts)
                return {
                    "file_path": file_path,
                    "content": numbered_content,
                    "mode": "search",
                    "search_pattern": search_pattern,
                    "total_lines": total_lines,
                    "match_count": len(matches),
                    "context_lines": effective_context_lines,
                    "returned_lines": len(sorted_indices),
                    "size": content_file.size,
                    "branch": tried_branches[0] if tried_branches else "unknown",
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
                    "branch": tried_branches[0] if tried_branches else "unknown",
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
                "branch": tried_branches[0] if tried_branches else "unknown",
            }

        except Exception as e:
            logger.error(f"读取文件 {file_path} 时发生未预期的错误: {e}", exc_info=True)
            return {
                "file_path": file_path,
                "error": f"读取文件时发生错误: {str(e)}",
                "hint": "请检查文件路径是否正确，或基于PR diff进行审查",
            }

    async def list_directory(
        self, directory: str, repo: Any, pr: Any
    ) -> Dict[str, Any]:
        """列出目录内容的工具实现

        Args:
            directory: 目录路径
            repo: GitHub仓库对象
            pr: GitHub PR对象

        Returns:
            目录内容字典
        """
        try:
            # 检查是否应该跳过该路径
            skip_paths = get_strategy_config().get_file_filters().get("skip_paths", [])
            for skip_path in skip_paths:
                if directory.startswith(skip_path.rstrip("/")):
                    logger.info(f"跳过列出目录（在skip_paths中）: {directory}")
                    return {
                        "directory": directory,
                        "error": "该路径在跳过列表中，无法访问",
                        "items": [],
                        "count": 0,
                    }

            # 智能分支选择：优先尝试PR的HEAD分支
            contents = None
            tried_branches = []

            # 1. 先尝试从PR的HEAD分支读取
            try:
                contents = repo.get_contents(directory, pr.head.sha)
                tried_branches.append("HEAD")
                logger.debug(f"✅ 从PR的HEAD分支列出目录成功: {directory}")
            except Exception as head_error:
                logger.debug(
                    f"⚠️  从PR的HEAD分支列出目录失败: {directory}, 错误: {head_error}"
                )

                # 2. 如果HEAD分支失败，尝试从base分支读取
                try:
                    contents = repo.get_contents(directory, pr.base.sha)
                    tried_branches.append("base")
                    logger.debug(f"✅ 从PR的base分支列出目录成功: {directory}")
                except Exception as base_error:
                    logger.debug(
                        f"⚠️  从PR的base分支列出目录也失败: {directory}, 错误: {base_error}"
                    )

                    return {
                        "directory": directory,
                        "error": "目录在PR的HEAD和base分支中都不存在",
                        "hint": "这可能是一个新增的目录，请基于PR diff中的patch进行审查",
                        "items": [],
                        "count": 0,
                        "tried_branches": tried_branches,
                    }

            if isinstance(contents, list):
                items = []
                # 过滤掉skip_paths中的项目
                for item in contents:
                    should_skip = False
                    for skip_path in skip_paths:
                        if item.path.startswith(skip_path.rstrip("/")):
                            should_skip = True
                            break

                    if not should_skip:
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
                    "branch": tried_branches[0] if tried_branches else "unknown",
                }
            else:
                # 单个文件 - 也需要检查skip_paths
                for skip_path in skip_paths:
                    if contents.path.startswith(skip_path.rstrip("/")):
                        return {
                            "directory": directory,
                            "error": "该路径在跳过列表中",
                            "items": [],
                            "count": 0,
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
                    "branch": tried_branches[0] if tried_branches else "unknown",
                }

        except Exception as e:
            logger.error(f"列出目录 {directory} 时发生未预期的错误: {e}", exc_info=True)
            return {
                "directory": directory,
                "error": f"列出目录时发生错误: {str(e)}",
                "hint": "请检查目录路径是否正确，或基于PR diff进行审查",
                "items": [],
                "count": 0,
            }
