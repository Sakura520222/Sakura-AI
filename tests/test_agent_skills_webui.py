"""Agent Skills WebUI 配置与翻译测试。"""

from backend.core.config import (
    DYNAMIC_CONFIG_GROUPS,
    DYNAMIC_CONFIG_LABELS,
    Settings,
    get_dynamic_config_input_type,
)
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
        "nav.agent_skills",
        "agent_skills.title",
        "agent_skills.install_github",
        "agent_skills.files",
        "config.group.agent_team",
        "config.label.agent_team_skills_enabled",
        "config.desc.agent_team_skills_root",
        "toast.agent_skill_installed",
        "toast.agent_skill_install_failed",
    ]

    for lang in ("zh-CN", "en"):
        for key in keys:
            assert i18n.t(key, lang=lang) != key
