"""Agent 工具框架测试 - 验证新的 Claude Code 风格工具系统

测试覆盖：
- base.py: BaseTool, ToolExecutor, ToolResult
- file_state.py: ReadFileState stale 检查
- file_utils.py: find_actual_string 容错匹配
- registry.py: 工具注册和 schema 导出
- 各个具体工具: ReadTool, EditTool, WriteTool, ReplaceLinesTool, InsertLinesTool
"""

import json

import pytest

from backend.services.agent_team.tool_executor import AgentToolExecutor
from backend.services.agent_team.tools.base import ToolContext, ToolExecutor, ToolResult
from backend.services.agent_team.tools.file_state import ReadFileState
from backend.services.agent_team.tools.file_utils import (
    find_actual_string,
    make_unified_diff,
)
from backend.services.agent_team.tools.grep_tool import MAX_GREP_KEYWORD_LENGTH
from backend.services.agent_team.tools.registry import (
    create_executor,
    get_fullstack_tools,
    get_reviewer_tools,
    get_tool_definitions,
    tool_registry,
)
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


# ── 辅助 ──────────────────────────────────────────────


def _make_ctx(workspace: str, file_state: ReadFileState | None = None) -> ToolContext:
    service = AgentTeamWorkspaceService()
    return ToolContext(
        workspace=workspace,
        workspace_service=service,
        extra={"file_state": file_state or ReadFileState()},
    )


class FakeToolCall:
    def __init__(self, name: str, arguments: dict):
        self.function = type(
            "F", (), {"name": name, "arguments": json.dumps(arguments)}
        )()
        self.id = f"call_{name}"


def _setup_workspace(tmp_path):
    """创建 workspace 并返回 (workspace_path_str, ToolContext)。"""
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = str(service.ensure_workspace("owner", "repo"))
    ctx = ToolContext(
        workspace=workspace,
        workspace_service=service,
        extra={"file_state": ReadFileState()},
    )
    return workspace, ctx


# ── ToolResult ────────────────────────────────────────


def test_tool_result_success():
    r = ToolResult(success=True, output={"path": "a.py", "size": 10})
    assert r.success
    assert not r.is_terminal


def test_tool_result_terminal():
    r = ToolResult(success=True, output={"_terminal": True, "summary": "done"})
    assert r.is_terminal


def test_tool_result_error():
    r = ToolResult(success=False, error="文件不存在")
    assert not r.success
    assert "文件不存在" in r.error


# ── ToolExecutor ──────────────────────────────────────


@pytest.mark.asyncio
async def test_executor_unknown_tool(tmp_path):
    _, ctx = _setup_workspace(tmp_path)
    executor = ToolExecutor()

    tc = FakeToolCall("nonexistent_tool", {})
    result = await executor.execute_tool_call(tc, ctx)
    assert not result.success
    assert "未知工具" in result.error


@pytest.mark.asyncio
async def test_executor_raw_call(tmp_path):
    """通过 execute_raw 直接调用工具（不走 JSON 解析）。"""
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("fullstack")

    result = await executor.execute_raw(
        "write_file", {"file_path": "test.py", "content": "x = 1\n"}, ctx
    )
    assert result.success
    assert result.output["created"]


# ── ReadFileState ─────────────────────────────────────


def test_file_state_stale_detection(tmp_path):
    """mtime 变化时应检测到 stale。"""
    f = tmp_path / "test.py"
    f.write_text("hello\n", encoding="utf-8")

    state = ReadFileState()
    state.set(f, content="hello\n", mtime=f.stat().st_mtime)

    # 未修改 → 安全
    assert state.check_not_stale(f) is None

    # 修改文件
    import time

    time.sleep(0.05)
    f.write_text("world\n", encoding="utf-8")

    error = state.check_not_stale(f)
    assert error is not None
    assert "外部修改" in error


def test_file_state_content_unchanged(tmp_path):
    """mtime 变了但内容没变（Windows 场景），应该安全。"""
    f = tmp_path / "test.py"
    f.write_text("hello\n", encoding="utf-8")

    state = ReadFileState()
    state.set(f, content="hello\n", mtime=0.0)  # 旧 mtime

    # 内容没变 → 安全（因为 is_full_read=True）
    assert state.check_not_stale(f) is None


def test_file_state_invalidate(tmp_path):
    f = tmp_path / "test.py"
    f.write_text("hello\n", encoding="utf-8")

    state = ReadFileState()
    state.set(f, content="hello\n", mtime=f.stat().st_mtime)
    assert state.get(f) is not None

    state.invalidate(f)
    assert state.get(f) is None


# ── find_actual_string 容错匹配 ──────────────────────


def test_find_exact():
    assert find_actual_string("hello world", "hello") == "hello"


def test_find_with_smart_quotes():
    content = "msg = \u2018hello\u2019"
    result = find_actual_string(content, "'hello'")
    assert result is not None


def test_find_with_tab_normalization():
    content = "def foo():\n\treturn 42"
    result = find_actual_string(content, "def foo():\n    return 42")
    assert result is not None


