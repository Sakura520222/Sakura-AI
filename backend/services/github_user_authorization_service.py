"""GitHub App user authorization and installation discovery.

This service reuses the encrypted GitHub App user-to-server credentials managed
by the Star Aid authorization flow. Installation data is always read through
GitHub's ``/user/installations`` endpoints so GitHub remains the authority for
what the currently authenticated GitHub user may see.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.services import star_aid_github_service as credential_service

_GITHUB_API_BASE = "https://api.github.com"
_GITHUB_API_VERSION = "2022-11-28"
_REQUEST_TIMEOUT = 15
_PAGE_SIZE = 100
_INSTALLATION_FETCH_CONCURRENCY = 4
_DISCOVERY_TIMEOUT_SECONDS = _REQUEST_TIMEOUT


@dataclass(frozen=True)
class GitHubUserInstallation:
    """A constrained installation DTO rendered by the authorization center."""

    installation_id: int
    account_login: str
    account_type: str
    account_avatar_url: str = ""
    repository_selection: str = "selected"
    manage_url: str = ""
    permissions: dict[str, bool] = field(default_factory=dict)
    repositories: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class GitHubUserAuthorization:
    """Authorization-center state without tokens or raw GitHub payloads."""

    status: str
    github_username: str = ""
    installations: list[GitHubUserInstallation] = field(default_factory=list)
    error_code: str | None = None

    @property
    def repository_count(self) -> int:
        return sum(
            len(installation.repositories) for installation in self.installations
        )


class _GitHubUserAPIError(Exception):
    def __init__(self, status_code: int, error_code: str):
        super().__init__(f"GitHub user API failed: HTTP {status_code}")
        self.status_code = status_code
        self.error_code = error_code


def _safe_github_url(value: Any) -> str:
    """Only allow GitHub URLs into page templates."""
    if not isinstance(value, str):
        return ""
    if not value.startswith(
        ("https://github.com/", "https://avatars.githubusercontent.com/")
    ):
        return ""
    return value


def _permissions(payload: dict[str, Any]) -> dict[str, bool]:
    raw = payload.get("permissions")
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): value is True
        for name, value in raw.items()
        if isinstance(name, str) and isinstance(value, bool)
    }


def _fallback_manage_url(
    installation_id: int, account_login: str, account_type: str
) -> str:
    if account_type.casefold() == "organization" and account_login:
        return (
            "https://github.com/organizations/"
            f"{account_login}/settings/installations/{installation_id}"
        )
    return f"https://github.com/settings/installations/{installation_id}"


class GitHubUserAuthorizationService:
    """Read user-scoped GitHub App installations from existing user tokens."""

    async def get_installations(
        self,
        session: AsyncSession,
        user_id: int,
        expected_github_username: str | None = None,
    ) -> GitHubUserAuthorization:
        settings = get_settings()
        client_id = settings.star_aid_github_app_client_id
        callback_url = settings.star_aid_github_app_callback_url
        if not client_id or not settings.star_aid_github_app_client_secret:
            return GitHubUserAuthorization(
                status="unconfigured", error_code="app_not_configured"
            )
        authorization_flow_configured = bool(callback_url)

        credential = await credential_service.get_credential(session, int(user_id))
        github_username = (credential.github_username or "") if credential else ""
        expected = expected_github_username or ""
        credential_matches_user = not expected or (
            github_username.casefold() == expected.casefold()
        )
        credential_matches_app = bool(credential) and (
            credential.github_app_client_id == client_id
        )

        if (
            credential is None
            or credential.revoked_at is not None
            or not credential_matches_app
            or not credential_matches_user
        ):
            if not authorization_flow_configured:
                return GitHubUserAuthorization(
                    status="unconfigured",
                    github_username=expected or github_username,
                    error_code="app_not_configured",
                )
            return GitHubUserAuthorization(
                status="needs_authorization",
                github_username=expected or github_username,
                error_code="credential_not_usable",
            )

        access_token, token_result = await credential_service.get_effective_access_token(
            session, int(user_id)
        )
        if not access_token:
            status = "needs_authorization" if token_result.reauth_required else "error"
            if token_result.reauth_required and not authorization_flow_configured:
                status = "unconfigured"
            if token_result.reauth_required:
                # get_effective_access_token() may have marked the credential and
                # member as reauthorization-required before returning no token.
                await session.commit()
            return GitHubUserAuthorization(
                status=status,
                github_username=expected or github_username,
                error_code=(
                    "app_not_configured"
                    if status == "unconfigured"
                    else token_result.error_code or "token_unavailable"
                ),
            )

        # GitHub rotates the refresh token during get_effective_access_token().
        # Persist that rotation before any installation discovery request can fail;
        # otherwise rollback would discard the only still-valid refresh token.
        await session.commit()

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        }
        try:
            async with asyncio.timeout(_DISCOVERY_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                    installations_payload = await self._paginate(
                        client,
                        f"{_GITHUB_API_BASE}/user/installations",
                        headers,
                        "installations",
                    )
                    # Repository discovery is secondary fan-out for every account
                    # installation. Bound it and keep the whole page under one
                    # deadline so a large account cannot extend this request by
                    # one HTTP timeout per GitHub call.
                    semaphore = asyncio.Semaphore(_INSTALLATION_FETCH_CONCURRENCY)

                    async def fetch_installation(
                        raw_installation: dict[str, Any],
                    ) -> GitHubUserInstallation:
                        async with semaphore:
                            return await self._installation_with_repositories(
                                client, raw_installation, headers
                            )

                    fetched: list[GitHubUserInstallation | BaseException] = (
                        await asyncio.gather(
                            *(
                                asyncio.create_task(fetch_installation(raw))
                                for raw in installations_payload
                            ),
                            return_exceptions=True,
                        )
                    )
                    installations: list[GitHubUserInstallation] = []
                    for result in fetched:
                        if isinstance(result, BaseException):
                            raise result
                        installations.append(result)
        except _GitHubUserAPIError as exc:
            if exc.status_code == 401:
                await credential_service.mark_reauth_required(session, int(user_id))
                # The token is definitively rejected by GitHub. Persist the shared
                # credential/member state so later page loads and workers stop
                # treating the revoked token as usable.
                await session.commit()
                status = (
                    "needs_authorization"
                    if authorization_flow_configured
                    else "unconfigured"
                )
            else:
                status = "error"
            logger.warning(
                "GitHub App user installations request failed: "
                "user_id={}, status={}, error={}",
                user_id,
                exc.status_code,
                exc.error_code,
            )
            return GitHubUserAuthorization(
                status=status,
                github_username=expected or github_username,
                error_code=(
                    "app_not_configured"
                    if status == "unconfigured"
                    else exc.error_code
                ),
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning(
                "GitHub App user installations network error: user_id={}, error={}",
                user_id,
                type(exc).__name__,
            )
            return GitHubUserAuthorization(
                status="error",
                github_username=expected or github_username,
                error_code="github_unavailable",
            )
        except TimeoutError:
            logger.warning(
                "GitHub App user installations discovery timed out: user_id={}",
                user_id,
            )
            return GitHubUserAuthorization(
                status="error",
                github_username=expected or github_username,
                error_code="github_unavailable",
            )

        return GitHubUserAuthorization(
            status="connected",
            github_username=expected or github_username,
            installations=installations,
        )

    async def _paginate(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        items_key: str,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        first_page = True
        while next_url:
            params = {"per_page": _PAGE_SIZE} if first_page else None
            response = await client.get(next_url, headers=headers, params=params)
            if response.status_code != 200:
                error_code = (
                    "unauthorized"
                    if response.status_code == 401
                    else "github_request_failed"
                )
                raise _GitHubUserAPIError(response.status_code, error_code)
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(
                payload.get(items_key), list
            ):
                raise _GitHubUserAPIError(response.status_code, "invalid_github_response")
            page_items = payload[items_key]
            if not all(isinstance(item, dict) for item in page_items):
                raise _GitHubUserAPIError(response.status_code, "invalid_github_response")
            items.extend(page_items)
            next_url = response.links.get("next", {}).get("url")
            first_page = False
        return items

    async def _installation_with_repositories(
        self,
        client: httpx.AsyncClient,
        raw_installation: dict[str, Any],
        headers: dict[str, str],
    ) -> GitHubUserInstallation:
        try:
            installation_id = int(raw_installation["id"])
        except (KeyError, TypeError, ValueError):
            raise _GitHubUserAPIError(200, "invalid_installation_id") from None

        account = raw_installation.get("account")
        if not isinstance(account, dict):
            account = {}
        account_login = str(account.get("login") or "")
        account_type = str(raw_installation.get("target_type") or "")
        repository_selection = (
            "all"
            if raw_installation.get("repository_selection") == "all"
            else "selected"
        )
        manage_url = _safe_github_url(raw_installation.get("html_url"))
        if not manage_url:
            manage_url = _fallback_manage_url(
                installation_id, account_login, account_type
            )

        raw_repositories = await self._paginate(
            client,
            f"{_GITHUB_API_BASE}/user/installations/{installation_id}/repositories",
            headers,
            "repositories",
        )
        repositories: list[dict[str, Any]] = []
        for raw_repository in raw_repositories:
            try:
                private = raw_repository.get("private") is True
                raw_visibility = str(raw_repository.get("visibility") or "").lower()
                if raw_visibility in {"public", "private", "internal"}:
                    visibility = raw_visibility
                else:
                    visibility = "private" if private else "public"
                repositories.append(
                    {
                        "id": int(raw_repository["id"]),
                        "full_name": str(raw_repository["full_name"]),
                        "name": str(raw_repository["name"]),
                        "private": private,
                        "visibility": visibility,
                        "html_url": _safe_github_url(raw_repository.get("html_url")),
                    }
                )
            except (KeyError, TypeError, ValueError):
                raise _GitHubUserAPIError(
                    200, "invalid_repository_response"
                ) from None

        return GitHubUserInstallation(
            installation_id=installation_id,
            account_login=account_login,
            account_type=account_type,
            account_avatar_url=_safe_github_url(account.get("avatar_url")),
            repository_selection=repository_selection,
            manage_url=manage_url,
            permissions=_permissions(raw_installation),
            repositories=repositories,
        )
