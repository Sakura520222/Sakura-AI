"""Small asynchronous GitHub Releases client used by the updater daemon.

The updater has deliberately few production dependencies.  HTTP work is
performed by the standard library in a worker thread, keeping Docker and
network latency off the daemon event loop without adding ``httpx``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import platform
import socket
import ssl
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sakura_ai_updater.semver import SemVer, parse_semver


class ReleaseClientError(RuntimeError):
    """Base class for Release API and manifest failures."""


class ReleaseUnavailableError(ReleaseClientError):
    """GitHub could not be reached and no cached result is available."""

    def __init__(self, message: str, *, detail: str = "request_failed") -> None:
        self.detail = detail
        super().__init__(message)


class ReleaseNotFoundError(ReleaseClientError):
    """No matching stable release exists."""


class ManifestNotFoundError(ReleaseClientError):
    """A release exists but does not include update-manifest.json."""


def _request_failure_detail(exc: BaseException) -> str:
    """Return a credential-free reason suitable for IPC and operator logs."""

    if isinstance(exc, HTTPError):
        return f"http_status_{exc.code}"
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, FileNotFoundError):
            filename = Path(reason.filename).name if reason.filename else "unknown"
            safe_name = "".join(
                character if character.isalnum() or character in ".-_" else "_"
                for character in filename
            )
            return f"file_not_found_{safe_name}"
        if isinstance(reason, ssl.SSLCertVerificationError):
            return "tls_certificate_verification_failed"
        if isinstance(reason, ssl.SSLError):
            return "tls_failed"
        if isinstance(reason, socket.gaierror):
            return "dns_failed"
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout"
        return f"url_error_{type(reason).__name__.lower()}"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(exc, UnicodeError):
        return "invalid_utf8"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return f"os_error_{type(exc).__name__.lower()}"
    if isinstance(exc, ValueError):
        return "invalid_response"
    return "request_failed"


class ReleaseClient:
    """Fetch stable releases and their ``update-manifest.json`` asset."""

    def __init__(
        self,
        owner: str = "sakura520222",
        repo: str = "Sakura-AI",
        *,
        api_url: str | None = None,
        token: str | None = None,
        timeout: float = 15.0,
        manifest_name: str = "update-manifest.json",
        user_agent: str = "sakura-ai-updater/0.1",
        max_pages: int = 10,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.api_url = api_url or f"https://api.github.com/repos/{owner}/{repo}/releases"
        self.token = token
        self.timeout = timeout
        self.manifest_name = manifest_name
        self.user_agent = user_agent
        self.max_pages = max_pages
        self._last_releases: list[dict[str, Any]] | None = None
        self._last_manifest: Any = None
        self._last_release: dict[str, Any] | None = None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self.user_agent,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _fetch_json_sync(self, url: str) -> Any:
        request = Request(url, headers=self._headers())
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseUnavailableError(
                f"GitHub request failed for {url!r}: {exc}",
                detail=_request_failure_detail(exc),
            ) from exc

    async def _fetch_json(self, url: str) -> Any:
        return await asyncio.to_thread(self._fetch_json_sync, url)

    async def list_releases(self) -> list[dict[str, Any]]:
        """Return paginated releases, using the previous result on outages."""

        releases: list[dict[str, Any]] = []
        try:
            for page in range(1, self.max_pages + 1):
                query = urlencode({"per_page": 100, "page": page})
                payload = await self._fetch_json(f"{self.api_url}?{query}")
                if not isinstance(payload, list):
                    raise ReleaseUnavailableError("GitHub releases response is not an array")
                page_items = [item for item in payload if isinstance(item, dict)]
                releases.extend(page_items)
                if len(page_items) < 100:
                    break
        except ReleaseClientError:
            if self._last_releases is not None:
                return list(self._last_releases)
            raise
        self._last_releases = releases
        return list(releases)

    @staticmethod
    def _stable_release(release: Mapping[str, Any]) -> bool:
        return not bool(release.get("draft")) and not bool(release.get("prerelease"))

    @staticmethod
    def _stable_release_version(release: Mapping[str, Any]) -> SemVer | None:
        """Return a stable release's strict SemVer tag, if it is valid."""
        if not ReleaseClient._stable_release(release):
            return None
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag:
            return None
        parsed = parse_semver(tag.removeprefix("v"))
        if parsed is None or parsed.prerelease:
            return None
        return parsed

    async def latest_release(self) -> dict[str, Any] | None:
        releases = await self.list_releases()
        stable = [
            (version, item)
            for item in releases
            if (version := self._stable_release_version(item)) is not None
        ]
        if not stable:
            return None
        # GitHub's API ordering and timestamps are not version precedence.
        stable.sort(key=lambda item: item[0], reverse=True)
        self._last_release = dict(stable[0][1])
        return dict(stable[0][1])

    async def get_release(self, version: str) -> dict[str, Any]:
        """Find a stable release by SemVer, accepting either ``3.1.0`` or ``v3.1.0``."""

        expected_tag = version if version.startswith("v") else f"v{version}"
        releases = await self.list_releases()
        for release in releases:
            if release.get("tag_name") == expected_tag and self._stable_release(release):
                self._last_release = dict(release)
                return dict(release)
        raise ReleaseNotFoundError(f"stable release {expected_tag!r} was not found")

    @staticmethod
    def _asset(release: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
        assets = release.get("assets")
        if not isinstance(assets, list):
            return None
        for asset in assets:
            if isinstance(asset, Mapping) and asset.get("name") == name:
                return asset
        return None

    async def fetch_manifest(self, version: str | None = None) -> Any:
        """Download and parse a release manifest.

        The parsed ``Manifest`` object is returned when the S4-1 parser is
        present.  Keeping the parser import local makes this module usable in
        isolation during package bootstrap and lets tests provide a raw dict.
        """

        try:
            release_call = self.latest_release() if version is None else self.get_release(version)
            release = await release_call if inspect.isawaitable(release_call) else release_call
        except ReleaseClientError:
            if self._last_manifest is not None:
                return self._last_manifest
            raise
        if release is None:
            if self._last_manifest is not None:
                return self._last_manifest
            raise ReleaseNotFoundError("no stable release is available")
        asset = self._asset(release, self.manifest_name)
        if asset is None:
            if self._last_manifest is not None:
                return self._last_manifest
            raise ManifestNotFoundError(f"release has no {self.manifest_name!r} asset")
        url = asset.get("browser_download_url") or asset.get("url")
        if not isinstance(url, str) or not url:
            raise ManifestNotFoundError(f"manifest asset has no download URL: {asset!r}")
        try:
            payload = await self._fetch_json(url)
        except ReleaseClientError:
            if self._last_manifest is not None:
                return self._last_manifest
            raise
        if not isinstance(payload, dict):
            raise ReleaseClientError("update manifest response is not an object")
        expected_version = version
        if expected_version is None:
            tag = str(release.get("tag_name") or "")
            expected_version = tag.removeprefix("v")
        try:
            from sakura_ai_updater.manifest import parse_manifest

            parsed = parse_manifest(payload, expected_version=expected_version)
        except ImportError:
            parsed = payload
        self._last_manifest = parsed
        self._last_release = dict(release)
        return parsed

    async def get_manifest(self, version: str | None = None) -> Any:
        """Compatibility alias for callers that use ``get_manifest``."""

        return await self.fetch_manifest(version)

    async def latest_manifest(self) -> Any:
        return await self.fetch_manifest(None)

    async def get_release_assets(self, version: str | None = None) -> list[str]:
        release_call = self.latest_release() if version is None else self.get_release(version)
        release = await release_call if inspect.isawaitable(release_call) else release_call
        if release is None:
            return []
        assets = release.get("assets")
        if not isinstance(assets, list):
            return []
        return [str(asset["name"]) for asset in assets if isinstance(asset, Mapping) and asset.get("name")]

    async def has_required_assets(self, manifest: Any, version: str | None = None) -> bool:
        """Check architecture updater asset and SHA256SUMS presence."""

        release_call = self.latest_release() if version is None else self.get_release(version)
        release = await release_call if inspect.isawaitable(release_call) else release_call
        if release is None:
            return False
        names = set(await self.get_release_assets(version))
        updater = manifest.get("updater", {}) if isinstance(manifest, Mapping) else getattr(manifest, "updater", {})
        if isinstance(updater, Mapping):
            asset_name = updater.get(
                "asset_linux_arm64" if platform.machine().lower() in {"aarch64", "arm64"} else "asset_linux_amd64"
            )
        else:
            asset_name = getattr(
                updater,
                "asset_linux_arm64" if platform.machine().lower() in {"aarch64", "arm64"} else "asset_linux_amd64",
                None,
            )
        if not isinstance(asset_name, str) or asset_name not in names:
            return False
        return "SHA256SUMS" in names or "sha256sums" in {name.lower() for name in names}

    @property
    def cached_manifest(self) -> Any:
        return self._last_manifest

    @property
    def cached_release(self) -> dict[str, Any] | None:
        return dict(self._last_release) if self._last_release else None
