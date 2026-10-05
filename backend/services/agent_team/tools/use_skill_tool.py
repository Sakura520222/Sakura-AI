"""Progressive Skill loading with fresh bounds and autonomous workflow cleanup."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from backend.services.agent_team.repository_context import (
    RepositoryContext,
    RepositoryContextError,
    skills_enabled,
)
from backend.services.agent_team.skill_scope import (
    SkillRestriction,
    parse_allowed_tools,
)
from backend.services.agent_team.skill_service import (
    MAX_SKILL_BYTES,
    SKILL_FILE_NAME,
    _safe_skill_relative_path,
    normalize_skill_slug,
)
from backend.services.agent_team.tools.base import (
    BaseTool,
    ToolContext,
    ToolMetadata,
    ToolResult,
)

_ARG_PLACEHOLDER_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _substitute_arguments(content: str, args: str, arg_names: list[str] | None) -> str:
    content = content.replace("$ARGUMENTS", args)
    if not arg_names:
        return content
    values = args.split() if args.strip() else []
    mapping = {
        name: values[i] if i < len(values) else "" for i, name in enumerate(arg_names)
    }
    return _ARG_PLACEHOLDER_RE.sub(
        lambda m: mapping.get(m.group(1) or m.group(2), m.group(0)), content
    )


class UseSkillTool(BaseTool):
    name = "use_skill"
    _schema = {
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": "按需读取已启用 Skill 主文件/附件，或只列出文件。end_skill=true 自主结束工作流，恢复此前运行时工具范围，不授予新权限。",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "已启用的 Skill slug。"},
                    "file": {
                        "type": "string",
                        "description": "技能目录内文件，默认 SKILL.md。",
                    },
                    "list_files": {
                        "type": "boolean",
                        "description": "只列出目录文件，不读取正文。",
                    },
                    "args": {
                        "type": "string",
                        "description": "替换 $ARGUMENTS 和声明的命名参数。",
                    },
                    "end_skill": {
                        "type": "boolean",
                        "default": False,
                        "description": "结束此工作流；Skill 全局关闭时也可清理已有工作流。",
                    },
                },
                "required": ["slug"],
            },
        },
    }

    def is_read_only(self) -> bool:
        return True

    def runtime_metadata(self):
        return ToolMetadata(read_only=True)

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        slug = normalize_skill_slug(str(args.get("slug") or ""))
        if args.get("end_skill") is True:
            ctx.active_skill_tools.pop(slug, None)
            return ToolResult(True, output={"slug": slug, "ended": True})
        ctx.skills_enabled = await skills_enabled()
        if not ctx.skills_enabled:
            ctx.extra.pop("skills_cache", None)
            return ToolResult(
                False, error="Agent Skills are disabled", error_code="SKILLS_DISABLED"
            )
        entry = (ctx.extra.get("skills_index") or {}).get(slug)
        if entry is None:
            return ToolResult(False, error=f"Skill 未启用或不存在: {slug}")
        try:
            scope = SkillRestriction.from_metadata(entry.get("allowed_tools"))
        except ValueError as exc:
            return ToolResult(
                False, error=str(exc), error_code="SKILL_METADATA_REJECTED"
            )
        filename = str(args.get("file") or SKILL_FILE_NAME).strip()
        safe_target = _safe_skill_relative_path(filename)
        if safe_target is None:
            return ToolResult(False, error="文件路径不在 Skill 目录内")
        try:
            if entry.get("source_type") == "repository":
                repository = ctx.repository_context
                if repository is None:
                    raise RepositoryContextError("Repository Skill context unavailable")
                if args.get("list_files"):
                    files = await asyncio.to_thread(repository.list_skill_files, entry)
                    return self._listing(slug, entry, files)
                content, current = await asyncio.to_thread(
                    repository.load_skill, entry, safe_target.as_posix()
                )
                scope = scope.intersect(
                    SkillRestriction.from_metadata(current.get("allowed_tools"))
                )
                entry = current
            else:
                reader, directory = self._admin_reader(entry, ctx)
                if args.get("list_files"):
                    files = await asyncio.to_thread(reader.list_files, directory)
                    return self._listing(slug, entry, files)
                content = await asyncio.to_thread(
                    reader.read_text,
                    directory / safe_target.as_posix(),
                    max_bytes=MAX_SKILL_BYTES,
                )
        except (OSError, ValueError) as exc:
            return ToolResult(
                False, error=str(exc), error_code="SKILL_CONTENT_REJECTED"
            )

        digest = hashlib.sha256(content.encode()).hexdigest()
        arguments = self._parse_json_list(entry.get("arguments", ""))
        args_str = str(args.get("args") or "").strip()
        if args_str:
            content = _substitute_arguments(content, args_str, arguments)
        cache = ctx.extra.setdefault("skills_cache", {})
        cache_key = json.dumps([slug, safe_target.as_posix(), args_str])
        cached = cache.get(cache_key, {}).get("content_hash") == digest
        output = {
            "slug": slug,
            "name": entry.get("name", slug),
            "description": entry.get("description", ""),
            "when_to_use": entry.get("when_to_use", ""),
            "file": safe_target.as_posix(),
            "content": content,
            "content_hash": digest,
            "cached": cached,
        }
        allowed = parse_allowed_tools(entry.get("allowed_tools"))
        if allowed is not None:
            output["allowed_tools"] = sorted(allowed)
        if entry.get("requires"):
            output["requires"] = entry["requires"]
        if arguments:
            output["arguments"] = arguments
        if entry.get("source_type") == "repository":
            output["source_type"] = "repository"
        # Retain fingerprints only. Every request securely reads fresh content.
        cache[cache_key] = {"content_hash": digest}
        previous = ctx.active_skill_tools.get(slug, SkillRestriction())
        ctx.active_skill_tools[slug] = previous.intersect(scope)
        return ToolResult(True, output=output)

    @staticmethod
    def _admin_reader(entry, ctx):
        # Preserve lexical components until no-follow descriptor opens.
        main = Path(str(entry.get("install_path") or ""))
        if not main.is_absolute():
            main = Path.cwd() / main
        directory = (
            main.parent if main.name.upper() == SKILL_FILE_NAME.upper() else main
        )
        configured = ctx.extra.get("skills_root")
        root = Path(str(configured)) if configured else directory.parent
        reader = RepositoryContext(root)
        return reader, reader.relative(directory)

    @staticmethod
    def _listing(slug, entry, files):
        return ToolResult(
            True,
            output={
                "slug": slug,
                "name": entry.get("name", slug),
                "files": files,
                "file_count": len(files),
                "has_attachments": len(files) > 1,
            },
        )

    @staticmethod
    def _parse_json_list(value: str) -> list[str]:
        if not value:
            return []
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except ValueError, TypeError:
            pass
        return []
