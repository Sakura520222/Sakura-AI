"""全局配置页 /config（R3）回归测试。

覆盖：新 GET /config 渲染（super_admin 权限、平铺组 + 节上下文）、
旧 GET 页面 302 重定向、保存端点保留、团队页配置端点移除、
web_search 键经 general/save 通用循环保存、protocol_repair 单保存路径。
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.requests import Request

from backend.core import config_sections
from backend.core.config import DYNAMIC_CONFIG_GROUPS, DYNAMIC_CONFIG_RANGES
from backend.webui.deps import require_csrf, require_super_admin
from backend.webui.routes import config as config_routes
from backend.webui.routes.agent_team import router as agent_team_router
from backend.webui.routes.config import router


@pytest.fixture(autouse=True)
def _clean_section_store():
    """每个测试前后清空进程级节存储，避免测试间串扰。"""
    config_sections.clear_section_store()
    yield
    config_sections.clear_section_store()


def _make_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/config",
            "raw_path": b"/config",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


class _ScalarResult:
    """execute 的标量结果（无既有行 → 创建路径）。"""

    def __init__(self, value=None) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return _ScalarsResult()


class _ScalarsResult:
    def all(self):
        return []


class _FakeSession:
    """最小 AsyncSession 模拟：记录新增行，execute 恒空。"""

    def __init__(self) -> None:
        self.added: list = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, _stmt):
        return _ScalarResult()

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FormRequest:
    """只实现保存路由需要的 Request 接口。"""

    def __init__(self, form_data: Mapping[str, str]) -> None:
        self._form_data = form_data

    async def form(self) -> Mapping[str, str]:
        return self._form_data


def _route(path: str, method: str, target_router=router) -> APIRoute:
    for route in target_router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in route.methods
        ):
            return route
    raise AssertionError(f"Route {method} {path} not found")


def _dependency_calls(route: APIRoute) -> list[object]:
    return [dependency.call for dependency in route.dependant.dependencies]


# ---------- 路由注册与权限 ----------


def test_unified_page_requires_super_admin():
    route = _route("/config", "GET")
    assert require_super_admin in _dependency_calls(route)


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/config/general/save", "POST"),
        ("/config/strategies/save", "POST"),
        ("/config/labels/save-labels", "POST"),
        ("/config/labels/save-settings", "POST"),
        ("/config/labels/save-conflict-rules", "POST"),
    ],
)
def test_legacy_save_endpoints_preserved(path: str, method: str):
    """保存链路保留既有 POST 端点路径与语义（不做新端点）。"""
    route = _route(path, method)
    assert require_super_admin in _dependency_calls(route)
    assert require_csrf in _dependency_calls(route)


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/config/general", "GET"),
        ("/config/strategies", "GET"),
        ("/config/labels", "GET"),
    ],
)
@pytest.mark.asyncio
async def test_legacy_get_pages_redirect_to_unified(path: str, method: str):
    """旧 GET 页面返回 302 → /config，且仍需 super_admin。"""
    route = _route(path, method)
    assert require_super_admin in _dependency_calls(route)
    user = {"sub": "admin", "role": "super_admin", "user_id": 1}

    if path == "/config/general":
        response = await config_routes.general_config_page(user=user)
    elif path == "/config/strategies":
        response = await config_routes.strategies_page(user=user)
    else:
        response = await config_routes.labels_page(user=user)
    assert response.status_code == 302
    assert response.headers["location"].startswith("/config")


def test_agent_team_config_save_endpoint_removed():
    """团队页配置保存端点已移除（A9：其余功能保留）。"""
    paths = [
        route.path for route in agent_team_router.routes if isinstance(route, APIRoute)
    ]
    assert "/agent-team/config/save" not in paths


# ---------- 统一页渲染 ----------


@pytest.mark.asyncio
async def test_unified_page_renders_flat_groups_and_section_forms(
    monkeypatch: pytest.MonkeyPatch,
):
    """GET /config 渲染含平铺动态组卡片与策略/标签节表单锚点。"""
    monkeypatch.setattr(config_routes, "detect_language", lambda prefs: "zh-CN")

    response = await config_routes.unified_config_page(
        _make_request(),
        db=_FakeSession(),
        user={"sub": "admin", "role": "super_admin", "user_id": 1},
        user_prefs={"language": "zh-CN"},
    )
    html = response.body.decode("utf-8")

    # 平铺动态组卡片
    for probe in (
        "section-basic",
        "section-web-search",
        "section-issue",
        "section-agent-team",
        'action="/config/general/save"',
        "sakura_enabled",
    ):
        assert probe in html, probe
    assert "section-label-behavior" not in html

    # 策略 7 节表单（含 pr_summary，A10）与标签 3 节表单
    for probe in (
        "section-strategy-strategies",
        "section-strategy-filters",
        "section-strategy-context",
        "section-strategy-policy",
        "section-strategy-issue-analysis",
        "section-strategy-depgraph",
        "section-strategy-pr-summary",
        "section-label-recommendation",
        "section-label-conflict",
        "section-label-definitions",
    ):
        assert probe in html, probe

    # 左侧锚点导航
    assert 'id="configForm"' in html or 'name="csrf_token"' in html

    # 右上角唯一保存按钮；10 个分区保存按钮文案已全部合并移除
    # （导航栏另有退出登录 submit 按钮，故按按钮文案断言而非 type="submit"）
    assert 'id="save-all-btn"' in html
    for removed_label in (
        "保存策略配置",
        "保存过滤规则",
        "保存上下文增强配置",
        "保存审查政策",
        "保存 Issue 分析配置",
        "保存依赖图配置",
        "保存 PR 总结模板",
        "保存推荐设置",
        "保存冲突规则",
        "保存标签定义",
    ):
        assert removed_label not in html, removed_label


# ---------- 分组重组（任务 5） ----------


def test_issue_label_behavior_keys_unified_into_recommendation():
    """issue_auto_create_labels / issue_confidence_threshold 彻底移除：
    PR 与 Issue 标签统一读 label.recommendation 节设置。"""
    from backend.core.config import DYNAMIC_CONFIG_GROUPS, Settings

    for group in DYNAMIC_CONFIG_GROUPS.values():
        keys = set(group["keys"])
        assert "issue_auto_create_labels" not in keys
        assert "issue_confidence_threshold" not in keys
    assert "issue_auto_create_labels" not in Settings.model_fields
    assert "issue_confidence_threshold" not in Settings.model_fields
    assert "label_behavior" not in DYNAMIC_CONFIG_GROUPS


def test_protocol_repair_registered_once_in_review_basic():
    """protocol_repair_max_attempts 双保存路径合一（A10）：仅 review_basic 组。"""
    issue_keys = set(DYNAMIC_CONFIG_GROUPS["issue_analysis"]["keys"])
    assert "protocol_repair_max_attempts" not in issue_keys
    assert "protocol_repair_max_attempts" in set(
        DYNAMIC_CONFIG_GROUPS["review_basic"]["keys"]
    )
    assert DYNAMIC_CONFIG_RANGES["protocol_repair_max_attempts"] == (1, 10)


def test_depgraph_mode_removed_from_dynamic_group():
    """pr_dependency_graph_mode 由节表单单源管理，平铺组不再暴露。"""
    depgraph_keys = set(DYNAMIC_CONFIG_GROUPS["pr_dependency_graph"]["keys"])
    assert "pr_dependency_graph_mode" not in depgraph_keys
    assert "enable_pr_dependency_graph" in depgraph_keys


# ---------- general/save 通用循环 ----------


def _patch_save_deps(monkeypatch: pytest.MonkeyPatch):
    """替换 save_general_config 的副作用依赖（函数内延迟导入 → patch 源模块）。"""
    monkeypatch.setattr(config_routes, "detect_language", lambda: "zh-CN")
    monkeypatch.setattr(config_routes, "log_admin_action", _noop_async)
    monkeypatch.setattr(
        "backend.core.config.update_settings_field", lambda _key, _value: None
    )
    # 信号量重置走延迟导入，patch 目标模块级函数
    monkeypatch.setattr(
        "backend.workers.issue_worker.reset_issue_semaphore", _noop
    )
    monkeypatch.setattr(
        "backend.workers.review_worker.reset_review_semaphore", _noop
    )


async def _noop_async(*_args, **_kwargs) -> None:
    return None


def _noop(*_args, **_kwargs) -> None:
    return None


@pytest.mark.asyncio
async def test_general_save_persists_web_search_and_review_basic_keys(
    monkeypatch: pytest.MonkeyPatch,
):
    """web_search 六键与 review_basic 八键经通用循环落库。"""
    _patch_save_deps(monkeypatch)
    form = {
        "csrf_token": "t",
        "max_concurrent_reviews": "7",
        "review_timeout_seconds": "600",
        "enable_auto_review": "true",
        "enable_check_runs": "true",
        "enable_analysis_check": "true",
        "enable_findings_check": "true",
        "analysis_min_interval_sec": "300",
        "protocol_repair_max_attempts": "5",
        "web_search_enabled": "true",
        "web_search_provider": "duckduckgo",
        "web_search_api_key": "sk-test-1234",
        "web_search_api_key_changed": "true",
        "web_search_max_results": "5",
        "web_search_max_content_length": "2000",
        "web_search_timeout": "30",
    }
    db = _FakeSession()
    response = await config_routes.save_general_config(
        _FormRequest(form),
        db=db,
        user={"sub": "admin", "role": "super_admin", "user_id": 1},
        csrf_token="t",
    )
    assert response.status_code == 302
    assert db.committed
    saved = {row.key_name: row.key_value for row in db.added}
    assert saved["max_concurrent_reviews"] == "7"
    assert saved["web_search_enabled"] == "true"
    assert saved["web_search_provider"] == "duckduckgo"
    assert saved["web_search_api_key"] == "sk-test-1234"
    assert saved["protocol_repair_max_attempts"] == "5"


@pytest.mark.asyncio
async def test_general_save_rejects_below_min_open_ended_range(
    monkeypatch: pytest.MonkeyPatch,
):
    """开区间上界键只约束下界：0 → toast.value_min_required。"""
    _patch_save_deps(monkeypatch)
    calls: list[dict] = []

    def fake_toast_redirect(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(config_routes, "toast_redirect", fake_toast_redirect)
    form = {"csrf_token": "t", "max_concurrent_reviews": "0"}
    await config_routes.save_general_config(
        _FormRequest(form),
        db=_FakeSession(),
        user={"sub": "admin", "role": "super_admin", "user_id": 1},
        csrf_token="t",
    )
    assert calls and calls[0]["args"][1] == "toast.value_min_required"
    assert calls[0]["kwargs"]["min_v"] == 1


@pytest.mark.asyncio
async def test_general_save_accepts_large_open_ended_value(
    monkeypatch: pytest.MonkeyPatch,
):
    """无硬编码上限：大数值通过下界校验后正常保存。"""
    _patch_save_deps(monkeypatch)
    form = {"csrf_token": "t", "max_concurrent_reviews": "999999"}
    db = _FakeSession()
    response = await config_routes.save_general_config(
        _FormRequest(form),
        db=db,
        user={"sub": "admin", "role": "super_admin", "user_id": 1},
        csrf_token="t",
    )
    assert response.status_code == 302
    saved = {row.key_name: row.key_value for row in db.added}
    assert saved["max_concurrent_reviews"] == "999999"


@pytest.mark.asyncio
async def test_general_save_validates_protocol_repair_range(
    monkeypatch: pytest.MonkeyPatch,
):
    """protocol_repair_max_attempts 上界 10：越界 → toast.value_range。"""
    _patch_save_deps(monkeypatch)
    calls: list[dict] = []

    def fake_toast_redirect(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(config_routes, "toast_redirect", fake_toast_redirect)
    form = {"csrf_token": "t", "protocol_repair_max_attempts": "11"}
    await config_routes.save_general_config(
        _FormRequest(form),
        db=_FakeSession(),
        user={"sub": "admin", "role": "super_admin", "user_id": 1},
        csrf_token="t",
    )
    assert calls and calls[0]["args"][1] == "toast.value_range"
    expected = {
        "min_v": 1,
        "max_v": 10,
        "field_key": "protocol_repair_max_attempts",
    }
    assert expected.items() <= calls[0]["kwargs"].items()


@pytest.mark.asyncio
async def test_general_save_rejects_unknown_web_search_provider(
    monkeypatch: pytest.MonkeyPatch,
):
    """web_search_provider 走 SELECT_OPTIONS 校验：非法值 → toast.value_invalid。"""
    _patch_save_deps(monkeypatch)
    calls: list[dict] = []

    def fake_toast_redirect(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(config_routes, "toast_redirect", fake_toast_redirect)
    form = {"csrf_token": "t", "web_search_provider": "google"}
    await config_routes.save_general_config(
        _FormRequest(form),
        db=_FakeSession(),
        user={"sub": "admin", "role": "super_admin", "user_id": 1},
        csrf_token="t",
    )
    assert calls and calls[0]["args"][1] == "toast.value_invalid"


# ---------- 统一保存 save-all ----------


class _JsonRequest:
    """save-all 路由需要的 Request 接口（仅 json()）。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def test_save_all_endpoint_registered_with_admin_and_csrf_header():
    """POST /config/save-all 需 super_admin + CSRF Header（JSON 请求）。"""
    from backend.webui.deps import require_csrf_header

    route = _route("/config/save-all", "POST")
    calls = _dependency_calls(route)
    assert require_super_admin in calls
    assert require_csrf_header in calls


