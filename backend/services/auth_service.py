"""Authentication provider boundary for GitHub OAuth.

HTTP/provider concerns live here, while account matching and persistence are
delegated to :mod:`identity_service`.  Web and mobile callbacks can therefore
share exactly the same email selection and user creation semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from backend.core.config import Settings, get_settings
from backend.models.identity_models import AuthProvider
from backend.services.identity_service import GitHubAccount, upsert_github_account


class AuthProviderError(RuntimeError):
    """A provider request failed in a way suitable for the login surface."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class GitHubOAuthResult:
    """Normalized result of exchanging an OAuth authorization code."""

    account: GitHubAccount
    access_token: str


def select_github_email(
    profile: dict[str, Any], emails: list[dict[str, Any]] | None = None
) -> tuple[str | None, bool]:
    """Select a usable GitHub email, preferring verified primary addresses."""

    candidates: list[dict[str, Any]] = []
    profile_email = profile.get("email")
    if isinstance(profile_email, str) and profile_email.strip():
        candidates.append(
            {
                "email": profile_email,
                "primary": True,
                "verified": bool(profile.get("email_verified", False)),
            }
        )
    for item in emails or []:
        if isinstance(item, dict) and isinstance(item.get("email"), str):
            candidates.append(item)

    # Stable priority: verified primary > verified > primary > any address.
    ranked = sorted(
        candidates,
        key=lambda item: (
            bool(item.get("verified")) and bool(item.get("primary")),
            bool(item.get("verified")),
            bool(item.get("primary")),
        ),
        reverse=True,
    )
    for item in ranked:
        email = str(item.get("email") or "").strip().lower()
        if "@" in email and len(email) <= 320:
            return email, bool(item.get("verified"))
    return None, False


class GitHubOAuthProvider:
    """GitHub OAuth transport and profile normalization."""

    provider = AuthProvider.GITHUB.value

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    @staticmethod
    def _auth_headers(access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def exchange_code(self, code: str) -> GitHubOAuthResult:
        if not code:
            raise AuthProviderError("authorization code is missing")
        try:
            async with httpx.AsyncClient() as client:
                token_response = await client.post(
                    self.settings.github_oauth_token_url,
                    data={
                        "client_id": self.settings.github_oauth_client_id,
                        "client_secret": self.settings.github_oauth_client_secret,
                        "code": code,
                    },
                    headers={"Accept": "application/json"},
                    timeout=10,
                )
                if token_response.status_code != 200:
                    raise AuthProviderError(
                        "GitHub token exchange failed",
                        status_code=token_response.status_code,
                    )
                token_data = token_response.json()
                access_token = token_data.get("access_token")
                if not isinstance(access_token, str) or not access_token:
                    raise AuthProviderError("GitHub token response missing access token")

                user_response = await client.get(
                    self.settings.github_oauth_user_url,
                    headers=self._auth_headers(access_token),
                    timeout=10,
                )
                if user_response.status_code != 200:
                    raise AuthProviderError(
                        "GitHub user request failed",
                        status_code=user_response.status_code,
                    )
                profile = user_response.json()
                if not isinstance(profile, dict):
                    raise AuthProviderError("GitHub user response is invalid")

                emails: list[dict[str, Any]] = []
                # Email permission is deliberately best-effort.  A user who
                # declined user:email must still be able to log in.
                try:
                    email_response = await client.get(
                        getattr(
                            self.settings,
                            "github_oauth_emails_url",
                            "https://api.github.com/user/emails",
                        ),
                        headers=self._auth_headers(access_token),
                        timeout=10,
                    )
                    if email_response.status_code == 200:
                        payload = email_response.json()
                        if isinstance(payload, list):
                            emails = [item for item in payload if isinstance(item, dict)]
                except (httpx.TimeoutException, httpx.RequestError, ValueError):
                    logger.warning("GitHub email API unavailable; continuing without email")

        except AuthProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise AuthProviderError("GitHub request timed out") from exc
        except httpx.RequestError as exc:
            raise AuthProviderError("GitHub network request failed") from exc
        except (ValueError, TypeError) as exc:
            raise AuthProviderError("GitHub response is invalid") from exc

        username = profile.get("login")
        github_id = profile.get("id")
        if not isinstance(username, str) or not username.strip():
            raise AuthProviderError("GitHub user response missing login")
        if github_id is None:
            # This should not happen with GitHub, but keeps custom test/mocks
            # compatible while still producing a deterministic identity.
            provider_user_id = f"login:{username.strip().lower()}"
        else:
            provider_user_id = str(github_id)
        email, verified = select_github_email(profile, emails)
        return GitHubOAuthResult(
            account=GitHubAccount(
                provider_user_id=provider_user_id,
                username=username.strip(),
                avatar_url=(
                    str(profile.get("avatar_url"))
                    if profile.get("avatar_url")
                    else None
                ),
                email=email,
                email_verified=verified,
            ),
            access_token=access_token,
        )


class AuthService:
    """Provider-independent account authentication service."""

    async def authenticate_github(
        self,
        db,
        account: GitHubAccount,
        *,
        create_if_missing: bool = True,
    ):
        return await upsert_github_account(
            db, account, create_if_missing=create_if_missing
        )


auth_service = AuthService()
