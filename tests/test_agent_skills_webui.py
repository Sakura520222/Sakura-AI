"""Agent Skills configuration, filtering and translation contracts.

The agent_skills.html page was replaced by the React SPA; the config
registration, catalog, pure helper and navigation contracts below still
exercise live backend code and stay under test.
"""

from types import SimpleNamespace

from backend.core.config import (
    DYNAMIC_CONFIG_GROUPS,
    DYNAMIC_CONFIG_LABELS,
    Settings,
    get_dynamic_config_input_type,
)
from backend.services.agent_team.skill_helpers import _filter_skills, _metadata_list
from backend.webui.i18n import i18n

AGENT_TEAM_DYNAMIC_KEYS = set(DYNAMIC_CONFIG_GROUPS["agent_team"]["keys"])


def test_agent_skills_dynamic_config_registered():
    settings = Settings()

    assert settings.agent_team_skills_enabled is True
    assert settings.agent_team_skills_root == "./Skills"
    assert "agent_team_skills_enabled" in AGENT_TEAM_DYNAMIC_KEYS
    assert "agent_team_skills_root" in AGENT_TEAM_DYNAMIC_KEYS
    assert DYNAMIC_CONFIG_LABELS["agent_team_skills_enabled"] == "启用 Agent Skills"
    assert DYNAMIC_CONFIG_LABELS["agent_team_skills_root"] == "Skills 根目录"
    assert get_dynamic_config_input_type("agent_team_skills_enabled") == "boolean"
    assert get_dynamic_config_input_type("agent_team_skills_root") == "text"


def test_agent_team_skills_config_registered_in_unified_group():
    assert {"agent_team_skills_enabled", "agent_team_skills_root"} <= set(
        AGENT_TEAM_DYNAMIC_KEYS
    )


def test_agent_skills_translations_exist():
    i18n.reload()
    keys = [
        "config.group.agent_team",
        "config.label.agent_team_skills_enabled",
        "config.desc.agent_team_skills_root",
        "toast.agent_skill_installed",
        "toast.agent_skill_install_failed",
    ]

    for lang in ("zh-CN", "en"):
        for key in keys:
            assert i18n.t(key, lang=lang) != key


def _skill(**overrides):
    values = {
        "name": "Release helper",
        "slug": "release-helper",
        "description": "Prepare release notes",
        "when_to_use": "When preparing a release",
        "version": "1.2.0",
        "source_type": "github",
        "source_url": "https://github.com/example/skills/blob/main/release/SKILL.md",
        "source_ref": "main",
        "allowed_tools": '["read_file", "run_command"]',
        "arguments": '["version"]',
        "requires": "Git repository",
        "enabled": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_agent_skills_filters_search_status_and_source():
    skills = [
        _skill(),
        _skill(
            name="Local formatter",
            slug="local-formatter",
            description="Format documents",
            when_to_use="When formatting local documents",
            source_type="upload",
            source_url="",
            source_ref="",
            allowed_tools='["edit_file"]',
            enabled=0,
        ),
    ]

    assert _filter_skills(skills, query="release") == [skills[0]]
    assert _filter_skills(skills, query="edit_file") == [skills[1]]
    assert _filter_skills(skills, status="enabled") == [skills[0]]
    assert _filter_skills(skills, status="disabled") == [skills[1]]
    assert _filter_skills(skills, source="upload") == [skills[1]]


def test_agent_skills_metadata_tags_fail_closed():
    assert _metadata_list('["read_file", " run_command ", ""]') == [
        "read_file",
        "run_command",
    ]
    assert _metadata_list('{"tool": "read_file"}') == []
    assert _metadata_list("not-json") == []
    assert _metadata_list(None) == []


def test_agent_sidebar_navigation_name_is_agent_only():
    i18n.reload()
    assert i18n.t("config.ai_role_agent_team", lang="zh-CN") == "Agent 模型"
