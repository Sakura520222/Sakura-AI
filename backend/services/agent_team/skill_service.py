"""Agent Skills 安装、索引与读取服务。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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

# 中文 Windows 常见 ZIP 文件名编码
_ZIP_FILENAME_ENCODINGS = ("utf-8", "gbk", "gb2312", "big5", "cp932", "cp949")
_ZIP_CONTENT_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "latin-1")


def _resolve_within(parent: Path, child: str | Path) -> Path:
    """解析 child 并确保它位于 parent 内。"""
    parent_resolved = Path(parent).resolve()
    child_resolved = Path(child).resolve()
    try:
        child_resolved.relative_to(parent_resolved)
    except ValueError as exc:
        raise ValueError(f"路径不在允许目录内: {child_resolved}") from exc
    return child_resolved


def _safe_skill_relative_path(path: str | Path) -> Path | None:
    """将 Skill 内部文件路径规范化为安全相对路径。"""
    raw = str(path or "").replace("\\", "/").strip()
    if not raw:
        return None

    candidate = PurePosixPath(raw)
    if candidate.is_absolute():
        return None

    parts: list[str] = []
    candidate_parts = candidate.parts
    for index, part in enumerate(candidate_parts):
        if part in {"", "."}:
            continue
        if part == ".." or ":" in part or "\x00" in part:
            return None
        if part.startswith((".", "__")):
            # 拒绝隐藏文件和双下划线私有文件，避免安装敏感/缓存文件；
            # 但允许 Python 包 Skill 必需的 __init__.py。
            if part != "__init__.py" or index != len(candidate_parts) - 1:
                return None
        parts.append(part)

    if not parts:
        return None
    return Path(*parts)


def _decode_zip_filename(raw_name: str) -> str:
    """修复 ZIP 文件名编码问题。

    Python zipfile 默认使用 CP437 解码文件名，而许多中文 Windows 工具（资源管理器、
    7-Zip 等）用 GBK 编码存储文件名且不设置 UTF-8 标志。此函数尝试检测并修正编码。
    """
    if raw_name.isascii():
        return raw_name
    try:
        raw_bytes = raw_name.encode("cp437")
    except UnicodeEncodeError:
        return raw_name
    for enc in _ZIP_FILENAME_ENCODINGS:
        try:
            decoded = raw_bytes.decode(enc)
            if decoded != raw_name:
                return decoded
        except UnicodeDecodeError, UnicodeEncodeError:
            continue
    return raw_name


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
        return GitHubSkillSource(
            owner=owner, repo=repo, ref=ref, path=path, raw_url=raw_url
        )

    if host == "raw.githubusercontent.com":
        if len(parts) < 4:
            raise ValueError("GitHub raw 链接格式不正确")
        owner, repo = parts[0], parts[1]
        ref, path = _split_github_ref_and_path(parts[2:])
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
        return GitHubSkillSource(
            owner=owner, repo=repo, ref=ref, path=path, raw_url=raw_url
        )

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


def _list_safe_skill_files(skill_dir: Path) -> list[str]:
    """列出 Skill 目录内文件，忽略隐藏文件和越界符号链接。"""
    base = skill_dir.resolve()
    files: list[str] = []
    for file_path in base.rglob("*"):
        if not file_path.is_file() or file_path.name.startswith("."):
            continue
        try:
            resolved = _resolve_within(base, file_path)
        except ValueError:
            continue
        files.append(resolved.relative_to(base).as_posix())
    return sorted(files)


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
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
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
            allowed_tools=metadata.get("allowed_tools", ""),
            arguments=metadata.get("arguments", ""),
            requires=metadata.get("requires", ""),
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
            raise ValueError(
                f"ZIP 文件不能超过 {MAX_SKILL_DIR_BYTES // (1024 * 1024)}MB"
            )

        extracted: dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            # 检测并剥离 ZIP 的顶层目录（如 GitHub 下载的 <repo>-<ref>/ 前缀）
            # ZIP 内路径始终使用正斜杠，不使用 os.path / pathlib
            names = [
                _decode_zip_filename(n) for n in zf.namelist() if not n.endswith("/")
            ]
            strip_prefix = ""
            if names:
                top_dirs = set()
                for n in names:
                    parts = n.split("/")
                    if len(parts) > 1:
                        top_dirs.add(parts[0])
                # 所有文件共享同一个顶层目录时，剥离该前缀
                if len(top_dirs) == 1:
                    strip_prefix = top_dirs.pop() + "/"

            for info in zf.infolist():
                if info.is_dir():
                    continue
                # 修复中文文件名编码
                rel = _decode_zip_filename(info.filename)
                rel_for_prefix = rel.replace("\\", "/")
                if strip_prefix and rel_for_prefix.startswith(strip_prefix):
                    rel = rel_for_prefix[len(strip_prefix) :]
                if not rel:
                    continue
                safe_rel = _safe_skill_relative_path(rel)
                if safe_rel is None:
                    continue
                basename = safe_rel.name
                raw_bytes = zf.read(info)
                if len(raw_bytes) > MAX_SKILL_BYTES:
                    raise ValueError(
                        f"文件 {basename} 超过 {MAX_SKILL_BYTES // 1024}KB"
                    )
                # 文件内容尝试多种编码
                file_text: str | None = None
                for enc in _ZIP_CONTENT_ENCODINGS:
                    try:
                        file_text = raw_bytes.decode(enc).replace("\r\n", "\n")
                        break
                    except UnicodeDecodeError:
                        continue
                if file_text is None:
                    file_text = raw_bytes.decode("utf-8", errors="replace").replace(
                        "\r\n", "\n"
                    )
                extracted[safe_rel.as_posix()] = file_text

        skill_text = extracted.get(SKILL_FILE_NAME)
        if not skill_text or not skill_text.strip():
            # 也尝试在根目录下的子目录中查找 SKILL.md
            for k, v in extracted.items():
                if Path(k).name == SKILL_FILE_NAME and not skill_text:
                    skill_text = v
            if not skill_text or not skill_text.strip():
                raise ValueError("ZIP 中缺少 SKILL.md 或内容为空")

        metadata = self._extract_metadata(skill_text)
        skill_name = name.strip() or metadata.get("name") or "Uploaded Skill"
        slug = normalize_skill_slug(metadata.get("slug") or skill_name)
        root = await self.resolve_root()
        skill_dir = await self._ensure_skill_dir(root, slug)

        for rel_path, fcontent in extracted.items():
            safe_rel = _safe_skill_relative_path(rel_path)
            if safe_rel is None:
                continue
            target = _resolve_within(skill_dir, skill_dir / safe_rel)
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(
                target.write_text, fcontent, encoding="utf-8", newline="\n"
            )

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
            allowed_tools=metadata.get("allowed_tools", ""),
            arguments=metadata.get("arguments", ""),
            requires=metadata.get("requires", ""),
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
            safe_rel = _safe_skill_relative_path(fname)
            if safe_rel is None:
                continue
            if fname.lower() == SKILL_FILE_NAME.lower():
                decoded = text
            else:
                try:
                    decoded = fcontent.decode("utf-8-sig").replace("\r\n", "\n")
                except UnicodeDecodeError:
                    continue
            target = _resolve_within(skill_dir, skill_dir / safe_rel)
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            if isinstance(decoded, str):
                await asyncio.to_thread(
                    target.write_text, decoded, encoding="utf-8", newline="\n"
                )
            else:
                await asyncio.to_thread(target.write_bytes, fcontent)

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
            allowed_tools=metadata.get("allowed_tools", ""),
            arguments=metadata.get("arguments", ""),
            requires=metadata.get("requires", ""),
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
            if name.startswith((".", "__")):
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
        result = await db.execute(
            select(AgentSkill).order_by(AgentSkill.updated_at.desc())
        )
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
        root = await self.resolve_root()
        try:
            skill_dir = _resolve_within(root, Path(skill.install_path).parent)
        except ValueError as exc:
            raise ValueError("Skill 安装路径不在 Skills 根目录内") from exc
        if skill_dir == root:
            raise ValueError("Skill 安装路径不在 Skills 根目录内")

        await db.delete(skill)
        await db.commit()
        await asyncio.to_thread(shutil.rmtree, skill_dir, ignore_errors=True)
        logger.info("Agent Skill 已删除: slug={}, path={}", skill.slug, skill_dir)
        return skill

    async def ensure_builtin_skills(self, db: AsyncSession) -> int:
        """注册内置技能（已存在则跳过），返回新安装数量。"""
        from backend.services.agent_team.builtin_skills import install_builtin_skills

        return await install_builtin_skills(db, self)

    async def build_enabled_skills_summary(self, db: AsyncSession) -> str:
        """构建注入 Agent Prompt 的已启用 Skills 摘要。"""
        skills = await self._enabled_skills(db)
        if not skills:
            return ""

        lines = [
            "## 可用 Skills",
            "需要使用某个 Skill 时，调用 `use_skill` 并传入 skill slug 读取完整内容。",
            "Skill 可声明需要使用的工具（allowed_tools）、参数（arguments）和前置条件（requires）。",
            "读取 Skill 后，按照其指导使用对应的工具执行操作。",
        ]
        for skill in skills:
            lines.append(f"- `{skill.slug}`: {skill.name}")
            if skill.description:
                lines.append(f"  - description: {skill.description.strip()[:500]}")
            if skill.when_to_use:
                lines.append(f"  - when_to_use: {skill.when_to_use.strip()[:500]}")
            # 展示动作能力信息
            if skill.arguments:
                try:
                    arg_names = json.loads(skill.arguments)
                    if arg_names:
                        lines.append(
                            f"  - arguments: {', '.join(str(a) for a in arg_names)}"
                        )
                except ValueError, TypeError:
                    pass
            if skill.allowed_tools:
                try:
                    tools = json.loads(skill.allowed_tools)
                    if tools:
                        lines.append(f"  - tools: {', '.join(str(t) for t in tools)}")
                except ValueError, TypeError:
                    pass
            if skill.requires:
                lines.append(f"  - requires: {skill.requires.strip()[:200]}")
        return "\n".join(lines)

    async def snapshot_enabled_skills(
        self, db: AsyncSession
    ) -> list[dict[str, str | int]]:
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
                "allowed_tools": skill.allowed_tools or "",
                "arguments": skill.arguments or "",
                "requires": skill.requires or "",
            }
            for skill in skills
        ]

    async def load_skill_content(self, slug: str, file: str = "") -> dict[str, str]:
        """按 slug 读取本地技能文件内容，默认读取 SKILL.md。"""
        normalized = normalize_skill_slug(slug)
        root = await self.resolve_root()
        target_name = file.strip() if file else SKILL_FILE_NAME
        try:
            skill_dir = _resolve_within(root, root / normalized)
        except ValueError:
            raise ValueError("Skill 路径不在 Skills 根目录内")
        safe_target = _safe_skill_relative_path(target_name)
        if safe_target is None:
            raise ValueError("文件路径不在 Skill 目录内")
        try:
            skill_path = _resolve_within(skill_dir, skill_dir / safe_target)
        except ValueError:
            raise ValueError("文件路径不在 Skill 目录内")
        if not skill_path.is_file():
            raise FileNotFoundError(f"文件不存在: {normalized}/{target_name}")
        content = await asyncio.to_thread(skill_path.read_text, encoding="utf-8")
        return {
            "slug": normalized,
            "file": safe_target.as_posix(),
            "path": str(skill_path),
            "content": content,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }

    async def list_skill_files(self, slug: str) -> list[str]:
        """列出技能目录中的所有文件（相对路径）。"""
        normalized = normalize_skill_slug(slug)
        root = await self.resolve_root()
        try:
            skill_dir = _resolve_within(root, root / normalized)
        except ValueError:
            return []
        if not skill_dir.is_dir():
            return []
        return await asyncio.to_thread(_list_safe_skill_files, skill_dir)

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
        await asyncio.to_thread(
            skill_path.write_text, content, encoding="utf-8", newline="\n"
        )
        return str(skill_path.resolve())

    async def _ensure_skill_dir(self, root: Path, slug: str) -> Path:
        """创建并返回技能目录，校验安全边界。"""
        try:
            skill_dir = _resolve_within(root, root / normalize_skill_slug(slug))
        except ValueError:
            raise ValueError("Skill 安装目录不在 Skills 根目录内")
        await asyncio.to_thread(skill_dir.mkdir, parents=True, exist_ok=True)
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
        allowed_tools: str = "",
        arguments: str = "",
        requires: str = "",
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
        skill.allowed_tools = allowed_tools or None
        skill.arguments = arguments or None
        skill.requires = requires or None
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

    # 需要从 frontmatter 提取的标量字段
    _META_STRING_KEYS = (
        "name",
        "slug",
        "description",
        "when_to_use",
        "version",
        "requires",
    )

    # 需要从 frontmatter 提取的列表字段（存为 JSON 字符串）
    _META_LIST_KEYS = ("allowed-tools", "allowed_tools", "arguments", "argument-hint")

    def _extract_metadata(self, content: str) -> dict[str, str]:
        frontmatter = self._extract_frontmatter(content)
        metadata: dict[str, str] = {}
        if frontmatter:
            try:
                parsed = yaml.safe_load(frontmatter) or {}
                if isinstance(parsed, dict):
                    for key in self._META_STRING_KEYS:
                        value = parsed.get(key)
                        if value is not None:
                            metadata[key] = str(value).strip()
                    # 列表字段序列化为 JSON 字符串存储
                    for key in self._META_LIST_KEYS:
                        value = parsed.get(key)
                        if value is not None:
                            if isinstance(value, list):
                                metadata[key] = json.dumps(value, ensure_ascii=False)
                            else:
                                metadata[key] = json.dumps(
                                    [str(value).strip()], ensure_ascii=False
                                )
                    # 标准化：allowed-tools → allowed_tools
                    if "allowed-tools" in metadata and "allowed_tools" not in metadata:
                        metadata["allowed_tools"] = metadata.pop("allowed-tools")
                    else:
                        metadata.pop("allowed-tools", None)
                    if "argument-hint" in metadata:
                        metadata["argument_hint"] = metadata.pop("argument-hint")
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
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.startswith("---\n"):
            return ""
        end = normalized.find("\n---", 4)
        if end <= 0:
            return ""
        return normalized[4:end]

    def _extract_first_heading(self, content: str) -> str:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
        return ""

    def _extract_first_paragraph(self, content: str) -> str:
        for block in content.split("\n\n"):
            text = " ".join(line.strip() for line in block.splitlines() if line.strip())
            if not text or text.startswith(("---", "#")):
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
