"""Telegram billing command helper tests"""

import pytest

from backend.telegram.handlers import _reply_if_payment_disabled


@pytest.mark.asyncio
async def test_reply_if_payment_disabled_handles_missing_message():
    class UpdateWithoutMessage:
        message = None

    assert await _reply_if_payment_disabled(UpdateWithoutMessage()) is True
