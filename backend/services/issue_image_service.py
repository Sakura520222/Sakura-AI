"""Issue 图片多模态服务 / Issue image multimodal service.

从 Issue 正文与评论 Markdown 中提取图片引用，经域名白名单与大小限制
校验后，用 GitHub App installation 凭据下载并转 base64，供
``UnifiedMessage.images`` 多模态输入使用（Issue #538）。

Extract image references from issue/comment markdown, download them with
installation credentials under a domain allowlist and size limits, and
return base64 payloads for multimodal ``UnifiedMessage.images``.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import io
import ipaddress
import re
import warnings
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger
from PIL import Image, ImageOps, UnidentifiedImageError

from backend.core.config import get_settings

# 重定向跳数是 SSRF 防护边界（每跳都重新校验白名单），非业务可调项；
# GitHub 资产 URL 最多 1-2 跳即可到达签名地址。
_MAX_IMAGE_REDIRECTS = 3
_DOWNLOAD_TIMEOUT_SECONDS = 30.0
_IMAGE_COMPRESSION_THRESHOLD_BYTES = 5 * 1024 * 1024
_IMAGE_OUTPUT_TARGET_BYTES = 500 * 1024
_IMAGE_OUTPUT_FALLBACK_MAX_BYTES = 5 * 1024 * 1024
_MAX_IMAGE_COUNT = 20
_MAX_TOTAL_ENCODED_IMAGE_BYTES = 15 * 1024 * 1024
_SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)
_IMAGE_FORMAT_MEDIA_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}

_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_HTML_IMAGE_RE = re.compile(r"<img[^>]+src=[\"'](https?://[^\"']+)[\"']", re.IGNORECASE)


def extract_image_references(text: str | None) -> list[str]:
    """提取 Markdown / HTML 图片引用（去重保序）/ Extract image refs in order.

    仅识别显式图片语法；裸链接保持在正文中以文本形式可见，不在此处理。
    """
    if not text:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    matches = sorted(
        (*_MARKDOWN_IMAGE_RE.finditer(text), *_HTML_IMAGE_RE.finditer(text)),
        key=lambda match: match.start(),
    )
    for match in matches:
        url = match.group(1).strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _allowed_domain_entries() -> list[str]:
    return [
        entry.strip().lower()
        for entry in get_settings().issue_vision_allowed_image_domains.split(",")
        if entry.strip()
    ]


def _host_matches_pattern(host: str, pattern: str) -> bool:
    """段级匹配：``*`` 只在段内展开（不跨 ``.``），如 github-production-user-asset-*.s3.amazonaws.com。"""
    host_parts = host.split(".")
    pattern_parts = pattern.split(".")
    if len(host_parts) != len(pattern_parts):
        return False
    for part, pat in zip(host_parts, pattern_parts):
        if part == pat:
            continue
        if "*" not in pat and "?" not in pat:
            return False
        if not fnmatch.fnmatchcase(part, pat):
            return False
    return True


def _host_allowed(host: str, path: str, entries: list[str]) -> bool:
    """白名单匹配：条目可为裸域、``host/path-prefix`` 或含 ``*`` 通配的模式。"""
    host = host.lower()
    path = path if path.startswith("/") else f"/{path}"
    for entry in entries:
        if "/" in entry:
            entry_host, _, entry_prefix = entry.partition("/")
            entry_prefix = "/" + entry_prefix.strip("/")
            if (
                (entry_host == host or _host_matches_pattern(host, entry_host))
                and (
                    path == entry_prefix or path.startswith(f"{entry_prefix}/")
                )
            ):
                return True
        elif entry == host or _host_matches_pattern(host, entry):
            return True
    return False


def _validate_image_url(url: str, entries: list[str]) -> str | None:
    """校验图片 URL：https(s) + 白名单域 + 拒绝 IP 字面量，返回原始 URL 或 None。"""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return None  # IP 字面量直接拒绝，白名单只允许域名形态
    except ValueError:
        pass
    if not _host_allowed(host, parsed.path or "/", entries):
        return None
    return url


def _normalize_image_media_type(value: str) -> str | None:
    media_type = value.split(";", 1)[0].strip().lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    if media_type not in _SUPPORTED_IMAGE_MEDIA_TYPES:
        return None
    return media_type


def _verified_image_media_type(payload: bytes) -> str | None:
    """Verify bytes with Pillow instead of trusting Content-Type alone."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                media_type = _IMAGE_FORMAT_MEDIA_TYPES.get(image.format or "")
                image.verify()
        return media_type
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        UnidentifiedImageError,
        ValueError,
    ):
        return None