@pytest.mark.asyncio
async def test_save_all_dispatches_handlers_and_aggregates_results(
    monkeypatch: pytest.MonkeyPatch,
):
    """save-all 按页面顺序调用各分区保存 handler，聚合单条 toast JSON。

    strategies 表单透传 section；单项 error toast 标记该分区失败并回传 anchor。
    """
    from fastapi.responses import RedirectResponse

    calls: list[tuple] = []

    async def _ok_handler(request, db, user, csrf_token, **kwargs):
        calls.append(("ok", tuple(sorted(kwargs))))
        return RedirectResponse("/config?_toast=saved&_toast_type=success")

    async def _fail_handler(request, db, user, csrf_token, **kwargs):
        calls.append(("fail", tuple(sorted(kwargs))))
        return RedirectResponse("/config?_toast=bad&_toast_type=error")

    monkeypatch.setattr(config_routes, "save_general_config", _ok_handler)
    monkeypatch.setattr(config_routes, "save_strategies_section", _ok_handler)
    monkeypatch.setattr(config_routes, "save_labels_definitions", _ok_handler)
    monkeypatch.setattr(config_routes, "save_recommendation_settings", _fail_handler)
    monkeypatch.setattr(config_routes, "save_conflict_rules", _ok_handler)
    monkeypatch.setattr(config_routes, "detect_language", lambda: "zh-CN")

    payload = {
        "requests": [
            {
                "action": "/config/general/save",
                "anchor": None,
                "fields": {"max_review_iterations": "3"},
            },
            {
                "action": "/config/strategies/save",
                "anchor": "section-strategy-strategies",
                "fields": {"section": "strategies", "strategy_quick_name": "快速"},
            },
            {
                "action": "/config/labels/save-settings",
                "anchor": "section-label-recommendation",
                "fields": {"rec_enabled": "true", "confidence_threshold": "0.7"},
            },
        ]
    }
    response = await config_routes.save_all_config(
        _JsonRequest(payload),
        db=object(),
        user={"sub": "admin", "role": "super_admin", "user_id": 1},
        csrf_token="t",
    )
    data = json.loads(response.body)

    # 全部 handler 均被调用，strategies 透传 section 参数
    assert [name for name, _kwargs in calls] == ["ok", "ok", "fail"]
    assert calls[1][1] == ("section",)

    assert data["ok"] is False
    failed = [r for r in data["results"] if not r["ok"]]
    assert len(failed) == 1
    assert failed[0]["anchor"] == "section-label-recommendation"
    assert failed[0]["toast"] == "bad"
    assert "1 项配置保存失败" in data["toast"]


