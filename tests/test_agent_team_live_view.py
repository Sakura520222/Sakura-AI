"""Agent Team stream payload regressions and active configuration coverage.

The Alpine live-view fragment and its labels were replaced by the React SPA;
the stream payload helpers remain in use by the mounted /api/v1/agent routes.
"""

from pathlib import Path

from backend.core.config import DYNAMIC_CONFIG_GROUPS
from backend.services.agent_team.api_operations import _message_guidance_ids

ROUTE_PATH = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "services"
    / "agent_team"
    / "api_operations.py"
)


def test_stream_data_exposes_assistant_message_id_for_tool_cards():
    route = ROUTE_PATH.read_text(encoding="utf-8")

    assert '"assistant_message_id": tc.assistant_message_id' in route
    assert "msg_ids = [m.id for m in msg_rows]" not in route


def test_stream_data_exposes_guidance_ids_for_prompt_deduplication():
    route = ROUTE_PATH.read_text(encoding="utf-8")

    assert '"guidance_ids": _message_guidance_ids(m.message_json)' in route
    assert _message_guidance_ids(
        '{"role":"user","metadata":{"guidance_ids":[42,"43",42,"bad"]}}'
    ) == [42, 43]
    assert _message_guidance_ids("not-json") == []


def test_agent_team_dynamic_config_contains_live_subagent_control():
    keys = set(DYNAMIC_CONFIG_GROUPS["agent_team"]["keys"])
    assert "agent_team_subagent_concurrency" in keys
