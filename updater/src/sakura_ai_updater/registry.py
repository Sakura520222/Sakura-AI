"""Strict development-channel registry target validation.

Stable updates intentionally continue to use ``update-manifest.json`` v1.  A
development target is a separate protocol object and is never converted into
or accepted as a stable manifest.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPOSITORY = "ghcr.io/sakura520222/sakura-ai"
_TAG_RE = re.compile(
    r"^dev-(?P<created>[0-9]{14})-v(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))-(?P<revision>[0-9a-f]{40})$"
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class RegistryTargetError(ValueError):
    """Target is not an exact, current development image identity."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DevelopmentTarget:
    channel: str
    version: str
    revision: str
    tag: str
    digest: str
    repository: str = REPOSITORY

    @property
    def image(self) -> str:
        return f"{self.repository}:{self.tag}@{self.digest}"

    def to_dict(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "version": self.version,
            "revision": self.revision,
            "tag": self.tag,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class StableTarget:
    channel: str
    version: str
    tag: str
    digest: str
    repository: str = REPOSITORY

    @property
    def image(self) -> str:
        return f"{self.repository}:{self.tag}@{self.digest}"

    def to_dict(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "version": self.version,
            "tag": self.tag,
            "digest": self.digest,
        }


def parse_development_target(value: Any) -> DevelopmentTarget:
    if not isinstance(value, dict):
        raise RegistryTargetError("target must be an object")
    if value.get("channel") != "development":
        raise RegistryTargetError("target channel must be development")
    repository = value.get("repository", REPOSITORY)
    if repository != REPOSITORY:
        raise RegistryTargetError("target repository is not trusted")
    tag = value.get("tag")
    digest = value.get("digest")
    version = value.get("version")
    revision = value.get("revision")
    if not all(isinstance(item, str) for item in (tag, digest, version, revision)):
        raise RegistryTargetError("target fields must be strings")
    match = _TAG_RE.fullmatch(tag)
    if match is None:
        raise RegistryTargetError("tag is not an immutable development tag")
    if match.group("version") != version or match.group("revision") != revision:
        raise RegistryTargetError("tag identity does not match version/revision")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RegistryTargetError("revision must be lowercase full SHA")
    if not _DIGEST_RE.fullmatch(digest):
        raise RegistryTargetError("digest must be a sha256 manifest digest")
    return DevelopmentTarget("development", version, revision, tag, digest, repository)


def parse_stable_target(value: Any) -> StableTarget:
    if not isinstance(value, dict):
        raise RegistryTargetError("target must be an object")
    if value.get("channel") != "stable":
        raise RegistryTargetError("target channel must be stable")
    repository = value.get("repository", REPOSITORY)
    if repository != REPOSITORY:
        raise RegistryTargetError("target repository is not trusted")
    version = value.get("version")
    tag = value.get("tag")
    digest = value.get("digest")
    if not all(isinstance(item, str) for item in (version, tag, digest)):
        raise RegistryTargetError("target fields must be strings")
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise RegistryTargetError("stable target version is invalid")
    if tag != f"v{version}":
        raise RegistryTargetError("stable tag does not match version")
    if not _DIGEST_RE.fullmatch(digest):
        raise RegistryTargetError("digest must be a sha256 manifest digest")
    return StableTarget("stable", version, tag, digest, repository)


class RegistryClient:
    """Minimal standard-library GHCR client used by the host trust boundary."""

    def __init__(self, repository: str = REPOSITORY, *, timeout: float = 15.0):
        if repository != REPOSITORY:
            raise ValueError("registry repository is fixed")
        self.repository = repository
        self.timeout = timeout

    def _json_sync(self, url: str, headers: dict[str, str]) -> tuple[Any, dict[str, str]]:
        try:
            with urlopen(Request(url, headers=headers), timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8")), {
                    str(k).lower(): str(v) for k, v in response.headers.items()
                }
        except HTTPError as exc:
            raise RegistryTargetError(
                "registry request failed", status_code=int(exc.code)
            ) from exc
        except (URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            raise RegistryTargetError("registry request failed") from exc

    def _manifest_sync(self, tag: str, token: str) -> str:
        registry, path = self.repository.split("/", 1)
        _, headers = self._json_sync(
            f"https://{registry}/v2/{path}/manifests/{tag}",
            {
                "Authorization": f"Bearer {token}",
                "Accept": (
                    "application/vnd.oci.image.index.v1+json, "
                    "application/vnd.docker.distribution.manifest.list.v2+json"
                ),
            },
        )
        digest = headers.get("docker-content-digest", "").lower()
        if not _DIGEST_RE.fullmatch(digest):
            raise RegistryTargetError("registry did not return a manifest digest")
        return digest

    async def verify_target(self, target: DevelopmentTarget | StableTarget) -> DevelopmentTarget | StableTarget:
        if target.repository != self.repository:
            raise RegistryTargetError("target repository mismatch")
        registry, path = self.repository.split("/", 1)
        payload, _ = await asyncio.to_thread(
            self._json_sync,
            f"https://{registry}/token?scope=repository:{path}:pull",
            {"Accept": "application/json"},
        )
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise RegistryTargetError("registry token response invalid")
        actual = await asyncio.to_thread(self._manifest_sync, target.tag, token)
        if actual != target.digest:
            raise RegistryTargetError("registry manifest digest mismatch")
        # ``edge``/``latest`` are only moving discovery aliases; an update
        # request must still point at the current channel head, never an
        # arbitrary historical tag. Compare the alias independently to close
        # the TOCTOU window between catalog and destructive submission.
        head_tag = "edge" if target.channel == "development" else "latest"
        head_digest = await asyncio.to_thread(self._manifest_sync, head_tag, token)
        if head_digest != target.digest:
            raise RegistryTargetError(
                f"{target.channel} target is not the current {head_tag} head"
            )
        return target


__all__ = [
    "REPOSITORY",
    "DevelopmentTarget",
    "RegistryClient",
    "RegistryTargetError",
    "StableTarget",
    "parse_development_target",
    "parse_stable_target",
]
