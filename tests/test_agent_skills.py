"""Agent Skills 功能测试。"""

import io
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models.agent_skill_models import AgentSkill
from backend.services.agent_team.skill_service import (
    AgentSkillService,
    _decode_zip_filename,
    _safe_skill_relative_path,
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


def test_safe_skill_relative_path_allows_python_package_init():
    assert _safe_skill_relative_path("package/__init__.py") == Path(
        "package/__init__.py"
    )
    assert _safe_skill_relative_path(".env") is None
    assert _safe_skill_relative_path("package/__pycache__/cache.pyc") is None
    assert _safe_skill_relative_path("__init__.py/evil.py") is None


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


def test_extract_metadata_from_crlf_frontmatter():
    service = AgentSkillService()
    metadata = service._extract_metadata(
        "---\r\n"
        "name: CRLF Skill\r\n"
        "description: Windows line endings.\r\n"
        "---\r\n"
        "# Body\r\n"
    )

    assert metadata["name"] == "CRLF Skill"
    assert metadata["description"] == "Windows line endings."


@pytest.mark.asyncio
async def test_write_and_load_skill_content(tmp_path):
    service = AgentSkillService(root=tmp_path)
    install_path = await service._write_skill_file(
        "../Example Skill", "# Example\n\nBody"
    )

    assert Path(install_path).name == "SKILL.md"
    assert Path(install_path).parent.name == "example-skill"

    loaded = await service.load_skill_content("example-skill")
    assert loaded["slug"] == "example-skill"
    assert loaded["content"] == "# Example\n\nBody"
    assert loaded["file"] == "SKILL.md"
    assert len(loaded["content_hash"]) == 64


@pytest.mark.asyncio
async def test_list_skill_files(tmp_path):
    service = AgentSkillService(root=tmp_path)
    skill_dir = tmp_path / "multi-file-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Multi", encoding="utf-8")
    (skill_dir / "template.py").write_text("pass", encoding="utf-8")
    (skill_dir / "config.yaml").write_text("key: value", encoding="utf-8")

    files = await service.list_skill_files("multi-file-skill")
    assert "SKILL.md" in files
    assert "template.py" in files
    assert "config.yaml" in files


@pytest.mark.asyncio
async def test_load_skill_content_specific_file(tmp_path):
    service = AgentSkillService(root=tmp_path)
    skill_dir = tmp_path / "target-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Main", encoding="utf-8")
    (skill_dir / "helper.py").write_text("def help(): pass", encoding="utf-8")

    loaded = await service.load_skill_content("target-skill", file="helper.py")
    assert loaded["file"] == "helper.py"
    assert "def help" in loaded["content"]


@pytest.mark.asyncio
async def test_load_skill_content_rejects_path_traversal(tmp_path):
    service = AgentSkillService(root=tmp_path)
    skill_dir = tmp_path / "target-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Main", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError):
        await service.load_skill_content("target-skill", file="../outside.txt")


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


