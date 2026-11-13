"""Update checker — GitHub Releases 检测、SemVer 比较、解析。

Slice 2：只检测、不更新。Release 数据仅用于 discovery/UI（见 spec §6.0）；
destructive operation 的 authoritative gate 在 Slice 4 updater PREFLIGHT。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import timedelta

import httpx
from loguru import logger

from backend import __version__
from backend.core.log_sanitizer import sanitize_exception_text
from backend.core.redis import get_async_redis
from backend.core.time_service import format_rfc3339, now_utc

# 严格 SemVer X.Y.Z（P0 过滤 prerelease，P2 beta channel 再实现 prerelease precedence）。
# 不接受 v 前缀（tag 去 v 后再比较）；不接受前导零（01.2.3 非法）。
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _parse_semver(version: str) -> tuple[int, int, int] | None:
    """严格 SemVer 解析；非法返回 None。"""
    if not version:
        return None
    m = _SEMVER_RE.match(version)
    if not m:
        return None
    # 拒绝前导零（01.2.3 非法）：\d+ 会匹配 "01"，需显式检查
    if any(len(g) > 1 and g[0] == "0" for g in m.groups()):
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer_version(current: str, candidate: str) -> bool:
    """candidate 是否比 current 新。非法版本返回 False。

    公开函数：update_available 是 derived state，Web 层用它即时计算，
    不信任缓存里可能陈旧的布尔值。
    """
    c, n = _parse_semver(current), _parse_semver(candidate)
    if not c or not n:
        return False
    return n > c


@dataclass(frozen=True)
class ReleaseInfo:
    """单个 Release 的规范化表示。"""

    version: str  # 无 v 前缀
    tag: str
    name: str
    body: str  # Markdown
    published_at: str
    prerelease: bool
    html_url: str


def parse_releases_payload(
    payload: list[dict], include_prerelease: bool = False
) -> list[ReleaseInfo]:
    """解析 GitHub Releases JSON → ReleaseInfo 列表。

    - 过滤 draft。
    - include_prerelease=False 时过滤 prerelease（Slice 2 默认）。
    - **过滤非法 SemVer tag**（nightly/latest/prerelease 等），latest 判断可靠。
    - **按 SemVer 降序排序**（不依赖 API 返回顺序）。
    """
    result: list[ReleaseInfo] = []
    for raw in payload:
        if raw.get("draft"):
            continue
        if raw.get("prerelease") and not include_prerelease:
            continue
        tag = raw.get("tag_name", "")
        version = tag.removeprefix("v")
        if _parse_semver(version) is None:
            continue  # 过滤非法 SemVer
        result.append(
            ReleaseInfo(
                version=version,
                tag=tag,
                name=raw.get("name", "") or "",
                body=raw.get("body", "") or "",
                published_at=raw.get("published_at", "") or "",
                prerelease=bool(raw.get("prerelease")),
                html_url=raw.get("html_url", "") or "",
            )
        )
    result.sort(key=lambda r: _parse_semver(r.version) or (0, 0, 0), reverse=True)
    return result


def release_to_dict(release: ReleaseInfo) -> dict:
    """ReleaseInfo → JSON 可写 dict（缓存用）。"""
    return asdict(release)


CACHE_KEY = "update:releases:cache"
_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "SakuraAI-UpdateChecker",
}


async def read_cache() -> dict | None:
    """读取缓存；不存在/异常返回 None。"""
    try:
        r = await get_async_redis()
        raw = await r.get(CACHE_KEY)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def write_cache(data: dict) -> None:
    """写入缓存（不过期；新鲜度由 fetched_at 判断）。失败静默。"""
    try:
        r = await get_async_redis()
        await r.set(CACHE_KEY, json.dumps(data, ensure_ascii=False))
    except Exception:
        pass


class GitHubReleasesClient:
    """GitHub Releases REST API 客户端（公开仓库无需认证）。

    **分页获取全部**：GitHub List releases 是分页接口（per_page 上限 100），
    只看第一页可能漏掉更高的 SemVer（如维护分支 v2.9.x 占满第一页时 v3.2.0 在第 2 页）。
    每页不足 per_page 即结束（本项目 Release 量通常 1-2 次请求）。
    """

    def __init__(self, repo: str, http: httpx.AsyncClient | None = None):
        self._repo = repo
        self._http = http or httpx.AsyncClient(timeout=15.0)

    async def list_releases(self, per_page: int = 100) -> list[ReleaseInfo]:
        all_raw: list[dict] = []
        page = 1
        while True:
            url = f"https://api.github.com/repos/{self._repo}/releases"
            resp = await self._http.get(
                url,
                params={"per_page": per_page, "page": page},
                headers=_GITHUB_HEADERS,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            all_raw.extend(batch)
            if len(batch) < per_page:
                break  # 不足一页即结束
            page += 1
        return parse_releases_payload(all_raw, include_prerelease=False)

    async def aclose(self) -> None:
        await self._http.aclose()


def _build_cache_data(
    releases: list[ReleaseInfo],
    check_error: str | None,
    last_known: dict | None,
    success: bool,
) -> dict:
    """组装缓存数据。

    成功：刷新 fetched_at + last_checked；更新 latest/releases/update_available。
    失败：保留 last_known 的 fetched_at/releases/latest；只更新 last_checked + check_error。
    成功但 releases=[]：走成功分支（latest=None，update_available=False）。
    注意：缓存里的 update_available 只是诊断快照，Web 层必须用 __version__ + latest 重 derive。
    """
    current = __version__
    now = format_rfc3339(now_utc())
    if success:
        if releases:
            # max 防御（parse 已排序，但显式 max 保证即使无序也对）
            latest_rel = max(
                releases, key=lambda r: _parse_semver(r.version) or (0, 0, 0)
            )
            latest = latest_rel.version
            available = is_newer_version(current, latest)
        else:
            latest = None
            available = False
        return {
            "current_version": current,
            "latest_version": latest,
            "update_available": available,
            "fetched_at": now,
            "last_checked": now,
            "check_error": None,
            "releases": [release_to_dict(r) for r in releases],
        }
    # 失败：保留 last-known-good
    lk = last_known or {}
    prev_latest = lk.get("latest_version")
    return {
        "current_version": current,
        "latest_version": prev_latest,
        "update_available": is_newer_version(current, prev_latest)
        if prev_latest
        else False,
        "fetched_at": lk.get("fetched_at"),  # 保留旧 fetched_at
        "last_checked": now,
        "check_error": check_error,
        "releases": lk.get("releases", []),
    }


class UpdateChecker:
    """One scheduler and in-flight discovery shared by automatic/manual checks."""

    CHECK_INTERVAL_SECONDS = 3600
    STARTUP_DELAY_SECONDS = 10

    def __init__(
        self,
        repo: str | None = None,
        *,
        channel_check: Callable[[], Awaitable[dict]] | None = None,
    ):
        from backend.core.config import get_settings

        self._repo = repo or get_settings().sakura_update_repo
        self._task: asyncio.Task | None = None
        self._inflight: asyncio.Task[dict] | None = None
        self._client = GitHubReleasesClient(self._repo)
        self._channel_check = channel_check
        self._next_check_at: str | None = None
        self._closed = False

    def status(self) -> dict:
        running = self._task is not None and not self._task.done()
        return {
            "scheduler_running": running,
            "checking": self._inflight is not None and not self._inflight.done(),
            "next_check_at": self._next_check_at if running else None,
        }

    def start(self) -> None:
        if self._task is None or self._task.done():
            from backend.services.database_reset_runtime_service import (
                create_registered_background_task,
            )

            if self._closed:
                self._client = GitHubReleasesClient(self._repo)
                self._closed = False
            self._next_check_at = format_rfc3339(
                now_utc() + timedelta(seconds=self.STARTUP_DELAY_SECONDS)
            )
            self._task = create_registered_background_task(
                self._run(), "update_checker"
            )

    async def stop(self) -> None:
        tasks = [task for task in (self._task, self._inflight) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._inflight = None
        self._next_check_at = None
        await self._client.aclose()
        self._closed = True

    async def _run(self) -> None:
        await asyncio.sleep(self.STARTUP_DELAY_SECONDS)
        while True:
            self._next_check_at = None
            try:
                await self.check_once()
            except Exception as exc:
                logger.opt(exception=exc).warning("Update checker cycle failed")
            self._next_check_at = format_rfc3339(
                now_utc() + timedelta(seconds=self.CHECK_INTERVAL_SECONDS)
            )
            await asyncio.sleep(self.CHECK_INTERVAL_SECONDS)

    async def check_once(self) -> dict:
        # No await between inspecting and assigning: callers on this event loop
        # share the same task, including when a waiting HTTP request is cancelled.
        if self._inflight is None or self._inflight.done():
            self._inflight = asyncio.create_task(self._check_once())
        return await asyncio.shield(self._inflight)

    async def _check_once(self) -> dict:
        last_known = await read_cache()
        errors: list[str] = []
        try:
            releases = await self._client.list_releases()
            data = _build_cache_data(releases, None, last_known, success=True)
        except Exception as exc:
            error = sanitize_exception_text(exc)[:240]
            errors.append(error)
            data = _build_cache_data([], error, last_known, success=False)

        if self._channel_check is not None:
            previous = last_known or {}
            try:
                channel_result = await self._channel_check()
                from backend.core.build_info import get_build_info

                data["channel_result"] = channel_result
                data["channel_build"] = get_build_info()
                data["channel_fetched_at"] = format_rfc3339(now_utc())
                data["channel_check_error"] = None
            except Exception as exc:
                error = sanitize_exception_text(exc)[:240]
                errors.append(error)
                data["channel_result"] = previous.get("channel_result")
                data["channel_build"] = previous.get("channel_build")
                data["channel_fetched_at"] = previous.get("channel_fetched_at")
                data["channel_check_error"] = error
            data["channel_last_checked"] = format_rfc3339(now_utc())
        data["check_error"] = "; ".join(errors) or None
        await write_cache(data)
        return data