def test_find_not_found():
    assert find_actual_string("abc", "xyz") is None


# ── make_unified_diff ────────────────────────────────


def test_unified_diff():
    diff = make_unified_diff("test.py", "a\nb\nc\n", "a\nx\nc\n")
    assert "--- a/test.py" in diff
    assert "+++ b/test.py" in diff


# ── Registry ──────────────────────────────────────────


def test_registry_has_all_fullstack_tools():
    tools = get_fullstack_tools()
    names = {t.name for t in tools}
    expected = {
        "read_file",
        "list_directory",
        "glob",
        "search_in_files",
        "write_file",
        "edit_file",
        "replace_lines",
        "insert_lines",
        "run_command",
        "use_skill",
        "finish_task",
        "revert_file",
        "detect_project",
        "check_changes",
    }
    assert expected == names


def test_registry_has_all_reviewer_tools():
    tools = get_reviewer_tools()
    names = {t.name for t in tools}
    assert "use_skill" in names
    assert "submit_review" in names
    assert "write_file" not in names
    assert "edit_file" not in names


def test_get_tool_definitions():
    schemas = get_tool_definitions("fullstack")
    assert len(schemas) == 14
    names = {s["function"]["name"] for s in schemas}
    assert "edit_file" in names
    assert "replace_lines" in names
    assert "use_skill" in names


def test_get_tool_definitions_zai_requires_all_properties():
    schemas = get_tool_definitions("fullstack", provider="zai")

    for schema in schemas:
        params = schema["function"]["parameters"]
        properties = params.get("properties", {})
        assert params.get("additionalProperties") is False
        if properties:
            assert set(params["required"]) == set(properties)


def test_tool_registry_by_name():
    assert "edit_file" in tool_registry
    assert "submit_review" in tool_registry


# ── ReadTool ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_tool_basic(tmp_path):
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("fullstack")

    # 先写入
    await executor.execute_raw(
        "write_file", {"file_path": "demo.py", "content": "a\nb\nc\n"}, ctx
    )

    result = await executor.execute_raw("read_file", {"file_path": "demo.py"}, ctx)
    assert result.success
    assert "a" in result.output["content"]
    # "a\nb\nc\n" split → ["a","b","c",""] = 4 行
    assert result.output["total_lines"] == 4


@pytest.mark.asyncio
async def test_read_tool_with_range(tmp_path):
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("fullstack")

    await executor.execute_raw(
        "write_file", {"file_path": "demo.py", "content": "a\nb\nc\nd\ne\n"}, ctx
    )

    result = await executor.execute_raw(
        "read_file", {"file_path": "demo.py", "start_line": 2, "end_line": 4}, ctx
    )
    assert result.success
    assert result.output["start_line"] == 2
    assert result.output["end_line"] == 4


@pytest.mark.asyncio
async def test_read_tool_missing_file(tmp_path):
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("fullstack")

    result = await executor.execute_raw("read_file", {"file_path": "missing.py"}, ctx)
    assert not result.success
    assert "不存在" in result.error


# ── EditTool ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_tool_basic(tmp_path):
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("fullstack")

    await executor.execute_raw(
        "write_file", {"file_path": "demo.py", "content": "x = 1\ny = 2\n"}, ctx
    )

    result = await executor.execute_raw(
        "edit_file",
        {"file_path": "demo.py", "old_text": "x = 1", "new_text": "x = 42"},
        ctx,
    )
    assert result.success
    assert result.output["replacements"] == 1
    assert "diff" in result.output


@pytest.mark.asyncio
async def test_edit_tool_multiple_matches(tmp_path):
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("fullstack")

    await executor.execute_raw(
        "write_file", {"file_path": "demo.py", "content": "x = 1\nx = 1\n"}, ctx
    )

    result = await executor.execute_raw(
        "edit_file",
        {"file_path": "demo.py", "old_text": "x = 1", "new_text": "x = 2"},
        ctx,
    )
    assert not result.success
    assert "2 处匹配" in result.error


@pytest.mark.asyncio
async def test_edit_tool_missing_text(tmp_path):
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("fullstack")

    await executor.execute_raw(
        "write_file", {"file_path": "demo.py", "content": "hello\n"}, ctx
    )

    result = await executor.execute_raw(
        "edit_file", {"file_path": "demo.py", "old_text": "xyz", "new_text": "abc"}, ctx
    )
    assert not result.success
    assert "未找到" in result.error


# ── WriteTool ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_tool_create(tmp_path):
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("fullstack")

    result = await executor.execute_raw(
        "write_file", {"file_path": "new.py", "content": "print('hello')\n"}, ctx
    )
    assert result.success
    assert result.output["created"]


# ── ReplaceLinesTool ──────────────────────────────────


