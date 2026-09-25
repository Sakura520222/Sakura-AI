"""GitHub App authorization-center security and behavior regressions."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest
from fastapi.routing import APIRoute
from jinja2 import DictLoader, Environment, StrictUndefined

from backend.services import github_user_authorization_service as service_module
from backend.services.github_user_authorization_service import (
    GitHubUserAuthorizationService,
    GitHubUserInstallation,
)
from backend.services.star_aid_github_service import GitHubCallResult
from backend.webui.deps import require_admin, require_super_admin
from backend.webui.routes import github_app, repos


def _route(router: Any, path: str, method: str = "GET") -> APIRoute:
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return route
    raise AssertionError(f"Route {method} {path} was not found")


def _dependency_calls(route: APIRoute) -> list[Any]:
    return [dependency.call for dependency in route.dependant.dependencies]


def test_github_app_routes_are_available_to_every_authenticated_user() -> None:
    """The authorization center is user-scoped and must not require admin."""
    for path in ("/github-app/", "/github-app/list-fragment"):
        dependencies = _dependency_calls(_route(github_app.router, path))
        assert github_app.require_auth in dependencies
        assert require_admin not in dependencies
        assert require_super_admin not in dependencies


def test_admin_repository_routes_keep_their_admin_boundary() -> None:
    for path in ("/repos/", "/repos/list-fragment"):
        dependencies = _dependency_calls(_route(repos.router, path))
        assert repos.require_admin in dependencies


def test_user_authorization_routes_do_not_accept_installation_ids() -> None:
    """GitHub user token scoping, rather than a client ID parameter, is authoritative."""
    paths = {route.path for route in github_app.router.routes}
    assert paths == {"/github-app/", "/github-app/list-fragment"}
    assert all("installation_id" not in path for path in paths)


class _Response:
    def __init__(self, payload: dict[str, Any], links: dict[str, Any] | None = None):
        self.status_code = 200
        self._payload = payload
        self.links = links or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _AsyncClient:
    def __init__(self):
        self.urls: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(
        self, url: str, headers: dict[str, str], params: dict[str, int] | None = None
    ) -> _Response:
        self.urls.append(url)
        if url.endswith("/user/installations"):
            return _Response(
                {
                    "installations": [
                        {
                            "id": 1001,
                            "account": {
                                "login": "alice",
                                "avatar_url": "https://github.com/alice.png",
                            },
                            "target_type": "User",
                            "repository_selection": "selected",
                            "html_url": "https://github.com/settings/installations/1001",
                            "permissions": {"metadata": True, "administration": False},
                            "raw_unconstrained_field": "must-not-render",
                        }
                    ]
                }
            )
        if url == "https://api.github.com/user/installations/1001/repositories":
            return _Response(
                {
                    "repositories": [
                        {
                            "id": index,
                            "full_name": f"alice/repo-{index}",
                            "name": f"repo-{index}",
                            "private": index % 2 == 0,
                            "html_url": "https://github.com/alice/repo",
                            "raw_unconstrained_field": "must-not-render",
                        }
                        for index in range(1, 9)
                    ]
                }
            )
        raise AssertionError(f"Unexpected GitHub URL: {url}")


def _configure_valid_user(monkeypatch, *, client_id: str = "client") -> None:
    settings = SimpleNamespace(
        star_aid_github_app_client_id=client_id,
        star_aid_github_app_client_secret="secret",
    )
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    credential = SimpleNamespace(
        github_username="alice",
        revoked_at=None,
        github_app_client_id=client_id,
    )
    monkeypatch.setattr(
        service_module.credential_service,
        "get_credential",
        lambda session, user_id: _await_value(credential),
    )
    monkeypatch.setattr(
        service_module.credential_service,
        "get_effective_access_token",
        lambda session, user_id: _await_value(("token", GitHubCallResult(success=True))),
    )


async def _await_value(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_user_installations_use_github_user_scope_and_dto_boundary(
    monkeypatch,
) -> None:
    _configure_valid_user(monkeypatch)
    client = _AsyncClient()
    monkeypatch.setattr(
        service_module.httpx, "AsyncClient", lambda timeout=None: client
    )

    authorization = await GitHubUserAuthorizationService().get_installations(
        session=SimpleNamespace(), user_id=7, expected_github_username="Alice"
    )

    assert authorization.status == "connected"
    assert authorization.github_username == "Alice"
    assert authorization.repository_count == 8
    assert client.urls == [
        "https://api.github.com/user/installations",
        "https://api.github.com/user/installations/1001/repositories",
    ]
    installation = asdict(authorization.installations[0])
    assert installation["account_type"] == "User"
    assert installation["repository_selection"] == "selected"
    assert installation["permissions"] == {"metadata": True, "administration": False}
    assert all("raw_unconstrained_field" not in row for row in installation["repositories"])


@pytest.mark.asyncio
async def test_credential_from_another_github_app_is_not_reused(monkeypatch) -> None:
    _configure_valid_user(monkeypatch, client_id="review-app")
    credential = SimpleNamespace(
        github_username="alice",
        revoked_at=None,
        github_app_client_id="star-aid-app",
    )
    monkeypatch.setattr(
        service_module.credential_service,
        "get_credential",
        lambda session, user_id: _await_value(credential),
    )

    authorization = await GitHubUserAuthorizationService().get_installations(
        session=SimpleNamespace(), user_id=7, expected_github_username="alice"
    )

    assert authorization.status == "needs_authorization"
    assert authorization.error_code == "credential_not_usable"
    assert authorization.installations == []


@pytest.mark.asyncio
async def test_transient_refresh_failure_is_an_error_not_authorization_loss(
    monkeypatch,
) -> None:
    _configure_valid_user(monkeypatch)
    monkeypatch.setattr(
        service_module.credential_service,
        "get_effective_access_token",
        lambda session, user_id: _await_value(
            (None, GitHubCallResult(error_code="refresh_network_error"))
        ),
    )

    authorization = await GitHubUserAuthorizationService().get_installations(
        session=SimpleNamespace(), user_id=7, expected_github_username="alice"
    )

    assert authorization.status == "error"
    assert authorization.error_code == "refresh_network_error"


def test_authorization_template_is_parseable_and_has_no_operations_actions() -> None:
    template_root = Path(__file__).resolve().parents[1] / "backend/webui/templates"
    fragment = (
        template_root / "components/github_app_list_fragment.html"
    ).read_text(encoding="utf-8")
    Environment().parse(fragment)

    assert "data-github-app-repository-filter" in fragment
    assert "loop.index0 >= 6" in fragment
    assert "data-github-app-toggle-repositories" in fragment
    assert "/repos/" not in fragment
    assert "triggerIndex" not in fragment
    assert "triggerScan" not in fragment


def test_connected_authorization_template_renders_with_strict_variables() -> None:
    template_root = Path(__file__).resolve().parents[1] / "backend/webui/templates"
    environment = Environment(
        loader=DictLoader({
            "base.html": (
                "{% block content %}{% endblock %}"
                "{% block extra_scripts %}{% endblock %}"
            ),
            "github_app.html": (template_root / "github_app.html").read_text(
                encoding="utf-8"
            ),
            "components/github_app_list_fragment.html": (
                template_root / "components/github_app_list_fragment.html"
            ).read_text(encoding="utf-8"),
        }),
        undefined=StrictUndefined,
        autoescape=True,
    )
    authorization = service_module.GitHubUserAuthorization(
        status="connected",
        github_username="alice",
        installations=[
            GitHubUserInstallation(
                installation_id=1001,
                account_login="alice",
                account_type="User",
                manage_url="https://github.com/settings/installations/1001",
                repositories=[
                    {
                        "id": 1,
                        "full_name": "alice/private",
                        "name": "private",
                        "private": True,
                        "html_url": "https://github.com/alice/private",
                    },
                    {
                        "id": 2,
                        "full_name": "alice/public",
                        "name": "public",
                        "private": False,
                        "html_url": "https://github.com/alice/public",
                    },
                ],
            )
        ],
    )

    output = environment.get_template("github_app.html").render(
        request=SimpleNamespace(query_params={}),
        current_user=SimpleNamespace(role="user"),
        active_page="github_app",
        authorization=authorization,
        authorization_url="/star-aid/auth/start",
        install_url="https://github.com/apps/sakura-ai/installations/new",
        error_code=None,
        _=lambda key, **_kwargs: key,
    )

    assert "alice/private" in output
    assert "github_app.manage_authorization" in output


def test_sidebar_exposes_github_app_to_authenticated_users() -> None:
    sidebar = (
        Path(__file__).resolve().parents[1]
        / "backend/webui/templates/components/sidebar.html"
    ).read_text(encoding="utf-8")
    assert '{% if current_user %}\n        <a href="/github-app/"' in sidebar