@pytest.mark.asyncio
async def test_save_all_success_returns_aggregated_toast(
    monkeypatch: pytest.MonkeyPatch,
):
    """全部成功时返回 ok=True 与成功 toast。"""
    from fastapi.responses import RedirectResponse

    async def _ok_handler(request, db, user, csrf_token, **kwargs):
        return RedirectResponse("/config?_toast=saved&_toast_type=success")

    for name in (
        "save_general_config",
        "save_strategies_section",
        "save_labels_definitions",
        "save_recommendation_settings",
        "save_conflict_rules",
    ):
        monkeypatch.setattr(config_routes, name, _ok_handler)
    monkeypatch.setattr(config_routes, "detect_language", lambda: "zh-CN")

    payload = {"requests": [{"action": "/config/general/save", "anchor": None, "fields": {}}]}
    response = await config_routes.save_all_config(
        _JsonRequest(payload),
        db=object(),
        user={"sub": "admin", "role": "super_admin", "user_id": 1},
        csrf_token="t",
    )
    data = json.loads(response.body)
    assert data["ok"] is True
    assert data["results"][0]["ok"] is True
    assert "全部配置已保存" in data["toast"]


@pytest.mark.asyncio
async def test_save_all_rejects_malformed_payload():
    """非数组 requests → 400。"""
    with pytest.raises(HTTPException) as exc_info:
        await config_routes.save_all_config(
            _JsonRequest({"requests": "nope"}),
            db=object(),
            user={"sub": "admin", "role": "super_admin", "user_id": 1},
            csrf_token="t",
        )
    assert exc_info.value.status_code == 400
