"""Phase 2 configuration must render localized controls with persisted values."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from backend.webui.deps import get_templates
from backend.webui.i18n import i18n, make_translation_func
from backend.webui.routes.config import _build_dynamic_groups

REMOVED_SETTINGS = (
    "agent_team_max_model_rounds",
    "agent_team_max_tool_calls",
    "agent_team_max_parallel_reads",
    "agent_team_max_no_progress_rounds",
    "agent_team_repository_file_bytes",
    "agent_team_repository_total_bytes",
    "agent_team_repository_scan_entries",
    "agent_team_repository_skill_count",
    "agent_team_repository_metadata_bytes",
)


class Inputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name"):
            self.inputs[attrs["name"]] = attrs


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh-CN", "en"])
async def test_repository_config_controls_are_localized_and_preserve_values(language):
    rows = [
        SimpleNamespace(key_name="agent_team_skills_enabled", key_value="false"),
    ]
    rows.extend(
        SimpleNamespace(key_name=key, key_value="1") for key in REMOVED_SETTINGS
    )

    class Database:
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    i18n.reload()
    groups = await _build_dynamic_groups(Database(), language)
    group = next(group for group in groups if group["id"] == "agent_team")
    fields = {item["key"]: item for item in group["fields"]}
    assert not set(REMOVED_SETTINGS) & fields.keys()
    for key in ("agent_team_skills_enabled", "agent_team_skills_root"):
        for kind in ("label", "description"):
            catalog_key = f"config.{'desc' if kind == 'description' else kind}.{key}"
            translated = i18n.t(catalog_key, lang=language)
            assert translated != catalog_key, f"Missing {language}: {catalog_key}"
            assert fields[key][kind] == translated
            if language == "en":
                assert not re.search(r"[\u4e00-\u9fff]", fields[key][kind])

    html = (
        get_templates()
        .env.get_template("components/config_dynamic_card.html")
        .render(
            group=group,
            card_id="section-agent-team",
            _=make_translation_func(language),
        )
    )
    controls = Inputs()
    controls.feed(html)
    assert not set(REMOVED_SETTINGS) & controls.inputs.keys()
    assert "checked" not in controls.inputs["agent_team_skills_enabled"]
