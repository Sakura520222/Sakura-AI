# Auto-Update P0 — Slice 2: UpdateChecker + 版本管理器只读 + navbar badge

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每 60 分钟（+启动后非阻塞 + 手动）检查 GitHub Releases，缓存结果，navbar 对所有用户显示更新 badge，super_admin 可进入版本管理器查看 Release Notes（Markdown 渲染）。本 slice **只检测、不更新**——更新动作在 Slice 3/4。

**Architecture:** `UpdateChecker`（仿 `ScanScheduler`，main.py lifespan 创建**唯一实例**挂 `app.state`，scheduler 与手动端点共用，内置 `_check_lock` 防并发）用 `httpx` **分页获取全部** GitHub Releases（不只看第一页），`parse_releases_payload` **过滤非法 SemVer 并按 SemVer 降序排序**（不依赖 API 顺序），SemVer 严格限定 `X.Y.Z`（P0 过滤 prerelease；P2 再实现 prerelease precedence）。**`update_available` 是 derived state**——Web 层用当前进程 `__version__` + cached latest **即时计算**（`is_newer_version`），不信 Redis 里可能陈旧的布尔值。缓存写入：成功刷新 `fetched_at`+`last_checked`；失败保留 last-known-good 的 `fetched_at`/releases/latest，只更新 `last_checked`+`check_error`。版本管理器页面用 `render_template` + `csrf_token` + `get_user_preferences`，Release Notes 用前端既有 `data-markdown` + marked + DOMPurify 管线。

**Tech Stack:** httpx / asyncio scheduler / Redis / Jinja2 + marked+DOMPurify / pytest + pytest-asyncio。

**关联设计：** [2026-08-07-auto-update-design.md](../specs/2026-08-07-auto-update-design.md)（§6、§13、§6.0）。

**前置：** Slice 1 已提交（`4425f34b`）。

---

## ⚠️ 提交合规

按 `CLAUDE.md`：**执行者不得自主 `git commit`，也不允许子代理提交**。每个 task 完成后只 `git add` 暂存。**每个 task 必须自己 green**（只 import 自己用到的）。

## 范围与非目标

**交付：** GitHub Releases client（分页）/ SemVer（严格 X.Y.Z）/ Redis 缓存（last-known-good）/ UpdateChecker（唯一实例 + 并发锁 + async stop）/ version API（derived update_available）/ 版本管理器页面（super_admin）/ navbar badge。

**不做：** 实际更新（Slice 3/4）/ manifest 门禁（Slice 4）/ prerelease & beta channel（P2）/ 数据库持久化。

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/services/update_checker.py` | Create | `ReleaseInfo` + `_parse_semver`/`is_newer_version` + `parse_releases_payload`（过滤+排序）+ Redis 缓存 + `GitHubReleasesClient`（分页）+ `_build_cache_data`（成功/失败语义）+ `UpdateChecker` |
| `backend/core/config.py` | Modify | Settings 加 `sakura_update_repo` |
| `backend/main.py` | Modify | lifespan：唯一实例挂 `app.state.update_checker` + `await stop()` |
| `backend/webui/routes/version.py` | Modify | `build_version_info`（derive update_available）+ `/version/releases` + `/version/check`（CSRF + app.state）+ `/version/manager` |
| `backend/webui/templates/version_manager.html` | Create | 版本管理器页 |
| `backend/webui/templates/components/navbar.html` | Modify | badge + 点击入口 + 15s 补 fetch |
| `tests/test_update_checker.py` | Create | TDD |
| `tests/test_version_info.py` | Modify | update_info + derive 测试 |

---

## Task 1: ReleaseInfo + SemVer（严格 X.Y.Z）+ parse（过滤+排序）（TDD）

**Files:**
- Create: `backend/services/update_checker.py`（本 task 只放 dataclass + semver + parse）
- Test: `tests/test_update_checker.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_update_checker.py`:

```python
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
    assert r.version == "3.1.0"
    assert r.tag == "v3.1.0"
    assert r.name == "Sakura 3.1.0"
    assert r.prerelease is False
    assert r.html_url.endswith("v3.1.0")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_update_checker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.services.update_checker'`

