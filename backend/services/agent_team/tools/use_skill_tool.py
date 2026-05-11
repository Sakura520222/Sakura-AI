"""按需加载 Agent Skill 内容的只读工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.services.agent_team.skill_service import normalize_skill_slug
from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult


class UseSkillTool(BaseTool):
    """读取已启用 Skill 的完整 SKILL.md 内容。"""

    name = "use_skill"
    _schema = {
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": "读取已启用 Agent Skill 的完整 SKILL.md 内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Skill slug，例如 algodocs-automation。",
                    }
                },
                "required": ["slug"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        slug = normalize_skill_slug(str(args.get("slug") or ""))
        skills_index = ctx.extra.get("skills_index") or {}
        if slug not in skills_index:
            return ToolResult(success=False, error=f"Skill 未启用或不存在: {slug}")

        cache = ctx.extra.setdefault("skills_cache", {})
        if slug in cache:
            cached = dict(cache[slug])
            cached["cached"] = True
            return ToolResult(success=True, output=cached)

        entry = skills_index[slug]
        install_path = Path(str(entry.get("install_path") or "")).resolve()
        skills_root_value = ctx.extra.get("skills_root")
        skills_root = Path(str(skills_root_value)).resolve() if skills_root_value else None
        if not install_path.is_file():
            return ToolResult(success=False, error=f"Skill 文件不存在: {slug}")
        if skills_root and skills_root not in install_path.parents:
            return ToolResult(success=False, error="Skill 文件不在 Skills 根目录内")

        content = install_path.read_text(encoding="utf-8")
        output = {
            "slug": slug,
            "name": entry.get("name", slug),
            "description": entry.get("description", ""),
            "when_to_use": entry.get("when_to_use", ""),
            "source_type": entry.get("source_type", ""),
            "source_url": entry.get("source_url", ""),
            "content_hash": entry.get("content_hash", ""),
            "content": content,
            "cached": False,
        }
        cache[slug] = dict(output)
        return ToolResult(success=True, output=output)