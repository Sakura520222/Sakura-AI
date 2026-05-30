"""Behavior tests for require_csrf and require_csrf_header dependency functions."""

import pytest
from fastapi import HTTPException

from backend.webui.deps import require_csrf, require_csrf_header


@pytest.mark.asyncio
async def test_require_csrf_rejects_empty_token():
    with pytest.raises(HTTPException) as exc_info:
        await require_csrf(csrf_token="")

    assert exc_info.value.status_code == 403
    assert "CSRF" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_csrf_rejects_invalid_token():
    with pytest.raises(HTTPException) as exc_info:
        await require_csrf(csrf_token="invalid-token")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_csrf_accepts_valid_token():
    from backend.webui.deps import generate_csrf_token

    token = generate_csrf_token()
    result = await require_csrf(csrf_token=token)
    assert result == token


@pytest.mark.asyncio
async def test_require_csrf_header_rejects_invalid_token():
    with pytest.raises(HTTPException) as exc_info:
        await require_csrf_header(x_csrf_token="bad-token")

    assert exc_info.value.status_code == 403
    assert "CSRF" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_csrf_header_accepts_valid_token():
    from backend.webui.deps import generate_csrf_token

    token = generate_csrf_token()
    result = await require_csrf_header(x_csrf_token=token)
    assert result == token