- [ ] **Step 3: 实现 update_checker.py 基础层**

Create `backend/services/update_checker.py`:

```python
"""Update checker — GitHub Releases 检测、SemVer 比较、解析。

Slice 2：只检测、不更新。Release 数据仅用于 discovery/UI（见 spec §6.0）；
destructive operation 的 authoritative gate 在 Slice 4 updater PREFLIGHT。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

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
        version = tag[1:] if tag.startswith("v") else tag
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
    result.sort(
        key=lambda r: _parse_semver(r.version) or (0, 0, 0), reverse=True
    )
    return result


def release_to_dict(release: ReleaseInfo) -> dict:
    """ReleaseInfo → JSON 可写 dict（缓存用）。"""
    return asdict(release)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_update_checker.py -v`
Expected: 8 passed

- [ ] **Step 5: ruff + 暂存**

```bash
python run_ruff.py --check backend/services/update_checker.py tests/test_update_checker.py
git add backend/services/update_checker.py tests/test_update_checker.py
```

**建议 commit 信息：** `feat(update): add ReleaseInfo + strict SemVer + filtered sorted releases parser`

---

## Task 2: GitHubReleasesClient（分页）+ 缓存 + UpdateChecker

**Files:**
- Modify: `backend/services/update_checker.py`（追加 client + 缓存 + `_build_cache_data` + scheduler）
- Modify: `backend/core/config.py`（加 `sakura_update_repo`）
- Modify: `backend/main.py`（lifespan：唯一实例挂 `app.state` + `await stop()`）
- Test: `tests/test_update_checker.py`（追加 client/check_once 测试）

- [ ] **Step 1: 追加失败测试（分页 + last-known-good + lock 真验证 + stop 关闭）**

在 `tests/test_update_checker.py` 末尾追加：

```python
import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.services.update_checker import (
    GitHubReleasesClient,
    ReleaseInfo,
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
    # 两页都获取；SemVer 排序后 4.0.0 最前
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_update_checker.py -v`
Expected: 5 个新测试 FAIL（`GitHubReleasesClient`/`UpdateChecker` 未定义）。

- [ ] **Step 3: 加 Settings 字段**

Modify `backend/core/config.py`，在 `sakura_deploy_mode` 附近加：

```python
    # 更新检查目标仓库（owner/repo）。默认官方仓库；fork 部署可覆盖。
    sakura_update_repo: str = Field(
        "Sakura520222/Sakura-AI",
        description="GitHub owner/repo，UpdateChecker 检查此仓库的 Releases",
    )
```

- [ ] **Step 4: 追加 client（分页）+ 缓存 + _build_cache_data + scheduler**

在 `backend/services/update_checker.py` **顶部** import 区追加（保留 Task 1 的 `re`/`dataclasses` import）：

```python
import asyncio
import json
from datetime import datetime, timezone

import httpx

from backend import __version__
from backend.core.redis import get_async_redis
```

在文件末尾追加：