def _encode_webp(image: Image.Image, quality: int) -> bytes:
    output = io.BytesIO()
    image.save(output, format="WEBP", quality=quality, method=6)
    return output.getvalue()


def _compress_image_payload(payload: bytes) -> bytes | None:
    """Downscale and encode as WebP, preferring 500 KiB but allowing 5 MiB."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as source:
                if (source.format or "") not in _IMAGE_FORMAT_MEDIA_TYPES:
                    return None
                source.seek(0)
                oriented = ImageOps.exif_transpose(source)
                oriented.load()
                has_alpha = oriented.mode in {"RGBA", "LA"} or (
                    "transparency" in oriented.info
                )
                current = oriented.convert("RGBA" if has_alpha else "RGB")

        best: bytes | None = None
        for attempt in range(8):
            quality = max(40, 86 - attempt * 6)
            encoded = _encode_webp(current, quality)
            if best is None or len(encoded) < len(best):
                best = encoded
            if len(encoded) <= _IMAGE_OUTPUT_TARGET_BYTES:
                return encoded

            ratio = (_IMAGE_OUTPUT_TARGET_BYTES / len(encoded)) ** 0.5
            scale = max(0.45, min(0.85, ratio * 0.95))
            new_size = (
                max(1, int(current.width * scale)),
                max(1, int(current.height * scale)),
            )
            if new_size == current.size:
                break
            current = current.resize(new_size, Image.Resampling.LANCZOS)

        if best is not None and len(best) <= _IMAGE_OUTPUT_FALLBACK_MAX_BYTES:
            return best
        return None
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        UnidentifiedImageError,
        ValueError,
    ):
        return None


def _prepare_image_payload(
    payload: bytes, declared_media_type: str
) -> tuple[bytes, str] | None:
    declared = _normalize_image_media_type(declared_media_type)
    if declared is None:
        return None
    if len(payload) > _IMAGE_COMPRESSION_THRESHOLD_BYTES:
        compressed = _compress_image_payload(payload)
        if compressed is None:
            return None
        return compressed, "image/webp"

    verified = _verified_image_media_type(payload)
    if verified is None:
        return None
    return payload, verified


async def _get_installation_token(
    github_app: Any,
    installation_id: Any,
    *,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> str | None:
    """获取 installation 访问令牌（PyGithub 同步调用 → to_thread）。"""
    if github_app is None:
        return None
    integration = getattr(github_app, "integration", None)
    if integration is None:
        return None
    try:
        resolved_installation_id = installation_id
        if not resolved_installation_id and repo_owner and repo_name:
            installation = await asyncio.to_thread(
                integration.get_installation,
                owner=repo_owner,
                repo=repo_name,
            )
            resolved_installation_id = getattr(installation, "id", None)
        if not resolved_installation_id:
            return None
        auth_token = await asyncio.to_thread(
            integration.get_access_token, int(resolved_installation_id)
        )
        return getattr(auth_token, "token", None)
    except Exception as exc:
        logger.warning("获取 installation 令牌失败，尝试匿名下载图片: {}", exc)
        return None


async def _download_image(
    client: httpx.AsyncClient,
    url: str,
    entries: list[str],
    *,
    token: str | None,
    max_size: int,
) -> dict[str, Any] | None:
    """下载单张图片（手动跟随重定向并逐跳校验白名单）。

    Returns ``{"url", "media_type", "data"(base64)}`` 或 None（失败/超限跳过）。
    """
    current = url
    auth_headers = {"Accept": "application/octet-stream"}
    if token:
        auth_headers["Authorization"] = f"Bearer {token}"
    # 鉴权头只发首跳：重定向目标（S3 签名 URL 等）自带 query 鉴权，
    # 再带 Authorization 会被目标拒绝
    first_hop = True
    for _ in range(_MAX_IMAGE_REDIRECTS + 1):
        if _validate_image_url(current, entries) is None:
            logger.warning("图片重定向目标不在白名单内，跳过: {}", current)
            return None
        try:
            async with client.stream(
                "GET",
                current,
                headers=auth_headers if first_hop else {"Accept": "*/*"},
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    first_hop = False
                    continue
                if response.status_code != 200:
                    logger.warning(
                        "图片下载返回非成功状态（跳过）: {} status={}",
                        current,
                        response.status_code,
                    )
                    return None

                declared_media_type = response.headers.get("content-type") or ""
                if _normalize_image_media_type(declared_media_type) is None:
                    logger.warning(
                        "图片格式不受多协议支持（跳过）: {} type={}",
                        current,
                        declared_media_type,
                    )
                    return None

                content_length = response.headers.get("content-length")
                if (
                    content_length
                    and content_length.isdigit()
                    and int(content_length) > max_size
                ):
                    logger.warning(
                        "图片超过下载硬上限（跳过）: {} bytes>{}",
                        current,
                        content_length,
                    )
                    return None

                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(buffer) + len(chunk) > max_size:
                        logger.warning(
                            "图片流超过下载硬上限（中止）: {} bytes>{}",
                            current,
                            len(buffer) + len(chunk),
                            max_size,
                        )
                        return None
                    buffer.extend(chunk)
        except httpx.HTTPError as exc:
            logger.warning("图片下载失败（跳过）: {} err={}", current, exc)
            return None

        payload = bytes(buffer)
        prepared = await asyncio.to_thread(
            _prepare_image_payload,
            payload,
            declared_media_type,
        )
        if prepared is None:
            logger.warning("图片解码、格式验证或压缩失败（跳过）: {}", current)
            return None
        prepared_payload, media_type = prepared
        if len(payload) > _IMAGE_COMPRESSION_THRESHOLD_BYTES:
            logger.info(
                "Issue 图片已压缩: {} {} bytes -> {} bytes ({})",
                url,
                len(payload),
                len(prepared_payload),
                media_type,
            )
        return {
            "url": url,
            "media_type": media_type,
            "data": base64.b64encode(prepared_payload).decode("ascii"),
        }
    logger.warning("图片重定向次数超限（跳过）: {}", url)
    return None


async def collect_issue_images(
    urls: list[str],
    *,
    github_app: Any = None,
    installation_id: Any = None,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> list[dict[str, Any]]:
    """按配置下载图片列表，返回多模态 images 载荷（失败项自动跳过）。"""
    settings = get_settings()
    entries = _allowed_domain_entries()
    if not entries:
        return []
    valid_urls: list[str] = []
    for url in urls:
        if _validate_image_url(url, entries) is not None:
            valid_urls.append(url)
    if not valid_urls:
        return []

    token = await _get_installation_token(
        github_app,
        installation_id,
        repo_owner=repo_owner,
        repo_name=repo_name,
    )
    images: list[dict[str, Any]] = []
    total_encoded_bytes = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_DOWNLOAD_TIMEOUT_SECONDS, connect=10.0),
        follow_redirects=False,
    ) as client:
        for url in valid_urls[:_MAX_IMAGE_COUNT]:
            image = await _download_image(
                client,
                url,
                entries,
                token=token,
                max_size=settings.issue_vision_max_image_size_bytes,
            )
            if image is not None:
                encoded_bytes = len(image.get("data", ""))
                if (
                    total_encoded_bytes + encoded_bytes
                    > _MAX_TOTAL_ENCODED_IMAGE_BYTES
                ):
                    logger.warning(
                        "Issue 图片总编码体积达到上限，停止收集: {} bytes>{}",
                        total_encoded_bytes + encoded_bytes,
                        _MAX_TOTAL_ENCODED_IMAGE_BYTES,
                    )
                    break
                images.append(image)
                total_encoded_bytes += encoded_bytes
    if len(valid_urls) > _MAX_IMAGE_COUNT:
        logger.warning(
            "Issue 图片数量超过上限，仅处理前 {} 张: total={}",
            _MAX_IMAGE_COUNT,
            len(valid_urls),
        )
    if images:
        logger.info(
            "Issue 图片下载完成: {}/{} 张（多模态输入）",
            len(images),
            len(valid_urls),
        )
    return images


def strip_image_payloads_for_display(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """推送/SSE/持久化边界脱敏：剥离 base64 载荷，保留 url 与 media_type。"""
    sanitized: list[dict[str, Any]] = []
    for message in messages:
        images = message.get("images")
        if not images:
            sanitized.append(message)
            continue
        stripped = dict(message)
        stripped["images"] = [
            {
                key: value
                for key, value in (
                    ("url", image.get("url")),
                    ("media_type", image.get("media_type")),
                )
                if value
            }
            or {"url": None}
            for image in images
            if isinstance(image, dict)
        ]
        sanitized.append(stripped)
    return sanitized
