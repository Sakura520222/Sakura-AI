"""PR Diff 工具处理器

当 prompt 超长时，将代码 diff 从初始 prompt 中移除，
改为提供此工具让 AI 按需查看特定文件的 diff。
"""

from typing import Any

from loguru import logger


class DiffToolHandler:
    """PR Diff 工具处理器

    负责处理 get_file_diff 工具调用，按需返回 PR 中指定文件的 diff 内容。
    """

    def __init__(self):
        """初始化，存储 PR 文件 diff 数据"""
        self._files_data: dict[str, dict[str, Any]] = {}

    def set_files_data(self, files: list[dict[str, Any]]) -> None:
        """设置当前 PR 的文件 diff 数据

        在审查开始前调用，将 context 中的 files 列表缓存到工具内部。

        Args:
            files: 文件信息列表，每个元素包含 path、status、patch 等字段
        """
        self._files_data = {}
        for file_info in files:
            path = file_info.get("path", "")
            if path:
                self._files_data[path] = file_info
        logger.debug("DiffTool 缓存了 {} 个文件的 diff 数据", len(self._files_data))

    def clear(self) -> None:
        """清除缓存的文件数据"""
        self._files_data.clear()

    @property
    def has_data(self) -> bool:
        """是否已有文件 diff 数据缓存"""
        return bool(self._files_data)

    async def get_file_diff(
        self,
        file_path: str,
    ) -> dict[str, Any]:
        """获取 PR 中指定文件的 diff 内容

        Args:
            file_path: 文件路径（相对于项目根目录）

        Returns:
            文件 diff 信息字典
        """
        if not file_path:
            return {"error": "缺少必填参数: file_path"}

        file_info = self._files_data.get(file_path)
        if not file_info:
            # 尝试模糊匹配
            available = list(self._files_data.keys())
            close_matches = [p for p in available if file_path in p]
            hint = ""
            if close_matches:
                hint = f"相近的文件: {close_matches[:5]}"
            return {
                "error": f"文件 '{file_path}' 不在 PR 变更列表中",
                "available_files": available[:20],
                "hint": hint,
            }

        patch = file_info.get("patch")
        if not patch:
            return {
                "file_path": file_path,
                "status": file_info.get("status", "unknown"),
                "changes": file_info.get("changes", 0),
                "additions": file_info.get("additions", 0),
                "deletions": file_info.get("deletions", 0),
                "info": "该文件没有 diff 内容（可能是二进制文件或仅有元数据变更）",
            }

        return {
            "file_path": file_path,
            "status": file_info.get("status", "unknown"),
            "changes": file_info.get("changes", 0),
            "additions": file_info.get("additions", 0),
            "deletions": file_info.get("deletions", 0),
            "diff": patch,
        }

    async def list_changed_files(self) -> dict[str, Any]:
        """列出 PR 中所有变更的文件概览

        Returns:
            变更文件列表
        """
        if not self._files_data:
            return {"error": "没有可用的文件 diff 数据"}

        files_list = []
        for path, info in self._files_data.items():
            files_list.append(
                {
                    "path": path,
                    "status": info.get("status", "unknown"),
                    "additions": info.get("additions", 0),
                    "deletions": info.get("deletions", 0),
                    "changes": info.get("changes", 0),
                    "has_diff": bool(info.get("patch")),
                }
            )

        return {
            "total_files": len(files_list),
            "files": files_list,
        }
