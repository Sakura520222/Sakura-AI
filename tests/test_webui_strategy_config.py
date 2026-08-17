"""WebUI 审查策略配置保存的回归测试（统一节配置存储）。"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from fastapi.routing import APIRoute

from backend.core import config_sections
from backend.webui.deps import require_csrf, require_super_admin
from backend.webui.routes import config as config_routes
from backend.webui.routes.config import router


@pytest.fixture(autouse=True)
def _clean_section_store():
    """每个测试前后清空进程级节存储，避免测试间串扰。"""
    config_sections.clear_section_store()
    yield
    config_sections.clear_section_store()


class _FormRequest:
    """只实现策略保存路由需要的 Request 接口。"""

    def __init__(self, form_data: Mapping[str, str]) -> None:
        self._form_data = form_data

    async def form(self) -> Mapping[str, str]:
        return self._form_data


class _ScalarResult:
    def scalar_one_or_none(self):
        return None


class _FakeSession:
    """最小 AsyncSession 模拟（无既有节覆盖行）。"""

    def __init__(self) -> None:
        self.rows: list = []
        self.added: list = []

    async def execute(self, _stmt):
        return _ScalarResult()

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.rows.extend(self.added)
        self.added = []

    async def rollback(self) -> None:
        self.added = []


def _strategy_form() -> dict[str, str]:
    form = {}
    for key in config_routes.STRATEGY_KEYS:
        form.update(
            {
                f"strategy_{key}_name": key,
                f"strategy_{key}_max_files": "1",
                f"strategy_{key}_max_lines": "1",
                f"strategy_{key}_prompt": "Review the change.",
            }
        )
    form["strategy_large_max_files"] = "999999"
    form["strategy_large_max_lines"] = "99999999"
    return form


@pytest.mark.asyncio
async def test_save_strategies_accepts_unbounded_large_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_routes, "detect_language", lambda: "zh-CN")

    async def fake_log_admin_action(*_args: object, **_kwargs: object) -> None:
        return None

    def fake_toast_redirect(*args: object, **kwargs: object) -> dict[str, object]:
        return {"args": args, "kwargs": kwargs}

    monkeypatch.setattr(config_routes, "log_admin_action", fake_log_admin_action)
    monkeypatch.setattr(config_routes, "toast_redirect", fake_toast_redirect)

    db = _FakeSession()
    response = await config_routes.save_strategies_section(
        _FormRequest(_strategy_form()),
        db=db,
        user={"sub": "admin", "user_id": 1},
        csrf_token="csrf-token",
        section="strategies",
    )

    assert response["args"][1] == "toast.strategy_saved"
    # large 策略的无上限条件值原样落库，不设数值上限
    saved = json.loads(db.rows[0].key_value)
    assert saved["large"]["conditions"] == {
        "max_files": 999999,
        "max_lines": 99999999,
    }
    assert db.rows[0].key_name == "strategy.strategies"


@pytest.mark.asyncio
async def test_save_strategies_rejects_non_positive_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_routes, "detect_language", lambda: "zh-CN")

    def fake_toast_redirect(*args: object, **kwargs: object) -> dict[str, object]:
        return {"args": args, "kwargs": kwargs}

    monkeypatch.setattr(config_routes, "toast_redirect", fake_toast_redirect)

    form = _strategy_form()
    form["strategy_quick_max_files"] = "0"

    db = _FakeSession()
    response = await config_routes.save_strategies_section(
        _FormRequest(form),
        db=db,
        user={"sub": "admin", "user_id": 1},
        csrf_token="csrf-token",
        section="strategies",
    )

    assert response["args"][1] == "toast.config_validation_failed"
    # 校验失败不落库
    assert db.rows == [] and db.added == []


def _route(path: str, method: str) -> APIRoute:
    for route in router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in route.methods
        ):
            return route
    raise AssertionError(f"Route {method} {path} not found")


def _dependency_calls(route: APIRoute) -> list[object]:
    return [dependency.call for dependency in route.dependant.dependencies]


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/config/strategies/save", "POST"),
        ("/config/labels/save-labels", "POST"),
        ("/config/labels/save-settings", "POST"),
        ("/config/labels/save-conflict-rules", "POST"),
    ],
)
def test_strategy_and_label_mutations_require_super_admin(path: str, method: str):
    assert require_super_admin in _dependency_calls(_route(path, method))


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/config/strategies/save", "POST"),
        ("/config/labels/save-labels", "POST"),
        ("/config/labels/save-settings", "POST"),
        ("/config/labels/save-conflict-rules", "POST"),
    ],
)
def test_strategy_and_label_mutations_require_form_csrf(path: str, method: str):
    assert require_csrf in _dependency_calls(_route(path, method))
