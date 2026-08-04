"""Small in-process repository scope adapter for activity observability.

The full repository policy remains application-owned.  This adapter reuses the
existing WebUI task checks where a stable external resource identity maps to a
legacy PR/Issue/scan record; callers can inject a richer implementation.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.activity_observability_models import (
    ActivityObservabilitySession,
    ActivityResourceIdentity,
)
from backend.models.database import IssueAnalysis, PRReview
from backend.models.scan_models import RepoScan


class LegacyRepositoryScopeAuthorizer:
    """Repository scope checks without role-only admin bypass."""

    @staticmethod
    def _github_username(user: dict) -> str:
        return str(user.get("github_username") or user.get("sub") or "").strip()

    async def authorize_session(
        self, db: AsyncSession, *, session: ActivityObservabilitySession, user: dict
    ) -> bool:
        # 避免触发同步懒加载：直接按外键 id 显式查询 resource_identity。
        # 当上游已 selectinload 该关系时，db.get 命中 identity map 几乎零成本；
        # 当 session 由 db.get（未 eager load）查出时（如 resolve_recipients /
        # is_authorized 路径），避免在 async 上下文触发 MissingGreenlet，
        # 该异常曾导致 PR 审查直接失败、Issue 实时监控无法启动。
        identity = await db.get(ActivityResourceIdentity, session.resource_identity_id)
        if identity is None:
            return False
        role = identity.resource_type.lower()
        try:
            number = int(identity.resource_number)
        except TypeError, ValueError:
            return False
        username = self._github_username(user)
        is_admin = user.get("role") in {"admin", "super_admin"}
        if role == "pr":
            model = PRReview
            query = select(model).where(
                model.pr_number == number,
                model.repo_name == identity.repo_full_name.rsplit("/", 1)[-1],
                model.repo_owner == identity.repo_full_name.split("/", 1)[0],
            )
            if not is_admin:
                query = query.where(
                    or_(model.repo_owner == username, model.author == username)
                )
            return (await db.execute(query)).scalars().first() is not None
        if role == "issue":
            model = IssueAnalysis
            owner, _, repo = identity.repo_full_name.partition("/")
            query = select(model).where(
                model.issue_number == number,
                model.repo_owner == owner,
                model.repo_name == repo,
            )
            if not is_admin:
                query = query.where(
                    or_(model.repo_owner == username, model.author == username)
                )
            return (await db.execute(query)).scalars().first() is not None
        if role == "ephemeral":
            query = select(RepoScan).where(RepoScan.id == number)
            if user.get("role") not in {"admin", "super_admin"}:
                query = query.where(RepoScan.repo_owner == user.get("sub"))
            return (await db.execute(query)).scalars().first() is not None
        return False

    async def authorization_version(self, db: AsyncSession, *, user: dict) -> str:
        return str(
            user.get("auth_version")
            or f"user:{user.get('user_id', user.get('sub', ''))}"
        )

    async def may_view_trace(
        self, db: AsyncSession, *, session: ActivityObservabilitySession, user: dict
    ) -> bool:
        return user.get("role") in {"admin", "super_admin"}

    async def is_authorized(
        self, db: AsyncSession, *, user_id: str, session_id: int
    ) -> bool:
        from backend.models.telegram_models import TelegramUser

        result = await db.execute(
            select(TelegramUser).where(
                TelegramUser.id == user_id, TelegramUser.is_active
            )
        )
        account = result.scalar_one_or_none()
        if account is None:
            result = await db.execute(
                select(TelegramUser).where(
                    TelegramUser.github_username == user_id, TelegramUser.is_active
                )
            )
            account = result.scalar_one_or_none()
        if account is None:
            return False
        user = {
            "user_id": str(account.id),
            "sub": account.github_username,
            "github_username": account.github_username,
            "role": account.role,
        }
        session = await db.get(ActivityObservabilitySession, session_id)
        return bool(
            session and await self.authorize_session(db, session=session, user=user)
        )

    async def resolve_recipients(
        self,
        db: AsyncSession,
        *,
        session_id: int,
        event,
        visibility: str,
        payload: dict,
    ) -> tuple[str, ...]:
        """Resolve only active accounts currently authorized for this resource."""
        del event, payload
        if visibility == "hidden":
            return ()
        from backend.models.telegram_models import TelegramUser

        session = await db.get(ActivityObservabilitySession, session_id)
        if session is None:
            return ()
        try:
            accounts = (
                (await db.execute(select(TelegramUser).where(TelegramUser.is_active)))
                .scalars()
                .all()
            )
        except SQLAlchemyError:
            # Bootstrap/migration and isolated model tests may not have the
            # account table yet.  No audience is safer than a global fallback.
            return ()
        recipients: list[str] = []
        for account in accounts:
            role = str(account.role or "")
            if visibility == "admin_only" and role not in {"admin", "super_admin"}:
                continue
            user = {
                "user_id": str(account.id),
                "sub": account.github_username,
                "github_username": account.github_username,
                "role": role,
            }
            if await self.authorize_session(db, session=session, user=user):
                recipients.append(str(account.id))
        return tuple(recipients)


__all__ = ["LegacyRepositoryScopeAuthorizer"]
