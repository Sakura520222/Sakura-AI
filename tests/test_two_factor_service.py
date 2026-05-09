"""Two-factor authentication helper tests."""

import pyotp
from unittest.mock import patch

from backend.services.two_factor_service import (
    TwoFactorReplayError,
    generate_recovery_codes,
    hash_recovery_code,
    normalize_recovery_code,
    verify_totp_secret,
)


def test_verify_totp_secret_returns_step_and_rejects_replay():
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()

    used_step = verify_totp_secret(secret, code)

    assert used_step is not None
    with patch("backend.services.two_factor_service.get_current_totp_step", return_value=used_step):
        try:
            verify_totp_secret(secret, code, last_used_step=used_step)
        except TwoFactorReplayError:
            pass
        else:
            raise AssertionError("Expected replay error")


def test_recovery_code_hash_normalizes_input():
    assert normalize_recovery_code(" abcd-1234 ") == "ABCD1234"
    assert hash_recovery_code("abcd-1234") == hash_recovery_code("ABCD1234")


def test_generate_recovery_codes_uses_configured_count():
    codes = generate_recovery_codes()

    assert len(codes) >= 4
    assert all("-" in code for code in codes)
