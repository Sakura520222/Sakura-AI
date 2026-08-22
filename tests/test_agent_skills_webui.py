"""Agent Skills WebUI 配置与翻译测试。"""

import re
from pathlib import Path
from types import SimpleNamespace

import yaml

from backend.core.config import (
    DYNAMIC_CONFIG_GROUPS,
    DYNAMIC_CONFIG_LABELS,
    Settings,
    get_dynamic_config_input_type,
)
from backend.webui.deps import get_templates
from backend.webui.i18n import i18n
from backend.webui.routes.agent_skills import _filter_skills, _metadata_list

AGENT_TEAM_DYNAMIC_KEYS = set(DYNAMIC_CONFIG_GROUPS["agent_team"]["keys"])
ROOT = Path(__file__).resolve().parents[1]


def _flatten(data: dict, prefix: str = "") -> dict[str, object]:
    flattened: dict[str, object] = {}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flattened.update(_flatten(value, full_key))
        else:
            flattened[full_key] = value
    return flattened


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
        "agent_skills.install_dialog_title",
        "agent_skills.search_placeholder",
        "agent_skills.status_validation",
        "agent_skills.file_manifest",
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


def test_agent_skills_templates_compile_and_use_compact_ledger():
    templates = get_templates()
    templates.env.get_template("agent_skills.html")
    templates.env.get_template("components/agent_skills_list_fragment.html")

    root = Path(__file__).parents[1]
    page = (root / "backend/webui/templates/agent_skills.html").read_text(
        encoding="utf-8"
    )
    fragment = (
        root / "backend/webui/templates/components/agent_skills_list_fragment.html"
    ).read_text(encoding="utf-8")

    assert '<dialog x-ref="installDialog"' in page
    assert 'role="tablist"' in page
    assert 'name="q"' in page
    assert 'name="status"' in page
    assert 'name="source"' in page
    assert "prefers-reduced-motion: reduce" in page
    assert "rounded-3xl" not in page
    assert "from-purple" not in page
    assert "<details" in fragment
    assert "agent_skills.file_manifest" in fragment
    assert "/agent-skills/{{ skill.id }}/toggle" in fragment
    assert "/agent-skills/{{ skill.id }}/delete" in fragment


def test_agent_skills_template_translation_keys_have_catalog_parity():
    template_paths = (
        ROOT / "backend/webui/templates/agent_skills.html",
        ROOT / "backend/webui/templates/components/agent_skills_list_fragment.html",
    )
    sources = [path.read_text(encoding="utf-8") for path in template_paths]
    for source in sources:
        get_templates().env.parse(source)
    referenced = set(re.findall(r"_\(\s*['\"]([^'\"]+)['\"]", "\n".join(sources)))

    for filename in ("zh-CN.yaml", "en.yaml"):
        locale_path = ROOT / "backend/webui/translations" / filename
        with locale_path.open(encoding="utf-8") as stream:
            catalog = yaml.safe_load(stream)
        missing = sorted(key for key in referenced if key not in _flatten(catalog))
        assert not missing, f"{filename} is missing Agent Skills keys: {missing}"


def test_agent_sidebar_navigation_name_is_agent_only():
    i18n.reload()
    assert i18n.t("nav.agent_team", lang="zh-CN") == "Agent"
    assert i18n.t("nav.agent_team", lang="en") == "Agent"
    assert i18n.t("config.ai_role_agent_team", lang="zh-CN") == "Agent 模型"
    assert i18n.t("config.ai_role_agent_team", lang="en") == "Agent model"
