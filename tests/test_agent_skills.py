"""Agent Skills 功能测试。"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models.agent_skill_models import AgentSkill
from backend.services.agent_team.skill_service import (
    AgentSkillService,
    normalize_skill_slug,
    parse_github_skill_url,
    raw_url_from_github_blob,
)
from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.tools.use_skill_tool import UseSkillTool
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


USER_EXAMPLE_URL = (
    "https://github.com/ComposioHQ/awesome-claude-skills/blob/master/"
    "composio-skills/algodocs-automation/SKILL.md"
)
USER_EXAMPLE_RAW_URL = (
    "https://raw.githubusercontent.com/ComposioHQ/awesome-claude-skills/master/"
    "composio-skills/algodocs-automation/SKILL.md"
)


def test_parse_github_blob_skill_url_user_example():
    source = parse_github_skill_url(USER_EXAMPLE_URL)

    assert source.owner == "ComposioHQ"
    assert source.repo == "awesome-claude-skills"
    assert source.ref == "master"
    assert source.path == "composio-skills/algodocs-automation/SKILL.md"
    assert source.raw_url == USER_EXAMPLE_RAW_URL


def test_parse_github_raw_skill_url_user_example():
    source = parse_github_skill_url(USER_EXAMPLE_RAW_URL)

    assert source.owner == "ComposioHQ"
    assert source.repo == "awesome-claude-skills"
    assert source.ref == "master"
    assert source.path == "composio-skills/algodocs-automation/SKILL.md"
    assert source.raw_url == USER_EXAMPLE_RAW_URL


def test_raw_url_from_github_blob():
    assert raw_url_from_github_blob(USER_EXAMPLE_URL) == USER_EXAMPLE_RAW_URL


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("AlgoDocs Automation", "algodocs-automation"),
        ("../Bad Skill!!", "bad-skill"),
        ("中文 Skill", "skill"),
        ("", "skill"),
    ],
)
def test_normalize_skill_slug(value, expected):
    assert normalize_skill_slug(value) == expected


def test_extract_metadata_from_frontmatter():
    service = AgentSkillService()
    metadata = service._extract_metadata(
        "---\n"
        "name: AlgoDocs Automation\n"
        "slug: algodocs-automation\n"
        "description: Generate API docs.\n"
        "when_to_use: Use for documentation automation.\n"
        "version: 1.0.0\n"
        "---\n"
        "# Ignored title\n"
    )

    assert metadata["name"] == "AlgoDocs Automation"
    assert metadata["slug"] == "algodocs-automation"
    assert metadata["description"] == "Generate API docs."
    assert metadata["when_to_use"] == "Use for documentation automation."
    assert metadata["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_write_and_load_skill_content(tmp_path):
    service = AgentSkillService(root=tmp_path)
    install_path = await service._write_skill_file("../Example Skill", "# Example\n\nBody")

    assert Path(install_path).name == "SKILL.md"
    assert Path(install_path).parent.name == "example-skill"

    loaded = await service.load_skill_content("example-skill")
    assert loaded["slug"] == "example-skill"
    assert loaded["content"] == "# Example\n\nBody"
    assert len(loaded["content_hash"]) == 64


@pytest.mark.asyncio
async def test_build_enabled_skills_summary():
    service = AgentSkillService()
    db = MagicMock()
    skill = AgentSkill(
        id=1,
        name="AlgoDocs Automation",
        slug="algodocs-automation",
        description="Generate API documentation.",
        when_to_use="When documentation automation is needed.",
        source_type="upload",
        install_path="/tmp/skill/SKILL.md",
        content_hash="a" * 64,
        enabled=1,
    )
    service._enabled_skills = AsyncMock(return_value=[skill])

    summary = await service.build_enabled_skills_summary(db)

    assert "`algodocs-automation`" in summary
    assert "Generate API documentation." in summary
    assert "use_skill" in summary


@pytest.mark.asyncio
async def test_use_skill_tool_reads_and_caches(tmp_path):
    skill_dir = tmp_path / "algodocs-automation"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# AlgoDocs\n\nUse this skill.", encoding="utf-8")
    ctx = ToolContext(
        workspace=str(tmp_path),
        workspace_service=AgentTeamWorkspaceService(tmp_path),
        extra={
            "skills_root": str(tmp_path),
            "skills_index": {
                "algodocs-automation": {
                    "name": "AlgoDocs",
                    "slug": "algodocs-automation",
                    "description": "Docs skill",
                    "when_to_use": "Use for docs",
                    "source_type": "upload",
                    "source_url": "",
                    "install_path": str(skill_file),
                    "content_hash": "abc",
                }
            },
            "skills_cache": {},
        },
    )

    tool = UseSkillTool()
    first = await tool.execute({"slug": "algodocs-automation"}, ctx)
    second = await tool.execute({"slug": "algodocs-automation"}, ctx)

    assert first.success
    assert first.output["content"] == "# AlgoDocs\n\nUse this skill."
    assert first.output["cached"] is False
    assert second.success
    assert second.output["cached"] is True


@pytest.mark.asyncio
async def test_use_skill_tool_rejects_disabled_skill(tmp_path):
    ctx = ToolContext(
        workspace=str(tmp_path),
        workspace_service=AgentTeamWorkspaceService(tmp_path),
        extra={"skills_index": {}, "skills_cache": {}},
    )

    result = await UseSkillTool().execute({"slug": "missing"}, ctx)

    assert not result.success
    assert "未启用或不存在" in result.error
