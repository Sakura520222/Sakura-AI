"""Read-only GHCR/OCI catalog used by Version Manager.

Only the fixed, application-owned repository is queried.  Registry bearer
tokens are kept inside this service and are never included in returned models.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

from backend.core.time_service import format_rfc3339, monotonic, now_utc

REPOSITORY = "ghcr.io/sakura520222/sakura-ai"
_SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
_STABLE_RE = re.compile(rf"^v(?P<version>{_SEMVER})$")
_DEV_RE = re.compile(
    rf"^dev-(?P<timestamp>[0-9]{{14}})-v(?P<version>{_SEMVER})-(?P<revision>[0-9a-f]{{40}})$"
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")


def parse_stable_tag(tag: str) -> dict[str, Any] | None:
    match = _STABLE_RE.fullmatch(tag)
    if not match:
        return None
    return {"channel": "stable", "version": match.group("version"), "tag": tag}


def parse_development_tag(tag: str) -> dict[str, Any] | None:
    match = _DEV_RE.fullmatch(tag)
    if not match:
        return None
    timestamp = match.group("timestamp")
    try:
        created = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return {
        "channel": "development",
        "version": match.group("version"),
        "revision": match.group("revision"),
        "created_at": format_rfc3339(created),
        "tag": tag,
    }


def parse_registry_tag(tag: str) -> dict[str, Any] | None:
    return parse_stable_tag(tag) or parse_development_tag(tag)


def _semver_key(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _group_images(tags_to_digest: dict[str, str]) -> list[dict[str, Any]]:
    """Group aliases by manifest digest and mark only aligned heads selectable."""

    groups: dict[str, set[str]] = defaultdict(set)
    for tag, digest in tags_to_digest.items():
        if _DIGEST_RE.fullmatch(digest):
            groups[digest.lower()].add(tag)
    parsed_groups: list[dict[str, Any]] = []
    latest_digest = tags_to_digest.get("latest", "").lower()
    edge_digest = tags_to_digest.get("edge", "").lower()
    stable_by_digest: dict[str, list[dict[str, Any]]] = defaultdict(list)
    dev_by_digest: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for digest, aliases in groups.items():
        for tag in aliases:
            parsed = parse_registry_tag(tag)
            if parsed is None:
                continue
            (dev_by_digest if parsed["channel"] == "development" else stable_by_digest)[digest].append(parsed)
    stable_head_digest = ""
    if latest_digest and len(stable_by_digest.get(latest_digest, [])) == 1:
        stable_head_digest = latest_digest
    dev_head_digest = ""
    if edge_digest and len(dev_by_digest.get(edge_digest, [])) == 1:
        dev_head_digest = edge_digest
    for digest, aliases in groups.items():
        stable = stable_by_digest.get(digest, [])
        development = dev_by_digest.get(digest, [])
        if stable:
            stable.sort(key=lambda item: _semver_key(item["version"]), reverse=True)
            item = stable[0]
            canonical = item["tag"]
            tags = sorted(aliases)
            parsed_groups.append(
                {
                    "channel": "stable",
                    "version": item["version"],
                    "revision": None,
                    "created_at": None,
                    "digest": digest,
                    "tags": tags,
                    "canonical_tag": canonical,
                    "is_channel_head": digest == stable_head_digest,
                    "selectable": digest == stable_head_digest,
                    "legacy_reason": None if digest == stable_head_digest else "not_channel_head",
                }
            )
        elif development:
            development.sort(key=lambda item: item["created_at"], reverse=True)
            item = development[0]
            parsed_groups.append(
                {
                    "channel": "development",
                    "version": item["version"],
                    "revision": item["revision"],
                    "created_at": item["created_at"],
                    "digest": digest,
                    "tags": sorted(aliases),
                    "canonical_tag": item["tag"],
                    "is_channel_head": digest == dev_head_digest,
                    "selectable": digest == dev_head_digest,
                    "legacy_reason": None if digest == dev_head_digest else "not_channel_head",
                }
            )
        else:
            parsed_groups.append(
                {
                    "channel": "unknown",
                    "version": None,
                    "revision": None,
                    "created_at": None,
                    "digest": digest,
                    "tags": sorted(aliases),
                    "canonical_tag": min(aliases),
                    "is_channel_head": False,
                    "selectable": False,
                    "legacy_reason": "invalid_tag",
                }
            )
    def sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
        version = _semver_key(item["version"]) if item.get("version") else (0, 0, 0)
        return (
            not item["is_channel_head"],
            item["channel"] != "stable",
            -version[0],
            -version[1],
            -version[2],
            -int((item.get("created_at") or "19700101000000").replace("-", "").replace(":", "").replace("T", "").replace("Z", "").replace("+00", "")[:14])
            if item.get("channel") == "development" and item.get("created_at")
            else 0,
        )

    parsed_groups.sort(key=sort_key)
    return parsed_groups


class ContainerRegistryError(RuntimeError):
    """GHCR request or response failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class ContainerRegistryClient:
    def __init__(self, repository: str | None = None, *, ttl: float = 30.0, timeout: float = 10.0):
        # The registry is a trust-boundary constant.  Do not allow deployment
        # configuration or request data to redirect catalog reads elsewhere.
        if repository is not None and repository != REPOSITORY:
            raise ValueError("registry repository is fixed")
        self.repository = REPOSITORY
        self.ttl = ttl
        self.timeout = timeout
        self._cache: dict[str, Any] | None = None
        self._cache_at = 0.0

    def _request_json(self, url: str, headers: dict[str, str] | None = None) -> tuple[Any, dict[str, str]]:
        request = Request(url, headers={"Accept": "application/json", **(headers or {})})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return payload, {str(k).lower(): str(v) for k, v in response.headers.items()}
        except HTTPError as exc:
            raise ContainerRegistryError(
                "registry request failed", status_code=int(exc.code)
            ) from exc
        except (URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            raise ContainerRegistryError("registry request failed") from exc

    async def _token(self) -> str:
        registry, path = self.repository.split("/", 1)
        payload, _ = await asyncio.to_thread(self._request_json, f"https://{registry}/token?scope=repository:{path}:pull")
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise ContainerRegistryError("registry token response invalid")
        return token

    async def _tags(self, token: str) -> list[str]:
        registry, path = self.repository.split("/", 1)
        url = f"https://{registry}/v2/{path}/tags/list?n=100"
        result: list[str] = []
        for _ in range(100):
            payload, headers = await asyncio.to_thread(self._request_json, url, {"Authorization": f"Bearer {token}"})
            if not isinstance(payload, dict) or not isinstance(payload.get("tags"), list):
                raise ContainerRegistryError("registry tags response invalid")
            result.extend(tag for tag in payload["tags"] if isinstance(tag, str))
            link = headers.get("link")
            if not link:
                break
            match = re.search(r"<([^>]+)>;\s*rel=\"next\"", link)
            if match:
                url = urljoin(url, match.group(1))
                continue
            query = parse_qs(urlparse(url).query)
            last = query.get("last", [None])[0]
            if last is None:
                break
            url = f"{url.split('?', 1)[0]}?n=100&last={last}"
        return sorted(set(result))

    async def _manifest_digest(self, token: str, tag: str) -> str | None:
        registry, path = self.repository.split("/", 1)
        url = f"https://{registry}/v2/{path}/manifests/{tag}"
        try:
            _payload, headers = await asyncio.to_thread(
                self._request_json,
                url,
                {"Authorization": f"Bearer {token}", "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"},
            )
        except ContainerRegistryError as exc:
            # A tag disappearing between tags/list and manifest lookup is a
            # normal registry race. Every other error must fail the refresh so
            # a partial directory cannot be presented as fresh/selectable.
            if exc.status_code == 404:
                return None
            raise
        digest = headers.get("docker-content-digest")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise ContainerRegistryError("registry manifest digest header missing")
        return digest.lower()

    async def list_images(self, *, force_refresh: bool = False) -> dict[str, Any]:
        now = monotonic()
        if (
            not force_refresh
            and self._cache is not None
            and now - self._cache_at < self.ttl
        ):
            return self._cache
        try:
            token = await self._token()
            tags = await self._tags(token)
            pairs = await asyncio.gather(*(self._manifest_digest(token, tag) for tag in tags))
            tag_digests = {tag: digest for tag, digest in zip(tags, pairs) if digest}
            payload = {
                "repository": self.repository,
                "fetched_at": format_rfc3339(now_utc()),
                "stale": False,
                "images": _group_images(tag_digests),
            }
            payload["heads"] = {
                channel: next(({
                    "digest": item["digest"], "canonical_tag": item["canonical_tag"],
                    "tag": item["canonical_tag"],
                    "version": item["version"], "revision": item.get("revision"),
                } for item in payload["images"] if item["channel"] == channel and item["is_channel_head"]), None)
                for channel in ("stable", "development")
            }
            self._cache = payload
            self._cache_at = now
            return payload
        except ContainerRegistryError:
            if self._cache is None:
                raise
            stale = dict(self._cache)
            stale["stale"] = True
            return stale


__all__ = [
    "REPOSITORY",
    "ContainerRegistryClient",
    "ContainerRegistryError",
    "_group_images",
    "parse_development_tag",
    "parse_registry_tag",
    "parse_stable_tag",
]