```python
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
    now = datetime.now(timezone.utc).isoformat()
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
    """后台更新检查调度器（仿 ScanScheduler）。

    唯一实例（main.py lifespan 创建挂 app.state）；scheduler 与手动端点共用；
    _check_lock 串行化并发 check_once（防 scheduler + 多浏览器竞态打 GitHub）。
    """

    CHECK_INTERVAL_SECONDS = 3600  # 60 min
    STARTUP_DELAY_SECONDS = 10  # 启动后非阻塞延迟

    def __init__(self, repo: str | None = None):
        from backend.core.config import get_settings

        self._task: asyncio.Task | None = None
        self._client = GitHubReleasesClient(repo or get_settings().sakura_update_repo)
        self._check_lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """干净关闭：cancel + await task + 关 client。"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._client.aclose()

    async def _run(self) -> None:
        await asyncio.sleep(self.STARTUP_DELAY_SECONDS)
        while True:
            try:
                await self.check_once()
            except Exception as e:  # 兜底防 scheduler 死
                import logging

                logging.getLogger(__name__).warning(
                    f"UpdateChecker 循环异常: {e}"
                )
            await asyncio.sleep(self.CHECK_INTERVAL_SECONDS)

    async def check_once(self) -> dict:
        """一次检查 + 写缓存。_check_lock 串行化并发调用。返回缓存数据。"""
        async with self._check_lock:
            last_known = await read_cache()
            try:
                releases = await self._client.list_releases()
                data = _build_cache_data(releases, None, last_known, success=True)
            except Exception as e:
                data = _build_cache_data([], str(e), last_known, success=False)
            await write_cache(data)
        return data
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_update_checker.py -v`
Expected: 13 passed（8 旧 + 5 新）

- [ ] **Step 6: main.py lifespan 接入（唯一实例 + app.state + await stop）**

Modify `backend/main.py`：

(a) lifespan 顶部变量声明区（`scan_scheduler = None` 附近）加：

```python
    update_checker = None
```

(b) 在 `_should_start_background_tasks` 块内、`star_aid_scheduler` 启动之后加：

```python
                # 启动更新检查调度器（Slice 2）—— 唯一实例挂 app.state，供手动端点共用
                try:
                    from backend.services.update_checker import UpdateChecker

                    update_checker = UpdateChecker()
                    update_checker.start()
                    app.state.update_checker = update_checker
                    logger.info("✅ 更新检查调度器已启动（60min 周期）")
                except Exception as e:
                    logger.error(f"❌ 更新检查调度器启动失败: {e}")
```

(c) shutdown 段（`star_aid_scheduler.stop()` 之后）：

```python
    # 停止更新检查调度器（async stop：cancel + await + aclose）
    if update_checker:
        await update_checker.stop()
```

- [ ] **Step 7: ruff + 暂存**

```bash
python run_ruff.py --check backend/services/update_checker.py backend/core/config.py backend/main.py tests/test_update_checker.py
git add backend/services/update_checker.py backend/core/config.py backend/main.py tests/test_update_checker.py
```

**建议 commit 信息：** `feat(update): add paginated Releases client + Redis cache + UpdateChecker (async stop, lock, app.state)`

---

## Task 3: version API（derived update_available + releases + check）

**Files:**
- Modify: `backend/webui/routes/version.py`
- Test: `tests/test_version_info.py`

> **本 task 只 import Task 3 用到的：`require_super_admin`、`require_csrf_header`、`is_newer_version`。Task 4 再补 `render_template`/`get_user_preferences`/`get_csrf_serializer`。**

- [ ] **Step 1: 追加失败测试（update_info + derived update_available）**

在 `tests/test_version_info.py` 末尾追加：

