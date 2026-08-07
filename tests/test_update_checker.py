"""UpdateChecker 基础层：SemVer（严格 X.Y.Z）+ ReleaseInfo 解析（过滤 + 排序）。"""

from backend.services.update_checker import (
    ReleaseInfo,
    _parse_semver,
    is_newer_version,
    parse_releases_payload,
)


def test_parse_semver_valid():
    assert _parse_semver("3.1.0") == (3, 1, 0)


def test_parse_semver_invalid():
    assert _parse_semver("3.1") is None
    assert _parse_semver("latest") is None
    assert _parse_semver("") is None
    assert _parse_semver("v3.1.0") is None  # 不接受 v 前缀
    assert _parse_semver("01.2.3") is None  # 前导零非法
    assert _parse_semver("3.1.0-rc1") is None  # P0 严格 X.Y.Z（P2 再支持 prerelease）


def test_is_newer_version_basic():
    assert is_newer_version("3.0.0", "3.1.0") is True
    assert is_newer_version("3.1.0", "3.0.0") is False
    assert is_newer_version("3.1.0", "3.1.0") is False


def test_is_newer_version_invalid_falls_false():
    assert is_newer_version("3.0.0", "latest") is False
    assert is_newer_version("garbage", "3.1.0") is False


def test_parse_filters_draft_and_prerelease_and_strips_v():
    payload = [
        {"tag_name": "v3.1.0", "name": "3.1.0", "body": "n",
         "published_at": "2026-08-07T10:00:00Z", "prerelease": False,
         "draft": False, "html_url": "u1"},
        {"tag_name": "v3.2.0-beta", "name": "b", "body": "",
         "published_at": "", "prerelease": True, "draft": False, "html_url": "u2"},
        {"tag_name": "v3.0.0", "name": "", "body": "",
         "published_at": "", "prerelease": False, "draft": True, "html_url": "u3"},
    ]
    releases = parse_releases_payload(payload)
    assert [r.version for r in releases] == ["3.1.0"]


def test_parse_sorts_by_semver_descending_not_api_order():
    # GitHub API 不保证 SemVer 排序；parser 必须自己降序排序
    payload = [
        {"tag_name": "v3.0.1", "name": "", "body": "",
         "published_at": "", "prerelease": False, "draft": False, "html_url": "a"},
        {"tag_name": "v3.2.0", "name": "", "body": "",
         "published_at": "", "prerelease": False, "draft": False, "html_url": "b"},
        {"tag_name": "v3.1.0", "name": "", "body": "",
         "published_at": "", "prerelease": False, "draft": False, "html_url": "c"},
    ]
    releases = parse_releases_payload(payload)
    assert [r.version for r in releases] == ["3.2.0", "3.1.0", "3.0.1"]


def test_parse_filters_non_semver_tags():
    payload = [
        {"tag_name": "v3.1.0", "name": "", "body": "",
         "published_at": "", "prerelease": False, "draft": False, "html_url": "x"},
        {"tag_name": "nightly", "name": "", "body": "",
         "published_at": "", "prerelease": False, "draft": False, "html_url": "y"},
        {"tag_name": "latest", "name": "", "body": "",
         "published_at": "", "prerelease": False, "draft": False, "html_url": "z"},
    ]
    releases = parse_releases_payload(payload)
    assert [r.version for r in releases] == ["3.1.0"]


def test_release_info_fields():
    payload = [
        {"tag_name": "v3.1.0", "name": "Sakura 3.1.0", "body": "## Changed",
         "published_at": "2026-08-07T10:00:00Z", "prerelease": False,
         "draft": False, "html_url": "https://github.com/x/releases/tag/v3.1.0"},
    ]
    r = parse_releases_payload(payload)[0]
    assert isinstance(r, ReleaseInfo)
    assert r.version == "3.1.0"
    assert r.tag == "v3.1.0"
    assert r.name == "Sakura 3.1.0"
    assert r.prerelease is False
    assert r.html_url.endswith("v3.1.0")


import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.services.update_checker import (
    GitHubReleasesClient,
    UpdateChecker,
)


class FakeRedis:
    """内存 Redis 替身（方法带 self）。"""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, val: str) -> None:
        self.store[key] = val


def _tag(t: str) -> dict:
    return {"tag_name": "v" + t, "name": "", "body": "", "published_at": "",
            "prerelease": False, "draft": False, "html_url": ""}


