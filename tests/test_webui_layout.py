"""WebUI 基础布局回归测试。

锁定 ``base.html`` 主内容容器 ``<main>`` 必须包含防止页面被长内容
横向撑开的约束（WebUI 主内容区域宽度随内容撑开）。

核心契约：
  - ``min-w-0``：允许 flex 子项收缩到内容固有宽度以下，使内部
    ``overflow-x-auto`` 表格容器能各自独立横向滚动，而不是把整个
    ``<main>`` 乃至整页撑宽。
  - ``max-w-full``：约束主内容不超过父容器宽度（相对值，非硬编码数值，
    符合反硬编码限制偏好）。
  - ``overflow-x-hidden``：兜底裁剪未被滚动容器包裹的溢出内容，
    避免出现整页横向滚动条。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent / "backend" / "webui" / "templates"
)
BASE_HTML = TEMPLATES_DIR / "base.html"


def _extract_main_classes() -> list[str]:
    """读取 base.html，返回 ``<main>`` 元素的 class 列表。"""
    html = BASE_HTML.read_text(encoding="utf-8")
    match = re.search(r'<main\b[^>]*\bclass="([^"]*)"', html)
    assert match, "<main> element with class attribute not found in base.html"
    classes = match.group(1).split()
    assert classes, "<main> must have a non-empty class list"
    return classes


@pytest.fixture(scope="module")
def main_classes() -> list[str]:
    return _extract_main_classes()


def test_main_has_min_width_zero(main_classes: list[str]) -> None:
    """``<main>`` 必须能收缩，防止 flex 子项被内容固有宽度撑宽。"""
    assert "min-w-0" in main_classes


def test_main_has_max_width_full(main_classes: list[str]) -> None:
    """``<main>`` 宽度不得超过父容器（相对值，避免硬编码像素上限）。"""
    assert "max-w-full" in main_classes


def test_main_has_horizontal_overflow_guard(main_classes: list[str]) -> None:
    """``<main>`` 必须裁剪横向溢出，避免整页出现横向滚动条。"""
    assert "overflow-x-hidden" in main_classes
