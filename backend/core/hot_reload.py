"""开发模式模块级热重载。

``python -m backend.main`` 的应用子进程在未关闭热重载时，由 watcher 线程
监视仓库内 ``*.py`` 变化；变化不再终止/重启整个子进程，而是：

1. 将变更文件映射为 ``backend.*`` 模块，并计算传递依赖闭包——
   import 了变更模块的模块也必须 reload，from-import 才会重新绑定；
2. 按依赖拓扑序对闭包内模块执行 ``importlib.reload``；
3. 把路由树中已注册的旧 APIRouter 引用替换为 reload 后的新 router
   （FastAPI 引用式 include），路由的新增/删除自动生效。

与整进程重启的差异（取舍于"保留进程状态"）：
- 黑名单模块（``RELOAD_BLOCKED_PREFIXES``）持有进程级可变状态或
  入口组装职责，不参与 reload；直接变更这些文件时提示手动重启；
- lifespan 挂在 ``app.state`` 的服务实例与运行中的后台调度器仍持有
  旧类引用，改动仅对"新请求/新任务"生效。
"""

from __future__ import annotations

import ast
import importlib
import sys
import threading
import types
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from fastapi import APIRouter

# 不参与热重载的模块（支持包前缀）：
# - backend.main / backend.models：app 组装与 SQLAlchemy 声明注册表，
#   reload 会重复装配/重复注册表；
# - backend.webui.sse：SSEManager 模块级单例持有活跃订阅者队列；
# - backend.core.rate_limit：slowapi limiter 运行状态挂 app.state；
# - backend.core.server_runtime：登记的当前 Server 句柄，reload 后
#   应用内重启请求会失效；
# - backend.core.logging_bridge：模块级 configure_logging 重复配置 handler；
# - backend.core.time_service：时间服务单例遍布全进程。
RELOAD_BLOCKED_PREFIXES: frozenset[str] = frozenset(
    {
        "backend.main",
        "backend.models",
        "backend.webui.sse",
        "backend.core.rate_limit",
        "backend.core.server_runtime",
        "backend.core.logging_bridge",
        "backend.core.time_service",
    }
)

# 路由手术见 _patch_routes：FastAPI 引用式 include 下，把路由树中的
# 旧 router 引用替换为 reload 后的新 router 并失效展开缓存。


@dataclass
class HotReloadResult:
    """一次热重载的结果汇总，供日志与测试断言。"""

    reloaded: list[str] = field(default_factory=list)
    # 变更直接命中黑名单、无法热重载需手动重启的模块。
    blocked_restart: list[str] = field(default_factory=list)
    # 完成路由树引用替换的路由模块（新增/删除路由自动生效）。
    patched_route_modules: list[str] = field(default_factory=list)
    # reload 抛异常的模块及错误描述（如语法错误），修复后下次变更重试。
    failed: list[tuple[str, str]] = field(default_factory=list)


# 依赖解析缓存：模块文件 -> (mtime_ns, size, 依赖集合)。
# 文件未变化时复用，避免每次变更全量重跑 AST。
_DEP_CACHE: dict[str, tuple[tuple[int, int], frozenset[str]]] = {}


def _default_root() -> Path:
    """仓库根目录（backend/core/hot_reload.py 向上三级）。"""

    return Path(__file__).resolve().parent.parent.parent


def module_name_for_path(path: Path, root: Path) -> str | None:
    """把 backend 包内的 .py 文件映射为模块名；包外返回 None。"""

    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if not parts or parts[0] != "backend" or not str(path).endswith(".py"):
        return None
    stem_parts = parts[:-1]
    if parts[-1] != "__init__.py":
        stem_parts = (*stem_parts, parts[-1].removesuffix(".py"))
    if not stem_parts:
        return None
    return ".".join(stem_parts)


def _is_blocked(module_name: str) -> bool:
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in RELOAD_BLOCKED_PREFIXES
    )


def _iter_loaded_backend_modules() -> dict[str, types.ModuleType]:
    """收集当前已加载、可定位源文件的 backend.* 模块。"""

    loaded: dict[str, types.ModuleType] = {}
    for name, module in list(sys.modules.items()):
        if not name.startswith("backend"):
            continue
        file = getattr(module, "__file__", None)
        if isinstance(file, str) and file.endswith(".py"):
            loaded[name] = module
    return loaded


