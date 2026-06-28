"""一键应用 suggestion 块的缩进兜底测试。

GitHub 渲染 suggestion 块时逐字替换被评论的代码行：块内写什么缩进，应用后就是
什么缩进。当 AI 在 SUGGESTION 里输出顶格代码（丢失原代码缩进）时，一键应用后
会破坏代码缩进。解析层（review_protocol）只会忠实保留 AI 写下的缩进，因此这里
覆盖工程层基于原代码行缩进的兜底对齐：从 patch 提取每行原缩进，对缩进不足的
suggestion 块补齐到原缩进（保持内部相对缩进不变）。
"""

from types import SimpleNamespace

from backend.services.comment_service import CommentService
from backend.services.pr_analyzer import PRAnalyzer, PRFileInfo


# ---------------------------------------------------------------------------
# comment_service: suggestion 缩进兜底
# ---------------------------------------------------------------------------


def _comment(body: str, *, file_path="src/Main.java", line=10, start_line=10):
    return {
        "file_path": file_path,
        "line_number": line,
        "start_line": start_line,
        "body": body,
        "severity": "major",
    }


def _suggestion_body(code: str) -> str:
    return f"**Title**\n\nDesc.\n\n**Suggestion:**\n```suggestion\n{code}\n```"


def test_realigns_unindented_single_line_suggestion():
    """AI 顶格输出单行 suggestion，原代码有缩进 → 补齐到原缩进（用户报告场景）。"""
    service = CommentService()
    analysis = SimpleNamespace(original_indent_map={"src/Main.java": {10: "    "}})
    comment = _comment(
        _suggestion_body(
            'System.out.println("Loading FireflyMC " + FireflyMCMod.VERSION);'
        )
    )

    service._realign_suggestion_indentation(comment, analysis)

    assert "```suggestion\n    System.out.println(" in comment["body"]


def test_leaves_correctly_indented_suggestion_untouched():
    """AI 已写对缩进 → 原样保留，不二次修改。"""
    service = CommentService()
    analysis = SimpleNamespace(original_indent_map={"src/Main.java": {10: "    "}})
    comment = _comment(
        _suggestion_body(
            '    System.out.println("Loading FireflyMC " + FireflyMCMod.VERSION);'
        )
    )

    service._realign_suggestion_indentation(comment, analysis)

    assert "```suggestion\n    System.out.println(" in comment["body"]
    # 确认没有叠加到 8 空格
    assert "        System.out.println(" not in comment["body"]


def test_realigns_multiline_suggestion_preserving_relative_indent():
    """多行 suggestion 整体顶格 → 每行补齐原缩进，内部相对缩进保持。"""
    service = CommentService()
    analysis = SimpleNamespace(original_indent_map={"src/Main.java": {10: "    "}})
    code = "if (ready) {\n    start();\n}"
    comment = _comment(_suggestion_body(code))

    service._realign_suggestion_indentation(comment, analysis)

    expected = "```suggestion\n    if (ready) {\n        start();\n    }\n```"
    assert expected in comment["body"]


def test_skips_when_no_original_indent_info():
    """没有原代码缩进信息（如 analysis 未升级或行不在 diff 内）→ 安全跳过，不破坏。"""
    service = CommentService()
    analysis = SimpleNamespace(original_indent_map=None)
    body = _suggestion_body('System.out.println("x");')
    comment = _comment(body)

    service._realign_suggestion_indentation(comment, analysis)

    assert comment["body"] == body


def test_skips_comment_without_suggestion_block():
    """评论里没有 ```suggestion 块（overall finding 的文本建议）→ 跳过。"""
    service = CommentService()
    analysis = SimpleNamespace(original_indent_map={"src/Main.java": {10: "    "}})
    body = "**Title**\n\nDesc.\n\n**Suggestion:** consider adding retry."
    comment = _comment(body)

    service._realign_suggestion_indentation(comment, analysis)

    assert comment["body"] == body


def test_validate_inline_comments_realigns_suggestion_end_to_end():
    """端到端：_validate_inline_comments 验证通过后，顶格 suggestion 被对齐。"""
    service = CommentService()
    analysis = SimpleNamespace(
        changed_lines_map={"src/Main.java": {10}},
        hunk_boundaries={},
        original_indent_map={"src/Main.java": {10: "    "}},
    )
    inline_comments = [
        {
            "file_path": "src/Main.java",
            "line_number": 10,
            "body": (
                "**Hardcoded version**\n\nDesc.\n\n"
                "**Suggestion:**\n"
                "```suggestion\n"
                'System.out.println("Loading FireflyMC " + FireflyMCMod.VERSION);'
                "\n```"
            ),
            "severity": "minor",
        }
    ]

    validated = service._validate_inline_comments(inline_comments, analysis)

    assert len(validated) == 1
    assert "```suggestion\n    System.out.println(" in validated[0]["body"]


# ---------------------------------------------------------------------------
# pr_analyzer: 从 patch 提取每行原缩进
# ---------------------------------------------------------------------------


def test_extracts_original_indent_map_from_patch():
    """_extract_changed_lines 顺便记录每个 PR 后行的前导空白。"""
    analyzer = PRAnalyzer()
    patch = (
        "@@ -1,3 +1,4 @@\n"
        " class Demo {\n"
        "     void run() {\n"
        '+        System.out.println("x");\n'
        "     }\n"
    )
    code_files = [
        PRFileInfo(
            path="Demo.java",
            status="modified",
            additions=1,
            deletions=0,
            changes=1,
            patch=patch,
            is_code_file=True,
        )
    ]

    _, _, indent_map = analyzer._extract_changed_lines(code_files)

    assert indent_map["Demo.java"][1] == ""  # class Demo {
    assert indent_map["Demo.java"][2] == "    "  # void run() {
    assert indent_map["Demo.java"][3] == "        "  # println (added)
    assert indent_map["Demo.java"][4] == "    "  # }
