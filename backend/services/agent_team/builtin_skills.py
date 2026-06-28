"""内置 Agent 技能定义与自动注册。"""

from __future__ import annotations

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.agent_skill_models import AgentSkill

BUILTIN_SKILLS: list[dict[str, str]] = [
    {
        "slug": "ruff-lint",
        "name": "Ruff Lint & Format",
        "content": (
            "---\n"
            "name: Ruff Lint & Format\n"
            "slug: ruff-lint\n"
            "description: 使用 Ruff 进行 Python 代码检查、自动修复和格式化\n"
            "when_to_use: 修改 Python 文件后，需要检查代码质量或修复格式问题时使用\n"
            'allowed-tools: ["run_command"]\n'
            "---\n"
            "\n"
            "# Ruff Lint & Format\n"
            "\n"
            "## 检查代码问题（不修改文件）\n"
            "ruff check <files-or-dirs>\n"
            "ruff check --output-format=concise .\n"
            "\n"
            "## 自动修复\n"
            "ruff check --fix <files-or-dirs>\n"
            "\n"
            "## 格式化\n"
            "ruff format <files-or-dirs>\n"
            "\n"
            "## 使用流程\n"
            "1. 修改文件后先运行 `ruff check` 发现问题\n"
            "2. 运行 `ruff check --fix` 自动修复可修复的问题\n"
            "3. 运行 `ruff format` 格式化代码\n"
            "4. 再次 `ruff check` 确认无遗留问题\n"
        ),
    },
]


async def install_builtin_skills(
    db: AsyncSession,
    service,  # AgentSkillService
) -> int:
    """注册内置技能，已存在则跳过。返回新安装数量。"""
    installed = 0
    for skill_def in BUILTIN_SKILLS:
        slug = skill_def["slug"]
        existing = await db.scalar(select(AgentSkill.id).where(AgentSkill.slug == slug))
        if existing is not None:
            continue
        content = skill_def["content"].encode("utf-8")
        await service.install_from_upload(
            db,
            content=content,
            filename="SKILL.md",
            name=skill_def["name"],
            created_by="system",
        )
        installed += 1
        logger.info("内置技能已注册: {}", slug)
    return installed
