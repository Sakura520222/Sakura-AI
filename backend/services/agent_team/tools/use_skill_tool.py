"""按需加载 Agent Skill 内容的只读工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.services.agent_team.skill_service import normalize_skill_slug
from backend.services.agent_team.tools.base import BaseTool, ToolContext, ToolResult


class UseSkillTool(BaseTool):
    """读取已启用 Skill 的完整内容，支持多文件技能目录。"""

    name = "use_skill"
    _schema = {
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": (
                "读取已启用 Agent Skill 的内容。"
                "默认读取 SKILL.md 主文件；可指定 file 参数读取技能目录中的其他附件。"
                "可传入 list_files=true 列出技能目录中所有文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Skill slug，例如 algodocs-automation。",
                    },
                    "file": {
                        "type": "string",
                        "description": "要读取的文件名，默认 SKILL.md。例如 template.py。",
                    },
                    "list_files": {
                        "type": "boolean",
                        "description": "设为 true 则列出技能目录中所有文件，不读取内容。",
                    },
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

        entry = skills_index[slug]
        install_path = Path(str(entry.get("install_path") or "")).resolve()
        skills_root_value = ctx.extra.get("skills_root")
        skills_root = Path(str(skills_root_value)).resolve() if skills_root_value else None

        skill_dir = install_path.parent if install_path.name.upper() == "SKILL.MD" else install_path
        if not skill_dir.is_dir():
            return ToolResult(success=False, error=f"Skill 目录不存在: {slug}")
        if skills_root and skills_root not in skill_dir.parents:
            return ToolResult(success=False, error="Skill 目录不在 Skills 根目录内")

        if args.get("list_files"):
            return self._list_files(slug, skill_dir, entry)

        target_file = str(args.get("file") or "").strip() or "SKILL.md"
        target_path = (skill_dir / target_file).resolve()
        if skill_dir not in target_path.parents and target_path.parent != skill_dir:
            return ToolResult(success=False, error="文件路径不在 Skill 目录内")
        if not target_path.is_file():
            return ToolResult(success=False, error=f"文件不存在: {slug}/{target_file}")

        cache_key = f"{slug}:{target_file}"
        cache = ctx.extra.setdefault("skills_cache", {})
        if cache_key in cache:
            cached = dict(cache[cache_key])
            cached["cached"] = True
            return ToolResult(success=True, output=cached)

        content = target_path.read_text(encoding="utf-8")
        output = {
            "slug": slug,
            "name": entry.get("name", slug),
            "file": target_file,
            "description": entry.get("description", ""),
            "when_to_use": entry.get("when_to_use", ""),
            "content": content,
            "content_hash": entry.get("content_hash", ""),
            "cached": False,
        }
        cache[cache_key] = dict(output)
        return ToolResult(success=True, output=output)

    @staticmethod
    def _list_files(
        slug: str, skill_dir: Path, entry: dict[str, Any]
    ) -> ToolResult:
        files = sorted(
            str(f.relative_to(skill_dir))
            for f in skill_dir.rglob("*")
            if f.is_file() and not f.name.startswith(".")
        )
        return ToolResult(
            success=True,
            output={
                "slug": slug,
                "name": entry.get("name", slug),
                "files": files,
                "file_count": len(files),
                "has_attachments": len(files) > 1,
            },
        )