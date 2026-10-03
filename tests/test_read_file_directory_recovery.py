"""read_file 目录路径误用的结构化恢复测试（Issue #645）

PR 审查中模型曾把「在目录中搜索关键词」误映射为
``read_file(file_path="tests", search_pattern=...)``。本文件锁定：

- 带 search_pattern 的目录误用 → 返回指向 search_in_files 的
  结构化 recovery（含等价 retry_arguments）；
- 纯目录读取 → 保持 list_directory 提示，不产生 search recovery；
- 工具 schema 与审查 prompt 中的工具选择规则文案防回归。
"""

from types import SimpleNamespace

import pytest

from backend.services.ai_reviewer.constants import (
    READ_FILE_TOOL,
    SEARCH_IN_FILES_TOOL,
)
from backend.services.ai_reviewer.prompt_builder import PromptBuilder
from backend.services.ai_reviewer.tools.file_tool import FileToolHandler


class _FakeContent:
    """模拟 PyGithub ContentFile，仅覆盖目录列表场景需要的字段。"""

    def __init__(self, path, type_="file"):
        self.path = path
        self.name = path.rsplit("/", 1)[-1]
        self.type = type_
        self.size = 0
        self.decoded_content = b""


class _FakeRepo:
    """get_contents 对目录路径返回列表，模拟 GitHub API 的目录响应。"""

    def __init__(self, directory_items, default_branch="main"):
        self._directory_items = directory_items
        self.default_branch = default_branch
        self.full_name = "owner/repo"

    def get_contents(self, path, ref=None):
        if path == "tests":
            return self._directory_items
        raise Exception(f"Not found: {path} @ {ref}")


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


@pytest.fixture
def file_strategy(monkeypatch):
    cfg = _FileStrategyConfig()
    monkeypatch.setattr(
        "backend.services.ai_reviewer.tools.file_tool.get_strategy_config",
        lambda: cfg,
    )
    return cfg


def _directory_repo():
    return _FakeRepo(
        directory_items=[
            _FakeContent("tests/test_a.py"),
            _FakeContent("tests/test_b.py"),
        ]
    )


# ── 目录 + search_pattern：结构化 recovery 指向 search_in_files ──


@pytest.mark.asyncio
async def test_read_file_directory_with_search_pattern_returns_search_recovery(
    file_strategy,
):
    """目录误用带搜索意图时，返回等价的 search_in_files retry 参数。"""
    repo = _directory_repo()
    handler = FileToolHandler()
    result = await handler.read_file(
        "tests",
        repo,
        pr=None,
        search_pattern="Findings Summary",
        context_lines=2,
        branch="main",
    )

    assert "目录" in result["error"]
    assert "read_file" in result["error"]
    recovery = result["recovery"]
    assert recovery["action"] == "retry_search_in_files"
    assert recovery["automatic_retry"] is False
    assert recovery["reason"] == "path_is_directory"
    assert recovery["retry_arguments"] == {
        "keyword": "Findings Summary",
        "directory": "tests",
        "context_lines": 2,
        "branch": "main",
    }
    # 指引文案同时给出 search_in_files 与 list_directory 的分工
    assert "search_in_files" in result["hint"]
    assert "list_directory" in result["hint"]


@pytest.mark.asyncio
async def test_read_file_directory_search_recovery_omits_unset_arguments(
    file_strategy,
):
    """未显式指定 context_lines 时不透传；PR 场景不透传 branch。"""
    repo = _directory_repo()
    pr = SimpleNamespace(head=SimpleNamespace(sha="abc123"))
    handler = FileToolHandler()
    result = await handler.read_file(
        "tests", repo, pr=pr, search_pattern="Findings Summary"
    )

    retry_arguments = result["recovery"]["retry_arguments"]
    assert retry_arguments == {"keyword": "Findings Summary", "directory": "tests"}


@pytest.mark.asyncio
async def test_read_file_directory_without_search_pattern_keeps_list_directory_hint(
    file_strategy,
):
    """纯目录读取（无搜索意图）仍提示 list_directory，不返回 search recovery。"""
    repo = _directory_repo()
    handler = FileToolHandler()
    result = await handler.read_file("tests", repo, pr=None)

    assert "list_directory" in result["error"]
    assert "目录" in result["error"]
    assert "2 个项目" in result["hint"]
    assert "recovery" not in result


# ── 工具 schema 与审查 prompt 的工具选择规则 ──


def test_read_file_tool_schema_documents_directory_restriction():
    """read_file schema 明确 file_path 必须是单文件并给出替代工具指引。"""
    description = READ_FILE_TOOL["function"]["description"]
    assert "目录" in description
    assert "search_in_files" in description
    assert "list_directory" in description

    file_path_desc = READ_FILE_TOOL["function"]["parameters"]["properties"][
        "file_path"
    ]["description"]
    assert "search_in_files" in file_path_desc


def test_search_in_files_tool_schema_covers_directory_scope():
    """search_in_files schema 自述覆盖目录/仓库级搜索场景。"""
    description = SEARCH_IN_FILES_TOOL["function"]["description"]
    assert "目录" in description
    assert "read_file" in description


def test_system_prompt_tool_use_section_defines_selection_rules():
    """system prompt 的 Tool use 部分包含按范围选择工具的规则。"""
    prompt = PromptBuilder().build_system_prompt(
        "Focus on correctness.",
        {"files": []},
        include_tools=True,
        output_language="zh-CN",
    )

    tool_use = prompt.split("## Tool use", 1)[1].split("##", 1)[0]
    assert "read_file" in tool_use
    assert "search_in_files" in tool_use
    assert "list_directory" in tool_use
    assert "directory path" in tool_use


def test_user_message_tool_section_documents_selection_rules():
    """用户消息的可用工具部分列出工具选择规则并标明 read_file 不接受目录。"""
    message = PromptBuilder().build_user_message(
        {"title": "t", "files": []}, "balanced", include_tools=True, compact=False
    )

    assert "工具选择规则" in message
    read_file_line = message.split("`read_file`", 1)[1].split("\n", 1)[0]
    assert "目录" in read_file_line
    assert "search_in_files" in message
