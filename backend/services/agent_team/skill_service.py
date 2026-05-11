"""Agent Skills 安装、索引与读取服务。"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import zipfile
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
MAX_SKILL_DIR_BYTES = 5 * 1024 * 1024
SKILL_FILE_NAME = "SKILL.md"
_SLUG_PATTERN = re.compile(r"[^a-z0-9_-]+")
_GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class GitHubSkillSource:
    """GitHub Skill 来源信息。"""

    owner: str
    repo: str
    ref: str
    path: str
    raw_url: str


@dataclass(frozen=True)
class GitHubSkillDirectory:
    """解析后的 GitHub 技能目录信息。"""

    owner: str
    repo: str
    ref: str
    dir_path: str
    skill_file_path: str


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
        content: bytes,
        filename: str = "",
        name: str = "",
        created_by: str = "",
    ) -> AgentSkill:
        """从上传内容安装 Skill，支持单 SKILL.md 或 ZIP 压缩包。"""
        lower_name = (filename or "").lower()
        if lower_name.endswith(".zip"):
            return await self._install_from_zip(db, content, name, created_by)

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
            file_count=1,
        )
        logger.info("Agent Skill 已通过上传安装: slug={}, path={}", slug, install_path)
        return skill

    async def _install_from_zip(
        self,
        db: AsyncSession,
        content: bytes,
        name: str,
        created_by: str,
    ) -> AgentSkill:
        """从 ZIP 压缩包安装 Skill。"""
        if len(content) > MAX_SKILL_DIR_BYTES:
            raise ValueError(f"ZIP 文件不能超过 {MAX_SKILL_DIR_BYTES // (1024 * 1024)}MB")

        extracted: dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                basename = Path(info.filename).name
                if basename.startswith(".") or basename.startswith("__"):
                    continue
                raw = zf.read(info)
                if len(raw) > MAX_SKILL_BYTES:
                    raise ValueError(f"文件 {basename} 超过 {MAX_SKILL_BYTES // 1024}KB")
                extracted[basename] = raw.decode("utf-8-sig").replace("\r\n", "\n")

        skill_text = extracted.get(SKILL_FILE_NAME)
        if not skill_text or not skill_text.strip():
            raise ValueError("ZIP 中缺少 SKILL.md 或内容为空")

        metadata = self._extract_metadata(skill_text)
        skill_name = name.strip() or metadata.get("name") or "Uploaded Skill"
        slug = normalize_skill_slug(metadata.get("slug") or skill_name)
        root = await self.resolve_root()
        skill_dir = await self._ensure_skill_dir(root, slug)

        for fname, fcontent in extracted.items():
            target = (skill_dir / fname).resolve()
            if skill_dir not in target.parents and target != skill_dir / fname:
                continue
            target.write_text(fcontent, encoding="utf-8", newline="\n")

        install_path = str((skill_dir / SKILL_FILE_NAME).resolve())
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
            content=skill_text,
            created_by=created_by,
            file_count=len(extracted),
        )
        logger.info(
            "Agent Skill 已通过 ZIP 上传安装: slug={}, files={}", slug, len(extracted)
        )
        return skill

    async def install_from_github_url(
        self,
        db: AsyncSession,
        url: str,
        created_by: str = "",
    ) -> AgentSkill:
        """从 GitHub SKILL.md 链接安装 Skill（下载整个技能目录）。"""
        source = parse_github_skill_url(url)
        dir_info = _parse_github_skill_directory(source)

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            dir_files = await self._download_github_skill_directory(client, dir_info)
            if not dir_files:
                response = await client.get(source.raw_url)
                response.raise_for_status()
                dir_files = {SKILL_FILE_NAME: response.content}

        skill_content = dir_files.get(SKILL_FILE_NAME)
        if not skill_content:
            raise ValueError("GitHub 目录中未找到 SKILL.md")

        text = self._decode_content(skill_content)
        metadata = self._extract_metadata(text)
        fallback_name = Path(dir_info.dir_path).name or source.repo
        skill_name = metadata.get("name") or fallback_name
        slug = normalize_skill_slug(metadata.get("slug") or skill_name)

        root = await self.resolve_root()
        skill_dir = await self._ensure_skill_dir(root, slug)

        for fname, fcontent in dir_files.items():
            if fname.lower() == SKILL_FILE_NAME.lower():
                decoded = text
            else:
                try:
                    decoded = fcontent.decode("utf-8-sig").replace("\r\n", "\n")
                except UnicodeDecodeError:
                    continue
            target = (skill_dir / fname).resolve()
            if skill_dir not in target.parents and target != skill_dir / fname:
                continue
            if isinstance(decoded, str):
                target.write_text(decoded, encoding="utf-8", newline="\n")
            else:
                target.write_bytes(fcontent)

        install_path = str((skill_dir / SKILL_FILE_NAME).resolve())
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
            file_count=len(dir_files),
        )
        logger.info(
            "Agent Skill 已通过 GitHub 安装: slug={}, files={}, source={}",
            slug,
            len(dir_files),
            dir_info.dir_path,
        )
        return skill

    async def _download_github_skill_directory(
        self,
        client: httpx.AsyncClient,
        dir_info: GitHubSkillDirectory,
    ) -> dict[str, bytes]:
        """通过 GitHub Contents API 下载技能目录中的所有文件。"""
        api_url = (
            f"{_GITHUB_API}/repos/{dir_info.owner}/{dir_info.repo}"
            f"/contents/{dir_info.dir_path}?ref={dir_info.ref}"
        )
        headers = {"Accept": "application/vnd.github.v3+json"}

        try:
            response = await client.get(api_url, headers=headers)
            if response.status_code != 200:
                logger.debug("GitHub API 返回 {}: {}", response.status_code, api_url)
                return {}
            entries = response.json()
        except Exception as exc:
            logger.debug("GitHub API 请求失败: {}", exc)
            return {}

        if not isinstance(entries, list):
            return {}

        files: dict[str, bytes] = {}
        total_size = 0
        for entry in entries:
            if entry.get("type") != "file":
                continue
            name = entry.get("name", "")
            if name.startswith(".") or name.startswith("__"):
                continue
            download_url = entry.get("download_url", "")
            if not download_url:
                continue
            try:
                file_resp = await client.get(download_url)
                file_resp.raise_for_status()
                content = file_resp.content
                total_size += len(content)
                if total_size > MAX_SKILL_DIR_BYTES:
                    logger.warning("GitHub 技能目录总大小超限: {}", dir_info.dir_path)
                    break
                files[name] = content
            except Exception as exc:
                logger.debug("下载 GitHub 文件失败: {} - {}", name, exc)
        return files

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

    async def load_skill_content(self, slug: str, file: str = "") -> dict[str, str]:
        """按 slug 读取本地技能文件内容，默认读取 SKILL.md。"""
        normalized = normalize_skill_slug(slug)
        root = await self.resolve_root()
        target_name = file.strip() if file else SKILL_FILE_NAME
        skill_dir = (root / normalized).resolve()
        if root not in skill_dir.parents and skill_dir != root / normalized:
            raise ValueError("Skill 路径不在 Skills 根目录内")
        skill_path = (skill_dir / target_name).resolve()
        if skill_dir not in skill_path.parents and skill_path != skill_dir / target_name:
            raise ValueError("文件路径不在 Skill 目录内")
        if not skill_path.is_file():
            raise FileNotFoundError(f"文件不存在: {normalized}/{target_name}")
        content = skill_path.read_text(encoding="utf-8")
        return {
            "slug": normalized,
            "file": target_name,
            "path": str(skill_path),
            "content": content,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }

    async def list_skill_files(self, slug: str) -> list[str]:
        """列出技能目录中的所有文件（相对路径）。"""
        normalized = normalize_skill_slug(slug)
        root = await self.resolve_root()
        skill_dir = (root / normalized).resolve()
        if not skill_dir.is_dir():
            return []
        return sorted(
            str(f.relative_to(skill_dir))
            for f in skill_dir.rglob("*")
            if f.is_file() and not f.name.startswith(".")
        )

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
        skill_dir = await self._ensure_skill_dir(root, slug)
        skill_path = skill_dir / SKILL_FILE_NAME
        skill_path.write_text(content, encoding="utf-8", newline="\n")
        return str(skill_path.resolve())

    async def _ensure_skill_dir(self, root: Path, slug: str) -> Path:
        """创建并返回技能目录，校验安全边界。"""
        skill_dir = (root / normalize_skill_slug(slug)).resolve()
        if root not in skill_dir.parents:
            raise ValueError("Skill 安装目录不在 Skills 根目录内")
        skill_dir.mkdir(parents=True, exist_ok=True)
        return skill_dir

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
        file_count: int = 1,
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
        skill.file_count = file_count
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


def _parse_github_skill_directory(source: GitHubSkillSource) -> GitHubSkillDirectory:
    """从 GitHubSkillSource 提取目录路径信息。"""
    parent = str(Path(source.path).parent)
    return GitHubSkillDirectory(
        owner=source.owner,
        repo=source.repo,
        ref=source.ref,
        dir_path=parent,
        skill_file_path=source.path,
    )