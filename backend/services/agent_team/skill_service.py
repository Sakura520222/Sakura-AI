"""Agent Skills 安装、索引与读取服务。"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
import yaml
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_dynamic_config, get_settings
from backend.models.agent_skill_models import AgentSkill

MAX_SKILL_BYTES = 512 * 1024
SKILL_FILE_NAME = "SKILL.md"
_SLUG_PATTERN = re.compile(r"[^a-z0-9_-]+")


@dataclass(frozen=True)
class GitHubSkillSource:
    """GitHub Skill 来源信息。"""

    owner: str
    repo: str
    ref: str
    path: str
    raw_url: str


def normalize_skill_slug(name: str) -> str:
    """生成可作为目录名使用的 Skill slug。"""
    value = (name or "").strip().lower().replace(" ", "-")
    value = _SLUG_PATTERN.sub("-", value).strip("-_")
    return value[:120].strip("-_") or "skill"


def parse_github_skill_url(url: str) -> GitHubSkillSource:
    """解析 GitHub blob/raw SKILL.md 链接。"""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("仅支持 HTTP/HTTPS GitHub 链接")

    host = parsed.netloc.lower()
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if host == "github.com":
        if len(parts) < 5 or parts[2] != "blob":
            raise ValueError("GitHub 链接必须指向 blob/SKILL.md 文件")
        owner, repo = parts[0], parts[1]
        ref, path = _split_github_ref_and_path(parts[3:])
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
        return GitHubSkillSource(owner=owner, repo=repo, ref=ref, path=path, raw_url=raw_url)

    if host == "raw.githubusercontent.com":
        if len(parts) < 4:
            raise ValueError("GitHub raw 链接格式不正确")
        owner, repo = parts[0], parts[1]
        ref, path = _split_github_ref_and_path(parts[2:])
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
        return GitHubSkillSource(owner=owner, repo=repo, ref=ref, path=path, raw_url=raw_url)

    raise ValueError("仅支持 github.com 或 raw.githubusercontent.com 链接")


def raw_url_from_github_blob(url: str) -> str:
    """将 GitHub blob/raw 链接解析为 raw 下载链接。"""
    return parse_github_skill_url(url).raw_url


def _find_skill_file_index(parts: list[str]) -> int:
    for index, part in enumerate(parts):
        if part.lower() == SKILL_FILE_NAME.lower():
            return index
    return -1


def _split_github_ref_and_path(parts: list[str]) -> tuple[str, str]:
    if len(parts) < 2:
        raise ValueError("GitHub 链接缺少分支或文件路径")
    ref = parts[0]
    path = "/".join(parts[1:])
    if Path(path).name.lower() != SKILL_FILE_NAME.lower():
        raise ValueError("GitHub 链接必须指向 SKILL.md")
    return ref, path


class AgentSkillService:
    """Agent Skills 服务。"""

    def __init__(self, root: str | Path | None = None):
        self._root = Path(root) if root is not None else None

    async def resolve_root(self) -> Path:
        """解析并创建 Skills 根目录。"""
        if self._root is not None:
            root_value = self._root
        else:
            configured = await get_dynamic_config("agent_team_skills_root")
            root_value = Path(str(configured or get_settings().agent_team_skills_root))
        root = root_value if root_value.is_absolute() else Path.cwd() / root_value
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    async def install_from_upload(
        self,
        db: AsyncSession,
        content: bytes | str,
        name: str = "",
        created_by: str = "",
    ) -> AgentSkill:
        """从上传的 SKILL.md 内容安装 Skill。"""
        text = self._decode_content(content)
        metadata = self._extract_metadata(text)
        skill_name = name.strip() or metadata.get("name") or "Uploaded Skill"
        slug = normalize_skill_slug(metadata.get("slug") or skill_name)
        install_path = await self._write_skill_file(slug, text)
        skill = await self._upsert_skill(
            db,
            slug=slug,
            name=skill_name,
            description=metadata.get("description", ""),
            when_to_use=metadata.get("when_to_use", ""),
            version=metadata.get("version", ""),
            source_type="upload",
            source_url="",
            source_ref="",
            source_path="",
            install_path=install_path,
            content=text,
            created_by=created_by,
        )
        logger.info("Agent Skill 已通过上传安装: slug={}, path={}", slug, install_path)
        return skill

    async def install_from_github_url(
        self,
        db: AsyncSession,
        url: str,
        created_by: str = "",
    ) -> AgentSkill:
        """从 GitHub SKILL.md 链接安装 Skill。"""
        source = parse_github_skill_url(url)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(source.raw_url)
            response.raise_for_status()
            content = response.content

        text = self._decode_content(content)
        metadata = self._extract_metadata(text)
        fallback_name = Path(source.path).parent.name or source.repo
        skill_name = metadata.get("name") or fallback_name
        slug = normalize_skill_slug(metadata.get("slug") or skill_name)
        install_path = await self._write_skill_file(slug, text)
        skill = await self._upsert_skill(
            db,
            slug=slug,
            name=skill_name,
            description=metadata.get("description", ""),
            when_to_use=metadata.get("when_to_use", ""),
            version=metadata.get("version", ""),
            source_type="github",
            source_url=url.strip(),
            source_ref=source.ref,
            source_path=source.path,
            install_path=install_path,
            content=text,
            created_by=created_by,
        )
        logger.info(
            "Agent Skill 已通过 GitHub 安装: slug={}, source={}",
            slug,
            source.raw_url,
        )
        return skill

    async def list_skills(self, db: AsyncSession) -> list[AgentSkill]:
        """列出所有已安装 Skill。"""
        result = await db.execute(select(AgentSkill).order_by(AgentSkill.updated_at.desc()))
        return list(result.scalars().all())

    async def set_enabled(
        self,
        db: AsyncSession,
        skill_id: int,
        enabled: bool,
    ) -> AgentSkill:
        """启用或停用 Skill。"""
        skill = await self._get_skill(db, skill_id)
        skill.enabled = 1 if enabled else 0
        await db.commit()
        await db.refresh(skill)
        logger.info("Agent Skill 状态已更新: slug={}, enabled={}", skill.slug, enabled)
        return skill

    async def delete_skill(self, db: AsyncSession, skill_id: int) -> AgentSkill:
        """删除 Skill 元数据及本地目录。"""
        skill = await self._get_skill(db, skill_id)
        skill_dir = Path(skill.install_path).parent.resolve()
        root = await self.resolve_root()
        if skill_dir == root or root not in skill_dir.parents:
            raise ValueError("Skill 安装路径不在 Skills 根目录内")

        await db.delete(skill)
        await db.commit()
        shutil.rmtree(skill_dir, ignore_errors=True)
        logger.info("Agent Skill 已删除: slug={}, path={}", skill.slug, skill_dir)
        return skill

    async def build_enabled_skills_summary(self, db: AsyncSession) -> str:
        """构建注入 Agent Prompt 的已启用 Skills 摘要。"""
        skills = await self._enabled_skills(db)
        if not skills:
            return ""

        lines = [
            "## 可用 Skills",
            "需要使用某个 Skill 时，调用 `use_skill` 并传入 skill slug 读取完整内容。",
        ]
        for skill in skills:
            lines.append(f"- `{skill.slug}`: {skill.name}")
            if skill.description:
                lines.append(f"  - description: {skill.description.strip()[:500]}")
            if skill.when_to_use:
                lines.append(f"  - when_to_use: {skill.when_to_use.strip()[:500]}")
        return "\n".join(lines)

    async def snapshot_enabled_skills(self, db: AsyncSession) -> list[dict[str, str | int]]:
        """返回已启用 Skills 的快照信息。"""
        skills = await self._enabled_skills(db)
        return [
            {
                "id": skill.id,
                "name": skill.name,
                "slug": skill.slug,
                "description": skill.description or "",
                "when_to_use": skill.when_to_use or "",
                "source_type": skill.source_type,
                "source_url": skill.source_url or "",
                "source_ref": skill.source_ref or "",
                "source_path": skill.source_path or "",
                "install_path": skill.install_path,
                "content_hash": skill.content_hash,
            }
            for skill in skills
        ]

    async def load_skill_content(self, slug: str) -> dict[str, str]:
        """按 slug 读取本地 SKILL.md 内容。"""
        normalized = normalize_skill_slug(slug)
        root = await self.resolve_root()
        skill_path = (root / normalized / SKILL_FILE_NAME).resolve()
        if root not in skill_path.parents:
            raise ValueError("Skill 路径不在 Skills 根目录内")
        if not skill_path.is_file():
            raise FileNotFoundError(f"Skill 不存在: {normalized}")
        content = skill_path.read_text(encoding="utf-8")
        return {
            "slug": normalized,
            "path": str(skill_path),
            "content": content,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }

    async def _enabled_skills(self, db: AsyncSession) -> list[AgentSkill]:
        result = await db.execute(
            select(AgentSkill)
            .where(AgentSkill.enabled == 1)
            .order_by(AgentSkill.name.asc(), AgentSkill.slug.asc())
        )
        return list(result.scalars().all())

    async def _get_skill(self, db: AsyncSession, skill_id: int) -> AgentSkill:
        result = await db.execute(select(AgentSkill).where(AgentSkill.id == skill_id))
        skill = result.scalar_one_or_none()
        if skill is None:
            raise ValueError("Skill 不存在")
        return skill

    async def _write_skill_file(self, slug: str, content: str) -> str:
        root = await self.resolve_root()
        skill_dir = (root / normalize_skill_slug(slug)).resolve()
        if root not in skill_dir.parents:
            raise ValueError("Skill 安装目录不在 Skills 根目录内")
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / SKILL_FILE_NAME
        skill_path.write_text(content, encoding="utf-8", newline="\n")
        return str(skill_path.resolve())

    async def _upsert_skill(
        self,
        db: AsyncSession,
        *,
        slug: str,
        name: str,
        description: str,
        when_to_use: str,
        version: str,
        source_type: str,
        source_url: str,
        source_ref: str,
        source_path: str,
        install_path: str,
        content: str,
        created_by: str,
    ) -> AgentSkill:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        result = await db.execute(select(AgentSkill).where(AgentSkill.slug == slug))
        skill = result.scalar_one_or_none()
        if skill is None:
            skill = AgentSkill(slug=slug, created_by=created_by)
            db.add(skill)

        skill.name = name[:255]
        skill.description = description or None
        skill.when_to_use = when_to_use or None
        skill.version = version[:100] if version else None
        skill.source_type = source_type
        skill.source_url = source_url or None
        skill.source_ref = source_ref[:255] if source_ref else None
        skill.source_path = source_path or None
        skill.install_path = install_path
        skill.enabled = 1
        skill.content_hash = content_hash
        skill.error_message = None
        await db.commit()
        await db.refresh(skill)
        return skill

    def _decode_content(self, content: bytes | str) -> str:
        if isinstance(content, str):
            raw = content.encode("utf-8")
            text = content
        else:
            raw = content
            text = content.decode("utf-8-sig")
        if len(raw) > MAX_SKILL_BYTES:
            raise ValueError(f"SKILL.md 不能超过 {MAX_SKILL_BYTES // 1024}KB")
        if not text.strip():
            raise ValueError("SKILL.md 内容不能为空")
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _extract_metadata(self, content: str) -> dict[str, str]:
        frontmatter = self._extract_frontmatter(content)
        metadata: dict[str, str] = {}
        if frontmatter:
            try:
                parsed = yaml.safe_load(frontmatter) or {}
                if isinstance(parsed, dict):
                    for key in ("name", "slug", "description", "when_to_use", "version"):
                        value = parsed.get(key)
                        if value is not None:
                            metadata[key] = str(value).strip()
            except yaml.YAMLError as exc:
                logger.debug("解析 Skill frontmatter 失败: {}", exc)

        if "name" not in metadata:
            title = self._extract_first_heading(content)
            if title:
                metadata["name"] = title
        if "description" not in metadata:
            paragraph = self._extract_first_paragraph(content)
            if paragraph:
                metadata["description"] = paragraph
        return metadata

    def _extract_frontmatter(self, content: str) -> str:
        if not content.startswith("---\n"):
            return ""
        end = content.find("\n---", 4)
        if end <= 0:
            return ""
        return content[4:end]

    def _extract_first_heading(self, content: str) -> str:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
        return ""

    def _extract_first_paragraph(self, content: str) -> str:
        for block in content.split("\n\n"):
            text = " ".join(line.strip() for line in block.splitlines() if line.strip())
            if not text or text.startswith("---") or text.startswith("#"):
                continue
            return text[:500]
        return ""