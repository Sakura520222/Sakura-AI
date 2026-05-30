"""WebUI auth route CSRF dependency coverage."""

from fastapi.routing import APIRoute

from backend.webui.deps import require_csrf, require_csrf_header
from backend.webui.routes.auth import router


def _route(path: str, method: str) -> APIRoute:
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return route
    msg = f"Route {method} {path} not found"
    raise AssertionError(msg)


def _dependency_calls(route: APIRoute) -> list[object]:
    return [dependency.call for dependency in route.dependant.dependencies]


def test_verify_two_factor_uses_shared_form_csrf_dependency():
    route = _route("/auth/2fa", "POST")

    assert require_csrf in _dependency_calls(route)


def test_logout_uses_shared_form_csrf_dependency():
    route = _route("/auth/logout", "POST")

    assert require_csrf in _dependency_calls(route)


def test_set_theme_uses_shared_header_csrf_dependency():
    route = _route("/auth/api/theme", "POST")

    assert require_csrf_header in _dependency_calls(route)