```python
from backend.services.update_checker import is_newer_version


def test_with_update_info_when_update_available():
    # 测试环境 __version__=3.0.0 < latest 3.1.0 → derived True
    info = build_version_info(
        "image",
        update_info={
            "latest_version": "3.1.0",
            "update_available": True,
            "last_checked": "2026-08-07T10:00:00Z",
            "check_error": None,
        },
    )
    assert info["latest_version"] == "3.1.0"
    assert info["update_available"] is True
    assert info["last_checked"] == "2026-08-07T10:00:00Z"


def test_update_available_derived_not_cached_bool():
    # 陈旧缓存说 update_available=true，但 latest == 当前 __version__ → derive False
    info = build_version_info(
        "image",
        update_info={
            "latest_version": __version__,  # == 当前进程版本
            "update_available": True,  # 陈旧缓存布尔值
            "last_checked": "2026-08-07T10:00:00Z",
            "check_error": None,
        },
    )
    assert info["update_available"] is False


def test_with_update_info_none_keeps_nulls():
    info = build_version_info("source")
    assert info["update_available"] is None
    assert info["latest_version"] is None
    assert info["last_checked"] is None


def test_with_check_error_no_latest():
    # 有缓存数据但 latest=None（失败且无 last-known-good）→ False 而非 None
    info = build_version_info(
        "image",
        update_info={
            "latest_version": None,
            "update_available": False,
            "last_checked": "2026-08-07T10:00:00Z",
            "check_error": "timeout",
        },
    )
    assert info["update_available"] is False
    assert info["check_error"] == "timeout"


def test_is_newer_version_public_helper():
    # 公开比较函数可被 Web 层复用（derived state 的单一真相源）
    assert is_newer_version("3.0.0", "3.1.0") is True
    assert is_newer_version("3.1.0", "3.1.0") is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_version_info.py -v`
Expected: 新测试 FAIL（`build_version_info` 不接受 `update_info`）。

- [ ] **Step 3: 整体替换 version.py（derived update_available）**

Modify `backend/webui/routes/version.py`，整体替换为：

```python
"""Version & deployment info route.

Slice 1：只读展示当前版本与部署模式。
Slice 2：扩展 update_available/latest（Redis 缓读，derived update_available）+ /version/releases + /version/check。
/version/manager 页面在 Task 4 加（届时再 import render_template 等）。

update_available 是 derived state：必须用当前进程 __version__ + cached latest_version
即时计算（is_newer_version），不信任缓存里可能陈旧的布尔值（外部升级后旧缓存会误报）。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from backend import __version__
from backend.core.config import get_settings
from backend.services.update_checker import is_newer_version, read_cache
from backend.webui.deps import require_auth, require_csrf_header, require_super_admin

router = APIRouter(tags=["Version"])

_VALID_MODES = {"image", "source"}


def build_version_info(
    deploy_mode: str, update_info: dict | None = None
) -> dict:
    """构造版本与部署信息（纯函数）。

    Args:
        deploy_mode: 部署模式。非法值归一化为 "unknown"。
        update_info: 可选的更新检查缓存数据。None 时相关字段为 null。

    update_available 即时 derive：is_newer_version(__version__, latest)。
    - 无缓存（update_info=None）→ None
    - 有缓存且 latest 有值 → derive 布尔
    - 有缓存但 latest 为 None（空列表/失败无 last-known-good）→ False
    """
    mode = deploy_mode if deploy_mode in _VALID_MODES else "unknown"

    update_supported = False
    if mode == "source":
        reason = "source_updater_not_available"
    elif mode == "image":
        reason = "updater_not_connected"  # Slice 4 接入 updater 后改判
    else:
        reason = "unknown_deployment"

    ui = update_info or {}
    latest = ui.get("latest_version")
    if ui:
        available = is_newer_version(__version__, latest) if latest else False
    else:
        available = None
    return {
        "current_version": __version__,
        "deployment_type": mode,
        "update_supported": update_supported,
        "update_unsupported_reason": reason,
        "update_available": available,
        "latest_version": latest,
        "last_checked": ui.get("last_checked"),
        "check_error": ui.get("check_error"),
    }


@router.get("/version/info")
async def get_version_info(user: dict = Depends(require_auth)):
    """当前版本 + 部署模式 + 更新可用性（所有登录用户，驱动 navbar badge）。"""
    mode = get_settings().sakura_deploy_mode or "unknown"
    update_info = await read_cache()
    info = build_version_info(mode, update_info)
    return JSONResponse(
        info,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/version/releases")
async def get_version_releases(user: dict = Depends(require_super_admin)):
    """Release 列表（含 Markdown notes）— 版本管理器数据源，super_admin only。

    update_available 同样 derive，不裸返回缓存布尔值。
    """
    cache = await read_cache() or {}
    latest = cache.get("latest_version")
    available = is_newer_version(__version__, latest) if latest else False
    return JSONResponse(
        {
            "current_version": __version__,
            "latest_version": latest,
            "update_available": available,
            "last_checked": cache.get("last_checked"),
            "check_error": cache.get("check_error"),
            "releases": cache.get("releases", []),
        },
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.post("/version/check")
async def trigger_check(
    request: Request,
    user: dict = Depends(require_super_admin),
    _csrf: str = Depends(require_csrf_header),
):
    """手动触发一次更新检查（super_admin + CSRF）。

    复用 app.state.update_checker（lifespan 创建的唯一实例，含 _check_lock）。
    后台任务未启动（dev/bootstrap）时返回 503。
    """
    checker = getattr(request.app.state, "update_checker", None)
    if checker is None:
        return JSONResponse(
            {"error": "update_checker_unavailable"},
            status_code=503,
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    data = await checker.check_once()
    return JSONResponse(
        data,
        headers={"Cache-Control": "no-store, max-age=0"},
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_version_info.py tests/test_update_checker.py -v`
Expected: 全 passed（version_info 10 = 5 旧 + 5 新；update_checker 13）。