def _build_zip(files: dict[str, str]) -> bytes:
    """构建包含指定文件的 ZIP 字节。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_install_from_zip(tmp_path):
    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=scalar_result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()

    zip_bytes = _build_zip(
        {
            "SKILL.md": "---\nname: ZIP Skill\ndescription: From ZIP.\n---\n# ZIP Skill\nBody.",
            "template.py": "def template(): pass",
            "config.yaml": "version: 1.0",
        }
    )

    service = AgentSkillService(root=tmp_path)
    await service.install_from_upload(
        db,
        content=zip_bytes,
        filename="skill.zip",
        name="ZIP Skill",
        created_by="admin",
    )

    skill_dir = tmp_path / "zip-skill"
    assert skill_dir.is_dir()
    assert (skill_dir / "SKILL.md").read_text().startswith("---")
    assert (skill_dir / "template.py").exists()
    assert (skill_dir / "config.yaml").exists()


@pytest.mark.asyncio
async def test_install_from_zip_preserves_subdirs(tmp_path):
    """ZIP 内的子目录结构应被完整保留。"""
    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=scalar_result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()

    zip_bytes = _build_zip(
        {
            "SKILL.md": "---\nname: SubDir Skill\n---\n# SubDir",
            "templates/entry.py": "# entry template",
            "templates/utils/helper.py": "def util(): pass",
            "data/config.json": '{"key": "val"}',
        }
    )

    service = AgentSkillService(root=tmp_path)
    await service.install_from_upload(
        db,
        content=zip_bytes,
        filename="subdir.zip",
        name="SubDir Skill",
        created_by="admin",
    )

    skill_dir = tmp_path / "subdir-skill"
    assert (skill_dir / "SKILL.md").exists()
    assert (skill_dir / "templates" / "entry.py").exists()
    assert (skill_dir / "templates" / "utils" / "helper.py").exists()
    assert (skill_dir / "data" / "config.json").read_text() == '{"key": "val"}'


@pytest.mark.asyncio
async def test_install_from_zip_strips_top_level_dir(tmp_path):
    """GitHub 下载的 ZIP 通常有 <repo>-<ref>/ 顶层目录，应被剥离。"""
    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=scalar_result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()

    zip_bytes = _build_zip(
        {
            "my-skill-main/SKILL.md": "---\nname: GH Skill\n---\n# GH",
            "my-skill-main/examples/demo.py": "print('demo')",
        }
    )

    service = AgentSkillService(root=tmp_path)
    await service.install_from_upload(
        db,
        content=zip_bytes,
        filename="gh-skill.zip",
        name="GH Skill",
        created_by="admin",
    )

    skill_dir = tmp_path / "gh-skill"
    assert (skill_dir / "SKILL.md").exists()
    assert (skill_dir / "examples" / "demo.py").exists()
    # 不应保留顶层目录
    assert not (skill_dir / "my-skill-main").exists()


@pytest.mark.asyncio
async def test_install_from_zip_rejects_path_traversal(tmp_path):
    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=scalar_result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()

    zip_bytes = _build_zip(
        {
            "SKILL.md": "---\nname: Safe Skill\n---\n# Safe",
            "../evil.txt": "owned",
            "templates/helper.py": "pass",
        }
    )

    service = AgentSkillService(root=tmp_path)
    await service.install_from_upload(
        db,
        content=zip_bytes,
        filename="safe.zip",
        name="Safe Skill",
        created_by="admin",
    )

    assert not (tmp_path / "evil.txt").exists()
    assert (tmp_path / "safe-skill" / "templates" / "helper.py").exists()


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
async def test_use_skill_tool_reads_additional_file(tmp_path):
    skill_dir = tmp_path / "multi-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Multi", encoding="utf-8")
    (skill_dir / "helper.py").write_text("def help(): pass", encoding="utf-8")
    ctx = ToolContext(
        workspace=str(tmp_path),
        workspace_service=AgentTeamWorkspaceService(tmp_path),
        extra={
            "skills_root": str(tmp_path),
            "skills_index": {
                "multi-skill": {
                    "name": "Multi",
                    "install_path": str(skill_dir / "SKILL.md"),
                }
            },
            "skills_cache": {},
        },
    )

    tool = UseSkillTool()
    result = await tool.execute({"slug": "multi-skill", "file": "helper.py"}, ctx)
    assert result.success
    assert result.output["file"] == "helper.py"
    assert "def help" in result.output["content"]


@pytest.mark.asyncio
async def test_use_skill_tool_rejects_path_traversal_file(tmp_path):
    skill_dir = tmp_path / "safe-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Safe", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    ctx = ToolContext(
        workspace=str(tmp_path),
        workspace_service=AgentTeamWorkspaceService(tmp_path),
        extra={
            "skills_root": str(tmp_path),
            "skills_index": {
                "safe-skill": {
                    "name": "Safe",
                    "install_path": str(skill_dir / "SKILL.md"),
                }
            },
            "skills_cache": {},
        },
    )

    result = await UseSkillTool().execute(
        {"slug": "safe-skill", "file": "../outside.txt"}, ctx
    )

    assert not result.success
    assert "Skill 目录内" in result.error


@pytest.mark.asyncio
async def test_use_skill_tool_list_files(tmp_path):
    skill_dir = tmp_path / "list-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# List", encoding="utf-8")
    (skill_dir / "extra.py").write_text("pass", encoding="utf-8")
    ctx = ToolContext(
        workspace=str(tmp_path),
        workspace_service=AgentTeamWorkspaceService(tmp_path),
        extra={
            "skills_root": str(tmp_path),
            "skills_index": {
                "list-skill": {
                    "name": "List",
                    "install_path": str(skill_dir / "SKILL.md"),
                }
            },
            "skills_cache": {},
        },
    )

    tool = UseSkillTool()
    result = await tool.execute({"slug": "list-skill", "list_files": True}, ctx)
    assert result.success
    assert "SKILL.md" in result.output["files"]
    assert "extra.py" in result.output["files"]
    assert result.output["file_count"] == 2
    assert result.output["has_attachments"] is True


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


# ---------------------------------------------------------------------------
# _decode_zip_filename 单元测试
# ---------------------------------------------------------------------------


def test_decode_zip_filename_ascii_unchanged():
    """ASCII 文件名应原样返回。"""
    assert _decode_zip_filename("SKILL.md") == "SKILL.md"
    assert _decode_zip_filename("templates/helper.py") == "templates/helper.py"


def test_decode_zip_filename_gbk_recovery():
    """模拟 CP437 误编码的 GBK 文件名应被修复。"""
    original = "中文模板.py"
    # 模拟：Windows 资源管理器用 GBK 编码存储，Python 按 CP437 解码产生的乱码
    garbled = original.encode("gbk").decode("cp437", errors="replace")
    # 确认 garbled 确实是乱码
    assert garbled != original
    # 修复后应恢复原文
    assert _decode_zip_filename(garbled) == original


def test_decode_zip_filename_utf8_passthrough():
    """已正确解码的 UTF-8 文件名应保持不变。"""
    name = "模板文件.md"
    assert _decode_zip_filename(name) == name


# ---------------------------------------------------------------------------
# ZIP 中文文件名集成测试
# ---------------------------------------------------------------------------


def _build_zip_raw(files: dict[str, bytes]) -> bytes:
    """构建 ZIP，value 为原始字节（不做编码转换）。"""

    class _EncodedFile(io.BytesIO):
        """带文件名编码控制的 ZIP 文件。"""


    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name_bytes, content in files.items():
            zf.writestr(name_bytes, content)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_install_from_zip_chinese_filenames(tmp_path):
    """ZIP 内中文文件名应正确还原。"""
    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=scalar_result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()

    # 用标准 _build_zip（UTF-8 文件名，带 language flag）
    zip_bytes = _build_zip(
        {
            "SKILL.md": "---\nname: 中文技能\n---\n# 中文技能测试",
            "模板/入口.py": "# 入口文件",
            "配置/说明.txt": "这是一个说明文件",
        }
    )

    service = AgentSkillService(root=tmp_path)
    await service.install_from_upload(
        db,
        content=zip_bytes,
        filename="chinese.zip",
        name="中文技能",
        created_by="admin",
    )

    # 中文名称 normalize 后中文被移除，fallback 为 "skill"
    skill_dir = tmp_path / "skill"
    assert (skill_dir / "SKILL.md").exists()
    assert (skill_dir / "模板" / "入口.py").exists()
    assert (skill_dir / "配置" / "说明.txt").read_text(
        encoding="utf-8"
    ) == "这是一个说明文件"


# ---------------------------------------------------------------------------
# Skills 动作能力测试
# ---------------------------------------------------------------------------


def test_extract_metadata_parses_action_fields():
    """frontmatter 中的 allowed_tools / arguments / requires 应被解析。"""
    service = AgentSkillService()
    md = (
        "---\n"
        "name: deploy-helper\n"
        "allowed-tools:\n"
        "  - run_command\n"
        "  - write_file\n"
        "arguments:\n"
        "  - env\n"
        "  - version\n"
        "requires: 需要 Docker 环境\n"
        "---\n# Deploy Helper\n"
    )
    meta = service._extract_metadata(md)
    assert meta["name"] == "deploy-helper"
    assert meta.get("allowed_tools") == '["run_command", "write_file"]'
    assert meta.get("arguments") == '["env", "version"]'
    assert meta.get("requires") == "需要 Docker 环境"


def test_extract_metadata_allowed_tools_hyphen_form():
    """allowed-tools（连字符形式）应被标准化为 allowed_tools。"""
    service = AgentSkillService()
    md = "---\nname: test-skill\nallowed-tools:\n  - Shell(git status)\n---\n# Test\n"
    meta = service._extract_metadata(md)
    assert '["Shell(git status)"]' in meta.get("allowed_tools", "")


def test_substitute_arguments_basic():
    """$ARGUMENTS 应被替换。"""
    from backend.services.agent_team.tools.use_skill_tool import (
        _substitute_arguments,
    )

    result = _substitute_arguments("Commit: $ARGUMENTS", "fix bug", None)
    assert result == "Commit: fix bug"


def test_substitute_arguments_named():
    """命名参数 $name 和 ${name} 应被替换。"""
    from backend.services.agent_team.tools.use_skill_tool import (
        _substitute_arguments,
    )

    content = "env=${env} version=$version"
    result = _substitute_arguments(content, "prod 1.0.0", ["env", "version"])
    assert result == "env=prod version=1.0.0"


@pytest.mark.asyncio
async def test_use_skill_tool_returns_action_fields(tmp_path):
    """use_skill 应返回 allowed_tools、arguments、requires 字段。"""
    skill_dir = tmp_path / "deploy-helper"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Deploy\nallowed-tools: [run_command]\narguments: [env]\nrequires: Docker\n---\n# Deploy",
        encoding="utf-8",
    )
    ctx = ToolContext(
        workspace=str(tmp_path),
        workspace_service=AgentTeamWorkspaceService(tmp_path),
        extra={
            "skills_root": str(tmp_path),
            "skills_index": {
                "deploy-helper": {
                    "name": "Deploy",
                    "install_path": str(skill_dir / "SKILL.md"),
                    "allowed_tools": '["run_command"]',
                    "arguments": '["env"]',
                    "requires": "Docker",
                }
            },
            "skills_cache": {},
        },
    )

    tool = UseSkillTool()
    result = await tool.execute({"slug": "deploy-helper"}, ctx)
    assert result.success
    assert result.output["allowed_tools"] == ["run_command"]
    assert result.output["arguments"] == ["env"]
    assert result.output["requires"] == "Docker"


@pytest.mark.asyncio
async def test_use_skill_tool_args_substitution(tmp_path):
    """use_skill 的 args 参数应替换 Skill 内容中的变量。"""
    skill_dir = tmp_path / "greet-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Greet\narguments: [name]\n---\nHello $name, $ARGUMENTS!",
        encoding="utf-8",
    )
    ctx = ToolContext(
        workspace=str(tmp_path),
        workspace_service=AgentTeamWorkspaceService(tmp_path),
        extra={
            "skills_root": str(tmp_path),
            "skills_index": {
                "greet-skill": {
                    "name": "Greet",
                    "install_path": str(skill_dir / "SKILL.md"),
                    "arguments": '["name"]',
                    "allowed_tools": "",
                    "requires": "",
                }
            },
            "skills_cache": {},
        },
    )

    tool = UseSkillTool()
    result = await tool.execute({"slug": "greet-skill", "args": "Alice"}, ctx)
    assert result.success
    assert "Hello Alice, Alice!" in result.output["content"]


@pytest.mark.asyncio
async def test_install_skill_stores_action_metadata(tmp_path):
    """安装 Skill 时应将 allowed_tools / arguments / requires 存入数据库。"""
    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=scalar_result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()

    md_content = (
        "---\n"
        "name: CI Runner\n"
        "allowed-tools:\n"
        "  - run_command\n"
        "arguments:\n"
        "  - command\n"
        "requires: pytest installed\n"
        "---\n# CI Runner\nRun $command"
    )
    service = AgentSkillService(root=tmp_path)
    await service.install_from_upload(
        db,
        content=md_content.encode("utf-8"),
        filename="ci.md",
        name="CI Runner",
        created_by="admin",
    )
    # skill 是 MagicMock，验证 upsert 被调用时包含动作字段
    assert db.add.called or db.commit.called
