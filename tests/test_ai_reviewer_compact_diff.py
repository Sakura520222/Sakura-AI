import pytest

from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.ai_reviewer.compact_diff import extend_with_diff_tools
from backend.services.ai_reviewer.constants import DIFF_TOOLS
from backend.services.ai_reviewer.prompt_builder import PromptBuilder
from backend.services.ai_reviewer.token_tracker import TokenTracker
from backend.services.ai_reviewer.tools.diff_tool import DiffToolHandler


SAMPLE_FILES = [
    {
        "path": "backend/example.py",
        "status": "modified",
        "additions": 2,
        "deletions": 1,
        "changes": 3,
        "patch": "@@ -1,2 +1,3 @@\n-old\n+new\n+line",
    },
    {
        "path": "res/logo.png",
        "status": "added",
        "additions": 0,
        "deletions": 0,
        "changes": 0,
    },
]


@pytest.fixture
def review_context():
    return {
        "title": "Compact diff review",
        "description": "Test PR",
        "repo_name": "owner/repo",
        "pr_number": 1,
        "base_branch": "main",
        "head_branch": "feature/compact-diff",
        "files": SAMPLE_FILES,
    }


def test_api_client_recognizes_prompt_exceeds_max_length():
    error = Exception(
        "Error code: 400 - {'error': {'code': '1261', "
        "'message': 'Prompt exceeds max length'}}"
    )

    assert AIApiClient._is_context_overflow_error(error) is True


def test_prompt_builder_compact_mode_omits_diff(review_context):
    builder = PromptBuilder()

    compact_message = builder.build_user_message(
        review_context, "balanced", include_tools=True, compact=True
    )

    assert "```diff" not in compact_message
    assert "backend/example.py" in compact_message
    assert "get_file_diff" in compact_message
    assert "list_changed_files" in compact_message


def test_prompt_builder_standard_mode_includes_diff(review_context):
    builder = PromptBuilder()

    standard_message = builder.build_user_message(
        review_context, "balanced", include_tools=True, compact=False
    )

    assert "```diff" in standard_message
    assert "get_file_diff" not in standard_message


@pytest.mark.asyncio
async def test_diff_tool_lists_and_returns_file_diff():
    diff_tool = DiffToolHandler()
    diff_tool.set_files_data(SAMPLE_FILES)

    changed_files = await diff_tool.list_changed_files()
    file_diff = await diff_tool.get_file_diff("backend/example.py")
    binary_info = await diff_tool.get_file_diff("res/logo.png")

    assert changed_files["total_files"] == 2
    assert changed_files["files"][0]["path"] == "backend/example.py"
    assert file_diff["diff"] == SAMPLE_FILES[0]["patch"]
    assert (
        binary_info["info"]
        == "该文件没有 diff 内容（可能是二进制文件或仅有元数据变更）"
    )


def test_extend_diff_tools_without_duplicates():
    base_tools = [
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "get_file_diff"}},
    ]

    extended_tools = extend_with_diff_tools(base_tools)
    tool_names = [tool["function"]["name"] for tool in extended_tools]

    assert tool_names.count("get_file_diff") == 1
    assert "list_changed_files" in tool_names


def test_extend_diff_tools_adds_all_diff_tools():
    base_tools = [
        {"type": "function", "function": {"name": "read_file"}},
    ]

    extended_tools = extend_with_diff_tools(base_tools)
    tool_names = [tool["function"]["name"] for tool in extended_tools]

    for diff_tool_name in DIFF_TOOLS:
        assert diff_tool_name in tool_names


def test_token_tracker_logs_context_usage():
    tracker = TokenTracker()

    tracker.log_context_usage(5000, 10000, 1)
    tracker.log_context_usage(8000, 10000, 2)
    tracker.log_context_usage(9500, 10000, 3)

    assert len(tracker.context_usage_log) == 3
    assert tracker.context_usage_log[0].percentage == 50.0
    assert tracker.context_usage_log[1].percentage == 80.0
    assert tracker.context_usage_log[2].percentage == 95.0
