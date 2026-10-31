"""React browser fixture using the real configuration API and isolated SQLite.

Only the test runner imports this module. Production never mounts test auth.
External providers and deployment operations remain disabled by the base fixture.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.api.v1 import (
    agent_management,
    auth_webui,
    notifications,
    personal_security,
    queue,
    scans,
    settings_webui,
    webui,
)
from backend.api.v1.deps import require_api_admin
from backend.core.config import get_settings
from backend.core.time_service import monotonic
from backend.models.agent_skill_models import AgentSkill
from backend.models.database import Base, ReviewQueue
from backend.models.notification_models import NotificationLockoutRound
from backend.models.scan_models import RepoScan, ScanFinding
from backend.services import mfa_lockout_service, webauthn_service
from backend.services.agent_team.skill_service import AgentSkillService
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService
from backend.webui.routes import spa
from tests import config_center_app as fixture
from tests.config_center_app import admin, app, user

_real_list_skills = AgentSkillService.list_skills


class FixtureRedis:
    """Only WebAuthn/TOTP challenge and lockout keys; never a deployment Redis."""

    def __init__(self):
        self.values = {}

    async def setex(self, key, ttl, value):
        self.values[key] = (value, monotonic() + ttl)

    async def get(self, key):
        value, deadline = self.values.get(key, (None, 0))
        return value if deadline > monotonic() else None

    async def execute_command(self, command, key):
        assert command == "GETDEL"
        value = await self.get(key)
        self.values.pop(key, None)
        return value

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)

    async def ttl(self, key):
        return max(-2, int(self.values.get(key, (None, 0))[1] - monotonic()))

    async def incr(self, key):
        value = int(await self.get(key) or 0) + 1
        self.values[key] = (value, self.values.get(key, (None, float("inf")))[1])
        return value

    async def expire(self, key, ttl):
        if key in self.values:
            self.values[key] = (self.values[key][0], monotonic() + ttl)

app.include_router(webui.router, prefix="/api/v1")
app.include_router(spa.router)
app.include_router(auth_webui.router, prefix="/api/v1")
app.include_router(personal_security.router, prefix="/api/v1")
app.include_router(notifications.router, prefix="/api/v1")
app.include_router(settings_webui.router, prefix="/api/v1")
app.include_router(agent_management.router, prefix="/api/v1")
app.include_router(scans.router, prefix="/api/v1")
app.include_router(queue.router, prefix="/api/v1")
app.dependency_overrides[spa.require_app_session] = user
app.dependency_overrides[personal_security.security_user] = user
app.dependency_overrides[require_api_admin] = admin

_base_lifespan = app.router.lifespan_context


@asynccontextmanager
async def browser_lifespan(application):
    async with _base_lifespan(application):
        async with fixture.engine.begin() as connection:
            await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[
                AgentSkill.__table__, RepoScan.__table__, ScanFinding.__table__,
                ReviewQueue.__table__, NotificationLockoutRound.__table__,
            ]))
        async with fixture.sessions() as db:
            db.add_all([
                RepoScan(id=1, repo_name="browser-fixture", repo_owner="owner", trigger_type="manual", status="completed", progress=100, overall_health_score=0, total_findings=1),
                ScanFinding(id=1, scan_id=1, file_path="example.py", line_start=12, severity="major", category="reliability", title="Fixture finding", description="A browser fixture finding.", suggestion="Review the fixture."),
                ReviewQueue(id=11, pr_id=42, repo_name="owner/browser-fixture", action="opened", status="failed", error_message="Fixture failure"),
                ReviewQueue(id=12, pr_id=43, repo_name="owner/browser-fixture", action="opened", status="completed"),
            ])
            await db.commit()
        redis = FixtureRedis()
        skills = AgentSkillService(Path(fixture.temporary.name) / "skills")
        workspaces = AgentTeamWorkspaceService(Path(fixture.temporary.name) / "workspaces")
        with (
            patch.object(AgentSkillService, "list_skills", _real_list_skills),
            patch.object(agent_management, "AgentSkillService", lambda: skills),
            patch.object(agent_management, "AgentTeamWorkspaceService", lambda: workspaces),
            patch.object(agent_management.team, "AgentTeamWorkspaceService", lambda: workspaces),
            patch.object(webauthn_service, "get_async_redis", AsyncMock(return_value=redis)),
            patch.object(fixture.settings, "get_async_redis", AsyncMock(return_value=redis)),
            patch.object(mfa_lockout_service, "get_async_redis", AsyncMock(return_value=redis)),
            patch.object(get_settings(), "passkeys_rp_id", "localhost"),
            patch.object(get_settings(), "passkeys_origin", "http://localhost:8768"),
            patch.object(get_settings(), "passkeys_enabled", True),
        ):
            yield


app.router.lifespan_context = browser_lifespan
