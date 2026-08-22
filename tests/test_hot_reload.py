"""模块级热重载（backend/core/hot_reload.py）的单元测试。"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core import hot_reload


@pytest.fixture
def hot_env(tmp_path, monkeypatch):
    """临时仓库根 + backend 目录；返回写文件/加载模块的工具。"""

    import backend as backend_pkg

    root = tmp_path
    (root / "backend").mkdir(parents=True)
    # importlib.reload 经父包 __path__ 重新定位源文件；把临时 backend
    # 目录挂进真实包路径，reload 才能找到注入的测试模块。
    original_paths = list(backend_pkg.__path__)
    backend_pkg.__path__.append(str(root / "backend"))
    injected: list[str] = []

    def write_module(rel: str, content: str) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def load_module(name: str, path: Path) -> types.ModuleType:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        injected.append(name)
        spec.loader.exec_module(module)
        return module

    def mount_app(app: FastAPI) -> None:
        monkeypatch.setitem(sys.modules, "backend.main", SimpleNamespace(app=app))

    yield SimpleNamespace(
        root=root,
        write_module=write_module,
        load_module=load_module,
        mount_app=mount_app,
    )

    backend_pkg.__path__[:] = original_paths
    for name in injected:
        sys.modules.pop(name, None)
    hot_reload._DEP_CACHE.clear()


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("backend/foo.py", "backend.foo"),
        ("backend/__init__.py", "backend"),
        ("backend/pkg/__init__.py", "backend.pkg"),
        ("backend/pkg/mod.py", "backend.pkg.mod"),
        ("scripts/dev.py", None),
        ("updater/src/x.py", None),
    ],
)
def test_module_name_for_path(hot_env, rel, expected):
    path = hot_env.write_module(rel, "")
    assert hot_reload.module_name_for_path(path, hot_env.root) == expected


def test_closure_includes_transitive_importers(hot_env):
    leaf = hot_env.write_module("backend/_closure_leaf.py", "VALUE = 1\n")
    middle = hot_env.write_module(
        "backend/_closure_middle.py",
        "from backend._closure_leaf import VALUE\n",
    )
    top = hot_env.write_module(
        "backend/_closure_top.py",
        "import backend._closure_middle\n",
    )
    hot_env.load_module("backend._closure_leaf", leaf)
    hot_env.load_module("backend._closure_middle", middle)
    hot_env.load_module("backend._closure_top", top)

    closure = hot_reload._affected_closure({"backend._closure_leaf"})

    assert {
        "backend._closure_leaf",
        "backend._closure_middle",
        "backend._closure_top",
    } <= closure


def test_apply_code_changes_reloads_and_patches_routes(hot_env):
    path = hot_env.write_module(
        "backend/_hot_case.py",
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        "@router.get('/ping')\n"
        "def ping():\n"
        "    return {'value': 'old'}\n",
    )
    module = hot_env.load_module("backend._hot_case", path)

    app = FastAPI()
    app.include_router(module.router)
    hot_env.mount_app(app)
    client = TestClient(app)
    assert client.get("/ping").json() == {"value": "old"}

    hot_env.write_module(
        "backend/_hot_case.py",
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        "@router.get('/ping')\n"
        "def ping():\n"
        "    return {'value': 'brand new'}\n"
        "\n"
        "\n"
        "@router.get('/pong')\n"
        "def pong():\n"
        "    return {'value': 'added'}\n",
    )

    result = hot_reload.apply_code_changes([path], root=hot_env.root)

    assert result.reloaded == ["backend._hot_case"]
    assert result.patched_route_modules == ["backend._hot_case"]
    assert result.failed == []
    # 路由树已指向 reload 后的新 router：旧路由更新、新增路由生效。
    assert client.get("/ping").json() == {"value": "brand new"}
    assert client.get("/pong").json() == {"value": "added"}


def test_patch_routes_tolerates_legacy_included_router_cache_attributes(
    hot_env, monkeypatch
):
    """旧 FastAPI/测试替身缺少私有缓存字段时仍完成路由替换。"""

    class LegacyIncludedRouter:
        __slots__ = ("original_router", "routes")

        def __init__(self, original_router):
            self.original_router = original_router
            self.routes = []

    class AttributeErrorIncludedRouter:
        __slots__ = ("original_router", "routes")

        def __init__(self, original_router):
            self.original_router = original_router
            self.routes = []

        @property
        def _effective_candidates_version(self):
            raise AttributeError("legacy cache field")

        @_effective_candidates_version.setter
        def _effective_candidates_version(self, value):
            raise AttributeError("legacy cache field")

        @property
        def _effective_low_priority_routes_version(self):
            raise AttributeError("legacy cache field")

        @_effective_low_priority_routes_version.setter
        def _effective_low_priority_routes_version(self, value):
            raise AttributeError("legacy cache field")

    import fastapi.routing

    monkeypatch.setattr(
        fastapi.routing,
        "_IncludedRouter",
        (LegacyIncludedRouter, AttributeErrorIncludedRouter),
    )

    old_router = SimpleNamespace(routes=[])
    old_router_with_error_fields = SimpleNamespace(routes=[])
    new_router = SimpleNamespace(routes=[])
    new_router_with_error_fields = SimpleNamespace(routes=[])
    legacy_route = LegacyIncludedRouter(old_router)
    error_field_route = AttributeErrorIncludedRouter(old_router_with_error_fields)
    app = SimpleNamespace(
        router=SimpleNamespace(routes=[legacy_route, error_field_route]),
        openapi_schema={"cached": True},
    )
    hot_env.mount_app(app)

    patched = hot_reload._patch_routes(
        {
            id(old_router): new_router,
            id(old_router_with_error_fields): new_router_with_error_fields,
        },
        {
            id(new_router): "backend._legacy_router",
            id(new_router_with_error_fields): "backend._attribute_error_router",
        },
    )

    assert patched == [
        "backend._attribute_error_router",
        "backend._legacy_router",
    ]
    assert legacy_route.original_router is new_router
    assert error_field_route.original_router is new_router_with_error_fields
    assert app.openapi_schema is None


def test_patch_preserves_aggregated_router_dependencies(hot_env):
    leaf_path = hot_env.write_module(
        "backend/_hot_leaf.py",
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        "@router.get('/leaf')\n"
        "def leaf():\n"
        "    return {'v': 1}\n",
    )
    agg_path = hot_env.write_module(
        "backend/_hot_agg.py",
        "from fastapi import APIRouter, Depends\n"
        "\n"
        "from backend._hot_leaf import router as leaf_router\n"
        "\n"
        "\n"
        "MARKED = []\n"
        "\n"
        "\n"
        "def marker():\n"
        "    MARKED.append(1)\n"
        "    return None\n"
        "\n"
        "\n"
        "router = APIRouter(dependencies=[Depends(marker)])\n"
        "router.include_router(leaf_router)\n",
    )
    hot_env.load_module("backend._hot_leaf", leaf_path)
    agg = hot_env.load_module("backend._hot_agg", agg_path)

    app = FastAPI()
    app.include_router(agg.router, prefix="/api")
    hot_env.mount_app(app)
    client = TestClient(app)
    assert client.get("/api/leaf").json() == {"v": 1}
    marked_before = len(agg.MARKED)
    assert marked_before == 1  # 聚合层依赖已在请求链上生效

    hot_env.write_module(
        "backend/_hot_leaf.py",
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        "@router.get('/leaf')\n"
        "def leaf():\n"
        "    return {'v': 2}\n",
    )

    result = hot_reload.apply_code_changes([leaf_path], root=hot_env.root)

    assert "backend._hot_leaf" in result.reloaded
    assert "backend._hot_agg" in result.reloaded  # import 闭包成员
    # 替换后聚合层 router 级 dependencies 仍在请求链上
    # （reload 重新执行模块体会重置 MARKED，此处按新列表计数）。
    assert client.get("/api/leaf").json() == {"v": 2}
    assert len(agg.MARKED) == 1


def test_blocked_modules_report_restart_required(hot_env):
    main_path = hot_env.write_module("backend/main.py", "")
    models_path = hot_env.write_module("backend/models/database.py", "")

    result = hot_reload.apply_code_changes([main_path, models_path], root=hot_env.root)

    assert result.blocked_restart == ["backend.main", "backend.models.database"]
    assert result.reloaded == []


def test_syntax_error_is_recorded_not_raised(hot_env):
    path = hot_env.write_module("backend/_hot_broken.py", "VALUE = 1\n")
    module = hot_env.load_module("backend._hot_broken", path)
    assert module.VALUE == 1

    hot_env.write_module("backend/_hot_broken.py", "def broken(:\n")

    result = hot_reload.apply_code_changes([path], root=hot_env.root)

    assert len(result.failed) == 1
    assert result.failed[0][0] == "backend._hot_broken"
    assert "SyntaxError" in result.failed[0][1]
    # reload 失败不影响进程，旧绑定保持可用。
    assert module.VALUE == 1


def test_unloaded_module_is_ignored(hot_env):
    path = hot_env.write_module("backend/_hot_fresh.py", "VALUE = 1\n")

    result = hot_reload.apply_code_changes([path], root=hot_env.root)

    assert result.reloaded == []
    assert result.blocked_restart == []
    assert result.failed == []
