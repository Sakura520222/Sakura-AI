"""Issue templates retain safe field errors through section validation and WebUI."""

import json
import traceback
from urllib.parse import parse_qs, urlsplit

import pytest
from loguru import logger
from starlette.datastructures import FormData

from backend.core.config_sections import deep_merge, get_section_defaults
from backend.services.section_config_service import (
    SectionConfigValidationError,
    validate_section_config,
)
from backend.webui.routes.config import save_strategies_section


@pytest.mark.parametrize("field", ["comment_template", "comment_template_en"])
@pytest.mark.parametrize(
    ("template", "code", "params"),
    [
        (
            "PRIVATE_PROMPT {feasibility}",
            "missing_placeholders",
            {"placeholders": "summary"},
        ),
        (
            "PRIVATE_PROMPT {summary}",
            "missing_placeholders",
            {"placeholders": "feasibility"},
        ),
        (
            "PRIVATE_PROMPT {summary} {feasibility} {PRIVATE_SECRET}",
            "unknown_placeholders",
            {},
        ),
        (
            "PRIVATE_PROMPT {summary} {feasibility:{PRIVATE_SECRET}}",
            "unknown_placeholders",
            {},
        ),
        (
            "PRIVATE_PROMPT {summary} {feasibility} {",
            "invalid_template_format",
            {},
        ),
        (
            "PRIVATE_PROMPT {summary} {feasibility!PRIVATE_SECRET}",
            "invalid_template_format",
            {},
        ),
        (
            "PRIVATE_PROMPT {summary} {feasibility:PRIVATE_SECRET}",
            "invalid_template_format",
            {},
        ),
    ],
)
def test_issue_template_errors_preserve_safe_field_metadata(
    field, template, code, params
):
    effective = deep_merge(
        get_section_defaults("strategy.issue_analysis"), {field: template}
    )

    with pytest.raises(SectionConfigValidationError) as caught:
        validate_section_config("strategy.issue_analysis", effective)

    error = caught.value
    assert error.section == "strategy.issue_analysis"
    assert error.path == error.field == field
    assert error.code == code
    assert error.translation_key == f"section_validation.{code}"
    assert error.params == params
    published = json.dumps({"message": str(error), "metadata": vars(error)})
    published += "".join(traceback.format_exception(error))
    assert "PRIVATE_PROMPT" not in published
    assert "PRIVATE_SECRET" not in published


@pytest.mark.parametrize("field", ["comment_template", "comment_template_en"])
@pytest.mark.parametrize(
    "template",
    [
        "{summary} {feasibility}",
        "{{literal}} {summary!s} {feasibility:>12}",
        "{summary} {feasibility} {category} {priority} {labels}",
    ],
)
def test_issue_template_valid_metadata_omissions_and_formatting_remain_accepted(
    field, template
):
    validate_section_config(
        "strategy.issue_analysis",
        deep_merge(get_section_defaults("strategy.issue_analysis"), {field: template}),
    )


class _TemplateRequest:
    def __init__(self, form, ajax):
        self._form = FormData(form)
        self.headers = {"accept": "application/json"} if ajax else {}

    async def form(self):
        return self._form


class _ReadOnlySession:
    """Permit the override lookup; invalid templates must reject before writes."""

    async def execute(self, _statement):
        return self

    def scalar_one_or_none(self):
        return None

    def add(self, _row):
        raise AssertionError("Invalid templates must not write configuration")

    async def commit(self):
        raise AssertionError("Invalid templates must not commit configuration")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["issue_comment_template", "issue_comment_template_en"]
)
@pytest.mark.parametrize("language", ["zh-CN", "en"])
@pytest.mark.parametrize("ajax", [False, True])
@pytest.mark.parametrize(
    ("template", "code", "zh_reason", "en_reason"),
    [
        (
            "PRIVATE_PROMPT {summary}",
            "missing_placeholders",
            "缺少必需占位符",
            "Required template placeholders are missing",
        ),
        (
            "PRIVATE_PROMPT {summary} {feasibility} {PRIVATE_SECRET}",
            "unknown_placeholders",
            "未知占位符",
            "unsupported placeholders",
        ),
        (
            "PRIVATE_PROMPT {summary} {feasibility:PRIVATE_SECRET}",
            "invalid_template_format",
            "格式无效",
            "Invalid template format",
        ),
    ],
)
async def test_issue_template_form_errors_are_localized_without_input_or_log_leaks(
    field, language, ajax, template, code, zh_reason, en_reason
):
    form = {
        "cat_name": "bug",
        "cat_desc": "A failure",
        "cat_keywords": "error",
        "issue_system_prompt": "PRIVATE_SYSTEM_PROMPT",
        "issue_comment_template": "{summary} {feasibility}",
        "issue_comment_template_en": "{summary} {feasibility}",
        field: template,
    }
    logs = []
    sink = logger.add(logs.append, format="{message}", level="WARNING")
    try:
        response = await save_strategies_section(
            _TemplateRequest(form, ajax),
            db=_ReadOnlySession(),
            user={"sub": "admin", "role": "super_admin", "user_id": 1},
            csrf_token="test",
            section="issue_analysis",
            user_prefs={"language": language},
        )
    finally:
        logger.remove(sink)

    if ajax:
        assert response.status_code == 400
        feedback = json.loads(response.body)
        assert feedback["ok"] is False
    else:
        assert response.status_code == 302
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["_toast_type"] == ["error"]
        feedback = {
            "errors": json.loads(query["_errors"][0]),
            "toast": query["_toast"][0],
        }

    assert len(feedback["errors"]) == 1
    issue = feedback["errors"][0]
    assert issue["field"] == field
    assert issue["code"] == code
    reason = zh_reason if language == "zh-CN" else en_reason
    assert reason in issue["message"]
    assert reason in feedback["toast"]
    assert "section_validation." not in issue["message"]
    if code == "missing_placeholders":
        assert issue["params"] == {"placeholders": "feasibility"}
    else:
        assert issue["params"] == {}
    assert logs
    published = json.dumps(feedback) + "".join(logs)
    for private in ("PRIVATE_PROMPT", "PRIVATE_SECRET", "PRIVATE_SYSTEM_PROMPT"):
        assert private not in published
