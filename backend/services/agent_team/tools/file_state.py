"""文件状态缓存 - 防止覆盖用户或外部修改

Read 工具读取文件后记录内容与 mtime，
Edit/Write/ReplaceLines/InsertLines 写入前检查文件是否在上次读取后被修改。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ReadFileEntry:
    """记录一次文件读取的状态。"""

    content: str
    mtime: float
    start_line: int | None = None
    end_line: int | None = None
    is_full_read: bool = True


class ReadFileState:
    """文件读取状态缓存。

    path(str(resolve)) → ReadFileEntry
    """

    def __init__(self) -> None:
        self._entries: dict[str, ReadFileEntry] = {}

    def get(self, path: str | Path) -> ReadFileEntry | None:
        return self._entries.get(str(Path(path).resolve()))

    def set(
        self,
        path: str | Path,
        content: str,
        mtime: float,
        start_line: int | None = None,
        end_line: int | None = None,
        is_full_read: bool = True,
    ) -> None:
        key = str(Path(path).resolve())
        self._entries[key] = ReadFileEntry(
            content=content,
            mtime=mtime,
            start_line=start_line,
            end_line=end_line,
            is_full_read=is_full_read,
        )

    def invalidate(self, path: str | Path) -> None:
        key = str(Path(path).resolve())
        self._entries.pop(key, None)

    def check_not_stale(self, path: str | Path) -> str | None:
        """检查文件自上次读取后是否被修改。

        Returns:
            None 表示安全，可以写入。
            str 表示错误信息，应该拒绝写入。
        """
        entry = self.get(path)
        if entry is None:
            return None  # 没有读取记录，允许（非强制模式）

        resolved = Path(path).resolve()
        if not resolved.exists():
            return None

        current_mtime = resolved.stat().st_mtime
        if current_mtime <= entry.mtime:
            return None  # mtime 没变化，安全

        # mtime 变了，但如果是完整读取且内容没变，也算安全
        if entry.is_full_read:
            current_content = resolved.read_text(encoding="utf-8", errors="replace")
            if current_content == entry.content:
                return None

        return (
            f"文件 {path} 在上次读取后被外部修改（mtime 变化）。"
            "请先重新 read_file 获取最新内容后再编辑。"
        )
