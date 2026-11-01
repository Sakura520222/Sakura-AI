"""Issue templates retain safe field errors through section validation."""

import json
import traceback

import pytest

from backend.core.config_sections import deep_merge, get_section_defaults
from backend.services.section_config_service import (
    SectionConfigValidationError,
    validate_section_config,
)


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
