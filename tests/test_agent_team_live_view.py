"""Agent Team live view WebUI regression tests."""

from pathlib import Path

from backend.webui.i18n import i18n
from backend.webui.routes.agent_team import _message_guidance_ids

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "webui"
    / "templates"
    / "components"
    / "agent_team_live_view_fragment.html"
)
BASE_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "webui"
    / "templates"
    / "base.html"
)


def test_live_view_has_manual_refresh_and_natural_tool_views():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "refreshLiveView()" in template
    assert "buildToolCallView(tc, result = '')" in template
    assert "describeToolAction(name, args, status)" in template
    assert "parseToolArguments(argumentsJson)" in template
    assert "tc.name + '()'" not in template


def test_live_view_sanitizes_markdown_before_html_rendering():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "DOMPurify.sanitize(marked.parse(content))" in template


def test_base_markdown_code_blocks_have_theme_appropriate_foreground_colors():
    template = BASE_TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "--tw-prose-pre-code: #24292e" in template
    assert ".prose pre code.hljs" in template
    assert ".dark .prose pre code.hljs { color: #c9d1d9; }" in template


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


def test_stream_data_exposes_guidance_ids_for_prompt_deduplication():
    route = (
        Path(__file__).resolve().parents[1]
        / "backend"
        / "webui"
        / "routes"
        / "agent_team.py"
    ).read_text(encoding="utf-8")

    assert '"guidance_ids": _message_guidance_ids(m.message_json)' in route
    assert _message_guidance_ids(
        '{"role":"user","metadata":{"guidance_ids":[42,"43",42,"bad"]}}'
    ) == [42, 43]
    assert _message_guidance_ids("not-json") == []


def test_live_view_replaces_consumed_prompt_card_with_user_message():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "const consumedPromptIds = new Set()" in template
    assert "prompt.status !== 'pending'" in template
    assert "consumedPromptIds.has(String(prompt.id))" in template
    assert "type: 'prompt'" not in template


def test_live_view_user_bubbles_have_no_redundant_label():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "item.isInitial ? '{{ _('agent_team.live_initial_input') }}'" not in template
    assert "'{{ _('agent_team.live_user_guidance') }}'" not in template


def test_live_view_has_dedicated_back_to_bottom_control():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "aria-label=\"{{ _('common.back') }}\"" in template
    assert "agent_team.live_back_to_bottom" not in template
    assert "inline-flex h-9 w-9" in template
    assert "relative z-20 h-0 shrink-0" in template
    assert "absolute bottom-3 right-4" in template
    assert "bottom-28" not in template
    assert "↓ {{ _('agent_team.live_refresh') }}" not in template


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
