"""Agent 专家团队文件读写工具

在工作区内安全地读取、写入、列举文件。
所有路径都经过工作区安全校验。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from backend.services.agent_team.workspace_service import (
    AgentTeamWorkspaceService,
    WorkspaceSecurityError,
)


@dataclass(frozen=True)
class FileReadResult:
    """文件读取结果。"""

    path: str
    content: str
    size: int
    exists: bool


@dataclass(frozen=True)
class FileWriteResult:
    """文件写入结果。"""

    path: str
    size: int
    created: bool


@dataclass(frozen=True)
class FileEditResult:
    """精确替换编辑结果。"""

    path: str
    replacements: int
    size: int


@dataclass(frozen=True)
class FileEntry:
    """目录列表项。"""

    path: str
    is_dir: bool
    size: int


class AgentTeamFileTools:
    """Agent 工作区内文件读写工具集。"""

    # 不允许读写这些路径（相对工作区根）
    BLOCKED_PATTERNS = (
        ".git/",
        ".env",
        ".ssh/",
        "__pycache__/",
        "node_modules/",
    )

    def __init__(
        self,
        workspace: str | Path,
        workspace_service: AgentTeamWorkspaceService | None = None,
    ):
        self.workspace_service = workspace_service or AgentTeamWorkspaceService()
        self.workspace = self.workspace_service.resolve_inside_workspace(workspace)

    def read_file(self, relative_path: str) -> FileReadResult:
        """读取工作区内文件。"""
        resolved = self._resolve(relative_path)
        self._check_blocked(relative_path)
        if not resolved.exists():
            return FileReadResult(path=relative_path, content="", size=0, exists=False)
        if resolved.is_dir():
            raise WorkspaceSecurityError(f"路径是目录，不是文件: {relative_path}")
        content = resolved.read_text(encoding="utf-8", errors="replace")
        return FileReadResult(
            path=relative_path,
            content=content,
            size=len(content),
            exists=True,
        )

    def write_file(self, relative_path: str, content: str) -> FileWriteResult:
        """写入工作区内文件。"""
        resolved = self._resolve(relative_path)
        self._check_blocked(relative_path)
        created = not resolved.exists()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return FileWriteResult(path=relative_path, size=len(content), created=created)

    def list_files(
        self, relative_dir: str = ".", recursive: bool = False
    ) -> list[FileEntry]:
        """列举工作区内目录。"""
        resolved = self._resolve(relative_dir)
        if not resolved.exists() or not resolved.is_dir():
            return []
        entries: list[FileEntry] = []
        if recursive:
            for child in resolved.rglob("*"):
                rel = child.relative_to(self.workspace).as_posix()
                if self._is_blocked_path(rel):
                    continue
                entries.append(
                    FileEntry(
                        path=rel,
                        is_dir=child.is_dir(),
                        size=child.stat().st_size if child.is_file() else 0,
                    )
                )
        else:
            for child in resolved.iterdir():
                rel = child.relative_to(self.workspace).as_posix()
                if self._is_blocked_path(rel):
                    continue
                entries.append(
                    FileEntry(
                        path=rel,
                        is_dir=child.is_dir(),
                        size=child.stat().st_size if child.is_file() else 0,
                    )
                )
        return entries

    async def read_file_async(self, relative_path: str) -> FileReadResult:
        """异步包装：读取工作区内文件。"""
        return await asyncio.to_thread(self.read_file, relative_path)

    async def write_file_async(self, relative_path: str, content: str) -> FileWriteResult:
        """异步包装：写入工作区内文件。"""
        return await asyncio.to_thread(self.write_file, relative_path, content)

    async def list_files_async(
        self, relative_dir: str = ".", recursive: bool = False
    ) -> list[FileEntry]:
        """异步包装：列举工作区内目录。"""
        return await asyncio.to_thread(self.list_files, relative_dir, recursive)

    async def edit_file_async(
        self,
        relative_path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> FileEditResult:
        """异步包装：精确字符串替换。"""
        return await asyncio.to_thread(
            self.edit_file,
            relative_path,
            old_text,
            new_text,
            replace_all,
        )

    async def replace_lines_async(
        self,
        relative_path: str,
        start_line: int,
        end_line: int,
        new_content: str,
    ) -> FileEditResult:
        """异步包装：按行号范围替换。"""
        return await asyncio.to_thread(
            self.replace_lines,
            relative_path,
            start_line,
            end_line,
            new_content,
        )

    async def insert_lines_async(
        self,
        relative_path: str,
        after_line: int,
        content: str,
    ) -> FileEditResult:
        """异步包装：在指定行号之后插入。"""
        return await asyncio.to_thread(
            self.insert_lines,
            relative_path,
            after_line,
            content,
        )

    def edit_file(
        self,
        relative_path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> FileEditResult:
        """精确字符串替换：在文件中查找 old_text 并替换为 new_text。

        - 默认只替换第一个匹配；replace_all=True 替换所有匹配。
        - 如果 old_text 不存在则报错。
        - 如果有多处匹配且未指定 replace_all，会报错要求提供更多上下文使匹配唯一。
        """
        resolved = self._resolve(relative_path)
        self._check_blocked(relative_path)
        if not resolved.exists():
            raise FileNotFoundError(f"文件不存在: {relative_path}")
        if resolved.is_dir():
            raise WorkspaceSecurityError(f"路径是目录，不是文件: {relative_path}")

        content = resolved.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count == 0:
            raise ValueError(
                f"在 {relative_path} 中未找到要替换的文本。"
                "请先用 read_file 查看文件内容，确保 old_text 与文件中的文本完全一致（包括缩进和空行）。"
            )
        if not replace_all and count > 1:
            raise ValueError(
                f"在 {relative_path} 中找到 {count} 处匹配。"
                "请扩大 old_text 的范围（包含更多上下文行）使匹配唯一，"
                "或者使用 replace_all=true 替换所有匹配。"
            )
        content = content.replace(old_text, new_text, -1 if replace_all else 1)
        resolved.write_text(content, encoding="utf-8")
        return FileEditResult(
            path=relative_path,
            replacements=count if replace_all else 1,
            size=len(content),
        )

    def replace_lines(
        self,
        relative_path: str,
        start_line: int,
        end_line: int,
        new_content: str,
    ) -> FileEditResult:
        """按行号范围替换：将文件的 start_line 到 end_line（含）替换为 new_content。

        行号从 1 开始。new_content 是替换后的内容（不含末尾换行）。
        start_line 和 end_line 是 read_file 输出中看到的行号。
        """
        resolved = self._resolve(relative_path)
        self._check_blocked(relative_path)
        if not resolved.exists():
            raise FileNotFoundError(f"文件不存在: {relative_path}")
        if resolved.is_dir():
            raise WorkspaceSecurityError(f"路径是目录，不是文件: {relative_path}")
        if start_line < 1:
            raise ValueError(f"start_line 必须 >= 1，当前: {start_line}")
        if end_line < start_line:
            raise ValueError(f"end_line ({end_line}) 不能小于 start_line ({start_line})")

        content = resolved.read_text(encoding="utf-8")
        lines = content.split("\n")
        total = len(lines)

        if start_line > total:
            raise ValueError(f"start_line ({start_line}) 超出文件总行数 ({total})")

        safe_end = min(end_line, total)
        new_lines = new_content.split("\n")

        # 替换 [start_line-1 : safe_end] 为 new_lines
        result_lines = lines[: start_line - 1] + new_lines + lines[safe_end:]
        result_content = "\n".join(result_lines)
        resolved.write_text(result_content, encoding="utf-8")

        replaced_count = safe_end - start_line + 1
        return FileEditResult(
            path=relative_path,
            replacements=replaced_count,
            size=len(result_content),
        )

    def insert_lines(
        self,
        relative_path: str,
        after_line: int,
        content: str,
    ) -> FileEditResult:
        """在指定行号之后插入新内容。

        after_line=0 表示在文件开头插入（第 1 行之前）。
        content 是要插入的文本。
        """
        resolved = self._resolve(relative_path)
        self._check_blocked(relative_path)
        if not resolved.exists():
            raise FileNotFoundError(f"文件不存在: {relative_path}")
        if resolved.is_dir():
            raise WorkspaceSecurityError(f"路径是目录，不是文件: {relative_path}")
        if after_line < 0:
            raise ValueError(f"after_line 必须 >= 0，当前: {after_line}")

        file_content = resolved.read_text(encoding="utf-8")
        lines = file_content.split("\n")
        total = len(lines)

        if after_line > total:
            raise ValueError(f"after_line ({after_line}) 超出文件总行数 ({total})")

        insert_lines = content.split("\n")
        result_lines = lines[:after_line] + insert_lines + lines[after_line:]
        result_content = "\n".join(result_lines)
        resolved.write_text(result_content, encoding="utf-8")

        return FileEditResult(
            path=relative_path,
            replacements=len(insert_lines),
            size=len(result_content),
        )

    def file_exists(self, relative_path: str) -> bool:
        """检查文件是否存在。"""
        resolved = self._resolve(relative_path)
        return resolved.exists() and resolved.is_file()

    def _resolve(self, relative_path: str) -> Path:
        return self.workspace_service.resolve_inside_workspace(
            self.workspace, relative_path
        )

    def _check_blocked(self, relative_path: str) -> None:
        normalized = relative_path.replace("\\", "/")
        for pattern in self.BLOCKED_PATTERNS:
            if pattern in normalized or normalized.startswith(pattern.rstrip("/")):
                raise WorkspaceSecurityError(
                    f"不允许访问的路径: {relative_path}"
                )

    def _is_blocked_path(self, relative_path: str) -> bool:
        normalized = relative_path.replace("\\", "/")
        for pattern in self.BLOCKED_PATTERNS:
            if normalized.startswith(pattern) or f"/{pattern}" in normalized:
                return True
        return False
