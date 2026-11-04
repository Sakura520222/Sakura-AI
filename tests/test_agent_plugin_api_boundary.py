import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.api.v1 import config as api
from backend.api.v1.schemas import ConfigGeneralUpdateRequest
from backend.services.agent_team.plugin_config import PLUGIN_KEY


def test_generic_api_masks_plugin_headers_and_corrupt_json():
    raw = json.dumps(
        {
            "version": 1,
            "mcp": {
                "servers": [
                    {
                        "id": "docs",
                        "url": "https://example.test/mcp",
                        "headers": {"Authorization": "fake-header-secret"},
                    }
                ]
            },
        }
    )
    masked = api._mask_sensitive(raw, PLUGIN_KEY)
    assert "fake-header-secret" not in masked
    assert "docs" in masked
    assert "raw-secret" not in api._mask_sensitive("raw-secret", PLUGIN_KEY)
    assert api._mask_sensitive("unchanged", "ordinary_setting") == "unchanged"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [PLUGIN_KEY, "agent_team_permission_profile", "agent_team_mcp_io_timeout_seconds"],
)
async def test_generic_api_cannot_write_plugin_settings(key, monkeypatch):
    from backend.core import config

    monkeypatch.setattr(config, "load_dynamic_configs_to_settings", AsyncMock())
    row = SimpleNamespace(key_value="original")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: row)),
        commit=AsyncMock(),
    )
    response = await api.update_general_config(
        ConfigGeneralUpdateRequest(configs={key: "invalid"}),
        db=db,
        user={"sub": "fixture", "user_id": 1},
    )
    assert response.status_code == 400
    db.execute.assert_not_called()
    db.commit.assert_not_called()
