"""Telegram billing command helper tests"""

from unittest.mock import AsyncMock, patch

import pytest

from backend.telegram.handlers import _reply_if_payment_disabled


@pytest.mark.asyncio
async def test_reply_if_payment_disabled_handles_missing_message():
    class UpdateWithoutMessage:
        message = None

    with patch(
        "backend.telegram.handlers.is_payment_enabled",
        new=AsyncMock(return_value=False),
    ) as mock_enabled:
        assert await _reply_if_payment_disabled(UpdateWithoutMessage()) is True

    mock_enabled.assert_awaited_once()
