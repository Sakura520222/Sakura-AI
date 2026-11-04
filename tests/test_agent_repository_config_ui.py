"""The unified metadata keeps localized Agent controls and persisted values."""

import re

import pytest

from backend.models.database import AppConfig
from backend.services.config_center_service import config_center_service
from backend.webui.i18n import i18n
from tests.config_sqlite_session import SQLiteConfigSession

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

@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh-CN", "en"])
async def test_repository_config_controls_are_localized_and_preserve_values(language):
    db = SQLiteConfigSession([AppConfig(key_name="agent_team_skills_enabled", key_value="false")])
    fields = {item["key"]: item for item in await config_center_service.describe(db, {"user_id": 1, "role": "super_admin"}, category="agent", lang=language)}
    assert not set(REMOVED_SETTINGS) & fields.keys()
    for key in ("agent_team_skills_enabled", "agent_team_skills_root"):
        for kind in ("label", "description"):
            catalog_key = f"config.{'desc' if kind == 'description' else kind}.{key}"
            assert fields[key][kind] == i18n.t(catalog_key, lang=language)
            if language == "en":
                assert not re.search(r"[\u4e00-\u9fff]", fields[key][kind])
    assert fields["agent_team_skills_enabled"]["value"] is False
    assert fields["agent_team_skills_enabled"]["type"] == "boolean"