@pytest.mark.asyncio
async def test_replace_lines(tmp_path):
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("fullstack")

    await executor.execute_raw(
        "write_file", {"file_path": "demo.py", "content": "a\nb\nc\nd\ne\n"}, ctx
    )

    result = await executor.execute_raw(
        "replace_lines",
        {"file_path": "demo.py", "start_line": 2, "end_line": 4, "new_content": "B\nC"},
        ctx,
    )
    assert result.success
    assert result.output["lines_replaced"] == 3
    assert "diff" in result.output


# ── InsertLinesTool ───────────────────────────────────


@pytest.mark.asyncio
async def test_insert_lines(tmp_path):
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("fullstack")

    await executor.execute_raw(
        "write_file", {"file_path": "demo.py", "content": "a\nb\nc\n"}, ctx
    )

    result = await executor.execute_raw(
        "insert_lines",
        {"file_path": "demo.py", "after_line": 1, "content": "inserted"},
        ctx,
    )
    assert result.success
    assert result.output["lines_inserted"] == 1


# ── FinishTaskTool ────────────────────────────────────


@pytest.mark.asyncio
async def test_finish_task(tmp_path):
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("fullstack")

    result = await executor.execute_raw(
        "finish_task", {"summary": "done", "risk_level": "low"}, ctx
    )
    assert result.success
    assert result.is_terminal
    assert result.output["summary"] == "done"


# ── SubmitReviewTool ──────────────────────────────────


@pytest.mark.asyncio
async def test_submit_review(tmp_path):
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("reviewer")

    result = await executor.execute_raw(
        "submit_review", {"verdict": "pass", "score": 8, "summary": "Good"}, ctx
    )
    assert result.success
    assert result.is_terminal
    assert result.output["verdict"] == "pass"


@pytest.mark.asyncio
async def test_search_in_files_rejects_long_keyword(tmp_path):
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("fullstack")

    result = await executor.execute_raw(
        "search_in_files", {"keyword": "x" * (MAX_GREP_KEYWORD_LENGTH + 1)}, ctx
    )

    assert not result.success
    assert str(MAX_GREP_KEYWORD_LENGTH) in result.error


@pytest.mark.asyncio
async def test_search_in_files_invalid_regex_case_insensitive_fallback(tmp_path):
    workspace, ctx = _setup_workspace(tmp_path)
    (ctx.workspace_service.resolve_inside_workspace(workspace) / "demo.py").write_text(
        "FOO[BAR\n",
        encoding="utf-8",
    )
    executor = create_executor("fullstack")

    result = await executor.execute_raw(
        "search_in_files",
        {"keyword": "foo[bar", "case_insensitive": True, "output_mode": "content"},
        ctx,
    )

    assert result.success
    assert any("FOO[BAR" in match for match in result.output["matches"])


@pytest.mark.asyncio
async def test_legacy_search_in_files_rejects_long_keyword(tmp_path):
    workspace, ctx = _setup_workspace(tmp_path)
    executor = AgentToolExecutor(workspace, ctx.workspace_service)

    result = await executor._handle_search_in_files(
        {"keyword": "x" * (MAX_GREP_KEYWORD_LENGTH + 1)}
    )

    assert str(MAX_GREP_KEYWORD_LENGTH) in result["error"]


@pytest.mark.asyncio
async def test_legacy_search_in_files_reuses_grep_tool(tmp_path):
    workspace, ctx = _setup_workspace(tmp_path)
    (ctx.workspace_service.resolve_inside_workspace(workspace) / "demo.py").write_text(
        "needle = 1\n",
        encoding="utf-8",
    )
    executor = AgentToolExecutor(workspace, ctx.workspace_service)

    result = await executor._handle_search_in_files(
        {"keyword": "needle", "file_extension": ".py"}
    )

    assert result["keyword"] == "needle"
    assert result["total"] >= 1
    assert any("demo.py" in match for match in result["matches"])


# ── ToolExecutor 完整流程 ─────────────────────────────


@pytest.mark.asyncio
async def test_full_pipeline_with_fake_tool_call(tmp_path):
    """通过 FakeToolCall 模拟完整的工具调用流程。"""
    _, ctx = _setup_workspace(tmp_path)
    executor = create_executor("fullstack")

    # 写入
    tc_write = FakeToolCall(
        "write_file", {"file_path": "demo.py", "content": "x = 1\ny = 2\n"}
    )
    result = await executor.execute_tool_call(tc_write, ctx)
    assert result.success

    # 读取
    tc_read = FakeToolCall("read_file", {"file_path": "demo.py"})
    result = await executor.execute_tool_call(tc_read, ctx)
    assert result.success
    assert "x = 1" in result.output["content"]

    # 编辑
    tc_edit = FakeToolCall(
        "edit_file",
        {
            "file_path": "demo.py",
            "old_text": "x = 1",
            "new_text": "x = 42",
        },
    )
    result = await executor.execute_tool_call(tc_edit, ctx)
    assert result.success
    assert result.output["replacements"] == 1

    # 完成
    tc_finish = FakeToolCall("finish_task", {"summary": "modified x"})
    result = await executor.execute_tool_call(tc_finish, ctx)
    assert result.is_terminal
