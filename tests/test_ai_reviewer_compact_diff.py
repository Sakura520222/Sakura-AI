import pytest

from backend.services.ai_reviewer.api_client import AIApiClient
from backend.services.ai_reviewer.constants import COMPACT_TOOLS
from backend.services.ai_reviewer.prompt_builder import PromptBuilder
from backend.services.ai_reviewer.reviewer import AIReviewer
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


def test_prompt_builder_exposes_diff_tools_only_in_compact_mode(review_context):
    builder = PromptBuilder()

    standard_message = builder.build_user_message(
        review_context, "balanced", include_tools=True, compact=False
    )
    compact_message = builder.build_user_message(
        review_context, "balanced", include_tools=True, compact=True
    )

    assert "```diff" in standard_message
    assert "get_file_diff" not in standard_message
    assert "list_changed_files" not in standard_message

    assert "```diff" not in compact_message
    assert "backend/example.py" in compact_message
    assert "get_file_diff" in compact_message
    assert "list_changed_files" in compact_message


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
    assert binary_info["info"] == "该文件没有 diff 内容（可能是二进制文件或仅有元数据变更）"


def test_ai_reviewer_extends_compact_tools_without_duplicates(monkeypatch):
    reviewer = AIReviewer.__new__(AIReviewer)

    base_tools = [
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "get_file_diff"}},
    ]

    extended_tools = reviewer._extend_with_compact_tools(base_tools)
    tool_names = [tool["function"]["name"] for tool in extended_tools]

    assert tool_names.count("get_file_diff") == 1
    assert "list_changed_files" in tool_names


def test_ai_reviewer_should_use_compact_prompt_when_over_budget(review_context):
    reviewer = AIReviewer.__new__(AIReviewer)
    reviewer._get_initial_prompt_budget = lambda _messages: (101, 100)

    should_compact, current_tokens, threshold_tokens = reviewer._should_use_compact_prompt(
        [{"role": "user", "content": "large prompt"}], review_context
    )

    assert should_compact is True
    assert current_tokens == 101
    assert threshold_tokens == 100


@pytest.mark.asyncio
async def test_compact_diff_review_uses_compact_messages_and_tools(monkeypatch, review_context):
    reviewer = AIReviewer.__new__(AIReviewer)
    reviewer.prompt_builder = PromptBuilder()
    reviewer.context_compressor = type(
        "Compressor", (), {"estimate_messages_tokens": lambda self, messages: 42}
    )()

    async def fake_run_tool_loop(**kwargs):
        tool_names = [tool["function"]["name"] for tool in kwargs["enabled_tools"]]
        user_message = kwargs["messages"][1]["content"]

        assert all(tool_name in tool_names for tool_name in COMPACT_TOOLS)
        assert "```diff" not in user_message
        assert "get_file_diff" in user_message
        assert kwargs["tool_handler"].diff_tool.has_data is True
        return {"summary": "ok", "comments": [], "inline_comments": []}

    monkeypatch.setattr(reviewer, "_run_tool_loop", fake_run_tool_loop)
    monkeypatch.setattr(
        reviewer,
        "_build_tool_handler_with_diff",
        lambda diff_tool: type("Handler", (), {"diff_tool": diff_tool})(),
    )

    result = await reviewer._run_compact_diff_review(
        context=review_context,
        strategy="balanced",
        system_prompt="system",
        enabled_tools=[],
        repo=None,
        pr=None,
        tracker=TokenTracker(),
        original_tokens=1000,
        reason="test",
    )

    assert result["summary"] == "ok"
