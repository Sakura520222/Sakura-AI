"""Local browser fixture for the real notification pages/API and SQLite.

Run uv run python -m tests.notification_ui_app_646. Authentication is explicitly
a local test double. No AI/GitHub/SMTP/Telegram or production database is used.
"""
import asyncio

import uvicorn
from fastapi import FastAPI, Request
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.models import Base
from backend.models import database as db_module
from backend.models.identity_models import NotificationEndpoint
from backend.models.telegram_models import TelegramUser
from backend.services.notification_events import emit_event
from backend.services.notification_service import notification_service
from backend.webui.deps import (
    get_db,
    get_user_preferences,
    require_auth,
    require_super_admin,
)
from backend.webui.routes import announcements, notifications, settings
from tests.test_notification_events_646 import AsyncDB

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
db_module.async_session = lambda: AsyncDB(engine)


async def seed():
    async with AsyncDB(engine) as db:
        db.add(TelegramUser(id=1, github_username="alice", role="super_admin", is_active=True))
        db.add(NotificationEndpoint(id=1, user_id=1, provider="email", address="alice@example.org", name="Personal email", enabled=True, verified=True))
        await db.commit()
        item = await emit_event(db, event_type="review.completed", resource_type="review", resource_id=646,
            run_key="browser-fixture", title="Sakura-AI PR #646 review completed", content="The review is complete. Open the review to inspect the result.",
            repo_name="alice/Sakura-AI", recipient_ids=[1], detail_url="/pr/646")
        await db.commit()
        await notification_service.materialize_event(db, item.id)


asyncio.run(seed())
app = FastAPI()
app.include_router(settings.router)
app.include_router(announcements.router)
app.include_router(notifications.router)


async def identity():
    return {"user_id": 1, "sub": "alice", "role": "super_admin"}


async def preferences(request: Request):
    return {"language": request.query_params.get("lang", "zh-CN"), "theme": "light", "items_per_page": 20}


async def database():
    async with AsyncDB(engine) as db:
        yield db


app.dependency_overrides[require_auth] = identity
app.dependency_overrides[require_super_admin] = identity
app.dependency_overrides[get_user_preferences] = preferences
app.dependency_overrides[get_db] = database

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8646)