def _parse_module_deps(module_name: str, module: types.ModuleType) -> frozenset[str]:
    """AST 解析模块 import 的 backend.* 目标集合（含相对导入）。

    ``from backend.x import y`` 中 y 可能是子模块：仅当该全名已加载时
    才计入（运行时视角，未加载的子模块不持有旧引用、无需 reload）。
    结果按文件 mtime/size 缓存。
    """

    file = module.__file__
    stat = Path(file).stat()
    cache_key = (stat.st_mtime_ns, stat.st_size)
    cached = _DEP_CACHE.get(file)
    if cached is not None and cached[0] == cache_key:
        return cached[1]

    try:
        tree = ast.parse(Path(file).read_text(encoding="utf-8"), filename=file)
    except SyntaxError:
        # 文件处于语法损坏的中间状态：退回空依赖集，让 reload 阶段
        # 记录失败详情；文件修复后 mtime 变化会重新解析。
        deps = frozenset()
        _DEP_CACHE[file] = (cache_key, deps)
        return deps
    is_package = Path(file).name == "__init__.py"
    package = module_name if is_package else module_name.rpartition(".")[0]

    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("backend."):
                    targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                pkg_parts = package.split(".") if package else []
                up = node.level - 1
                if up > len(pkg_parts):
                    continue
                pkg = ".".join(pkg_parts[: len(pkg_parts) - up])
                base = f"{pkg}.{node.module}" if node.module else pkg
            if not base.startswith("backend"):
                continue
            if base != "backend":
                targets.add(base)
            for alias in node.names:
                if alias.name == "*":
                    continue
                candidate = f"{base}.{alias.name}"
                if candidate in sys.modules:
                    targets.add(candidate)

    deps = frozenset(targets)
    _DEP_CACHE[file] = (cache_key, deps)
    return deps


def _affected_closure(changed: set[str]) -> set[str]:
    """计算传递依赖闭包：变更模块 + 所有（传递）import 它们的已加载模块。"""

    loaded = _iter_loaded_backend_modules()
    deps = {name: _parse_module_deps(name, module) for name, module in loaded.items()}
    importers: dict[str, set[str]] = {}
    for name, ds in deps.items():
        for dep in ds:
            if dep != name:
                importers.setdefault(dep, set()).add(name)

    closure: set[str] = set()
    queue = deque(changed)
    while queue:
        current = queue.popleft()
        if current in closure:
            continue
        closure.add(current)
        queue.extend(importers.get(current, ()))

    return closure & set(loaded) | (changed & set(loaded))


def _topological_order(closure: set[str], deps: dict[str, set[str]]) -> list[str]:
    """Kahn 拓扑排序：被依赖者先 reload，from-import 才能拿到新符号。"""

    remaining = {
        name: {d for d in deps.get(name, ()) if d in closure and d != name}
        for name in closure
    }
    order: list[str] = []
    ready = deque(sorted(name for name, ds in remaining.items() if not ds))
    while ready:
        name = ready.popleft()
        order.append(name)
        for other, other_deps in remaining.items():
            if name in other_deps:
                other_deps.discard(name)
                if not other_deps:
                    ready.append(other)
    # 理论上 import 无环；防御性附加剩余模块，保证不丢。
    emitted = set(order)
    order.extend(sorted(name for name in remaining if name not in emitted))
    return order


def _module_routers(module: types.ModuleType) -> dict[str, APIRouter]:
    """抓取模块级 APIRouter 实例（属性名 -> router）。"""

    from fastapi import APIRouter

    return {
        attr: value
        for attr, value in vars(module).items()
        if isinstance(value, APIRouter)
    }


def _patch_routes(
    router_map: dict[int, APIRouter],
    module_of_router: dict[int, str],
) -> list[str]:
    """把已注册路由树中的旧 router 引用替换为 reload 后的新 router。

    FastAPI（引用式 include）下 app.router.routes 持有 _IncludedRouter，
    其 ``original_router`` 指向被 include 的 router 对象；替换引用并
    失效展开缓存后，路由的新增/删除/依赖变更自动生效。递归下钻子树，
    覆盖"聚合模块 reload 失败、仅深层叶子成功"的残余场景。

    返回发生替换的模块名列表。
    """

    try:
        from fastapi.routing import _IncludedRouter
    except ImportError:  # FastAPI 版本变更，无引用式 include
        logger.warning("当前 FastAPI 不支持 _IncludedRouter，跳过路由更新")
        return []

    main_module = sys.modules.get("backend.main")
    app = getattr(main_module, "app", None)
    if app is None or not router_map:
        return []

    patched: set[str] = set()
    visited: set[int] = set()

    def _swap(router: object) -> None:
        if id(router) in visited:
            return
        visited.add(id(router))
        for route in list(getattr(router, "routes", ())):
            if not isinstance(route, _IncludedRouter):
                continue
            new_router = router_map.get(id(route.original_router))
            if new_router is not None:
                route.original_router = new_router
                # 版本号可能恰好相同，显式失效该节点展开缓存。
                route._effective_candidates_version = None
                route._effective_low_priority_routes_version = None
                patched.add(module_of_router[id(new_router)])
            _swap(route.original_router)

    _swap(app.router)
    if patched:
        # 路由集可能变化，作废 OpenAPI 缓存。
        app.openapi_schema = None
    return sorted(patched)


