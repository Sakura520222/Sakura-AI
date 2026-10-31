"""Version-bound WebUI artifacts, downloaded without a Node.js runtime.

Builds are immutable directories. A small atomic pointer selects the cached build;
an interrupted download never replaces the currently installed distribution.
This module uses only the standard library so release packaging can reuse it.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from backend import __version__

ROOT = Path(__file__).resolve().parents[2]
BUILD_METADATA = "webui-build.json"
SOURCE_INPUTS = (
    "src",
    "public",
    "scripts",
    "index.html",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "vite.config.ts",
    "openapi.json",
)
TEXT_EXTENSIONS = {
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".css",
    ".html",
    ".json",
    ".svg",
    ".yaml",
    ".yml",
    ".md",
}
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 200 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_FILES = 10_000
DOWNLOAD_SECONDS = 90
RETRY_SECONDS = 30
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_CACHE_KEY = re.compile(r"^[0-9a-f]{64}(?:-[0-9a-f]{32})?$")
_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
}
_thread_lock = threading.Lock()
_active: Path | None = None
_retry_at = 0.0
_last_error = "not_ready"


class WebUIDistributionError(RuntimeError):
    """Only safe error codes cross the runtime/logging boundary."""


@dataclass(frozen=True)
class BuildIdentity:
    version: str
    revision: str | None
    source_hash: str | None


def _linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def source_hash(web: Path) -> str | None:
    if not (web / "src").is_dir():
        return None
    paths: list[Path] = []
    for name in SOURCE_INPUTS:
        path = web / name
        if not path.exists():
            continue
        if _linked(path):
            raise WebUIDistributionError("linked_frontend_source")
        if path.is_dir():
            for entry in path.rglob("*"):
                if _linked(entry):
                    raise WebUIDistributionError("linked_frontend_source")
                if entry.is_file():
                    paths.append(entry)
        else:
            paths.append(path)
    entries = []
    for path in paths:
        content = path.read_bytes()
        if path.suffix in TEXT_EXTENSIONS:
            content = content.decode("utf-8").replace("\r\n", "\n").encode("utf-8")
        entries.append((path.relative_to(web).as_posix(), sha256(content).hexdigest()))
    entries.sort(key=lambda item: item[0].encode("utf-8"))
    return sha256(
        "".join(f"{name}\0{digest}\n" for name, digest in entries).encode()
    ).hexdigest()


def build_identity(root: Path = ROOT) -> BuildIdentity:
    revision = os.environ.get("SAKURA_BUILD_REVISION", "")
    if not _REVISION.fullmatch(revision) and (root / ".git").exists():
        try:
            revision = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL,
                timeout=5,
                text=True,
            ).strip()
        except OSError, subprocess.SubprocessError:
            revision = ""
    return BuildIdentity(
        __version__,
        revision if _REVISION.fullmatch(revision) else None,
        source_hash(root / "web"),
    )


@lru_cache(maxsize=1)
def runtime_identity() -> BuildIdentity:
    # Backend code identity is frozen for this process, including during reloads.
    return build_identity()


def _json(data: bytes) -> dict:
    if len(data) > MAX_MANIFEST_BYTES:
        raise WebUIDistributionError("manifest_too_large")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise WebUIDistributionError("invalid_manifest") from exc
    if not isinstance(value, dict):
        raise WebUIDistributionError("invalid_manifest")
    return value


def _name(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise WebUIDistributionError("invalid_asset_path")
    parts = value.split("/")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if (
        PurePosixPath(value).is_absolute()
        or any(char in value for char in "\\:\x00")
        or any(ord(char) < 32 for char in value)
    ):
        raise WebUIDistributionError("invalid_asset_path")
    if any(
        part in {"", ".", ".."}
        or part.rstrip(" .") != part
        or part.split(".")[0].upper() in reserved
        for part in parts
    ):
        raise WebUIDistributionError("invalid_asset_path")
    if value not in {
        "index.html",
        BUILD_METADATA,
        ".vite/manifest.json",
    } and not value.startswith("assets/"):
        raise WebUIDistributionError("unexpected_asset_path")
    return value


def _identity_matches(metadata: dict, identity: BuildIdentity) -> bool:
    if (
        type(metadata.get("schema_version")) is not int
        or metadata.get("schema_version") != 1
        or metadata.get("version") != identity.version
        or not _DIGEST.fullmatch(str(metadata.get("source_hash", "")))
    ):
        return False
    if identity.source_hash:
        # Backend-only commits can reuse identical frontend inputs. CRLF is
        # normalized by both producers so Windows checkouts match Linux CI.
        return metadata["source_hash"] == identity.source_hash
    return not identity.revision or metadata.get("revision") == identity.revision


def _inventory(metadata: dict) -> dict:
    files = metadata.get("files")
    if (
        not isinstance(files, dict)
        or not 1 < len(files) <= MAX_FILES
        or "index.html" not in files
    ):
        raise WebUIDistributionError("invalid_file_inventory")
    seen: set[str] = set()
    total = 0
    for name, entry in files.items():
        _name(name)
        if (
            name == BUILD_METADATA
            or name.casefold() in seen
            or not isinstance(entry, dict)
        ):
            raise WebUIDistributionError("invalid_file_inventory")
        seen.add(name.casefold())
        size = entry.get("size")
        if (
            type(size) is not int
            or not 0 <= size <= MAX_FILE_BYTES
            or not _DIGEST.fullmatch(str(entry.get("sha256", "")))
        ):
            raise WebUIDistributionError("invalid_file_inventory")
        total += size
    if total > MAX_EXPANDED_BYTES or not any(
        name.startswith("assets/") for name in files
    ):
        raise WebUIDistributionError("invalid_file_inventory")
    if not files["index.html"]["size"]:
        raise WebUIDistributionError("empty_frontend_entry")
    return files


def _read_file(root: Path, name: str, limit: int) -> bytes:
    path = root
    for part in name.split("/"):
        path /= part
        if _linked(path):
            raise WebUIDistributionError("linked_distribution_path")
    if not path.is_file() or path.stat().st_size > limit:
        raise WebUIDistributionError("incomplete_distribution")
    return path.read_bytes()


def validate_distribution(root: Path, identity: BuildIdentity) -> dict:
    if _linked(root) or not root.is_dir():
        raise WebUIDistributionError("invalid_distribution_directory")
    metadata = _json(_read_file(root, BUILD_METADATA, MAX_MANIFEST_BYTES))
    if not _identity_matches(metadata, identity):
        raise WebUIDistributionError("frontend_version_mismatch")
    for name, entry in _inventory(metadata).items():
        data = _read_file(root, name, MAX_FILE_BYTES)
        if len(data) != entry["size"] or sha256(data).hexdigest() != entry["sha256"]:
            raise WebUIDistributionError("frontend_file_checksum_mismatch")
    return metadata


def package_distribution(
    dist: Path, output: Path, identity: BuildIdentity
) -> dict[str, Path]:
    metadata = validate_distribution(dist, identity)
    if not _REVISION.fullmatch(
        str(metadata.get("revision", ""))
    ) or not _VERSION.fullmatch(identity.version):
        raise WebUIDistributionError("release_build_identity_required")
    if identity.revision and metadata["revision"] != identity.revision:
        raise WebUIDistributionError("release_build_revision_mismatch")
    output.mkdir(parents=True, exist_ok=True)
    result = {}
    for kind, archive_name, manifest_name in (
        ("stable", f"sakura-ai-webui-v{identity.version}.zip", "webui-manifest.json"),
        (
            "commit",
            f"sakura-ai-webui-{metadata['revision']}.zip",
            f"webui-{metadata['revision']}.json",
        ),
    ):
        directory = output / kind
        directory.mkdir(exist_ok=True)
        archive = directory / archive_name
        with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=9) as bundle:
            for name in sorted([*metadata["files"], BUILD_METADATA]):
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.compress_type = ZIP_DEFLATED
                bundle.writestr(info, _read_file(dist, name, MAX_FILE_BYTES))
        content = archive.read_bytes()
        if len(content) > MAX_ARCHIVE_BYTES:
            raise WebUIDistributionError("archive_too_large")
        manifest = {
            key: metadata[key]
            for key in ("schema_version", "version", "revision", "source_hash")
        }
        manifest["archive"] = {
            "name": archive_name,
            "size": len(content),
            "sha256": sha256(content).hexdigest(),
        }
        (directory / manifest_name).write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        result[kind] = directory
    return result


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise WebUIDistributionError("untrusted_download_url") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _DOWNLOAD_HOSTS
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise WebUIDistributionError("untrusted_download_url")


def _download(url: str, limit: int, deadline: float) -> bytes:
    _check_url(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WebUIDistributionError("download_timeout")
    request = Request(
        url,
        headers={"User-Agent": "SakuraAI-WebUI", "Accept": "application/octet-stream"},
    )
    try:
        with build_opener(_SafeRedirect()).open(
            request, timeout=min(20, remaining)
        ) as response:
            chunks, size = [], 0
            while chunk := response.read(64 * 1024):
                size += len(chunk)
                if size > limit or time.monotonic() > deadline:
                    raise WebUIDistributionError("download_limit_exceeded")
                chunks.append(chunk)
            return b"".join(chunks)
    except HTTPError as exc:
        raise WebUIDistributionError(
            "artifact_not_published" if exc.code == 404 else "download_failed"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise WebUIDistributionError("download_unavailable") from exc


@contextmanager
def _install_lock(web: Path):
    lock_path = web / ".dist.lock"
    if _linked(lock_path):
        raise WebUIDistributionError("unsafe_install_lock")
    fd = os.open(
        lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WebUIDistributionError("unsafe_install_lock")
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        deadline = time.monotonic() + DOWNLOAD_SECONDS + 30
        while not locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise WebUIDistributionError("install_lock_unavailable") from exc
                if time.monotonic() >= deadline:
                    raise WebUIDistributionError("install_lock_busy") from exc
                time.sleep(0.2)
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _local_distribution(web: Path, identity: BuildIdentity) -> Path | None:
    candidates = [web / "dist"]
    try:
        pointer = _json(_read_file(web, ".dist-current.json", MAX_MANIFEST_BYTES))
        key = pointer.get("directory", "")
        if (
            isinstance(key, str)
            and _CACHE_KEY.fullmatch(key)
            and not _linked(web / ".dist-cache")
        ):
            candidates.append(web / ".dist-cache" / key)
    except OSError, WebUIDistributionError:
        pass
    for candidate in candidates:
        try:
            validate_distribution(candidate, identity)
            return candidate
        except OSError, WebUIDistributionError:
            continue
    return None


def _extract(
    content: bytes, destination: Path, identity: BuildIdentity, manifest: dict
) -> None:
    try:
        with ZipFile(BytesIO(content)) as bundle:
            entries = bundle.infolist()
            if len(entries) > MAX_FILES + 1:
                raise WebUIDistributionError("archive_too_many_files")
            seen: set[str] = set()
            expanded = 0
            for entry in entries:
                _name(entry.filename)
                mode = (entry.external_attr >> 16) & 0xFFFF
                if (
                    entry.filename.casefold() in seen
                    or entry.is_dir()
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                    or entry.flag_bits & 1
                    or entry.file_size > MAX_FILE_BYTES
                ):
                    raise WebUIDistributionError("unsafe_archive_entry")
                seen.add(entry.filename.casefold())
                expanded += entry.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise WebUIDistributionError("archive_too_large")
            metadata = _json(bundle.read(BUILD_METADATA))
            if any(
                metadata.get(key) != manifest[key]
                for key in ("schema_version", "version", "revision", "source_hash")
            ):
                raise WebUIDistributionError("archive_identity_mismatch")
            inventory = _inventory(metadata)
            if {entry.filename for entry in entries} != {*inventory, BUILD_METADATA}:
                raise WebUIDistributionError("archive_inventory_mismatch")
            for entry in entries:
                path = destination / entry.filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bundle.read(entry))
            validate_distribution(destination, identity)
    except WebUIDistributionError:
        raise
    except (
        BadZipFile,
        KeyError,
        ValueError,
        EOFError,
        RuntimeError,
        zlib.error,
    ) as exc:
        raise WebUIDistributionError("invalid_archive") from exc


def ensure_distribution(repo: str, identity: BuildIdentity, root: Path = ROOT) -> Path:
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo
    ) or not _VERSION.fullmatch(identity.version):
        raise WebUIDistributionError("invalid_release_identity")
    web = root / "web"
    if _linked(web):
        raise WebUIDistributionError("unsafe_frontend_directory")
    if local := _local_distribution(web, identity):
        return local
    web.mkdir(parents=True, exist_ok=True)
    with _install_lock(web):
        if local := _local_distribution(web, identity):
            return local
        candidates = []
        if identity.revision:
            candidates.append(
                (
                    f"webui-builds-v{identity.version}",
                    f"webui-{identity.revision}.json",
                    f"sakura-ai-webui-{identity.revision}.zip",
                    True,
                )
            )
        candidates.append(
            (
                f"v{identity.version}",
                "webui-manifest.json",
                f"sakura-ai-webui-v{identity.version}.zip",
                False,
            )
        )
        deadline = time.monotonic() + DOWNLOAD_SECONDS
        for tag, name, archive_name, exact in candidates:
            base = f"https://github.com/{repo}/releases/download/{quote(tag, safe='')}"
            try:
                manifest = _json(
                    _download(f"{base}/{name}", MAX_MANIFEST_BYTES, deadline)
                )
            except WebUIDistributionError as exc:
                if str(exc) == "artifact_not_published":
                    continue
                raise
            if (
                not _identity_matches(manifest, identity)
                or not _REVISION.fullmatch(str(manifest.get("revision", "")))
                or (exact and manifest["revision"] != identity.revision)
            ):
                if exact:
                    raise WebUIDistributionError("release_identity_mismatch")
                continue
            archive = manifest.get("archive")
            if (
                not isinstance(archive, dict)
                or archive.get("name") != archive_name
                or type(archive.get("size")) is not int
                or not 0 < archive["size"] <= MAX_ARCHIVE_BYTES
                or not _DIGEST.fullmatch(str(archive.get("sha256", "")))
            ):
                raise WebUIDistributionError("invalid_archive_manifest")
            content = _download(f"{base}/{archive_name}", archive["size"], deadline)
            if (
                len(content) != archive["size"]
                or sha256(content).hexdigest() != archive["sha256"]
            ):
                raise WebUIDistributionError("archive_checksum_mismatch")
            cache = web / ".dist-cache"
            if _linked(cache):
                raise WebUIDistributionError("unsafe_frontend_cache")
            cache.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=".webui-stage-", dir=web
            ) as temporary:
                stage = Path(temporary) / "dist"
                stage.mkdir()
                _extract(content, stage, identity, manifest)
                key = f"{archive['sha256']}-{uuid4().hex}"
                target = cache / key
                os.replace(stage, target)
                pointer = Path(temporary) / "current.json"
                pointer.write_text(
                    json.dumps({"directory": key}) + "\n", encoding="utf-8"
                )
                os.replace(pointer, web / ".dist-current.json")
            return target
    raise WebUIDistributionError("matching_frontend_not_published")


def _runtime_distribution(repo: str) -> Path:
    global _active, _retry_at, _last_error
    with _thread_lock:
        if _active is not None:
            return _active
        if time.monotonic() < _retry_at:
            raise WebUIDistributionError(_last_error)
        try:
            _active = ensure_distribution(repo, runtime_identity())
            return _active
        except (OSError, UnicodeError, WebUIDistributionError) as exc:
            _last_error = (
                str(exc)
                if isinstance(exc, WebUIDistributionError)
                else "frontend_storage_unavailable"
            )
            _retry_at = time.monotonic() + RETRY_SECONDS
            raise WebUIDistributionError(_last_error) from exc


async def get_distribution(repo: str) -> Path:
    return await asyncio.to_thread(_runtime_distribution, repo)
