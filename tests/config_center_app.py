"""Local E2E fixture: real routes and SQLite, isolated from deployment services.

Run only via the Playwright test runner. Its auth overrides are never mounted
by the production app and no test request can reach GitHub/Redis/AI services.
"""

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.v1 import config, config_center
from backend.api.v1.deps import require_api_auth, require_api_super_admin
from backend.core.config import get_settings
from backend.models.database import (
    AppConfig,
    Base,
    ConfigRevision,
    UserConfig,
    WebUIConfig,
)
from backend.webui.deps import (
    get_db,
    get_user_preferences,
    require_auth,
    require_super_admin,
)
from backend.webui.routes import config as legacy_config
from backend.webui.routes import config_center as pages
from backend.webui.routes import notifications, settings, system_config

temporary = tempfile.TemporaryDirectory(prefix="sakura-settings-e2e-")
engine = create_async_engine(
    f"sqlite+aiosqlite:///{Path(temporary.name) / 'test.sqlite'}"
)
sessions = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def lifespan(app):
    from backend.models.admin_action_log import AdminActionLog
    from backend.models.identity_models import NotificationEndpoint
    from backend.models.notification_models import NotificationSubscription
    from backend.models.telegram_models import (
        TelegramUser,
        UserRecoveryCode,
        UserWebAuthnCredential,
    )

    tables = [
        AppConfig.__table__,
        ConfigRevision.__table__,
        UserConfig.__table__,
        WebUIConfig.__table__,
        AdminActionLog.__table__,
        TelegramUser.__table__,
        UserRecoveryCode.__table__,
        UserWebAuthnCredential.__table__,
        NotificationEndpoint.__table__,
        NotificationSubscription.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda conn: Base.metadata.create_all(conn, tables=tables)
        )
    async with sessions() as db:
        db.add_all(
            [
                TelegramUser(id=1, github_username="admin", role="super_admin"),
                TelegramUser(id=2, github_username="user", role="user"),
            ]
        )
        await db.commit()
    from backend.models import database

    database.async_session = sessions
    get_settings().webui_secret_key = "isolated-e2e-session-signing-key"
    from unittest.mock import AsyncMock, patch

    with (
        patch.object(
            legacy_config,
            "read_sandbox_capability_status",
            AsyncMock(return_value={"available": False, "egress_capability": "none"}),
        ),
        patch.object(
            system_config,
            "_schedule_application_restart",
            side_effect=RuntimeError("Disabled in E2E fixture"),
        ),
        patch.object(
            system_config.database_reset_service,
            "reset",
            AsyncMock(side_effect=RuntimeError("Disabled in E2E fixture")),
        ),
    ):
        yield
    await engine.dispose()
    temporary.cleanup()


app = FastAPI(lifespan=lifespan)
app.include_router(notifications.router)
app.include_router(pages.router)
app.include_router(legacy_config.router)
app.include_router(settings.router)
app.include_router(system_config.router)
app.include_router(config_center.router, prefix="/api/v1")
app.include_router(config.router, prefix="/api/v1")


async def session():
    async with sessions() as db:
        yield db


async def user(request: Request):
    ordinary = request.cookies.get("test-role") == "user"
    return {
        "user_id": 2 if ordinary else 1,
        "sub": "user" if ordinary else "admin",
        "role": "user" if ordinary else "super_admin",
    }


async def admin(request: Request):
    actor = await user(request)
    if actor["role"] != "super_admin":
        raise HTTPException(403)
    return actor


async def preferences(request: Request):
    actor = await user(request)
    from sqlalchemy import select

    async with sessions() as db:
        row = (
            await db.execute(
                select(WebUIConfig).where(WebUIConfig.user_id == actor["user_id"])
            )
        ).scalar_one_or_none()
        return {
            "language": row.language or "zh-CN" if row else "zh-CN",
            "items_per_page": row.items_per_page or 20 if row else 20,
        }


app.dependency_overrides.update(
    {
        get_db: session,
        require_auth: user,
        require_super_admin: admin,
        require_api_auth: user,
        require_api_super_admin: admin,
        get_user_preferences: preferences,
    }
)
