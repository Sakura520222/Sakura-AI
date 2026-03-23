"""文件工具处理器

从原 ai_reviewer.py 迁移的文件工具相关方法：
- _tool_read_file (1476-1585行)
- _tool_list_directory (1587-1711行)
"""

from typing import Any, Dict

from loguru import logger

from backend.core.config import get_strategy_config
from backend.services.ai_reviewer.constants import MAX_FILE_SIZE_BYTES, MAX_FILE_LINES


class FileToolHandler:
    """文件工具处理器

    负责处理文件读取和目录列出工具调用。
    """

    async def read_file(
        self, file_path: str, repo: Any, pr: Any
    ) -> Dict[str, Any]:
        """读取文件内容的工具实现

        Args:
            file_path: 文件路径
            repo: GitHub仓库对象
            pr: GitHub PR对象

        Returns:
            文件内容字典
        """
        try:
            # 检查是否应该跳过该路径
            skip_paths = get_strategy_config().get_file_filters().get(
                "skip_paths", []
            )
            for skip_path in skip_paths:
                if file_path.startswith(skip_path.rstrip("/")):
                    logger.info(f"跳过读取文件（在skip_paths中）: {file_path}")
                    return {
                        "file_path": file_path,
                        "error": "该路径在跳过列表中，无法访问",
                    }

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

            # 检查行数，超大文件只返回前500行
            lines = content.split("\n")
            if len(lines) > MAX_FILE_LINES:
                truncated_content = "\n".join(lines[:MAX_FILE_LINES])
                logger.warning(
                    f"文件 {file_path} 过大 ({len(lines)} 行)，已截断为前 {MAX_FILE_LINES} 行"
                )
                return {
                    "file_path": file_path,
                    "content": truncated_content,
                    "size": content_file.size,
                    "original_lines": len(lines),
                    "truncated_lines": MAX_FILE_LINES,
                    "warning": f"文件过大，仅显示前 {MAX_FILE_LINES} 行（共 {len(lines)} 行）",
                    "branch": tried_branches[0] if tried_branches else "unknown",
                }

            return {
                "file_path": file_path,
                "content": content,
                "size": content_file.size,
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
            skip_paths = get_strategy_config().get_file_filters().get(
                "skip_paths", []
            )
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
