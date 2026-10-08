"""Field-specific section validation without reflecting submitted content."""

import json

import pytest
import yaml

from backend.core.config_sections import deep_merge, get_section_defaults
from backend.services import section_config_service as service


def validate(section, changes):
    service.validate_section_config(
        section, deep_merge(get_section_defaults(section), changes)
    )


@pytest.mark.parametrize(
    ("section", "changes", "path", "code"),
    [
        (
            "strategy.strategies",
            {"standard": {"conditions": {"max_files": "PRIVATE_SECRET"}}},
            "standard.conditions.max_files",
            "integer_minimum",
        ),
        (
            "strategy.context_enhancement",
            {"search_in_files": {"concurrency": "PRIVATE_SECRET"}},
            "search_in_files.concurrency",
            "integer_required",
        ),
        (
            "strategy.review_policy",
            {"enabled": "PRIVATE_SECRET"},
            "enabled",
            "boolean_required",
        ),
        (
            "strategy.file_filters",
            {"skip_paths": [{"secret": "PRIVATE_SECRET"}]},
            "skip_paths[0]",
            "string_required",
        ),
        (
            "strategy.pr_summary",
            {"system_prompt": {"secret": "PRIVATE_SECRET"}},
            "system_prompt",
            "string_required",
        ),
        (
            "label.definitions",
            {"bug": {"color": "PRIVATE_SECRET"}},
            "bug.color",
            "label_color",
        ),
        (
            "label.recommendation",
            {"confidence_threshold": "PRIVATE_SECRET"},
            "confidence_threshold",
            "number_required",
        ),
        (
            "strategy.pr_dependency_graph",
            {"mode": "PRIVATE_SECRET"},
            "mode",
            "invalid_option",
        ),
        (
            "label.conflict_rules",
            {"bug": ["PRIVATE_SECRET\n"]},
            "bug[0]",
            "label_name_characters",
        ),
    ],
)
def test_structured_validation_identifies_field_without_echo(
    section, changes, path, code
):
    with pytest.raises(ValueError) as caught:
        validate(section, changes)
    error = caught.value
    assert isinstance(error, service.SectionConfigValidationError)
    assert error.section == section
    assert error.path == error.field == path
    assert error.code == code
    assert error.translation_key == f"section_validation.{code}"
    assert "PRIVATE_SECRET" not in str(error)
    assert "PRIVATE_SECRET" not in json.dumps(vars(error))


def test_missing_placeholders_identifies_template_and_uses_only_default_names():
    with pytest.raises(ValueError) as caught:
        validate("strategy.pr_summary", {"user_template": "PRIVATE_SECRET {title}"})
    error = caught.value
    assert isinstance(error, service.SectionConfigValidationError)
    assert error.path == "user_template"
    assert error.code == "missing_placeholders"
    assert "file_count" in error.params["placeholders"]
    assert "PRIVATE_SECRET" not in str(error)
    assert "PRIVATE_SECRET" not in json.dumps(vars(error))


def test_numeric_range_retains_constraints():
    with pytest.raises(ValueError) as caught:
        validate("label.recommendation", {"confidence_threshold": 2})
    assert caught.value.params == {"minimum": 0, "maximum": 1}
    assert caught.value.code == "number_range"


@pytest.mark.parametrize(
    ("section", "changes"),
    [
        ("strategy.strategies", {"PRIVATE_SECRET": None}),
        ("strategy.review_policy", {"review_templates": {"PRIVATE_SECRET": 2}}),
        ("strategy.issue_analysis", {"priority_rules": {"PRIVATE_SECRET": 2}}),
        ("label.definitions", {"PRIVATE_SECRET": {"color": "invalid"}}),
        ("label.conflict_rules", {"PRIVATE_SECRET": [2]}),
    ],
)
def test_unknown_mapping_keys_use_safe_index_paths(section, changes):
    with pytest.raises(ValueError) as caught:
        validate(section, changes)
    assert "PRIVATE_SECRET" not in str(caught.value)
    assert "PRIVATE_SECRET" not in json.dumps(vars(caught.value))


def test_validator_unknown_failure_is_safe_and_structured(monkeypatch):
    def invalid(_):
        raise ValueError("PRIVATE_SECRET")

    monkeypatch.setitem(service.SECTION_VALIDATORS, "strategy.scan", invalid)
    with pytest.raises(ValueError) as caught:
        validate("strategy.scan", {})
    assert isinstance(caught.value, service.SectionConfigValidationError)
    assert caught.value.code == "invalid_config"
    assert caught.value.section == "strategy.scan"
    assert "PRIVATE_SECRET" not in str(caught.value)


def test_placeholder_failure_keeps_legacy_valueerror_reason():
    with pytest.raises(ValueError, match="丢失必需占位符"):
        validate("strategy.pr_summary", {"user_template": "PRIVATE_SECRET"})


def test_unknown_save_mode_does_not_reflect_submitted_mode():
    import asyncio

    with pytest.raises(ValueError) as caught:
        asyncio.run(
            service.section_config_service.save_section(
                None, "strategy.pr_summary", {}, mode="PRIVATE_SECRET"
            )
        )
    assert caught.value.path == "mode"
    assert caught.value.code == "save_mode"
    assert "PRIVATE_SECRET" not in str(caught.value)


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_every_validation_reason_has_translations(locale):
    from pathlib import Path

    translations = yaml.safe_load(
        Path(f"backend/webui/translations/{locale}.yaml").read_text()
    )["section_validation"]
    assert set(service.SECTION_VALIDATION_CODES) <= set(translations)
    assert all(
        isinstance(translations[code], str) for code in service.SECTION_VALIDATION_CODES
    )
