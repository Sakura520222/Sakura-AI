"""Agent Team live view WebUI regression tests."""

from pathlib import Path

from backend.webui.i18n import i18n

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "webui"
    / "templates"
    / "components"
    / "agent_team_live_view_fragment.html"
)


def test_live_view_has_manual_refresh_and_natural_tool_views():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "refreshLiveView()" in template
    assert "buildToolCallView(tc)" in template
    assert "describeToolAction(name, args, status)" in template
    assert "parseToolArguments(argumentsJson)" in template
    assert "tc.name + '()'" not in template


def test_live_view_sanitizes_markdown_before_html_rendering():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "DOMPurify.sanitize(marked.parse(content))" in template


def test_live_view_incremental_processes_tool_updates_without_new_messages():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "data.tool_calls.length === 0" in template
    assert "this._processStreamData(data);" in template
    assert "!data.messages || data.messages.length === 0) return" not in template


def test_stream_data_exposes_assistant_message_id_for_tool_cards():
    route = (
        Path(__file__).resolve().parents[1]
        / "backend"
        / "webui"
        / "routes"
        / "agent_team.py"
    ).read_text(encoding="utf-8")

    assert '"assistant_message_id": tc.assistant_message_id' in route
    assert "msg_ids = [m.id for m in msg_rows]" not in route


def test_live_view_tool_translations_exist():
    i18n.reload()
    keys = [
        "agent_team.live_refresh",
        "agent_team.live_refreshing",
        "agent_team.live_tool_details",
        "agent_team.live_tool_result",
        "agent_team.live_tool_error",
        "agent_team.live_tool_read_file_running",
        "agent_team.live_tool_read_file_completed",
        "agent_team.live_tool_bash_running",
        "agent_team.live_tool_bash_completed",
        "agent_team.live_tool_search_running",
        "agent_team.live_tool_search_completed",
        "agent_team.live_tool_edit_running",
        "agent_team.live_tool_edit_completed",
        "agent_team.live_tool_generic_running",
        "agent_team.live_tool_generic_completed",
    ]

    for lang in ("zh-CN", "en"):
        for key in keys:
            assert i18n.t(key, lang=lang) != key
