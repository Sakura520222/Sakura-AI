"""Active subagent slots use the unified localized configuration contract."""

import pytest

from backend.models.database import AppConfig
from backend.services.config_center_service import config_center_service
from backend.webui.i18n import i18n
from tests.config_sqlite_session import SQLiteConfigSession
from tests.test_agent_repository_config_ui import REMOVED_SETTINGS


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh-CN", "en"])
async def test_subagent_slots_are_localized_and_keep_saved_value(language):
    key = "agent_team_subagent_concurrency"
    db = SQLiteConfigSession([AppConfig(key_name=key, key_value="3")])
    fields = {item["key"]: item for item in await config_center_service.describe(db, {"user_id": 1, "role": "super_admin"}, category="agent", lang=language)}
    assert not set(REMOVED_SETTINGS) & fields.keys()
    for kind, namespace in (("label", "label"), ("description", "desc")):
        catalog = f"config.{namespace}.{key}"
        assert fields[key][kind] == i18n.t(catalog, lang=language)
    assert fields[key]["type"] == "integer"
    assert fields[key]["constraints"]["ge"] == 1
    assert fields[key]["value"] == 3
