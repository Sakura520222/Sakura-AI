"""GitHub App authorization-center security and behavior regressions."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest
import yaml
from fastapi.routing import APIRoute
from jinja2 import DictLoader, Environment, StrictUndefined

from backend.core.config import CORE_CONFIG_KEYS, get_all_db_config_keys
from backend.services import github_user_authorization_service as service_module
from backend.services.github_user_authorization_service import (
    GitHubUserAuthorizationService,
    GitHubUserInstallation,
)
from backend.services.star_aid_github_service import GitHubCallResult
from backend.webui.deps import require_admin, require_super_admin
from backend.webui.routes import github_app, repos, star_aid, system_config


def _route(router: Any, path: str, method: str = "GET") -> APIRoute:
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return route
    raise AssertionError(f"Route {method} {path} was not found")


def _dependency_calls(route: APIRoute) -> list[Any]:
    return [dependency.call for dependency in route.dependant.dependencies]


class _JsonRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


class _FormRequest:
    def __init__(self, form: dict) -> None:
        self._form = form

    async def form(self) -> dict:
        return self._form


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


def test_user_authorization_app_slug_is_loaded_from_database() -> None:
    key = "star_aid_github_app_slug"

    assert key in CORE_CONFIG_KEYS
    assert key in get_all_db_config_keys()
    assert system_config.system_config_service.validate_updates({key: ""}) == {key: ""}


def test_user_authorization_routes_do_not_accept_installation_ids() -> None:
    """GitHub user token scoping, rather than a client ID parameter, is authoritative."""
    paths = {route.path for route in github_app.router.routes}
    assert paths == {"/github-app/", "/github-app/list-fragment"}
    assert all("installation_id" not in path for path in paths)


def test_fragment_refresh_preserves_user_language_dependency() -> None:
    dependencies = _dependency_calls(
        _route(github_app.router, "/github-app/list-fragment")
    )

    assert github_app.get_user_preferences in dependencies


@pytest.mark.asyncio
async def test_fragment_refresh_passes_user_preferences_to_renderer(
    monkeypatch,
) -> None:
    captured = {}

    async def authorization_state(db: Any, user: dict) -> tuple[Any, bool, None]:
        return None, False, None

    async def install_url() -> None:
        return None

    def render_template(
        template_name: str,
        request: Any,
        user_prefs: dict | None = None,
        **context: Any,
    ):
        captured.update({"template": template_name, "prefs": user_prefs, **context})
        return "rendered"

    monkeypatch.setattr(github_app, "_authorization_state", authorization_state)
    monkeypatch.setattr(github_app, "_get_install_url", install_url)
    monkeypatch.setattr(github_app, "render_template", render_template)

    result = await github_app.list_fragment(
        request=SimpleNamespace(query_params={}),
        db=SimpleNamespace(),
        user={"user_id": 7, "sub": "alice", "role": "user"},
        user_prefs={"language": "en", "items_per_page": 20},
    )

    assert result == "rendered"
    assert captured["template"] == "components/github_app_list_fragment.html"
    assert captured["prefs"] == {"language": "en", "items_per_page": 20}
    assert captured["error_code"] is None


class _Response:
    def __init__(
        self,
        payload: Any,
        links: dict[str, Any] | None = None,
        status_code: int = 200,
    ):
        self.status_code = status_code
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


@pytest.mark.asyncio
async def test_configured_separate_app_slug_builds_install_url_without_main_app(
    monkeypatch,
) -> None:
    monkeypatch.setattr(github_app, "_app_slug", None)
    monkeypatch.setattr(
        github_app,
        "get_settings",
        lambda: SimpleNamespace(
            star_aid_github_app_slug="separate-app",
            star_aid_github_app_client_id="Iv1.separate",
        ),
    )

    url = await github_app._get_install_url()

    assert url == "https://github.com/apps/separate-app/installations/new"


@pytest.mark.asyncio
async def test_separate_app_without_slug_does_not_fall_back_to_review_app(
    monkeypatch,
) -> None:
    from backend.core import github_app as core_github_app

    class FakeMainApp:
        def get_app_identity(self) -> tuple[str, str]:
            return ("review-app", "Iv1.review")

    monkeypatch.setattr(github_app, "_app_slug", None)
    monkeypatch.setattr(github_app, "get_settings", lambda: SimpleNamespace(
        star_aid_github_app_slug="",
        star_aid_github_app_client_id="Iv1.separate",
    ))
    monkeypatch.setattr(core_github_app, "GitHubAppClient", FakeMainApp)

    assert await github_app._get_install_url() is None


@pytest.mark.asyncio
async def test_inferred_install_url_cache_is_scoped_to_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.core import github_app as core_github_app

    class FakeMainApp:
        def get_app_identity(self) -> tuple[str, str]:
            return ("review-app", "Iv1.review")

    settings = SimpleNamespace(
        star_aid_github_app_slug="",
        star_aid_github_app_client_id="Iv1.review",
    )
    monkeypatch.setattr(github_app, "_app_slug", None)
    monkeypatch.setattr(github_app, "_app_slug_client_id", None)
    monkeypatch.setattr(github_app, "get_settings", lambda: settings)
    monkeypatch.setattr(core_github_app, "GitHubAppClient", FakeMainApp)

    first_url = await github_app._get_install_url()
    settings.star_aid_github_app_client_id = "Iv1.separate"
    second_url = await github_app._get_install_url()

    assert first_url == "https://github.com/apps/review-app/installations/new"
    assert second_url is None


@pytest.mark.asyncio
async def test_denied_authorization_returns_to_authorization_center(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_states: list[str] = []

    async def get_current_user(request: Any) -> dict:
        return {"user_id": 7, "sub": "alice"}

    async def get_auth_state(state: str) -> dict:
        assert state == "state-token"
        return {
            "user_id": 7,
            "intent": "github_app",
            "return_to": "/github-app/",
        }

    async def delete_auth_state(state: str) -> None:
        deleted_states.append(state)

    captured = {}

    def toast_redirect(path: str, toast_key: str, **kwargs: Any):
        captured.update({"path": path, "toast_key": toast_key, **kwargs})
        return SimpleNamespace(path=path)

    monkeypatch.setattr(star_aid, "get_current_user", get_current_user)
    monkeypatch.setattr(star_aid, "_get_auth_state", get_auth_state)
    monkeypatch.setattr(star_aid, "_delete_auth_state", delete_auth_state)
    monkeypatch.setattr(star_aid, "toast_redirect", toast_redirect)

    response = await star_aid.auth_callback(
        request=SimpleNamespace(),
        code=None,
        state="state-token",
        error="access_denied",
        error_description="The user has denied your application access",
        setup_action=None,
    )

    assert response.path == "/github-app/"
    assert captured["toast_key"] == "star_aid.auth_denied"
    assert captured["toast_type"] == "error"
    assert deleted_states == ["state-token"]


class _Session:
    def __init__(self):
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


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
        session=_Session(), user_id=7, expected_github_username="Alice"
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
        session=_Session(), user_id=7, expected_github_username="alice"
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
        session=_Session(), user_id=7, expected_github_username="alice"
    )

    assert authorization.status == "error"
    assert authorization.error_code == "refresh_network_error"


@pytest.mark.asyncio
async def test_rotated_refresh_token_is_persisted_before_discovery_failure(
    monkeypatch,
) -> None:
    _configure_valid_user(monkeypatch)

    class InvalidPayloadClient(_AsyncClient):
        async def get(self, url: str, **_kwargs: Any) -> _Response:
            self.urls.append(url)
            if url.endswith("/user/installations"):
                return _Response([])
            raise AssertionError(f"Unexpected GitHub URL: {url}")

    session = _Session()
    client = InvalidPayloadClient()
    monkeypatch.setattr(
        service_module.httpx, "AsyncClient", lambda timeout=None: client
    )

    authorization = await GitHubUserAuthorizationService().get_installations(
        session=session, user_id=7, expected_github_username="alice"
    )

    assert authorization.status == "error"
    assert authorization.error_code == "invalid_github_response"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_github_401_persists_reauthorization_state(monkeypatch) -> None:
    _configure_valid_user(monkeypatch)

    class UnauthorizedClient(_AsyncClient):
        async def get(self, url: str, **_kwargs: Any) -> _Response:
            self.urls.append(url)
            return _Response({}, status_code=401)

    marked = []

    async def mark_reauth_required(session: Any, user_id: int) -> None:
        marked.append(user_id)

    monkeypatch.setattr(
        service_module.credential_service,
        "mark_reauth_required",
        mark_reauth_required,
    )
    session = _Session()
    monkeypatch.setattr(
        service_module.httpx,
        "AsyncClient",
        lambda timeout=None: UnauthorizedClient(),
    )

    authorization = await GitHubUserAuthorizationService().get_installations(
        session=session, user_id=7, expected_github_username="alice"
    )

    assert authorization.status == "needs_authorization"
    assert authorization.error_code == "unauthorized"
    assert marked == [7]
    assert session.commits == 2


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


def test_error_authorization_renders_failure_not_connected_state() -> None:
    template_root = Path(__file__).resolve().parents[1] / "backend/webui/templates"
    environment = Environment(
        loader=DictLoader({
            "components/github_app_list_fragment.html": (
                template_root / "components/github_app_list_fragment.html"
            ).read_text(encoding="utf-8")
        }),
        autoescape=True,
    )
    authorization = service_module.GitHubUserAuthorization(
        status="error", github_username="alice", error_code="github_request_failed"
    )

    output = environment.get_template(
        "components/github_app_list_fragment.html"
    ).render(
        authorization=authorization,
        authorization_url=None,
        install_url=None,
        error_code=None,
        _=lambda key, **_kwargs: key,
    )

    assert "github_app.request_failed" in output
    assert "github_app.connected" not in output


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


def test_system_config_translations_cover_shared_user_authorization_app() -> None:
    translation_root = (
        Path(__file__).resolve().parents[1] / "backend/webui/translations"
    )
    expected_keys = {
        "group_star_aid_app_desc",
        "key_star_aid_github_app_client_id_desc",
        "key_star_aid_github_app_client_secret_desc",
        "key_star_aid_github_app_slug",
        "key_star_aid_github_app_slug_desc",
        "key_star_aid_github_app_callback_url_desc",
    }

    for filename in ("en.yaml", "zh-CN.yaml"):
        payload = yaml.safe_load(
            (translation_root / filename).read_text(encoding="utf-8")
        )
        assert expected_keys <= payload["system_config"].keys()


def test_github_app_slug_fetch_endpoint_keeps_admin_and_csrf_boundaries() -> None:
    route = _route(
        system_config.router,
        "/system-config/github-app-slug",
        method="POST",
    )
    calls = _dependency_calls(route)

    assert system_config.require_super_admin in calls
    assert system_config.require_csrf_header in calls


@pytest.mark.asyncio
async def test_github_app_slug_fetch_reuses_review_app_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(client_id: str) -> tuple[str | None, str | None]:
        assert client_id == "Iv1.review"
        return "review-app", None

    monkeypatch.setattr(system_config, "_resolve_review_github_app_slug", resolve)

    result = await system_config.fetch_github_app_slug(
        _JsonRequest({"clientId": "Iv1.review"}),
        _user={"user_id": 1, "sub": "alice"},
    )

    assert result["success"] is True
    assert result["slug"] == "review-app"


@pytest.mark.asyncio
async def test_github_app_slug_fetch_rejects_separate_app_without_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(client_id: str) -> tuple[str | None, str | None]:
        return None, "github_app_slug_requires_manual_entry"

    monkeypatch.setattr(system_config, "_resolve_review_github_app_slug", resolve)

    result = await system_config.fetch_github_app_slug(
        _JsonRequest({"clientId": "Iv1.separate"}),
        _user={"user_id": 1, "sub": "alice"},
    )

    assert result["success"] is False
    assert result["error_code"] == "github_app_slug_requires_manual_entry"
    assert "slug" not in result


def test_system_config_slug_field_has_dedicated_auto_fetch_button() -> None:
    template = (
        Path(__file__).resolve().parents[1]
        / "backend/webui/templates/system_config.html"
    ).read_text(encoding="utf-8")

    assert 'x-ref="githubAppSlugInput"' in template
    assert "fetchGithubAppSlug($refs.githubAppSlugInput)" in template
    assert template.count("fetchGithubAppSlug(") >= 2
    assert 'name="star_aid_github_app_slug_changed"' in template
    assert (
        'x-on:input="$refs.star_aid_github_app_slug_changed.value = \'true\'"'
        in template
    )


@pytest.mark.asyncio
async def test_system_config_slug_can_be_explicitly_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "star_aid_github_app_slug"

    class FakeConfigService:
        def __init__(self) -> None:
            self.validated: list[dict[str, str]] = []
            self.applied: list[dict[str, dict[str, str]]] = []
            self.save_calls = 0

        def validate_updates(self, updates: dict[str, str]) -> dict[str, str]:
            self.validated.append(dict(updates))
            return dict(updates)

        async def save_configs(self, db: Any, updates: dict[str, str]):
            self.save_calls += 1
            assert updates == {key: ""}
            return ({key: {"old": "old-app", "new": "", "raw_new": ""}}, False)

        async def apply_live_settings(self, changed: dict) -> None:
            self.applied.append(changed)

        def build_audit_log(self, changed: dict) -> dict:
            return changed

    service = FakeConfigService()
    admin_logs = []

    async def log_admin_action(*args: Any, **kwargs: Any) -> None:
        admin_logs.append((args, kwargs))

    redirects = []

    def toast_redirect(path: str, toast_key: str, *args: Any, **kwargs: Any):
        redirects.append(toast_key)
        return SimpleNamespace(path=path, toast_key=toast_key)

    monkeypatch.setattr(system_config, "system_config_service", service)
    monkeypatch.setattr(system_config, "log_admin_action", log_admin_action)
    monkeypatch.setattr(system_config, "toast_redirect", toast_redirect)

    cleared = await system_config.save_system_config(
        request=_FormRequest({key: "", f"{key}_changed": "true"}),
        db=SimpleNamespace(),
        user={"user_id": 1, "sub": "alice"},
        csrf_token="valid",
    )
    untouched = await system_config.save_system_config(
        request=_FormRequest({key: "", f"{key}_changed": "false"}),
        db=SimpleNamespace(),
        user={"user_id": 1, "sub": "alice"},
        csrf_token="valid",
    )

    assert cleared.toast_key == "system_config.saved"
    assert untouched.toast_key == "toast.config_no_change"
    assert service.validated == [{key: ""}, {}]
    assert service.save_calls == 1
    assert service.applied == [{key: {"old": "old-app", "new": "", "raw_new": ""}}]
    assert len(admin_logs) == 1
