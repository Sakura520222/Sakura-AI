"""AI 文件工具 branch 参数测试

验证 Issue 分析等非 PR 场景下，read_file / list_directory / search_in_files
支持显式指定分支，并在无效分支时回退默认分支；PR 场景仍优先 PR HEAD/base。

覆盖计划 docs/plans/2026-06-26-issue-analysis-branch-file-tools.md Task 5 全部用例。
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.ai_reviewer.tools.file_tool import FileToolHandler
from backend.services.ai_reviewer.tools.handler import ToolHandler
from backend.services.ai_reviewer.tools.search_files_tool import (
    SearchFilesToolHandler,
)

# ── Fake 对象 ───────────────────────────────────────────


class _FakeContent:
    """模拟 PyGithub ContentFile，足够覆盖 read_file / list_directory 的访问。"""

    def __init__(self, path, data="", type_="file", name=None):
        self.path = path
        self.name = name or path.rsplit("/", 1)[-1]
        self.type = type_
        if isinstance(data, bytes):
            self.decoded_content = data
        else:
            self.decoded_content = data.encode("utf-8")
        self.size = len(self.decoded_content)


class _FakeTreeEntry:
    def __init__(self, path, type_="blob", size=0):
        self.path = path
        self.type = type_
        self.size = size


class _FakeRequester:
    """模拟 PyGithub requester.requestJsonAndCheck，返回固定 Search API 响应。"""

    def __init__(self, data):
        self._data = data
        self.call_count = 0

    def requestJsonAndCheck(self, method, url):
        self.call_count += 1
        return (None, self._data)


class _FakeTree:
    def __init__(self, paths, sizes=None):
        sizes = sizes or {}
        self.tree = [_FakeTreeEntry(p, size=sizes.get(p, 0)) for p in paths]


class _FakeRepo:
    """模拟 PyGithub Repository。

    Args:
        branches: {ref: {path: _FakeContent | list}}，用于 get_contents。
        trees: {ref: [paths]}，用于 get_git_tree（per_file 搜索路径）。
        default_branch: 默认分支名。
        requester: _FakeRequester，用于 Search API 路径的 requestJsonAndCheck。
    """

    def __init__(
        self,
        branches=None,
        trees=None,
        default_branch="main",
        full_name="owner/repo",
        requester=None,
    ):
        self.branches = branches or {}
        self.trees = trees or {}
        self.default_branch = default_branch
        self.full_name = full_name
        self._requester = requester

    def get_contents(self, path, ref=None):
        effective_ref = ref or self.default_branch
        tree = self.branches.get(effective_ref, {})
        if path not in tree:
            raise Exception(f"Not found: {path} @ {effective_ref}")
        return tree[path]

    def get_git_tree(self, sha=None, recursive=False):
        if sha not in self.trees:
            raise Exception(f"ref not found: {sha}")
        return self.trees[sha]


class _FakeToolCall:
    """模拟 OpenAI tool_call 对象。"""

    def __init__(self, name, arguments):
        self.function = SimpleNamespace(name=name, arguments=json.dumps(arguments))
        self.id = f"call_{name}"


class _FileStrategyConfig:
    """FileToolHandler 依赖的策略配置替身。"""

    def is_path_skipped(self, path):
        return False

    def get_context_enhancement_config(self):
        return {
            "max_file_lines": 500,
            "default_context_lines": 20,
            "max_context_lines": 200,
        }

    def get_file_filters(self):
        return {"skip_paths": []}


class _SearchStrategyConfig:
    """SearchFilesToolHandler 依赖的策略配置替身。

    Args:
        use_search_api: 是否走 GitHub Search API 主路径（False 强制 per_file）。
    """

    def __init__(self, use_search_api: bool = False):
        self._use_search_api = use_search_api

    def get_context_enhancement_config(self):
        return {
            "search_in_files": {
                "default_context_lines": 3,
                "default_max_results": 20,
                "skip_binary": True,
                "use_search_api": self._use_search_api,
                "max_files_to_search": 100,
            }
        }

    def get_file_filters(self):
        return {"skip_paths": []}


@pytest.fixture
def file_strategy(monkeypatch):
    cfg = _FileStrategyConfig()
    monkeypatch.setattr(
        "backend.services.ai_reviewer.tools.file_tool.get_strategy_config",
        lambda: cfg,
    )
    return cfg


@pytest.fixture
def search_strategy(monkeypatch):
    """per_file 路径配置（use_search_api=False）。"""
    cfg = _SearchStrategyConfig(use_search_api=False)
    monkeypatch.setattr(
        "backend.services.ai_reviewer.tools.search_files_tool.get_strategy_config",
        lambda: cfg,
    )
    return cfg


@pytest.fixture
def search_strategy_api(monkeypatch):
    """Search API 主路径配置（use_search_api=True）。"""
    cfg = _SearchStrategyConfig(use_search_api=True)
    monkeypatch.setattr(
        "backend.services.ai_reviewer.tools.search_files_tool.get_strategy_config",
        lambda: cfg,
    )
    return cfg


# ── read_file 非 PR 场景 ─────────────────────────────────


@pytest.mark.asyncio
async def test_read_file_non_pr_with_branch_uses_specified_branch(file_strategy):
    """非 PR 场景传入有效 branch 时，从该分支读取内容。"""
    repo = _FakeRepo(
        branches={
            "feature/x": {"a.py": _FakeContent("a.py", "branch-x-content\n")},
            "main": {"a.py": _FakeContent("a.py", "main-content\n")},
        },
        default_branch="main",
    )
    handler = FileToolHandler()
    result = await handler.read_file("a.py", repo, pr=None, branch="feature/x")

    assert result["branch_used"] == "feature/x"
    assert result["branch_requested"] == "feature/x"
    assert result["branch"] == "feature/x"
    assert "branch-x-content" in result["content"]
    assert "main-content" not in result["content"]


@pytest.mark.asyncio
async def test_read_file_non_pr_invalid_branch_falls_back(file_strategy):
    """非 PR 场景指定分支不可访问时，回退默认分支。"""
    repo = _FakeRepo(
        branches={"main": {"a.py": _FakeContent("a.py", "main-content\n")}},
        default_branch="main",
    )
    handler = FileToolHandler()
    result = await handler.read_file("a.py", repo, pr=None, branch="feature/missing")

    assert result["branch_used"] == "main"
    assert result["branch_requested"] == "feature/missing"
    assert result["tried_branches"] == ["feature/missing", "main"]
    assert result["ref_used"] == "main"
    assert result["tried_refs"] == ["feature/missing", "main"]
    assert "main-content" in result["content"]


@pytest.mark.asyncio
async def test_read_file_non_pr_no_branch_uses_default(file_strategy):
    """非 PR 场景不传 branch 时，行为与默认分支读取一致。"""
    repo = _FakeRepo(
        branches={"main": {"a.py": _FakeContent("a.py", "main-content\n")}},
        default_branch="main",
    )
    handler = FileToolHandler()
    result = await handler.read_file("a.py", repo, pr=None)

    assert result["branch_used"] == "main"
    assert result["branch_requested"] is None
    assert "main-content" in result["content"]


@pytest.mark.asyncio
async def test_read_file_large_file_line_range_is_allowed(file_strategy):
    """大文件按请求行范围读取，而不是按文件字节数直接拒绝。"""
    content = "\n".join(f"line {i}" for i in range(1, 151))
    fake_content = _FakeContent("large.sh", content)
    fake_content.size = 262335
    repo = _FakeRepo(
        branches={"main": {"large.sh": fake_content}},
        default_branch="main",
    )
    handler = FileToolHandler()

    result = await handler.read_file(
        "large.sh", repo, pr=None, start_line=1, end_line=100
    )

    assert "error" not in result
    assert result["mode"] == "line_range"
    assert result["returned_lines"] == 100
    assert result["size"] == 262335
    assert result["content"].splitlines()[0].endswith("line 1")
    assert result["content"].splitlines()[-1].endswith("line 100")


@pytest.mark.asyncio
async def test_read_file_large_file_search_is_allowed(file_strategy):
    """大文件搜索只返回匹配上下文，不因文件字节数提前跳过。"""
    content = "\n".join(
        f"line {i}" if i != 125 else "line 125 with keyword" for i in range(1, 151)
    )
    fake_content = _FakeContent("large.sh", content)
    fake_content.size = 262335
    repo = _FakeRepo(
        branches={"main": {"large.sh": fake_content}},
        default_branch="main",
    )
    handler = FileToolHandler()

    result = await handler.read_file(
        "large.sh",
        repo,
        pr=None,
        search_pattern="keyword",
        context_lines=1,
    )

    assert "error" not in result
    assert result["mode"] == "search"
    assert result["match_count"] == 1
    assert result["returned_lines"] == 3
    assert "keyword" in result["content"]


@pytest.mark.asyncio
async def test_read_file_large_file_full_read_is_line_truncated(file_strategy):
    """大文件完整读取仍按输出行数截断，并保留任务无关的后续读取提示。"""
    content = "\n".join(f"line {i}" for i in range(1, 551))
    fake_content = _FakeContent("large.sh", content)
    fake_content.size = 262335
    repo = _FakeRepo(
        branches={"main": {"large.sh": fake_content}},
        default_branch="main",
    )
    handler = FileToolHandler()

    result = await handler.read_file("large.sh", repo, pr=None)

    assert "error" not in result
    assert result["mode"] == "full"
    assert result["total_lines"] == 550
    assert result["returned_lines"] == 500
    assert result["truncated_lines"] == 500
    assert result["content"].splitlines()[-1].endswith("line 500")
    assert "PR" not in result["warning"]
    assert "start_line/end_line" in result["warning"]
    assert "search_pattern" in result["warning"]


@pytest.mark.asyncio
async def test_read_file_line_range_output_is_capped_without_rejecting(
    file_strategy,
):
    """超大行范围请求返回首个输出窗口，并给出下一段读取参数。"""
    content = "\n".join(f"line {i}" for i in range(1, 601))
    fake_content = _FakeContent("large.sh", content)
    fake_content.size = 1_000_000
    repo = _FakeRepo(
        branches={"main": {"large.sh": fake_content}},
        default_branch="main",
    )
    handler = FileToolHandler()

    result = await handler.read_file(
        "large.sh", repo, pr=None, start_line=1, end_line=600
    )

    assert "error" not in result
    assert result["end_line"] == 500
    assert result["returned_lines"] == 500
    assert result["line_range"]["status"] == "output_truncated"
    assert result["line_range"]["truncated"] is True
    assert result["line_range"]["output_line_limit"] == 500
    assert "start_line=501" in result["hint"]
    assert "end_line=600" in result["hint"]


@pytest.mark.asyncio
async def test_read_file_line_range_reports_combined_truncation(file_strategy):
    """行范围同时越界并超过输出上限时，两种截断原因都保持可见。"""
    content = "\n".join(f"line {i}" for i in range(1, 551))
    fake_content = _FakeContent("large.sh", content)
    fake_content.size = 1_000_000
    repo = _FakeRepo(
        branches={"main": {"large.sh": fake_content}},
        default_branch="main",
    )
    handler = FileToolHandler()

    result = await handler.read_file(
        "large.sh", repo, pr=None, start_line=1, end_line=600
    )

    assert result["line_range"]["status"] == "end_line_and_output_truncated"
    assert result["line_range"]["output_line_limit"] == 500
    assert "超出当前文件总行数" in result["hint"]
    assert "单次返回上限" in result["hint"]
    assert "start_line=501" in result["hint"]
    assert "end_line=550" in result["hint"]


@pytest.mark.asyncio
async def test_read_file_char_budget_reports_actual_lines_and_recovery(
    file_strategy,
):
    """Character truncation cannot claim selected lines that were not returned."""
    file_strategy.get_context_enhancement_config = lambda: {
        "max_file_lines": 500,
        "max_file_output_chars": 100,
        "default_context_lines": 20,
        "max_context_lines": 200,
    }
    content = "\n".join(f"line-{i}: {'x' * 30}" for i in range(1, 501))
    repo = _FakeRepo(
        branches={"main": {"long.txt": _FakeContent("long.txt", content)}},
        default_branch="main",
    )
    handler = FileToolHandler()

    result = await handler.read_file(
        "long.txt", repo, pr=None, start_line=1, end_line=500
    )

    assert result["returned_lines"] == 3
    assert result["end_line"] == 3
    assert result["line_range"]["returned"] == {
        "start_line": 1,
        "end_line": 3,
    }
    assert result["partial_line"]["line"] == 3
    assert result["partial_line"]["end_char"] > result["partial_line"]["start_char"]
    assert result["line_range"]["status"] == "output_char_truncated"
    assert result["recovery"]["retry_arguments"]["file_path"] == "long.txt"
    assert result["recovery"]["retry_arguments"]["start_line"] == 3
    assert result["recovery"]["retry_arguments"]["end_line"] == 3
    assert result["recovery"]["retry_arguments"]["start_char"] > 0
    assert result["recovery"]["retry_arguments"]["branch"] == "main"


@pytest.mark.asyncio
async def test_read_file_partial_line_continuation_advances_character_offset(
    file_strategy,
):
    """A minified line can be continued without repeatedly returning its prefix."""
    file_strategy.get_context_enhancement_config = lambda: {
        "max_file_lines": 500,
        "max_file_output_chars": 100,
        "default_context_lines": 20,
        "max_context_lines": 200,
    }
    line = "ab" * 500
    repo = _FakeRepo(
        branches={"main": {"minified.js": _FakeContent("minified.js", line)}},
        default_branch="main",
    )
    handler = FileToolHandler()

    first = await handler.read_file(
        "minified.js", repo, pr=None, start_line=1, end_line=1
    )
    second = await handler.read_file(
        "minified.js",
        repo,
        pr=None,
        start_line=1,
        end_line=1,
        start_char=first["partial_line"]["end_char"],
    )

    assert first["returned_lines"] == 1
    assert first["partial_line"]["line"] == 1
    assert first["partial_line"]["start_char"] == 0
    assert first["recovery"]["retry_arguments"]["start_line"] == 1
    assert first["recovery"]["retry_arguments"]["start_char"] > 0
    assert len(second["content"]) == 100
    assert second["content"] != first["content"]
    assert "b" in second["content"]
    assert second["partial_line"]["start_char"] == first["partial_line"]["end_char"]


@pytest.mark.asyncio
async def test_read_file_full_mode_char_budget_reports_actual_lines(file_strategy):
    """Full-read metadata follows the rendered output, not the nominal line cap."""
    file_strategy.get_context_enhancement_config = lambda: {
        "max_file_lines": 500,
        "max_file_output_chars": 100,
        "default_context_lines": 20,
        "max_context_lines": 200,
    }
    content = "\n".join(f"{i}: {'x' * 30}" for i in range(1, 501))
    repo = _FakeRepo(
        branches={"main": {"full.txt": _FakeContent("full.txt", content)}},
        default_branch="main",
    )
    handler = FileToolHandler()

    result = await handler.read_file("full.txt", repo, pr=None)

    assert result["mode"] == "full"
    assert result["total_lines"] == 500
    assert result["returned_lines"] == 3
    assert result["end_line"] == 3
    assert result["recovery"]["retry_arguments"]["start_line"] == 3
    assert result["recovery"]["retry_arguments"]["start_char"] > 0


@pytest.mark.asyncio
async def test_read_file_full_output_is_capped_by_characters(file_strategy):
    """A single very long line cannot bypass the response-size limit."""
    file_strategy.get_context_enhancement_config = lambda: {
        "max_file_lines": 500,
        "max_file_output_chars": 100,
        "default_context_lines": 20,
        "max_context_lines": 200,
    }
    fake_content = _FakeContent("minified.js", "x" * 1_000)
    repo = _FakeRepo(branches={"main": {"minified.js": fake_content}})
    handler = FileToolHandler()

    result = await handler.read_file("minified.js", repo, pr=None)

    assert "error" not in result
    assert result["mode"] == "full"
    assert len(result["content"]) == 100
    assert result["output_truncated"] is True
    assert result["output_char_limit"] == 100


@pytest.mark.asyncio
async def test_read_file_range_output_is_capped_by_characters(file_strategy):
    """One-line ranges are also bounded by response characters."""
    file_strategy.get_context_enhancement_config = lambda: {
        "max_file_lines": 500,
        "max_file_output_chars": 100,
        "default_context_lines": 20,
        "max_context_lines": 200,
    }
    fake_content = _FakeContent("minified.js", "x" * 1_000)
    repo = _FakeRepo(branches={"main": {"minified.js": fake_content}})
    handler = FileToolHandler()

    result = await handler.read_file(
        "minified.js", repo, pr=None, start_line=1, end_line=1
    )

    assert "error" not in result
    assert len(result["content"]) == 100
    assert result["output_truncated"] is True
    assert result["output_char_limit"] == 100
    assert result["line_range"]["output_char_truncated"] is True
    assert result["line_range"]["status"] == "output_char_truncated"


@pytest.mark.asyncio
async def test_read_file_search_output_is_capped_by_characters(file_strategy):
    """Search results cannot return an unbounded minified line."""
    file_strategy.get_context_enhancement_config = lambda: {
        "max_file_lines": 500,
        "max_file_output_chars": 100,
        "default_context_lines": 0,
        "max_context_lines": 200,
    }
    fake_content = _FakeContent("minified.js", "needle" + "x" * 1_000)
    repo = _FakeRepo(branches={"main": {"minified.js": fake_content}})
    handler = FileToolHandler()

    result = await handler.read_file(
        "minified.js", repo, pr=None, search_pattern="needle", context_lines=0
    )

    assert "error" not in result
    assert result["mode"] == "search"
    assert len(result["content"]) == 100
    assert result["output_truncated"] is True
    assert result["output_char_limit"] == 100


@pytest.mark.asyncio
async def test_read_file_unexpected_error_hint_is_task_agnostic(
    file_strategy, monkeypatch
):
    """异常兜底提示不得默认当前任务是 PR 审查。"""
    handler = FileToolHandler()
    monkeypatch.setattr(
        handler,
        "_fetch_contents",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = await handler.read_file("a.py", object(), pr=None)

    assert "读取文件时发生错误" in result["error"]
    assert "PR" not in result["hint"]
    assert "start_line/end_line" in result["hint"]
    assert "search_pattern" in result["hint"]


@pytest.mark.asyncio
async def test_read_file_non_pr_all_branches_fail_returns_error(file_strategy):
    """非 PR 场景指定分支和默认分支都失败时返回结构化错误并保留尝试记录。"""
    repo = _FakeRepo(branches={}, default_branch="main")
    handler = FileToolHandler()
    result = await handler.read_file("a.py", repo, pr=None, branch="feature/missing")

    assert "error" in result
    assert result["branch_requested"] == "feature/missing"
    assert result["branch_used"] is None
    assert result["tried_branches"] == ["feature/missing", "main"]


@pytest.mark.asyncio
async def test_read_file_pr_ignores_branch_uses_head(file_strategy):
    """PR 场景传入 branch 时，仍优先使用 pr.head.sha，忽略 branch。"""
    pr = SimpleNamespace(
        head=SimpleNamespace(sha="headsha"),
        base=SimpleNamespace(sha="basesha"),
    )
    repo = _FakeRepo(
        branches={
            "headsha": {"a.py": _FakeContent("a.py", "head-content\n")},
            "basesha": {"a.py": _FakeContent("a.py", "base-content\n")},
        },
        default_branch="main",
    )
    handler = FileToolHandler()
    result = await handler.read_file("a.py", repo, pr=pr, branch="feature/x")

    assert result["branch_used"] == "HEAD"
    assert result["branch_requested"] is None
    assert result["ref_used"] == "headsha"
    assert result["tried_refs"] == ["headsha"]
    assert "head-content" in result["content"]


@pytest.mark.asyncio
async def test_read_file_start_line_out_of_range_returns_recovery_metadata(
    file_strategy,
):
    """行号来自旧上下文时，返回可执行的恢复参数且不伪造自动重试。"""
    repo = _FakeRepo(
        branches={
            "main": {
                "a.py": _FakeContent(
                    "a.py", "line 1\nline 2\nline 3\nline 4\nline 5"
                ),
            }
        },
        default_branch="main",
    )
    handler = FileToolHandler()

    result = await handler.read_file(
        "a.py",
        repo,
        pr=None,
        start_line=10,
        end_line=12,
        branch="feature/missing",
    )

    assert result["error"] == "start_line 10 超出文件范围"
    assert result["total_lines"] == 5
    assert result["branch_requested"] == "feature/missing"
    assert result["branch_used"] == "main"
    assert result["tried_branches"] == ["feature/missing", "main"]
    assert result["ref_used"] == "main"
    assert result["tried_refs"] == ["feature/missing", "main"]
    assert result["line_range"] == {
        "requested": {"start_line": 10, "end_line": 12},
        "returned": None,
        "total_lines": 5,
        "status": "start_line_out_of_range",
        "start_line_valid": False,
        "end_line_valid": False,
        "truncated": False,
        "stale_context_suspected": True,
    }
    assert result["recovery"] == {
        "action": "retry_read_file",
        "automatic_retry": False,
        "reason": "start_line_out_of_range",
        "retry_arguments": {
            "file_path": "a.py",
            "start_line": 3,
            "end_line": 5,
            "branch": "main",
        },
    }
    assert "工具未自动重试" in result["hint"]
    assert "start_line=3" in result["hint"]
    assert "end_line=5" in result["hint"]


@pytest.mark.asyncio
async def test_read_file_end_line_truncation_is_visible_without_error(file_strategy):
    """结束行越界继续返回内容，但明确暴露请求范围与实际范围。"""
    repo = _FakeRepo(
        branches={
            "main": {
                "a.py": _FakeContent("a.py", "line 1\nline 2\nline 3"),
            }
        },
        default_branch="main",
    )
    handler = FileToolHandler()

    result = await handler.read_file(
        "a.py", repo, pr=None, start_line=2, end_line=10
    )

    assert "error" not in result
    assert result["start_line"] == 2
    assert result["end_line"] == 3
    assert result["content"].endswith("line 3")
    assert result["line_range"] == {
        "requested": {"start_line": 2, "end_line": 10},
        "returned": {"start_line": 2, "end_line": 3},
        "total_lines": 3,
        "status": "end_line_truncated",
        "start_line_valid": True,
        "end_line_valid": False,
        "truncated": True,
        "stale_context_suspected": False,
    }
    assert "truncation" not in result
    assert "end_line=10" in result["hint"]
    assert "end_line=3" in result["hint"]


# ── list_directory 非 PR 场景 ────────────────────────────


@pytest.mark.asyncio
async def test_list_directory_non_pr_with_branch(file_strategy):
    """非 PR 场景传入有效 branch 时，列出该分支目录内容。"""
    repo = _FakeRepo(
        branches={
            "feature/x": {
                "src": [
                    _FakeContent("src/a.py", "", type_="file"),
                    _FakeContent("src/sub", "", type_="dir"),
                ]
            },
            "main": {"src": [_FakeContent("src/old.py", "", type_="file")]},
        },
        default_branch="main",
    )
    handler = FileToolHandler()
    result = await handler.list_directory("src", repo, pr=None, branch="feature/x")

    assert result["branch_used"] == "feature/x"
    names = {item["name"] for item in result["items"]}
    assert "a.py" in names
    assert "old.py" not in names


@pytest.mark.asyncio
async def test_list_directory_non_pr_invalid_branch_falls_back(file_strategy):
    """非 PR 场景 branch 不可访问时，回退默认分支。"""
    repo = _FakeRepo(
        branches={"main": {"src": [_FakeContent("src/old.py", "", type_="file")]}},
        default_branch="main",
    )
    handler = FileToolHandler()
    result = await handler.list_directory(
        "src", repo, pr=None, branch="feature/missing"
    )

    assert result["branch_used"] == "main"
    assert result["tried_branches"] == ["feature/missing", "main"]
    names = {item["name"] for item in result["items"]}
    assert "old.py" in names


@pytest.mark.asyncio
async def test_list_directory_non_pr_no_branch_uses_default(file_strategy):
    """非 PR 场景不传 branch 时，列出默认分支目录。"""
    repo = _FakeRepo(
        branches={"main": {"src": [_FakeContent("src/old.py", "", type_="file")]}},
        default_branch="main",
    )
    handler = FileToolHandler()
    result = await handler.list_directory("src", repo, pr=None)

    assert result["branch_used"] == "main"
    assert result["branch_requested"] is None


# ── search_in_files 非 PR 场景 ───────────────────────────


@pytest.mark.asyncio
async def test_search_in_files_non_pr_with_branch(search_strategy):
    """非 PR 场景传入 branch 后，搜索 ref 使用该分支。"""
    repo = _FakeRepo(
        branches={
            "feature/x": {
                "a.py": _FakeContent("a.py", "keyword here\n"),
                "b.py": _FakeContent("b.py", "no match\n"),
            },
            "main": {"a.py": _FakeContent("a.py", "different text\n")},
        },
        trees={
            "feature/x": _FakeTree(["a.py", "b.py"]),
            "main": _FakeTree(["a.py"]),
        },
        default_branch="main",
    )
    handler = SearchFilesToolHandler()
    result = await handler.search_in_files("keyword", repo, pr=None, branch="feature/x")

    assert result["branch_used"] == "feature/x"
    assert result["branch_requested"] == "feature/x"
    assert any(r["file_path"] == "a.py" for r in result["results"])


@pytest.mark.asyncio
async def test_search_in_files_invalid_branch_falls_back(search_strategy):
    """指定分支搜索异常时（ref 不存在），回退默认分支。"""
    repo = _FakeRepo(
        branches={
            "main": {"a.py": _FakeContent("a.py", "keyword here\n")},
        },
        trees={"main": _FakeTree(["a.py"])},
        default_branch="main",
    )
    handler = SearchFilesToolHandler()
    result = await handler.search_in_files(
        "keyword", repo, pr=None, branch="feature/missing"
    )

    assert result["branch_used"] == "main"
    assert result["branch_requested"] == "feature/missing"
    assert result["tried_branches"] == ["feature/missing", "main"]
    assert any(r["file_path"] == "a.py" for r in result["results"])


@pytest.mark.asyncio
async def test_search_in_files_zero_matches_does_not_fall_back(search_strategy):
    """有效分支搜索成功但零匹配时，不回退默认分支（零匹配是有效结果）。"""
    repo = _FakeRepo(
        branches={
            "feature/x": {"a.py": _FakeContent("a.py", "nothing relevant\n")},
            "main": {"a.py": _FakeContent("a.py", "keyword match\n")},
        },
        trees={
            "feature/x": _FakeTree(["a.py"]),
            "main": _FakeTree(["a.py"]),
        },
        default_branch="main",
    )
    handler = SearchFilesToolHandler()
    result = await handler.search_in_files("keyword", repo, pr=None, branch="feature/x")

    assert result["branch_used"] == "feature/x"
    assert result["total_matches"] == 0
    assert result["tried_branches"] == ["feature/x"]


@pytest.mark.asyncio
async def test_search_in_files_scans_large_files(search_strategy):
    """跨文件搜索不因文件字节数跳过，只返回匹配内容与上下文。"""
    fake_content = _FakeContent("large.sh", "before\nkeyword here\nafter\n")
    fake_content.size = 262335
    repo = _FakeRepo(
        branches={"main": {"large.sh": fake_content}},
        trees={"main": _FakeTree(["large.sh"])},
        default_branch="main",
    )
    handler = SearchFilesToolHandler()

    result = await handler.search_in_files("keyword", repo, pr=None)

    assert "error" not in result
    assert result["total_matches"] == 1
    assert result["results"][0]["file_path"] == "large.sh"
    assert "keyword here" in result["results"][0]["content"]


@pytest.mark.asyncio
async def test_search_in_files_reports_file_size_budget_skips(search_strategy):
    """Files beyond the search-specific budget are visible, not silently lost."""
    search_strategy.get_context_enhancement_config = lambda: {
        "search_in_files": {
            "default_context_lines": 1,
            "default_max_results": 20,
            "skip_binary": True,
            "use_search_api": False,
            "max_files_to_search": 100,
            "concurrency": 2,
            "max_file_bytes": 100,
            "max_total_scan_bytes": 1_000,
            "max_matches_per_file": 20,
            "max_output_chars": 10_000,
        }
    }
    large = _FakeContent("generated.txt", "keyword\n")
    large.size = 500
    repo = _FakeRepo(
        branches={"main": {"generated.txt": large}},
        trees={"main": _FakeTree(["generated.txt"])},
        default_branch="main",
    )
    handler = SearchFilesToolHandler()

    result = await handler.search_in_files("keyword", repo, pr=None)

    assert "error" not in result
    assert result["results"] == []
    assert result["skipped_files"] == [
        {
            "file_path": "generated.txt",
            "status": "skipped",
            "reason": "file_size_limit",
            "size": 500,
            "limit": 100,
        }
    ]


@pytest.mark.asyncio
async def test_search_in_files_preflight_tree_size_avoids_blob_fetch(
    search_strategy,
):
    """Known-large traversal blobs are skipped before get_contents downloads them."""
    search_strategy.get_context_enhancement_config = lambda: {
        "search_in_files": {
            "default_context_lines": 1,
            "default_max_results": 20,
            "skip_binary": True,
            "use_search_api": False,
            "max_files_to_search": 100,
            "concurrency": 2,
            "max_file_bytes": 100,
            "max_total_scan_bytes": 1_000,
            "max_matches_per_file": 20,
            "max_output_chars": 10_000,
        }
    }

    def _unexpected_get_contents(path, ref=None):
        raise AssertionError(f"large blob must not be fetched: {path}")

    repo = _FakeRepo(
        branches={"main": {}},
        trees={"main": _FakeTree(["generated.txt"], {"generated.txt": 100_000})},
        default_branch="main",
    )
    repo.get_contents = _unexpected_get_contents
    handler = SearchFilesToolHandler()

    result = await handler.search_in_files("keyword", repo, pr=None)

    assert "error" not in result
    assert result["skipped_files"][0]["reason"] == "file_size_limit"


@pytest.mark.asyncio
async def test_search_in_files_total_scan_budget_bounds_work(search_strategy):
    """The aggregate scan budget is enforced and reported as inexact completion."""
    search_strategy.get_context_enhancement_config = lambda: {
        "search_in_files": {
            "default_context_lines": 0,
            "default_max_results": 20,
            "skip_binary": True,
            "use_search_api": False,
            "max_files_to_search": 100,
            "concurrency": 4,
            "max_file_bytes": 100,
            "max_total_scan_bytes": 100,
            "max_matches_per_file": 20,
            "max_output_chars": 10_000,
        }
    }
    repo = _FakeRepo(
        branches={
            "main": {
                "a.txt": _FakeContent("a.txt", "x" * 72 + "\nkeyword\n"),
                "b.txt": _FakeContent("b.txt", "x" * 72 + "\nkeyword\n"),
            }
        },
        trees={"main": _FakeTree(["a.txt", "b.txt"])},
        default_branch="main",
    )
    handler = SearchFilesToolHandler()

    result = await handler.search_in_files("keyword", repo, pr=None)

    assert "error" not in result
    assert result["bytes_scanned"] <= 100
    assert result["scan_complete"] is False
    assert result["total_matches_exact"] is False
    assert len(result["results"]) == 1
    assert len(result["skipped_files"]) == 1
    assert result["skipped_files"][0]["reason"] == "total_scan_budget"


@pytest.mark.asyncio
async def test_search_in_files_match_budget_stops_and_marks_inexact(search_strategy):
    """Per-file match capping avoids materializing every generated match."""
    search_strategy.get_context_enhancement_config = lambda: {
        "search_in_files": {
            "default_context_lines": 0,
            "default_max_results": 20,
            "skip_binary": True,
            "use_search_api": False,
            "max_files_to_search": 100,
            "concurrency": 2,
            "max_file_bytes": 10_000,
            "max_total_scan_bytes": 1_000_000,
            "max_matches_per_file": 2,
            "max_output_chars": 10_000,
        }
    }
    content = "\n".join(f"keyword {i}" for i in range(20))
    repo = _FakeRepo(
        branches={"main": {"generated.txt": _FakeContent("generated.txt", content)}},
        trees={"main": _FakeTree(["generated.txt"])},
        default_branch="main",
    )
    handler = SearchFilesToolHandler()

    result = await handler.search_in_files("keyword", repo, pr=None)

    assert result["total_matches"] == 2
    assert result["total_matches_exact"] is False
    assert result["results"][0]["match_count"] == 2
    assert result["results"][0]["matches_truncated"] is True
# ── Search API 路径（ref-inaccessible 检测 + 降级）──────────


@pytest.mark.asyncio
async def test_search_via_api_ref_inaccessible_when_all_reads_fail():
    """Search API 返回匹配文件但全部 get_contents 失败时，返回 error 标记 ref 不可访问。"""
    requester = _FakeRequester({"items": [{"path": "a.py"}, {"path": "b.py"}]})
    repo = _FakeRepo(branches={}, default_branch="main", requester=requester)
    handler = SearchFilesToolHandler()

    result = await handler._search_via_api(
        "keyword", repo, "feature/missing", [], True, None, None, 3, 20
    )

    assert "error" in result
    assert "feature/missing" in result["error"]
    assert result["files_searched"] == 2
    assert result["search_method"] == "github_search_api"


@pytest.mark.asyncio
async def test_search_via_api_normal_match_does_not_flag_inaccessible():
    """ref 有效且能读取到匹配文件时正常返回，不误判为 ref 不可访问。"""
    requester = _FakeRequester({"items": [{"path": "a.py"}]})
    repo = _FakeRepo(
        branches={"feature/x": {"a.py": _FakeContent("a.py", "keyword here\n")}},
        default_branch="main",
        requester=requester,
    )
    handler = SearchFilesToolHandler()

    result = await handler._search_via_api(
        "keyword", repo, "feature/x", [], True, None, None, 3, 20
    )

    assert "error" not in result
    assert any(r["file_path"] == "a.py" for r in result["results"])


@pytest.mark.asyncio
async def test_search_via_api_uses_returned_size_before_blob_fetch():
    """If Search API supplies size, over-limit blobs are not downloaded."""
    requester = _FakeRequester(
        {"items": [{"path": "generated.txt", "size": 3_000_000}]}
    )
    repo = _FakeRepo(
        branches={"main": {}},
        default_branch="main",
        requester=requester,
    )

    def _unexpected_get_contents(path, ref=None):
        raise AssertionError(f"large blob must not be fetched: {path}")

    repo.get_contents = _unexpected_get_contents
    handler = SearchFilesToolHandler()

    result = await handler._search_via_api(
        "keyword", repo, "main", [], True, None, None, 3, 20
    )

    assert "error" not in result
    assert result["skipped_files"][0]["reason"] == "file_size_limit"


@pytest.mark.asyncio
async def test_dispatch_search_round_falls_back_to_per_file_when_api_unavailable(
    search_strategy,
):
    """repo 不支持 Search API（非 Repository）时，_dispatch_search_round 降级到 per_file。"""
    repo = _FakeRepo(
        branches={"main": {"a.py": _FakeContent("a.py", "keyword\n")}},
        trees={"main": _FakeTree(["a.py"])},
        default_branch="main",
    )
    handler = SearchFilesToolHandler()
    config = handler._get_config()
    config["use_search_api"] = True  # 触发 API 尝试 → isinstance 失败 → 降级 per_file

    result = await handler._dispatch_search_round(
        "keyword", repo, "main", [], True, None, None, 3, 20, config
    )

    assert "error" not in result
    assert result["search_method"] == "per_file_traversal"
    assert any(r["file_path"] == "a.py" for r in result["results"])


@pytest.mark.asyncio
async def test_search_in_files_api_ref_inaccessible_triggers_external_fallback(
    search_strategy_api, monkeypatch
):
    """Search API 路径下 ref 不可访问时，error 触发 search_in_files 外部回退到默认分支。"""
    import github.Repository

    # 让 isinstance(repo, Repository) 通过，使 _dispatch_search_round 走 API 路径
    monkeypatch.setattr(github.Repository, "Repository", _FakeRepo)

    requester = _FakeRequester({"items": [{"path": "a.py"}]})
    repo = _FakeRepo(
        branches={"main": {"a.py": _FakeContent("a.py", "keyword here\n")}},
        trees={"main": _FakeTree(["a.py"])},
        default_branch="main",
        requester=requester,
    )
    handler = SearchFilesToolHandler()

    result = await handler.search_in_files(
        "keyword", repo, pr=None, branch="feature/missing"
    )

    # feature/missing 经 API → ref-inaccessible error → 外部回退 main → API 成功
    assert result["branch_used"] == "main"
    assert result["branch_requested"] == "feature/missing"
    assert result["tried_branches"] == ["feature/missing", "main"]
    assert any(r["file_path"] == "a.py" for r in result["results"])
    assert requester.call_count == 2  # 两个候选 ref 各调用一次 Search API


# ── ToolHandler 透传 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_tool_call_passes_branch_to_read_file():
    file_tool = SimpleNamespace(
        read_file=AsyncMock(return_value={"file_path": "a.py", "content": "x"}),
    )
    handler = ToolHandler(file_tool=file_tool, search_tool=SimpleNamespace())

    tc = _FakeToolCall("read_file", {"file_path": "a.py", "branch": "feature/x"})
    await handler.handle_tool_call(tc, repo=object(), pr=None)

    _, kwargs = file_tool.read_file.call_args
    assert kwargs.get("branch") == "feature/x"


@pytest.mark.asyncio
async def test_handle_tool_call_passes_branch_to_list_directory():
    file_tool = SimpleNamespace(
        list_directory=AsyncMock(
            return_value={"directory": "src", "items": [], "count": 0}
        ),
    )
    handler = ToolHandler(file_tool=file_tool, search_tool=SimpleNamespace())

    tc = _FakeToolCall("list_directory", {"directory": "src", "branch": "feature/x"})
    await handler.handle_tool_call(tc, repo=object(), pr=None)

    _, kwargs = file_tool.list_directory.call_args
    assert kwargs.get("branch") == "feature/x"


@pytest.mark.asyncio
async def test_handle_tool_call_passes_branch_to_search_in_files():
    search_files_tool = SimpleNamespace(
        search_in_files=AsyncMock(
            return_value={"keyword": "k", "results": [], "total_matches": 0}
        ),
    )
    handler = ToolHandler(
        file_tool=SimpleNamespace(),
        search_tool=SimpleNamespace(),
        search_files_tool=search_files_tool,
    )

    tc = _FakeToolCall("search_in_files", {"keyword": "k", "branch": "feature/x"})
    await handler.handle_tool_call(tc, repo=object(), pr=None)

    _, kwargs = search_files_tool.search_in_files.call_args
    assert kwargs.get("branch") == "feature/x"


@pytest.mark.asyncio
async def test_handle_tool_call_without_branch_keeps_old_behavior():
    """不传 branch 时透传 None，行为与旧版本一致。"""
    file_tool = SimpleNamespace(
        read_file=AsyncMock(return_value={"file_path": "a.py", "content": "x"}),
    )
    handler = ToolHandler(file_tool=file_tool, search_tool=SimpleNamespace())

    tc = _FakeToolCall("read_file", {"file_path": "a.py"})
    await handler.handle_tool_call(tc, repo=object(), pr=None)

    _, kwargs = file_tool.read_file.call_args
    assert kwargs.get("branch") is None
