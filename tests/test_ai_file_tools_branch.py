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
    def __init__(self, path, type_="blob"):
        self.path = path
        self.type = type_


class _FakeRequester:
    """模拟 PyGithub requester.requestJsonAndCheck，返回固定 Search API 响应。"""

    def __init__(self, data):
        self._data = data
        self.call_count = 0

    def requestJsonAndCheck(self, method, url):
        self.call_count += 1
        return (None, self._data)


class _FakeTree:
    def __init__(self, paths):
        self.tree = [_FakeTreeEntry(p) for p in paths]


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
    assert "head-content" in result["content"]


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