def _reload_module(module: types.ModuleType) -> None:
    """使字节码缓存失效后 reload。

    pyc 有效性校验只比较源文件"秒级 mtime + size"：编辑器在同一秒内
    保存等长改动（改常量值、等长重写）会被误判为未变化，reload 拿到
    旧字节码。reload 前删除对应 pyc 强制重编译。
    """

    file = getattr(module, "__file__", None)
    if isinstance(file, str) and file.endswith(".py"):
        try:
            Path(importlib.util.cache_from_source(file)).unlink(missing_ok=True)
        except OSError:
            pass  # 删除失败时退回常规 reload（等长同秒改动可能不生效）
    importlib.reload(module)


def apply_code_changes(
    changed_paths: Iterable[Path | str],
    *,
    root: Path | None = None,
) -> HotReloadResult:
    """对一批变更文件执行模块级热重载与路由手术。"""

    result = HotReloadResult()
    root = root or _default_root()

    changed_modules: dict[str, None] = {}
    for path in changed_paths:
        name = module_name_for_path(Path(path), root)
        if name is None:
            continue  # backend 包外（updater/scripts 等）忽略
        if _is_blocked(name):
            result.blocked_restart.append(name)
        elif name in sys.modules:
            changed_modules[name] = None
        # 尚未加载的模块无需 reload，首次 import 自然使用新代码。

    if changed_modules:
        closure = _affected_closure(set(changed_modules))
        loaded = _iter_loaded_backend_modules()
        deps = {
            name: set(_parse_module_deps(name, module))
            for name, module in loaded.items()
        }
        reload_plan = [
            name
            for name in _topological_order(closure, deps)
            # 黑名单模块作为闭包成员时跳过（保留其模块级引用与状态）；
            # 只有作为变更起点时才进入 blocked_restart 提示。
            if not _is_blocked(name) and name in sys.modules
        ]
        # reload 前抓取模块级 router，用于 reload 后替换路由树引用。
        router_snapshots = {
            name: _module_routers(sys.modules[name]) for name in reload_plan
        }
        for name in reload_plan:
            try:
                _reload_module(sys.modules[name])
            except Exception as exc:
                result.failed.append((name, f"{type(exc).__name__}: {exc}"))
                continue
            result.reloaded.append(name)

        router_map: dict[int, APIRouter] = {}
        module_of_router: dict[int, str] = {}
        for name in result.reloaded:
            new_routers = _module_routers(sys.modules[name])
            for attr, old_router in router_snapshots.get(name, {}).items():
                new_router = new_routers.get(attr)
                if new_router is not None and new_router is not old_router:
                    router_map[id(old_router)] = new_router
                    module_of_router[id(new_router)] = name
        result.patched_route_modules = _patch_routes(router_map, module_of_router)

    return result


def _log_result(result: HotReloadResult) -> None:
    """输出热重载摘要；无法热处理的情况给出手动重启指引。"""

    if result.reloaded:
        logger.info("热重载完成: {} 个模块", len(result.reloaded))
    if result.patched_route_modules:
        logger.info("路由已更新: {}", ", ".join(result.patched_route_modules))
    for name, error in result.failed:
        logger.error("模块 reload 失败 {}: {}", name, error)
    if result.blocked_restart:
        logger.warning(
            "以下模块持有进程级状态，无法热重载，请手动重启"
            "（Ctrl+C 后重新运行，或使用管理页重启按钮）: {}",
            ", ".join(result.blocked_restart),
        )


def start_reload_watcher(root: Path) -> threading.Thread:
    """启动后台 watcher 线程：backend/ 内 *.py 变化 → 模块热重载。

    只监视 ``backend/`` 目录（updater、scripts、tests 等仓库内其他代码
    不触发）；PythonFilter 默认忽略 .venv、node_modules 等目录。
    """

    from watchfiles import PythonFilter, watch

    stop_event = threading.Event()

    def _watch() -> None:
        for changes in watch(
            root / "backend",
            watch_filter=PythonFilter(),
            stop_event=stop_event,
            ignore_permission_denied=True,
        ):
            paths = sorted({str(path) for _kind, path in changes})
            if not paths:
                continue
            logger.info("检测到代码文件变化: {}", ", ".join(paths))
            try:
                _log_result(apply_code_changes(paths, root=root))
            except Exception:
                logger.exception("热重载处理失败")

    thread = threading.Thread(target=_watch, daemon=True, name="sakura-hot-reload")
    thread.start()
    return thread
