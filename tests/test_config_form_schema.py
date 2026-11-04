"""The complete settings inventory must have translated, typed presentation hints."""

import json

import pytest

from backend.core.config import DYNAMIC_CONFIG_GROUPS
from backend.services.config_center_service import ConfigCenterService
from backend.services.config_form_schema import form_schema
from backend.services.config_metadata import field_catalog, model_fields
from backend.webui.i18n import i18n


@pytest.mark.parametrize("lang", ["zh-CN", "en"])
@pytest.mark.asyncio
async def test_inventory_labels_groups_and_model_paths_are_translated(lang):
    service = ConfigCenterService()
    user = {"user_id": 1, "role": "super_admin"}
    fields = await service.describe(None, user, lang=lang, search=True)
    fields += await service.describe(
        None, user, lang=lang, search=True, model=("openai", "fixture-model")
    )
    for field in fields:
        assert field["label"] != field["key"].replace("_", " "), field["id"]
        assert "config_center." not in field["label"]
        if field["path"] and not field["id"].startswith("binding/"):
            for part in field["path"]:
                key = "config_center.field." + part
                assert i18n.t(key, lang=lang) != key, field["id"]
        # Search still has no effective values, inherited values or version tokens.
        assert not {"value", "default", "inheritedValue", "version"} & field.keys()
        if field["type"] in {"array", "object"} and not field["id"].startswith(
            "binding/"
        ):
            assert field["editorSchema"], field["id"]
    personal = next(f for f in fields if f["id"] == "personal/theme")
    assert personal["optionLabels"]["system"] == i18n.t(
        "config_center.option.system", lang=lang
    )


def test_structured_schemas_cover_extensible_maps_empty_overrides_and_nullable_thinking():
    fields = field_catalog()
    categories = form_schema(fields["section/strategy.issue_analysis/categories"])
    assert categories["items"]["properties"]["keywords"]["items"]["type"] == "string"
    labels = form_schema(fields["section/label.definitions"])
    assert "bug" in labels["x-fixedKeys"]
    assert labels["additionalProperties"]["properties"]["color"]["type"] == "string"
    overrides = form_schema(fields["section/strategy.review_policy/repo_overrides"])
    assert "approve_threshold" in overrides["additionalProperties"]["properties"]
    thinking = form_schema(
        next(
            f
            for f in model_fields("openai", "fixture-model").values()
            if f.path == ("reasoning_params", "thinking")
        )
    )
    assert thinking["anyOf"][0]["type"] == "null"
    assert thinking["anyOf"][1]["properties"]["type"]["enum"] == [
        "adaptive",
        "disabled",
    ]
    # Form metadata contains structure, not a second copy of prompts or secrets.
    schemas = json.dumps([form_schema(f) for f in fields.values()])
    assert "Focus on syntax errors" not in schemas


@pytest.mark.parametrize("lang", ["zh-CN", "en"])
def test_legacy_field_help_is_retained_in_both_languages(lang):
    for field in field_catalog().values():
        if any(
            group.get("descriptions", {}).get(field.key)
            for group in DYNAMIC_CONFIG_GROUPS.values()
        ):
            assert ConfigCenterService._text(field, lang)[1], field.id
