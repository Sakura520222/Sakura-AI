"""Ambient cookie authentication cannot replay a financial event without CSRF."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.api.v1 import deps
from backend.core.time_service import now_utc
from backend.models.admin_action_log import AdminActionLog
from backend.webui.auth import WEBUI_TOKEN_COOKIE_NAME
from backend.webui.deps import generate_csrf_token
from tests.test_billing_credits_api import billing_client as real_client_fixture

billing_client = real_client_fixture
real_get_api_current_user = deps.get_api_current_user


@pytest.fixture
def replay_auth(monkeypatch):
    payload = {"user_id": 3, "sub": "operator", "role": "super_admin"}
    monkeypatch.setattr(deps, "get_api_current_user", real_get_api_current_user)
    monkeypatch.setattr(
        deps,
        "decode_access_token",
        lambda token: (
            dict(payload) if token in {"test-cookie", "test-bearer"} else None
        ),
    )
    monkeypatch.setattr(
        deps, "is_access_token_payload", lambda value: value is not None
    )
    monkeypatch.setattr(
        deps, "refresh_login_claims", AsyncMock(side_effect=lambda value: value)
    )
    record = SimpleNamespace(
        id=10,
        provider="stripe",
        event_key="verified-test-event",
        status="processed",
        pending_reason=None,
        order_id=1,
        evidence={},
        received_at=now_utc(),
        resolved_at=now_utc(),
    )
    replay = AsyncMock(return_value=record)
    monkeypatch.setattr(
        "backend.services.payment_event_service.PaymentEventService.replay", replay
    )
    return replay


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer "])
@pytest.mark.parametrize("body", [None, {"ignored": "same-site form"}])
async def test_cookie_replay_without_csrf_cannot_reach_financial_sink(
    billing_client, replay_auth, authorization, body
):
    client, sessions = billing_client
    client.cookies.set(WEBUI_TOKEN_COOKIE_NAME, "test-cookie")
    headers = {"Origin": "https://sibling.example.org"}
    if authorization is not None:
        headers["Authorization"] = authorization
    response = await client.post(
        "/api/v1/billing/admin/payment-events/10/replay", data=body, headers=headers
    )
    assert response.status_code == 403
    replay_auth.assert_not_awaited()
    async with sessions() as db:
        assert (await db.execute(select(AdminActionLog))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["cookie", "cookie_empty_bearer", "bearer_with_cookie", "query"]
)
async def test_explicit_or_csrf_protected_replay_preserves_auth_transport(
    billing_client, replay_auth, mode
):
    client, sessions = billing_client
    headers, query = {}, ""
    if mode != "query":
        client.cookies.set(WEBUI_TOKEN_COOKIE_NAME, "test-cookie")
    if mode.startswith("cookie"):
        headers["X-CSRF-Token"] = generate_csrf_token()
        if mode == "cookie_empty_bearer":
            headers["Authorization"] = "Bearer "
    elif mode == "bearer_with_cookie":
        headers["Authorization"] = "Bearer test-bearer"
    else:
        query = "?token=test-bearer"
    response = await client.post(
        "/api/v1/billing/admin/payment-events/10/replay" + query, headers=headers
    )
    assert response.status_code == 200 and response.json()["processed"] is True
    replay_auth.assert_awaited_once_with(10)
    async with sessions() as db:
        audit = (await db.execute(select(AdminActionLog))).scalar_one()
        assert audit.admin_id == 3 and audit.action == "billing_replay_payment"


@pytest.mark.asyncio
async def test_invalid_nonempty_bearer_cannot_fall_back_to_a_valid_cookie(
    billing_client, replay_auth
):
    client, _ = billing_client
    client.cookies.set(WEBUI_TOKEN_COOKIE_NAME, "test-cookie")
    response = await client.post(
        "/api/v1/billing/admin/payment-events/10/replay",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert response.status_code == 401
    replay_auth.assert_not_awaited()
