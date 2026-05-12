"""路径、编码、行尾、差异等文件操作工具函数"""

from __future__ import annotations

import difflib
from pathlib import Path


# ── 编码与行尾 ────────────────────────────────────────


def detect_line_ending(raw: bytes) -> str:
    """检测字节序列的行尾风格。"""
    sample = raw[:4096]
    crlf_count = sample.count(b"\r\n")
    lf_count = sample.count(b"\n") - crlf_count
    return "\r\n" if crlf_count > lf_count else "\n"


def read_text_with_metadata(path: str | Path) -> tuple[str, str, str]:
    """读取文件，返回 (unix换行内容, 编码, 原始行尾)。

    自动处理 UTF-8 / UTF-16-LE 编码。
    """
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xff\xfe"):
        encoding = "utf-16-le"
    else:
        encoding = "utf-8"
    line_ending = detect_line_ending(raw)
    text = raw.decode(encoding)
    # 统一为 LF 方便内部处理
    text = text.replace("\r\n", "\n")
    return text, encoding, line_ending


def write_text_preserving(
    path: str | Path, content_lf: str, encoding: str, line_ending: str
) -> None:
    """写入文件，保留原始编码和行尾风格。"""
    final = (
        content_lf.replace("\n", line_ending) if line_ending == "\r\n" else content_lf
    )
    Path(path).write_bytes(final.encode(encoding))


# ── 差异生成 ──────────────────────────────────────────


def make_unified_diff(
    file_path: str, old_content: str, new_content: str, context_lines: int = 3
) -> str:
    """生成 unified diff。"""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=context_lines,
    )
    return "".join(diff)


# ── 容错匹配 ──────────────────────────────────────────


def normalize_quotes(text: str) -> str:
    """弯引号 → 直引号。"""
    return (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


def normalize_whitespace(text: str) -> str:
    """Tab → 4空格。"""
    return text.replace("\t", "    ")


def find_actual_string(file_content: str, search: str) -> str | None:
    """容错查找：依次尝试精确、弯引号归一、空格归一、两者组合。

    Returns:
        匹配到的原始文本（用于后续替换），或 None。
    """
    # 1. 精确匹配
    if search in file_content:
        return search

    # 2. 弯引号归一
    norm_file = normalize_quotes(file_content)
    norm_search = normalize_quotes(search)
    idx = norm_file.find(norm_search)
    if idx != -1:
        return file_content[idx : idx + len(search)]

    # 3. 空格归一
    ws_file = normalize_whitespace(file_content)
    ws_search = normalize_whitespace(search)
    idx = ws_file.find(ws_search)
    if idx != -1:
        return file_content[idx : idx + len(search)]

    # 4. 两者组合
    combined_file = normalize_whitespace(norm_file)
    combined_search = normalize_whitespace(norm_search)
    idx = combined_file.find(combined_search)
    if idx != -1:
        return file_content[idx : idx + len(search)]

    return None