- [ ] **Step 5: ruff + 暂存**

```bash
python run_ruff.py --check backend/webui/routes/version.py tests/test_version_info.py
git add backend/webui/routes/version.py tests/test_version_info.py
```

**建议 commit 信息：** `feat(version): derived update_available + /version/releases + CSRF-guarded /version/check`

---

## Task 4: 版本管理器页面（render_template + csrf + 模板逻辑）

**Files:**
- Modify: `backend/webui/routes/version.py`（加 manager 路由，补 imports）
- Create: `backend/webui/templates/version_manager.html`

- [ ] **Step 1: 加 manager 路由（render_template + user_prefs + csrf_token）**

Modify `backend/webui/routes/version.py`：

(a) 把 Task 3 的 `from backend.webui.deps import require_auth, require_csrf_header, require_super_admin` 替换为：

```python
from backend.webui.deps import (
    get_csrf_serializer,
    get_user_preferences,
    render_template,
    require_auth,
    require_csrf_header,
    require_super_admin,
)
```

(b) 文件末尾追加：

```python
@router.get("/version/manager")
async def version_manager_page(
    request: Request,
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """版本管理器页面（super_admin only）。

    render_template 注入 i18n；csrf_token 供 recheck 按钮 X-CSRF-Token 用。
    """
    mode = get_settings().sakura_deploy_mode or "unknown"
    update_info = await read_cache()
    info = build_version_info(mode, update_info)
    return render_template(
        "version_manager.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        version_info=info,
        releases=(update_info or {}).get("releases", []),
        active_page="version_manager",
        page_title="版本管理",
    )
```

- [ ] **Step 2: 创建版本管理器模板**

Create `backend/webui/templates/version_manager.html`:

```html
{% extends "base.html" %}

{% block title %}版本管理{% endblock %}

{% block content %}
<div class="max-w-4xl mx-auto px-4 py-6 space-y-6">
  <h1 class="text-2xl font-bold text-gray-800 dark:text-gray-100">版本管理</h1>

  {# 当前部署信息卡 / Current deployment card #}
  <section class="bg-white dark:bg-gray-800 rounded-2xl shadow-sm border border-sakura-200/50 dark:border-sakura-800/30 p-6">
    <h2 class="text-lg font-semibold mb-4 text-gray-700 dark:text-gray-200">当前部署</h2>
    <dl class="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
      <div><dt class="text-gray-500 dark:text-gray-400">当前版本</dt>
           <dd class="font-mono font-medium">v{{ version_info.current_version }}</dd></div>
      <div><dt class="text-gray-500 dark:text-gray-400">部署方式</dt>
           <dd>{{ version_info.deployment_type }}</dd></div>
      <div><dt class="text-gray-500 dark:text-gray-400">最近检查</dt>
           <dd>{{ version_info.last_checked or "尚未检查" }}</dd></div>
      <div><dt class="text-gray-500 dark:text-gray-400">更新支持</dt>
           <dd>{{ "是" if version_info.update_supported else "否" }}
               <span class="text-gray-400 text-xs">（{{ version_info.update_unsupported_reason }}）</span></dd></div>
    </dl>

    {# 状态分支：check_error 始终可见（即使有 update）；last_checked None 显示"尚未检查" #}
    {% if version_info.check_error %}
    <div class="mt-4 text-sm text-amber-600 dark:text-amber-400">
      检查失败：{{ version_info.check_error }}（显示上次缓存结果）
    </div>
    {% endif %}
    {% if version_info.update_available %}
    <div class="mt-4 flex items-center gap-2 text-sakura-600 dark:text-sakura-400">
      <span class="w-2 h-2 rounded-full bg-sakura-500 animate-pulse"></span>
      <span class="font-medium">v{{ version_info.latest_version }} 可用</span>
      <span class="text-xs text-gray-400">（更新功能将在后续版本启用）</span>
    </div>
    {% elif version_info.last_checked and not version_info.check_error %}
    <div class="mt-4 text-sm text-gray-500">已是最新版本。</div>
    {% elif not version_info.last_checked %}
    <div class="mt-4 text-sm text-gray-500">尚未完成首次检查，请稍候或点击"重新检查"。</div>
    {% endif %}

    <div class="mt-4">
      <button id="recheck-btn"
              class="px-4 py-2 rounded-xl bg-sakura-500 text-white text-sm font-medium hover:bg-sakura-600 disabled:opacity-50">
        重新检查
      </button>
    </div>
  </section>

  {# Release 列表 / Release list（已按 SemVer 降序） #}
  <section class="space-y-4">
    <h2 class="text-lg font-semibold text-gray-700 dark:text-gray-200">Release 历史</h2>
    {% if not releases %}
    <div class="text-sm text-gray-500">暂无 Release 信息（尚未完成首次检查，或 GitHub 不可达）。</div>
    {% endif %}
    {% for rel in releases %}
    <article class="bg-white dark:bg-gray-800 rounded-2xl shadow-sm border border-sakura-200/50 dark:border-sakura-800/30 p-6">
      <header class="flex items-center justify-between mb-2">
        <h3 class="font-mono font-semibold text-gray-800 dark:text-gray-100">v{{ rel.version }}</h3>
        {% if rel.version == version_info.latest_version and version_info.update_available %}
        <span class="text-xs px-2 py-0.5 rounded-full bg-sakura-100 text-sakura-700 dark:bg-sakura-900/40 dark:text-sakura-300">最新</span>
        {% elif rel.version == version_info.current_version %}
        <span class="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300">当前</span>
        {% endif %}
      </header>
      <div class="text-xs text-gray-400 mb-3">{{ rel.published_at or "—" }}</div>
      {# data-markdown：前端 marked + DOMPurify 自动渲染（base.html 管线）。
         Jinja2 默认 escape 保证 rel.body 作为 textContent 安全注入。 #}
      <div data-markdown class="prose dark:prose-invert max-w-none text-sm">{{ rel.body }}</div>
      {% if rel.html_url %}
      <a href="{{ rel.html_url }}" target="_blank" rel="noopener"
         class="inline-block mt-3 text-xs text-sakura-600 dark:text-sakura-400 hover:underline">查看完整说明</a>
      {% endif %}
    </article>
    {% endfor %}
  </section>
</div>

<script>
// CSRF token 从 template context 注入（navbar restart 同款模式）
const CSRF_TOKEN = "{{ csrf_token }}";
document.getElementById('recheck-btn').addEventListener('click', async function() {
  this.disabled = true;
  this.textContent = '检查中...';
  try {
    const r = await fetch('/version/check', {
      method: 'POST',
      headers: { 'X-CSRF-Token': CSRF_TOKEN, 'Accept': 'application/json' }
    });
    if (r.ok) { location.reload(); }
    else { this.textContent = '失败，点击重试'; this.disabled = false; }
  } catch (e) { this.textContent = '失败，点击重试'; this.disabled = false; }
});
</script>
{% endblock %}
```

