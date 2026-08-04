"""Agent 工具调用模式测试 - edit_file、tool_executor、fullstack、reviewer"""

import json

import pytest

from backend.services.agent_team.file_tools import AgentTeamFileTools
from backend.services.agent_team.tool_definitions import (
    AGENT_EDIT_FILE_TOOL,
    FULLSTACK_EXPERT_TOOLS,
    REVIEWER_TOOLS,
)
from backend.services.agent_team.tool_executor import AgentToolExecutor
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService

# ── file_tools.edit_file ──────────────────────────────────


def test_edit_file_replaces_first_occurrence(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file(
        "test.py",
        "def hello():\n    return 'hello'\n\ndef hello():\n    return 'world'\n",
    )

    result = tools.edit_file("test.py", "return 'hello'", "return 'hi'")

    assert result.replacements == 1
    content = tools.read_file("test.py").content
    assert "return 'hi'" in content
    assert "return 'world'" in content


def test_edit_file_raises_on_multiple_matches(tmp_path):
    """多处匹配时必须报错，要求 AI 扩大上下文使匹配唯一。"""
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file("test.py", "x = 1\nx = 1\n")

    with pytest.raises(ValueError, match="2 处匹配"):
        tools.edit_file("test.py", "x = 1", "x = 2")


def test_edit_file_replace_all(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file("test.py", "old_value\nold_value\nold_value\n")

    result = tools.edit_file("test.py", "old_value", "new_value", replace_all=True)

    assert result.replacements == 3
    content = tools.read_file("test.py").content
    assert content == "new_value\nnew_value\nnew_value\n"


def test_edit_file_raises_on_missing_text(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file("test.py", "print('hello')\n")

    with pytest.raises(ValueError, match="未找到"):
        tools.edit_file("test.py", "nonexistent text", "anything")


def test_edit_file_raises_on_missing_file(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    with pytest.raises(FileNotFoundError):
        tools.edit_file("missing.py", "old", "new")


def test_edit_file_preserves_indentation(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    original = "class Foo:\n    def bar(self):\n        pass\n"
    tools.write_file("test.py", original)

    tools.edit_file("test.py", "        pass", "        return 42")

    content = tools.read_file("test.py").content
    assert "return 42" in content
    assert "pass" not in content


# ── tool_definitions 结构 ─────────────────────────────────


def test_fullstack_tools_include_edit_file():
    names = [t["function"]["name"] for t in FULLSTACK_EXPERT_TOOLS]
    assert "edit_file" in names
    assert "read_file" in names
    assert "write_file" in names
    assert "finish_task" in names


def test_reviewer_tools_do_not_include_write():
    names = [t["function"]["name"] for t in REVIEWER_TOOLS]
    assert "write_file" not in names
    assert "edit_file" not in names
    assert "submit_review" in names


def test_edit_file_tool_schema_is_valid():
    params = AGENT_EDIT_FILE_TOOL["function"]["parameters"]
    required = params["required"]
    assert "file_path" in required
    assert "old_text" in required
    assert "new_text" in required
    assert "replace_all" not in required


def test_fullstack_tools_include_replace_and_insert():
    names = [t["function"]["name"] for t in FULLSTACK_EXPERT_TOOLS]
    assert "replace_lines" in names
    assert "insert_lines" in names


def test_reviewer_tools_do_not_include_replace_lines():
    names = [t["function"]["name"] for t in REVIEWER_TOOLS]
    assert "replace_lines" not in names
    assert "insert_lines" not in names


# ── file_tools.replace_lines ──────────────────────────────


def test_replace_lines_basic(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file("test.py", "line1\nline2\nline3\nline4\nline5\n")

    # 替换第 2-3 行
    result = tools.replace_lines("test.py", 2, 3, "new2\nnew3")
    assert result.replacements == 2

    content = tools.read_file("test.py").content
    assert content == "line1\nnew2\nnew3\nline4\nline5\n"


def test_replace_lines_delete(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file("test.py", "a\nb\nc\nd\n")

    # 删除第 2-3 行（new_content 为空）
    result = tools.replace_lines("test.py", 2, 3, "")
    assert result.replacements == 2

    content = tools.read_file("test.py").content
    assert content == "a\n\nd\n"


def test_replace_lines_replace_single_line(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file("test.py", "a\nb\nc\n")

    result = tools.replace_lines("test.py", 2, 2, "B")
    assert result.replacements == 1

    content = tools.read_file("test.py").content
    assert content == "a\nB\nc\n"


def test_replace_lines_expand(tmp_path):
    """替换 2 行为 4 行（扩展）。"""
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file("test.py", "a\nb\nc\n")

    tools.replace_lines("test.py", 2, 2, "B1\nB2\nB3")
    content = tools.read_file("test.py").content
    assert content == "a\nB1\nB2\nB3\nc\n"


def test_replace_lines_invalid_range(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file("test.py", "a\nb\n")

    with pytest.raises(ValueError, match="end_line.*不能小于.*start_line"):
        tools.replace_lines("test.py", 3, 1, "x")


def test_replace_lines_out_of_range(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file("test.py", "a\nb\n")

    with pytest.raises(ValueError, match="超出文件总行数"):
        tools.replace_lines("test.py", 5, 6, "x")


# ── file_tools.insert_lines ───────────────────────────────


def test_insert_lines_after_line(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file("test.py", "a\nb\nc\n")

    result = tools.insert_lines("test.py", 1, "inserted")
    assert result.replacements == 1

    content = tools.read_file("test.py").content
    assert content == "a\ninserted\nb\nc\n"


def test_insert_lines_at_beginning(tmp_path):
    """after_line=0 插入到文件开头。"""
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file("test.py", "a\nb\n")

    tools.insert_lines("test.py", 0, "header")
    content = tools.read_file("test.py").content
    assert content == "header\na\nb\n"


def test_insert_lines_multi_line(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file("test.py", "a\nb\n")

    result = tools.insert_lines("test.py", 1, "x\ny\nz")
    assert result.replacements == 3
    content = tools.read_file("test.py").content
    assert content == "a\nx\ny\nz\nb\n"


def test_insert_lines_out_of_range(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    tools = AgentTeamFileTools(workspace, service)

    tools.write_file("test.py", "a\nb\n")

    with pytest.raises(ValueError, match="超出文件总行数"):
        tools.insert_lines("test.py", 10, "x")


# ── tool_executor edit_file ───────────────────────────────


class FakeToolCall:
    def __init__(self, name: str, arguments: dict):
        self.function = type(
            "F", (), {"name": name, "arguments": json.dumps(arguments)}
        )()
        self.id = f"call_{name}"


@pytest.mark.asyncio
async def test_tool_executor_handles_edit_file(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentToolExecutor(workspace, service)

    # 先写文件
    write_tc = FakeToolCall(
        "write_file", {"file_path": "demo.py", "content": "x = 1\ny = 2\n"}
    )
    write_result = await executor.execute_tool_call(write_tc)
    assert write_result["success"] is True

    # 编辑
    edit_tc = FakeToolCall(
        "edit_file",
        {
            "file_path": "demo.py",
            "old_text": "x = 1",
            "new_text": "x = 42",
        },
    )
    edit_result = await executor.execute_tool_call(edit_tc)
    assert edit_result["success"] is True
    assert edit_result["replacements"] == 1


@pytest.mark.asyncio
async def test_tool_executor_edit_file_missing_text(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentToolExecutor(workspace, service)

    write_tc = FakeToolCall(
        "write_file", {"file_path": "demo.py", "content": "hello\n"}
    )
    await executor.execute_tool_call(write_tc)

    edit_tc = FakeToolCall(
        "edit_file",
        {
            "file_path": "demo.py",
            "old_text": "nonexistent",
            "new_text": "replacement",
        },
    )
    result = await executor.execute_tool_call(edit_tc)
    assert "error" in result
    assert "未找到" in result["error"]


@pytest.mark.asyncio
async def test_tool_executor_handles_unknown_tool(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentToolExecutor(workspace, service)

    tc = FakeToolCall("nonexistent_tool", {})
    result = await executor.execute_tool_call(tc)
    assert "error" in result
    assert "未知工具" in result["error"]


@pytest.mark.asyncio
async def test_tool_executor_finish_task(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentToolExecutor(workspace, service)

    tc = FakeToolCall(
        "finish_task",
        {
            "summary": "完成修改",
            "modified_files": ["a.py", "b.py"],
            "risk_level": "low",
        },
    )
    result = await executor.execute_tool_call(tc)
    assert result["_finish"] is True
    assert result["summary"] == "完成修改"
    assert len(result["modified_files"]) == 2


@pytest.mark.asyncio
async def test_tool_executor_submit_review(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentToolExecutor(workspace, service)

    tc = FakeToolCall(
        "submit_review",
        {
            "verdict": "pass",
            "score": 8,
            "summary": "代码质量良好",
        },
    )
    result = await executor.execute_tool_call(tc)
    assert result["_review"] is True
    assert result["verdict"] == "pass"
    assert result["score"] == 8


@pytest.mark.asyncio
async def test_tool_executor_replace_lines(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentToolExecutor(workspace, service)

    await executor.execute_tool_call(
        FakeToolCall(
            "write_file",
            {
                "file_path": "demo.py",
                "content": "a\nb\nc\nd\ne\n",
            },
        )
    )

    result = await executor.execute_tool_call(
        FakeToolCall(
            "replace_lines",
            {
                "file_path": "demo.py",
                "start_line": 2,
                "end_line": 4,
                "new_content": "B\nC\nD",
            },
        )
    )
    assert result["success"] is True
    assert result["lines_replaced"] == 3

    # 验证实际内容
    read_result = await executor.execute_tool_call(
        FakeToolCall(
            "read_file",
            {
                "file_path": "demo.py",
            },
        )
    )
    assert "B\nC\nD" in read_result["content"] or "B" in read_result["content"]


@pytest.mark.asyncio
async def test_tool_executor_replace_lines_delete(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentToolExecutor(workspace, service)

    await executor.execute_tool_call(
        FakeToolCall(
            "write_file",
            {
                "file_path": "demo.py",
                "content": "a\nb\nc\nd\n",
            },
        )
    )

    result = await executor.execute_tool_call(
        FakeToolCall(
            "replace_lines",
            {
                "file_path": "demo.py",
                "start_line": 2,
                "end_line": 3,
                "new_content": "",
            },
        )
    )
    assert result["success"] is True


@pytest.mark.asyncio
async def test_tool_executor_insert_lines(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentToolExecutor(workspace, service)

    await executor.execute_tool_call(
        FakeToolCall(
            "write_file",
            {
                "file_path": "demo.py",
                "content": "a\nb\nc\n",
            },
        )
    )

    result = await executor.execute_tool_call(
        FakeToolCall(
            "insert_lines",
            {
                "file_path": "demo.py",
                "after_line": 1,
                "content": "inserted",
            },
        )
    )
    assert result["success"] is True
    assert result["lines_inserted"] == 1


@pytest.mark.asyncio
async def test_tool_executor_insert_at_beginning(tmp_path):
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentToolExecutor(workspace, service)

    await executor.execute_tool_call(
        FakeToolCall(
            "write_file",
            {
                "file_path": "demo.py",
                "content": "a\nb\n",
            },
        )
    )

    result = await executor.execute_tool_call(
        FakeToolCall(
            "insert_lines",
            {
                "file_path": "demo.py",
                "after_line": 0,
                "content": "# header",
            },
        )
    )
    assert result["success"] is True


@pytest.mark.asyncio
async def test_tool_executor_edit_file_multiple_matches(tmp_path):
    """多处匹配时 executor 应返回错误信息。"""
    service = AgentTeamWorkspaceService(tmp_path)
    workspace = service.ensure_workspace("owner", "repo")
    executor = AgentToolExecutor(workspace, service)

    await executor.execute_tool_call(
        FakeToolCall(
            "write_file",
            {
                "file_path": "demo.py",
                "content": "x = 1\nx = 1\n",
            },
        )
    )

    result = await executor.execute_tool_call(
        FakeToolCall(
            "edit_file",
            {
                "file_path": "demo.py",
                "old_text": "x = 1",
                "new_text": "x = 2",
            },
        )
    )
    assert "error" in result
    assert "2 处匹配" in result["error"]