@pytest.mark.asyncio
async def test_client_list_releases_paginates_all_pages(monkeypatch):
    """分页获取全部：第一页满（per_page=2）→ 第二页不足一页结束。"""
    client = GitHubReleasesClient("x")

    def make_resp(batch):
        r = AsyncMock()
        r.raise_for_status = lambda: None
        r.json = lambda: batch
        return r

    responses = [
        make_resp([_tag("3.0.0"), _tag("2.9.0")]),  # 满页
        make_resp([_tag("4.0.0")]),  # 不足一页 → 结束
    ]
    calls: list[dict] = []

    async def fake_get(url, params=None, headers=None):
        calls.append(params)
        return responses.pop(0)

    monkeypatch.setattr(client._http, "get", fake_get)
    try:
        releases = await client.list_releases(per_page=2)
    finally:
        await client.aclose()
    assert [r.version for r in releases] == ["4.0.0", "3.0.0", "2.9.0"]
    assert len(calls) == 2
    assert calls[0]["page"] == 1
    assert calls[1]["page"] == 2


@pytest.mark.asyncio
async def test_check_once_success_refreshes_fetched_and_flags(monkeypatch):
    monkeypatch.setattr(
        "backend.services.update_checker.__version__", "3.0.0", raising=False
    )
    checker = UpdateChecker(repo="x")
    checker._client.list_releases = AsyncMock(
        return_value=[ReleaseInfo("3.1.0", "v3.1.0", "n", "body", "", False, "u")]
    )
    fake = FakeRedis()
    monkeypatch.setattr(
        "backend.services.update_checker.get_async_redis",
        AsyncMock(return_value=fake),
    )
    try:
        data = await checker.check_once()
    finally:
        await checker.stop()  # 关闭 httpx client，避免资源警告
    assert data["current_version"] == "3.0.0"
    assert data["latest_version"] == "3.1.0"
    assert data["update_available"] is True
    assert data["check_error"] is None
    assert data["fetched_at"] == data["last_checked"]  # 成功：两者刷新
    assert "update:releases:cache" in fake.store


@pytest.mark.asyncio
async def test_check_once_failure_preserves_last_known_good(monkeypatch):
    """失败时 fetched_at/releases/latest 保留；只更新 last_checked + check_error。"""
    monkeypatch.setattr(
        "backend.services.update_checker.__version__", "3.0.0", raising=False
    )
    checker = UpdateChecker(repo="x")
    checker._client.list_releases = AsyncMock(side_effect=RuntimeError("network"))
    fake = FakeRedis()
    fake.store["update:releases:cache"] = (
        '{"current_version":"3.0.0","latest_version":"3.1.0",'
        '"update_available":true,"fetched_at":"T0","last_checked":"T0",'
        '"check_error":null,"releases":[{"version":"3.1.0","tag":"v3.1.0",'
        '"name":"","body":"","published_at":"","prerelease":false,"html_url":""}]}'
    )
    monkeypatch.setattr(
        "backend.services.update_checker.get_async_redis",
        AsyncMock(return_value=fake),
    )
    try:
        data = await checker.check_once()
    finally:
        await checker.stop()
    assert data["check_error"] == "network"
    assert data["fetched_at"] == "T0"  # 保留旧 fetched_at
    assert data["last_checked"] != "T0"  # last_checked 刷新
    assert data["latest_version"] == "3.1.0"
    assert data["update_available"] is True
    assert len(data["releases"]) == 1


@pytest.mark.asyncio
async def test_check_once_empty_success_not_treated_as_failure(monkeypatch):
    """请求成功但 releases=[]：走成功分支，latest=None，fetched_at 刷新。"""
    monkeypatch.setattr(
        "backend.services.update_checker.__version__", "3.0.0", raising=False
    )
    checker = UpdateChecker(repo="x")
    checker._client.list_releases = AsyncMock(return_value=[])
    fake = FakeRedis()
    monkeypatch.setattr(
        "backend.services.update_checker.get_async_redis",
        AsyncMock(return_value=fake),
    )
    try:
        data = await checker.check_once()
    finally:
        await checker.stop()
    assert data["check_error"] is None
    assert data["latest_version"] is None
    assert data["update_available"] is False
    assert data["fetched_at"] == data["last_checked"]


@pytest.mark.asyncio
async def test_check_once_lock_limits_concurrency(monkeypatch):
    """_check_lock 真验证：最大并发进入 GitHub 调用必须 == 1。"""
    monkeypatch.setattr(
        "backend.services.update_checker.__version__", "3.0.0", raising=False
    )
    checker = UpdateChecker(repo="x")
    call_count = 0
    active = 0
    max_active = 0

    async def counting_list():
        nonlocal call_count, active, max_active
        call_count += 1
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.05)
            return [ReleaseInfo("3.1.0", "v3.1.0", "", "", "", False, "")]
        finally:
            active -= 1

    checker._client.list_releases = counting_list
    monkeypatch.setattr(
        "backend.services.update_checker.get_async_redis",
        AsyncMock(return_value=FakeRedis()),
    )
    try:
        await asyncio.gather(checker.check_once(), checker.check_once())
    finally:
        await checker.stop()
    # lock 保证串行化：任何时刻最多 1 个并发 GitHub 调用
    assert max_active == 1
    assert call_count == 2