- [ ] **Step 3: ruff + 运行测试**

```bash
python run_ruff.py --check backend/webui/routes/version.py
python -m pytest tests/test_version_info.py tests/test_update_checker.py -v
```
Expected: ruff 无错；测试全 passed（本 task 不加新测试，确保不回归）。

- [ ] **Step 4: 手动验证（需完整应用）**

生产模式启动，super_admin 访问 `/version/manager`：部署卡 / Release 列表 Markdown / "重新检查"（X-CSRF-Token）/ 断网时 check_error 可见且 Release 保留。本机无法启动则标 DONE_WITH_CONCERNS。

- [ ] **Step 5: 暂存**

```bash
git add backend/webui/routes/version.py backend/webui/templates/version_manager.html
```

**建议 commit 信息：** `feat(webui): add version manager page (render_template + csrf + markdown notes)`

---

## Task 5: navbar 更新 badge + super_admin 点击入口 + 15s 补 fetch

**Files:**
- Modify: `backend/webui/templates/components/navbar.html`

- [ ] **Step 1: 替换 Slice 1 的版本区域为 badge 版**

找到 Slice 1 插入的版本区域 `<div>`（`{# 版本号区域（所有登录用户可见；点击进版本管理器的能力在 Slice 2 提供） #}` 注释下），整体替换为：

```html
        {# 版本区域：所有登录用户可见版本号 + 更新 badge；super_admin 点击进版本管理器 #}
        <div x-data="{ version: '', updateAvailable: false, latestVersion: '', isAdmin: {% if current_user.role == 'super_admin' %}true{% else %}false{% endif %}, async loadVersion() { try { const r = await fetch('/version/info', { headers: { 'Accept': 'application/json' } }); if (r.ok) { const d = await r.json(); this.version = d.current_version || ''; this.updateAvailable = d.update_available === true; this.latestVersion = d.latest_version || ''; } } catch (e) {} } }"
             x-init="loadVersion(); setTimeout(() => loadVersion(), 15000); setInterval(() => loadVersion(), 600000)"
             @click="if (isAdmin) window.location.href = '/version/manager'"
             :class="isAdmin ? 'cursor-pointer hover:bg-sakura-50 dark:hover:bg-sakura-900/20 rounded-xl' : ''"
             class="hidden sm:flex items-center gap-1.5 px-2 py-1 text-xs font-medium text-gray-500 dark:text-gray-400 transition-colors"
             :title="isAdmin ? '点击进入版本管理器' : 'Sakura AI 版本'">
            <span>v<span x-text="version || '...'"></span></span>
            <template x-if="updateAvailable">
                <span class="flex items-center gap-1 text-sakura-600 dark:text-sakura-400">
                    <span class="w-2 h-2 rounded-full bg-sakura-500 animate-pulse"></span>
                    <span x-text="latestVersion"></span>
                </span>
            </template>
        </div>
```

> - `setTimeout(() => loadVersion(), 15000)`：startup check 在 10s 后完成，15s 补 fetch 让 badge 及时反映（避免最坏 10min 延迟）。
> - `setInterval(..., 600000)`：每 10min 补刷。
> - `isAdmin`：仅 super_admin 可点击；`current_user.role` 在 navbar context（Slice 1 验证过）。

- [ ] **Step 2: 手动验证（需完整应用）**

普通用户不可点击；super_admin 点击 → `/version/manager`；新版本检测后 15s 内 badge 出现。本机无法启动则标 DONE_WITH_CONCERNS。

