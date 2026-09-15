"""Regression tests for explicit asynchronous database session cleanup."""

import pytest

from backend.models import database as db_module
from backend.services.activity_observability.tool_service import ToolService
from backend.webui import deps as webui_deps


class _TrackingSession:
    def __init__(self):
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        self.exited += 1
        return False

    async def get(self, _model, _object_id):
        return None


@pytest.mark.asyncio
async def test_tool_service_closes_owned_session_after_early_return(monkeypatch):
    session = _TrackingSession()
    monkeypatch.setattr(db_module, "async_session", lambda: session)

    result = await ToolService().get_tool_execution(123)

    assert result is None
    assert session.entered == 1
    assert session.exited == 1


@pytest.mark.asyncio
async def test_require_auth_closes_mfa_session(monkeypatch):
    session = _TrackingSession()
    user = {"user_id": 7}
    monkeypatch.setattr(db_module, "async_session", lambda: session)

    async def fake_get_current_user(_request):
        return user

    async def fake_enforce_mfa(_request, current_user, db):
        assert current_user is user
        assert db is session

    monkeypatch.setattr(webui_deps, "get_current_user", fake_get_current_user)
    monkeypatch.setattr(webui_deps, "enforce_mfa_enrollment", fake_enforce_mfa)

    result = await webui_deps.require_auth(object())

    assert result is user
    assert session.entered == 1
    assert session.exited == 1