- [ ] **Step 3: 暂存**

```bash
git add backend/webui/templates/components/navbar.html
```

**建议 commit 信息：** `feat(webui): navbar update badge + version manager entry + timely first refresh`

---

## Self-Review（计划自检）

**1. 两轮审查意见覆盖：**

| # | 审查点 | 修正位置 |
|---|---|---|
| 1 | `releases[0]` 不可靠 | Task 1 parse 过滤非法 SemVer + 降序排序；Task 2 `max()` 防御 |
| 2 | last-known-good 时间语义 | Task 2 `_build_cache_data(success, last_known)`；failure/empty 两测试 |
| 3 | UpdateChecker 生命周期 | Task 2 `async stop()`（cancel+await+aclose）；main.py `await stop()` |
| 4 | app.state 共用 + 并发锁 | Task 2 `app.state.update_checker` + `_check_lock`；Task 3 `/version/check` 用 `request.app.state` |
| 5 | FakeRedis + pytest-asyncio | Task 2 `FakeRedis` class + `@pytest.mark.asyncio` |
| 6 | CSRF 阻断级 | Task 3 `/version/check` `require_csrf_header`；Task 4 `get_csrf_serializer().dumps({})` 注入 |
| 7 | render_template + 模板逻辑 | Task 4 `render_template` + `get_user_preferences` + 模板分支修正 |
| 8 | badge 首次延迟 | Task 5 `setTimeout(..., 15000)` |
| 9 | 分页漏洞 | Task 2 `list_releases` 分页循环（per_page=100，不足一页即结束）+ 分页测试 |
| 10 | update_available 是 derived state | Task 1 公开 `is_newer_version`；Task 3 `build_version_info` + `/version/releases` 用 `is_newer_version(__version__, latest)` 即时计算，不信缓存 bool + stale-cache 测试 |
| 11 | lock 测试真验证 | Task 2 lock 测试改 `max_active == 1` |
| 非阻断 a | SemVer 严格 X.Y.Z | Task 1 regex 无 prerelease + 前导零拒绝（P2 再实现 prerelease precedence） |
| 非阻断 b | 测试关 client | Task 2 各 check_once 测试末尾 `await checker.stop()` |
| 组织 | task 自洽 | Task 3 只 import 自己的；Task 4 再补 render_template 等 |

**2. 占位符扫描：** 无 TBD/TODO；所有代码完整；CSRF/分页用真实 GitHub 语义。

**3. 类型一致性：**
- `ReleaseInfo` 字段五处一致；缓存 key 六处一致。
- `is_newer_version(current, candidate)` 公开函数在 Task 1 定义、Task 2 `_build_cache_data`、Task 3 `build_version_info`/`/version/releases`、测试四处一致。
- `build_version_info` derived 语义：无缓存→None；有缓存 latest 有值→derive；latest None→False。
- UpdateChecker 单实例：main.py 创建 → app.state → `/version/check` → `await stop()`。

**4. 范围检查：** 5 task 各自 green，合并后交付"navbar badge + 版本管理器只读 + 手动检查"。无夹带 updater。

**执行者重点：**
- Task 1 的 `_parse_semver` 前导零拒绝（`01.2.3` → None）——正则 `(\d+)` 会匹配 "01"，靠显式检查拒绝。
- Task 2 分页测试用 `per_page=2` 模拟满页/次页；真实默认 100。
- Task 3 `build_version_info` 的 derived 分支顺序：先 `if ui`（有缓存）→ `latest 有值 → is_newer_version`；`latest None → False`；`else None`。
- Task 4 模板分支：check_error 始终可见 → update_available → last_checked 判"已最新/尚未检查"。

**后续 slice（本计划不含）：** Slice 3 Host Updater daemon + UDS IPC + PyInstaller；Slice 4 ImageAdapter + 状态机 + manifest 门禁。
